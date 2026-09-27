"""Phase 8 (live enable) finding: a real poll tick did not finish in 120 s.

``collect.collect()`` walked every file under ``~/.kiro/crew`` — on the live
box that is ~1.4 million files, almost all under ``scratch/`` and
``workspace/`` (repos, node_modules, venvs) — although every allowlist
pattern names a fixed top-level segment (``steering``, ``skills``,
``config.json``, ``config-bundles/...``, ``agents``, ...). The walk must be
scoped to the top-level entries an allowlist entry for that root can ever
match; everything else is never entered, not merely filtered afterwards.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from backend import allowlist, collect


@pytest.fixture
def isolated_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    root_a = tmp_path / "kirocrew_home"
    root_b = tmp_path / "kiro_home"
    root_a.mkdir()
    root_b.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(root_a))
    monkeypatch.setenv("KIRO_HOME", str(root_b))
    return {"root_a": root_a, "root_b": root_b}


def _write(root: Path, relpath: str, content: bytes = b"content") -> None:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_tracked_top_level_names_are_derived_from_the_allowlist() -> None:
    """Every entry's first path segment is in the set for its root, and the
    set contains nothing else — so a new allowlist entry is picked up
    automatically and the live tree's big untracked siblings are not."""
    for root in ("A", "B"):
        names = allowlist.tracked_top_level_names(root)
        expected = {
            entry.pattern.split("/")[0]
            for entry in allowlist.ALLOWLIST
            if entry.root == root
        }
        assert names == expected, (root, names, expected)
        assert names, root
        for never in ("workspace", "scratch", "apps", "sessions", "subagents"):
            assert never not in names


def _record_dir_opens(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every directory the walk opens. pathlib's ``iterdir`` bottoms
    out in ``os.listdir`` (3.12) and ``rglob``/``os.walk`` in ``os.scandir``;
    recording both observes pruning regardless of the walk strategy."""
    opened: list[str] = []
    real_scandir = os.scandir
    real_listdir = os.listdir

    def scandir(path: Any = ".") -> Any:
        opened.append(os.fsdecode(path))
        return real_scandir(path)

    def listdir(path: Any = ".") -> Any:
        opened.append(os.fsdecode(path))
        return real_listdir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    monkeypatch.setattr(os, "listdir", listdir)
    return opened


def test_collector_never_enters_a_top_level_dir_no_entry_can_match(
    isolated_roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Behavioural proof of pruning: the untracked sibling directory is never
    opened, and the tracked files are still collected."""
    root_a = isolated_roots["root_a"]
    _write(root_a, "steering/rules.md", b"# rules")
    _write(root_a, "config.json", b"{}")
    _write(root_a, "workspace/repo/steering/decoy.md", b"# not tracked")
    opened = _record_dir_opens(monkeypatch)
    collected = collect.collect()
    assert set(collected) == {"steering/rules.md", "config.json"}
    entered = {Path(o).resolve() for o in opened}
    assert (root_a / "workspace").resolve() not in entered, sorted(opened)
    assert (root_a / "steering").resolve() in entered


def test_collector_still_prunes_never_tracked_segments_inside_tracked_dirs(
    isolated_roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`skills/x/scratch/...` is a never-tracked segment (Req 1.4); the walk
    must not enter it either, even though `skills/` itself is tracked."""
    root_a = isolated_roots["root_a"]
    _write(root_a, "skills/x/SKILL.md", b"# skill")
    _write(root_a, "skills/x/scratch/junk.md", b"junk")
    opened = _record_dir_opens(monkeypatch)
    collected = collect.collect()
    assert set(collected) == {"skills/x/SKILL.md"}
    entered = {Path(o).resolve() for o in opened}
    assert (root_a / "skills" / "x" / "scratch").resolve() not in entered


def test_top_level_tracked_file_and_dir_are_both_collected(
    isolated_roots: dict[str, Path],
) -> None:
    """The scoped walk must handle both shapes an allowlist first segment
    can take: a file (`config.json`) and a directory (`agents/`)."""
    root_b = isolated_roots["root_b"]
    root_a = isolated_roots["root_a"]
    _write(root_b, "agents/one.json", b"{}")
    _write(root_a, "mcp.json", b"{}")
    _write(root_a, "config-bundles/agent-prompts/p.md", b"# p")
    collected = collect.collect()
    assert set(collected) == {
        "agents/one.json",
        "mcp.json",
        "config-bundles/agent-prompts/p.md",
    }


def test_an_entry_without_a_literal_first_segment_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pruning is only sound when every entry's first segment is literal; an
    entry such as `**/*.md` must be refused loudly rather than silently
    widening the walk back to the whole root."""
    bad = allowlist.AllowlistEntry(
        "A", "**/*.md", allowlist.PropagationClass.LIVE_IN_NEW_SESSION
    )
    monkeypatch.setattr(allowlist, "ALLOWLIST", (*allowlist.ALLOWLIST, bad))
    with pytest.raises(ValueError, match="no literal first segment"):
        allowlist.tracked_top_level_names("A")
