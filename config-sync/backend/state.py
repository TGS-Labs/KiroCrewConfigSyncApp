"""Durable app state for config-sync (design.md `backend/state.py`).

One JSON document, under the app's OWN state directory — never inside
either tracked configuration root (``KIROCREW_HOME`` / ``KIRO_HOME``), so
the app's own bookkeeping can never be swept into a commit it makes.

Persists: ``last_pushed_hash``, ``last_push`` (time/branch/PR URL),
``last_seen_sha``, ``last_poll_failure`` (a persistently failing poll
tick's cause, mirroring ``last_push_failure``), ``base_sha`` (the head
commit as of the operator's LAST approve/decline decision, or this
instance's first-ever polled commit before any decision has ever been
made — the decision-boundary `poll.py` ranges its changed-path fetch from;
see `set_pending`/`accumulate_pending` below and Kiro-Config-Bundles#65),
``pending`` (sha/author/subject/classified paths, accumulated since
``base_sha``), ``pending_pr`` /
``pending_pr_failure`` (the PR-handoff pending/failure record — see
`backend/pr_handoff.py`), ``pending_pr_stale`` (a superseded
confirmation/failure report ignored because a newer push had already
overwritten ``pending_pr`` — see `confirm_pr_created`/
`record_pr_pending_failure`), a bounded ``history``, and ``restore_dirs``.

Writes are atomic: every write goes to a temp file in the same directory,
then ``os.replace()``s it into place, so a reader never observes a
truncated or partial document and a crash mid-write leaves the previous
good file untouched. A failed push never changes ``last_pushed_hash`` —
only :func:`StateStore.record_push_success` does.

## Cross-process locking (senior-review C1)

``poll.py``/``push.py`` (separate cron processes) and the backend server
each build their own ``StateStore`` against the SAME on-disk file, with
no in-process coordination between them. ``_atomic_write_json``'s
``os.replace`` only protects a READER from observing a half-written
file — it does nothing to stop two processes each doing
load -> mutate -> save from losing one side's update when the second
save is a full-payload write of a payload it loaded before the first
save landed.

Every read-modify-write mutation therefore goes through
:func:`StateStore._locked_rmw`, which holds an ``fcntl.flock`` on a
sibling ``.lock`` file (never the state file itself, so a lock never
blocks a plain read of ``state.json``) for the full span: acquire ->
re-read the payload fresh from disk -> apply the mutation -> write ->
release. Re-reading INSIDE the lock (rather than trusting whatever
``self._payload`` already held) is what makes this a real fix rather
than only serializing two stale writes — the mutation always applies on
top of the newest on-disk value, so a concurrent writer's fields (e.g.
``last_pushed_hash`` while this call only means to touch ``pending``)
are never reverted.

The wait for the lock is bounded (:data:`_LOCK_TIMEOUT_SECONDS`); on
timeout ``_locked_rmw`` raises :class:`TimeoutError` rather than
proceeding unlocked or silently skipping the write — a lock that can be
silently bypassed is not a lock.
"""

from __future__ import annotations

import contextlib
import copy
import errno
import fcntl
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, cast

HISTORY_LIMIT = 50

_STATE_DIR_ENV = "CONFIG_SYNC_STATE_DIR"
_STATE_FILE_NAME = "state.json"
_LOCK_FILE_NAME = "state.json.lock"

#: Bounded wait for the cross-process file lock. A real cron tick's own
#: read-modify-write span is a handful of milliseconds (one JSON parse,
#: one dict mutation, one atomic write), so a few seconds is ample
#: headroom for another process's in-flight write while still failing
#: loudly (never hanging the request/tick indefinitely) if the lock is
#: somehow never released (e.g. a crashed holder on a platform where the
#: OS did not reclaim the lock promptly).
_LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_POLL_INTERVAL_SECONDS = 0.05

_DEFAULT_FIELDS: dict[str, Any] = {
    "last_pushed_hash": None,
    "last_push": None,
    "last_push_failure": None,
    "last_seen_sha": None,
    "last_poll_failure": None,
    "base_sha": None,
    "pending": None,
    "pending_pr": None,
    "pending_pr_failure": None,
    "pending_pr_stale": None,
    "history": [],
    "restore_dirs": {},
}


_STATE_DIR_NAME = (".config-sync", "state")


def get_state_dir() -> Path:
    """Return the app's own state directory, creating it if needed.

    Honors ``CONFIG_SYNC_STATE_DIR`` as an explicit override for operators
    and tests. Otherwise the state directory is a fixed, KiroCrew-owned
    location named ``~/.config-sync/state`` — a dedicated top-level
    dot-directory of its own, never nested under ``.kiro`` at all.

    This is outside both tracked configuration roots (``KIROCREW_HOME`` /
    ``KIRO_HOME``) *by construction/naming*, not by walking either root's
    ancestry: no allowlist entry (``backend/allowlist.py``) can ever match a
    path under ``config-sync/state/**`` in the first place, so isolation
    from the tracked roots was never actually at risk regardless of where
    they resolve to. A prior version of this function derived the state
    directory from ``KIROCREW_HOME``'s and ``KIRO_HOME``'s resolved parents,
    walking up until clear of both — that bought a property that was never
    threatened while risking landing on an unwritable or arbitrary anchor
    (e.g. a shared root's parent). Naming a fixed, independent location
    avoids both the walk-up complexity and that risk.
    """
    override = os.environ.get(_STATE_DIR_ENV)
    if override:
        state_dir = Path(override)
    else:
        state_dir = Path.home().joinpath(*_STATE_DIR_NAME)
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def get_state_path() -> Path:
    """Return the path to the single JSON state document."""
    return get_state_dir() / _STATE_FILE_NAME


def get_lock_path() -> Path:
    """Return the path to the cross-process lock file, sibling to the

    state document itself. A SEPARATE file, never ``state.json`` — the
    lock must be acquirable (and pollable) without opening the document
    in a mode that would disturb `_atomic_write_json`'s own
    temp-file-then-``os.replace`` sequence, and a lock held on a path
    that a writer then ``os.replace``s out from under it would silently
    stop protecting anything.
    """
    return get_state_dir() / _LOCK_FILE_NAME


@contextlib.contextmanager
def _file_lock(path: Path, timeout: float = _LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    """Hold an exclusive ``fcntl.flock`` on ``path`` for the duration of

    the ``with`` block, creating ``path`` if it does not yet exist.

    Blocks up to ``timeout`` seconds waiting for the lock (polling with
    a non-blocking `flock` attempt rather than a blocking one, so a
    genuinely stuck holder cannot hang the caller past the bound) and
    raises :class:`TimeoutError` if it is never acquired — never
    proceeds unlocked and never silently skips the caller's write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"timed out after {timeout}s waiting for the "
                        f"config-sync state lock at {path}"
                    ) from exc
                time.sleep(_LOCK_POLL_INTERVAL_SECONDS)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` to ``path`` atomically.

    Never opens the real path in a truncating write mode: the document is
    written to a temp file in the same directory first, flushed and fsync'd,
    then moved into place with ``os.replace`` (atomic on POSIX and Windows).
    If ``os.replace`` raises, the temp file is removed and the previous
    on-disk document is left exactly as it was.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise


def _load_payload(path: Path) -> dict[str, Any]:
    """Read the on-disk document, falling back to defaults when absent.

    A file that fails to parse as JSON is treated as corrupt and reset to a
    fresh, well-formed default document rather than silently surfacing a
    half-parsed value — the caller never sees truncated state.
    """
    if not path.exists():
        return _fresh_defaults()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (json.JSONDecodeError, ValueError, OSError):
        return _fresh_defaults()
    payload = _fresh_defaults()
    if isinstance(raw, dict):
        for key in _DEFAULT_FIELDS:
            if key in raw:
                payload[key] = raw[key]
    return payload


def _fresh_defaults() -> dict[str, Any]:
    """A fresh copy of the default document.

    Uses ``copy.deepcopy`` rather than ``dict(_DEFAULT_FIELDS)``: a shallow
    copy would alias the mutable ``history`` list and ``restore_dirs`` dict
    across every "fresh" document, so mutating one test's/instance's state
    would silently mutate every other one sharing the same process.
    """
    return copy.deepcopy(_DEFAULT_FIELDS)


@dataclass
class StateStore:
    """In-memory view of the state document, persisted on every mutation."""

    _path: Path
    _payload: dict[str, Any] = field(default_factory=_fresh_defaults)

    # -- cross-process read-modify-write -----------------------------------

    def _locked_rmw(self, mutate: Callable[[dict[str, Any]], None]) -> None:
        """Run ``mutate(payload)`` under the cross-process file lock,

        against a payload freshly loaded from disk — never a merge with
        this instance's own possibly-stale in-memory view.

        A per-field "adopt the disk value only if it differs from the
        field's default" merge (the previous shape of this method) is
        indistinguishable, from the on-disk value alone, between "no
        concurrent process has touched this field" and "a concurrent
        process just cleared this field back to its default" — both read
        as "disk equals default". The second case is exactly what a
        concurrent `resolve_pending`/`confirm_pr_created`/etc. produces,
        and the old merge silently kept THIS instance's stale, already-
        superseded non-default value and wrote it straight back on the
        next unrelated mutation, resurrecting a field another process had
        deliberately cleared (senior-review round-3, Kiro-Config-Bundles).

        The only correct source of truth for "what does this mutation
        apply on top of" is the newest on-disk payload, full stop — not a
        merge with anything this instance loaded earlier. ``mutate``
        receives that fresh-from-disk payload and mutates it in-place;
        every field lookup a mutation needs (e.g. `current["pending"]` for
        `accumulate_pending`'s merge) reads from that same freshly-loaded
        dict, so a concurrent write is exactly what the mutation itself
        sees.

        This does mean a value set via `last_pushed_hash`'s test-only
        setter (in-memory only, never persisted) does NOT survive a
        `_locked_rmw` call made afterward — correctly so: an unpersisted
        in-memory value is not a real concurrent-write case this method
        needs to protect, and a caller wanting it to persist should call
        a real mutation method instead of the raw setter.

        After a successful write, ``self._payload`` is replaced with the
        just-written payload so this instance's own subsequent reads
        (`self.pending`, etc.) reflect what is now on disk. If the write
        itself fails, ``self._payload`` is left unchanged (the in-memory
        view never runs ahead of what is actually on disk) and the
        exception propagates.
        """
        lock_path = self._path.parent / _LOCK_FILE_NAME
        with _file_lock(lock_path):
            fresh = _load_payload(self._path)
            mutate(fresh)
            previous = self._payload
            self._payload = fresh
            try:
                self._save()
            except Exception:
                self._payload = previous
                raise

    # -- read-only views -------------------------------------------------

    @property
    def last_pushed_hash(self) -> str | None:
        return cast("str | None", self._payload["last_pushed_hash"])

    @last_pushed_hash.setter
    def last_pushed_hash(self, value: str | None) -> None:
        """Allow direct assignment for test setup (e.g. seeding a store to

        simulate an already-pushed hash before exercising the push-job
        hash-gate). Production code should use `record_push_success`,
        which is the only path that persists the change to disk and pairs
        it with a `last_push` record; this setter mutates the in-memory
        value only and does not call `_save()`.
        """
        self._payload["last_pushed_hash"] = value

    @property
    def last_push(self) -> dict[str, Any] | None:
        return cast("dict[str, Any] | None", self._payload["last_push"])

    @property
    def last_push_failure(self) -> dict[str, Any] | None:
        return cast("dict[str, Any] | None", self._payload["last_push_failure"])

    @property
    def last_seen_sha(self) -> str | None:
        return cast("str | None", self._payload["last_seen_sha"])

    @property
    def last_poll_failure(self) -> dict[str, Any] | None:
        return cast("dict[str, Any] | None", self._payload["last_poll_failure"])

    @property
    def base_sha(self) -> str | None:
        """The head commit as of the operator's LAST approve/decline

        decision, or this instance's first-ever polled commit before any
        decision has ever been made (requirements.md 4.9). This is the
        decision-boundary `poll.py` computes its changed-path range from
        (``base_sha..head``) — distinct from `last_seen_sha`, which
        advances on every tick regardless of pending state, and from
        `pending`'s own ``sha``, which always reflects the newest head
        seen. ``None`` only ever before this instance's first poll tick.
        """
        return cast("str | None", self._payload["base_sha"])

    @property
    def pending(self) -> dict[str, Any] | None:
        return cast("dict[str, Any] | None", self._payload["pending"])

    @property
    def pending_pr(self) -> dict[str, Any] | None:
        return cast("dict[str, Any] | None", self._payload["pending_pr"])

    @property
    def pending_pr_failure(self) -> dict[str, Any] | None:
        return cast("dict[str, Any] | None", self._payload["pending_pr_failure"])

    @property
    def pending_pr_stale(self) -> dict[str, Any] | None:
        """The most recent superseded confirmation/failure report, if any.

        Set by `confirm_pr_created` or `record_pr_pending_failure` when the
        caller's ``tree_hash``/``branch`` no longer matches the CURRENT
        `pending_pr` — i.e. a newer push already overwrote it before this
        report arrived. Never cleared automatically; a fresh occurrence
        overwrites it, same as `pending_pr_failure`.
        """
        return cast("dict[str, Any] | None", self._payload["pending_pr_stale"])

    @property
    def history(self) -> list[dict[str, Any]]:
        return cast("list[dict[str, Any]]", self._payload["history"])

    @property
    def restore_dirs(self) -> dict[str, str]:
        return cast("dict[str, str]", self._payload["restore_dirs"])

    # -- mutations --------------------------------------------------------

    def record_branch_pushed(self, *, tree_hash: str, branch: str) -> None:
        """Record a successfully pushed branch WITHOUT advancing

        `last_pushed_hash`. This is the bare-push case: a branch reached
        the remote but no PR has been created (or confirmed) yet, so the
        change is not "delivered" per requirements.md 2.6 — only
        `record_push_success` (via `confirm_pr_created`) advances the hash.
        Shares `record_push_success`'s `last_push`/history recording shape
        so `last_push` always reflects the most recent push attempt,
        confirmed or not.
        """
        entry = {
            "tree_hash": tree_hash,
            "branch": branch,
            "pr_url": None,
            "time": _now_iso(),
        }

        def _mutate(payload: dict[str, Any]) -> None:
            payload["last_push"] = dict(entry)
            self._append_history(payload, dict(entry))

        self._locked_rmw(_mutate)

    def record_push_success(
        self, *, tree_hash: str, branch: str, pr_url: str | None
    ) -> None:
        """Record a successful, PR-confirmed push. Only this (and its

        caller `confirm_pr_created`) advances `last_pushed_hash` —
        `record_branch_pushed` is the bare-push counterpart that does not.

        Clears `last_push_failure` (senior-review M4): a push that failed
        once and later succeeds must stop reporting the earlier failure
        forever, mirroring `clear_poll_failure`'s already-correct pairing
        for the poll side — otherwise `GET status` has no way to show
        "recovered" versus "still failing".
        """
        entry = {
            "tree_hash": tree_hash,
            "branch": branch,
            "pr_url": pr_url,
            "time": _now_iso(),
        }

        def _mutate(payload: dict[str, Any]) -> None:
            payload["last_pushed_hash"] = tree_hash
            payload["last_push"] = dict(entry)
            payload["last_push_failure"] = None
            self._append_history(payload, dict(entry))

        self._locked_rmw(_mutate)

    def record_push_failure(self, *, reason: str) -> None:
        """Record a failed push. Never changes `last_pushed_hash`."""
        entry = {"reason": reason, "time": _now_iso()}

        def _mutate(payload: dict[str, Any]) -> None:
            payload["last_push_failure"] = dict(entry)

        self._locked_rmw(_mutate)

    def record_poll_failure(self, *, reason: str) -> None:
        """Record a failed poll tick (`outcome="fetch-failed"`), mirroring

        `record_push_failure`'s shape for the poll job's own equivalent
        failure path (senior-review round-3 M2/L1). Even though `app.json`'s
        poll cron is `"silent": false` since round 4 (so a failing tick's
        stdout does surface to the operator), a persistently failing poll
        (the bundle-repo clone/fetch, the commit-metadata/changed-path git
        calls, or classification itself all raising) still needs to be
        visible in the app's OWN state — not only in the cron runner's
        exit-history — the same "invisible failure" gap `_FAILURE_OUTCOMES`/
        `fetch-failed`
        already exists to surface at the `PollResult` level, now also
        surfaced at the persisted-state level so the app's own UI can show
        it. Never changes `last_seen_sha` (poll's own `run()` already
        leaves that untouched on this path so the next tick retries the
        same head).
        """
        entry = {"reason": reason, "time": _now_iso()}

        def _mutate(payload: dict[str, Any]) -> None:
            payload["last_poll_failure"] = dict(entry)

        self._locked_rmw(_mutate)

    def clear_poll_failure(self) -> None:
        """Clear `last_poll_failure` after a successful poll tick

        (senior-review round-4 M2). Without this, a stale failure record
        from an earlier failing tick would keep showing in the app's own
        UI indefinitely even after the poll has been succeeding for a
        while — the UI has no way to tell "still failing" from "recovered
        an hour ago" apart from this being cleared on the next success.
        Called from `run()` on BOTH success paths: the unchanged-head
        outcome and the changed-head outcome (right where
        `record_seen_sha` advances), so any tick that resolves the head
        cleanly counts as a recovery regardless of whether it also found a
        new commit.
        """

        def _mutate(payload: dict[str, Any]) -> None:
            payload["last_poll_failure"] = None

        self._locked_rmw(_mutate)

    def record_pr_pending(
        self, *, branch: str, tree_hash: str, payload: dict[str, Any]
    ) -> None:
        """Record a pending-PR state entry after a successful branch push.

        Called BEFORE PR creation is confirmed (see `confirm_pr_created`)
        — never changes `last_pushed_hash`, matching requirements.md 2.6's
        "only after the push and PR creation both succeed" rule.
        """
        entry = {
            "branch": branch,
            "tree_hash": tree_hash,
            "payload": dict(payload),
            "time": _now_iso(),
        }

        def _mutate(fresh: dict[str, Any]) -> None:
            fresh["pending_pr"] = dict(entry)
            self._append_history(fresh, dict(entry))

        self._locked_rmw(_mutate)

    def record_pr_pending_failure(
        self,
        *,
        reason: str,
        tree_hash: str | None = None,
        branch: str | None = None,
    ) -> None:
        """Record a PR-handoff failure with its cause.

        Covers a payload-build failure, a notify failure, or a reported
        failed PR creation (requirements.md 2.7). Never changes
        `last_pushed_hash`.

        `tree_hash`/`branch` identify which attempt this failure is about.
        Three cases, checked against the CURRENT `pending_pr`:

        1. No `tree_hash` given: unconditional legacy behaviour — records
           `pending_pr_failure` and clears `pending_pr`. No caller uses
           this today; kept only so an existing caller with no attempt
           identity to give still degrades safely.
        2. `tree_hash` given and it MATCHES the current `pending_pr` (or
           `pending_pr` is empty — the payload-build-failure case, which
           runs before `record_pr_pending` has recorded anything for this
           attempt): this failure is about the CURRENT/newest attempt.
           Records `pending_pr_failure` and clears `pending_pr` (H-NEW-1:
           without this the next tick's hash-gate sees the same
           `pending_pr.tree_hash` and returns `awaiting-pr-confirmation`
           forever).
        3. `tree_hash` given but `pending_pr` names a DIFFERENT attempt:
           this report is either (a) a stale out-of-band
           `report_pr_creation_failed` call for an attempt a NEWER push
           already superseded, or (b) an in-tick failure for an attempt
           whose OWN `pending_pr` entry hasn't been written yet while an
           older, still-genuinely-pending different attempt occupies the
           slot. Either way the safe action is identical: never touch the
           current `pending_pr` (it may be the real, still-pending
           newer/older attempt), and record this failure to
           `pending_pr_stale` for observability instead of silently
           dropping it (H-NEW-2).
        """

        def _mutate(fresh: dict[str, Any]) -> None:
            current = fresh["pending_pr"]

            if (
                tree_hash is not None
                and current is not None
                and (
                    current.get("tree_hash") != tree_hash
                    or current.get("branch") != branch
                )
            ):
                fresh["pending_pr_stale"] = {
                    "reason": reason,
                    "tree_hash": tree_hash,
                    "branch": branch,
                    "time": _now_iso(),
                }
                return

            fresh["pending_pr_failure"] = {
                "reason": reason,
                "time": _now_iso(),
            }
            fresh["pending_pr"] = None

        self._locked_rmw(_mutate)

    def confirm_pr_created(self, *, tree_hash: str, branch: str, pr_url: str) -> None:
        """Confirm a pending PR was actually created — the only call that
        advances `last_pushed_hash` for the PR-handoff flow.

        Called out-of-band, from a KiroCrew agent context via the
        `complete-pr-handoff` skill, once Buildo has actually opened the
        PR. Applies the same field updates as `record_push_success`
        (inlined here rather than calling it, so the whole read-check-
        write span — including the staleness check against
        `pending_pr` below — runs under ONE lock acquisition, not two)
        since that is the only
        existing path that persists `last_pushed_hash` and pairs it with a
        `last_push` record (requirements.md 2.6).

        Validated against the CURRENT `pending_pr` before mutating anything
        (H-NEW-2): if `tree_hash`/`branch` no longer match `pending_pr` —
        because a newer push already overwrote it before this confirmation
        arrived — this call does NOT advance `last_pushed_hash` and does
        NOT clear the current `pending_pr` (which names the newer, still-
        pending change). The stale confirmation is instead recorded to
        `pending_pr_stale` so it is observable rather than silently
        swallowed.
        """

        def _mutate(fresh: dict[str, Any]) -> None:
            current = fresh["pending_pr"]
            if (
                current is None
                or current.get("tree_hash") != tree_hash
                or current.get("branch") != branch
            ):
                fresh["pending_pr_stale"] = {
                    "reason": "confirm_pr_created for a superseded tree_hash",
                    "tree_hash": tree_hash,
                    "branch": branch,
                    "pr_url": pr_url,
                    "time": _now_iso(),
                }
                return

            entry = {
                "tree_hash": tree_hash,
                "branch": branch,
                "pr_url": pr_url,
                "time": _now_iso(),
            }
            fresh["last_pushed_hash"] = tree_hash
            fresh["last_push"] = dict(entry)
            fresh["last_push_failure"] = None
            self._append_history(fresh, dict(entry))
            fresh["pending_pr"] = None

        self._locked_rmw(_mutate)

    def record_seen_sha(self, sha: str) -> None:
        """Record the latest polled SHA, overwriting any previous value."""

        def _mutate(fresh: dict[str, Any]) -> None:
            fresh["last_seen_sha"] = sha

        self._locked_rmw(_mutate)

    def set_pending(
        self,
        *,
        sha: str,
        author: str,
        subject: str,
        classified_paths: dict[str, str],
        ignored_paths: list[str] | None = None,
        touched_classes: list[str] | None = None,
    ) -> None:
        """Record a FRESH pending commit awaiting approval or decline —

        i.e. there is no existing pending record to accumulate into
        (`accumulate_pending` below is the merge counterpart for that
        case). Requirements.md 4.9: when a new pending record starts,
        ``base_sha`` is set to ``sha`` (the current head) — the range
        boundary for THIS commit's own accumulation is "nothing yet", so
        the boundary starts exactly at the commit just classified. This is
        also how ``base_sha`` gets its very first value ever, on an
        instance's first-ever pending commit (before any operator decision
        has ever been made — requirements.md 4.9's other clause).

        ``ignored_paths`` and ``touched_classes`` (senior-review round-2
        M2) carry `classify.classify_paths`'s ``Result.ignored`` /
        ``Result.touched_classes`` (already reduced to plain strings by
        the caller) into the pending record, so a later consumer (e.g.
        Deployment 4's apply/approval UI) can see what the commit touched
        and what it deliberately skipped WITHOUT re-fetching and
        re-classifying the commit itself. Both are additive/optional —
        default to an empty list — so an existing caller that only ever
        passed the original four keyword arguments is unaffected.
        """

        def _mutate(fresh: dict[str, Any]) -> None:
            fresh["base_sha"] = sha
            fresh["pending"] = {
                "sha": sha,
                "author": author,
                "subject": subject,
                "classified_paths": dict(classified_paths),
                "ignored_paths": list(ignored_paths or []),
                "touched_classes": list(touched_classes or []),
            }

        self._locked_rmw(_mutate)

    def accumulate_pending(
        self,
        *,
        sha: str,
        author: str,
        subject: str,
        classified_paths: dict[str, str],
        ignored_paths: list[str] | None = None,
        touched_classes: list[str] | None = None,
    ) -> None:
        """Merge a new poll tick's classified paths INTO the existing

        pending record, rather than replacing it — the fix for
        Kiro-Config-Bundles#65 (requirements.md 4.9): a poll tick that
        finds a new head while an earlier commit is still pending must
        never drop that earlier commit's still-unapplied changed files.

        Call only when `pending` already holds a record for the SAME
        decision boundary (i.e. `base_sha` is unchanged since that record
        was started) — `poll.py` is responsible for choosing between this
        and `set_pending` based on whether a pending record already
        exists. Raises if there is nothing to accumulate into, since that
        is a caller bug (should have called `set_pending` instead), not a
        recoverable state.

        Merge semantics, keyed by path (requirements.md 4.9: "keyed by
        path so a path changed in both ranges reflects the latest
        classification"):

        - ``classified_paths``: unioned; a path present in both the old
          and new mapping takes the NEW (latest) classification, since the
          new tick's range is the more recent reclassification of that
          path's propagation class.
        - ``ignored_paths``: unioned, order-preserving (old paths first,
          then any new path not already present) with duplicates
          collapsed.
        - ``touched_classes``: unioned, sorted, since it is a set of
          distinct propagation classes represented across the whole
          accumulated range, not a per-tick value.

        ``pending.sha`` moves to the new ``sha`` (the newest head seen) —
        this is what requirements.md 4.9 means by "the pending record's
        `sha` field SHALL always reflect the newest head seen". ``author``/
        ``subject`` also move to the new commit's, since those describe
        the LATEST commit in the accumulated range, matching what
        `poll.py`'s notification for this tick reports. ``base_sha`` is
        NOT touched here — it only ever changes via `advance_base_sha`.
        """

        def _mutate(fresh: dict[str, Any]) -> None:
            current = fresh["pending"]
            if current is None:
                raise ValueError(
                    "accumulate_pending called with no existing pending "
                    "record; use set_pending to start a new one"
                )

            merged_classified: dict[str, str] = dict(current["classified_paths"])
            merged_classified.update(classified_paths)

            merged_ignored: list[str] = list(current.get("ignored_paths") or [])
            for relpath in ignored_paths or []:
                if relpath not in merged_ignored:
                    merged_ignored.append(relpath)
            # A path newly classified this tick must not remain in the
            # accumulated ignored list even if an earlier tick ignored it.
            merged_ignored = [
                relpath
                for relpath in merged_ignored
                if relpath not in merged_classified
            ]

            merged_touched: set[str] = set(current.get("touched_classes") or [])
            merged_touched.update(touched_classes or [])

            fresh["pending"] = {
                "sha": sha,
                "author": author,
                "subject": subject,
                "classified_paths": merged_classified,
                "ignored_paths": merged_ignored,
                "touched_classes": sorted(merged_touched),
            }

        self._locked_rmw(_mutate)

    def record_poll_pending(
        self,
        *,
        sha: str,
        author: str,
        subject: str,
        classified_paths: dict[str, str],
        ignored_paths: list[str] | None = None,
        touched_classes: list[str] | None = None,
    ) -> None:
        """Decide between `set_pending` (no existing pending record) and

        accumulating onto one (the `accumulate_pending` merge semantics),
        in ONE lock acquisition, choosing from the FRESH on-disk `pending`
        value rather than a value the caller read outside the lock.

        This is `poll.py`'s single entry point for recording a
        changed-head tick's classified paths — it replaces the caller-side
        pattern of reading `store.pending` (a snapshot that can already be
        stale by the time the lock is taken) and then calling
        `set_pending` or `accumulate_pending` accordingly. Deciding outside
        the lock can choose wrong: e.g. this instance's in-memory `pending`
        still shows a record another process resolved moments ago, so the
        caller would wrongly call `accumulate_pending` (which raises,
        since there is nothing to accumulate onto) instead of
        `set_pending` for what is actually an unrelated new commit.

        Merge semantics when accumulating exactly match `accumulate_pending`
        (paths keyed union with the newest classification winning,
        ignored-paths union minus anything now classified, touched-classes
        union) — duplicated here rather than delegated to it because the
        decision and the write must happen against the SAME fresh payload
        under the SAME lock acquisition; calling out to `accumulate_pending`
        would re-enter `_locked_rmw` a second time against a payload that
        could have changed again in between.
        """

        def _mutate(fresh: dict[str, Any]) -> None:
            current = fresh["pending"]
            if current is None:
                fresh["base_sha"] = sha
                fresh["pending"] = {
                    "sha": sha,
                    "author": author,
                    "subject": subject,
                    "classified_paths": dict(classified_paths),
                    "ignored_paths": list(ignored_paths or []),
                    "touched_classes": list(touched_classes or []),
                }
                return

            merged_classified: dict[str, str] = dict(current["classified_paths"])
            merged_classified.update(classified_paths)

            merged_ignored: list[str] = list(current.get("ignored_paths") or [])
            for relpath in ignored_paths or []:
                if relpath not in merged_ignored:
                    merged_ignored.append(relpath)
            merged_ignored = [
                relpath
                for relpath in merged_ignored
                if relpath not in merged_classified
            ]

            merged_touched: set[str] = set(current.get("touched_classes") or [])
            merged_touched.update(touched_classes or [])

            fresh["pending"] = {
                "sha": sha,
                "author": author,
                "subject": subject,
                "classified_paths": merged_classified,
                "ignored_paths": merged_ignored,
                "touched_classes": sorted(merged_touched),
            }

        self._locked_rmw(_mutate)

    def advance_base_sha(self, sha: str) -> None:
        """Advance ``base_sha`` to ``sha`` — called ONLY when the operator

        approves or declines a pending commit (requirements.md 4.9:
        "`base_sha` SHALL NOT advance while a commit is pending; it SHALL
        advance only when the operator approves or declines, to the SHA
        that was just approved or declined").

        TODO(Deployment 4): no caller wires this yet. `apply.py`'s
        approve/decline routes do not exist in this codebase — this
        method exists now so `state.py`'s additive shape is complete and
        tested (accumulation correctly does NOT move `base_sha`), but
        Deployment 4 owns calling it from the approve/decline handlers,
        passing the SHA that was just approved or declined, once those
        routes are built.
        """

        def _mutate(fresh: dict[str, Any]) -> None:
            fresh["base_sha"] = sha

        self._locked_rmw(_mutate)

    def clear_pending(self) -> None:
        """Clear the pending record without changing anything else.

        Does NOT touch ``base_sha`` — clearing `pending` and advancing
        `base_sha` are deliberately separate operations (see
        `advance_base_sha`); a caller that means "operator decided"
        must call both.
        """

        def _mutate(fresh: dict[str, Any]) -> None:
            fresh["pending"] = None

        self._locked_rmw(_mutate)

    def resolve_pending(self, sha: str) -> None:
        """Resolve the pending record against an operator decision — the

        single call ``backend/routes.py``'s approve AND decline handlers
        both make (tasks.md 6.1; design.md's routes section: "Approve and
        decline call a single ``resolve_pending()`` in ``state.py`` that
        advances ``base_sha`` to the decided SHA and clears the pending
        record together (never one without the other)").

        Advances ``base_sha`` to ``sha`` and clears ``pending`` in ONE
        persisted write — never as two separate ``advance_base_sha()`` /
        ``clear_pending()`` calls, which would reopen a window where a
        crash between the two leaves ``base_sha`` advanced but ``pending``
        still present (or vice versa).

        Args:
            sha: the SHA the caller (approve or decline) asserts the
                operator actually decided on.

        Raises:
            ValueError: when there is no pending record at all, or when
                ``sha`` does not match ``pending["sha"]`` — the
                Kiro-Config-Bundles#65 staleness case: a poll tick
                accumulated a newer commit into ``pending`` after the
                operator's approve/decline UI was rendered against an
                older SHA. Neither ``base_sha`` nor ``pending`` is
                changed on refusal, so a stale decision never applies (or
                clears) a commit the operator never actually reviewed.
        """

        def _mutate(fresh: dict[str, Any]) -> None:
            pending = fresh["pending"]
            if pending is None or pending.get("sha") != sha:
                raise ValueError(
                    f"no pending commit matching sha {sha!r} to resolve "
                    "(nothing pending, or a newer commit has since "
                    "accumulated)"
                )
            fresh["base_sha"] = sha
            fresh["pending"] = None

        self._locked_rmw(_mutate)

    def record_partial_apply(self, *, sha: str, not_applied: dict[str, str]) -> None:
        """Record a PARTIAL apply's not-applied paths/reasons onto the

        pending record WITHOUT resolving it (H4, ratified): a partial
        apply must leave ``pending``/``base_sha`` untouched — the operator
        still needs to see this commit as pending — while the not-applied
        paths and their reasons become visible via ``status()``.

        Args:
            sha: the sha the caller (``approve``) just ran ``apply_commit``
                against. Applied only when it still matches
                ``pending["sha"]`` — the same staleness guard
                ``resolve_pending`` enforces, so a partial-apply record
                for a sha that is no longer the pending one (a poll tick
                accumulated a newer commit mid-apply) is silently a
                no-op rather than attaching stale data to a different
                commit's pending record.
            not_applied: ``{relpath: reason}`` for every path that failed
                to apply.

        Raises:
            ValueError: when there is no pending record at all, mirroring
                ``resolve_pending``'s own refusal shape — a caller with
                nothing pending to annotate is a caller bug.
        """

        def _mutate(fresh: dict[str, Any]) -> None:
            pending = fresh["pending"]
            if pending is None:
                raise ValueError(
                    "record_partial_apply called with no existing pending "
                    "record to annotate"
                )
            if pending.get("sha") != sha:
                # Stale: a newer commit has since accumulated into
                # pending. Do not attach this apply's not-applied paths
                # to a pending record that no longer names the commit
                # this apply actually ran against.
                return
            updated = dict(pending)
            updated["not_applied"] = dict(not_applied)
            fresh["pending"] = updated
            # H4: "base_sha not advanced" on a partial apply. base_sha
            # only ever moves via set_pending (bootstrapping the very
            # first-ever pending record to that commit's own sha) or a
            # genuine operator decision (resolve_pending/
            # advance_base_sha) -- accumulate_pending never touches it.
            # When base_sha still equals THIS pending commit's own sha,
            # no operator decision has actually happened yet: the only
            # thing that ever set it was set_pending's bootstrap side
            # effect for this exact cycle, which a partial apply must
            # not leave standing as if a decision boundary had moved.
            # Revert it to "no decision yet" (None) in that case; if
            # base_sha instead names an EARLIER, already-decided commit
            # (a later accumulate_pending moved pending.sha forward
            # while base_sha stayed at that prior decision), it is left
            # untouched -- there is a real decision boundary there to
            # preserve.
            if fresh.get("base_sha") == sha:
                fresh["base_sha"] = None

        self._locked_rmw(_mutate)

    def record_restore_dir(self, *, apply_id: str, restore_dir: str) -> None:
        """Record a restore-directory mapping for a previous apply."""

        def _mutate(fresh: dict[str, Any]) -> None:
            fresh["restore_dirs"][apply_id] = restore_dir

        self._locked_rmw(_mutate)

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _append_history(payload: dict[str, Any], entry: dict[str, Any]) -> None:
        history = payload["history"]
        history.append(entry)
        if len(history) > HISTORY_LIMIT:
            del history[: len(history) - HISTORY_LIMIT]

    def _save(self) -> None:
        _atomic_write_json(self._path, self._payload)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load_state() -> StateStore:
    """Load the state document from disk, creating a default one if absent."""
    path = get_state_path()
    payload = _load_payload(path)
    return StateStore(_path=path, _payload=payload)
