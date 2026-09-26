"""Per-applied-file propagation reporting (tasks.md 5.3).

Covers design.md's ``backend/propagate.py`` component and requirements.md
Requirement 5 acceptance criteria 5.1-5.5, 5.8, 5.9: an apply result must
report a propagation state for every applied file, and must never collapse
several files' distinct states into a single summary message.

This module never reimplements path-to-class matching. It reuses
``backend.classify.classify_paths`` (which itself reuses
``backend.allowlist.entry_matches`` / ``backend.allowlist.is_tracked``) to
get each applied file's *baseline* classification from the allowlist, then
refines a ``SKILL.md``'s baseline ``LIVE_IMMEDIATE`` up to
``LIVE_WITHIN_60S`` when the change is discoverability-shaped (an add,
remove, rename, or a frontmatter-trigger change) rather than a body-only
edit — a distinction the allowlist's pure path-pattern matching cannot make
on its own, because it is a property of *what changed*, not of *where* it
changed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List

from backend import classify
from backend.allowlist import PropagationClass


class ChangeKind(Enum):
    """How one applied file's path changed in the commit being applied.

    Member names are lower-case (unlike the rest of this codebase's enums)
    because callers construct these via ``getattr(ChangeKind, kind)`` from
    the same lower-case strings apply.py's own per-file change records use
    ("added", "removed", "renamed", "modified") — a design choice tests
    reach through directly rather than a hand-rolled string.
    """

    added = "added"
    removed = "removed"
    renamed = "renamed"
    modified = "modified"


@dataclass(frozen=True)
class AppliedFile:
    """One file apply.py applied, as handed to propagate.py.

    Attributes:
        root: Which configuration root this file belongs to (``"A"`` or
            ``"B"``), matching ``AllowlistEntry.root``.
        relpath: The file's path relative to ``root``, ``/``-separated.
        kind: How the path changed in the applied commit.
        frontmatter_changed: Whether a ``SKILL.md``'s frontmatter triggers
            changed. Meaningless (and ignored) for a non-``SKILL.md`` path.
    """

    root: str
    relpath: str
    kind: ChangeKind
    frontmatter_changed: bool = False


@dataclass(frozen=True)
class ReportEntry:
    """One applied file's propagation state, reported on its own.

    Attributes:
        propagation_class: The (possibly refined) ``PropagationClass``.
        message: A human-readable, honest description of when the change
            takes effect.
        requires_restart: Whether the change requires or triggers a
            gateway restart. Always ``False`` — no tracked configuration
            class needs one.
    """

    propagation_class: PropagationClass
    message: str
    requires_restart: bool = False


@dataclass(frozen=True)
class Report:
    """The full apply result's propagation report.

    Attributes:
        entries: Mapping of each applied file's ``relpath`` to its own
            ``ReportEntry``. Deliberately carries no ``message`` /
            ``summary`` / ``overall_message`` field of its own
            (requirements.md 5.9) — a caller must read the per-file
            entries, never a single collapsed status line.
    """

    entries: Dict[str, ReportEntry]


_SKILL_MD_SUFFIX = "SKILL.md"

_DISCOVERABILITY_KINDS = (ChangeKind.added, ChangeKind.removed, ChangeKind.renamed)

_MESSAGES: Dict[PropagationClass, str] = {
    PropagationClass.LIVE_IN_NEW_SESSION: (
        "Live in a new session — steering loads at session start (or a "
        "post-compaction warm reinjection); it is not live in an "
        "already-running session, and nothing else needs to happen for "
        "steering alone."
    ),
    PropagationClass.LIVE_IMMEDIATE: (
        "Live now — an agent reads this file at the time it is used."
    ),
    PropagationClass.LIVE_WITHIN_60S: (
        "Skill index visible within 60s — the in-process discovery-cache "
        "invalidator is unreachable from this out-of-process backend, so "
        "the bounded staleness window (at most 60 seconds) is reported "
        "rather than claiming immediate availability."
    ),
    PropagationClass.LIVE_ON_NEXT_RESOLUTION: (
        "Live on next resolution — this file is picked up on the next "
        "resolution/cache check with no restart required; sessions "
        "already running keep the value they already resolved."
    ),
}


def _is_skill_md(relpath: str) -> bool:
    """Return whether ``relpath``'s basename is exactly ``SKILL.md``."""
    return relpath.rsplit("/", 1)[-1] == _SKILL_MD_SUFFIX


def _refine_skill_class(
    baseline: PropagationClass, applied: AppliedFile
) -> PropagationClass:
    """Refine a ``SKILL.md``'s baseline class using what changed about it.

    The allowlist's baseline classification for ``skills/**/SKILL.md`` is
    always ``LIVE_IMMEDIATE`` (a pure function of the path pattern). This
    escalates it to ``LIVE_WITHIN_60S`` when the change is
    discoverability-shaped — added, removed, renamed, or a frontmatter
    trigger change — and leaves it at ``LIVE_IMMEDIATE`` for a body-only
    edit (requirements.md 5.3, 5.4, 5.5).
    """
    if not _is_skill_md(applied.relpath):
        return baseline
    if applied.kind in _DISCOVERABILITY_KINDS or applied.frontmatter_changed:
        return PropagationClass.LIVE_WITHIN_60S
    return baseline


def build_report(applied_files: List[AppliedFile]) -> Report:
    """Build the propagation report for a batch of applied files.

    Each file's baseline classification comes from
    ``classify.classify_paths`` (which reuses the allowlist matcher) against
    that single file's root and relpath; a ``SKILL.md`` baseline is then
    refined per ``_refine_skill_class``. A file that matches no allowlist
    entry is skipped — ``apply.py`` is responsible for having already
    filtered applied files to the allowlist, so an unmatched path here
    indicates nothing to report rather than an error.

    Args:
        applied_files: Every file apply.py wrote, removed, or renamed in
            this apply, each already relative to its own ``root``.

    Returns:
        A ``Report`` with one ``ReportEntry`` per applied file that matched
        an allowlist entry, keyed by ``relpath``. An empty input yields an
        empty, error-free ``Report``.
    """
    entries: Dict[str, ReportEntry] = {}
    for applied in applied_files:
        result = classify.classify_paths(applied.root, [applied.relpath])
        baseline = result.classified.get(applied.relpath)
        if baseline is None:
            continue

        propagation_class = _refine_skill_class(baseline, applied)
        entries[applied.relpath] = ReportEntry(
            propagation_class=propagation_class,
            message=_MESSAGES[propagation_class],
            requires_restart=False,
        )

    return Report(entries=entries)
