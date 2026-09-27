"""Tests for backend/state.py — durable app state (task 1.4).

Covers Requirements 2.2, 2.6, 2.7, 4.6, 4.7:
- One JSON document under the app's OWN state directory, never inside
  either tracked configuration root (KIROCREW_HOME / KIRO_HOME).
- Persists last_pushed_hash, last_push, last_seen_sha, pending, a bounded
  history, and restore_dirs.
- Writes are atomic: no partial/corrupt state file survives a simulated
  interrupted write.
- A failed push leaves last_pushed_hash unchanged.

This module intentionally imports `state` (backend/state.py), which does not
exist yet. All tests below are expected to fail with a collection-time
ImportError / ModuleNotFoundError until software-engineer implements it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator

import pytest

from backend import state


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Path]]:
    """Point both tracked configuration roots at disposable directories,
    and isolate the app's own state directory too.

    KIROCREW_HOME (root A) and KIRO_HOME (root B) are the two trees this app
    tracks and commits from. The app's own state directory must resolve
    OUTSIDE both, so tests give each an unambiguous, distinct path under
    tmp_path and assert the state directory is not a descendant of either.

    The default state directory (``~/.config-sync/state``) is a fixed,
    real-home location by design (Requirement 2.2) — it no longer varies
    with KIROCREW_HOME / KIRO_HOME. Tests therefore isolate it explicitly
    via CONFIG_SYNC_STATE_DIR so state from one test run never leaks into
    another or touches the real ``~/.config-sync``.
    """
    root_a = tmp_path / "kirocrew_home"
    root_b = tmp_path / "kiro_home"
    root_a.mkdir()
    root_b.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(root_a))
    monkeypatch.setenv("KIRO_HOME", str(root_b))
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(tmp_path / "state"))
    yield {"root_a": root_a, "root_b": root_b, "tmp_path": tmp_path}


@pytest.fixture
def state_store(isolated_roots: dict[str, Path]) -> Any:
    """A fresh state store instance/module state, isolated per test."""
    return state.load_state()


def _is_descendant(candidate: Path, ancestor: Path) -> bool:
    """True if `candidate` is `ancestor` itself or lives under it."""
    candidate = candidate.resolve()
    ancestor = ancestor.resolve()
    return candidate == ancestor or ancestor in candidate.parents


# ---------------------------------------------------------------------------
# State directory location — Requirement 2.2 / design.md state.py component
# ---------------------------------------------------------------------------


class TestStateDirectoryLocation:
    def test_state_dir_is_outside_kirocrew_home_root(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        state_dir = Path(state.get_state_dir())
        assert not _is_descendant(state_dir, isolated_roots["root_a"]), (
            "app state directory must never live inside KIROCREW_HOME "
            "(root A) or it could be swept into a tracked-tree commit"
        )

    def test_state_dir_is_outside_kiro_home_root(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        state_dir = Path(state.get_state_dir())
        assert not _is_descendant(state_dir, isolated_roots["root_b"]), (
            "app state directory must never live inside KIRO_HOME "
            "(root B) or it could be swept into a tracked-tree commit"
        )

    def test_state_dir_is_outside_real_default_kiro_home_when_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reproduces the real-host scenario (checkpoint 1.5): KIRO_HOME is
        genuinely absent from the environment (the common case — most hosts
        never set it explicitly), and only KIROCREW_HOME is configured.

        get_state_dir() must still resolve OUTSIDE the default KIRO_HOME
        (``home / ".kiro"``) per requirements.md 2.2. The fixed, independent
        ``<home>/.config-sync/state`` location satisfies this by naming
        rather than by any relationship to KIRO_HOME's value, so this holds
        regardless of whether KIRO_HOME is set. A fake home under
        ``tmp_path`` keeps this isolated from the real user home.
        """
        fake_home = tmp_path / "fake-home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "kirocrew_home"))
        monkeypatch.delenv("KIRO_HOME", raising=False)
        monkeypatch.delenv("CONFIG_SYNC_STATE_DIR", raising=False)

        state_dir = Path(state.get_state_dir())
        default_kiro_home = fake_home / ".kiro"

        assert not _is_descendant(state_dir, default_kiro_home), (
            "with KIRO_HOME unset, get_state_dir() must not resolve to a "
            "descendant of the default KIRO_HOME (~/.kiro)"
        )

    def test_config_sync_state_dir_override_is_honored_verbatim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CONFIG_SYNC_STATE_DIR, when set, is used as-is — the explicit
        operator/test override always wins over the fixed default."""
        override_dir = tmp_path / "explicit-override"
        monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(override_dir))

        state_dir = Path(state.get_state_dir())

        assert state_dir == override_dir
        assert state_dir.is_dir()

    def test_default_state_dir_does_not_depend_on_tracked_root_values(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The default state directory is a fixed, named location — it must
        not shift when KIROCREW_HOME / KIRO_HOME point at unusual or
        colliding values (e.g. both pointing at the same directory, or one
        nested inside the other). This replaces the removed walk-up-anchor
        mechanism's pathological-input test: the new contract has no
        anchor-walk to loop, so the property to prove is that the default
        is stable and independent of these env vars entirely. A fake home
        under ``tmp_path`` keeps this isolated from the real user home."""
        fake_home = tmp_path / "fake-home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        monkeypatch.delenv("CONFIG_SYNC_STATE_DIR", raising=False)
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.delenv("KIRO_HOME", raising=False)
        baseline = Path(state.get_state_dir())

        monkeypatch.setenv("KIROCREW_HOME", "/")
        monkeypatch.setenv("KIRO_HOME", "/")
        same_root_state_dir = Path(state.get_state_dir())

        assert same_root_state_dir == baseline
        assert same_root_state_dir == Path.home() / ".config-sync" / "state"

    def test_state_file_lives_under_the_state_dir(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        state_dir = Path(state.get_state_dir())
        state_path = Path(state.get_state_path())
        assert _is_descendant(state_path, state_dir)

    def test_state_file_is_a_single_json_document(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.record_push_success(
            tree_hash="abc123",
            branch="config-sync/instance-abc123",
            pr_url="https://github.com/TGS-Labs/Kiro-Config-Bundles/pull/1",
        )
        state_path = Path(state.get_state_path())
        assert state_path.is_file()
        assert state_path.suffix == ".json"
        # A single parse must succeed and it must be the ONE document
        # carrying every field this task lists.
        with open(state_path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        for key in (
            "last_pushed_hash",
            "last_push",
            "last_seen_sha",
            "pending",
            "history",
            "restore_dirs",
        ):
            assert key in payload, f"missing '{key}' in the single state document"


# ---------------------------------------------------------------------------
# Persisted fields — last_pushed_hash, last_push
# ---------------------------------------------------------------------------


class TestLastPushedHashAndLastPush:
    def test_record_push_success_persists_hash_and_push_metadata(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.record_push_success(
            tree_hash="deadbeef",
            branch="config-sync/instance-deadbeef",
            pr_url="https://github.com/TGS-Labs/Kiro-Config-Bundles/pull/42",
        )

        reloaded = state.load_state()
        assert reloaded.last_pushed_hash == "deadbeef"
        last_push = reloaded.last_push
        assert last_push is not None
        assert last_push["branch"] == "config-sync/instance-deadbeef"
        assert (
            last_push["pr_url"]
            == "https://github.com/TGS-Labs/Kiro-Config-Bundles/pull/42"
        )
        assert "time" in last_push

    def test_last_pushed_hash_survives_reload_from_disk(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.record_push_success(
            tree_hash="cafef00d", branch="config-sync/instance-cafef00d", pr_url=None
        )
        del store

        reloaded = state.load_state()
        assert reloaded.last_pushed_hash == "cafef00d"


# ---------------------------------------------------------------------------
# Failed push leaves last_pushed_hash unchanged — Requirements 2.6, 2.7
# ---------------------------------------------------------------------------


class TestFailedPushLeavesHashUnchanged:
    def test_record_push_failure_does_not_change_last_pushed_hash(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.record_push_success(
            tree_hash="original-hash",
            branch="config-sync/instance-original-hash",
            pr_url="https://github.com/TGS-Labs/Kiro-Config-Bundles/pull/1",
        )

        store.record_push_failure(reason="protected branch refused")

        assert store.last_pushed_hash == "original-hash"

    def test_record_push_failure_does_not_change_hash_when_none_set_yet(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        assert store.last_pushed_hash is None

        store.record_push_failure(reason="secret scan finding")

        assert store.last_pushed_hash is None

    def test_failed_push_is_reflected_on_disk_not_only_in_memory(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.record_push_success(
            tree_hash="stable-hash",
            branch="config-sync/instance-stable-hash",
            pr_url="https://github.com/TGS-Labs/Kiro-Config-Bundles/pull/2",
        )
        store.record_push_failure(reason="clone failed")

        reloaded = state.load_state()
        assert reloaded.last_pushed_hash == "stable-hash"

    def test_record_push_failure_surfaces_the_cause(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.record_push_failure(reason="ambiguous target branch")

        last_failure = store.last_push_failure
        assert last_failure is not None
        assert last_failure["reason"] == "ambiguous target branch"
        assert "time" in last_failure


# ---------------------------------------------------------------------------
# record_poll_failure — senior-review round-3 M2/L1: a persistently
# failing poll tick must be visible in the app's own state, mirroring
# record_push_failure's shape for push's equivalent failure.
# ---------------------------------------------------------------------------


class TestRecordPollFailure:
    def test_last_poll_failure_defaults_to_none(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        assert store.last_poll_failure is None

    def test_record_poll_failure_surfaces_the_cause(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.record_poll_failure(reason="git clone failed: unreachable remote")

        last_failure = store.last_poll_failure
        assert last_failure is not None
        assert last_failure["reason"] == "git clone failed: unreachable remote"
        assert "time" in last_failure

    def test_record_poll_failure_does_not_change_last_seen_sha(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.record_seen_sha("stable-sha")

        store.record_poll_failure(reason="fetch failed")

        assert store.last_seen_sha == "stable-sha"

    def test_poll_failure_is_reflected_on_disk_not_only_in_memory(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.record_poll_failure(reason="classification raised")

        reloaded = state.load_state()
        assert reloaded.last_poll_failure is not None
        assert reloaded.last_poll_failure["reason"] == "classification raised"


# ---------------------------------------------------------------------------
# clear_poll_failure — senior-review round-4 M2: last_poll_failure must not
# stay stale forever after the poll recovers; a successful tick clears it.
# ---------------------------------------------------------------------------


class TestClearPollFailure:
    def test_clear_poll_failure_resets_to_none(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.record_poll_failure(reason="git clone failed: unreachable remote")
        assert store.last_poll_failure is not None

        store.clear_poll_failure()

        assert store.last_poll_failure is None

    def test_clear_poll_failure_persists_across_reload(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.record_poll_failure(reason="fetch failed")
        store.clear_poll_failure()

        reloaded = state.load_state()
        assert reloaded.last_poll_failure is None

    def test_clear_poll_failure_is_a_no_op_when_already_clear(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        assert store.last_poll_failure is None

        store.clear_poll_failure()

        assert store.last_poll_failure is None

    def test_clear_poll_failure_does_not_change_last_seen_sha(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.record_seen_sha("stable-sha")
        store.record_poll_failure(reason="fetch failed")

        store.clear_poll_failure()

        assert store.last_seen_sha == "stable-sha"


# ---------------------------------------------------------------------------
# last_seen_sha (poll direction)
# ---------------------------------------------------------------------------


class TestLastSeenSha:
    def test_last_seen_sha_defaults_to_none_before_any_poll(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        assert store.last_seen_sha is None

    def test_record_seen_sha_persists_across_reload(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.record_seen_sha("0123456789abcdef0123456789abcdef01234567")

        reloaded = state.load_state()
        assert reloaded.last_seen_sha == "0123456789abcdef0123456789abcdef01234567"

    def test_record_seen_sha_overwrites_the_previous_value(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.record_seen_sha("1111111111111111111111111111111111111111")
        store.record_seen_sha("2222222222222222222222222222222222222222")

        reloaded = state.load_state()
        assert reloaded.last_seen_sha == "2222222222222222222222222222222222222222"


# ---------------------------------------------------------------------------
# pending record
# ---------------------------------------------------------------------------


class TestPending:
    def test_no_pending_record_by_default(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        assert store.pending is None

    def test_set_pending_persists_sha_author_subject_and_classified_paths(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.set_pending(
            sha="abcdef0123456789abcdef0123456789abcdef01",
            author="staneslevski",
            subject="Add new steering file",
            classified_paths={"steering/new-rule.md": "steering"},
        )

        reloaded = state.load_state()
        pending = reloaded.pending
        assert pending is not None
        assert pending["sha"] == "abcdef0123456789abcdef0123456789abcdef01"
        assert pending["author"] == "staneslevski"
        assert pending["subject"] == "Add new steering file"
        assert pending["classified_paths"] == {"steering/new-rule.md": "steering"}

    def test_clear_pending_removes_the_pending_record(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.set_pending(
            sha="abcdef0123456789abcdef0123456789abcdef01",
            author="staneslevski",
            subject="Add new steering file",
            classified_paths={"steering/new-rule.md": "steering"},
        )
        store.clear_pending()

        reloaded = state.load_state()
        assert reloaded.pending is None

    def test_declining_a_pending_commit_does_not_re_add_it_on_reload(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        # Requirement 4.6: decline leaves configuration byte-unchanged and
        # does not resurrect the same pending commit on its own.
        store = state.load_state()
        store.set_pending(
            sha="fedcba9876543210fedcba9876543210fedcba98",
            author="staneslevski",
            subject="Rotate a token",
            classified_paths={},
        )
        store.clear_pending()

        reloaded_again = state.load_state()
        assert reloaded_again.pending is None


# ---------------------------------------------------------------------------
# base_sha — the decision-boundary field (requirements.md 4.9,
# Kiro-Config-Bundles#65).
# ---------------------------------------------------------------------------


class TestBaseSha:
    def test_base_sha_is_none_before_any_pending_record(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        assert store.base_sha is None

    def test_set_pending_on_a_fresh_record_sets_base_sha_to_the_commit(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        """requirements.md 4.9: "When there is no existing pending record,

        starting a new one sets base_sha to the current head."
        """
        store = state.load_state()
        store.set_pending(
            sha="1111111111111111111111111111111111111111",
            author="staneslevski",
            subject="first commit",
            classified_paths={"steering/a.md": "steering"},
        )

        reloaded = state.load_state()
        assert reloaded.base_sha == "1111111111111111111111111111111111111111"

    def test_accumulate_pending_merges_classified_paths_keyed_by_path(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        """A path present in both the existing pending record and the new

        tick's classification takes the NEW classification (requirements.md
        4.9: "keyed by path so a path changed in both ranges reflects the
        latest classification"), while a path unique to either side is
        preserved.
        """
        store = state.load_state()
        store.set_pending(
            sha="1111111111111111111111111111111111111111",
            author="alice",
            subject="commit A",
            classified_paths={
                "steering/a.md": "steering",
                "config.json": "next_resolution",
            },
        )

        store.accumulate_pending(
            sha="2222222222222222222222222222222222222222",
            author="bob",
            subject="commit B",
            classified_paths={
                "steering/b.md": "steering",
                "config.json": "live_now",
            },
        )

        reloaded = state.load_state()
        pending = reloaded.pending
        assert pending is not None
        assert pending["classified_paths"] == {
            "steering/a.md": "steering",
            "steering/b.md": "steering",
            "config.json": "live_now",
        }, "config.json must take commit B's (the newer) classification"

    def test_accumulate_pending_does_not_advance_base_sha(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        """requirements.md 4.9: "base_sha SHALL NOT advance while a commit

        is pending."
        """
        store = state.load_state()
        store.set_pending(
            sha="1111111111111111111111111111111111111111",
            author="alice",
            subject="commit A",
            classified_paths={"steering/a.md": "steering"},
        )
        store.accumulate_pending(
            sha="2222222222222222222222222222222222222222",
            author="bob",
            subject="commit B",
            classified_paths={"steering/b.md": "steering"},
        )

        reloaded = state.load_state()
        assert reloaded.base_sha == "1111111111111111111111111111111111111111"

    def test_accumulate_pending_updates_sha_author_and_subject_to_the_newest(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        """requirements.md 4.9: "The pending record's `sha` field SHALL

        always reflect the newest head seen."
        """
        store = state.load_state()
        store.set_pending(
            sha="1111111111111111111111111111111111111111",
            author="alice",
            subject="commit A",
            classified_paths={"steering/a.md": "steering"},
        )
        store.accumulate_pending(
            sha="2222222222222222222222222222222222222222",
            author="bob",
            subject="commit B",
            classified_paths={"steering/b.md": "steering"},
        )

        reloaded = state.load_state()
        assert reloaded.pending is not None
        assert reloaded.pending["sha"] == "2222222222222222222222222222222222222222"
        assert reloaded.pending["author"] == "bob"
        assert reloaded.pending["subject"] == "commit B"

    def test_accumulate_pending_unions_ignored_paths_and_touched_classes(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.set_pending(
            sha="1111111111111111111111111111111111111111",
            author="alice",
            subject="commit A",
            classified_paths={"steering/a.md": "steering"},
            ignored_paths=["memory.db"],
            touched_classes=["steering"],
        )
        store.accumulate_pending(
            sha="2222222222222222222222222222222222222222",
            author="bob",
            subject="commit B",
            classified_paths={"config.json": "next_resolution"},
            ignored_paths=[".env"],
            touched_classes=["next_resolution"],
        )

        reloaded = state.load_state()
        pending = reloaded.pending
        assert pending is not None
        assert set(pending["ignored_paths"]) == {"memory.db", ".env"}
        assert pending["touched_classes"] == ["next_resolution", "steering"]

    def test_accumulate_pending_raises_when_nothing_is_pending(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        """Calling `accumulate_pending` with no existing pending record is a

        caller bug (should have called `set_pending`), not a recoverable
        state — it must raise rather than silently fabricate a record.
        """
        store = state.load_state()
        with pytest.raises(ValueError):
            store.accumulate_pending(
                sha="1111111111111111111111111111111111111111",
                author="alice",
                subject="commit A",
                classified_paths={"steering/a.md": "steering"},
            )

    def test_advance_base_sha_moves_it_to_the_given_sha(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        """requirements.md 4.9: base_sha advances only on an operator

        approve/decline, to the SHA just decided. No approve/decline route
        exists yet (Deployment 4) — this exercises `advance_base_sha`
        directly as the primitive Deployment 4 will call.
        """
        store = state.load_state()
        store.set_pending(
            sha="1111111111111111111111111111111111111111",
            author="alice",
            subject="commit A",
            classified_paths={"steering/a.md": "steering"},
        )

        store.advance_base_sha("1111111111111111111111111111111111111111")

        reloaded = state.load_state()
        assert reloaded.base_sha == "1111111111111111111111111111111111111111"

    def test_clear_pending_does_not_touch_base_sha(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        """Clearing `pending` and advancing `base_sha` are deliberately

        separate operations — a decline that only clears `pending` without
        also calling `advance_base_sha` must leave `base_sha` exactly as
        it was.
        """
        store = state.load_state()
        store.set_pending(
            sha="1111111111111111111111111111111111111111",
            author="alice",
            subject="commit A",
            classified_paths={"steering/a.md": "steering"},
        )
        store.clear_pending()

        reloaded = state.load_state()
        assert reloaded.pending is None
        assert reloaded.base_sha == "1111111111111111111111111111111111111111"


# ---------------------------------------------------------------------------
# bounded history
# ---------------------------------------------------------------------------


class TestBoundedHistory:
    def test_history_starts_empty(self, isolated_roots: dict[str, Path]) -> None:
        store = state.load_state()
        assert store.history == []

    def test_history_records_events_in_order(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.record_push_success(
            tree_hash="hash-1", branch="config-sync/instance-hash-1", pr_url=None
        )
        store.record_push_success(
            tree_hash="hash-2", branch="config-sync/instance-hash-2", pr_url=None
        )

        reloaded = state.load_state()
        hashes = [entry.get("tree_hash") for entry in reloaded.history]
        assert hashes.index("hash-1") < hashes.index("hash-2")

    def test_history_is_bounded_and_does_not_grow_without_limit(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        limit = state.HISTORY_LIMIT
        # Push one more event than the documented bound.
        for i in range(limit + 5):
            store.record_push_success(
                tree_hash=f"hash-{i}",
                branch=f"config-sync/instance-hash-{i}",
                pr_url=None,
            )

        reloaded = state.load_state()
        assert len(reloaded.history) <= limit

    def test_bounded_history_drops_oldest_entries_first(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        limit = state.HISTORY_LIMIT
        for i in range(limit + 3):
            store.record_push_success(
                tree_hash=f"hash-{i}",
                branch=f"config-sync/instance-hash-{i}",
                pr_url=None,
            )

        reloaded = state.load_state()
        surviving_hashes = {entry.get("tree_hash") for entry in reloaded.history}
        # The earliest pushes (hash-0, hash-1, hash-2) must have been evicted
        # first; the most recent push must still be present.
        assert "hash-0" not in surviving_hashes
        assert f"hash-{limit + 2}" in surviving_hashes


# ---------------------------------------------------------------------------
# restore_dirs
# ---------------------------------------------------------------------------


class TestRestoreDirs:
    def test_restore_dirs_starts_empty(self, isolated_roots: dict[str, Path]) -> None:
        store = state.load_state()
        assert store.restore_dirs == {}

    def test_record_restore_dir_persists_the_mapping(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.record_restore_dir(
            apply_id="apply-20260919-1",
            restore_dir="/some/state/dir/restores/apply-20260919-1",
        )

        reloaded = state.load_state()
        assert (
            reloaded.restore_dirs["apply-20260919-1"]
            == "/some/state/dir/restores/apply-20260919-1"
        )

    def test_multiple_restore_dirs_are_all_retained(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.record_restore_dir(apply_id="apply-1", restore_dir="/state/restores/1")
        store.record_restore_dir(apply_id="apply-2", restore_dir="/state/restores/2")

        reloaded = state.load_state()
        assert reloaded.restore_dirs["apply-1"] == "/state/restores/1"
        assert reloaded.restore_dirs["apply-2"] == "/state/restores/2"


# ---------------------------------------------------------------------------
# Pending-PR bookkeeping — record_pr_pending / record_pr_pending_failure /
# confirm_pr_created (backend/pr_handoff.py's extension point, task 3.3)
# ---------------------------------------------------------------------------


class TestPendingPr:
    def test_no_pending_pr_record_by_default(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        assert store.pending_pr is None
        assert store.pending_pr_failure is None

    def test_record_pr_pending_persists_branch_hash_and_payload(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.record_pr_pending(
            branch="config-sync/instance-abc123",
            tree_hash="abc123",
            payload={"repo": "TGS-Labs/Kiro-Config-Bundles", "head": "x"},
        )

        reloaded = state.load_state()
        pending_pr = reloaded.pending_pr
        assert pending_pr is not None
        assert pending_pr["branch"] == "config-sync/instance-abc123"
        assert pending_pr["tree_hash"] == "abc123"
        assert pending_pr["payload"] == {
            "repo": "TGS-Labs/Kiro-Config-Bundles",
            "head": "x",
        }
        assert "time" in pending_pr

    def test_record_pr_pending_does_not_change_last_pushed_hash(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.record_push_success(
            tree_hash="prior-hash", branch="config-sync/instance-prior", pr_url=None
        )

        store.record_pr_pending(
            branch="config-sync/instance-new",
            tree_hash="new-hash",
            payload={"repo": "x"},
        )

        assert store.last_pushed_hash == "prior-hash"

    def test_record_pr_pending_failure_persists_the_cause(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.record_pr_pending_failure(reason="malformed branch name")

        reloaded = state.load_state()
        failure = reloaded.pending_pr_failure
        assert failure is not None
        assert failure["reason"] == "malformed branch name"
        assert "time" in failure

    def test_record_pr_pending_failure_does_not_change_last_pushed_hash(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.record_push_success(
            tree_hash="stable-hash", branch="config-sync/instance-stable", pr_url=None
        )

        store.record_pr_pending_failure(reason="notification channel down")

        assert store.last_pushed_hash == "stable-hash"

    def test_confirm_pr_created_advances_last_pushed_hash_and_records_push(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.record_pr_pending(
            branch="config-sync/instance-confirmed",
            tree_hash="confirmed-hash",
            payload={"repo": "x"},
        )

        store.confirm_pr_created(
            tree_hash="confirmed-hash",
            branch="config-sync/instance-confirmed",
            pr_url="https://github.com/TGS-Labs/Kiro-Config-Bundles/pull/7",
        )

        reloaded = state.load_state()
        assert reloaded.last_pushed_hash == "confirmed-hash"
        last_push = reloaded.last_push
        assert last_push is not None
        assert last_push["branch"] == "config-sync/instance-confirmed"
        assert (
            last_push["pr_url"]
            == "https://github.com/TGS-Labs/Kiro-Config-Bundles/pull/7"
        )

    def test_confirm_pr_created_clears_the_pending_pr_record(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.record_pr_pending(
            branch="config-sync/instance-cleared",
            tree_hash="cleared-hash",
            payload={"repo": "x"},
        )

        store.confirm_pr_created(
            tree_hash="cleared-hash",
            branch="config-sync/instance-cleared",
            pr_url="https://github.com/TGS-Labs/Kiro-Config-Bundles/pull/8",
        )

        reloaded = state.load_state()
        assert reloaded.pending_pr is None


# ---------------------------------------------------------------------------
# Senior-review H-NEW-1 — record_pr_pending_failure must clear pending_pr
# ---------------------------------------------------------------------------


class TestPrFailureClearsPendingPr:
    def test_record_pr_pending_failure_clears_pending_pr_for_current_attempt(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.record_pr_pending(
            branch="config-sync/instance-abc",
            tree_hash="abc-hash",
            payload={"repo": "x"},
        )

        store.record_pr_pending_failure(
            reason="Buildo create_pull_request returned 422",
            tree_hash="abc-hash",
            branch="config-sync/instance-abc",
        )

        reloaded = state.load_state()
        assert reloaded.pending_pr is None
        assert reloaded.pending_pr_failure is not None
        assert reloaded.pending_pr_stale is None

    def test_record_pr_pending_failure_with_no_pending_pr_yet_still_records(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        """The in-tick payload-build-failure case: no pending_pr entry for

        this attempt exists yet (record_pr_pending hasn't run). Must still
        record the failure cleanly with nothing to clear."""
        store = state.load_state()

        store.record_pr_pending_failure(
            reason="malformed branch name", tree_hash="new-hash", branch="b"
        )

        reloaded = state.load_state()
        assert reloaded.pending_pr is None
        assert reloaded.pending_pr_failure is not None

    def test_record_pr_pending_failure_for_a_different_attempt_leaves_current_alone(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        """A failure report naming a DIFFERENT tree_hash/branch than the

        current pending_pr must not clear it -- the current entry may be a
        genuinely still-pending different attempt (H-NEW-2's sibling
        case)."""
        store = state.load_state()
        store.record_pr_pending(
            branch="config-sync/instance-current",
            tree_hash="current-hash",
            payload={"repo": "x"},
        )

        store.record_pr_pending_failure(
            reason="stale report",
            tree_hash="old-hash",
            branch="config-sync/instance-old",
        )

        reloaded = state.load_state()
        assert reloaded.pending_pr is not None
        assert reloaded.pending_pr.get("tree_hash") == "current-hash"
        assert reloaded.pending_pr_stale is not None
        assert reloaded.pending_pr_stale["tree_hash"] == "old-hash"


# ---------------------------------------------------------------------------
# Senior-review H-NEW-2 — confirm_pr_created / record_pr_pending_failure
# must validate against the CURRENT pending_pr before mutating anything, so
# a stale confirmation/failure for a superseded hash cannot regress
# last_pushed_hash or destroy the real current pending_pr.
# ---------------------------------------------------------------------------


class TestStaleConfirmationOwnershipCheck:
    def test_confirm_created_for_superseded_hash_does_not_regress_hash_or_clear_current(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        """push A -> push C (supersedes A) -> confirm_pr_created(A's hash)

        must NOT set last_pushed_hash to A, and pending_pr must still name
        C -- the stale confirmation is recorded, not silently swallowed."""
        store = state.load_state()

        # Push A: pending_pr now names A.
        store.record_pr_pending(
            branch="config-sync/instance-A", tree_hash="hash-A", payload={"repo": "x"}
        )
        # A second, distinct change (C) arrives before A is confirmed and
        # overwrites the single pending_pr slot -- exactly push.py's
        # documented fall-through-to-change-path behaviour.
        store.record_pr_pending(
            branch="config-sync/instance-C", tree_hash="hash-C", payload={"repo": "x"}
        )
        assert store.pending_pr is not None
        assert store.pending_pr.get("tree_hash") == "hash-C"

        # An external agent, still holding A's original branch/hash, now
        # reports A's PR as created.
        store.confirm_pr_created(
            tree_hash="hash-A",
            branch="config-sync/instance-A",
            pr_url="https://github.com/TGS-Labs/Kiro-Config-Bundles/pull/1",
        )

        reloaded = state.load_state()
        assert reloaded.last_pushed_hash != "hash-A"
        assert reloaded.pending_pr is not None
        assert reloaded.pending_pr.get("tree_hash") == "hash-C"
        assert reloaded.pending_pr_stale is not None
        assert reloaded.pending_pr_stale["tree_hash"] == "hash-A"

    def test_confirm_created_for_the_current_matching_attempt_still_works(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        """Regression guard: the normal, non-superseded matching case must

        behave exactly as before the H-NEW-2 fix."""
        store = state.load_state()
        store.record_pr_pending(
            branch="config-sync/instance-only",
            tree_hash="only-hash",
            payload={"repo": "x"},
        )

        store.confirm_pr_created(
            tree_hash="only-hash",
            branch="config-sync/instance-only",
            pr_url="https://github.com/TGS-Labs/Kiro-Config-Bundles/pull/2",
        )

        reloaded = state.load_state()
        assert reloaded.last_pushed_hash == "only-hash"
        assert reloaded.pending_pr is None
        assert reloaded.pending_pr_stale is None


# ---------------------------------------------------------------------------
# Atomic writes — no partial/corrupt state file survives an interrupted write
# ---------------------------------------------------------------------------


class TestAtomicWrites:
    def test_write_uses_a_temp_file_and_rename_not_in_place_truncation(
        self, isolated_roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An atomic writer creates a temp file and os.replace()s it into
        place; it must never open the real state path with a truncating
        mode ('w') directly, because that leaves a truncated-to-empty file
        visible to any concurrent reader for the duration of the write."""
        store = state.load_state()
        state_path = Path(state.get_state_path())

        opened_modes_on_real_path: list[str] = []
        real_open = open

        def spy_open(path: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            if Path(path) == state_path and "w" in mode:
                opened_modes_on_real_path.append(mode)
            return real_open(path, mode, *args, **kwargs)

        monkeypatch.setattr("builtins.open", spy_open)

        store.record_push_success(
            tree_hash="atomic-check", branch="config-sync/instance-x", pr_url=None
        )

        assert opened_modes_on_real_path == [], (
            "the real state file must never be opened in a writing/truncating "
            "mode directly; write to a temp file then rename/replace"
        )

    def test_interrupted_write_leaves_previous_valid_state_file_intact(
        self, isolated_roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulate a crash mid-write: os.replace (the atomic rename step)
        raises. The state file on disk must remain the LAST successfully
        committed, fully valid JSON document -- never truncated or partial."""
        store = state.load_state()
        store.record_push_success(
            tree_hash="good-hash", branch="config-sync/instance-good", pr_url=None
        )
        state_path = Path(state.get_state_path())
        good_bytes = state_path.read_bytes()

        def failing_replace(*args: Any, **kwargs: Any) -> Any:
            raise OSError("simulated crash during atomic rename")

        monkeypatch.setattr(os, "replace", failing_replace)

        with pytest.raises(OSError):
            store.record_push_success(
                tree_hash="never-committed",
                branch="config-sync/instance-never",
                pr_url=None,
            )

        # The on-disk file must be untouched: still the last good bytes,
        # and still parseable as the complete document.
        assert state_path.read_bytes() == good_bytes
        with open(state_path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        assert payload["last_pushed_hash"] == "good-hash"

    def test_no_leftover_temp_file_after_a_successful_write(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.record_push_success(
            tree_hash="clean-hash", branch="config-sync/instance-clean", pr_url=None
        )

        state_dir = Path(state.get_state_dir())
        # The cross-process lock file (state.json.lock) is a permanent,
        # intentional sibling of state.json (see state.py's own
        # cross-process locking section) -- allow exactly that one name
        # and nothing else, so this test still catches a real stray
        # temp/partial file left behind by a failed write.
        allowed_names = {
            Path(state.get_state_path()).name,
            Path(state.get_lock_path()).name,
        }
        leftover_temp_files = [
            p
            for p in state_dir.iterdir()
            if p.name not in allowed_names and p.is_file()
        ]
        assert leftover_temp_files == [], (
            "a successful write must not leave a stray temp/partial file "
            f"behind: found {leftover_temp_files}"
        )

    def test_load_state_never_returns_a_half_written_document(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        """Directly simulate a corrupt/truncated state file on disk (as an
        interrupted non-atomic write would leave) and confirm the loader
        does not silently accept truncated JSON as valid state -- it must
        either raise or fall back to a fresh, well-formed default document,
        never return a half-parsed/partial in-memory state."""
        store = state.load_state()
        store.record_push_success(
            tree_hash="pre-corruption", branch="config-sync/instance-x", pr_url=None
        )
        state_path = Path(state.get_state_path())

        # Truncate the file mid-document, as a crash during a non-atomic
        # write would leave it.
        original = state_path.read_text(encoding="utf-8")
        truncated = original[: len(original) // 2]
        state_path.write_text(truncated, encoding="utf-8")

        try:
            reloaded = state.load_state()
        except (json.JSONDecodeError, ValueError):
            # Failing loudly on unreadable state is an acceptable outcome.
            return

        # If it did not raise, it must not have silently kept the corrupt
        # value: last_pushed_hash must be well-defined (either the last
        # good value recovered from a backup, or a clean default), and the
        # full field set must still be present and JSON-serializable.
        assert reloaded.last_pushed_hash in (None, "pre-corruption")
        json.dumps(
            {
                "last_pushed_hash": reloaded.last_pushed_hash,
                "last_push": reloaded.last_push,
                "last_seen_sha": reloaded.last_seen_sha,
                "pending": reloaded.pending,
                "history": reloaded.history,
                "restore_dirs": reloaded.restore_dirs,
            }
        )
