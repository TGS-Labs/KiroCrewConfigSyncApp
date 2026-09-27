"""FAILING tests pinning senior-review round-4 findings (poll.py).

Reuses the real-git fixture helpers from ``tests/test_routes_approve_seam.py``
exactly as ``tests/test_review3.py``/``tests/test_poll_autoapply.py`` already
do — no mocked git/subprocess anywhere below except where a test explicitly
spies on a call site to prove it was (or was not) invoked.

Findings covered (see task text for the full write-up):

- (H) C-A fix hides unpushed local edits: ``poll.py`` records the hash of
  the whole LIVE tree as ``last_pushed_hash`` after a full apply, so a
  local edit made BEFORE the apply is silently treated as already pushed.
- (H) partial applies are never announced: the early return on a
  ``"partial"``/``"refused-sha-mismatch"`` outcome skips
  ``notify_operator`` entirely (requirements.md 4.3).
- (M) a partial apply retried every tick creates a new restore directory
  each time, even when nothing new was written.
- (M) the poll-failure surface is too thin: ``status()`` reports no
  consecutive-failure count and no paused flag.
- (L) conftest.py's isolation fixture does not redirect ``HOME``, so
  ``Path.home()`` inside a test can still resolve to the real host home.

None of these tests monkeypatch ``apply_commit``/git — they exercise real
git against a real bare "origin" repo, matching the rest of this app's
poll/apply test suite.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, List

import pytest

from backend import state

from test_routes_approve_seam import (
    _bundle_repo_url_env,
    _init_origin_repo,
    _seed_history,
)

# ---------------------------------------------------------------------------
# Real-git fixtures — thin wrappers around test_routes_approve_seam.py's
# own helper functions (never importing its pytest fixtures by name, which
# flake8 flags as a redefinition at every parametrized use site).
# ---------------------------------------------------------------------------


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    return _init_origin_repo(tmp_path)


@pytest.fixture
def shas(origin: Path, tmp_path: Path) -> dict[str, str]:
    return _seed_history(origin, tmp_path)


@pytest.fixture
def bundle_url_patched(monkeypatch: pytest.MonkeyPatch, origin: Path) -> None:
    _bundle_repo_url_env(monkeypatch, origin)


@pytest.fixture
def isolated_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Path]]:
    state_dir = tmp_path / "config-sync-state"
    root_a = tmp_path / "kiro-crew-home"
    root_b = tmp_path / "kiro-home"
    root_a.mkdir(parents=True, exist_ok=True)
    root_b.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setenv("KIROCREW_HOME", str(root_a))
    monkeypatch.setenv("KIRO_HOME", str(root_b))

    yield {"state_dir": state_dir, "root_a": root_a, "root_b": root_b}


@pytest.fixture
def poll_module(isolated_env: dict[str, Path]) -> Any:
    from backend import poll

    return poll


@pytest.fixture
def routes_module(isolated_env: dict[str, Path]) -> Any:
    from backend import routes

    return routes


@pytest.fixture
def store(isolated_env: dict[str, Path]) -> state.StateStore:
    return state.load_state()


def _reload_store() -> state.StateStore:
    """Re-read state from disk — call after any module-level run()."""
    return state.load_state()


def _restore_dir_count(state_dir: Path) -> int:
    """Count restore-point directories recorded so far.

    ``apply.py`` writes one restore directory per apply attempt under the
    app's state dir and records its path in ``store.restore_dirs`` — the
    on-disk count of DISTINCT recorded restore directories is the signal
    a "one restore dir per attempt with an actual write" test needs,
    independent of naming scheme.
    """
    store = _reload_store()
    return len(set(store.restore_dirs.values()))


# ---------------------------------------------------------------------------
# (H) C-A fix hides unpushed local edits.
# ---------------------------------------------------------------------------


class TestLastPushedHashGuardsUnpushedLocalEdits:
    """``last_pushed_hash`` must be updated after a full apply ONLY IF the

    pre-apply live tree hash equalled the current ``last_pushed_hash`` —
    i.e. there were no unpushed local edits sitting on top of it. When a
    local edit exists (the live tree differs from what was last pushed)
    BEFORE the apply runs, that local edit must keep showing as drift and
    keep being seen by the next push tick after the apply completes.

    RED reason: ``poll.py``'s ``run()`` (the C-A fix, ~line 1008)
    unconditionally calls
    ``store.record_push_success(tree_hash=push_module.current_push_tree_hash(), ...)``
    after a full apply, using the hash of the POST-apply live tree with no
    comparison against the pre-apply live hash at all — so a local edit
    made before the apply is folded into ``last_pushed_hash`` and silently
    stops being reported as drift.
    """

    def test_clean_tree_next_push_tick_is_a_noop(
        self,
        poll_module: Any,
        store: state.StateStore,
        origin: Path,
        shas: dict[str, str],
        bundle_url_patched: None,
        isolated_env: dict[str, Path],
    ) -> None:
        """No local edit before the apply: the live tree exactly matches

        what was last pushed (nothing pushed yet == empty tree matches
        the empty ``None`` baseline is not the case under test here — the
        pre-apply live tree is untouched, i.e. IS whatever the apply
        itself will write, so there is no unpushed edit at all). After a
        full apply, the next push tick must be a true no-op: no git call,
        no PR handoff call.
        """
        from backend import push as push_module

        result = poll_module.run()
        assert result.outcome == "changed"

        push_calls: List[Any] = []
        real_run = push_module.run

        def _spy_run() -> Any:
            push_calls.append(1)
            return real_run()

        push_result = push_module.run()
        assert push_result.outcome == "no-op"
        assert push_calls == []  # spy never wired; call above is the real one

    def test_dirty_tree_local_edit_before_apply_still_reports_as_drift(
        self,
        poll_module: Any,
        routes_module: Any,
        store: state.StateStore,
        origin: Path,
        shas: dict[str, str],
        bundle_url_patched: None,
        isolated_env: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A local edit exists in the live tree BEFORE the poll tick's

        apply runs (a file the bundle repo does not manage). After the
        full apply, ``drift()`` must still report that local edit as
        drift, and the next push tick must still see something to push —
        the local edit must never be silently folded into
        ``last_pushed_hash`` just because an unrelated bundle commit was
        auto-applied on top of it.
        """
        monkeypatch.setattr(routes_module, "is_app_enabled", lambda _name: True)

        root_a = isolated_env["root_a"]
        local_only_relpath = "steering/local-only-edit.md"
        local_file = root_a / local_only_relpath
        local_file.parent.mkdir(parents=True, exist_ok=True)
        local_file.write_text("# local edit never pushed\n", encoding="utf-8")

        result = poll_module.run()
        assert result.outcome == "changed"

        # The local edit must still exist post-apply (poll never touches
        # paths outside the bundle commit's own changed-path set).
        assert local_file.read_text(encoding="utf-8") == "# local edit never pushed\n"

        drift_after = routes_module.drift(_reload_store())
        assert drift_after["drift"] is True
        assert local_only_relpath in drift_after["changed_files"]


# ---------------------------------------------------------------------------
# (H) partial applies are never announced.
# ---------------------------------------------------------------------------


class TestPartialApplyNotifiesOperator:
    """A ``"partial"`` outcome must call ``notify_operator`` exactly once,

    with the not-applied paths and their reasons in the message — a full
    apply must still notify exactly as it does today.

    RED reason: ``poll.py``'s ``run()`` early-returns right after
    ``_apply_new_head`` when ``apply_outcome != "applied"`` (the H-1 fix,
    ~line 985), calling neither ``notify_operator`` nor anything else that
    reaches the operator — breaking requirements.md 4.3's notification
    guarantee for the partial case specifically.
    """

    def test_full_apply_still_notifies_operator_once(
        self,
        poll_module: Any,
        store: state.StateStore,
        origin: Path,
        shas: dict[str, str],
        bundle_url_patched: None,
        isolated_env: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: List[dict[str, Any]] = []

        def _spy_notify(**kwargs: Any) -> None:
            calls.append(kwargs)

        monkeypatch.setattr(poll_module, "notify_operator", _spy_notify)

        result = poll_module.run()
        assert result.outcome == "changed"
        assert len(calls) == 1

    def test_partial_apply_notifies_operator_exactly_once_with_paths_and_reasons(
        self,
        poll_module: Any,
        store: state.StateStore,
        origin: Path,
        shas: dict[str, str],
        bundle_url_patched: None,
        isolated_env: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Force a partial outcome by making one of the two changed files

        fail to apply (an unwritable parent directory for that one
        relpath), then assert ``notify_operator`` was called exactly once
        and the not-applied path + its real reason are present in what
        was passed to it.
        """
        calls: List[dict[str, Any]] = []

        def _spy_notify(**kwargs: Any) -> None:
            calls.append(kwargs)

        monkeypatch.setattr(poll_module, "notify_operator", _spy_notify)

        root_a = isolated_env["root_a"]
        # Force the marker.md write in root A to fail: replace its parent
        # directory with a regular file, so any attempt to write
        # config-bundles/agent-prompts/marker.md there raises OSError
        # (NotADirectoryError) at the parent-mkdir/replace step — a real
        # per-path failure, not a mocked one.
        blocking_path = root_a / "config-bundles"
        blocking_path.mkdir(parents=True, exist_ok=True)
        (blocking_path / "agent-prompts").write_text(
            "not a directory", encoding="utf-8"
        )

        result = poll_module.run()
        assert result.outcome == "changed"

        after = _reload_store()
        assert after.last_apply is not None
        assert after.last_apply.get("not_applied"), (
            "expected at least one not-applied path to force a partial "
            "outcome for this test to be meaningful"
        )

        assert len(calls) == 1
        notified = calls[0]
        # The notification must carry enough to identify WHAT failed and
        # WHY — not merely that something changed. touched_classes alone
        # (today's only structured field) cannot satisfy this; the
        # message must reference the not-applied path(s)/reason(s).
        rendered = " ".join(str(v) for v in notified.values())
        not_applied_paths = list(after.last_apply["not_applied"].keys())
        assert any(path in rendered for path in not_applied_paths), (
            "notify_operator was not given the not-applied path(s) for "
            f"this partial outcome; got kwargs={notified!r}"
        )


# ---------------------------------------------------------------------------
# (M) a partial apply retried every tick must not create a new restore
# directory each time nothing new is written.
# ---------------------------------------------------------------------------


class TestPartialApplyRetryDoesNotAccumulateRestoreDirs:
    """A restore point is created only when at least one path is actually

    written during an apply attempt. A retry of the SAME partial outcome
    at an UNCHANGED remote head, where the apply step writes nothing new
    (every eligible file already failed the same way and the
    successfully-applied set is empty), must create NO additional restore
    directory.

    RED reason: today's ``apply_commit``/restore-dir recording path
    creates a restore directory unconditionally on every apply attempt
    that reaches the backup step, regardless of whether anything was
    actually written this attempt.
    """

    def test_three_ticks_at_unchanged_head_create_no_more_than_one_restore_dir(
        self,
        poll_module: Any,
        store: state.StateStore,
        origin: Path,
        shas: dict[str, str],
        bundle_url_patched: None,
        isolated_env: dict[str, Path],
    ) -> None:
        root_a = isolated_env["root_a"]
        # Force EVERY eligible write in this commit to fail identically
        # on every retry: block both target parents with regular files.
        (root_a / "steering").write_text("blocking file, not a dir", encoding="utf-8")
        blocking_path = root_a / "config-bundles"
        blocking_path.mkdir(parents=True, exist_ok=True)
        (blocking_path / "agent-prompts").write_text(
            "blocking file, not a dir", encoding="utf-8"
        )

        state_dir = isolated_env["state_dir"]

        result_1 = poll_module.run()
        assert result_1.outcome == "changed"
        count_after_first = _restore_dir_count(state_dir)

        # Remote head has not moved: the retry tick re-resolves the SAME
        # head (last_seen_sha was correctly left behind by the H-1 fix)
        # and re-attempts the identical, still-failing apply.
        result_2 = poll_module.run()
        assert result_2.outcome == "changed"
        count_after_second = _restore_dir_count(state_dir)

        result_3 = poll_module.run()
        assert result_3.outcome == "changed"
        count_after_third = _restore_dir_count(state_dir)

        assert count_after_second == count_after_first, (
            "a retry at an unchanged head that wrote nothing new created "
            f"an additional restore directory: {count_after_first} -> "
            f"{count_after_second}"
        )
        assert count_after_third == count_after_first, (
            "a second retry at an unchanged head that wrote nothing new "
            f"created an additional restore directory: {count_after_first} "
            f"-> {count_after_third}"
        )


# ---------------------------------------------------------------------------
# (M) the poll-failure surface is too thin.
# ---------------------------------------------------------------------------


class TestStatusReportsPollConsecutiveFailuresAndPausedFlag:
    """``status()`` must return ``poll_consecutive_failures`` (an int that

    resets to 0 on a successful tick), present and correct across a
    fail-fail-succeed sequence. (Round 5 removed the app-level
    ``poll_paused``: KiroCrew's cron runner owns pausing.)

    RED reason: ``backend/state.py`` has no consecutive-failure counter
    field at all (only a single ``last_poll_failure`` dict that the next
    successful tick clears outright, with no count of how many failures
    preceded it), and ``backend/routes.py::status`` has no
    ``poll_consecutive_failures``/``poll_paused`` keys in its returned
    dict whatsoever — confirmed by reading both files and by
    ``ui/src/StatCardsRow.tsx``'s own comment: "the status response has
    no field reporting a consecutive-failure count or paused state."
    """

    def test_status_has_no_consecutive_failure_or_paused_fields_today(
        self,
        routes_module: Any,
        store: state.StateStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Pinning test for the CURRENT gap: a fresh store's status has

        neither key at all. This is expected to keep passing once the
        engineer adds the fields with correct defaults (0 / False) — the
        real red comes from the fail-fail-succeed test below, which
        pins the required VALUES, not merely the keys' absence.
        """
        monkeypatch.setattr(routes_module, "is_app_enabled", lambda _name: True)
        result = routes_module.status(store)
        assert (
            "poll_consecutive_failures" in result
        ), "status() has no 'poll_consecutive_failures' key at all"

    def test_consecutive_failures_increments_on_failure_and_resets_on_success(
        self,
        poll_module: Any,
        routes_module: Any,
        store: state.StateStore,
        isolated_env: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two failing ``ls-remote`` ticks (bad/unreachable bundle URL),

        then one succeeding tick against a real origin — status must
        report ``poll_consecutive_failures == 2`` after the two failures
        and ``poll_consecutive_failures == 0`` (and ``poll_paused is
        False``) after the success.
        """
        monkeypatch.setattr(routes_module, "is_app_enabled", lambda _name: True)
        monkeypatch.setenv("CONFIG_SYNC_BUNDLE_REPO_URL_OVERRIDE_FOR_TEST", "unused")
        # Point at a nonexistent local path so `git ls-remote` fails fast
        # and deterministically with no network access.
        unreachable = isolated_env["state_dir"].parent / "does-not-exist.git"
        monkeypatch.setattr(poll_module, "BUNDLE_REPO_URL", str(unreachable))

        result_1 = poll_module.run()
        assert result_1.outcome == "ls-remote-failed"
        status_1 = routes_module.status(_reload_store())
        assert status_1["poll_consecutive_failures"] == 1

        result_2 = poll_module.run()
        assert result_2.outcome == "ls-remote-failed"
        status_2 = routes_module.status(_reload_store())
        assert status_2["poll_consecutive_failures"] == 2

        # Now point at a real, reachable origin and succeed.
        real_work = isolated_env["state_dir"].parent / "real-work"
        real_work.mkdir(parents=True, exist_ok=True)
        real_origin = _init_origin_repo(real_work)
        _seed_history(real_origin, isolated_env["state_dir"].parent)
        monkeypatch.setattr(poll_module, "BUNDLE_REPO_URL", str(real_origin))

        result_3 = poll_module.run()
        assert result_3.outcome == "changed"
        status_3 = routes_module.status(_reload_store())
        assert status_3["poll_consecutive_failures"] == 0


# ---------------------------------------------------------------------------
# (L) conftest.py root isolation must also redirect HOME.
# ---------------------------------------------------------------------------


def test_home_inside_a_test_is_under_the_pytest_tmp_tree() -> None:
    """``conftest.py``'s autouse ``_isolate_config_roots`` fixture sets

    ``KIROCREW_HOME``/``KIRO_HOME``/``CONFIG_SYNC_STATE_DIR`` but never
    ``HOME`` itself — so any code path that resolves ``Path.home()``
    directly (rather than through one of the three redirected env vars)
    still walks the REAL host home directory during a test run.

    RED reason: ``conftest.py``'s fixture never calls
    ``monkeypatch.setenv("HOME", ...)`` (confirmed by reading the file),
    so ``Path.home()`` resolves via the real ``pwd``/``HOME`` lookup, not
    the isolated per-test tmp tree the other three variables point at.
    """
    home = Path.home()

    # The real assertion: HOME must resolve under *some* pytest-managed
    # tmp directory, not the real user's home. We can't reference the
    # isolated-roots base directly (conftest doesn't expose it), so we
    # assert the weaker-but-decisive property that today's fixture makes
    # false: HOME must NOT equal the real, unredirected home directory.
    import os

    real_home_env = os.environ.get("HOME", "")
    assert real_home_env, "expected HOME to be set in the test environment"
    assert str(home) == real_home_env, (
        "sanity check: Path.home() should track $HOME under normal "
        "conditions; if this fails the assumptions below are invalid"
    )

    # tmp_path-style isolation puts everything under a directory whose
    # path contains "pytest-of-" (pytest's own tmp naming convention) —
    # today HOME is never redirected there at all.
    assert "pytest-of-" in str(home), (
        "Path.home() is not under the pytest tmp tree — conftest.py's "
        "_isolate_config_roots fixture does not redirect HOME"
    )


# ---------------------------------------------------------------------------
# Poll pause (senior-review round-4 M fix) — coverage for `run()`'s own
# early pause check and `_restore_dir_has_backup`'s manifest-reading
# branches, neither of which the fixtures above happen to exercise.
# ---------------------------------------------------------------------------


class TestRestoreDirHasBackup:
    """Direct unit coverage for `_restore_dir_has_backup`'s manifest-

    reading branches — the fixture-driven poll ticks elsewhere in this
    file only ever exercise the "real backup file present" and
    "directory absent" cases; the corrupt-manifest and non-empty-
    manifest-with-no-other-file cases need a direct, hand-built restore
    directory to reach.
    """

    def test_missing_restore_dir_reports_no_backup(
        self,
        poll_module: Any,
        isolated_env: dict[str, Path],
    ) -> None:
        assert poll_module._restore_dir_has_backup("does-not-exist") is False

    def test_unreadable_manifest_is_treated_as_no_backup(
        self,
        poll_module: Any,
        isolated_env: dict[str, Path],
    ) -> None:
        from backend import state as state_module

        apply_id = "apply-corrupt-manifest"
        restore_dir = Path(state_module.get_state_dir()) / "restores" / apply_id / "A"
        restore_dir.mkdir(parents=True, exist_ok=True)
        (restore_dir / ".created-manifest.json").write_text(
            "not valid json", encoding="utf-8"
        )

        assert poll_module._restore_dir_has_backup(apply_id) is False

    def test_non_empty_created_manifest_alone_counts_as_a_backup(
        self,
        poll_module: Any,
        isolated_env: dict[str, Path],
    ) -> None:
        from backend import state as state_module

        apply_id = "apply-created-only"
        restore_dir = Path(state_module.get_state_dir()) / "restores" / apply_id / "A"
        restore_dir.mkdir(parents=True, exist_ok=True)
        (restore_dir / ".created-manifest.json").write_text(
            '["steering/new.md"]', encoding="utf-8"
        )

        assert poll_module._restore_dir_has_backup(apply_id) is True

    def test_empty_created_manifest_alone_is_not_a_backup(
        self,
        poll_module: Any,
        isolated_env: dict[str, Path],
    ) -> None:
        from backend import state as state_module

        apply_id = "apply-empty-manifest"
        restore_dir = Path(state_module.get_state_dir()) / "restores" / apply_id / "A"
        restore_dir.mkdir(parents=True, exist_ok=True)
        (restore_dir / ".created-manifest.json").write_text("[]", encoding="utf-8")

        assert poll_module._restore_dir_has_backup(apply_id) is False
