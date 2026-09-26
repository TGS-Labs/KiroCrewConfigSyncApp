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
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

HISTORY_LIMIT = 50

_STATE_DIR_ENV = "CONFIG_SYNC_STATE_DIR"
_STATE_FILE_NAME = "state.json"

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
        self._payload["last_push"] = dict(entry)
        self._append_history(entry)
        self._save()

    def record_push_success(
        self, *, tree_hash: str, branch: str, pr_url: str | None
    ) -> None:
        """Record a successful, PR-confirmed push. Only this (and its

        caller `confirm_pr_created`) advances `last_pushed_hash` —
        `record_branch_pushed` is the bare-push counterpart that does not.
        """
        entry = {
            "tree_hash": tree_hash,
            "branch": branch,
            "pr_url": pr_url,
            "time": _now_iso(),
        }
        self._payload["last_pushed_hash"] = tree_hash
        self._payload["last_push"] = dict(entry)
        self._append_history(entry)
        self._save()

    def record_push_failure(self, *, reason: str) -> None:
        """Record a failed push. Never changes `last_pushed_hash`."""
        self._payload["last_push_failure"] = {
            "reason": reason,
            "time": _now_iso(),
        }
        self._save()

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
        self._payload["last_poll_failure"] = {
            "reason": reason,
            "time": _now_iso(),
        }
        self._save()

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
        self._payload["last_poll_failure"] = None
        self._save()

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
        self._payload["pending_pr"] = entry
        self._append_history(dict(entry))
        self._save()

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
        current = self._payload["pending_pr"]

        if (
            tree_hash is not None
            and current is not None
            and (
                current.get("tree_hash") != tree_hash or current.get("branch") != branch
            )
        ):
            self._payload["pending_pr_stale"] = {
                "reason": reason,
                "tree_hash": tree_hash,
                "branch": branch,
                "time": _now_iso(),
            }
            self._save()
            return

        self._payload["pending_pr_failure"] = {
            "reason": reason,
            "time": _now_iso(),
        }
        self._payload["pending_pr"] = None
        self._save()

    def confirm_pr_created(self, *, tree_hash: str, branch: str, pr_url: str) -> None:
        """Confirm a pending PR was actually created — the only call that
        advances `last_pushed_hash` for the PR-handoff flow.

        Called out-of-band, from a KiroCrew agent context via the
        `complete-pr-handoff` skill, once Buildo has actually opened the
        PR. Delegates to `record_push_success` since that is the only
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
        current = self._payload["pending_pr"]
        if (
            current is None
            or current.get("tree_hash") != tree_hash
            or current.get("branch") != branch
        ):
            self._payload["pending_pr_stale"] = {
                "reason": "confirm_pr_created for a superseded tree_hash",
                "tree_hash": tree_hash,
                "branch": branch,
                "pr_url": pr_url,
                "time": _now_iso(),
            }
            self._save()
            return

        self.record_push_success(tree_hash=tree_hash, branch=branch, pr_url=pr_url)
        self._payload["pending_pr"] = None
        self._save()

    def record_seen_sha(self, sha: str) -> None:
        """Record the latest polled SHA, overwriting any previous value."""
        self._payload["last_seen_sha"] = sha
        self._save()

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
        self._payload["base_sha"] = sha
        self._payload["pending"] = {
            "sha": sha,
            "author": author,
            "subject": subject,
            "classified_paths": dict(classified_paths),
            "ignored_paths": list(ignored_paths or []),
            "touched_classes": list(touched_classes or []),
        }
        self._save()

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
        current = self._payload["pending"]
        if current is None:
            raise ValueError(
                "accumulate_pending called with no existing pending record; "
                "use set_pending to start a new one"
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
            relpath for relpath in merged_ignored if relpath not in merged_classified
        ]

        merged_touched: set[str] = set(current.get("touched_classes") or [])
        merged_touched.update(touched_classes or [])

        self._payload["pending"] = {
            "sha": sha,
            "author": author,
            "subject": subject,
            "classified_paths": merged_classified,
            "ignored_paths": merged_ignored,
            "touched_classes": sorted(merged_touched),
        }
        self._save()

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
        self._payload["base_sha"] = sha
        self._save()

    def clear_pending(self) -> None:
        """Clear the pending record without changing anything else.

        Does NOT touch ``base_sha`` — clearing `pending` and advancing
        `base_sha` are deliberately separate operations (see
        `advance_base_sha`); a caller that means "operator decided"
        must call both.
        """
        self._payload["pending"] = None
        self._save()

    def record_restore_dir(self, *, apply_id: str, restore_dir: str) -> None:
        """Record a restore-directory mapping for a previous apply."""
        self._payload["restore_dirs"][apply_id] = restore_dir
        self._save()

    # -- internals ---------------------------------------------------------

    def _append_history(self, entry: dict[str, Any]) -> None:
        history = self._payload["history"]
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
