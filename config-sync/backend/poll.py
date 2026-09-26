"""The poll job (design.md `backend/poll.py`), a cron `command` target.

Runs as a plain module invocation (``python3 -m backend.poll``), never a
``message`` cron target, so a quiet tick costs zero LLM tokens
(requirements.md 4.1). Resolves the bundle repo's default-branch head via a
single ``git ls-remote`` — never a full clone (design.md: "`git ls-remote
<bundle-repo> <default-branch>` -> head SHA. Unchanged -> exit.").

Outcomes:

1. Unchanged head (``ls-remote`` result == ``state.last_seen_sha``): exit 0,
   no notification, no git call beyond the single ``ls-remote``.
2. Changed head, not yet pending for this SHA (task 4.3): fetch the new
   commit's metadata and changed paths via a single second, hardened git
   call (``git show --no-patch --name-only --format=%an%x09%s <sha>``).
   Each changed path is classified via `classify.classify_paths` against
   every tracked root (`backend/collect.py`'s root A/B — the bundle tree
   interleaves both with no per-root prefix), the merged classified paths
   are recorded via `state.set_pending`, the operator is notified exactly
   once, and `state.last_seen_sha` advances to the new head
   (requirements.md 4.3).
3. ``ls-remote`` failure (non-zero exit or raised exception), or an
   unresolvable head (empty SHA): exit non-zero, ``state.last_seen_sha``
   left unchanged, no notification.

A commit that is already pending (recorded on a prior tick, not yet
approved/declined) never triggers a second classify/notify cycle: once
`state.last_seen_sha` advances to a pending commit's SHA, a later tick
reporting that same head is caught by the unchanged-head case above and
never re-enters the changed-head path (requirements.md 4.4).

Nothing in this module ever applies a change to either tracked
configuration root — that is Deployment 4's approved-only route
(requirements.md 4.6); this job only classifies, records, and notifies.

Modelled on `backend/push.py`'s module shape: a frozen result dataclass, a
zero-argument ``run()`` entrypoint, and a patchable ``notify_operator`` seam
matching `backend/pr_handoff.py`'s convention.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Dict, List, Tuple

from backend import classify, state
from backend.safety import git_safety

#: Name/URL of the bundle repo every poll tick checks, matching
#: `backend/push.py`'s `BUNDLE_REPO_URL` and `backend/buildo_pr.py`'s
#: `TARGET_REPO`/`TARGET_BASE` (`TGS-Labs/Kiro-Config-Bundles`, `main`).
BUNDLE_REPO_URL = "https://github.com/TGS-Labs/Kiro-Config-Bundles.git"
BUNDLE_DEFAULT_BRANCH = "main"

#: Root ids `classify.classify_paths` is tried against, matching
#: `backend/collect.py`'s `_roots()` — the bundle repo's tree interleaves
#: both roots' relpaths with no per-root prefix (`collect.collect()` merges
#: both into one flat mapping), so a changed-head commit's paths cannot be
#: pre-sorted by root; each path is classified against every root's
#: allowlist and the classified/ignored results are merged.
_ROOT_IDS: Tuple[str, ...] = ("A", "B")


@dataclass(frozen=True)
class PollResult:
    """Outcome of a single poll-job tick.

    ``outcome`` distinguishes the three cases this module reports:
    ``"unchanged"`` (head matches `state.last_seen_sha`), ``"changed"``
    (head differs — the signal task 4.2/4.3 consumes), and
    ``"ls-remote-failed"`` (the resolution itself failed). ``head_sha`` is
    the newly resolved head SHA on ``"changed"``; ``None`` otherwise.
    """

    outcome: str
    head_sha: str | None = None
    reason: str = ""


def notify_operator(*, head_sha: str, reason: str = "") -> None:
    """Notify the operator that the bundle repo's head has changed.

    The real notification channel is out of scope for this task — this is
    the seam tests patch. A production implementation wires this to the
    app's actual notification path; until then this is a deliberate no-op,
    matching `backend/pr_handoff.py`'s `notify_operator` convention so the
    poll job's own unchanged/failure paths can assert it was never called.
    """


def _resolve_remote_head(state_dir_owner: str) -> str:
    """Resolve the bundle repo's default-branch head via a single

    ``git ls-remote`` call, built through `git_safety.git_argv` (never a
    hand-built argv) — the single hardened call-site every host-side git
    invocation in this app must route through.

    Args:
        state_dir_owner: the directory `git_argv`'s ``-C`` targets. A
            pre-clone probe (design.md: "no full clone") has no gitdir of
            its own to protect, so any writable directory works; the app's
            own state directory is used so no scratch directory is
            created just for this call.

    Returns:
        The resolved head SHA (the first whitespace-delimited token of
        ``git ls-remote``'s stdout).

    Raises:
        subprocess.CalledProcessError: if the ``git ls-remote`` process
            exits non-zero.
    """
    completed = subprocess.run(
        git_safety.git_argv(
            state_dir_owner, "ls-remote", BUNDLE_REPO_URL, BUNDLE_DEFAULT_BRANCH
        ),
        capture_output=True,
        text=True,
        check=True,
    )
    stdout = completed.stdout or ""
    first_line = stdout.strip().splitlines()[0] if stdout.strip() else ""
    head_sha = first_line.split()[0] if first_line else ""
    return head_sha


def _fetch_commit_details(state_dir_owner: str, sha: str) -> Tuple[str, str, List[str]]:
    """Return ``(author, subject, changed_paths)`` for ``sha`` via a single

    hardened ``git show --no-patch --name-only --format=%an%x09%s <sha>``-
    shaped call, built through `git_safety.git_argv`. One combined call
    (metadata line, blank separator, then changed paths) rather than two
    separate git invocations — the standard `git show` shape for "this
    commit's metadata plus its changed-path list" (design.md: "fetch the
    commit's changed-path list"), and the format this app's own test
    fixtures model (`test_poll_pending.py`'s `_show_stdout`).

    Args:
        state_dir_owner: the directory `git_argv`'s ``-C`` targets, matching
            `_resolve_remote_head`'s convention.
        sha: the commit to read metadata and changed paths for.

    Returns:
        A ``(author, subject, changed_paths)`` tuple. ``author``/``subject``
        are empty strings and ``changed_paths`` is empty if the commit has
        no readable output.
    """
    completed = subprocess.run(
        git_safety.git_argv(
            state_dir_owner,
            "show",
            "--no-patch",
            "--name-only",
            "--format=%an%x09%s",
            sha,
        ),
        capture_output=True,
        text=True,
        check=True,
    )
    stdout = completed.stdout or ""
    lines = stdout.splitlines()
    if not lines:
        return ("", "", [])

    metadata_line = lines[0]
    if "\t" in metadata_line:
        author, subject = metadata_line.split("\t", 1)
    else:
        author, subject = (metadata_line, "")

    changed_paths = [line for line in lines[1:] if line]
    return (author, subject, changed_paths)


def _classify_changed_paths(changed_paths: List[str]) -> Dict[str, str]:
    """Classify ``changed_paths`` against every tracked root and merge

    the results into one ``{relpath: propagation_class.value}`` mapping
    suitable for `state.set_pending`'s ``classified_paths`` argument.

    The bundle repo's tree interleaves both roots' relpaths with no
    per-root prefix (`backend/collect.py`'s `collect()`), so a changed
    path cannot be pre-sorted by root before classifying — each path is
    tried against every root in `_ROOT_IDS` via `classify.classify_paths`
    (never a reimplemented matcher), and a path classified under any root
    is kept.
    """
    merged: Dict[str, str] = {}
    for root_id in _ROOT_IDS:
        result = classify.classify_paths(root_id, changed_paths)
        for relpath, propagation_class in result.classified.items():
            merged[relpath] = propagation_class.value
    return merged


def run() -> PollResult:
    """Run one poll-job tick: resolve the bundle repo's head, compare it

    against `state.last_seen_sha`, and report the outcome.

    Zero-argument, matching the `command` cron entrypoint shape
    (`python3 -m backend.poll`) — no agent/LLM context is required to call
    it (requirements.md 4.1).

    Returns:
        A :class:`PollResult` describing the outcome. On a resolution
        failure this function does not raise: it records no state change
        and returns ``outcome="ls-remote-failed"`` so the cron wrapper can
        turn that into a non-zero process exit without this module owning
        the exit-code mechanics itself.
    """
    store = state.load_state()

    try:
        head_sha = _resolve_remote_head(str(state.get_state_dir()))
    except (subprocess.CalledProcessError, OSError) as exc:
        return PollResult(outcome="ls-remote-failed", reason=str(exc))

    if head_sha == store.last_seen_sha:
        return PollResult(outcome="unchanged", head_sha=head_sha)

    if not head_sha:
        # `_resolve_remote_head` returns "" (rather than raising) when
        # `git ls-remote`'s stdout could not be parsed into a SHA — e.g. a
        # `returncode != 0` result that never raised because the caller
        # didn't enforce `check=True` (test_poll.py's
        # `test_ls_remote_failure_from_a_nonzero_returncode_leaves_sha_
        # unchanged`). An empty SHA is never a genuine changed head: it
        # must not be recorded as `last_seen_sha`, classified, or notified
        # on — treat it the same as an unresolved head.
        return PollResult(outcome="ls-remote-failed", reason="empty head sha")

    # NOTE: no separate "already pending for this exact SHA" guard is
    # needed here. `run()` always advances `state.last_seen_sha` to
    # `head_sha` in the SAME tick it calls `state.set_pending` below, so
    # `last_seen_sha == pending["sha"]` holds as an invariant from that
    # point on — a later tick reporting the same still-pending SHA is
    # already caught by the `head_sha == store.last_seen_sha` check above
    # and returns "unchanged" before reaching this point. A commit stays
    # observably pending (via `state.pending`) without a second
    # classify/notify cycle simply because `last_seen_sha` never moves
    # again until a genuinely NEW head appears (requirements.md 4.4).

    state_dir = str(state.get_state_dir())
    author, subject, changed_paths = _fetch_commit_details(state_dir, head_sha)
    classified_paths = _classify_changed_paths(changed_paths)

    store.set_pending(
        sha=head_sha,
        author=author,
        subject=subject,
        classified_paths=classified_paths,
    )
    notify_operator(head_sha=head_sha)
    store.record_seen_sha(head_sha)

    return PollResult(outcome="changed", head_sha=head_sha)


if __name__ == "__main__":
    run()
