"""Safe collection of tracked configuration files (design.md `backend/collect.py`).

Walks both configuration roots — root A (``KIROCREW_HOME``, default
``~/.kiro/crew``) and root B (``KIRO_HOME``, default ``~/.kiro``) — and
returns a mapping of ``{relpath: bytes}`` containing only files that satisfy
``backend.allowlist.is_tracked`` (requirements.md 1.3).

The allowlist is the *sole* admission test: this module never hand-rolls a
denylist or its own notion of "safe", it only walks the filesystem and asks
``allowlist.is_tracked`` about each path. An allowlisted path that does not
exist on disk (e.g. ``crons.json`` on an instance with no scheduled jobs) is
simply absent from the result — no error is raised, and the collector never
creates it as a side effect (requirements.md 1.5).

Content is returned verbatim as raw bytes: no redaction, no re-serialization.
That is ``backend/redact.py``'s job, downstream of this one.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Iterator, Tuple

from backend import allowlist

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


def _iter_relpaths(root: Path) -> Iterator[str]:
    """Yield every regular file under ``root`` as a ``/``-separated path

    relative to ``root``. Yields nothing if ``root`` does not exist —
    walking a never-created root is not an error.
    """
    if not root.is_dir():
        return
    for path in root.rglob("*"):
        if path.is_file():
            yield path.relative_to(root).as_posix()


def _collect_root(root_id: str, root_path: Path) -> Dict[str, bytes]:
    """Collect allowlist hits found under one root, keyed by their relpath."""
    collected: Dict[str, bytes] = {}
    for relpath in _iter_relpaths(root_path):
        if allowlist.is_tracked(root_id, relpath):
            collected[relpath] = (root_path / relpath).read_bytes()
    return collected


def _roots() -> Tuple[Tuple[str, Path], Tuple[str, Path]]:
    return (("A", _root_a_path()), ("B", _root_b_path()))


def collect() -> Dict[str, bytes]:
    """Walk both configuration roots and return allowlist hits as bytes.

    Returns:
        A mapping of relative path (``/``-separated, relative to whichever
        root it was found under) to the file's raw, unmodified bytes.
        Contains only paths for which ``allowlist.is_tracked`` returns
        ``True`` for that path's own root. An allowlisted path absent from
        disk is simply omitted — never an error, never created.
    """
    result: Dict[str, bytes] = {}
    for root_id, root_path in _roots():
        result.update(_collect_root(root_id, root_path))
    return result
