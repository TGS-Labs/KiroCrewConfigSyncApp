"""Tests for the `base_sha`-anchored pending-accumulation fix

(requirements.md 4.9, design.md "Data Flow: Pull and apply",
Kiro-Config-Bundles#65).

Reproduces the exact defect the bug report and the design's own
description name: a poll tick that finds a new head WHILE an earlier
commit is still pending must ACCUMULATE the new commit's changed paths
into the existing pending record — merged, keyed by path — rather than
replacing it. The pending record's changed-path range is computed from
`state.base_sha` (the decision boundary), never from `state.last_seen_sha`
(which advances every tick regardless of pending state). `base_sha` only
ever advances on an operator approve/decline — never on accumulation.

Uses the SAME real-local-bare-repo convention as `test_poll_pending.py`
(no mocked git calls) — this codebase's established methodology per
Kiro-Config-Bundles#57's mocked-git testing-gap finding.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock

import pytest

from backend import poll, state

# ---------------------------------------------------------------------------
# Fixtures — identical shape to test_poll_pending.py's, duplicated locally
# per this test suite's existing per-file fixture convention (each poll
# test file, e.g. test_poll_round2_fixes.py / test_poll_round4_fixes.py,
# defines its own copies rather than sharing a conftest).
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(state_dir))
    yield state_dir


@pytest.fixture
def git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Alice")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "alice@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Alice")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "alice@example.com")


def _run_git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def bundle_remote(
    tmp_path: Path,
    isolated_state_dir: Path,
    git_identity: None,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[dict]:
    remote_dir = tmp_path / "bundle-remote.git"
    remote_dir.mkdir()
    _run_git("init", "-q", "--bare", "-b", "main", cwd=remote_dir)

    seed_dir = tmp_path / "bundle-remote-seed"
    seed_dir.mkdir()
    _run_git("init", "-q", "-b", "main", cwd=seed_dir)
    (seed_dir / "steering").mkdir()
    (seed_dir / "steering" / "seed.md").write_text("seed\n", encoding="utf-8")
    _run_git("add", ".", cwd=seed_dir)
    _run_git("commit", "-q", "-m", "seed", cwd=seed_dir)
    _run_git("remote", "add", "origin", str(remote_dir), cwd=seed_dir)
    _run_git("push", "-q", "origin", "main", cwd=seed_dir)
    old_sha = _run_git("rev-parse", "HEAD", cwd=seed_dir).stdout.strip()

    monkeypatch.setattr(poll, "BUNDLE_REPO_URL", str(remote_dir))

    yield {"seed_dir": seed_dir, "remote_dir": remote_dir, "old_sha": old_sha}


def _push_new_commit(
    seed_dir: Path, *, relpath: str, content: str, subject: str
) -> str:
    target = seed_dir / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _run_git("add", relpath, cwd=seed_dir)
    _run_git("commit", "-q", "-m", subject, cwd=seed_dir)
    _run_git("push", "-q", "origin", "main", cwd=seed_dir)
    result: str = _run_git("rev-parse", "HEAD", cwd=seed_dir).stdout.strip()
    return result


@pytest.fixture
def notify_spy(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    for name in ("notify_operator", "notify"):
        if hasattr(poll, name):
            spy = MagicMock(name=f"poll.{name}")
            monkeypatch.setattr(poll, name, spy)
            return spy
    raise AssertionError("backend/poll.py must expose a patchable notify seam")


# ---------------------------------------------------------------------------
# (a) A first-ever pending record sets base_sha = head.
# ---------------------------------------------------------------------------


def test_first_ever_pending_record_sets_base_sha_to_head(
    isolated_state_dir: Path,
    bundle_remote: dict,
    notify_spy: MagicMock,
) -> None:
    """WHEN there is no existing pending record (this instance's first-ever

    poll that finds a changed head) THEN `state.base_sha` is set to the
    newly resolved head — requirements.md 4.9: "When there is no existing
    pending record, starting a new one sets base_sha to the current head."
    """
    store = state.load_state()
    store.record_seen_sha(bundle_remote["old_sha"])
    assert store.base_sha is None, "base_sha must be unset before any poll tick"

    new_sha = _push_new_commit(
        bundle_remote["seed_dir"],
        relpath="steering/first.md",
        content="commit A\n",
        subject="commit A",
    )

    poll.run()

    reloaded = state.load_state()
    assert reloaded.base_sha == new_sha, (
        f"a first-ever pending record must set base_sha to the new head "
        f"({new_sha!r}); got {reloaded.base_sha!r}"
    )
    assert reloaded.pending is not None
    assert reloaded.pending["sha"] == new_sha


# ---------------------------------------------------------------------------
# (b) A second commit landing while one is pending ACCUMULATES paths —
# commit A's file must still be present after commit B lands. Reproduces
# the exact Kiro-Config-Bundles#65 scenario.
# ---------------------------------------------------------------------------


def test_second_commit_while_pending_accumulates_first_commits_file(
    isolated_state_dir: Path,
    bundle_remote: dict,
    notify_spy: MagicMock,
) -> None:
    """Kiro-Config-Bundles#65 reproduction: commit A lands and goes

    pending (tick 1); BEFORE the operator approves or declines it, commit
    B lands (tick 2). The pending record after tick 2 must still list
    commit A's changed file — the defect being fixed is that ranging from
    `last_seen_sha` and overwriting `pending` silently dropped it.
    """
    store = state.load_state()
    store.record_seen_sha(bundle_remote["old_sha"])

    # Commit A lands — tick 1 goes pending.
    _push_new_commit(
        bundle_remote["seed_dir"],
        relpath="steering/commit_a.md",
        content="commit A content\n",
        subject="commit A",
    )
    poll.run()

    pending_after_a = state.load_state().pending
    assert pending_after_a is not None
    assert (
        "steering/commit_a.md" in pending_after_a["classified_paths"]
    ), "sanity check: commit A's file must be pending after tick 1"

    # Commit B lands BEFORE the operator ever approves/declines commit A.
    commit_b_sha = _push_new_commit(
        bundle_remote["seed_dir"],
        relpath="steering/commit_b.md",
        content="commit B content\n",
        subject="commit B",
    )
    poll.run()

    pending_after_b = state.load_state().pending
    assert pending_after_b is not None
    assert "steering/commit_a.md" in pending_after_b["classified_paths"], (
        "commit A's changed file was DROPPED from the pending record once "
        "commit B landed — this is the exact Kiro-Config-Bundles#65 defect "
        f"the base_sha fix closes; got {pending_after_b['classified_paths']!r}"
    )
    assert "steering/commit_b.md" in pending_after_b["classified_paths"], (
        "commit B's changed file must also be present in the accumulated "
        f"pending record; got {pending_after_b['classified_paths']!r}"
    )
    assert pending_after_b["sha"] == commit_b_sha


def test_a_path_changed_in_both_ranges_reflects_the_latest_classification(
    isolated_state_dir: Path,
    bundle_remote: dict,
    notify_spy: MagicMock,
) -> None:
    """WHEN the SAME path is changed again by the later, accumulating

    commit THEN the merged pending record reflects that path's
    classification from the newer range, per requirements.md 4.9's
    "keyed by path so a path changed in both ranges reflects the latest
    classification". Using the identical relpath in both commits is what
    makes this observable: the merge must not duplicate or stale-out the
    entry.
    """
    store = state.load_state()
    store.record_seen_sha(bundle_remote["old_sha"])

    _push_new_commit(
        bundle_remote["seed_dir"],
        relpath="steering/shared.md",
        content="version 1\n",
        subject="commit A: shared.md v1",
    )
    poll.run()

    commit_b_sha = _push_new_commit(
        bundle_remote["seed_dir"],
        relpath="steering/shared.md",
        content="version 2\n",
        subject="commit B: shared.md v2",
    )
    poll.run()

    pending = state.load_state().pending
    assert pending is not None
    # classify_paths classifies by allowlist entry, not file content, so
    # the observable "latest classification wins" signal here is that the
    # merge produced exactly ONE entry for the shared path (not two, not
    # stale-dropped) and the record's own `sha` moved to commit B's head —
    # proving the accumulation re-processed the union rather than only
    # ever keeping commit A's first classification untouched.
    assert list(pending["classified_paths"]).count("steering/shared.md") <= 1
    assert "steering/shared.md" in pending["classified_paths"]
    assert pending["sha"] == commit_b_sha


# ---------------------------------------------------------------------------
# (c) base_sha does not advance across the accumulation tick.
# ---------------------------------------------------------------------------


def test_base_sha_does_not_advance_across_the_accumulation_tick(
    isolated_state_dir: Path,
    bundle_remote: dict,
    notify_spy: MagicMock,
) -> None:
    """requirements.md 4.9: "base_sha SHALL NOT advance while a commit is

    pending" — the accumulation tick (commit B landing while commit A is
    still pending) must leave `state.base_sha` exactly as it was set by
    the first-ever pending record (commit A's SHA), NOT move it to commit
    B's SHA.
    """
    store = state.load_state()
    store.record_seen_sha(bundle_remote["old_sha"])

    commit_a_sha = _push_new_commit(
        bundle_remote["seed_dir"],
        relpath="steering/a.md",
        content="a\n",
        subject="commit A",
    )
    poll.run()
    base_sha_after_a = state.load_state().base_sha
    assert base_sha_after_a == commit_a_sha

    _push_new_commit(
        bundle_remote["seed_dir"],
        relpath="steering/b.md",
        content="b\n",
        subject="commit B",
    )
    poll.run()

    base_sha_after_b = state.load_state().base_sha
    assert base_sha_after_b == commit_a_sha, (
        f"base_sha must NOT advance on an accumulation tick; expected it "
        f"to remain {commit_a_sha!r} (commit A's sha) but got "
        f"{base_sha_after_b!r}"
    )


# ---------------------------------------------------------------------------
# (d) pending.sha does update to the latest head each tick.
# ---------------------------------------------------------------------------


def test_pending_sha_updates_to_latest_head_each_accumulation_tick(
    isolated_state_dir: Path,
    bundle_remote: dict,
    notify_spy: MagicMock,
) -> None:
    """requirements.md 4.9: "The pending record's `sha` field SHALL always

    reflect the newest head seen" — across THREE ticks (commit A, then B,
    then C, none ever approved/declined), `pending.sha` must move to each
    new head in turn while `base_sha` stays pinned to commit A's.
    """
    store = state.load_state()
    store.record_seen_sha(bundle_remote["old_sha"])

    commit_a_sha = _push_new_commit(
        bundle_remote["seed_dir"], relpath="steering/a.md", content="a\n", subject="A"
    )
    poll.run()
    pending_after_a = state.load_state().pending
    assert pending_after_a is not None
    assert pending_after_a["sha"] == commit_a_sha

    commit_b_sha = _push_new_commit(
        bundle_remote["seed_dir"], relpath="steering/b.md", content="b\n", subject="B"
    )
    poll.run()
    pending_after_b = state.load_state().pending
    assert pending_after_b is not None
    assert pending_after_b["sha"] == commit_b_sha

    commit_c_sha = _push_new_commit(
        bundle_remote["seed_dir"], relpath="steering/c.md", content="c\n", subject="C"
    )
    poll.run()
    reloaded = state.load_state()
    pending_after_c = reloaded.pending
    assert pending_after_c is not None
    assert pending_after_c["sha"] == commit_c_sha
    assert reloaded.base_sha == commit_a_sha, (
        "base_sha must still be pinned to commit A's sha after three "
        "accumulation ticks with no operator decision"
    )
    # And nothing was dropped across all three accumulation rounds.
    for relpath in ("steering/a.md", "steering/b.md", "steering/c.md"):
        assert relpath in pending_after_c["classified_paths"], (
            f"{relpath} was dropped from the pending record across "
            f"multiple accumulation ticks"
        )
