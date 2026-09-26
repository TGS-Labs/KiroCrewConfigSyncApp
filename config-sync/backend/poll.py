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
   commit's changed-path list and metadata (design.md: "fetch the commit's
   changed-path list") via `_fetch_commit_details`. Each changed path is
   classified via `classify.classify_paths` against every tracked root
   (`backend/collect.py`'s root A/B — the bundle tree interleaves both
   with no per-root prefix), the merged classified paths are recorded via
   `state.set_pending`, the operator is notified exactly once, and
   `state.last_seen_sha` advances to the new head (requirements.md 4.3).
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

## Changed-path fetch mechanism (senior review C1/C2/H4 fix)

`_fetch_commit_details` does **not** run a bare, object-less ``git show`` in
the app's state directory. Two real-git facts rule that shape out:

- ``git show --no-patch --name-only --format=...`` is an invalid flag
  combination — git rejects ``--name-only``/``--name-status``/``--check``
  combined with ``--no-patch``/``-s`` with exit 128 (confirmed against real
  git). ``--no-patch`` and ``--name-only`` both suppress/select the same
  diff output and cannot be combined.
- Even with valid flags, a single-commit ``git show`` on a **merge**
  commit returns an EMPTY changed-path list by default (confirmed against
  a real merge commit) — git only shows a merge's diff with ``-m``/``-c``,
  and Kiro-Config-Bundles disallows squash-merge org-wide, so every real
  head advance on that repo IS a merge commit.
- The app's state directory is not a git repository at all, so no local
  git call there can read any object regardless of flags — `git ls-remote`
  transfers no objects.

The fix reuses `backend.push`'s own bundle-repo clone
(`state.get_state_dir() / _BUNDLE_CLONE_DIRNAME`, the same directory
`push.py` clones/fetches to push a branch) as poll's object source — one
clone per host, not a second one, and never the full-clone-on-every-tick
design.md forbids: the clone is created once (first tick) and updated with
a plain ``fetch origin`` on every later tick, exactly like `push.py`'s own
clone-or-fetch step.

Changed paths are computed over the **range** `last_seen_sha..head_sha`
(the commits new since the last-recorded head) via
``git log --first-parent --name-only``, not a single commit's own diff —
`--first-parent` walks main's own line of history one merge at a time,
which correctly attributes every file a merge commit brought in (verified
against a real merge commit: the merge's own tree-diff is empty, but
`--first-parent --name-only` over the range containing it lists the
merged-in paths) and matches how the bundle repo's history actually looks
(PRs merge into `main`; no squash, no rebase-off-trunk divergent merges).
On the very first tick (`last_seen_sha is None`, nothing to range from),
`-1 head_sha` — a single-commit log — is used instead, since there is no
prior boundary to range against. Author/subject for the record come from a
separate, valid single-commit call: ``git show -s --format=%an%x09%s
<head_sha>`` (``-s`` alone, no ``--name-only``, so the flag conflict above
does not apply).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

from backend import classify, state
from backend.safety import git_safety

#: Name/URL of the bundle repo every poll tick checks, matching
#: `backend/push.py`'s `BUNDLE_REPO_URL` and `backend/buildo_pr.py`'s
#: `TARGET_REPO`/`TARGET_BASE` (`TGS-Labs/Kiro-Config-Bundles`, `main`).
BUNDLE_REPO_URL = "https://github.com/TGS-Labs/Kiro-Config-Bundles.git"
BUNDLE_DEFAULT_BRANCH = "main"

#: Directory name, under the app's own state directory
#: (`state.get_state_dir()`), that holds the bundle repo's working clone —
#: the SAME directory name `backend.push` clones/fetches to, so poll and
#: push share one on-disk clone per host rather than each maintaining its
#: own (design.md forbids a full clone; sharing one clone means poll's
#: object-fetch needs never trigger a second one).
_BUNDLE_CLONE_DIRNAME = "bundle-repo"

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


def _ensure_bundle_clone(clone_dir: Path) -> None:
    """Make sure ``clone_dir`` holds a git clone of the bundle repo, cloning

    it once if absent and otherwise fetching the latest objects — the same
    clone-or-fetch shape `backend.push`'s own bundle-repo step uses, and
    (by design) the SAME directory, so poll and push share one on-disk
    clone rather than each maintaining a separate one. This is the only
    place poll.py creates or updates that clone; `_fetch_commit_details`
    below only ever fetches/reads inside it once it exists.

    Never a full clone on every tick: after the first call the ``.git``
    directory already exists and this degrades to a plain ``fetch``.
    """
    clone_dir.mkdir(parents=True, exist_ok=True)
    if not (clone_dir / ".git").exists():
        subprocess.run(
            git_safety.git_argv(
                clone_dir.parent, "clone", BUNDLE_REPO_URL, str(clone_dir)
            ),
            check=True,
            capture_output=True,
            text=True,
        )
    else:
        subprocess.run(
            git_safety.git_argv(clone_dir, "fetch", "origin"),
            check=True,
            capture_output=True,
            text=True,
        )


def _changed_paths_for_range(
    clone_dir: Path, old_sha: str | None, new_sha: str
) -> List[str]:
    """Return the de-duplicated changed-path list for the commits new since

    ``old_sha`` (exclusive) up to and including ``new_sha``, via
    ``git log --first-parent --name-only``.

    A single commit's own ``git show``/``git diff-tree`` reports an EMPTY
    path list for a merge commit unless invoked with ``-m``/``-c``
    (confirmed against a real merge commit) — and Kiro-Config-Bundles
    disallows squash-merge org-wide, so every real head advance on that
    repo is itself a merge commit. Walking the ``--first-parent`` RANGE
    instead correctly attributes every file a merge (or a run of several
    merges since the last poll) brought in, and matches how the bundle
    repo's own history looks: PRs merge into `main` one at a time, so
    `--first-parent` never diverges from that single line.

    Args:
        clone_dir: the bundle repo's local clone (must already hold
            ``new_sha`` — the caller fetches first).
        old_sha: the previously-seen head (``state.last_seen_sha``), or
            ``None`` on the very first poll ever run, in which case there
            is no prior boundary to range against and a single-commit log
            of ``new_sha`` alone is used instead.
        new_sha: the newly resolved head to walk up to (inclusive).

    Returns:
        The changed paths across that range, in the order git reports
        them, with duplicates (a path touched by more than one commit in
        the range) collapsed to their first occurrence.
    """
    if old_sha:
        args = [
            "log",
            "--first-parent",
            "--name-only",
            "--format=",
            f"{old_sha}..{new_sha}",
        ]
    else:
        args = ["log", "--first-parent", "--name-only", "--format=", "-1", new_sha]

    completed = subprocess.run(
        git_safety.git_argv(clone_dir, *args),
        capture_output=True,
        text=True,
        check=True,
    )
    stdout = completed.stdout or ""
    seen: Dict[str, None] = {}
    for line in stdout.splitlines():
        if line and line not in seen:
            seen[line] = None
    return list(seen)


def _fetch_commit_details(
    state_dir_owner: str, sha: str, old_sha: str | None = None
) -> Tuple[str, str, List[str]]:
    """Return ``(author, subject, changed_paths)`` for the range up to

    ``sha``, using two valid, hardened git calls against a real object
    source — never the invalid ``--no-patch``/``--name-only`` combination
    (git rejects that with exit 128) and never a bare call in a directory
    with no fetched objects.

    Args:
        state_dir_owner: the app's own state directory
            (`state.get_state_dir()`) — the bundle repo's clone is kept at
            ``state_dir_owner / _BUNDLE_CLONE_DIRNAME``, matching
            `backend.push`'s own clone location so the two jobs share one
            on-disk clone.
        sha: the newly resolved head commit.
        old_sha: the previously-seen head (``state.last_seen_sha``), or
            ``None`` on the first-ever poll — forwarded to
            `_changed_paths_for_range` to select a range vs. a
            single-commit log.

    Returns:
        A ``(author, subject, changed_paths)`` tuple. ``author``/``subject``
        are empty strings if ``sha``'s metadata could not be read (e.g. an
        empty ``git show`` result on a repo test-double).
    """
    clone_dir = Path(state_dir_owner) / _BUNDLE_CLONE_DIRNAME
    _ensure_bundle_clone(clone_dir)

    metadata_completed = subprocess.run(
        git_safety.git_argv(clone_dir, "show", "-s", "--format=%an%x09%s", sha),
        capture_output=True,
        text=True,
        check=True,
    )
    metadata_stdout = metadata_completed.stdout or ""
    metadata_lines = metadata_stdout.splitlines()
    if not metadata_lines:
        author, subject = ("", "")
    else:
        metadata_line = metadata_lines[0]
        if "\t" in metadata_line:
            author, subject = metadata_line.split("\t", 1)
        else:
            author, subject = (metadata_line, "")

    changed_paths = _changed_paths_for_range(clone_dir, old_sha, sha)
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
    old_sha = store.last_seen_sha
    author, subject, changed_paths = _fetch_commit_details(state_dir, head_sha, old_sha)
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
    result = run()
    raise SystemExit(0 if result.outcome != "ls-remote-failed" else 1)
