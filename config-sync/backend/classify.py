"""Classify a commit's changed paths against the allowlist (tasks.md 4.2).

Covers design.md's ``backend/poll.py`` component step ("fetch the commit's
changed-path list, classify each path against the allowlist, write a
`pending` record") and requirements.md 4.3, 5.1, 1.6.

This module never reimplements path matching: it reuses
``backend.allowlist.entry_matches`` / ``backend.allowlist.is_tracked`` for
every admission decision, and only partitions/aggregates the results.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Set

from backend import allowlist
from backend.allowlist import PropagationClass


@dataclass
class Result:
    """The classification outcome for one batch of changed paths.

    Attributes:
        classified: Mapping of each allowlisted path to the
            ``PropagationClass`` of the allowlist entry it matched.
        ignored: The changed paths that matched no allowlist entry for the
            given root, in the order they were given.
        touched_classes: The set of distinct ``PropagationClass`` values
            actually present in ``classified`` — never a class with zero
            matching paths in this batch.
    """

    classified: Dict[str, PropagationClass] = field(default_factory=dict)
    ignored: List[str] = field(default_factory=list)
    touched_classes: Set[PropagationClass] = field(default_factory=set)


def _propagation_class_for(root: str, relpath: str) -> PropagationClass | None:
    """Return the ``PropagationClass`` of the entry ``relpath`` matches.

    Reuses ``allowlist.entry_matches`` against each of the root's own
    entries — never a separate/reimplemented matcher. Returns ``None`` when
    no entry on ``root`` matches, mirroring ``allowlist.is_tracked``.
    """
    for entry in allowlist.ALLOWLIST:
        if entry.root == root and allowlist.entry_matches(entry, relpath):
            return entry.propagation_class
    return None


def classify_paths(root: str, changed_paths: Sequence[str]) -> Result:
    """Partition ``changed_paths`` into allowlisted vs. ignored for ``root``.

    Args:
        root: Which configuration root the paths are relative to (``"A"``
            or ``"B"``), matching ``AllowlistEntry.root``.
        changed_paths: The commit's changed-path list, each already
            relative to ``root`` and ``/``-separated.

    Returns:
        A ``Result`` whose ``classified`` holds every path that
        ``allowlist.is_tracked`` admits (keyed to its entry's
        ``PropagationClass``), whose ``ignored`` holds every path that
        does not, and whose ``touched_classes`` names every
        ``PropagationClass`` actually represented in ``classified``. An
        empty ``changed_paths`` yields an empty, error-free ``Result``.
    """
    result = Result()
    for relpath in changed_paths:
        propagation_class = _propagation_class_for(root, relpath)
        if propagation_class is None:
            result.ignored.append(relpath)
            continue
        result.classified[relpath] = propagation_class
        result.touched_classes.add(propagation_class)
    return result
