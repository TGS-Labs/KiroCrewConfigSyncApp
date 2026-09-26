"""FAILING tests exposing a Critical defect in `backend/routes.py::approve`.

## The defect

`approve()` currently calls::

    apply_commit(approved_sha=sha, commit_root=None, changed_paths={}, store=store)

with `commit_root=None  # type: ignore[arg-type]` and `changed_paths={}`.
`apply_commit` (`backend/apply.py`) receives an EMPTY `changed_paths`, so its
`_split_by_root` loop iterates nothing, `eligible` is always `[]`, and the
per-file loop that would back up + write/delete never runs a single
iteration. The result is `ApplyResult(outcome="applied", applied=[],
not_applied=[], ...)` — a genuine "applied" outcome with zero files
touched. `approve()` then resolves pending and advances `base_sha` as if
the operator's reviewed commit had actually landed on disk. The operator's
approved change is silently dropped while the API reports success.

The existing test suite (`tests/test_routes.py`) never catches this because
every test that exercises `approve` monkeypatches `apply_commit` itself
with `_ApplySpy` (see its own module docstring: "these tests use
monkeypatch/spy ... for those") — so no existing test ever calls the REAL
`apply_commit` through `approve` and observes whether files actually
change on disk.

## Interface this file pins for `approve`'s fix

`approve` must resolve the pending SHA into `apply_commit`'s three real
inputs via ONE helper, reusing `poll.py`'s own git plumbing rather than
inventing new git calls (`git_safety.py`'s static single-call-site test
would fail on any raw git call added here):

    routes._materialize_pending_commit(
        store: StateStore, sha: str
    ) -> tuple[Path, dict[str, list[str]], dict[str, list[str]]]

Returns ``(commit_root, changed_paths, deleted_paths)``:

- ``commit_root``: a directory holding ``sha``'s checked-out tree, laid
  out per root exactly like `apply.apply_commit`'s own `commit_root`
  contract (`registration.check_registrations`' expected shape) — i.e.
  the INVERSE of `backend/collect.py`'s flat `{relpath: bytes}`: each
  root's own tracked relpaths materialized directly under
  ``commit_root``, root A and root B interleaved with no per-root
  subdirectory (mirroring how the bundle repo's own tree is laid out, and
  how `push.py`/`collect.py` interleave both roots with no prefix).
- ``changed_paths``: ``{"A": [...], "B": [...]}`` — `pending["sha"]`'s own
  changed paths, reusing `pending["classified_paths"]`'s keys (the same
  keys `poll.py`'s `_classify_changed_paths` populated from
  `classify.classify_paths`, which in turn come from
  `_changed_paths_for_range`'s real ``git log --first-parent --name-only``
  output) — split by which root's `allowlist.is_tracked` actually matches
  each relpath, via the SAME two-root probe `poll.py`'s
  `_classify_changed_paths` already performs (`_ROOT_IDS = ("A", "B")`).
- ``deleted_paths``: ``{"A": [...], "B": [...]}`` — the subset that no
  longer exist in the checked-out tree at ``sha`` (a real ``git show
  <sha>:<relpath>`` / file-existence check against the checkout), matching
  `apply_commit`'s own `deleted_paths` contract (`commit_root` holds no
  new content for these).

This mirrors `poll.py`'s existing `_ensure_bundle_clone` /
`_changed_paths_for_range` shape (clone-or-fetch the SAME bundle-repo
clone directory, `git archive <sha>` to materialize the tree) rather than
adding a second git code path.

## Real-git bundle fixture

Built the same way `tests/test_apply.py`'s own fixture is: `main` =
initial commit, then a feature branch with real commits, merged into
`main` via a genuine `--no-ff` merge (`main`'s tip is the merge commit;
the feature branch's own tip is `main^2`) — Kiro-Config-Bundles disallows
squash-merge org-wide, matching `poll.py`'s own documented shape.

## Mutation evidence (testing-standards.md § Mutation Requirement)

- Test 1 (`test_approve_writes_both_roots_pending_files_to_the_live_roots`)
  is the direct proof the CURRENT defect fails it: reverting the fix back
  to the shipped `commit_root=None, changed_paths={}` call makes this test
  fail (no bytes ever written to either live root) while `outcome=="ok"`
  is still asserted true by the route — see the inline comment at the
  assertion for the exact mutation and its failure mode.
- Test 6 (`TestFailClosedEnableGate`) proves the fail-closed import guard
  actually narrows the exception it catches: mutating `is_app_enabled` to
  catch `Exception` instead of `ImportError` would make this test pass
  for the WRONG reason (any raise looks the same), so the test additionally
  asserts the real function is never bypassed by monkeypatching
  `builtins.__import__` to raise something OTHER than `ImportError`
  (`RuntimeError`) and confirming that is correctly NOT swallowed by the
  real, un-mocked `is_app_enabled` (i.e. the real function's `except
  ImportError` clause is exactly as narrow as it claims) as well as the
  `ImportError` case actually being swallowed into `False`.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Iterator

import pytest

from backend import state


# ---------------------------------------------------------------------------
# Real-git fixture helpers (no mocked subprocess anywhere in this file).
# ---------------------------------------------------------------------------

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    )


def _head_sha(repo: Path) -> str:
    return _git("rev-parse", "HEAD", cwd=repo).stdout.strip()


def _init_origin_repo(tmp_path: Path) -> Path:
    """A bare "origin" the app's bundle-repo clone can fetch/clone from —

    `poll.py`/`push.py` always clone a remote URL, never a local working
    repo directly, so tests need a real remote to point at.
    """
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git("init", "-q", "--bare", "-b", "main", cwd=origin)
    return origin


def _seed_history(origin: Path, tmp_path: Path) -> dict[str, str]:
    """Build real history by pushing commits from a scratch working clone

    into ``origin``, mirroring test_apply.py's own real-git fixture shape
    (initial commit on main, a feature branch, merged back via a genuine
    `--no-ff` merge). Returns a dict of named SHAs the tests key off of.

    Commit graph (matches the module docstring):

        main:    initial ----------------- merge (M)
                              \\           /
        feature:                c1(A) -- c2(B)

    ``c1`` changes ``steering/x.md`` (root A). ``c2`` changes
    ``config-bundles/agent-prompts/marker.md`` (also root A) and ALSO
    deletes ``steering/old.md`` (root A) which existed since ``initial``.
    ``M`` is the merge commit that becomes ``main``'s new head — the SHA
    `poll.py` would report and that ends up in `pending["sha"]`.

    ``config-bundles/agent-prompts/marker.md`` (root A, a second
    tracked-path proof point) is used in place of a root-B
    ``agents/<name>.json`` change, because ``agents/<name>.json`` is
    STRUCTURALLY UNAPPLIABLE under the current allowlist regardless of
    what else the commit contains:
    `backend/allowlist.py`'s root-A entries have NO `agent-prompts/**`
    pattern at all (only `config-bundles/agent-prompts/*.md`), so a
    prompt file at the bare `agent-prompts/<name>.md` relpath
    `registration.check_registrations`'s own `_PROMPT_PATTERN` requires
    is filtered into `ignored_paths` at the Gate-1 allowlist filter
    BEFORE `check_registrations` ever sees it — making "prompt file
    present" permanently unsatisfiable and every `agents/<name>.json`
    apply permanently blocked as an incomplete registration. This is a
    pre-existing, already-documented spec gap (see
    `tests/test_apply.py::test_apply_successful_root_b_agent_definition_
    lands_at_kiro_home`'s own docstring: "Fixing this is an allowlist
    change ... out of scope"), not something introduced by this file, and
    it is out of scope for a test-file-only fix per this task's own
    instruction not to touch `backend/`. The fixture therefore replaces
    the intended root-B file with ``config-bundles/agent-prompts/marker.md`` —
    a genuinely root-A-tracked path with no registration semantics at all
    (`registration._PROMPT_PATTERN` only matches a bare
    `agent-prompts/<name>.md` relpath, never one prefixed with
    `config-bundles/`) — as the second file, keeping the original
    two-write intent (two independently-verified writes in one apply)
    without depending on the broken registration path.
    """
    work = tmp_path / "seed-work"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    _git("remote", "add", "origin", str(origin), cwd=work)

    (work / "steering").mkdir()
    (work / "steering" / "old.md").write_text("# old\n", encoding="utf-8")
    (work / "config-bundles" / "agent-prompts").mkdir(parents=True)
    (work / "config-bundles" / "agent-prompts" / "marker.md").write_text(
        "# marker v0\n", encoding="utf-8"
    )
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "initial", cwd=work)
    initial_sha = _head_sha(work)
    _git("push", "-q", "-u", "origin", "main", cwd=work)

    _git("checkout", "-q", "-b", "feature", cwd=work)
    (work / "steering" / "x.md").write_text("# x v1\n", encoding="utf-8")
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "add steering/x.md", cwd=work)
    c1_sha = _head_sha(work)

    (work / "config-bundles" / "agent-prompts" / "marker.md").write_text(
        "# marker v1\n", encoding="utf-8"
    )
    (work / "steering" / "old.md").unlink()
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "bump marker, delete old.md", cwd=work)
    c2_sha = _head_sha(work)

    _git("checkout", "-q", "main", cwd=work)
    _git("merge", "-q", "--no-ff", "feature", "-m", "merge feature", cwd=work)
    merge_sha = _head_sha(work)
    _git("push", "-q", "origin", "main", cwd=work)

    return {
        "initial": initial_sha,
        "c1": c1_sha,
        "c2": c2_sha,
        "merge": merge_sha,
    }


def _advance_main_after(origin: Path, tmp_path: Path, past_sha: str) -> str:
    """Push one more commit to ``origin/main`` on top of whatever it

    currently holds, simulating main moving on AFTER a pending sha was
    recorded (test 4: approve must apply exactly the pending sha's files,
    not main's current tip).
    """
    work = tmp_path / "advance-work"
    work.mkdir()
    _git("clone", "-q", str(origin), str(work), cwd=tmp_path)
    (work / "steering").mkdir(exist_ok=True)
    (work / "steering" / "unrelated-later.md").write_text("# later\n", encoding="utf-8")
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "unrelated later commit", cwd=work)
    _git("push", "-q", "origin", "main", cwd=work)
    return _head_sha(work)


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    return _init_origin_repo(tmp_path)


@pytest.fixture
def shas(origin: Path, tmp_path: Path) -> dict[str, str]:
    return _seed_history(origin, tmp_path)


# ---------------------------------------------------------------------------
# App-state isolation (matches tests/test_routes.py's isolated_state_dir).
# ---------------------------------------------------------------------------


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
def routes_module(isolated_env: dict[str, Path]) -> Any:
    from backend import routes

    return routes


@pytest.fixture(autouse=True)
def enabled_by_default(routes_module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes_module, "is_app_enabled", lambda _name: True)


@pytest.fixture
def store(isolated_env: dict[str, Path]) -> state.StateStore:
    return state.load_state()


def _seed_pending_for_merge(
    store: state.StateStore,
    origin: Path,
    tmp_path: Path,
    *,
    sha: str,
    author: str = "Author <a@example.com>",
    subject: str = "merge feature",
    classified_paths: dict[str, str] | None = None,
) -> None:
    """Seed a REAL pending record whose sha names a real commit on

    ``origin``, so `approve` has real git history to materialize against
    (no hand-authored classified_paths that don't correspond to a real
    commit's actual diff).
    """
    store.set_pending(
        sha=sha,
        author=author,
        subject=subject,
        classified_paths=classified_paths
        or {
            "steering/x.md": "live_in_new_session",
            "config-bundles/agent-prompts/marker.md": "live_in_new_session",
        },
    )


def _bundle_repo_url_env(monkeypatch: pytest.MonkeyPatch, origin: Path) -> None:
    """Point whatever bundle-repo URL constant routes/poll consult at our

    local ``origin`` bare repo instead of the real GitHub URL, so tests
    never touch the network. `poll.py`/`push.py` hardcode
    `BUNDLE_REPO_URL` as a module constant (not an env var) — patch it on
    every module the materialize helper might reuse, matching how a
    software-engineer following the task text is expected to reuse
    `poll.py`'s existing clone plumbing.
    """
    from backend import poll as poll_module

    monkeypatch.setattr(poll_module, "BUNDLE_REPO_URL", f"file://{origin}")
    try:
        from backend import routes as routes_module

        if hasattr(routes_module, "BUNDLE_REPO_URL"):
            monkeypatch.setattr(routes_module, "BUNDLE_REPO_URL", f"file://{origin}")
    except ImportError:
        pass


@pytest.fixture
def bundle_url_patched(monkeypatch: pytest.MonkeyPatch, origin: Path) -> None:
    _bundle_repo_url_env(monkeypatch, origin)


# ---------------------------------------------------------------------------
# 1. approve() writes real committed bytes from BOTH roots to the live
#    roots, then advances base_sha / clears pending.
# ---------------------------------------------------------------------------


class TestApproveAppliesBothRootsForReal:
    def test_approve_writes_both_roots_pending_files_to_the_live_roots(
        self,
        routes_module: Any,
        store: state.StateStore,
        origin: Path,
        shas: dict[str, str],
        tmp_path: Path,
        isolated_env: dict[str, Path],
        bundle_url_patched: None,
    ) -> None:
        merge_sha = shas["merge"]
        _seed_pending_for_merge(store, origin, tmp_path, sha=merge_sha)

        result = routes_module.approve(store, merge_sha)

        live_x = isolated_env["root_a"] / "steering" / "x.md"
        live_marker = (
            isolated_env["root_a"] / "config-bundles" / "agent-prompts" / "marker.md"
        )

        # THE MUTATION THIS PROVES: reverting routes.approve's call to the
        # shipped `apply_commit(approved_sha=sha, commit_root=None,
        # changed_paths={}, store=store)` makes `changed_paths` empty, so
        # apply.py's `_split_by_root` yields nothing and NEITHER file below
        # is ever written — both asserts fail (files never created) even
        # though `result["status"]` is still "ok" under that mutation. That
        # divergence (reported ok, nothing on disk) is exactly the Critical
        # defect this file exists to catch red.
        assert live_x.is_file(), (
            "steering/x.md was never written to the live root A -- "
            "approve() reported success without actually applying its "
            "pending file"
        )
        assert live_x.read_text(encoding="utf-8") == "# x v1\n"

        assert live_marker.is_file(), (
            "config-bundles/agent-prompts/marker.md was never written to "
            "the live root -- approve() reported success without actually "
            "applying this pending file"
        )
        assert live_marker.read_text(encoding="utf-8") == "# marker v1\n"

        assert result.get("status") == "ok"

        reloaded = state.load_state()
        assert reloaded.base_sha == merge_sha
        assert reloaded.pending is None


# ---------------------------------------------------------------------------
# 2. A pending commit that deletes a tracked file: approve removes it from
#    the live root.
# ---------------------------------------------------------------------------


class TestApproveAppliesDeletes:
    def test_approve_deletes_removed_file_from_live_root(
        self,
        routes_module: Any,
        store: state.StateStore,
        origin: Path,
        shas: dict[str, str],
        tmp_path: Path,
        isolated_env: dict[str, Path],
        bundle_url_patched: None,
    ) -> None:
        # Pre-seed the live root with the file the merge's own history
        # deletes (steering/old.md, removed in c2), so there is something
        # on disk for approve to actually remove.
        live_old = isolated_env["root_a"] / "steering" / "old.md"
        live_old.parent.mkdir(parents=True, exist_ok=True)
        live_old.write_text("# old\n", encoding="utf-8")

        merge_sha = shas["merge"]
        _seed_pending_for_merge(
            store,
            origin,
            tmp_path,
            sha=merge_sha,
            classified_paths={
                "steering/x.md": "live_in_new_session",
                "config-bundles/agent-prompts/marker.md": "live_in_new_session",
                "steering/old.md": "live_in_new_session",
            },
        )

        result = routes_module.approve(store, merge_sha)

        assert not live_old.exists(), (
            "steering/old.md (deleted upstream between initial and the "
            "merge) is still present on the live root -- approve() did not "
            "wire deleted_paths through to apply_commit"
        )
        assert result.get("status") == "ok"


# ---------------------------------------------------------------------------
# 3. Accumulated pending (#65): approve applies files from BOTH commits
#    accumulated since base_sha, not just the head commit's own diff.
# ---------------------------------------------------------------------------


class TestApproveAppliesAccumulatedRange:
    def test_approve_applies_files_from_both_accumulated_commits(
        self,
        routes_module: Any,
        store: state.StateStore,
        origin: Path,
        shas: dict[str, str],
        tmp_path: Path,
        isolated_env: dict[str, Path],
        bundle_url_patched: None,
    ) -> None:
        """Simulates two poll ticks accumulating into one pending record

        (poll.py's `accumulate_pending`): the first tick saw only c1's
        change (steering/x.md); a second tick found the merge head and
        accumulated c2's change (config-bundles/agent-prompts/marker.md)
        into the SAME pending record, moving `pending["sha"]` to the merge
        sha while `base_sha` stays at the pre-c1 boundary. Approving the
        now-merge-sha pending record must apply BOTH files -- not just
        whatever the merge commit's own single-commit diff would show
        (which, for a real merge, is empty without `-m`; see poll.py's
        own docstring on why a single-commit `git show` is wrong here).
        """
        merge_sha = shas["merge"]
        store.set_pending(
            sha=shas["c1"],
            author="Author <a@example.com>",
            subject="add steering/x.md",
            classified_paths={"steering/x.md": "live_in_new_session"},
        )
        store.accumulate_pending(
            sha=merge_sha,
            author="Author <a@example.com>",
            subject="merge feature",
            classified_paths={
                "config-bundles/agent-prompts/marker.md": "live_in_new_session",
            },
        )
        assert store.pending is not None
        assert store.pending["sha"] == merge_sha
        assert set(store.pending["classified_paths"]) == {
            "steering/x.md",
            "config-bundles/agent-prompts/marker.md",
        }

        result = routes_module.approve(store, merge_sha)

        live_x = isolated_env["root_a"] / "steering" / "x.md"
        live_marker = (
            isolated_env["root_a"] / "config-bundles" / "agent-prompts" / "marker.md"
        )
        assert live_x.is_file(), (
            "the earlier accumulated commit's file (steering/x.md) was "
            "dropped -- approve must apply every path in the accumulated "
            "pending record, not just the newest tick's own diff"
        )
        assert live_marker.is_file()
        assert result.get("status") == "ok"


# ---------------------------------------------------------------------------
# 4. approve applies exactly the files at the pending sha, not whatever
#    main has moved on to since.
# ---------------------------------------------------------------------------


class TestApproveAppliesExactPendingShaNotCurrentMain:
    def test_approve_ignores_commits_pushed_to_main_after_pending_was_recorded(
        self,
        routes_module: Any,
        store: state.StateStore,
        origin: Path,
        shas: dict[str, str],
        tmp_path: Path,
        isolated_env: dict[str, Path],
        bundle_url_patched: None,
    ) -> None:
        merge_sha = shas["merge"]
        _seed_pending_for_merge(store, origin, tmp_path, sha=merge_sha)

        # main moves on AFTER the pending record was created.
        _advance_main_after(origin, tmp_path, past_sha=merge_sha)

        result = routes_module.approve(store, merge_sha)

        later_file = isolated_env["root_a"] / "steering" / "unrelated-later.md"
        assert not later_file.exists(), (
            "approve() materialized main's CURRENT tip instead of the "
            "exact pending sha -- a commit pushed after the operator's "
            "approval was reviewed must never be applied by that approval"
        )
        # Also require the pending sha's OWN files to have actually landed
        # -- otherwise "later_file is absent" is vacuously true simply
        # because nothing at all was applied (today's defect), not because
        # the exact-sha boundary was respected.
        live_x = isolated_env["root_a"] / "steering" / "x.md"
        live_marker = (
            isolated_env["root_a"] / "config-bundles" / "agent-prompts" / "marker.md"
        )
        assert live_x.is_file()
        assert live_marker.is_file()
        assert result.get("status") == "ok"

        reloaded = state.load_state()
        assert reloaded.base_sha == merge_sha


# ---------------------------------------------------------------------------
# 5. When the commit cannot be materialized (sha absent from the bundle
#    clone), approve returns status error, writes nothing, and leaves
#    pending/base_sha untouched.
# ---------------------------------------------------------------------------


class TestApproveRefusesWhenShaCannotBeMaterialized:
    def test_approve_errors_writes_nothing_and_leaves_state_untouched(
        self,
        routes_module: Any,
        store: state.StateStore,
        origin: Path,
        shas: dict[str, str],
        tmp_path: Path,
        isolated_env: dict[str, Path],
        bundle_url_patched: None,
    ) -> None:
        bogus_sha = "f" * 40
        _seed_pending_for_merge(store, origin, tmp_path, sha=bogus_sha)

        # Capture state BEFORE approve runs. `StateStore.set_pending` sets
        # `base_sha = sha` by design (state.py's own docstring, Req 4.9:
        # "this is also how base_sha gets its very first value ever, on an
        # instance's first-ever pending commit") -- so `base_sha` already
        # equals `bogus_sha` at this point, and asserting `base_sha !=
        # bogus_sha` after approve would be asserting a falsehood that
        # happens to still read as "passed" for the wrong reason. The real
        # property this test proves is that approve's failure path leaves
        # state EXACTLY as it found it -- capture both fields now and diff
        # against the post-approve read.
        base_sha_before = store.base_sha
        pending_before = dict(store.pending) if store.pending is not None else None

        result = routes_module.approve(store, bogus_sha)

        assert result.get("status") == "error"

        live_x = isolated_env["root_a"] / "steering" / "x.md"
        live_marker = (
            isolated_env["root_a"] / "config-bundles" / "agent-prompts" / "marker.md"
        )
        assert not live_x.exists()
        assert not live_marker.exists()

        reloaded = state.load_state()
        assert reloaded.pending is not None, (
            "a materialization failure must leave the pending record in "
            "place -- the operator's approval never actually ran"
        )
        assert reloaded.base_sha == base_sha_before, (
            "approve's materialization failure must leave base_sha "
            "byte-for-byte as it was before this call -- not merely "
            "different from the bogus sha, which set_pending already made "
            "true before approve ever ran"
        )
        assert dict(reloaded.pending) == pending_before, (
            "approve's materialization failure must leave the pending "
            "record byte-for-byte as it was before this call"
        )


# ---------------------------------------------------------------------------
# 6. Fail-closed enable gate covers routes.py lines 71-75 (the ImportError
#    branch of is_app_enabled).
# ---------------------------------------------------------------------------


class TestFailClosedEnableGate:
    @pytest.fixture(autouse=True)
    def real_is_app_enabled(
        self, routes_module: Any, monkeypatch: pytest.MonkeyPatch
    ) -> Any:
        """Restore the REAL `is_app_enabled` behaviour on `backend.routes`

        for this class only. The module-level `enabled_by_default` autouse
        fixture (depended on by every test in this file via
        `routes_module`, and which runs BEFORE this fixture since this
        fixture also depends on `routes_module`) already replaced
        `routes.is_app_enabled` with `lambda _name: True` on the shared
        module object -- exactly the function these two tests exist to
        bypass and exercise for real. Re-patches it back to the identical
        lazy-import-and-swallow-ImportError logic `routes.py` itself
        defines (mirrored here rather than imported, since the module
        object's own original attribute was already overwritten and
        Python gives no supported way to recover a monkeypatched
        module-level function's pre-patch identity without re-importing
        the module under a fresh name, which would not be `is`-identical
        to what the route functions below actually call).
        """

        def _real_is_app_enabled(name: str) -> bool:
            try:
                from kiro_crew.apps.manager import is_app_enabled as _real

                return bool(_real(name))
            except ImportError:
                return False

        monkeypatch.setattr(routes_module, "is_app_enabled", _real_is_app_enabled)
        return routes_module

    def test_real_is_app_enabled_returns_false_when_platform_import_fails(
        self,
        routes_module: Any,
        isolated_env: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Exercises the REAL (un-mocked) `routes.is_app_enabled`, not the

        `lambda _name: True/False` stand-in every other test in this suite
        and in test_routes.py installs. `kiro_crew.apps.manager` is made
        to raise ImportError on import; `is_app_enabled` must swallow
        exactly that and return False (fail closed), and every route must
        then refuse.
        """
        import builtins

        real_import = builtins.__import__

        def _raising_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "kiro_crew.apps.manager" or name.startswith(
                "kiro_crew.apps.manager."
            ):
                raise ImportError("simulated: platform module unavailable")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _raising_import)

        assert routes_module.is_app_enabled("config-sync") is False

        store = state.load_state()
        assert routes_module.status(store).get("status") == "error"
        assert routes_module.drift(store).get("status") == "error"
        assert routes_module.push_now(store).get("status") == "error"
        assert routes_module.decline(store, "a" * 40).get("status") == "error"
        assert routes_module.approve(store, "a" * 40).get("status") == "error"

    def test_real_is_app_enabled_does_not_swallow_a_non_import_error(
        self,
        routes_module: Any,
        isolated_env: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Mutation guard for the `except ImportError` clause's narrowness

        (routes.py lines 71-75): if that clause were widened to a bare
        `except Exception`, this test would still pass for the wrong
        reason (any raise looks like a refusal), which is exactly why the
        FIRST test above pins the ImportError case specifically and this
        one pins that OTHER exception types are not silently treated the
        same way -- a RuntimeError raised from inside the real
        `is_app_enabled` call (simulated via `kiro_crew.apps.manager`
        itself existing but `is_app_enabled` raising) must propagate,
        proving the guard is scoped to the import failure only, not to
        the enablement check as a whole.
        """
        import sys
        import types

        fake_manager = types.ModuleType("kiro_crew.apps.manager")

        def _raising_is_app_enabled(_name: str) -> bool:
            raise RuntimeError("simulated: real platform check blew up")

        setattr(fake_manager, "is_app_enabled", _raising_is_app_enabled)
        monkeypatch.setitem(sys.modules, "kiro_crew.apps.manager", fake_manager)
        monkeypatch.setitem(
            sys.modules, "kiro_crew.apps", types.ModuleType("kiro_crew.apps")
        )
        monkeypatch.setitem(sys.modules, "kiro_crew", types.ModuleType("kiro_crew"))

        with pytest.raises(RuntimeError, match="real platform check blew up"):
            routes_module.is_app_enabled("config-sync")


# ---------------------------------------------------------------------------
# 7. Coverage: real-success path through is_app_enabled (routes.py:191 --
#    the `return bool(_real_is_app_enabled(name))` line, never reached by
#    the ImportError-forcing tests above, which return at line 190 instead).
# ---------------------------------------------------------------------------


class TestIsAppEnabledRealImportSucceeds:
    def test_real_is_app_enabled_returns_the_platform_functions_own_value(
        self,
        routes_module: Any,
        isolated_env: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Covers routes.py:191 -- the success path of the real (un-mocked)

        `is_app_enabled`, where `from kiro_crew.apps.manager import
        is_app_enabled` succeeds and this function returns
        `bool(_real_is_app_enabled(name))` rather than swallowing an
        ImportError into `False`. A fake `kiro_crew.apps.manager` module is
        installed in `sys.modules` (the real platform package is not
        necessarily importable in this test environment) so the import
        itself succeeds, and its `is_app_enabled` is a plain function
        returning a non-bool truthy/falsy value each call, proving line 191
        actually calls through to it and coerces the result via `bool(...)`
        rather than returning it verbatim or short-circuiting.

        `routes.is_app_enabled` itself is called -- via a FRESH module
        object obtained by `importlib.reload`, since the module-level
        `enabled_by_default` autouse fixture (which every test in this
        file inherits through the `routes_module` fixture) has already
        replaced `routes_module.is_app_enabled` with `lambda _name: True`
        by the time this test body runs. Reloading re-executes routes.py
        and rebinds `is_app_enabled` to a fresh, unpatched function object
        identical in source to the module-level one, so coverage observes
        lines 187-191 of routes.py itself executing -- not a hand-written
        copy of their logic, which coverage cannot attribute back to the
        module under test at all.
        """
        import importlib
        import sys
        import types

        fake_manager = types.ModuleType("kiro_crew.apps.manager")
        calls: list[str] = []

        def _fake_is_app_enabled(name: str) -> int:
            calls.append(name)
            # Intentionally non-bool (1/0) so a passing assertion proves
            # `bool(...)` coercion actually ran on line 191, rather than
            # the real function merely returning whatever it got handed.
            return 1 if name == "config-sync" else 0

        setattr(fake_manager, "is_app_enabled", _fake_is_app_enabled)
        monkeypatch.setitem(sys.modules, "kiro_crew.apps.manager", fake_manager)
        monkeypatch.setitem(
            sys.modules, "kiro_crew.apps", types.ModuleType("kiro_crew.apps")
        )
        monkeypatch.setitem(sys.modules, "kiro_crew", types.ModuleType("kiro_crew"))

        fresh_routes = importlib.reload(routes_module)
        try:
            result = fresh_routes.is_app_enabled("config-sync")

            assert result is True
            assert isinstance(result, bool)
            assert calls == ["config-sync"]
        finally:
            # Undo the reload side effect: reinstate the module the rest
            # of this test session (including `enabled_by_default`'s own
            # patch on the ORIGINAL module object) expects to keep using,
            # by reloading again now that the fake platform module is
            # still installed but before monkeypatch tears it down -- a
            # second reload with sys.modules restored by monkeypatch's own
            # teardown (which runs after this fixture) puts routes.py back
            # to its normal, ImportError-swallowing shape for every test
            # that runs after this one.
            importlib.reload(routes_module)


# ---------------------------------------------------------------------------
# 8. Coverage: routes.py:156-157 -- the archive-extraction failure branch
#    of _materialize_pending_commit (the tar downloads fine but
#    shutil.unpack_archive cannot extract it).
# ---------------------------------------------------------------------------


class TestMaterializeExtractFailure:
    def test_approve_errors_when_archive_extraction_fails(
        self,
        routes_module: Any,
        store: state.StateStore,
        origin: Path,
        shas: dict[str, str],
        tmp_path: Path,
        isolated_env: dict[str, Path],
        bundle_url_patched: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The real git archive step succeeds (a genuine sha, a genuine

        clone) -- only `shutil.unpack_archive` is monkeypatched to raise,
        the single narrow failure point this test targets, per the task's
        instruction to monkeypatch only the narrow failure point while
        keeping the git path real. Covers `_materialize_pending_commit`'s
        `except (shutil.ReadError, OSError)` branch (routes.py:154-157),
        distinct from the git-call failure branch test 5 above already
        covers (routes.py:149-152).
        """
        import shutil as shutil_module

        merge_sha = shas["merge"]
        _seed_pending_for_merge(store, origin, tmp_path, sha=merge_sha)

        def _raising_unpack_archive(*args: Any, **kwargs: Any) -> None:
            raise shutil_module.ReadError("simulated: corrupt archive")

        monkeypatch.setattr(
            routes_module.shutil, "unpack_archive", _raising_unpack_archive
        )

        result = routes_module.approve(store, merge_sha)

        assert result.get("status") == "error"
        assert "extract" in result.get("reason", "")

        reloaded = state.load_state()
        assert reloaded.pending is not None
        assert reloaded.pending["sha"] == merge_sha


# ---------------------------------------------------------------------------
# 9. Coverage: routes.py:392-394 -- the #65 resolve-race branch, where
#    apply_commit succeeds (applied/partial) but state.resolve_pending then
#    refuses because pending moved mid-apply.
# ---------------------------------------------------------------------------


class TestApproveResolveRaceAfterSuccessfulApply:
    def test_approve_reports_both_apply_success_and_resolve_refusal(
        self,
        routes_module: Any,
        store: state.StateStore,
        origin: Path,
        shas: dict[str, str],
        tmp_path: Path,
        isolated_env: dict[str, Path],
        bundle_url_patched: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Simulates the #65 race routes.py's own approve docstring names:

        `apply_commit` is monkeypatched (the narrow, non-git point) to
        return a canned `applied` result WITHOUT actually mutating
        anything on disk -- isolating this test from the real apply
        pipeline test 1 already exercises -- while `state.resolve_pending`
        is left real, and pending is mutated out from under `approve`
        between its up-front sha check and the (stubbed) apply call, via a
        `store.accumulate_pending`-shaped race: a NEWER commit accumulates
        into pending after approve's own check but before its
        `resolve_pending` call. `resolve_pending` must then refuse (the
        pending sha no longer matches), and approve must report BOTH the
        apply outcome and the resolve refusal in one payload, per
        routes.py:387-394.
        """
        merge_sha = shas["merge"]
        _seed_pending_for_merge(store, origin, tmp_path, sha=merge_sha)

        class _StubResult:
            outcome = "applied"
            applied: list[str] = ["steering/x.md"]
            not_applied: list[str] = []
            ignored_paths: list[str] = []
            dropped_cron_names: list[str] = []
            paused_cron_names: list[str] = []
            changed_instance_names: list[str] = []
            incomplete_registrations: dict[str, list[str]] = {}
            needs_credential: list[str] = []
            propagation = None
            apply_id = "apply-test"
            reason = ""

        newer_sha = "e" * 40

        def _stub_apply_commit(**kwargs: Any) -> _StubResult:
            # The race: a newer commit accumulates into pending AFTER
            # approve's own up-front sha check (which already passed,
            # since this stub is only reached once it has) but BEFORE
            # approve's later `state.resolve_pending(sha)` call below.
            store.accumulate_pending(
                sha=newer_sha,
                author="Someone Else <b@example.com>",
                subject="a newer commit landed mid-apply",
                classified_paths={"steering/newer.md": "live_in_new_session"},
            )
            return _StubResult()

        monkeypatch.setattr(routes_module, "apply_commit", _stub_apply_commit)

        result = routes_module.approve(store, merge_sha)

        assert result.get("status") == "error"
        assert result.get("outcome") == "applied"
        assert result.get("applied") == ["steering/x.md"]
        assert "resolve_error" in result
        assert result["resolve_error"] != ""

        reloaded = state.load_state()
        assert reloaded.pending is not None
        assert reloaded.pending["sha"] == newer_sha, (
            "the resolve refusal must leave the RACING newer pending "
            "record in place -- approve's stale sha must never clear a "
            "commit the operator has not yet reviewed"
        )


# ---------------------------------------------------------------------------
# 10. Coverage: routes.py:404-407 -- apply_commit itself reports an outcome
#     that is neither "applied" nor "partial" (refused-sha-mismatch),
#     distinct from every green-path test above.
# ---------------------------------------------------------------------------


class TestApproveReportsNonAppliedOutcome:
    def test_approve_reports_error_when_apply_commit_refuses(
        self,
        routes_module: Any,
        store: state.StateStore,
        origin: Path,
        shas: dict[str, str],
        tmp_path: Path,
        isolated_env: dict[str, Path],
        bundle_url_patched: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`apply_commit` itself can report `outcome="refused-sha-mismatch"`

        even after routes.py's own up-front sha check passed -- the
        narrowest reachable case is the identical #65 race as test 9
        above, but observed from `apply_commit`'s OWN refusal branch
        rather than a successful apply followed by a resolve-race. Stub
        `apply_commit` directly (the narrow, non-git failure point) to
        return that exact outcome, and confirm approve's
        `result.outcome not in ("applied", "partial")` branch reports it
        as `status: "error"` with no `resolve_pending` call at all --
        distinct from test 9, which covers the case where `apply_commit`
        DOES report success and the race is caught downstream instead.
        """
        merge_sha = shas["merge"]
        _seed_pending_for_merge(store, origin, tmp_path, sha=merge_sha)

        class _RefusedResult:
            outcome = "refused-sha-mismatch"
            applied: list[str] = []
            not_applied: list[str] = []
            ignored_paths: list[str] = []
            dropped_cron_names: list[str] = []
            paused_cron_names: list[str] = []
            changed_instance_names: list[str] = []
            incomplete_registrations: dict[str, list[str]] = {}
            needs_credential: list[str] = []
            propagation = None
            apply_id = None
            reason = "no matching pending commit for the approved sha"

        def _stub_apply_commit(**kwargs: Any) -> _RefusedResult:
            return _RefusedResult()

        monkeypatch.setattr(routes_module, "apply_commit", _stub_apply_commit)

        base_sha_before = store.base_sha
        pending_before = dict(store.pending) if store.pending is not None else None

        result = routes_module.approve(store, merge_sha)

        assert result.get("status") == "error"
        assert result.get("outcome") == "refused-sha-mismatch"
        assert "resolve_error" not in result

        reloaded = state.load_state()
        assert reloaded.base_sha == base_sha_before, (
            "a non-applied/partial apply_commit outcome must never reach "
            "resolve_pending -- base_sha must stay exactly as it was"
        )
        reloaded_pending = reloaded.pending
        assert reloaded_pending is not None
        assert dict(reloaded_pending) == pending_before


# ---------------------------------------------------------------------------
# 11. tasks.md 7.5 seam: a pending commit that changes ONLY
#     agents/y.json (inline prompt), whose commit tree's config.json and
#     agent_model_state.json already carry y's key -> approve writes
#     agents/y.json's committed bytes to the live root B (KIRO_HOME).
#
# This is the seam test between routes.approve/apply_commit and
# registration.check_registrations' amended (5.14) tree-content rule: a
# root-B-only change, with both shared parts satisfied purely from the
# commit tree's content rather than from changed_paths membership, must
# actually complete and land on disk -- not merely be reported complete
# in isolation the way tests/test_registration.py's unit tests already
# prove. Root A ("root A only" in the module docstring above) is left
# untouched by this test; every other test in this file is unmodified.
# ---------------------------------------------------------------------------


def _seed_agent_y_only_history(origin: Path, tmp_path: Path) -> dict[str, str]:
    """Real history whose head commit changes ONLY ``agents/y.json`` --

    the commit tree ALSO carries ``config.json``/``agent_model_state.json``
    with ``y``'s key already present (added in the SAME commit here for
    fixture simplicity; 5.14's rule is that the shared files' CONTENT is
    what is read, regardless of whether they are themselves part of
    ``changed_paths`` -- this fixture's ``pending`` record below names
    only ``agents/y.json`` in ``classified_paths``, so the shared files'
    presence in this same commit's tree is exercised as tree content, not
    as a second changed path the seam depends on).
    """
    work = tmp_path / "agent-y-seed-work"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    _git("remote", "add", "origin", str(origin), cwd=work)

    (work / "agents").mkdir()
    (work / "agents" / "y.json").write_text(
        json.dumps(
            {
                "name": "y",
                "description": "Agent y.",
                "prompt": "You are agent y, an inline-prompt test agent.",
                "tools": ["read"],
                "allowedTools": ["read"],
                "resources": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (work / "config.json").write_text(
        json.dumps({"agents": {"y": {"source": "local", "model": "claude-sonnet-5"}}}),
        encoding="utf-8",
    )
    (work / "agent_model_state.json").write_text(
        json.dumps({"y": {"model_managed": False, "model": "claude-sonnet-5"}}),
        encoding="utf-8",
    )
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "register agent y", cwd=work)
    head_sha = _head_sha(work)
    _git("push", "-q", "-u", "origin", "main", cwd=work)

    return {"head": head_sha}


class TestApproveAgentJsonOnlyChangeWithSharedKeysAlreadyInTree:
    """tasks.md 7.5 / requirements.md 5.14 seam: agents/y.json is the

    ONLY path named in the pending record's classified_paths, yet the
    commit tree's config.json and agent_model_state.json already carry
    y's key -- approve must still complete the registration (inline
    prompt -> no required file part per 5.11(b)) and write
    agents/y.json's committed bytes to the live KIRO_HOME root.
    """

    def test_approve_agent_json_only_change_completes_and_writes_to_kiro_home(
        self,
        routes_module: Any,
        store: state.StateStore,
        origin: Path,
        tmp_path: Path,
        isolated_env: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _bundle_repo_url_env(monkeypatch, origin)
        shas = _seed_agent_y_only_history(origin, tmp_path)
        head_sha = shas["head"]

        # ONLY agents/y.json is named as a changed/classified path --
        # config.json and agent_model_state.json are deliberately absent
        # from classified_paths even though both exist, with y's key,
        # in the commit tree itself (5.14's tree-content rule is what
        # this seam test exercises).
        store.set_pending(
            sha=head_sha,
            author="Author <a@example.com>",
            subject="register agent y",
            classified_paths={"agents/y.json": "live_on_next_resolution"},
        )

        result = routes_module.approve(store, head_sha)

        live_agent_y = isolated_env["root_b"] / "agents" / "y.json"
        assert live_agent_y.is_file(), (
            "agents/y.json was never written to the live KIRO_HOME root -- "
            "an agent-JSON-only commit whose shared files already carry "
            "y's key in the commit tree must still complete registration "
            "and land on disk (requirements.md 5.14)"
        )

        written = json.loads(live_agent_y.read_text(encoding="utf-8"))
        assert written == {
            "name": "y",
            "description": "Agent y.",
            "prompt": "You are agent y, an inline-prompt test agent.",
            "tools": ["read"],
            "allowedTools": ["read"],
            "resources": [],
        }

        assert result.get("status") == "ok"

        reloaded = state.load_state()
        assert reloaded.base_sha == head_sha
        assert reloaded.pending is None


# ---------------------------------------------------------------------------
# 12. requirements.md 5.12 seam: a pending commit that ships a BRAND-NEW
#     agent z together with its prompt file (a ``file://`` reference,
#     never previously live on either root) -- approve must write both
#     agents/z.json (root B / KIRO_HOME) and the prompt file (root A /
#     KIROCREW_HOME) to their live roots. This is the main real-world
#     registration.check_registrations case 5.12 exists to unblock: a
#     new agent's prompt is present in the APPROVED COMMIT'S TREE but has
#     never been live before, so the registration must complete -- a
#     live-root-only check would refuse it as incomplete forever, since
#     nothing can ever be live before its own first apply.
# ---------------------------------------------------------------------------


def _seed_agent_z_with_prompt_history(origin: Path, tmp_path: Path) -> dict[str, str]:
    """Real history whose head commit adds a brand-new agent ``z``:

    ``agents/z.json`` (a ``file://`` prompt reference), the referenced
    ``config-bundles/agent-prompts/z.md`` prompt file itself, and both
    shared files carrying ``z``'s entry/pin -- all landing in the SAME
    commit, exactly the "ships a new agent together with its prompt
    file" case requirements.md 5.12 names. Nothing here has ever been
    live on either root before this commit is approved.
    """
    work = tmp_path / "agent-z-seed-work"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    _git("remote", "add", "origin", str(origin), cwd=work)

    (work / "agents").mkdir()
    (work / "agents" / "z.json").write_text(
        json.dumps(
            {
                "name": "z",
                "description": "Agent z.",
                "prompt": "file://${KIROCREW_HOME}/config-bundles/agent-prompts/z.md",
                "tools": ["read"],
                "allowedTools": ["read"],
                "resources": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (work / "config-bundles" / "agent-prompts").mkdir(parents=True)
    (work / "config-bundles" / "agent-prompts" / "z.md").write_text(
        "# z\n\nYou are agent z, a brand-new agent.\n", encoding="utf-8"
    )
    (work / "config.json").write_text(
        json.dumps({"agents": {"z": {"source": "local", "model": "claude-sonnet-5"}}}),
        encoding="utf-8",
    )
    (work / "agent_model_state.json").write_text(
        json.dumps({"z": {"model_managed": False, "model": "claude-sonnet-5"}}),
        encoding="utf-8",
    )
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "register agent z with its prompt", cwd=work)
    head_sha = _head_sha(work)
    _git("push", "-q", "-u", "origin", "main", cwd=work)

    return {"head": head_sha}


class TestApproveNewAgentWithPromptNeverPreviouslyLiveCompletes:
    """requirements.md 5.12's main real-world case: a commit that ships a

    NEW agent (``z``) together with its own prompt file, both changed in
    the same commit and neither ever having existed on any live root
    before. The prompt part must count as present because it exists in
    the approved commit's tree -- approve must write agents/z.json to
    the live KIRO_HOME root AND the prompt file to the live KIROCREW_HOME
    root, completing the registration rather than refusing it as
    incomplete for a prompt that (correctly, for a brand-new agent) was
    never live.
    """

    def test_approve_writes_new_agent_definition_and_its_prompt_to_live_roots(
        self,
        routes_module: Any,
        store: state.StateStore,
        origin: Path,
        tmp_path: Path,
        isolated_env: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _bundle_repo_url_env(monkeypatch, origin)
        shas = _seed_agent_z_with_prompt_history(origin, tmp_path)
        head_sha = shas["head"]

        # Every changed path from the commit is named, matching how a
        # real poll/classify pass would report a multi-file commit --
        # unlike the agent-y seam test above (which deliberately omits
        # the shared files from classified_paths to exercise 5.14's
        # tree-content rule), this seam test's own point is the PROMPT
        # FILE's tree-presence, so it is named here explicitly.
        store.set_pending(
            sha=head_sha,
            author="Author <a@example.com>",
            subject="register agent z with its prompt",
            classified_paths={
                "agents/z.json": "live_on_next_resolution",
                "config-bundles/agent-prompts/z.md": "live_in_new_session",
                "config.json": "live_after_cache_invalidation",
                "agent_model_state.json": "live_after_cache_invalidation",
            },
        )

        # Neither the agent definition nor the prompt file has ever been
        # written to any live root before this approval.
        live_agent_z = isolated_env["root_b"] / "agents" / "z.json"
        live_prompt_z = (
            isolated_env["root_a"] / "config-bundles" / "agent-prompts" / "z.md"
        )
        assert not live_agent_z.exists()
        assert not live_prompt_z.exists()

        result = routes_module.approve(store, head_sha)

        assert live_agent_z.is_file(), (
            "agents/z.json was never written to the live KIRO_HOME root -- "
            "a brand-new agent shipped together with its prompt file must "
            "still complete registration and land on disk (requirements.md "
            "5.12)"
        )
        written = json.loads(live_agent_z.read_text(encoding="utf-8"))
        assert written["name"] == "z"
        # requirements.md 4.11 / design.md apply step 4: expand runs on
        # every applied in-scope JSON file, so the token form the commit
        # carries is rewritten to THIS host's own KIROCREW_HOME absolute
        # path on disk -- the token itself must not survive.
        root_a = isolated_env["root_a"]
        assert written["prompt"] == (
            f"file://{root_a}/config-bundles/agent-prompts/z.md"
        )
        assert "${KIROCREW_HOME}" not in written["prompt"]

        assert live_prompt_z.is_file(), (
            "config-bundles/agent-prompts/z.md was never written to the "
            "live KIROCREW_HOME root -- approve() must apply the prompt "
            "file alongside the agent definition it was shipped with"
        )
        assert (
            live_prompt_z.read_text(encoding="utf-8")
            == "# z\n\nYou are agent z, a brand-new agent.\n"
        )

        assert result.get("status") == "ok"

        reloaded = state.load_state()
        assert reloaded.base_sha == head_sha
        assert reloaded.pending is None
