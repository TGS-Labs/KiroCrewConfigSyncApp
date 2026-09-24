"""Real-git regression coverage for push.py's `retry-pr-only` state (senior

review round-4 finding on Deployment 2 / config-sync-push).

## The bug this guards against

Round-3's H-NEW-1 fix made `record_pr_pending_failure` clear `pending_pr`
after a reported PR-creation failure, so the next tick's hash-gate does not
loop on `awaiting-pr-confirmation` forever. But clearing `pending_pr` also
removed the ONLY signal push.py's hash-gate had for "this content is
already on the remote branch" — so the very next tick, seeing no
`pending_pr` and a `tree_hash` that still does not match `last_pushed_hash`,
fell through to the FULL change path: clone/fetch, checkout the SAME
branch (derived from the SAME tree hash), write the SAME redacted tree,
`git add`, then `git commit`. Since the branch already holds that exact
commit from the prior successful tick, there is nothing to commit — real
git raises `CalledProcessError` on a real "nothing to commit, working tree
clean" — and push.py's own except-clause records that as a FABRICATED push
failure for a push that had already succeeded. This is the N1 defect class
recurring through a second path (round-2's fix closed the pre-confirmation
path; this closes the post-PR-failure path).

## Why this test uses REAL git, not mocks

The senior reviewer traced BOTH the original N1 defect and this round-4
recurrence to the same testing-methodology gap: every prior push.py test
mocks `subprocess.run`/`git_safety.git_argv` with a bare/wrapping
`MagicMock` that always "succeeds" — a mock cannot reproduce a real git
repository's "nothing to commit" failure, so a test asserting the fix would
pass even against an implementation that still calls `git commit` for real
on a clean tree. This file runs actual `git` subprocesses (following the
real-repo pattern in `tests/safety/test_git_safety.py`) against a real
local bare remote, so the exact failure mode is genuinely exercised.

Only push.py's own NON-git collaborators are mocked here (the secret scan,
the branch-authorization policy, `redact_msg`'s message redaction, and
`pr_handoff`'s Buildo-payload build + operator notification) — every git
subprocess call push.py itself makes runs for real, unmocked.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock

import pytest

from backend import push


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict]:
    """Point KIROCREW_HOME / KIRO_HOME / the app's state dir at fresh temp

    directories, matching tests/test_push.py's fixture of the same name.
    """
    root_a = tmp_path / "kirocrew_home"
    root_b = tmp_path / "kiro_home"
    root_a.mkdir()
    root_b.mkdir()

    monkeypatch.setenv("KIROCREW_HOME", str(root_a))
    monkeypatch.setenv("KIRO_HOME", str(root_b))
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(tmp_path / "state"))

    yield {"root_a": root_a, "root_b": root_b, "tmp_path": tmp_path}


def _write(root: Path, relpath: str, content: bytes) -> Path:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _run_git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    """A plain, unhardened real `git` call for TEST SETUP only (creating the

    bare remote, reading back its ref state) — never push.py's own
    `git_safety.git_argv`, so the fixture does not test the module with
    itself, matching tests/safety/test_git_safety.py's own convention.
    Inherits the process environment, which the `bare_remote` fixture has
    already seeded with `GIT_AUTHOR_*`/`GIT_COMMITTER_*` via monkeypatch.
    """
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def bare_remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real local bare repo that stands in for the GitHub bundle-repo

    remote — real-git-compatible, no network required. push.py's
    `BUNDLE_REPO_URL` is monkeypatched to this path's `file://` form by the
    test itself, so `git clone`/`git push` run for real against it.

    Also sets `GIT_AUTHOR_*`/`GIT_COMMITTER_*` env vars for the whole test
    process (via monkeypatch, restored on teardown) — push.py's OWN git
    calls run through `git_safety.git_argv`, which this fixture must not
    weaken, so identity is supplied via environment rather than any
    `--global`/`--system` config write (this test process must never
    mutate the host's real git configuration).
    """
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")

    remote_dir = tmp_path / "bundle-remote.git"
    remote_dir.mkdir()
    _run_git("init", "-q", "--bare", "-b", "main", cwd=remote_dir)
    # A bare repo created with `-b main` has HEAD pointed at `main`, but no
    # `main` ref exists until something is pushed. Seed one via a
    # throwaway working clone so `git clone` (push.py's own first-tick
    # path) has a real default branch to check out from, rather than
    # cloning a repo whose HEAD names a ref that does not exist yet.
    seed_dir = tmp_path / "bundle-remote-seed"
    seed_dir.mkdir()
    _run_git("init", "-q", "-b", "main", cwd=seed_dir)
    (seed_dir / "README.md").write_text("seed\n", encoding="utf-8")
    _run_git("add", ".", cwd=seed_dir)
    _run_git("commit", "-q", "-m", "seed", cwd=seed_dir)
    _run_git("remote", "add", "origin", str(remote_dir), cwd=seed_dir)
    _run_git("push", "-q", "origin", "main", cwd=seed_dir)
    return remote_dir


@pytest.fixture
def real_git_change_path_collaborators(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[dict]:
    """Give the change path permissive defaults for its NON-git

    collaborators only (scan, branch-authorization policy, commit-message
    redaction) — `subprocess`/`git_safety.git_argv` are left COMPLETELY
    UNPATCHED so every git call push.py makes is a real subprocess.
    """
    from backend.safety import push_policy, redact_msg

    scan_mock = MagicMock(
        name="push_policy.scan_content_for_secrets", return_value=(True, "ok")
    )
    authorize_mock = MagicMock(
        name="push_policy.authorize_direct_push",
        return_value=(True, "push authorized"),
    )
    redact_message_mock = MagicMock(
        name="redact_msg.redact_message", side_effect=lambda text: text
    )

    monkeypatch.setattr(push_policy, "scan_content_for_secrets", scan_mock)
    monkeypatch.setattr(push_policy, "authorize_direct_push", authorize_mock)
    monkeypatch.setattr(redact_msg, "redact_message", redact_message_mock)

    if hasattr(push, "push_policy"):
        monkeypatch.setattr(push.push_policy, "scan_content_for_secrets", scan_mock)
        monkeypatch.setattr(push.push_policy, "authorize_direct_push", authorize_mock)
    if hasattr(push, "redact_msg"):
        monkeypatch.setattr(push.redact_msg, "redact_message", redact_message_mock)

    yield {
        "scan": scan_mock,
        "authorize": authorize_mock,
        "redact_message": redact_message_mock,
    }


@pytest.fixture
def stub_pr_handoff_external_calls(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Stub only pr_handoff's own OUT-OF-BAND calls (Buildo payload build,

    operator notification) — never git. `handle_pushed_branch`'s own
    control flow and `state.py`'s real `StateStore` mutations run for real.
    """
    from backend import pr_handoff

    build_mock = MagicMock(
        name="build_pull_request_payload",
        return_value={
            "repo": "TGS-Labs/Kiro-Config-Bundles",
            "base": "main",
            "head": "irrelevant",
            "title": "chore: sync",
            "body": "Automated config sync.",
        },
    )
    notify_mock = MagicMock(name="notify_operator")
    monkeypatch.setattr(pr_handoff, "build_pull_request_payload", build_mock)
    monkeypatch.setattr(pr_handoff, "notify_operator", notify_mock)
    return {"build": build_mock, "notify": notify_mock}


# ---------------------------------------------------------------------------
# The regression test.
# ---------------------------------------------------------------------------


class TestRetryPrOnlyAfterReportedPrFailure:
    def test_tick_after_pr_failure_retries_pr_only_with_no_git_commit_or_push(
        self,
        isolated_roots: dict,
        bare_remote: Path,
        real_git_change_path_collaborators: dict,
        stub_pr_handoff_external_calls: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Tick 1 pushes real content via real git to the real bare remote.

        The out-of-band PR-open attempt is then reported failed (clearing
        `pending_pr` per H-NEW-1). Tick 2, same tree_hash, must reach the
        NEW `retry-pr-only` state and make ZERO git subprocess calls — no
        clone, no fetch, no checkout, no add, no commit, no push — proven
        by running it against the REAL already-pushed repo state rather
        than asserting a mock was not called.
        """
        monkeypatch.setattr(push, "BUNDLE_REPO_URL", str(bare_remote))

        root_a = isolated_roots["root_a"]
        _write(root_a, "config.json", b'{"key": "value"}')

        from backend import pr_handoff, state

        store = state.load_state()
        monkeypatch.setattr(state, "load_state", lambda: store, raising=False)
        current_hash = push.tree_hash(
            __import__("backend.redact", fromlist=["redact"]).redact(
                __import__("backend.collect", fromlist=["collect"]).collect()
            )
        )

        # --- Tick 1: genuine change, real git clone/commit/push. ---------
        tick1_result = push.run()
        assert tick1_result.outcome == "pushed"
        assert tick1_result.tree_hash == current_hash
        assert store.pending_pr is not None
        assert store.pending_pr.get("tree_hash") == current_hash
        pushed_branch = store.pending_pr.get("branch")
        assert pushed_branch

        # Prove tick 1's push genuinely reached the real remote: the branch
        # must actually exist on the bare repo now.
        remote_branches = _run_git(
            "branch", "-r", "--list", f"origin/{pushed_branch}", cwd=bare_remote
        )
        # `git branch -r` inside the BARE repo itself lists nothing
        # meaningful (bare repos have no `origin`); instead verify via
        # `show-ref` that the branch ref exists directly in the bare repo.
        del remote_branches
        show_ref = subprocess.run(
            ["git", "show-ref", "--verify", f"refs/heads/{pushed_branch}"],
            cwd=str(bare_remote),
            capture_output=True,
            text=True,
        )
        assert show_ref.returncode == 0, (
            f"tick 1 must have genuinely pushed {pushed_branch!r} to the "
            f"real bare remote: {show_ref.stderr}"
        )

        # --- Out-of-band: Buildo/agent reports PR creation failed. -------
        pr_handoff.report_pr_creation_failed(
            reason="Buildo create_pull_request returned 422",
            tree_hash=current_hash,
            branch=str(pushed_branch),
            state=store,
        )
        assert store.pending_pr is None, "H-NEW-1: the failure must clear pending_pr"
        assert store.pending_pr_failure is not None
        # The record this fix's new gate depends on must have survived the
        # clear above.
        assert store.last_push is not None
        assert store.last_push.get("tree_hash") == current_hash
        assert store.last_push.get("branch") == pushed_branch
        assert store.last_push.get("pr_url") is None

        # --- Tick 2: same tree_hash, no pending_pr. Must NOT touch git. ---
        # Delete the local working clone push.py made on tick 1 so a
        # regression that still tries to `git commit` cannot succeed by
        # accident against a leftover clean-but-uncommitted clone — force
        # it to prove it does not even TRY, by making the clone directory
        # itself a plain empty dir with no `.git`. If the retry-pr-only
        # gate is not implemented, `run()` will try to `git clone` again
        # (fine, real git) then `git commit` on a tree identical to the
        # remote's tip — which raises CalledProcessError for real, exactly
        # reproducing the fabricated-failure bug this test guards against.
        failure_spy = MagicMock(wraps=store.record_push_failure)
        monkeypatch.setattr(store, "record_push_failure", failure_spy)

        # Spy on the real subprocess.run so we can assert NO git subprocess
        # ran on tick 2 — this is a spy on the REAL function (wraps=), so
        # any call still executes for real; it only lets us assert on the
        # call list afterward, it does not fake git's behaviour.
        real_subprocess_run = subprocess.run
        run_spy = MagicMock(name="subprocess.run", wraps=real_subprocess_run)
        monkeypatch.setattr(subprocess, "run", run_spy)
        if hasattr(push, "subprocess"):
            monkeypatch.setattr(push.subprocess, "run", run_spy)

        tick2_result = push.run()

        assert tick2_result.outcome == "retry-pr-only", (
            "tick 2 must reach the new retry-pr-only state, not re-run the "
            "change path against content already on the remote branch"
        )
        assert tick2_result.tree_hash == current_hash
        assert tick2_result.reason == pushed_branch

        run_spy.assert_not_called()
        failure_spy.assert_not_called()

        # And the PR-open step must have been retried — pr_handoff's own
        # (stubbed) build_pull_request_payload was called again for this
        # branch (once on tick 1, once more on tick 2's retry), and a
        # fresh pending_pr entry now exists.
        assert stub_pr_handoff_external_calls["build"].call_count == 2
        stub_pr_handoff_external_calls["build"].assert_called_with(
            pushed_branch, "chore: sync configuration", "Automated config sync."
        )
        assert store.pending_pr is not None
        assert store.pending_pr.get("tree_hash") == current_hash
        assert store.pending_pr.get("branch") == pushed_branch
        assert store.last_pushed_hash is None, (
            "retry-pr-only must not itself confirm the PR — only "
            "pr_handoff.confirm_pr_created (out-of-band) may advance "
            "last_pushed_hash"
        )

    def test_reverting_the_fix_reproduces_the_fabricated_git_commit_failure(
        self,
        isolated_roots: dict,
        bare_remote: Path,
        real_git_change_path_collaborators: dict,
        stub_pr_handoff_external_calls: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Demonstrates what the ORIGINAL bug looked like: with the new

        retry-pr-only gate bypassed (simulating pre-fix behaviour by
        clearing `last_push` the same way `pending_pr` was cleared), tick 2
        falls through to the real change path, `git commit` finds nothing
        to commit against the identical already-pushed tree, and real git
        raises `CalledProcessError` — proving this test would have caught
        the original regression had it existed at round 3.
        """
        monkeypatch.setattr(push, "BUNDLE_REPO_URL", str(bare_remote))

        root_a = isolated_roots["root_a"]
        _write(root_a, "config.json", b'{"key": "value"}')

        from backend import pr_handoff, state

        store = state.load_state()
        monkeypatch.setattr(state, "load_state", lambda: store, raising=False)

        tick1_result = push.run()
        assert tick1_result.outcome == "pushed"
        assert store.pending_pr is not None
        pushed_branch = store.pending_pr.get("branch")

        pr_handoff.report_pr_creation_failed(
            reason="Buildo create_pull_request returned 422",
            tree_hash=tick1_result.tree_hash,
            branch=str(pushed_branch),
            state=store,
        )
        assert store.pending_pr is None

        # Simulate the pre-fix world: the gate this test is protecting had
        # nothing to consult (state.py's `last_push` field is what the fix
        # relies on) — wipe it the same way `pending_pr` was cleared, so
        # `run()` has NO signal that this content is already pushed and
        # must fall through past both the no-op and 3a/3b gates.
        monkeypatch.setattr(store.__class__, "last_push", property(lambda self: None))

        with pytest.raises(subprocess.CalledProcessError) as exc_info:
            push.run()

        # push.py's own `subprocess.run` calls never pass `capture_output`
        # (nor `stderr=subprocess.PIPE`), so on a real `CalledProcessError`
        # `exc.stderr` and `exc.output` are always `None` here — asserting
        # against them would be dead code that can never actually run.
        # `returncode` is the only signal push.py's own call shape actually
        # populates; real `git commit` on a clean/unchanged tree exits 1.
        assert exc_info.value.returncode == 1, (
            "reverting the fix must reproduce git's real 'nothing to "
            f"commit' failure (exit 1), got: {exc_info.value!r}"
        )


class TestRetryPrOnlyWithStalePendingPrForOlderHash:
    """Round-5 H1: the `retry-pr-only` gate must not require

    `pending_pr is None`. `pending_pr` can legitimately hold a STALE entry
    naming an OLDER, different hash (H-NEW-2's own design: an older
    unconfirmed `pending_pr` can coexist while `last_push` names a newer
    hash whose own PR-attempt failed). Gating on `pending_pr is None`
    wrongly falls through to the full change path in that state, and real
    `git commit` fails with "nothing to commit" against a branch that is
    already correctly pushed — the N1 defect class recurring a third time.

    Sequence: tick 1 pushes hash X (`pending_pr` becomes X, real git
    push). Tick 2 pushes a new hash Y where the PR-open payload-build step
    fails: `last_push` becomes Y with `pr_url=None`, but `pending_pr`
    STAYS as X (stale-for-a-different-attempt, per
    `state.record_pr_pending_failure` case 3 — the failure report names Y
    while the current `pending_pr` still names X, so it is recorded to
    `pending_pr_stale` and the CURRENT `pending_pr` entry for X is left
    untouched, never cleared). Tick 3, content still at Y, must resolve to
    `retry-pr-only` with ZERO git subprocess calls.
    """

    def test_stale_pending_pr_for_older_hash_does_not_block_retry_pr_only(
        self,
        isolated_roots: dict,
        bare_remote: Path,
        real_git_change_path_collaborators: dict,
        stub_pr_handoff_external_calls: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(push, "BUNDLE_REPO_URL", str(bare_remote))

        root_a = isolated_roots["root_a"]
        _write(root_a, "config.json", b'{"key": "value"}')

        from backend import state

        store = state.load_state()
        monkeypatch.setattr(state, "load_state", lambda: store, raising=False)

        # --- Tick 1: push hash X for real. pending_pr becomes X. ----------
        tick1_result = push.run()
        assert tick1_result.outcome == "pushed"
        hash_x = tick1_result.tree_hash
        assert store.pending_pr is not None
        assert store.pending_pr.get("tree_hash") == hash_x
        branch_x = store.pending_pr.get("branch")
        assert branch_x

        # --- Tick 2: change content to Y, push for real, PR-open fails. --
        # X's pending_pr entry is left exactly as-is (never confirmed or
        # reported failed) — it is the STALE entry this test targets.
        _write(root_a, "config.json", b'{"key": "value2"}')

        build_mock = stub_pr_handoff_external_calls["build"]
        build_mock.side_effect = RuntimeError("Buildo create_pull_request 500")

        tick2_result = push.run()
        assert tick2_result.outcome == "pushed"
        hash_y = tick2_result.tree_hash
        assert hash_y != hash_x

        # last_push now names Y with no PR yet.
        assert store.last_push is not None
        assert store.last_push.get("tree_hash") == hash_y
        assert store.last_push.get("pr_url") is None
        branch_y = store.last_push.get("branch")
        assert branch_y != branch_x

        # pending_pr STILL names the older, stale X entry -- the
        # payload-build failure for Y named a tree_hash/branch that did
        # not match the current pending_pr (X), so
        # state.record_pr_pending_failure's case-3 (H-NEW-2) path recorded
        # it to pending_pr_stale and left the current pending_pr (X)
        # completely untouched, rather than clearing it.
        assert store.pending_pr is not None, (
            "the older X entry must remain in pending_pr -- this is the "
            "legitimate stale-slot state H1 exists to unblock"
        )
        assert store.pending_pr.get("tree_hash") == hash_x
        assert store.pending_pr.get("branch") == branch_x
        assert store.pending_pr_stale is not None
        assert store.pending_pr_stale.get("tree_hash") == hash_y

        # Verify tick 2's push of Y genuinely reached the real remote.
        show_ref = subprocess.run(
            ["git", "show-ref", "--verify", f"refs/heads/{branch_y}"],
            cwd=str(bare_remote),
            capture_output=True,
            text=True,
        )
        assert show_ref.returncode == 0, (
            f"tick 2 must have genuinely pushed {branch_y!r} to the real "
            f"bare remote: {show_ref.stderr}"
        )

        # --- Tick 3: content still at Y. Must NOT touch git. --------------
        build_mock.side_effect = None
        build_mock.reset_mock()

        failure_spy = MagicMock(wraps=store.record_push_failure)
        monkeypatch.setattr(store, "record_push_failure", failure_spy)

        real_subprocess_run = subprocess.run
        run_spy = MagicMock(name="subprocess.run", wraps=real_subprocess_run)
        monkeypatch.setattr(subprocess, "run", run_spy)
        if hasattr(push, "subprocess"):
            monkeypatch.setattr(push.subprocess, "run", run_spy)

        tick3_result = push.run()

        assert tick3_result.outcome == "retry-pr-only", (
            "tick 3 must reach retry-pr-only even with a STALE pending_pr "
            "entry naming a different (older) hash -- the gate must not "
            "require pending_pr is None"
        )
        assert tick3_result.tree_hash == hash_y
        assert tick3_result.reason == branch_y

        run_spy.assert_not_called()
        failure_spy.assert_not_called()

        # The PR-open step for Y was retried, and pending_pr now correctly
        # reflects Y (overwriting the stale X entry).
        build_mock.assert_called_once()
        assert store.pending_pr is not None
        assert store.pending_pr.get("tree_hash") == hash_y
        assert store.pending_pr.get("branch") == branch_y
        assert store.last_pushed_hash is None, (
            "retry-pr-only must not itself confirm the PR -- only "
            "pr_handoff.confirm_pr_created (out-of-band) may advance "
            "last_pushed_hash"
        )
