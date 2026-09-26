"""Tests for `backend/poll.py` (design.md "backend/poll.py", tasks.md 4.1).

Covers design.md's poll-job component ("`git ls-remote <bundle-repo>
<default-branch>` -> head SHA. Unchanged -> exit. Changed -> ...") and
requirements.md 4.1 / 4.2:

- 4.1: the poll job runs as a `command` cron target (never `message`), so a
  tick consumes no LLM tokens, and resolves the bundle repo's default-branch
  head via `git ls-remote` — never a full clone.
- 4.2: WHEN the resolved head equals `state.last_seen_sha` THEN the job
  exits 0 having sent no notification and made no further git call beyond
  the single `ls-remote`. WHEN the head differs THEN this must be
  observable in the job's returned result so the next wave's
  classify/pending-record logic (task 4.2/4.3) can consume it — this task
  only asserts the signal exists, not that pending-record logic runs.
  WHEN `git ls-remote` itself fails (non-zero exit / raises) THEN the job
  exits non-zero, `state.last_seen_sha` is left UNCHANGED, and no
  notification is sent.

`git_safety.git_argv` is the single call-site every host-side git
invocation in this app must route through (see
backend/safety/git_safety.py's own docstring and
tests/safety/test_git_safety.py's static grep test). poll.py's `ls-remote`
call must be built through it — never a bare `["git", ...]` argv — so
`git_argv` is spied (wrapped, not replaced) here rather than bypassed, and
`subprocess.run`/`Popen` are mocked to control the ls-remote outcome
without ever touching the network or a real repo.

This module intentionally imports `poll` (backend/poll.py), which does not
exist yet. All tests below are expected to fail with a collection-time
ImportError / ModuleNotFoundError until software-engineer implements it —
this is the correct TDD starting state, not a test defect.
"""

from __future__ import annotations

import runpy
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Iterator
from unittest.mock import MagicMock

import pytest

from backend import poll

# ---------------------------------------------------------------------------
# Fixtures — matching test_push.py's / test_state.py's isolated-roots and
# isolated-state-dir convention, so no test here ever touches the real host
# configuration or the real ~/.config-sync state.
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Point the app's own state directory at a fresh, disposable temp

    directory, matching test_push.py's / test_state.py's convention. No
    test here may touch the real host's ~/.config-sync state.
    """
    state_dir = tmp_path / "state"
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(state_dir))
    yield state_dir


@pytest.fixture
def git_argv_spy(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Spy on (wrap, don't replace) `git_safety.git_argv` so tests can

    assert poll.py's ls-remote call was built through the hardened argv
    builder, matching every other host-side git call in this app. Applies
    to both `backend.safety.git_safety`'s own namespace and `backend.poll`'s
    namespace, in case `poll.py` did `from backend.safety.git_safety import
    git_argv` rather than `from backend.safety import git_safety`.
    """
    from backend.safety import git_safety

    spy = MagicMock(name="git_safety.git_argv", wraps=git_safety.git_argv)
    monkeypatch.setattr(git_safety, "git_argv", spy)
    if hasattr(poll, "git_argv"):
        monkeypatch.setattr(poll, "git_argv", spy)
    if hasattr(poll, "git_safety"):
        monkeypatch.setattr(poll.git_safety, "git_argv", spy)
    return spy


@pytest.fixture
def no_further_git_calls(
    git_argv_spy: MagicMock,
) -> Callable[[], None]:
    """Return an assertion helper asserting `git_argv` was called AT MOST

    once — the single `ls-remote` the design allows — never a second git
    invocation (e.g. no fetch, no clone) from a single poll tick, matching
    design.md's "no full clone" instruction for this job.
    """

    def _assert_at_most_one_call() -> None:
        assert git_argv_spy.call_count <= 1, (
            "poll.py made more than one git_argv call in a single tick; "
            "design.md's poll job is a single ls-remote, not a clone"
        )

    return _assert_at_most_one_call


@pytest.fixture
def notify_spy(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Spy on/replace the poll job's notification seam so tests can assert

    whether a notification was sent, without requiring a live notification
    channel. Mirrors `pr_handoff.py`'s `notify_operator` seam, which is the
    established pattern in this codebase for a patchable, currently-no-op
    notify call. Tries the conventional names in order; fails loudly if
    poll.py exposes neither, since a poll job with no patchable notify seam
    cannot be tested for Requirement 4.2's "no notification sent" property.
    """
    for name in ("notify_operator", "notify"):
        if hasattr(poll, name):
            spy = MagicMock(name=f"poll.{name}")
            monkeypatch.setattr(poll, name, spy)
            return spy
    raise AssertionError(
        "backend/poll.py must expose a patchable notify seam named "
        "'notify_operator' or 'notify' (matching pr_handoff.py's "
        "notify_operator convention) so tests can assert no-notification "
        "on the unchanged-head and ls-remote-failure paths"
    )


def _run_subprocess_mock(*, stdout: str = "", returncode: int = 0) -> MagicMock:
    """Build a `subprocess.run` replacement returning a fixed

    `CompletedProcess`-shaped result, matching the way `git ls-remote`'s
    stdout (``"<sha>\\trefs/heads/<branch>\\n"``) is consumed.
    """
    completed = MagicMock(name="CompletedProcess")
    completed.stdout = stdout
    completed.returncode = returncode
    return MagicMock(name="subprocess.run", return_value=completed)


# ---------------------------------------------------------------------------
# Requirement 4.2(a) — unchanged head: exit 0, no notification, no further
# git call beyond the single ls-remote.
# ---------------------------------------------------------------------------


def test_unchanged_head_exits_zero(
    isolated_state_dir: Path,
    git_argv_spy: MagicMock,
    no_further_git_calls: Callable[[], None],
    notify_spy: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WHEN the resolved ls-remote head equals state.last_seen_sha THEN

    poll.run() must succeed (exit 0 / no raise) — the common, quiet-tick
    case (requirements.md 4.2, design.md "Unchanged -> exit").
    """
    sha = "a" * 40
    run_mock = _run_subprocess_mock(stdout=f"{sha}\trefs/heads/main\n")
    monkeypatch.setattr(subprocess, "run", run_mock)
    if hasattr(poll, "subprocess"):
        monkeypatch.setattr(poll.subprocess, "run", run_mock)

    from backend import state

    store = state.load_state()
    store.record_seen_sha(sha)

    result = poll.run()

    outcome = getattr(result, "outcome", result)
    assert str(outcome).lower() not in ("error", "failure", "failed")
    no_further_git_calls()


def test_unchanged_head_sends_no_notification(
    isolated_state_dir: Path,
    git_argv_spy: MagicMock,
    notify_spy: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WHEN the head is unchanged THEN no notification is sent — the

    unchanged-head tick must never call the poll job's notify seam
    (requirements.md 4.2).
    """
    sha = "b" * 40
    run_mock = _run_subprocess_mock(stdout=f"{sha}\trefs/heads/main\n")
    monkeypatch.setattr(subprocess, "run", run_mock)
    if hasattr(poll, "subprocess"):
        monkeypatch.setattr(poll.subprocess, "run", run_mock)

    from backend import state

    store = state.load_state()
    store.record_seen_sha(sha)

    poll.run()

    notify_spy.assert_not_called()


# ---------------------------------------------------------------------------
# `__main__` guard — exercised via `runpy` so the module-execution branch
# (`python3 -m backend.poll`, requirements.md 4.1's cron `command` shape)
# is genuinely covered rather than excluded, using the same clean-run mock
# pattern as `test_unchanged_head_exits_zero` so it never touches real
# network or git.
# ---------------------------------------------------------------------------


def test_main_guard_invokes_run_when_executed_as_a_module(
    isolated_state_dir: Path,
    git_argv_spy: MagicMock,
    notify_spy: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WHEN `backend/poll.py` is executed as `__main__` (the

    `python3 -m backend.poll` cron entrypoint, requirements.md 4.1) THEN
    its `if __name__ == "__main__":` guard must actually call `run()` —
    exercised for real via `runpy.run_module`, not skipped, on an
    unchanged-head tick so the run completes cleanly with no real
    network/git access.

    The guard now converts `run()`'s outcome into a process exit code
    (senior-review H2: a cron `command` target must exit non-zero on
    failure, requirements.md's error table) — an unchanged-head tick is
    success, so this must raise `SystemExit(0)`, not return silently.
    """
    sha = "7" * 40
    run_mock = _run_subprocess_mock(stdout=f"{sha}\trefs/heads/main\n")
    monkeypatch.setattr(subprocess, "run", run_mock)

    from backend import state

    store = state.load_state()
    store.record_seen_sha(sha)

    # `backend.poll` is already imported (module-level `from backend import
    # poll` above) — drop it from sys.modules first so `runpy.run_module`
    # genuinely re-executes the module body under `__name__ == "__main__"`
    # instead of warning about a stale cached entry. Restored in `finally`
    # so later tests keep using the same `poll` object their fixtures
    # patched.
    monkeypatch.delitem(sys.modules, "backend.poll", raising=False)
    try:
        with pytest.raises(SystemExit) as exc_info:
            runpy.run_module("backend.poll", run_name="__main__", alter_sys=True)
        assert exc_info.value.code == 0
    finally:
        sys.modules["backend.poll"] = poll

    run_mock.assert_called_once()


def test_unchanged_head_makes_no_further_git_calls(
    isolated_state_dir: Path,
    git_argv_spy: MagicMock,
    no_further_git_calls: Callable[[], None],
    notify_spy: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WHEN the head is unchanged THEN the ONLY git invocation for the

    whole tick is the single ls-remote that discovered the (unchanged)
    head — no fetch, no clone, no second ls-remote (design.md: "no full
    clone", and the unchanged path does strictly less work than the
    changed path).
    """
    sha = "c" * 40
    run_mock = _run_subprocess_mock(stdout=f"{sha}\trefs/heads/main\n")
    monkeypatch.setattr(subprocess, "run", run_mock)
    if hasattr(poll, "subprocess"):
        monkeypatch.setattr(poll.subprocess, "run", run_mock)

    from backend import state

    store = state.load_state()
    store.record_seen_sha(sha)

    poll.run()

    no_further_git_calls()
    assert git_argv_spy.call_count == 1


def test_unchanged_head_uses_git_argv_for_the_git_call(
    isolated_state_dir: Path,
    git_argv_spy: MagicMock,
    notify_spy: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ls-remote call must be built through `git_safety.git_argv` —

    never a bare `["git", ...]` argv — matching every other host-side git
    call in this app (backend/safety/git_safety.py's own docstring;
    tests/safety/test_git_safety.py's static grep test).
    """
    sha = "d" * 40
    run_mock = _run_subprocess_mock(stdout=f"{sha}\trefs/heads/main\n")
    monkeypatch.setattr(subprocess, "run", run_mock)
    if hasattr(poll, "subprocess"):
        monkeypatch.setattr(poll.subprocess, "run", run_mock)

    from backend import state

    store = state.load_state()
    store.record_seen_sha(sha)

    poll.run()

    git_argv_spy.assert_called()
    called_args = git_argv_spy.call_args[0]
    assert "ls-remote" in called_args


# ---------------------------------------------------------------------------
# Requirement 4.2(b) — changed head: the difference must be signaled/
# returned so the next wave's classify/pending-record logic can consume it.
# No claim is made here about what that next-wave logic DOES.
# ---------------------------------------------------------------------------


def test_changed_head_is_signaled_in_the_result(
    isolated_state_dir: Path,
    git_argv_spy: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WHEN the resolved head DIFFERS from state.last_seen_sha THEN

    poll.run()'s result must carry BOTH the new head SHA and a signal
    distinguishing this from the unchanged-head outcome, so a later wave's
    change-path logic (task 4.2 classify.py / task 4.3 pending-record) has
    something concrete to consume. This test does not build or assert that
    consuming logic — only that the signal exists (requirements.md 4.2).
    """
    old_sha = "e" * 40
    new_sha = "f" * 40
    run_mock = _run_subprocess_mock(stdout=f"{new_sha}\trefs/heads/main\n")
    monkeypatch.setattr(subprocess, "run", run_mock)
    if hasattr(poll, "subprocess"):
        monkeypatch.setattr(poll.subprocess, "run", run_mock)

    from backend import state

    store = state.load_state()
    store.record_seen_sha(old_sha)

    result = poll.run()

    outcome = getattr(result, "outcome", None)
    head_sha = getattr(result, "head_sha", None) or getattr(result, "sha", None)

    assert outcome is not None, (
        "poll.run() must return a result object with an 'outcome' "
        "distinguishing a changed head from an unchanged one"
    )
    assert str(outcome).lower() not in ("no-op", "noop", "no_op", "unchanged"), (
        "a changed head must not report the same outcome as an unchanged "
        "head — the next wave needs to tell the two apart"
    )
    assert head_sha == new_sha, (
        "poll.run()'s result must carry the newly resolved head SHA for "
        "the next wave's classify/pending-record logic to consume"
    )


def test_changed_head_result_differs_from_unchanged_head_result(
    isolated_state_dir: Path,
    git_argv_spy: MagicMock,
    notify_spy: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The changed-head outcome and the unchanged-head outcome must be

    DISTINGUISHABLE values, not merely both "truthy" — consuming logic in
    a later wave needs to branch on this (requirements.md 4.2).
    """
    from backend import state

    same_sha = "1" * 40
    run_mock_unchanged = _run_subprocess_mock(stdout=f"{same_sha}\trefs/heads/main\n")
    monkeypatch.setattr(subprocess, "run", run_mock_unchanged)
    if hasattr(poll, "subprocess"):
        monkeypatch.setattr(poll.subprocess, "run", run_mock_unchanged)
    store = state.load_state()
    store.record_seen_sha(same_sha)
    unchanged_result = poll.run()
    unchanged_outcome = getattr(unchanged_result, "outcome", unchanged_result)

    new_sha = "2" * 40
    run_mock_changed = _run_subprocess_mock(stdout=f"{new_sha}\trefs/heads/main\n")
    monkeypatch.setattr(subprocess, "run", run_mock_changed)
    if hasattr(poll, "subprocess"):
        monkeypatch.setattr(poll.subprocess, "run", run_mock_changed)
    changed_result = poll.run()
    changed_outcome = getattr(changed_result, "outcome", changed_result)

    assert unchanged_outcome != changed_outcome, (
        "poll.run() reported the same outcome for an unchanged head and a "
        "changed head; the next wave cannot branch on this"
    )


# ---------------------------------------------------------------------------
# Requirement 4.1 / error handling table — ls-remote failure: exit non-zero,
# last_seen_sha unchanged, no notification.
# ---------------------------------------------------------------------------


def test_ls_remote_nonzero_exit_raises_or_reports_failure(
    isolated_state_dir: Path,
    git_argv_spy: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WHEN `git ls-remote` exits non-zero THEN poll.run() must signal

    failure to its caller — either by raising, or by returning a result
    whose outcome the cron wrapper can turn into a non-zero process exit
    (design.md error table: "Poll exits non-zero"). Both shapes are
    accepted here; what is asserted is that success is NOT reported.
    """

    def _raising_run(*args: object, **kwargs: object) -> None:
        raise subprocess.CalledProcessError(returncode=128, cmd=["git", "ls-remote"])

    monkeypatch.setattr(subprocess, "run", _raising_run)
    if hasattr(poll, "subprocess"):
        monkeypatch.setattr(poll.subprocess, "run", _raising_run)

    from backend import state

    store = state.load_state()
    store.record_seen_sha("3" * 40)

    try:
        result = poll.run()
    except Exception:
        return  # raising is an accepted failure signal

    outcome = getattr(result, "outcome", result)
    assert str(outcome).lower() not in ("no-op", "noop", "no_op", "unchanged"), (
        "an ls-remote failure must not be reported as a successful/" "unchanged tick"
    )
    assert str(outcome).lower() in (
        "error",
        "failure",
        "failed",
        "refused",
        "ls-remote-failed",
    ), f"unexpected success-shaped outcome on ls-remote failure: {outcome!r}"


def test_ls_remote_failure_leaves_last_seen_sha_unchanged(
    isolated_state_dir: Path,
    git_argv_spy: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WHEN `git ls-remote` fails THEN state.last_seen_sha must be left

    EXACTLY as it was before the tick — a failed resolution must never be
    recorded as a seen SHA (requirements.md 4.2, design.md error table).
    """

    def _raising_run(*args: object, **kwargs: object) -> None:
        raise subprocess.CalledProcessError(returncode=128, cmd=["git", "ls-remote"])

    monkeypatch.setattr(subprocess, "run", _raising_run)
    if hasattr(poll, "subprocess"):
        monkeypatch.setattr(poll.subprocess, "run", _raising_run)

    from backend import state

    store = state.load_state()
    original_sha = "4" * 40
    store.record_seen_sha(original_sha)

    try:
        poll.run()
    except Exception:
        pass

    # Re-load from disk, not the in-memory store, so a real bug that writes
    # via a second StateStore instance is still caught.
    reloaded = state.load_state()
    assert reloaded.last_seen_sha == original_sha


def test_ls_remote_failure_from_a_nonzero_returncode_leaves_sha_unchanged(
    isolated_state_dir: Path,
    git_argv_spy: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same as the exception-raising case, but for a `subprocess.run` that

    returns a non-zero `returncode` rather than raising (e.g. a caller
    that does not pass `check=True`) — either failure shape must leave
    `last_seen_sha` unchanged.
    """
    run_mock = _run_subprocess_mock(stdout="", returncode=128)
    monkeypatch.setattr(subprocess, "run", run_mock)
    if hasattr(poll, "subprocess"):
        monkeypatch.setattr(poll.subprocess, "run", run_mock)

    from backend import state

    store = state.load_state()
    original_sha = "5" * 40
    store.record_seen_sha(original_sha)

    try:
        poll.run()
    except Exception:
        pass

    reloaded = state.load_state()
    assert reloaded.last_seen_sha == original_sha


def test_ls_remote_failure_sends_no_notification(
    isolated_state_dir: Path,
    git_argv_spy: MagicMock,
    notify_spy: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WHEN `git ls-remote` fails THEN no notification is sent — a failed

    resolution is not a "changed head" and must not reach the notify seam
    (requirements.md 4.2, design.md error table).
    """

    def _raising_run(*args: object, **kwargs: object) -> None:
        raise subprocess.CalledProcessError(returncode=128, cmd=["git", "ls-remote"])

    monkeypatch.setattr(subprocess, "run", _raising_run)
    if hasattr(poll, "subprocess"):
        monkeypatch.setattr(poll.subprocess, "run", _raising_run)

    from backend import state

    store = state.load_state()
    store.record_seen_sha("6" * 40)

    try:
        poll.run()
    except Exception:
        pass

    notify_spy.assert_not_called()


# ---------------------------------------------------------------------------
# `__main__` guard on the ls-remote-failed path — senior-review H2: the cron
# `command` entrypoint must exit non-zero on failure, matching design.md's
# error table ("`ls-remote` failure | Poll exits non-zero"). The prior
# implementation's `if __name__ == "__main__": run()` always exited 0
# regardless of `run()`'s outcome — a cron scheduler watching the exit code
# could never distinguish a failed tick from a successful one.
# ---------------------------------------------------------------------------


def test_main_guard_exits_non_zero_when_ls_remote_fails(
    isolated_state_dir: Path,
    git_argv_spy: MagicMock,
    notify_spy: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WHEN `backend/poll.py` is executed as `__main__` and the tick's

    outcome is `"ls-remote-failed"` THEN the process must exit non-zero —
    exercised for real via `runpy.run_module`, matching
    `test_main_guard_invokes_run_when_executed_as_a_module`'s convention,
    but on the failure path this time (senior-review H2).
    """

    def _raising_run(*args: object, **kwargs: object) -> None:
        raise subprocess.CalledProcessError(returncode=128, cmd=["git", "ls-remote"])

    monkeypatch.setattr(subprocess, "run", _raising_run)
    if hasattr(poll, "subprocess"):
        monkeypatch.setattr(poll.subprocess, "run", _raising_run)

    monkeypatch.delitem(sys.modules, "backend.poll", raising=False)
    try:
        with pytest.raises(SystemExit) as exc_info:
            runpy.run_module("backend.poll", run_name="__main__", alter_sys=True)
        assert exc_info.value.code == 1, (
            f"expected exit code 1 on an ls-remote-failed tick per "
            f"design.md's error table ('Poll exits non-zero'); got "
            f"{exc_info.value.code!r}"
        )
    finally:
        sys.modules["backend.poll"] = poll


def test_main_guard_exits_zero_on_a_changed_head(
    isolated_state_dir: Path,
    git_argv_spy: MagicMock,
    notify_spy: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WHEN `backend/poll.py` is executed as `__main__` and the tick's

    outcome is `"changed"` (not a failure) THEN the process must exit 0 —
    the H2 fix must not conflate "outcome is not literally 'unchanged'"
    with failure; only `"ls-remote-failed"` is a failure exit.
    """
    from backend import state

    old_sha = "9" * 40
    new_sha = "a" * 40
    ls_remote_result = _run_subprocess_mock(stdout=f"{new_sha}\trefs/heads/main\n")

    def _sequenced_run(argv: list[str], **kwargs: Any) -> MagicMock:
        args = list(argv)
        if "ls-remote" in args:
            result: MagicMock = ls_remote_result(argv, **kwargs)
            return result
        completed = MagicMock(name="CompletedProcess")
        completed.stdout = ""
        completed.returncode = 0
        return completed

    monkeypatch.setattr(subprocess, "run", _sequenced_run)
    if hasattr(poll, "subprocess"):
        monkeypatch.setattr(poll.subprocess, "run", _sequenced_run)

    store = state.load_state()
    store.record_seen_sha(old_sha)

    monkeypatch.delitem(sys.modules, "backend.poll", raising=False)
    try:
        with pytest.raises(SystemExit) as exc_info:
            runpy.run_module("backend.poll", run_name="__main__", alter_sys=True)
        assert exc_info.value.code == 0, (
            f"a changed-head tick is a successful tick and must exit 0, "
            f"got {exc_info.value.code!r}"
        )
    finally:
        sys.modules["backend.poll"] = poll
