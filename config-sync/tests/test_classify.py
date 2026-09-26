"""Tests for backend/classify.py (tasks.md 4.2).

Covers design.md's "backend/poll.py" component step ("fetch the commit's
changed-path list, classify each path against the allowlist, write a
`pending` record") and requirements.md:

- 1.6: WHEN a new configuration file class is added to the allowlist THEN a
  test SHALL fail if any allowlist entry has no propagation classification.
  Here that means: classify.py must be able to produce a
  `PropagationClass` for a path matching EVERY entry currently in
  `backend/allowlist.py`'s real `ALLOWLIST` — the test iterates the real
  allowlist rather than a hand-picked subset, so it fails automatically the
  day a future entry is added without classify.py being able to handle it.
- 4.3: the poll "classifies each path against the allowlist ... naming
  which tracked configuration classes the change touches" — a set of
  changed paths that mixes allowlisted and non-allowlisted paths must be
  correctly partitioned, naming the touched classes and flagging the rest
  as ignorable/non-tracked.
- 5.1: "The app SHALL hold a propagation classification for every tracked
  configuration class" — classify.py is the component that attaches that
  classification to a concrete changed path; it must reuse
  `allowlist.py`'s own matcher (`entry_matches` / `is_tracked`) rather than
  reimplementing path matching, per the task description.

Per requirements.md 1.6's own scope, this exercises the EXISTING
`PropagationClass` enum members already defined in `backend/allowlist.py`
(`LIVE_IN_NEW_SESSION`, `LIVE_IMMEDIATE`, `LIVE_WITHIN_60S`,
`LIVE_ON_NEXT_RESOLUTION`) — no new enum values are invented here.

This module intentionally imports `classify` (backend/classify.py), which
does not exist yet. All tests below are expected to fail with a
collection-time ImportError / ModuleNotFoundError until software-engineer
implements it — this is the correct TDD starting state, not a test defect.
"""

from __future__ import annotations

import inspect

import pytest

from backend import allowlist
from backend import classify


# ---------------------------------------------------------------------------
# Helpers — derive a concrete, matching relative path for any allowlist
# entry's glob pattern, so the "every entry is classifiable" test does not
# have to hand-maintain a parallel list of example paths per entry (which
# would silently drift from backend/allowlist.py's real ALLOWLIST).
# ---------------------------------------------------------------------------


def _concrete_path_for(entry: allowlist.AllowlistEntry) -> str:
    """Build one concrete relative path that matches ``entry.pattern``.

    Walks the pattern's `/`-separated segments, replacing ``**`` with a
    literal ``x`` segment and ``*`` (within a segment) with a literal
    ``x`` run, so the result is a plain path `entry_matches` can match
    against — never a glob itself.
    """
    segments = entry.pattern.split("/")
    concrete: list[str] = []
    for segment in segments:
        if segment == "**":
            concrete.append("x")
        elif "*" in segment:
            concrete.append(segment.replace("*", "x"))
        else:
            concrete.append(segment)
    return "/".join(concrete)


# ---------------------------------------------------------------------------
# (a) requirements.md 1.6 — every real allowlist entry is classifiable.
# ---------------------------------------------------------------------------


class TestEveryAllowlistEntryIsClassifiable:
    """classify.py must produce a PropagationClass for every ALLOWLIST entry.

    Iterates the REAL `backend.allowlist.ALLOWLIST` rather than a fixed
    example set, so this test fails automatically if a future allowlist
    entry is added whose matching paths classify.py cannot classify —
    exactly the regression requirements.md 1.6 names.
    """

    @pytest.mark.parametrize(
        "entry",
        list(allowlist.ALLOWLIST),
        ids=[f"{e.root}:{e.pattern}" for e in allowlist.ALLOWLIST],
    )
    def test_matching_path_classifies_to_entrys_own_propagation_class(
        self, entry: allowlist.AllowlistEntry
    ) -> None:
        path = _concrete_path_for(entry)
        assert allowlist.entry_matches(entry, path), (
            f"test bug: constructed path {path!r} does not actually match "
            f"entry pattern {entry.pattern!r} on root {entry.root!r}"
        )

        result = classify.classify_paths(entry.root, [path])

        assert path in result.classified, (
            f"classify_paths did not classify {path!r} (root {entry.root!r}), "
            f"which matches allowlist entry {entry.pattern!r}"
        )
        assert result.classified[path] == entry.propagation_class
        assert path not in result.ignored

    def test_every_allowlist_entry_has_a_reachable_propagation_class(self) -> None:
        """No allowlist entry is silently unclassifiable as a class.

        Complements the per-entry parametrized test above with a single
        aggregate assertion: every propagation class actually used by the
        real allowlist is one classify.py can return, and none is dropped
        or remapped in translation.
        """
        used_classes = {entry.propagation_class for entry in allowlist.ALLOWLIST}
        for entry in allowlist.ALLOWLIST:
            path = _concrete_path_for(entry)
            result = classify.classify_paths(entry.root, [path])
            assert result.classified.get(path) in used_classes


# ---------------------------------------------------------------------------
# (b) requirements.md 4.3 — mixed allowlisted / non-allowlisted paths are
# correctly partitioned, naming the touched classes and flagging the rest.
# ---------------------------------------------------------------------------


class TestMixedChangedPathsArePartitioned:
    def test_allowlisted_and_non_allowlisted_paths_are_partitioned(self) -> None:
        changed_paths = [
            "steering/foo.md",  # root A: LIVE_IN_NEW_SESSION
            "config.json",  # root A: LIVE_ON_NEXT_RESOLUTION
            "memory.db",  # never-tracked -> ignorable
            "some/random/file.txt",  # not on the allowlist at all -> ignorable
        ]

        result = classify.classify_paths("A", changed_paths)

        assert result.classified["steering/foo.md"] == (
            allowlist.PropagationClass.LIVE_IN_NEW_SESSION
        )
        assert result.classified["config.json"] == (
            allowlist.PropagationClass.LIVE_ON_NEXT_RESOLUTION
        )
        assert "memory.db" in result.ignored
        assert "some/random/file.txt" in result.ignored
        assert "memory.db" not in result.classified
        assert "some/random/file.txt" not in result.classified

    def test_touched_classes_names_only_the_classes_actually_touched(self) -> None:
        changed_paths = [
            "steering/foo.md",  # LIVE_IN_NEW_SESSION
            "steering/bar.md",  # LIVE_IN_NEW_SESSION (same class again)
            "config.json",  # LIVE_ON_NEXT_RESOLUTION
            "ignored/not/tracked.txt",
        ]

        result = classify.classify_paths("A", changed_paths)

        assert result.touched_classes == {
            allowlist.PropagationClass.LIVE_IN_NEW_SESSION,
            allowlist.PropagationClass.LIVE_ON_NEXT_RESOLUTION,
        }
        # A class nothing in this change touches must not be named.
        assert allowlist.PropagationClass.LIVE_IMMEDIATE not in result.touched_classes
        assert allowlist.PropagationClass.LIVE_WITHIN_60S not in result.touched_classes

    def test_all_non_allowlisted_paths_are_ignored_and_no_class_is_touched(
        self,
    ) -> None:
        changed_paths = ["random/untracked/file.txt", "another/one.bin"]

        result = classify.classify_paths("A", changed_paths)

        assert result.classified == {}
        assert set(result.ignored) == set(changed_paths)
        assert result.touched_classes == set()

    def test_root_b_agents_json_classifies_while_other_root_b_paths_are_ignored(
        self,
    ) -> None:
        changed_paths = ["agents/my-agent.json", "not-agents/other.json"]

        result = classify.classify_paths("B", changed_paths)

        assert result.classified["agents/my-agent.json"] == (
            allowlist.PropagationClass.LIVE_ON_NEXT_RESOLUTION
        )
        assert "not-agents/other.json" in result.ignored


# ---------------------------------------------------------------------------
# (c) an empty changed-paths list is a no-op result, no error.
# ---------------------------------------------------------------------------


class TestEmptyChangedPathsIsANoOp:
    def test_empty_list_returns_empty_result_without_error(self) -> None:
        result = classify.classify_paths("A", [])

        assert result.classified == {}
        assert result.ignored == []
        assert result.touched_classes == set()

    def test_empty_list_does_not_raise_for_either_root(self) -> None:
        # Must not error regardless of which root is passed, since a commit
        # with zero changed paths under a given root is a legitimate input,
        # not a caller mistake.
        classify.classify_paths("A", [])
        classify.classify_paths("B", [])


# ---------------------------------------------------------------------------
# classify.py must reuse allowlist.py's own matcher, not reimplement path
# matching (explicit task requirement). Verified by spying on the real
# matcher functions rather than asserting on classify.py's source text.
# ---------------------------------------------------------------------------


class TestClassifyReusesAllowlistsOwnMatcher:
    def test_classify_paths_calls_allowlists_entry_matches_or_is_tracked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []

        original_entry_matches = allowlist.entry_matches
        original_is_tracked = allowlist.is_tracked

        def _spy_entry_matches(entry: allowlist.AllowlistEntry, relpath: str) -> bool:
            calls.append("entry_matches")
            return original_entry_matches(entry, relpath)

        def _spy_is_tracked(root: str, relpath: str) -> bool:
            calls.append("is_tracked")
            return original_is_tracked(root, relpath)

        monkeypatch.setattr(allowlist, "entry_matches", _spy_entry_matches)
        monkeypatch.setattr(allowlist, "is_tracked", _spy_is_tracked)
        # classify.py may have imported either name directly into its own
        # namespace; patch those references too so the spy is hit
        # regardless of import style (`from backend import allowlist` vs
        # `from backend.allowlist import entry_matches`).
        if hasattr(classify, "entry_matches"):
            monkeypatch.setattr(classify, "entry_matches", _spy_entry_matches)
        if hasattr(classify, "is_tracked"):
            monkeypatch.setattr(classify, "is_tracked", _spy_is_tracked)

        classify.classify_paths("A", ["steering/foo.md", "not/tracked.txt"])

        assert calls, (
            "classify_paths never called allowlist.entry_matches or "
            "allowlist.is_tracked — it must reuse allowlist.py's own "
            "matcher rather than reimplementing path matching"
        )

    def test_classify_module_does_not_reimplement_glob_compilation(self) -> None:
        """classify.py must not define its own pattern-compiling helper.

        A module that reuses allowlist.py's matcher has no reason to
        define a same-shaped private helper (e.g. `_compile_pattern`) of
        its own; a second implementation is exactly the reimplementation
        the task forbids.
        """
        source = inspect.getsource(classify)
        assert "_compile_pattern" not in source
        assert "fnmatch" not in source
