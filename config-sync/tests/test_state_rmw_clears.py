"""FAILING tests for senior-review round-2 Critical C1: `StateStore._locked_rmw`

drops a concurrent process's CLEAR back to a field's default.

## The gap

`_locked_rmw` (backend/state.py, the merge loop inside the method) builds
its working payload as::

    merged = dict(self._payload)
    for key, default_value in defaults.items():
        disk_value = on_disk.get(key, default_value)
        if disk_value != default_value:
            merged[key] = disk_value

This only ever ADOPTS an on-disk value when that value differs from the
field's DEFAULT. It never asks whether the on-disk value differs from
THIS instance's own stale in-memory value. So when a concurrent process
has cleared a field back to its default (``None`` for ``pending``/
``pending_pr``, unchanged for scalars that were reverted) — the disk
value IS the default, the ``!=`` check is false, and `merged` keeps this
instance's stale (non-default, already-superseded) in-memory copy. The
next mutation this instance makes then writes that stale value straight
back to disk, resurrecting a field another process just cleared.

The same merge failure mode also mis-derives "is there already a pending
record to accumulate onto" for the poll-side set_pending/accumulate_pending
choice (test (e) below): a decision made from `self._payload["pending"]`
rather than the freshly-loaded on-disk value can decide wrong.

## Fixtures

Reuses the `isolated_state_dir` fixture and the `_second_process_store()`
helper from `tests/test_state_crossprocess.py` (same on-disk-file-sharing
pattern: two independent `StateStore` objects, standing in for two real
OS processes, pointed at one `CONFIG_SYNC_STATE_DIR`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest

from backend import state as state_module

# ---------------------------------------------------------------------------
# Fixtures (mirrors tests/test_state_crossprocess.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_state_dir(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Isolates the app's own state dir under a tmp_path, matching

    `tests/test_state_crossprocess.py`'s fixture of the same name so both
    files' `StateStore` instances resolve to the same on-disk location
    style without touching a real `~/.config-sync/state`.
    """
    state_dir = tmp_path / "config-sync-state"
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(state_dir))
    yield state_dir


def _second_process_store() -> "state_module.StateStore":
    """A second `StateStore`, standing in for a concurrent poll/push cron

    process, built via the same `state.load_state()` entry point a real
    cron invocation uses, against the same on-disk file
    `isolated_state_dir` already pointed `CONFIG_SYNC_STATE_DIR` at.
    """
    return state_module.load_state()


# ---------------------------------------------------------------------------
# (a) other process clears `pending` -> unrelated mutation here keeps it
#     cleared
# ---------------------------------------------------------------------------


def test_unrelated_mutation_does_not_resurrect_pending_cleared_elsewhere(
    isolated_state_dir: Path,
) -> None:
    """Store P loads state with `pending` set. A second store S (same

    on-disk file) resolves it (clearing `pending` back to `None` and
    advancing `base_sha`). P then performs an UNRELATED mutation
    (`record_seen_sha`) that never touches `pending` at all. P's write
    must not write its own stale in-memory `pending` back to disk —
    `pending` must stay cleared.
    """
    sha = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

    store_p = state_module.load_state()
    store_p.set_pending(
        sha=sha,
        author="poller",
        subject="a change",
        classified_paths={"a.md": "instant"},
    )
    # P's in-memory view now holds `pending` != None (the value just
    # written). Simulate P having loaded this state a while ago and
    # not yet having observed the clear below — reload P fresh to mimic
    # "P's own StateStore, already holding pending in memory" without
    # forcing a new load, matching the bug report's own repro shape.
    store_p = state_module.load_state()
    assert store_p.pending is not None

    store_s = _second_process_store()
    store_s.resolve_pending(sha)
    assert store_s.pending is None  # sanity: S's own clear landed

    # P performs an unrelated mutation.
    store_p.record_seen_sha("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")

    verify_store = _second_process_store()
    assert verify_store.pending is None, (
        "an other process's clear of `pending` must survive an unrelated "
        "mutation made from a stale in-memory copy that still held the "
        "old pending record"
    )
    assert verify_store.last_seen_sha == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


# ---------------------------------------------------------------------------
# (b) same for pending_pr after confirm_pr_created
# ---------------------------------------------------------------------------


def test_unrelated_mutation_does_not_resurrect_pending_pr_cleared_elsewhere(
    isolated_state_dir: Path,
) -> None:
    """Store P loads state with `pending_pr` set. A second store S (same

    on-disk file) confirms PR creation (clearing `pending_pr` back to
    `None` via `confirm_pr_created`). P then performs an unrelated
    mutation (`record_seen_sha`). `pending_pr` must stay cleared.
    """
    tree_hash = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    branch = "config-sync/push"

    store_p = state_module.load_state()
    store_p.record_pr_pending(
        branch=branch,
        tree_hash=tree_hash,
        payload={"some": "payload"},
    )
    store_p = state_module.load_state()
    assert store_p.pending_pr is not None

    store_s = _second_process_store()
    store_s.confirm_pr_created(
        tree_hash=tree_hash,
        branch=branch,
        pr_url="https://example.invalid/pr/9",
    )
    assert store_s.pending_pr is None  # sanity: S's own clear landed

    store_p.record_seen_sha("cccccccccccccccccccccccccccccccccccccccc")

    verify_store = _second_process_store()
    assert verify_store.pending_pr is None, (
        "an other process's clear of `pending_pr` (via confirm_pr_created) "
        "must survive an unrelated mutation made from a stale in-memory "
        "copy that still held the old pending_pr record"
    )


# ---------------------------------------------------------------------------
# (c) other process sets a field -> unrelated mutation here keeps it
# ---------------------------------------------------------------------------


def test_unrelated_mutation_does_not_drop_a_field_set_elsewhere(
    isolated_state_dir: Path,
) -> None:
    """Store P loads state while everything is still at defaults. A

    second store S sets `last_poll_failure`. P then performs an
    unrelated mutation (`record_seen_sha`). P's write must not revert
    `last_poll_failure` back to `None` — this direction (adopting a
    disk value that newly DIFFERS from default) already works under the
    current merge logic, but is included here as the companion case to
    (a)/(b): the fix must not regress it while making the "reverted to
    default" case (a)/(b) also work.
    """
    store_p = state_module.load_state()
    assert store_p.last_poll_failure is None

    store_s = _second_process_store()
    store_s.record_poll_failure(reason="fetch failed")
    assert store_s.last_poll_failure is not None

    store_p.record_seen_sha("1111111111111111111111111111111111111a")

    verify_store = _second_process_store()
    assert verify_store.last_poll_failure is not None, (
        "a field set by another process must survive this instance's "
        "own unrelated mutation"
    )
    assert verify_store.last_poll_failure["reason"] == "fetch failed"


# ---------------------------------------------------------------------------
# (d) other process advances last_seen_sha -> kept
# ---------------------------------------------------------------------------


def test_unrelated_mutation_does_not_revert_last_seen_sha_advanced_elsewhere(
    isolated_state_dir: Path,
) -> None:
    """Store P loads state with `last_seen_sha` at an old value. A second

    store S advances it to a new value. P then performs an unrelated
    mutation (`clear_poll_failure`, touching only `last_poll_failure`).
    P's write must not revert `last_seen_sha` back to the old value it
    still holds in memory.
    """
    old_sha = "2222222222222222222222222222222222222b"
    new_sha = "3333333333333333333333333333333333333c"

    store_p = state_module.load_state()
    store_p.record_seen_sha(old_sha)
    store_p = state_module.load_state()
    assert store_p.last_seen_sha == old_sha

    store_s = _second_process_store()
    store_s.record_seen_sha(new_sha)
    assert store_s.last_seen_sha == new_sha

    # Unrelated mutation from P — never touches last_seen_sha itself.
    store_p.clear_poll_failure()

    verify_store = _second_process_store()
    assert verify_store.last_seen_sha == new_sha, (
        "another process's advance of last_seen_sha must survive this "
        "instance's own unrelated mutation made from a stale in-memory "
        "copy that still held the OLD sha"
    )


# ---------------------------------------------------------------------------
# (e) poll-side pending decision must use disk state, not stale memory
# ---------------------------------------------------------------------------


def test_poll_side_pending_decision_must_reflect_on_disk_state(
    isolated_state_dir: Path,
) -> None:
    """A poll-side caller decides between `set_pending` (no existing

    pending record) and `accumulate_pending` (merge onto an existing
    one) by reading `store.pending` — this must reflect the freshly
    on-disk value at decision time, not a stale in-memory snapshot that
    still shows a pending record another process already resolved.

    Repro: store P loads state while `pending` is set (from an earlier
    commit). A second store S resolves it (clearing `pending`,
    advancing `base_sha`). P, still holding the stale non-None
    `pending` in its OWN in-memory view, must not have that
    stale-but-not-yet-persisted value survive a `_locked_rmw` call and
    get written back — because if it does, the merge computed inside
    `_locked_rmw` (which is also what backs `store.pending` after any
    mutation) resurrects the resolved commit, and a caller reading
    `store.pending` right after would wrongly conclude a record is
    still pending and call `accumulate_pending` (which would then
    itself raise `ValueError`, since — per the bug — it would be
    accumulating onto a record that was supposed to be gone) instead of
    `set_pending` for the new, unrelated commit.
    """
    old_sha = "4444444444444444444444444444444444444d"
    new_sha = "5555555555555555555555555555555555555e"

    store_p = state_module.load_state()
    store_p.set_pending(
        sha=old_sha,
        author="poller",
        subject="old pending commit",
        classified_paths={"a.md": "instant"},
    )
    store_p = state_module.load_state()
    assert store_p.pending is not None

    store_s = _second_process_store()
    store_s.resolve_pending(old_sha)
    assert store_s.pending is None  # sanity: S's own resolve landed

    # P performs some other unrelated mutation first, e.g. advancing
    # last_seen_sha for the new tick it is about to classify — this is
    # the same "stale merge resurrects a stale field" defect, but
    # observed through the lens that matters for poll.py's own
    # set_pending/accumulate_pending branch choice: `store_p.pending`
    # must not still read as non-None after this.
    store_p.record_seen_sha(new_sha)

    assert store_p.pending is None, (
        "after an unrelated mutation, this instance's own view of "
        "`pending` must reflect the on-disk clear made by another "
        "process — not resurrect the stale in-memory record, which "
        "would wrongly steer a poll-side caller into accumulate_pending "
        "instead of set_pending for an unrelated new commit"
    )

    # Confirms the practical consequence: set_pending (the correct
    # branch for "no pending record exists") succeeds cleanly.
    store_p.set_pending(
        sha=new_sha,
        author="poller",
        subject="new unrelated commit",
        classified_paths={"b.md": "instant"},
    )
    verify_store = _second_process_store()
    assert verify_store.pending is not None
    assert verify_store.pending["sha"] == new_sha
