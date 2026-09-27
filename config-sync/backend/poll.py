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
   changed-path list") via `_fetch_commit_details`, ranged from
   `state.base_sha` — the decision boundary, not `state.last_seen_sha`
   (requirements.md 4.9; see "Changed-path range boundary" below). Each
   changed path is classified via `classify.classify_paths` against every
   tracked root (`backend/collect.py`'s root A/B — the bundle tree
   interleaves both with no per-root prefix), and the merged classified
   paths are recorded via `state.set_pending` (no existing pending record)
   or `state.accumulate_pending` (a commit is already pending — merges
   rather than replaces). The operator is notified once per tick that
   found a changed head, and `state.last_seen_sha` advances to the new
   head (requirements.md 4.3).
3. ``ls-remote`` failure (non-zero exit or raised exception), or an
   unresolvable head (empty SHA): exit non-zero, ``state.last_seen_sha``
   left unchanged, no notification.
4. Any failure AFTER the head is resolved as changed — the bundle-repo
   clone/fetch, the commit-metadata/changed-path git calls, or classification
   itself (senior-review round-2 H-new-1): exit non-zero via
   ``outcome="fetch-failed"``, ``state.last_seen_sha`` left unchanged (so the
   next tick re-attempts the SAME head rather than skipping it), no
   notification. Without this, a persistently failing poll — the app.json
   cron is ``"silent": true`` — was completely invisible: no ``PollResult``
   reached the caller, nothing was recorded, and the raised exception simply
   propagated out of ``run()``.

A commit that is already pending (recorded on a prior tick, not yet
approved/declined) never triggers a second NOTIFICATION cycle for the
same head — once `state.last_seen_sha` advances to a pending commit's
SHA, a later tick reporting that same head is caught by the
unchanged-head case above. But a LATER, genuinely new head arriving while
a commit is still pending DOES re-enter the changed-head path, and MUST:
accumulate its classified paths into the existing pending record via
`state.accumulate_pending` rather than starting a fresh one, and leave
`state.base_sha` untouched (requirements.md 4.4, 4.9).

## Changed-path range boundary: `base_sha`, not `last_seen_sha` (#65)

The changed-path fetch below (`_fetch_commit_details`) is always ranged
from `state.base_sha` — the head commit as of the operator's LAST actual
approve/decline decision (or, before any decision has ever been made,
this instance's first-ever polled commit) — and NEVER from
`state.last_seen_sha`, which advances on every tick regardless of whether
anything is pending. Ranging from `last_seen_sha` was the original defect
(#65): once a commit went pending, the next changed tick would range from
THAT pending commit's own SHA, so `_classify_changed_paths` only ever saw
the newest tick's own commits, and the pre-fix `state.set_pending`
overwrote the pending record with just those — silently dropping the
earlier pending commit's still-unapplied files the moment a second commit
landed before the operator acted. `base_sha` is a separate, durable
`state.py` field for exactly this reason: it is the one thing in this
module that must NOT move on every tick, only on an operator decision (not
yet buildable in this codebase — see `state.advance_base_sha`'s TODO).

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

Changed paths are computed over the **range** `base_sha..head_sha` (the
commits new since the operator's last actual decision, or since this
instance's first-ever poll if no decision has been made yet — NOT
`last_seen_sha`, which advances on every tick regardless of pending
state; see "Changed-path range boundary" above) via
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

## Poll failures (senior-review rounds 4 and 5)

`state.record_poll_failure` increments `state.poll_consecutive_failures` on
every failing tick and `state.clear_poll_failure` resets it on the next
tick that resolves the head cleanly; `status()` exposes the count so the
page can say how long the poll has been failing. The app has NO pause of
its own. A failing tick exits non-zero, and KiroCrew's cron runner already
auto-pauses a command cron after 5 consecutive failures
(`kiro_crew/cron.py` `_AUTO_PAUSE_THRESHOLD`); the operator re-enables it
on the Schedule page. Round 4 added a second, app-level pause at the same
threshold and round 5 removed it: the two fought each other (Push now
cleared the app flag while the cron stayed off; a re-enabled cron then
exited 0 as "paused" and did nothing).

## Concurrent-clone lock (senior review round-2 M-new-2)

``config-sync-push`` and ``config-sync-poll`` are both declared in
``app.json`` on the same ``every: 900`` cadence and both read/write the
SAME ``_BUNDLE_CLONE_DIRNAME`` directory (poll fetches/reads it; push
clones/fetches/commits/pushes it). Two ticks landing at the same wall-clock
moment could both see no ``.git`` directory yet and both start a `clone`
into the identical path concurrently — one of git's own clone attempts can
then fail (target directory not empty / lock contention on
``.git/index.lock`` or the object store), leaving a directory that exists,
is non-empty, but has no working ``.git`` — a state
`_ensure_bundle_clone`'s own `.git`-exists check can never self-heal from,
because every later tick's ``clone`` attempt fails the same way against
that already-non-empty directory forever.

`_clone_lock` takes a simple, timeout-bounded ``flock`` (POSIX advisory
file lock, stdlib `fcntl`) on a dedicated lockfile living NEXT TO the clone
directory (not inside it, so the lock survives even a wedged/partial clone)
before either job touches the shared clone. Both `poll.py`'s
`_ensure_bundle_clone` and `push.py`'s inline clone-or-fetch step hold this
SAME lock (defined once in `git_safety.clone_lock`) for the same directory,
so the two jobs actually serialize against each other (round-3 H2 closed
the gap where only `poll.py`'s call site held it).
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple

from backend import apply as apply_module
from backend import classify, state
from backend.materialize import (
    _apply_result_to_dict,
    _materialize_pending_commit,
    _MaterializeError,
)
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

#: How long `_clone_lock` waits to acquire the shared clone-directory lock
#: before giving up and raising — bounded so a wedged/dead lock holder
#: cannot hang a poll tick forever. 60s comfortably exceeds a clone/fetch
#: of the bundle repo on a normal connection.
_CLONE_LOCK_TIMEOUT_SECS = 60.0

#: Poll interval, in seconds, between `_clone_lock` acquisition attempts.
_CLONE_LOCK_POLL_INTERVAL_SECS = 0.2

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

    ``outcome`` distinguishes the five cases this module reports:
    ``"unchanged"`` (head matches `state.last_seen_sha`), ``"changed"``
    (head differs — the signal task 4.2/4.3 consumes), ``"ls-remote-failed"``
    (the head resolution itself failed), ``"fetch-failed"`` (the head
    resolved as changed, but a LATER step — the bundle-repo clone/fetch, the
    commit-metadata/changed-path git calls, or classification — raised;
    senior-review round-2 H-new-1), and ``"apply-error"`` (the head resolved
    and classified, but the resolved commit could not be materialized from
    the bundle repo — the `git archive` step itself failed; `base_sha`/
    `pending` are reverted to their pre-tick values in this case, not left
    at whatever `record_poll_pending` wrote for this tick). ``head_sha`` is
    the newly resolved head SHA on ``"changed"``, ``"fetch-failed"``, or
    ``"apply-error"``; ``None`` otherwise.
    """

    outcome: str
    head_sha: str | None = None
    reason: str = ""


#: Outcomes that represent a failed tick — used by the ``__main__`` guard to
#: decide the process exit code (design.md's error table: "Poll exits
#: non-zero"). Kept as one named set rather than a per-call-site string
#: comparison so a THIRD failure outcome added later cannot be missed at
#: the single exit-code call-site (the ``__main__`` guard below).
_FAILURE_OUTCOMES = frozenset({"ls-remote-failed", "fetch-failed", "apply-error"})


def notify_operator(
    *,
    head_sha: str,
    author: str = "",
    subject: str = "",
    touched_classes: List[str] | None = None,
    not_applied: Dict[str, str] | None = None,
) -> None:
    """Notify the operator that the bundle repo's head has changed.

    Requirements.md 4.3: the notification must identify "the commit, its
    author, its subject, and which tracked configuration classes the
    change touches" — this prints a one-line summary carrying all four to
    stdout.

    ``not_applied`` (senior-review round-4 H fix) is the ``{relpath:
    reason}`` mapping for a `"partial"` apply outcome — every not-applied
    path and its real per-path reason is rendered into the same
    notification, so a partial apply's operator-facing message names WHAT
    failed and WHY, not merely that something changed. ``None``/empty for
    a full apply (the common case), which renders exactly as before this
    fix.

    This job runs as a `command` cron target (never `message`/an agent
    turn — requirements.md 4.1), so there is no agent session to hand a
    notification to. The delivery mechanism for a `command` cron job is
    its OWN stdout: KiroCrew's cron runner (`kiro_crew/slack/gateway.py`'s
    `_cron_callback`) captures the subprocess's stdout, and on a
    successful run with non-empty output records it as the job's result
    and surfaces it via the dashboard/Slack notification path UNLESS the
    job is `"silent": true` in `app.json` — the runner's own empty-output
    branch is commented "no output = no delivery", the exact contrapositive
    of what this function relies on. `app.json`'s `config-sync-poll` cron
    entry is therefore flipped to `"silent": false` alongside this fix: a
    silent job's stdout is captured into `last_result` for the dashboard's
    cron-history view but never pushed as a notification, which would
    leave `run()`'s "changed" outcome just as invisible to the operator as
    the empty no-op stub this replaces.

    On the `"unchanged"` and every failure outcome (`"ls-remote-failed"`,
    `"fetch-failed"`) `run()` never calls this function at all — printing
    nothing on those ticks is what keeps a quiet/failing poll silent
    per-tick while still surfacing the one outcome that matters
    (requirements.md 4.2's "no notification on unchanged" carries over
    unchanged: silence is enforced by never calling this, not by this
    function suppressing its own output). A `"partial"` outcome DOES call
    this function (senior-review round-4 H fix) — see `run()`'s own
    comment at that call site for why the pre-fix early return there was
    a defect.
    """
    touched = ", ".join(touched_classes or []) or "(none)"
    lines = [
        f"config-sync: bundle repo head changed to {head_sha}",
        f"  author:  {author}",
        f"  subject: {subject}",
        f"  touched: {touched}",
    ]
    if not_applied:
        lines.append("  not applied:")
        for relpath, reason in sorted(not_applied.items()):
            lines.append(f"    {relpath}: {reason}")
    print("\n".join(lines), flush=True)


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


def _clone_lock(clone_dir: Path) -> contextlib.AbstractContextManager[None]:
    """Hold the shared bundle-repo clone-directory lock (senior-review

    round-2 M-new-2; round-3 H2).

    Delegates to `git_safety.clone_lock` — the lock is now defined ONCE in
    a module both `poll.py` and `push.py` already import, so the two jobs
    actually serialize against each other (round-2's fix only wrapped
    poll's own call site with a poll-local lock, leaving push's identical
    unlocked clone-or-fetch on the SAME directory able to race against
    poll's — round-3 H2). This wrapper is kept, rather than calling
    `git_safety.clone_lock` directly at poll's call site, so
    `_CLONE_LOCK_TIMEOUT_SECS`/`_CLONE_LOCK_POLL_INTERVAL_SECS` stay
    patchable as poll.py module attributes (existing tests monkeypatch
    them here to exercise the timeout path without a slow test).
    """
    return git_safety.clone_lock(
        clone_dir,
        timeout_secs=_CLONE_LOCK_TIMEOUT_SECS,
        poll_interval_secs=_CLONE_LOCK_POLL_INTERVAL_SECS,
    )


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

    The whole clone-or-fetch decision AND action runs under `_clone_lock`
    (senior-review round-2 M-new-2): the ``.git``-exists check and the
    `clone`/`fetch` it selects must be atomic with respect to a concurrent
    `push.py` tick touching the same directory, or two processes can both
    observe "no `.git` yet" and both start a `clone` into the same path.
    """
    with _clone_lock(clone_dir):
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

    Invoked with ``-c core.quotePath=false`` (senior-review round-2 M3):
    git's default ``core.quotePath=true`` renders any non-ASCII filename
    (e.g. ``steering/café.md``) as a quoted string with C-style octal
    escapes (``"steering/caf\\303\\251.md"``), which never matches an
    allowlist entry written against the real UTF-8 relpath — silently
    dropping that file from `classify_paths`'s input. Disabling
    ``quotePath`` for this call makes git emit the raw UTF-8 path instead.

    Args:
        clone_dir: the bundle repo's local clone (must already hold
            ``new_sha`` — the caller fetches first).
        old_sha: the decision boundary to range from — `state.base_sha`
            (the operator's last approve/decline, or this instance's
            first-ever polled commit before any decision), NOT
            `state.last_seen_sha` — or ``None`` on the very first poll
            ever run, in which case there is no prior boundary to range
            against and a single-commit log of ``new_sha`` alone is used
            instead.
        new_sha: the newly resolved head to walk up to (inclusive).

    Returns:
        The changed paths across that range, in the order git reports
        them, with duplicates (a path touched by more than one commit in
        the range) collapsed to their first occurrence.
    """
    if old_sha:
        args = [
            "-c",
            "core.quotePath=false",
            "log",
            "--first-parent",
            "--name-only",
            "--format=",
            f"{old_sha}..{new_sha}",
        ]
    else:
        args = [
            "-c",
            "core.quotePath=false",
            "log",
            "--first-parent",
            "--name-only",
            "--format=",
            "-1",
            new_sha,
        ]

    completed = subprocess.run(
        git_safety.git_argv(clone_dir, *args),
        capture_output=True,
        text=True,
        encoding="utf-8",
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
        old_sha: the decision boundary (`state.base_sha`), or ``None`` on
            the first-ever poll — forwarded to `_changed_paths_for_range`
            to select a range vs. a single-commit log.

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
        encoding="utf-8",
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


def _classify_changed_paths(
    changed_paths: List[str],
) -> Tuple[Dict[str, str], List[str], List[str]]:
    """Classify ``changed_paths`` against every tracked root and merge

    the results into ``(classified, ignored, touched_classes)`` — the
    ``classified`` mapping suitable for `state.set_pending`'s
    ``classified_paths`` argument, plus the paths ``classify.classify_paths``
    matched against NO root (``ignored``) and the distinct propagation
    classes actually represented in ``classified`` (``touched_classes``,
    each rendered as its ``.value`` string — senior-review round-2 M2).

    The bundle repo's tree interleaves both roots' relpaths with no
    per-root prefix (`backend/collect.py`'s `collect()`), so a changed
    path cannot be pre-sorted by root before classifying — each path is
    tried against every root in `_ROOT_IDS` via `classify.classify_paths`
    (never a reimplemented matcher), and a path classified under any root
    is kept. A path counts as ``ignored`` only if it matched NO root at
    all — a path classified under one root is never also reported ignored
    just because a different root's allowlist doesn't recognize it too.
    """
    merged: Dict[str, str] = {}
    ignored_candidates: Dict[str, None] = {}
    touched: set[str] = set()
    for root_id in _ROOT_IDS:
        result = classify.classify_paths(root_id, changed_paths)
        for relpath, propagation_class in result.classified.items():
            merged[relpath] = propagation_class.value
            touched.add(propagation_class.value)
        for relpath in result.ignored:
            ignored_candidates.setdefault(relpath, None)
    ignored = [relpath for relpath in ignored_candidates if relpath not in merged]
    return merged, ignored, sorted(touched)


def _first_parent_parent_sha(state_dir_owner: str, sha: str) -> str | None:
    """Resolve ``sha``'s first-parent parent commit, or ``None`` if

    ``sha`` has no parent (the repository's very first commit).

    Used ONLY by ``_apply_new_head`` for the bootstrap case: the very
    first-ever poll tick has no ``base_sha`` yet (nothing has EVER fully
    applied on this instance), so there is no "last fully applied commit"
    to fall back to on a partial outcome. Falling back to the commit
    right before ``sha`` — rather than leaving ``base_sha`` at ``None``
    or advancing it to the broken ``sha`` itself — is what keeps the next
    tick's retry range correctly bounded (Kiro-Config-Bundles#65's
    skip-intervening-commits class: a ``None`` boundary would make the
    NEXT tick single-commit-log only its own new head, silently never
    retrying whatever of THIS commit's files never applied).

    Raises nothing: a resolution failure (e.g. a shallow clone missing
    the parent object) is treated as "no parent" — ``base_sha`` staying
    ``None`` in that rare case is a safe degradation, not a crash.
    """
    clone_dir = Path(state_dir_owner) / _BUNDLE_CLONE_DIRNAME
    try:
        completed = subprocess.run(
            git_safety.git_argv(clone_dir, "rev-parse", f"{sha}^"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
    except (subprocess.CalledProcessError, OSError, git_safety.GitSafetyError):
        return None
    parent = (completed.stdout or "").strip()
    return parent or None


def _root_ancestor_sha(state_dir_owner: str, sha: str) -> str:
    """Resolve the oldest first-parent ancestor of ``sha`` — the

    repository's very first commit reachable by walking ``sha^``
    repeatedly until a commit has no parent (senior-review round-3
    Medium: a multi-commit bootstrap partial, where NO prior ``base_sha``
    ever existed and the tick's own classify pass only single-commit-
    logged the newest head, must land ``base_sha`` at the earliest commit
    in that same first-parent chain — never at an intermediate commit
    that, like the head itself, was never independently and fully
    applied on its own).

    Used ONLY by ``_apply_new_head``'s bootstrap-partial correction, and
    only when ``pre_tick_base_sha is None`` (no real prior boundary ever
    recorded). Bounded by a plain iterative walk (never recursion) so a
    long history cannot exhaust the stack; each step reuses
    ``_first_parent_parent_sha``'s own resolution-failure handling — a
    walk that cannot resolve a further parent (e.g. a shallow clone)
    simply stops at the last SHA it could resolve, rather than raising.
    """
    current = sha
    while True:
        parent = _first_parent_parent_sha(state_dir_owner, current)
        if parent is None:
            return current
        current = parent


def _restore_dir_has_backup(apply_id: str) -> bool:
    """Return whether the restore directory `apply_commit` recorded for

    `apply_id` actually holds a real backup file or a non-empty
    created-file manifest — the on-disk signal that SOMETHING was
    genuinely written or created this apply attempt (senior-review
    round-4 M fix). `apply_commit` records a restore directory
    unconditionally at the START of every attempt (`backend/apply.py`'s
    own H3 rationale: recording it up front means a crash on file 2 still
    leaves file 1's backup reachable), so its mere existence proves
    nothing about whether this attempt actually wrote anything — only
    its CONTENTS do.

    Walks the directory tree looking for either: a regular file that is
    not named `.created-manifest.json` (a genuine pre-existing-file
    backup, written by `apply._backup_file`), or a
    `.created-manifest.json` whose JSON array is non-empty (a genuinely
    NEW file was created this attempt, even though nothing existed to
    back up). `apply.py`'s own `_CREATED_MANIFEST_NAME` constant is
    mirrored here rather than imported, since this module does not
    otherwise depend on `apply.py`'s internals — the filename is a
    stable, documented on-disk contract (`_write_created_manifest`'s own
    docstring), not a private implementation detail.

    Returns ``False`` (safe default) when the directory does not exist at
    all or cannot be read — an apply attempt that never reached the
    backup step in the first place wrote nothing either.
    """
    restore_dir = Path(state.get_state_dir()) / "restores" / apply_id
    if not restore_dir.is_dir():
        return False

    created_manifest_name = ".created-manifest.json"
    try:
        for candidate in restore_dir.rglob("*"):
            if not candidate.is_file():
                continue
            if candidate.name != created_manifest_name:
                return True
            try:
                import json

                manifest_contents = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # An unreadable/corrupt manifest is treated as "no proof
                # of a write" rather than raising — this is a best-effort
                # cleanup signal, not a correctness-critical read.
                continue
            if manifest_contents:
                return True
    except OSError:
        return False
    return False


def _apply_new_head(
    store: state.StateStore,
    head_sha: str,
    poll_ignored_paths: List[str],
    *,
    pre_tick_base_sha: str | None,
    pre_tick_pending: Dict[str, Any] | None,
) -> str | None:
    """Materialize and apply ``head_sha`` automatically — the auto-apply

    step every "changed" poll tick runs with NO operator action
    (requirements.md 4.4). Reuses ``backend.materialize._materialize_
    pending_commit`` (the SAME helper the former approve route used) so
    the tree this apply runs against is built identically; the
    materialized temp tree is ALWAYS removed via ``finally``, including
    when ``apply_commit`` itself raises.

    Outcome handling (requirements.md 4.9/4.14):

    - ``"applied"``: every eligible file succeeded. Resolves the pending
      record via ``state.resolve_pending`` — the SAME call the former
      approve route used — advancing ``base_sha`` to ``head_sha`` and
      clearing ``pending``. The caller (``run()``) is responsible for
      recording ``last_pushed_hash`` for the tree this apply just wrote
      (C-A) and for advancing ``last_seen_sha`` — both gated on this
      outcome being ``"applied"``, never on a lesser one.
    - ``"partial"``: at least one eligible file failed. ``pending`` is
      annotated (real per-path reasons, never a placeholder) via
      ``state.record_partial_apply`` rather than resolved, so the SAME
      range is retried on the next tick — the caller (``run()``) must
      NOT advance ``last_seen_sha`` for a partial outcome either (H-1),
      or the next tick's own ``head_sha == last_seen_sha`` short-circuit
      would swallow the retry before ``_apply_new_head`` ever runs again.
      ``base_sha`` must land on the last FULLY-applied commit — never
      ``None``, never ``head_sha`` itself, and never an INTERMEDIATE
      commit inside this same not-fully-applied range (senior-review
      round-3 Medium: a multi-commit tick whose range covers B then C,
      where C's break makes the whole range partial, must leave
      ``base_sha`` at the commit BEFORE the range even started — never
      at B, which was itself never independently and fully applied).
      ``pre_tick_base_sha`` (captured by the caller before this tick's
      own ``record_poll_pending`` call) is exactly that "before the range
      started" boundary, so the correction restores it directly rather
      than walking back a single first-parent hop from ``head_sha``
      (which lands on the range's own intermediate commit, not the
      pre-tick boundary, whenever the range spans more than one commit).
      On the very first-ever tick (no prior ``base_sha`` at all —
      ``pre_tick_base_sha is None``), there is no earlier boundary to
      restore, so the correction instead falls back to ``head_sha``'s own
      parent via ``_first_parent_parent_sha`` — the single-commit-range
      case this fallback already correctly covered.
    - ``"refused-sha-mismatch"``: a later tick already accumulated a
      newer commit into ``pending`` before this apply ran (the same #65
      staleness race the old approve route guarded). Nothing to do here —
      state was never mutated by ``apply_commit``, and the newer pending
      record is untouched.

    A ``_MaterializeError`` (the commit could not be fetched/extracted)
    reverts ``base_sha``/``pending`` to exactly what they were BEFORE this
    tick's own ``record_poll_pending`` call (via ``state.revert_pending``)
    and reports failure to the caller — nothing was ever eligible for
    apply, so a pending record naming a commit this instance never even
    materialized must not survive the tick. Returns ``None`` in this
    case; the caller (``run()``) turns that into an error outcome and
    skips ``record_seen_sha``/``notify_operator`` for this tick, so the
    SAME head is re-resolved and retried on the next poll.

    Args:
        store: the loaded state store for this tick.
        head_sha: the newly resolved head commit being applied.
        poll_ignored_paths: the classifier's ``ignored_paths`` for this
            tick's range (`_classify_changed_paths`'s own ``ignored``
            result) — carried into `_record_last_apply` so `last_apply`
            reports every path the classifier matched against NO
            allowlist, not just whatever `apply_commit`'s narrower
            allowlist gate happens to also flag.
        pre_tick_base_sha: ``store.base_sha`` as it was captured by the
            caller BEFORE this tick's `record_poll_pending` call — restored
            verbatim on a `_MaterializeError`, and used as the exact
            correction target for a multi-commit partial outcome.
        pre_tick_pending: ``store.pending`` as it was captured by the
            caller BEFORE this tick's `record_poll_pending` call — restored
            verbatim on a `_MaterializeError`.

    Returns:
        The apply outcome string (``"applied"``, ``"partial"``, or
        ``"refused-sha-mismatch"``) when the materialize step ran at all.
        ``None`` only when the commit could not be materialized at all.
    """

    try:
        commit_root, changed_paths, deleted_paths = _materialize_pending_commit(
            store, head_sha
        )
    except _MaterializeError:
        store.revert_pending(base_sha=pre_tick_base_sha, pending=pre_tick_pending)
        return None

    try:
        result = apply_module.apply_commit(
            approved_sha=head_sha,
            commit_root=commit_root,
            changed_paths=changed_paths,
            store=store,
            deleted_paths=deleted_paths,
        )
    finally:
        shutil.rmtree(commit_root, ignore_errors=True)

    # M (senior review round 4): `apply_commit` records a restore
    # directory for every attempt unconditionally, before it knows
    # whether this attempt will actually write anything — so a retry of
    # the SAME still-failing partial apply against an unchanged remote
    # head (every eligible file failing the same way on every tick)
    # accumulates one new, empty restore directory per tick with nothing
    # in it to restore.
    #
    # `result.applied` is NOT by itself the right signal for "nothing was
    # written": a delete of a path that no longer exists (e.g. a file
    # this SAME apply already deleted on an earlier, still-partial tick)
    # is recorded as `applied` unconditionally by `apply_commit`
    # (`_apply_one_file`'s delete branch appends to `applied` whether or
    # not `live_target.exists()` was true) even though nothing on disk
    # actually changed and no backup was written for it. `_restore_dir_
    # has_backup` below checks the actual restore directory contents —
    # the one thing genuinely written to disk only when a real
    # backup/creation happened — rather than trusting `applied`'s own
    # bookkeeping. When the restore dir holds no real backup or
    # created-file record for ANY root, this attempt wrote nothing new,
    # and the restore dir `apply_commit` just recorded is discarded
    # immediately, before any other state mutation below. An attempt
    # that wrote at least one file keeps its restore dir untouched: it
    # is genuinely load-bearing for `routes.restore` even when OTHER
    # files in the same attempt failed.
    if result.apply_id and not _restore_dir_has_backup(result.apply_id):
        store.discard_restore_dir(apply_id=result.apply_id)

    if result.outcome == "applied":
        try:
            store.resolve_pending(head_sha)
        except ValueError:
            # #65 staleness race: pending moved between materialize and
            # this call. apply_commit already ran against head_sha and
            # fully applied it, but there is nothing left to resolve —
            # leave the newer pending record exactly as it is.
            pass
    elif result.outcome == "partial":
        store.record_partial_apply(sha=head_sha, not_applied=dict(result.not_applied))
        # set_pending's/record_poll_pending's bootstrap (requirements.md
        # 4.9's "no decision has ever been made" clause) sets
        # base_sha = head_sha unconditionally when this is the FIRST
        # pending record ever — so base_sha is never None here, even on
        # the very first tick. That bootstrap value is exactly the
        # signature this partial outcome now proves wrong (head_sha was
        # never actually fully applied): correct it back to the boundary
        # from BEFORE this tick's own range started. When base_sha
        # instead names an EARLIER, already-fully-applied commit (a real
        # prior boundary, from either a genuine earlier full apply or a
        # PRIOR partial tick's own already-corrected value), it is left
        # untouched.
        if store.base_sha == head_sha:
            if pre_tick_base_sha is not None:
                # A real prior boundary existed before this tick's range
                # started (whether this tick's range covered one commit
                # or several) — restore it directly. This is what keeps a
                # multi-commit partial range (base A, range picks up B
                # then C, C's break makes the whole range partial) at A,
                # never at the intermediate commit B: B was folded into
                # the SAME not-fully-applied range and was never itself
                # independently and fully applied.
                store.advance_base_sha(pre_tick_base_sha)
            else:
                # No prior boundary ever existed (this instance's very
                # first-ever pending record). This tick's own classify
                # pass only single-commit-logged head_sha itself (no
                # prior boundary to range from), so a single first-parent
                # hop back would land on whatever intermediate commit sits
                # between the range's true start and head_sha — the exact
                # same "lands on an intermediate commit" defect this fix
                # closes for the pre_tick_base_sha-known case above.
                # Walking all the way back to the oldest first-parent
                # ancestor of head_sha is what correctly lands on the
                # commit BEFORE this bootstrap tick's own range even
                # started, regardless of how many commits that range
                # bundled together.
                root_sha = _root_ancestor_sha(str(state.get_state_dir()), head_sha)
                if root_sha != head_sha:
                    store.advance_base_sha(root_sha)
    # "refused-sha-mismatch": apply_commit refused before touching
    # anything; no state mutation of ours is needed either.

    _record_last_apply(store, head_sha, result, changed_paths, poll_ignored_paths)
    return result.outcome


def _record_last_apply(
    store: state.StateStore,
    head_sha: str,
    result: Any,
    changed_paths: Dict[str, List[str]],
    poll_ignored_paths: List[str],
) -> None:
    """Record a dashboard-facing summary of the most recent automatic

    apply (requirements.md: "status() reports the last apply ... for the
    dashboard") — applied sha, not-applied paths with their real reasons,
    paused cron names, the command checked for each, and needs-credential
    entries. Rendered via the SAME ``_apply_result_to_dict`` the former
    approve route used, so the shape is identical to what
    ``routes._pending_changed_commands``'s sibling summaries already
    produce.

    ``poll_ignored_paths`` — `_classify_changed_paths`'s own ``ignored``
    result for this tick's range — is merged into the recorded
    ``ignored_paths`` alongside whatever `apply_commit` itself flagged
    (`_apply_result_to_dict`'s ``ignored_paths`` comes from
    ``ApplyResult.ignored_paths``, which only ever sees the already-
    classified paths `_materialize_pending_commit` split by root — a path
    the classifier matched against NO allowlist entry at all never
    reaches `apply_commit`'s own gate, so without this merge it would be
    silently absent from `last_apply` even though the classifier itself
    already knew about it).
    """
    payload = _apply_result_to_dict(result, changed_paths)
    payload["sha"] = head_sha
    merged_ignored: Dict[str, None] = {}
    for relpath in payload.get("ignored_paths", []):
        merged_ignored.setdefault(relpath, None)
    for relpath in poll_ignored_paths:
        merged_ignored.setdefault(relpath, None)
    payload["ignored_paths"] = list(merged_ignored)
    store.record_last_apply(payload)


def run() -> PollResult:
    """Run one poll-job tick: resolve the bundle repo's head, compare it

    against `state.last_seen_sha`, and report the outcome.

    Zero-argument, matching the `command` cron entrypoint shape
    (`python3 -m backend.poll`) — no agent/LLM context is required to call
    it (requirements.md 4.1).

    Returns:
        A :class:`PollResult` describing the outcome. On a resolution
        failure this function does not raise: it records no state change
        and returns ``outcome="ls-remote-failed"`` (head resolution itself
        failed) or ``outcome="fetch-failed"`` (a later step on an already-
        resolved changed head failed; senior-review round-2 H-new-1) so the
        cron wrapper can turn either into a non-zero process exit without
        this module owning the exit-code mechanics itself. There is no
        app-level pause: KiroCrew's cron runner auto-pauses the job after 5
        consecutive non-zero exits (see "Poll failures" above).
    """
    store = state.load_state()

    try:
        head_sha = _resolve_remote_head(str(state.get_state_dir()))
    except (subprocess.CalledProcessError, OSError) as exc:
        store.record_poll_failure(reason=str(exc))
        return PollResult(outcome="ls-remote-failed", reason=str(exc))

    if head_sha == store.last_seen_sha:
        store.clear_poll_failure()
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
        store.record_poll_failure(reason="empty head sha")
        return PollResult(outcome="ls-remote-failed", reason="empty head sha")

    # NOTE: no separate "already pending for this exact SHA" guard is
    # needed here for the NOTIFICATION cycle. `run()` always advances
    # `state.last_seen_sha` to `head_sha` in the SAME tick it calls
    # `state.record_poll_pending` below, so
    # `last_seen_sha == pending["sha"]` holds as an invariant from that
    # point on — a later tick reporting that SAME still-pending SHA is
    # already caught by the `head_sha == store.last_seen_sha` check above
    # and returns "unchanged" before reaching this point. But a
    # GENUINELY NEW head arriving while a commit is still pending DOES
    # reach this point again — that is the accumulation case
    # `state.record_poll_pending` decides internally (from the fresh
    # on-disk `pending` value, under its own lock — never from this
    # process's possibly-stale in-memory `store.pending`), not a case
    # this comment is claiming is excluded.

    # H-new-1 (senior review round 2): everything from here on is a git
    # call (the bundle-repo clone/fetch, the commit-metadata and
    # changed-path log calls) or pure computation (classification) — any
    # of it can raise (CalledProcessError from a git subprocess, OSError
    # from an unreadable/unwritable clone directory, or GitSafetyError from
    # `git_safety.git_argv`'s own attributes-pin check). Before round 4 fixed
    # H-A, `app.json`'s poll cron was `"silent": true`, so an uncaught
    # exception here would have left a persistently failing poll completely
    # invisible: no `PollResult`
    # reaches the caller, nothing is recorded, and the cron runner just
    # sees a bare stack trace with no failure record to alert on (push.py
    # has `record_push_failure` for its own equivalent failures; poll had
    # none). `state.last_seen_sha` is deliberately left UNCHANGED on this
    # path — unlike the successful case below — so the NEXT tick
    # re-resolves and re-attempts the SAME head rather than silently
    # skipping the commit that failed to fetch/classify.
    try:
        state_dir = str(state.get_state_dir())
        # requirements.md 4.9 / Kiro-Config-Bundles#65: the changed-path
        # range is computed from `base_sha` — the decision boundary — NOT
        # `last_seen_sha`, which advances every tick regardless of
        # pending state. Ranging from `last_seen_sha` here is exactly the
        # bug #65 reported: once a commit is pending, the NEXT tick's
        # range would start from that pending commit's own SHA and only
        # ever report the newest tick's own changed paths, silently
        # dropping the earlier pending commit's files once `set_pending`
        # (pre-fix) overwrote rather than merged the record. `base_sha` is
        # `None` only before this instance's very first poll tick ever —
        # `_fetch_commit_details`/`_changed_paths_for_range` already
        # handle `old_sha=None` as "no prior boundary, single-commit log".
        base_sha = store.base_sha
        author, subject, changed_paths = _fetch_commit_details(
            state_dir, head_sha, base_sha
        )
        classified_paths, ignored_paths, touched_classes = _classify_changed_paths(
            changed_paths
        )
    except (
        subprocess.CalledProcessError,
        OSError,
        TimeoutError,
        git_safety.GitSafetyError,
    ) as exc:
        store.record_poll_failure(reason=str(exc))
        return PollResult(outcome="fetch-failed", head_sha=head_sha, reason=str(exc))

    # Captured BEFORE `record_poll_pending` below mutates `base_sha`/
    # `pending` for this tick — `_apply_new_head` restores exactly these
    # values via `state.revert_pending` if the commit cannot be
    # materialized at all, so the tick leaves state byte-for-byte as it
    # was on entry rather than with a pending record for a commit nothing
    # was ever applied against.
    pre_tick_base_sha = store.base_sha
    pre_tick_pending = dict(store.pending) if store.pending is not None else None

    store.record_poll_pending(
        sha=head_sha,
        author=author,
        subject=subject,
        classified_paths=classified_paths,
        ignored_paths=ignored_paths,
        touched_classes=touched_classes,
    )

    # Auto-apply (requirements.md 4.4/4.9/4.14): under the operator ruling
    # the PR merge into main IS the approval gate — this tick now
    # materializes head_sha's tree and calls apply_commit itself, with NO
    # operator action anywhere in this path. `_apply_new_head` owns the
    # full materialize -> apply -> outcome-driven state mutation sequence
    # (a "changed" tick that finds nothing to apply — every path ignored —
    # still runs this and simply reports `outcome="applied"` with empty
    # `applied`/`not_applied`, since `apply_commit` handles that case
    # itself). A raised exception here (including one `_apply_new_head`
    # deliberately re-raises from `apply_commit` itself) propagates out of
    # `run()` unchanged — `_apply_new_head`'s own `finally` still removes
    # the materialized temp tree first.
    #
    # A `None` return means the commit could not be materialized at all
    # (the bundle-repo `git archive` step raised) — `_apply_new_head`
    # already reverted `base_sha`/`pending` to their pre-tick values, so
    # this tick reports an error outcome and must NOT advance
    # `last_seen_sha` or notify: the next tick re-resolves and retries the
    # SAME head, exactly like the `fetch-failed` degrade-and-retry path
    # above.
    # C-A (senior review round 4 H fix): capture the LIVE tree's hash
    # BEFORE this tick's own apply writes anything — the only way to tell
    # apart "the live tree already matched what was last pushed" (no
    # unpushed local edit sitting on top of it) from "a local edit exists
    # right now, before this tick touches anything". Computed via the
    # SAME `push.current_push_tree_hash()` pipeline the post-apply hash
    # below uses, so the two are directly comparable. `pre_apply_tree_empty`
    # additionally records whether EITHER tracked root held any file at
    # all before this tick — the bootstrap signal the guard below needs
    # to tell "nothing has ever been pushed AND nothing local exists
    # either, so there is no baseline this apply could be hiding a local
    # edit under" apart from "nothing has ever been pushed but a local
    # edit already exists" (both share `last_pushed_hash is None`, so
    # that field ALONE cannot distinguish them).
    from backend import collect as collect_module
    from backend import push as push_module

    pre_apply_tree_hash = push_module.current_push_tree_hash()
    pre_apply_tree_empty = not collect_module.collect()

    apply_outcome = _apply_new_head(
        store,
        head_sha,
        ignored_paths,
        pre_tick_base_sha=pre_tick_base_sha,
        pre_tick_pending=pre_tick_pending,
    )
    if apply_outcome is None:
        store.record_poll_failure(
            reason=f"could not materialize commit {head_sha}",
        )
        return PollResult(
            outcome="apply-error",
            head_sha=head_sha,
            reason=f"could not materialize commit {head_sha}",
        )

    # H-1 (senior review round 3): a PARTIAL apply must NOT advance
    # `last_seen_sha` past `head_sha`. Advancing it unconditionally (the
    # pre-fix shape) makes the NEXT tick's own
    # `head_sha == store.last_seen_sha` short-circuit — the very first
    # check in `run()` — report "unchanged" before `_apply_new_head` ever
    # runs again, permanently swallowing the retry requirements.md 4.14
    # requires for a still-failing partial apply. Only an `"applied"`
    # outcome (every eligible file succeeded) is durable enough to advance
    # the seen-SHA marker; `"partial"` and `"refused-sha-mismatch"` both
    # leave `last_seen_sha` exactly where it was so this SAME head is
    # re-resolved, re-classified, and retried on the next tick — the
    # pending record `_apply_new_head` already wrote (via
    # `record_partial_apply`, or left untouched on a staleness refusal)
    # is what carries the retry's own real per-path reasons forward, not
    # a second notification cycle (the `head_sha == last_seen_sha` guard
    # never applies here since `last_seen_sha` stays behind `head_sha`,
    # so `run()` re-enters this whole block on the next tick, exactly as
    # H-1 requires).
    #
    # H (senior review round 4): a `"partial"` outcome must still reach
    # the operator exactly once (requirements.md 4.3) — the pre-fix
    # early-return here skipped `notify_operator` entirely, so a partial
    # apply's not-applied paths and reasons were visible only through
    # `status()`'s `last_apply`, never pushed to the operator the way a
    # full apply's notification already is. Reuses the SAME
    # `notify_operator` seam; the not-applied paths/reasons come straight
    # from `store.last_apply` (`_apply_new_head`'s own
    # `_record_last_apply` call already wrote it for this tick, before
    # this point), so the message names WHAT failed and WHY, not merely
    # that something changed.
    if apply_outcome != "applied":
        store.clear_poll_failure()
        not_applied = (store.last_apply or {}).get("not_applied") or {}
        # Senior-review round 5: a stuck partial is retried every tick
        # (H-1), but the operator is told once. `pre_tick_pending` is the
        # pending record as it stood BEFORE this tick; when it names this
        # same head and carries the identical not-applied set, nothing has
        # changed since the last notification. A different set (a blocker
        # cleared, a new one appeared) is news and is announced again.
        previous = pre_tick_pending or {}
        already_announced = (
            previous.get("sha") == head_sha
            and previous.get("not_applied") == not_applied
        )
        if not already_announced:
            notify_operator(
                head_sha=head_sha,
                author=author,
                subject=subject,
                touched_classes=touched_classes,
                not_applied=not_applied,
            )
        return PollResult(outcome="changed", head_sha=head_sha)

    # C-A (senior review round 3; guarded further in round 4 H fix): a
    # FULLY-applied tick must record the just-applied tree's own push hash
    # as `last_pushed_hash`, in the SAME tick that records the apply — not
    # merely "eventually", since a push tick can run at any moment after
    # this one returns. The applied tree IS the live tree right now
    # (apply_commit already wrote every file to disk), so
    # `push.current_push_tree_hash()` — the exact
    # collect -> redact -> tokenize -> tree_hash pipeline `push.run()`'s
    # own no-op gate reads — computed here and now is the value that
    # gate must see, so the very next push tick recognizes this content as
    # already delivered and short-circuits to `outcome="no-op"` with zero
    # git calls, rather than re-cloning and trying to push the bundle
    # repo's own content straight back onto itself.
    #
    # H (senior review round 4): this update must happen ONLY IF the
    # PRE-apply live tree already matched `last_pushed_hash` — i.e. there
    # was no unpushed local edit sitting on top of it BEFORE this tick's
    # apply ran. The pre-fix shape updated `last_pushed_hash`
    # unconditionally to the POST-apply tree's hash, which folds any such
    # local edit into "already pushed" the moment an unrelated bundle
    # commit auto-applies on top of it — silently hiding that edit from
    # `drift()`/the next push tick forever, since nothing else will ever
    # produce a hash that differs from this one again. Comparing against
    # `pre_apply_tree_hash` (captured above, before `_apply_new_head` ran)
    # is what tells the two cases apart: when it already equalled
    # `last_pushed_hash`, the apply's own changes are the ONLY difference
    # between pre- and post-apply, so advancing the hash is exactly
    # correct; when it did not, a local edit predates this tick and must
    # keep showing as drift after the apply too, so `last_pushed_hash` is
    # left untouched — the local edit is never silently absorbed just
    # because an unrelated commit happened to auto-apply on top of it.
    #
    # `last_pushed_hash is None` (nothing has EVER been recorded as pushed
    # on this instance — the bootstrap case) alone is NOT sufficient to
    # trust this first apply: a local edit made before this instance's
    # very first poll tick also presents with `last_pushed_hash is None`
    # (nothing to compare against) yet must still be protected exactly
    # like the general case. `pre_apply_tree_empty` (captured above, from
    # `collect.collect()` before `_apply_new_head` ran) is the signal that
    # actually distinguishes them: only when NEITHER tracked root held any
    # file at all before this tick is there truly no baseline a local
    # edit could be hiding under, so advancing the hash is safe. A
    # non-empty pre-apply tree with `last_pushed_hash is None` falls
    # through to the `pre_apply_tree_hash == store.last_pushed_hash`
    # comparison below, which correctly evaluates to `False` (a real hash
    # never equals `None`) and leaves `last_pushed_hash` untouched.
    from backend import push as push_module

    if pre_apply_tree_empty or pre_apply_tree_hash == store.last_pushed_hash:
        store.record_push_success(
            tree_hash=push_module.current_push_tree_hash(),
            branch="",
            pr_url=None,
        )

    # H3 (senior review round 1, still open going into round 2): advance
    # `last_seen_sha` to `head_sha` BEFORE notifying, not after. If
    # `notify_operator` raises with the OLD ordering, `last_seen_sha` never
    # moves — so the NEXT tick re-resolves the SAME head, sees it as
    # "changed" all over again, and re-enters this whole block: a permanent
    # re-nag loop on every subsequent tick until the operator's own
    # notification channel is fixed (Requirement 4.4's notify-once
    # guarantee is invariant only while the ordering is
    # classify -> set_pending -> record_seen_sha -> notify; the pending
    # record and the seen-SHA marker must both be durably written before
    # the one step that can fail on something outside this app's control).
    # With THIS ordering, a notify failure costs at most ONE missed
    # notification for this commit — `state.pending` still holds the full
    # record, so the operator can discover it through the app's own UI
    # even if the push notification never arrived — rather than an
    # unbounded loop of duplicate notifications for the same commit
    # (Kiro-Config-Bundles#57's single-pending-slot seam has already
    # produced repeat defects of this exact "guard ordering" shape).
    store.record_seen_sha(head_sha)
    store.clear_poll_failure()
    notify_operator(
        head_sha=head_sha,
        author=author,
        subject=subject,
        touched_classes=touched_classes,
    )

    return PollResult(outcome="changed", head_sha=head_sha)


if __name__ == "__main__":
    result = run()
    raise SystemExit(0 if result.outcome not in _FAILURE_OUTCOMES else 1)
