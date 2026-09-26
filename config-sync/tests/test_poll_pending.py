"""Tests for the pending-record + notify-once wiring in `backend/poll.py`

(tasks.md 4.3), extending task 4.1's `poll.run()` rather than a new module —
per engineering-manager's wave-8 plan ("wire poll.py + classify.py output
into state.set_pending") and matching `backend/push.py`'s precedent of a
single `run()` orchestrating a full multi-step flow inline.

Covers design.md's "Data Flow: Pull" step list:

    changed head -> classify changed paths -> state.pending -> notify once

and requirements.md:

- 4.3: on a changed head, the poll job fetches the new commit's changed
  paths, classifies them via `classify.classify_paths`, and records a
  `pending` record (sha, author, subject, classified paths) via
  `state.set_pending`, then notifies the operator.
- 4.4: a pending commit does not re-nag — a second tick that resolves the
  SAME new head (already pending, not yet approved/declined) must NOT
  send a second notification. Keyed on SHA, not on "is there a pending
  record at all".
- 4.6: while a commit is pending (not yet acted on), nothing is applied:
  the instance's own configuration is untouched by this wiring, and no
  apply-triggering call exists anywhere in the changed-head code path.

This module fetches a new commit's changed paths via a second git
invocation (design.md: "fetch the commit's changed-path list") — the
poll job's SINGLE-ls-remote invariant from task 4.1 (`no_further_git_calls`
in test_poll.py) applies only to the UNCHANGED-head path; the changed-head
path is explicitly allowed a second, THIRD git call for path/metadata
discovery. That second call must still route through
`git_safety.git_argv`, matching every other host-side git call in this app.

The exact plumbing (a single combined `git show --name-only --format=...`
vs. a separate `diff-tree` and metadata read) is an implementation detail
this test suite deliberately does not pin down — what's pinned is the
observable contract: `classify.classify_paths` is called with the new
head's changed paths, `state.set_pending` receives sha/author/subject/
classified_paths derived from that commit, and the notify seam fires
exactly once per newly-pending SHA.

All tests below are expected to fail for one of two RIGHT reasons until
software-engineer implements this wiring:
- a collection-time error if the wiring hooks this suite patches
  (`_fetch_commit_metadata`/`_fetch_changed_paths`/`classify_paths` call
  site) do not exist yet, or
- an assertion failure because `poll.run()`'s changed-head path today
  (task 4.1) only returns `PollResult(outcome="changed", head_sha=...)`
  without classifying, recording a pending state, or notifying at all.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock

import pytest

from backend import classify, poll, state

# ---------------------------------------------------------------------------
# Fixtures — matching test_poll.py's isolated-state-dir / git_argv_spy /
# notify_spy conventions exactly, so this suite composes with task 4.1's
# fixtures rather than reinventing them.
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(state_dir))
    yield state_dir


@pytest.fixture
def git_argv_spy(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Spy on (wrap, don't replace) `git_safety.git_argv`, matching

    test_poll.py's fixture exactly, so the changed-path fetch call is
    provably built through the hardened argv builder too.
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
def notify_spy(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Spy on/replace the poll job's notification seam, matching

    test_poll.py's fixture and `pr_handoff.py`'s `notify_operator`
    convention.
    """
    for name in ("notify_operator", "notify"):
        if hasattr(poll, name):
            spy = MagicMock(name=f"poll.{name}")
            monkeypatch.setattr(poll, name, spy)
            return spy
    raise AssertionError(
        "backend/poll.py must expose a patchable notify seam named "
        "'notify_operator' or 'notify' for the pending/notify-once wiring "
        "to be testable"
    )


@pytest.fixture
def classify_paths_spy(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Spy on (wrap, don't replace) `classify.classify_paths` so tests can

    assert it was actually invoked, with what arguments, on the
    changed-head path — without hand-rolling allowlist matching here.
    """
    spy = MagicMock(name="classify.classify_paths", wraps=classify.classify_paths)
    monkeypatch.setattr(classify, "classify_paths", spy)
    if hasattr(poll, "classify_paths"):
        monkeypatch.setattr(poll, "classify_paths", spy)
    if hasattr(poll, "classify"):
        monkeypatch.setattr(poll.classify, "classify_paths", spy)
    return spy


def _run_subprocess_sequence(*outcomes: MagicMock) -> MagicMock:
    """Build a `subprocess.run` replacement that returns each of

    ``outcomes`` in order across successive calls — the ls-remote call
    first, then the changed-path/metadata fetch call(s) that follow it on
    the changed-head path.
    """
    return MagicMock(name="subprocess.run", side_effect=list(outcomes))


def _completed(*, stdout: str = "", returncode: int = 0) -> MagicMock:
    completed = MagicMock(name="CompletedProcess")
    completed.stdout = stdout
    completed.returncode = returncode
    return completed


# A realistic `git show --name-only --format=...`-shaped payload: one
# metadata line (author + subject, tab-delimited so parsing is trivial and
# unambiguous even if the subject contains spaces), a blank separator line,
# then the changed paths. Tests do not assert poll.py parses THIS exact
# format — only that whatever it fetches ends up correctly classified and
# recorded. This fixture models one plausible, simple contract.
def _show_stdout(*, author: str, subject: str, paths: list[str]) -> str:
    lines = [f"{author}\t{subject}", ""] + paths
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Requirement 4.3 — new head detected: fetch changed paths, classify,
# set_pending, notify exactly once.
# ---------------------------------------------------------------------------


def test_changed_head_classifies_paths_via_classify_paths(
    isolated_state_dir: Path,
    git_argv_spy: MagicMock,
    notify_spy: MagicMock,
    classify_paths_spy: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WHEN poll detects a new head THEN it fetches that commit's changed

    paths and classifies them via `classify.classify_paths` — not a
    reimplemented/ad-hoc matcher (requirements.md 4.3, design.md: "classify
    each path against the allowlist").
    """
    old_sha = "a" * 40
    new_sha = "b" * 40
    changed_paths = ["steering/foo.md", "some/untracked/file.txt"]

    ls_remote_result = _completed(stdout=f"{new_sha}\trefs/heads/main\n")
    show_result = _completed(
        stdout=_show_stdout(
            author="alice", subject="update steering", paths=changed_paths
        )
    )
    run_mock = _run_subprocess_sequence(ls_remote_result, show_result)
    monkeypatch.setattr(subprocess, "run", run_mock)
    if hasattr(poll, "subprocess"):
        monkeypatch.setattr(poll.subprocess, "run", run_mock)

    store = state.load_state()
    store.record_seen_sha(old_sha)

    poll.run()

    assert classify_paths_spy.called, (
        "poll.run() did not call classify.classify_paths on a changed-head "
        "tick; requirements.md 4.3 requires classifying the new commit's "
        "changed paths before recording a pending state"
    )


def test_changed_head_records_pending_via_state_set_pending(
    isolated_state_dir: Path,
    git_argv_spy: MagicMock,
    notify_spy: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WHEN poll detects a new head THEN `state.set_pending` is called with

    the SHA, author, subject and classified paths of the new commit
    (requirements.md 4.3, design.md "write a `pending` record (sha,
    author, subject, classified paths)").
    """
    old_sha = "c" * 40
    new_sha = "d" * 40
    changed_paths = ["steering/bar.md"]

    ls_remote_result = _completed(stdout=f"{new_sha}\trefs/heads/main\n")
    show_result = _completed(
        stdout=_show_stdout(
            author="bob", subject="add steering doc", paths=changed_paths
        )
    )
    run_mock = _run_subprocess_sequence(ls_remote_result, show_result)
    monkeypatch.setattr(subprocess, "run", run_mock)
    if hasattr(poll, "subprocess"):
        monkeypatch.setattr(poll.subprocess, "run", run_mock)

    store = state.load_state()
    store.record_seen_sha(old_sha)

    poll.run()

    reloaded = state.load_state()
    pending = reloaded.pending
    assert pending is not None, (
        "poll.run() did not record a pending state via state.set_pending "
        "on a changed-head tick"
    )
    assert pending["sha"] == new_sha
    assert pending["author"] == "bob"
    assert pending["subject"] == "add steering doc"
    assert "steering/bar.md" in pending["classified_paths"]


def test_changed_head_notifies_exactly_once(
    isolated_state_dir: Path,
    git_argv_spy: MagicMock,
    notify_spy: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WHEN poll detects a new head THEN the notify seam is called exactly

    ONCE for that tick (requirements.md 4.3).
    """
    old_sha = "e" * 40
    new_sha = "f" * 40

    ls_remote_result = _completed(stdout=f"{new_sha}\trefs/heads/main\n")
    show_result = _completed(
        stdout=_show_stdout(
            author="carol", subject="rotate mcp config", paths=["config.json"]
        )
    )
    run_mock = _run_subprocess_sequence(ls_remote_result, show_result)
    monkeypatch.setattr(subprocess, "run", run_mock)
    if hasattr(poll, "subprocess"):
        monkeypatch.setattr(poll.subprocess, "run", run_mock)

    store = state.load_state()
    store.record_seen_sha(old_sha)

    poll.run()

    assert notify_spy.call_count == 1, (
        f"expected exactly one notification on a changed-head tick, got "
        f"{notify_spy.call_count}"
    )


def test_changed_head_records_the_new_head_as_seen(
    isolated_state_dir: Path,
    git_argv_spy: MagicMock,
    notify_spy: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed-head tick that records a pending commit must also advance

    `state.last_seen_sha` to the new head — otherwise every subsequent
    tick would keep re-classifying/re-detecting the same "changed" head
    forever instead of settling into the keyed-pending no-renotify state
    (requirements.md 4.4's premise: a SECOND tick with the SAME head must
    be recognizable as "already seen, already pending").
    """
    old_sha = "1" * 40
    new_sha = "2" * 40

    ls_remote_result = _completed(stdout=f"{new_sha}\trefs/heads/main\n")
    show_result = _completed(
        stdout=_show_stdout(author="dave", subject="tweak crons", paths=["crons.json"])
    )
    run_mock = _run_subprocess_sequence(ls_remote_result, show_result)
    monkeypatch.setattr(subprocess, "run", run_mock)
    if hasattr(poll, "subprocess"):
        monkeypatch.setattr(poll.subprocess, "run", run_mock)

    store = state.load_state()
    store.record_seen_sha(old_sha)

    poll.run()

    reloaded = state.load_state()
    assert reloaded.last_seen_sha == new_sha


# ---------------------------------------------------------------------------
# Requirement 4.4 — a second tick with the SAME new head (still pending,
# not yet approved/declined) must NOT re-notify. Keyed on SHA.
# ---------------------------------------------------------------------------


def test_second_tick_with_same_pending_head_does_not_renotify(
    isolated_state_dir: Path,
    git_argv_spy: MagicMock,
    notify_spy: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WHEN a commit is already pending (recorded, not yet acted on) AND a

    later tick resolves the SAME head via ls-remote THEN no second
    notification is sent — the pending record is keyed on SHA and a
    repeat sighting of the same pending SHA is a no-op notification-wise
    (requirements.md 4.4: "does not re-nag on every 15-minute tick").
    """
    old_sha = "3" * 40
    new_sha = "4" * 40
    changed_paths = ["steering/baz.md"]

    # Tick 1: head changes, gets recorded as pending + notified once.
    ls_remote_1 = _completed(stdout=f"{new_sha}\trefs/heads/main\n")
    show_1 = _completed(
        stdout=_show_stdout(
            author="erin", subject="first sighting", paths=changed_paths
        )
    )
    run_mock_1 = _run_subprocess_sequence(ls_remote_1, show_1)
    monkeypatch.setattr(subprocess, "run", run_mock_1)
    if hasattr(poll, "subprocess"):
        monkeypatch.setattr(poll.subprocess, "run", run_mock_1)

    store = state.load_state()
    store.record_seen_sha(old_sha)

    poll.run()
    assert notify_spy.call_count == 1, "tick 1 should notify once"

    # Tick 2: ls-remote reports the SAME head again (nothing new landed
    # upstream since tick 1) — last_seen_sha was already advanced to
    # new_sha by tick 1, so this is the "already seen" case.
    ls_remote_2 = _completed(stdout=f"{new_sha}\trefs/heads/main\n")
    run_mock_2 = _run_subprocess_sequence(ls_remote_2)
    monkeypatch.setattr(subprocess, "run", run_mock_2)
    if hasattr(poll, "subprocess"):
        monkeypatch.setattr(poll.subprocess, "run", run_mock_2)

    poll.run()

    assert notify_spy.call_count == 1, (
        "a second tick resolving the SAME already-pending head must NOT "
        "send a second notification (requirements.md 4.4); got "
        f"{notify_spy.call_count} total notifications"
    )


def test_second_tick_with_same_pending_head_leaves_pending_record_intact(
    isolated_state_dir: Path,
    git_argv_spy: MagicMock,
    notify_spy: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repeat sighting of the same already-pending SHA must not overwrite

    or clear the existing pending record (still keyed on the same SHA)
    while the operator has neither approved nor declined it.
    """
    old_sha = "5" * 40
    new_sha = "6" * 40
    changed_paths = ["steering/qux.md"]

    ls_remote_1 = _completed(stdout=f"{new_sha}\trefs/heads/main\n")
    show_1 = _completed(
        stdout=_show_stdout(
            author="frank", subject="original pending commit", paths=changed_paths
        )
    )
    run_mock_1 = _run_subprocess_sequence(ls_remote_1, show_1)
    monkeypatch.setattr(subprocess, "run", run_mock_1)
    if hasattr(poll, "subprocess"):
        monkeypatch.setattr(poll.subprocess, "run", run_mock_1)

    store = state.load_state()
    store.record_seen_sha(old_sha)
    poll.run()

    pending_after_tick_1 = state.load_state().pending
    assert pending_after_tick_1 is not None
    assert pending_after_tick_1["sha"] == new_sha

    ls_remote_2 = _completed(stdout=f"{new_sha}\trefs/heads/main\n")
    run_mock_2 = _run_subprocess_sequence(ls_remote_2)
    monkeypatch.setattr(subprocess, "run", run_mock_2)
    if hasattr(poll, "subprocess"):
        monkeypatch.setattr(poll.subprocess, "run", run_mock_2)

    poll.run()

    pending_after_tick_2 = state.load_state().pending
    assert pending_after_tick_2 is not None, (
        "the pending record must still exist after a repeat sighting of "
        "the same not-yet-acted-on SHA"
    )
    assert pending_after_tick_2["sha"] == new_sha, (
        "a repeat sighting of the same pending SHA must not replace the "
        "pending record with a different identity"
    )
    assert pending_after_tick_2["author"] == pending_after_tick_1["author"]
    assert pending_after_tick_2["subject"] == pending_after_tick_1["subject"]


# ---------------------------------------------------------------------------
# Requirement 4.6 — while pending (not yet acted on), the instance's own
# configuration is byte-unchanged and no apply-triggering call exists in
# this wiring's code path.
# ---------------------------------------------------------------------------


def test_pending_tick_never_imports_or_calls_apply(
    isolated_state_dir: Path,
    git_argv_spy: MagicMock,
    notify_spy: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WHILE a commit is pending (not yet approved/declined) THE poll

    module must contain no call to any apply-triggering function —
    asserted by confirming `backend.poll` never imports `backend.apply`
    (which does not exist yet per this deployment's scope, design.md
    Deployment 3 vs Deployment 4) and exposes no attribute whose name
    suggests one (requirements.md 4.6: "no route/flag/env var exists that
    would auto-apply").
    """
    import sys

    assert "backend.apply" not in sys.modules or not any(
        name for name in dir(poll) if "apply" in name.lower()
    ), "backend/poll.py must not import or reference an apply-triggering call"

    apply_like_names = [name for name in dir(poll) if "apply" in name.lower()]
    assert apply_like_names == [], (
        f"backend/poll.py exposes apply-like names {apply_like_names}; "
        "the pending/notify-once wiring (Deployment 3) must not auto-apply "
        "anything — apply is Deployment 4's approved-only route"
    )


def test_pending_tick_does_not_write_to_either_tracked_root(
    isolated_state_dir: Path,
    git_argv_spy: MagicMock,
    notify_spy: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """WHILE a commit is pending THE instance's own configuration roots

    (KIROCREW_HOME / KIRO_HOME) must be byte-unchanged by the poll tick —
    the ONLY things this tick writes are to the app's own state directory
    (`CONFIG_SYNC_STATE_DIR`), never into either tracked root
    (requirements.md 4.6).
    """
    kirocrew_home = tmp_path / "kirocrew_home"
    kiro_home = tmp_path / "kiro_home"
    kirocrew_home.mkdir()
    kiro_home.mkdir()
    sentinel_a = kirocrew_home / "steering" / "untouched.md"
    sentinel_a.parent.mkdir(parents=True)
    sentinel_a.write_bytes(b"original content A")
    sentinel_b = kiro_home / "agents" / "untouched.json"
    sentinel_b.parent.mkdir(parents=True)
    sentinel_b.write_bytes(b"original content B")

    monkeypatch.setenv("KIROCREW_HOME", str(kirocrew_home))
    monkeypatch.setenv("KIRO_HOME", str(kiro_home))

    old_sha = "7" * 40
    new_sha = "8" * 40
    ls_remote_result = _completed(stdout=f"{new_sha}\trefs/heads/main\n")
    show_result = _completed(
        stdout=_show_stdout(
            author="grace", subject="pending, not applied", paths=["steering/x.md"]
        )
    )
    run_mock = _run_subprocess_sequence(ls_remote_result, show_result)
    monkeypatch.setattr(subprocess, "run", run_mock)
    if hasattr(poll, "subprocess"):
        monkeypatch.setattr(poll.subprocess, "run", run_mock)

    store = state.load_state()
    store.record_seen_sha(old_sha)

    poll.run()

    assert sentinel_a.read_bytes() == b"original content A", (
        "poll.run() must not write into the KIROCREW_HOME tracked root "
        "while a commit is only pending"
    )
    assert sentinel_b.read_bytes() == b"original content B", (
        "poll.run() must not write into the KIRO_HOME tracked root while "
        "a commit is only pending"
    )
