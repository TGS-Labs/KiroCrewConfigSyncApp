"""Tracked-file definition for config-sync.

Declares the two configuration roots as pure data (never control flow) and
provides a pure-predicate matcher over relative path strings.

Root A (``KIROCREW_HOME``, default ``~/.kiro/crew``) tracks steering,
skills, and a fixed set of top-level JSON config files. Root B
(``KIRO_HOME``, default ``~/.kiro``) tracks only ``agents/*.json`` —
``kiro_home()``'s own contract is that only the agents directory follows
``KIRO_HOME`` today, so this module does not widen that.

A file is included only on an allowlist *hit* (requirements.md 1.3):
there is no denylist consulted at match time. The never-tracked paths named
in requirements.md 1.4 (``.env``, ``trust/sel_hmac.key``, ``memory.db``,
``memory_index.db``, ``sessions/*.jsonl``, ``models/*.gguf``, ``scratch/**``,
``snapshots/**``, ``gateway.log``, and any lock/pid file) are not encoded
here at all — they are structurally unreachable because no entry's pattern
can match them, and the test suite proves that stays true.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Sequence


class PropagationClass(Enum):
    """How a tracked file's changes reach a running instance.

    Every allowlist entry must carry one of these (requirements.md 1.6), so
    downstream reporting (requirements.md 5.9) can exhaustively branch on it.
    """

    LIVE_IMMEDIATE = "live_immediate"
    BOUNDED_STALE = "bounded_stale"
    RESTART_REQUIRED = "restart_required"


@dataclass(frozen=True)
class AllowlistEntry:
    """One declarative allowlist rule.

    ``pattern`` is a glob evaluated against the path *relative to its root*
    (``root``), using ``/`` as the separator on every platform. Glob
    semantics (path-aware, not ``fnmatch``'s flat wildcard):

    - ``*`` matches any run of characters *within one path segment* (never
      crosses a ``/``).
    - ``**/`` matches zero or more whole path segments.
    - every other character is literal.
    """

    root: str
    pattern: str
    propagation_class: PropagationClass


def _compile_pattern(pattern: str) -> re.Pattern[str]:
    """Translate a path-aware glob pattern into a compiled regex.

    ``**`` only has its "any number of segments" meaning when it appears as
    a whole path segment on its own (``**/foo``, ``foo/**``, or
    ``foo/**/bar``); a single ``*`` never matches across a ``/``.
    """
    segments = pattern.split("/")
    body_parts: list[str] = []
    prev_was_doublestar = False

    for i, segment in enumerate(segments):
        is_last = i == len(segments) - 1

        if segment == "**":
            if i > 0 and not prev_was_doublestar:
                body_parts.append("/")
            if is_last:
                # Trailing "**": one or more remaining path components,
                # with no requirement to end on a '/'.
                body_parts.append(r"[^/]+(?:/[^/]+)*")
            else:
                # Zero or more whole path segments, each followed by '/'.
                body_parts.append(r"(?:[^/]+/)*")
            prev_was_doublestar = True
            continue

        if i > 0 and not prev_was_doublestar:
            body_parts.append("/")

        escaped = re.escape(segment).replace(r"\*", "[^/]*")
        body_parts.append(escaped)
        prev_was_doublestar = False

    body = "".join(body_parts)
    return re.compile(f"^{body}$")


# ---------------------------------------------------------------------------
# Root A — KIROCREW_HOME (default ~/.kiro/crew)
# ---------------------------------------------------------------------------

_ROOT_A_ENTRIES: Sequence[AllowlistEntry] = (
    AllowlistEntry("A", "steering/**/*.md", PropagationClass.LIVE_IMMEDIATE),
    AllowlistEntry("A", "skills/**/SKILL.md", PropagationClass.BOUNDED_STALE),
    AllowlistEntry("A", "skills/**/scripts/**", PropagationClass.BOUNDED_STALE),
    AllowlistEntry("A", "config.json", PropagationClass.RESTART_REQUIRED),
    AllowlistEntry("A", "hooks.json", PropagationClass.RESTART_REQUIRED),
    AllowlistEntry("A", "agent_model_state.json", PropagationClass.RESTART_REQUIRED),
    AllowlistEntry("A", "mcp.json", PropagationClass.RESTART_REQUIRED),
    AllowlistEntry("A", "crons.json", PropagationClass.LIVE_IMMEDIATE),
    AllowlistEntry("A", "instances.json", PropagationClass.LIVE_IMMEDIATE),
)

# ---------------------------------------------------------------------------
# Root B — KIRO_HOME (default ~/.kiro) — agents/*.json only
# ---------------------------------------------------------------------------

_ROOT_B_ENTRIES: Sequence[AllowlistEntry] = (
    AllowlistEntry("B", "agents/*.json", PropagationClass.RESTART_REQUIRED),
)

ALLOWLIST: Sequence[AllowlistEntry] = tuple(_ROOT_A_ENTRIES) + tuple(_ROOT_B_ENTRIES)


def entry_matches(entry: AllowlistEntry, relpath: str) -> bool:
    """Pure predicate: does ``relpath`` match this single entry's pattern?

    Case-sensitive (a same-named file with different case, e.g.
    ``skill.md`` vs ``SKILL.md``, must not be swept in) and never touches
    the filesystem.
    """
    return _compile_pattern(entry.pattern).match(relpath) is not None


def is_tracked(root: str, relpath: str) -> bool:
    """Pure predicate: is ``relpath`` (relative to ``root``) allowlisted?

    Evaluated purely from the path string and the declarative ``ALLOWLIST``
    data — never stats the filesystem, so "matches an entry" and "exists on
    disk" stay two separate questions (the latter is collect.py's job).
    """
    return any(
        entry.root == root and entry_matches(entry, relpath) for entry in ALLOWLIST
    )
