"""Tracked-file definition for config-sync.

Declares the two configuration roots as pure data (never control flow) and
provides a pure-predicate matcher over relative path strings.

Root A (``KIROCREW_HOME``, default ``~/.kiro/crew``) tracks steering,
skills, and a fixed set of top-level JSON config files. Root B
(``KIRO_HOME``, default ``~/.kiro``) tracks only ``agents/*.json`` —
``kiro_home()``'s own contract is that only the agents directory follows
``KIRO_HOME`` today, so this module does not widen that.

A file is included only on an allowlist *hit* (requirements.md 1.3):
there is no separate denylist consulted *instead of* the allowlist. The
never-tracked paths named in requirements.md 1.4 (``.env``,
``trust/sel_hmac.key``, ``memory.db``, ``memory_index.db``,
``sessions/*.jsonl``, ``models/*.gguf``, ``scratch/**``, ``snapshots/**``,
``gateway.log``, and any lock/pid file) are structurally unreachable: most
entry patterns simply never match their shape, and for the one entry whose
glob otherwise would (``skills/**/scripts/**`` can span arbitrarily deep
nested components, including a leaked ``.env``, key, or lock/pid file under
a skill's ``scripts/`` tree), ``entry_matches`` applies an explicit
requirements.md-1.4 exclusion *inside* the matcher before consulting any
entry's pattern — so a never-tracked path can never become a hit through
any entry, on either root. The test suite proves that stays true.
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
    Exactly four non-collapsible states (design.md's propagation table;
    requirements.md 5.2, 5.7, 5.8, 5.9):

    - ``LIVE_IN_NEW_SESSION``: ``steering/**``. Loaded at session start (or a
      post-compaction warm reinjection); explicitly NOT live in an
      already-running session (requirements.md 5.2 forbids claiming
      otherwise), and no restart is needed or triggered for steering alone.
    - ``LIVE_IMMEDIATE``: a ``SKILL.md`` body, or a skill's ``scripts/**`` —
      an agent reads the file at time of use, so the new content is live the
      next time it is read (requirements.md 5.3).
    - ``LIVE_WITHIN_60S``: skill *index/triggers* (which skills exist, what
      triggers them) — bounded by the discovery cache's 60-second TTL
      (requirements.md 5.4, 5.5). Distinct from a SKILL.md body's own
      immediacy: adding/removing/renaming a skill, or changing its
      frontmatter triggers, is a *discoverability* change, not a body read.
    - ``LIVE_ON_NEXT_RESOLUTION``: ``config.json``, ``hooks.json``,
      ``agent_model_state.json``, ``mcp.json``, ``crons.json``,
      ``instances.json``, and ``agents/*.json`` — picked up on the next
      resolution/cache check with no restart required; an already-running
      session keeps whatever it already resolved (requirements.md 5.7, 5.8).
      Named for what actually happens, not "RESTART_REQUIRED" — that name is
      itself wrong per 5.7/5.8, since no restart is required for this class.
    """

    LIVE_IN_NEW_SESSION = "live_in_new_session"
    LIVE_IMMEDIATE = "live_immediate"
    LIVE_WITHIN_60S = "live_within_60s"
    LIVE_ON_NEXT_RESOLUTION = "live_on_next_resolution"


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
    AllowlistEntry("A", "steering/**/*.md", PropagationClass.LIVE_IN_NEW_SESSION),
    AllowlistEntry("A", "skills/**/SKILL.md", PropagationClass.LIVE_IMMEDIATE),
    AllowlistEntry("A", "skills/**/scripts/**", PropagationClass.LIVE_IMMEDIATE),
    AllowlistEntry("A", "config.json", PropagationClass.LIVE_ON_NEXT_RESOLUTION),
    AllowlistEntry("A", "hooks.json", PropagationClass.LIVE_ON_NEXT_RESOLUTION),
    AllowlistEntry(
        "A", "agent_model_state.json", PropagationClass.LIVE_ON_NEXT_RESOLUTION
    ),
    AllowlistEntry("A", "mcp.json", PropagationClass.LIVE_ON_NEXT_RESOLUTION),
    AllowlistEntry("A", "crons.json", PropagationClass.LIVE_ON_NEXT_RESOLUTION),
    AllowlistEntry("A", "instances.json", PropagationClass.LIVE_ON_NEXT_RESOLUTION),
    AllowlistEntry(
        "A",
        "config-bundles/agent-prompts/*.md",
        PropagationClass.LIVE_IN_NEW_SESSION,
    ),
)

# ---------------------------------------------------------------------------
# Root B — KIRO_HOME (default ~/.kiro) — agents/*.json only
# ---------------------------------------------------------------------------

_ROOT_B_ENTRIES: Sequence[AllowlistEntry] = (
    AllowlistEntry("B", "agents/*.json", PropagationClass.LIVE_ON_NEXT_RESOLUTION),
)

ALLOWLIST: Sequence[AllowlistEntry] = tuple(_ROOT_A_ENTRIES) + tuple(_ROOT_B_ENTRIES)


_NEVER_TRACKED_BASENAME_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern)
    for pattern in (
        r"(?:^|/)\.env$",
        r"(?:^|/)sel_hmac\.key$",
        r"(?:^|/)memory\.db$",
        r"(?:^|/)memory_index\.db$",
        r"(?:^|/)[^/]+\.jsonl$",
        r"(?:^|/)[^/]+\.gguf$",
        r"(?:^|/)gateway\.log(?:\.\d+)?$",
        r"(?:^|/)[^/]+\.lock$",
        r"(?:^|/)[^/]+\.pid$",
        r"(?:^|/)id_rsa(?:\.[^/]+)?$",
        r"(?:^|/)id_ed25519(?:\.[^/]+)?$",
        # Host-DERIVED agent aliases, not operator config: the gateway projects
        # one `kirocrew-skill-view-<hash>.json` per skill view into ~/.kiro/agents
        # at launch (kiro_crew.acp.skill_projection, NATIVE_SKILL_ALIAS_PREFIX)
        # and rewrites them every session — a different set on every box.
        r"(?:^|/)kirocrew-skill-view-[0-9a-f]+\.json$",
        # Host-SHIPPED agents, not operator config: the gateway writes these
        # agent files itself (kiro_crew/agent_files.py `*_AGENT_FILENAME`) and
        # owns their registration, so a box has no config.json entry or model
        # pin for most of them. Synced, they fail the registration check and
        # block the shared config.json / agent_model_state.json for every
        # other agent in the commit (first live tick, 2026-10-01: 47 files
        # refused). tests/test_allowlist.py pins this list to the host's.
        r"(?:^|/)agents/kirocrew(?:-(?:lite|guest|conductor|pipeline-conductor"
        r"|ledger-conductor|security-conductor|worker|knowledge|research"
        r"|heartbeat))?\.json$",
    )
)

_NEVER_TRACKED_SEGMENT_NAMES: tuple[str, ...] = ("trust", "scratch", "snapshots")


_GLOB_CHARS = frozenset("*?[")


def tracked_top_level_names(root: str) -> frozenset[str]:
    """Top-level path segments under ``root`` that some allowlist entry can

    match — the ONLY directory entries the collector may descend into or
    read. Derived from ``ALLOWLIST`` so a new entry is honoured without a
    second list to maintain. Every current pattern starts with a literal
    segment; a pattern whose first segment carried a glob would make this
    pruning unsound, so such an entry is refused at import time rather than
    silently widening the walk to the whole root (the live ``~/.kiro/crew``
    holds over a million untracked files under ``scratch/`` and
    ``workspace/`` — walking them made a poll tick take minutes).
    """
    names: set[str] = set()
    for entry in ALLOWLIST:
        if entry.root != root:
            continue
        first = entry.pattern.split("/", 1)[0]
        if not first or _GLOB_CHARS & set(first):
            raise ValueError(
                f"allowlist pattern {entry.pattern!r} has no literal first "
                "segment; the collector cannot prune its walk"
            )
        names.add(first)
    return frozenset(names)


def never_tracked_segment(name: str) -> bool:
    """Whether a directory named ``name`` can never contain a tracked path
    (requirements.md 1.4's structural exclusion), so a walk may skip it."""
    return name in _NEVER_TRACKED_SEGMENT_NAMES


def _is_structurally_never_tracked(relpath: str) -> bool:
    """requirements.md 1.4: paths matching this predicate are never tracked,

    regardless of which allowlist entry's pattern would otherwise admit
    them. This is a structural exclusion checked *inside* the matcher — not
    a separate denylist consulted instead of the allowlist (requirements.md
    1.3 still holds: an excluded path was never a hit in the first place,
    it is simply never allowed to become one) — so a credential- or
    secret-shaped basename (``.env``, ``id_rsa``, ``sel_hmac.key``,
    ``*.lock``/``*.pid``, ``*.jsonl``, ``*.gguf``) or a scratch/snapshot/
    trust directory segment cannot be re-admitted by an otherwise-matching
    entry such as ``skills/**/scripts/**``.
    """
    segments = relpath.split("/")
    if any(segment in _NEVER_TRACKED_SEGMENT_NAMES for segment in segments[:-1]):
        return True
    return any(pattern.search(relpath) for pattern in _NEVER_TRACKED_BASENAME_PATTERNS)


def entry_matches(entry: AllowlistEntry, relpath: str) -> bool:
    """Pure predicate: does ``relpath`` match this single entry's pattern?

    Case-sensitive (a same-named file with different case, e.g.
    ``skill.md`` vs ``SKILL.md``, must not be swept in) and never touches
    the filesystem. A path shaped like one of the requirements.md 1.4
    never-tracked classes cannot match here even when the entry's glob
    would otherwise admit it (e.g. ``skills/x/scripts/.env`` under
    ``skills/**/scripts/**``).
    """
    if _is_structurally_never_tracked(relpath):
        return False
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
