"""Durable app state for config-sync (design.md `backend/state.py`).

One JSON document, under the app's OWN state directory — never inside
either tracked configuration root (``KIROCREW_HOME`` / ``KIRO_HOME``), so
the app's own bookkeeping can never be swept into a commit it makes.

Persists: ``last_pushed_hash``, ``last_push`` (time/branch/PR URL),
``last_seen_sha``, ``pending`` (sha/author/subject/classified paths), a
bounded ``history``, and ``restore_dirs``.

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
    "pending": None,
    "history": [],
    "restore_dirs": {},
}


_ROOT_A_ENV = "KIROCREW_HOME"
_ROOT_B_ENV = "KIRO_HOME"


def _root_a_path() -> Path:
    """Resolve root A: ``KIROCREW_HOME``, defaulting to ``~/.kiro/crew``."""
    override = os.environ.get(_ROOT_A_ENV)
    return Path(override) if override else Path.home() / ".kiro" / "crew"


def _root_b_path() -> Path:
    """Resolve root B: ``KIRO_HOME``, defaulting to ``~/.kiro``."""
    override = os.environ.get(_ROOT_B_ENV)
    return Path(override) if override else Path.home() / ".kiro"


def _is_descendant_or_same(candidate: Path, ancestor: Path) -> bool:
    """True if ``candidate`` resolves to ``ancestor`` or lives under it."""
    candidate = candidate.resolve()
    ancestor = ancestor.resolve()
    return candidate == ancestor or ancestor in candidate.parents


def get_state_dir() -> Path:
    """Return the app's own state directory, creating it if needed.

    Deliberately independent of ``KIROCREW_HOME`` and ``KIRO_HOME`` — the
    two tracked configuration roots this app reads from and commits — so
    this app's bookkeeping can never land inside a tree it syncs.

    Honors ``CONFIG_SYNC_STATE_DIR`` as an explicit override for operators
    and tests. Otherwise both roots are resolved (matching
    ``backend.collect``'s ``_root_a_path`` / ``_root_b_path`` convention:
    env var override, else the real default) and the state directory is
    anchored at the *shallowest common ancestor* of both resolved roots'
    parents, walking further up if that ancestor still turns out to be a
    descendant of (or equal to) either root. This is provably outside both
    roots regardless of their actual on-disk relationship — on a real host
    ``KIROCREW_HOME`` (``~/.kiro/crew``) is itself a descendant of
    ``KIRO_HOME``'s default (``~/.kiro``), so anchoring merely one level
    above one root is not sufficient in general; walking up from the
    shallower root's parent until clear of both is.
    """
    override = os.environ.get(_STATE_DIR_ENV)
    if override:
        state_dir = Path(override)
    else:
        root_a = _root_a_path().resolve()
        root_b = _root_b_path().resolve()
        # Start from whichever root is shallower (fewer path parts), since
        # its parent is the more likely candidate to already sit outside
        # the other root too.
        anchor = min(root_a.parent, root_b.parent, key=lambda p: len(p.parts))
        while _is_descendant_or_same(anchor, root_a) or _is_descendant_or_same(
            anchor, root_b
        ):
            if anchor.parent == anchor:
                # Reached the filesystem root without clearing either tree
                # (pathological input) — stop rather than loop forever.
                break
            anchor = anchor.parent
        state_dir = anchor / "config-sync-state"
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
    def pending(self) -> dict[str, Any] | None:
        return cast("dict[str, Any] | None", self._payload["pending"])

    @property
    def history(self) -> list[dict[str, Any]]:
        return cast("list[dict[str, Any]]", self._payload["history"])

    @property
    def restore_dirs(self) -> dict[str, str]:
        return cast("dict[str, str]", self._payload["restore_dirs"])

    # -- mutations --------------------------------------------------------

    def record_push_success(
        self, *, tree_hash: str, branch: str, pr_url: str | None
    ) -> None:
        """Record a successful push. Only this advances `last_pushed_hash`."""
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
    ) -> None:
        """Record a pending commit awaiting approval or decline."""
        self._payload["pending"] = {
            "sha": sha,
            "author": author,
            "subject": subject,
            "classified_paths": dict(classified_paths),
        }
        self._save()

    def clear_pending(self) -> None:
        """Clear the pending record without changing anything else."""
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
