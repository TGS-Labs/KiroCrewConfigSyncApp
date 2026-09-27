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

## Real git, not mocks (senior review C1/C2/H4 fix)

The previous version of this file mocked `subprocess.run` with a
hand-written `git show`-shaped string standing in for the changed-path
fetch. That mock encoded an invalid git invocation (C1: `--no-patch` +
`--name-only` together, which real git rejects with exit 128) and could
never have caught it, because the mock always "succeeds" regardless of
what real git would do — the exact testing-methodology gap
Kiro-Config-Bundles#57 tracks as a recurring pattern. This file now runs a
real local bare repo as the bundle-repo stand-in (matching
`tests/test_push_retry_pr_only.py`'s established real-git convention) and
lets `poll.run()`'s own git calls (`ls-remote`, clone/fetch, `show -s`,
`log --first-parent`) execute for real against it. Only the notify seam
and (where noted) `classify.classify_paths`'s spy wrapper are test
collaborators; no git call `poll.run()` itself makes is mocked.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock

import pytest

from backend import classify, poll, state

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Isolates the app's own state dir AND both tracked config roots.

    Every test in this file drives a real `poll.run()` tick on a changed
    head, and under the auto-apply ruling (requirements.md 4.4) that
    always materializes and applies the new commit for real — so
    KIROCREW_HOME/KIRO_HOME must be pinned to a tmp_path-scoped directory
    here, once, rather than in every individual test, or an unpinned test
    would let `apply_commit` write into the real host home directory.
    """
    state_dir = tmp_path / "state"
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(state_dir))
    kirocrew_home = tmp_path / "kirocrew_home"
    kiro_home = tmp_path / "kiro_home"
    kirocrew_home.mkdir(parents=True, exist_ok=True)
    kiro_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("KIROCREW_HOME", str(kirocrew_home))
    monkeypatch.setenv("KIRO_HOME", str(kiro_home))
    yield state_dir


@pytest.fixture
def git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Author/committer identity via environment only, matching

    `test_push_retry_pr_only.py`'s `bare_remote` fixture — this test
    process must never mutate the host's real git configuration.
    """
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Alice")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "alice@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Alice")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "alice@example.com")


def _run_git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    """A plain, unhardened real `git` call for TEST SETUP only — never

    `poll.py`'s own `git_safety.git_argv` — matching
    `test_push_retry_pr_only.py`'s convention exactly.
    """
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
    """A real local bare repo standing in for the GitHub bundle repo, with

    one commit already pushed to `main` — the state the "old" head
    (`state.last_seen_sha`) will be pinned to before each test advances
    the remote further. `poll.BUNDLE_REPO_URL` is monkeypatched to this
    path so `poll.run()`'s own clone/fetch calls run for real against it,
    no network required.
    """
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
    """Add/commit/push one new file on `main` in the seed clone, returning

    the new head SHA — the real-git equivalent of "a new commit landed
    upstream" for the poll job to discover on its next tick.
    """
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


# ---------------------------------------------------------------------------
# Requirement 4.3 — new head detected: fetch changed paths, classify,
# set_pending, notify exactly once.
# ---------------------------------------------------------------------------


def test_changed_head_classifies_paths_via_classify_paths(
    isolated_state_dir: Path,
    bundle_remote: dict,
    notify_spy: MagicMock,
    classify_paths_spy: MagicMock,
) -> None:
    """WHEN poll detects a new head THEN it fetches that commit's changed

    paths and classifies them via `classify.classify_paths` — not a
    reimplemented/ad-hoc matcher (requirements.md 4.3, design.md: "classify
    each path against the allowlist").
    """
    store = state.load_state()
    store.record_seen_sha(bundle_remote["old_sha"])

    _push_new_commit(
        bundle_remote["seed_dir"],
        relpath="steering/foo.md",
        content="new steering content\n",
        subject="update steering",
    )

    poll.run()

    assert classify_paths_spy.called, (
        "poll.run() did not call classify.classify_paths on a changed-head "
        "tick; requirements.md 4.3 requires classifying the new commit's "
        "changed paths before recording a pending state"
    )


def test_changed_head_records_pending_via_state_set_pending(
    isolated_state_dir: Path,
    bundle_remote: dict,
    notify_spy: MagicMock,
) -> None:
    """WHEN poll detects a new head THEN it fetches, classifies, and

    notifies with the SHA, author, subject and classified paths of the
    new commit (requirements.md 4.3, design.md "write a `pending` record
    (sha, author, subject, classified paths)").

    Under the auto-apply ruling (requirements.md 4.4/4.9) a fully-applied
    commit resolves its own pending record in the SAME tick, so
    `state.pending` is `None` again by the time `run()` returns — the
    record's fields (sha/author/subject/classified paths) are asserted
    against the notify call instead, which `run()` makes with exactly
    those values before resolving pending, and against `last_apply`,
    which records the sha this commit was actually applied under.
    """
    store = state.load_state()
    store.record_seen_sha(bundle_remote["old_sha"])

    new_sha = _push_new_commit(
        bundle_remote["seed_dir"],
        relpath="steering/bar.md",
        content="another steering doc\n",
        subject="add steering doc",
    )

    poll.run()

    notify_spy.assert_called_once()
    _, notify_kwargs = notify_spy.call_args
    assert notify_kwargs["head_sha"] == new_sha
    assert notify_kwargs["author"] == "Alice"
    assert notify_kwargs["subject"] == "add steering doc"

    reloaded = state.load_state()
    assert reloaded.pending is None, (
        "a fully-applied commit must resolve its own pending record in "
        "the same tick (requirements.md 4.4) — there is no operator "
        "decision step left to hold it open for"
    )
    assert reloaded.last_apply is not None
    assert reloaded.last_apply["sha"] == new_sha
    assert "steering/bar.md" in reloaded.last_apply["applied"]


def test_changed_head_notifies_exactly_once(
    isolated_state_dir: Path,
    bundle_remote: dict,
    notify_spy: MagicMock,
) -> None:
    """WHEN poll detects a new head THEN the notify seam is called exactly

    ONCE for that tick (requirements.md 4.3).
    """
    store = state.load_state()
    store.record_seen_sha(bundle_remote["old_sha"])

    _push_new_commit(
        bundle_remote["seed_dir"],
        relpath="config.json",
        content='{"rotated": true}\n',
        subject="rotate mcp config",
    )

    poll.run()

    assert notify_spy.call_count == 1, (
        f"expected exactly one notification on a changed-head tick, got "
        f"{notify_spy.call_count}"
    )


def test_changed_head_records_the_new_head_as_seen(
    isolated_state_dir: Path,
    bundle_remote: dict,
    notify_spy: MagicMock,
) -> None:
    """A changed-head tick that records a pending commit must also advance

    `state.last_seen_sha` to the new head — otherwise every subsequent
    tick would keep re-classifying/re-detecting the same "changed" head
    forever instead of settling into the keyed-pending no-renotify state
    (requirements.md 4.4's premise: a SECOND tick with the SAME head must
    be recognizable as "already seen, already pending").
    """
    store = state.load_state()
    store.record_seen_sha(bundle_remote["old_sha"])

    new_sha = _push_new_commit(
        bundle_remote["seed_dir"],
        relpath="crons.json",
        content="{}\n",
        subject="tweak crons",
    )

    poll.run()

    reloaded = state.load_state()
    assert reloaded.last_seen_sha == new_sha


# ---------------------------------------------------------------------------
# Requirement 4.4 — a second tick with the SAME new head (still pending,
# not yet approved/declined) must NOT re-notify. Keyed on SHA.
# ---------------------------------------------------------------------------


def test_second_tick_with_same_pending_head_does_not_renotify(
    isolated_state_dir: Path,
    bundle_remote: dict,
    notify_spy: MagicMock,
) -> None:
    """WHEN a commit is already pending (recorded, not yet acted on) AND a

    later tick resolves the SAME head via ls-remote THEN no second
    notification is sent — the pending record is keyed on SHA and a
    repeat sighting of the same pending SHA is a no-op notification-wise
    (requirements.md 4.4: "does not re-nag on every 15-minute tick").
    """
    store = state.load_state()
    store.record_seen_sha(bundle_remote["old_sha"])

    _push_new_commit(
        bundle_remote["seed_dir"],
        relpath="steering/baz.md",
        content="first sighting\n",
        subject="first sighting",
    )

    # Tick 1: head changes, gets recorded as pending + notified once.
    poll.run()
    assert notify_spy.call_count == 1, "tick 1 should notify once"

    # Tick 2: nothing new landed upstream since tick 1 — ls-remote reports
    # the same head again. last_seen_sha was already advanced to it by
    # tick 1, so this is the "already seen" case.
    poll.run()

    assert notify_spy.call_count == 1, (
        "a second tick resolving the SAME already-pending head must NOT "
        "send a second notification (requirements.md 4.4); got "
        f"{notify_spy.call_count} total notifications"
    )


def test_second_tick_with_same_pending_head_leaves_pending_record_intact(
    isolated_state_dir: Path,
    bundle_remote: dict,
    notify_spy: MagicMock,
) -> None:
    """A repeat tick that resolves the SAME already-applied head must be a

    clean no-op — under the auto-apply ruling the first tick both applies
    and resolves the commit in one step, so there is no pending record
    left to leave "intact"; instead this asserts the no-op property the
    original pending-record-stability check was really protecting: a
    second tick against the identical head neither re-notifies nor
    changes `last_apply`'s record of the applied commit.
    """
    store = state.load_state()
    store.record_seen_sha(bundle_remote["old_sha"])

    _push_new_commit(
        bundle_remote["seed_dir"],
        relpath="steering/qux.md",
        content="original pending commit\n",
        subject="original pending commit",
    )

    poll.run()
    reloaded_after_tick_1 = state.load_state()
    assert reloaded_after_tick_1.pending is None
    last_apply_after_tick_1 = reloaded_after_tick_1.last_apply
    assert last_apply_after_tick_1 is not None
    applied_sha = last_apply_after_tick_1["sha"]

    poll.run()

    reloaded_after_tick_2 = state.load_state()
    assert reloaded_after_tick_2.pending is None, (
        "a repeat tick against the same already-applied head must not "
        "create a new pending record"
    )
    assert reloaded_after_tick_2.last_apply == last_apply_after_tick_1, (
        "a repeat tick against the identical already-applied head must "
        "not change the recorded last_apply summary"
    )
    assert reloaded_after_tick_2.last_apply["sha"] == applied_sha
    assert notify_spy.call_count == 1, (
        "a repeat tick resolving the same already-seen head must not "
        f"send a second notification; got {notify_spy.call_count} total"
    )


# ---------------------------------------------------------------------------
# Requirement 4.4/4.6 — an automatic apply only ever touches the paths the
# applied commit actually changed; it must never disturb unrelated existing
# content already living under either tracked root.
# ---------------------------------------------------------------------------


def test_pending_tick_does_not_write_to_either_tracked_root(
    isolated_state_dir: Path,
    bundle_remote: dict,
    notify_spy: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An automatic apply must not disturb unrelated, pre-existing content

    under either tracked root (KIROCREW_HOME / KIRO_HOME) — only the
    relpaths the applied commit itself changed are touched
    (requirements.md 4.4/4.14). The sentinel files here are not part of
    the pushed commit, so they must remain byte-unchanged even though
    this tick DOES auto-apply the commit's own new file elsewhere under
    the same roots.
    """
    kirocrew_home = tmp_path / "kirocrew_home"
    kiro_home = tmp_path / "kiro_home"
    sentinel_a = kirocrew_home / "steering" / "untouched.md"
    sentinel_a.parent.mkdir(parents=True)
    sentinel_a.write_bytes(b"original content A")
    sentinel_b = kiro_home / "agents" / "untouched.json"
    sentinel_b.parent.mkdir(parents=True)
    sentinel_b.write_bytes(b"original content B")

    monkeypatch.setenv("KIROCREW_HOME", str(kirocrew_home))
    monkeypatch.setenv("KIRO_HOME", str(kiro_home))

    store = state.load_state()
    store.record_seen_sha(bundle_remote["old_sha"])

    _push_new_commit(
        bundle_remote["seed_dir"],
        relpath="steering/x.md",
        content="applied automatically\n",
        subject="applied automatically",
    )

    poll.run()

    assert sentinel_a.read_bytes() == b"original content A", (
        "an automatic apply must not touch unrelated pre-existing content "
        "under the KIROCREW_HOME tracked root"
    )
    assert sentinel_b.read_bytes() == b"original content B", (
        "an automatic apply must not touch unrelated pre-existing content "
        "under the KIRO_HOME tracked root"
    )


# ---------------------------------------------------------------------------
# H4 regression guard — a MERGE commit (the common case on
# Kiro-Config-Bundles, which disallows squash-merge org-wide) must still
# be classified/recorded correctly, not silently dropped to an empty
# changed-path list.
# ---------------------------------------------------------------------------


def test_changed_head_that_is_a_merge_commit_still_reports_changed_paths(
    isolated_state_dir: Path,
    bundle_remote: dict,
    notify_spy: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """WHEN the new head is a MERGE commit (parents=[previous-main,

    feature-branch-tip]) THEN the file the merge brought in is still
    classified AND automatically applied to the live root — proving H4's
    fix against a real merge in the SAME remote a poll tick actually
    reads, not just the standalone fixture in
    test_poll_fetch_commit_details.py. Checked via `last_apply`/the live
    root rather than `pending`, since a fully-applied commit resolves its
    own pending record in the same tick under the auto-apply ruling.
    """
    kirocrew_home = tmp_path / "kirocrew_home"
    seed_dir = bundle_remote["seed_dir"]
    old_sha = bundle_remote["old_sha"]

    _run_git("checkout", "-qb", "feature", cwd=seed_dir)
    (seed_dir / "steering" / "merged_in.md").parent.mkdir(parents=True, exist_ok=True)
    (seed_dir / "steering" / "merged_in.md").write_text(
        "brought in by the merge\n", encoding="utf-8"
    )
    _run_git("add", "steering/merged_in.md", cwd=seed_dir)
    _run_git("commit", "-q", "-m", "feature change", cwd=seed_dir)
    _run_git("checkout", "-q", "main", cwd=seed_dir)
    _run_git(
        "merge",
        "--no-ff",
        "-q",
        "-m",
        "merge feature into main",
        "feature",
        cwd=seed_dir,
    )
    merge_sha = _run_git("rev-parse", "HEAD", cwd=seed_dir).stdout.strip()
    _run_git("push", "-q", "origin", "main", cwd=seed_dir)

    store = state.load_state()
    store.record_seen_sha(old_sha)

    poll.run()

    reloaded = state.load_state()
    assert reloaded.pending is None
    last_apply = reloaded.last_apply
    assert last_apply is not None
    assert last_apply["sha"] == merge_sha
    assert "steering/merged_in.md" in last_apply["applied"], (
        f"a merge commit's incoming file must be applied; got "
        f"{last_apply['applied']!r}"
    )
    assert (kirocrew_home / "steering" / "merged_in.md").read_text(
        encoding="utf-8"
    ) == "brought in by the merge\n"
