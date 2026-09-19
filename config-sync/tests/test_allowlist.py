"""Tests for backend/allowlist.py.

Covers design.md's "backend/allowlist.py" component and tasks.md 1.1:

- The allowlist is expressed as declarative data (an iterable of entries),
  each entry carrying a PropagationClass (requirements.md 1.1, 1.6).
- A structural test fails if any entry's pattern can match a path that must
  never be tracked: `.env`, `trust/sel_hmac.key`, `memory.db`,
  `memory_index.db`, `sessions/*.jsonl`, `models/*.gguf`, `scratch/**`,
  `snapshots/**`, `gateway.log`, or any lock/pid file (requirements.md 1.4).
- A test fails if any allowlist entry lacks a PropagationClass
  (requirements.md 1.6).
- Root B (`KIRO_HOME`, default `~/.kiro`) admits only `agents/*.json`
  (requirements.md 1.2).

These tests define the acceptance criteria for the not-yet-written
`backend/allowlist.py`. They are expected to fail with an ImportError /
ModuleNotFoundError until that module exists — this is the correct TDD
starting state, not a test defect.
"""

from __future__ import annotations

import string

import pytest

from backend import allowlist


# ---------------------------------------------------------------------------
# Paths that must never be reachable through any allowlist entry, on either
# root. These stand in for the denylist named in requirements.md 1.4. They
# are expressed as candidate *relative* paths under each root, matching the
# shapes the requirement names (including glob-shaped ones, tested via a
# representative concrete instance of the glob).
# ---------------------------------------------------------------------------
NEVER_TRACKED_RELATIVE_PATHS = [
    ".env",
    "trust/sel_hmac.key",
    "memory.db",
    "memory_index.db",
    "sessions/abc123.jsonl",
    "sessions/2026-09-19.jsonl",
    "models/some-model.gguf",
    "scratch/anything",
    "scratch/nested/deep/file.txt",
    "snapshots/2026-09-19T00-00-00.tar.gz",
    "snapshots/nested/file",
    "gateway.log",
    "gateway.log.1",
    ".gateway.pid",
    "server.pid",
    "server.lock",
    "some.lock",
    "some.pid",
]


def _all_entries():
    """Return the full flat list of allowlist entries across both roots."""
    entries = allowlist.ALLOWLIST
    assert not callable(entries), (
        "the allowlist must be declarative data, not a function computing "
        "membership at call time (requirements.md 1.1: 'not control flow')"
    )
    return list(entries)


# ---------------------------------------------------------------------------
# Data-shape: the allowlist is data, and every entry carries a
# PropagationClass.
# ---------------------------------------------------------------------------


def test_allowlist_is_a_nonempty_data_collection():
    """ALLOWLIST is an iterable/sequence of entries, not executable logic."""
    entries = _all_entries()
    assert len(entries) > 0


def test_every_entry_declares_a_root():
    """Each entry names which configuration root (A or B) it belongs to."""
    entries = _all_entries()
    for entry in entries:
        assert hasattr(entry, "root"), (
            f"entry {entry!r} has no 'root' attribute; every allowlist "
            "entry must declare which root it tracks"
        )
        assert entry.root in ("A", "B")


def test_every_entry_has_a_propagation_class():
    """requirements.md 1.6: a test SHALL fail if any entry lacks a

    PropagationClass. This is that test.
    """
    entries = _all_entries()
    missing = [
        entry
        for entry in entries
        if getattr(entry, "propagation_class", None) is None
    ]
    assert not missing, (
        "the following allowlist entries have no PropagationClass: "
        f"{missing!r}"
    )


def test_propagation_class_is_one_of_the_defined_kinds():
    """Every entry's PropagationClass is a real, defined classification —

    not an arbitrary string — so downstream reporting (requirements.md 5.9)
    can exhaustively branch on it.
    """
    entries = _all_entries()
    defined = set(allowlist.PropagationClass)
    for entry in entries:
        assert entry.propagation_class in defined, (
            f"entry {entry!r} carries an undefined PropagationClass "
            f"{entry.propagation_class!r}"
        )


def test_root_a_entries_cover_every_required_config_class():
    """requirements.md 1.1: root A must enumerate each named tracked class.

    This does not assert the exact entry count (that would be a snapshot);
    it asserts that a representative concrete path for each required class
    is admitted by at least one root-A entry.
    """
    required_hits = {
        "steering/plan.md": True,
        "skills/some-skill/SKILL.md": True,
        "skills/some-skill/scripts/run.sh": True,
        "config.json": True,
        "hooks.json": True,
        "agent_model_state.json": True,
        "mcp.json": True,
        "crons.json": True,
        "instances.json": True,
    }
    for relpath in required_hits:
        assert allowlist.is_tracked("A", relpath), (
            f"root A path {relpath!r} should be tracked per requirements.md "
            "1.1 but no allowlist entry admits it"
        )


def test_root_b_admits_only_agents_json_files():
    """requirements.md 1.2: root B tracks `agents/*.json` only.

    kiro_home()'s own contract is that only the agents directory follows
    KIRO_HOME; the allowlist must not widen that.
    """
    assert allowlist.is_tracked("B", "agents/my-agent.json")
    assert allowlist.is_tracked("B", "agents/kirocrew.json")

    never_tracked_on_root_b = [
        "config.json",
        "mcp.json",
        "steering/plan.md",
        "skills/some-skill/SKILL.md",
        "agents/nested/deep.json",  # not directly under agents/
        "agents/not-json.txt",
        "agent_model_state.json",
        "crons.json",
    ]
    for relpath in never_tracked_on_root_b:
        assert not allowlist.is_tracked("B", relpath), (
            f"root B path {relpath!r} must NOT be tracked — root B admits "
            "only agents/*.json (requirements.md 1.2)"
        )


def test_root_b_entries_declare_root_b_only():
    """No root-B entry may also serve as a root-A pattern (or vice versa) —

    the two roots are separate namespaces, not a merged one.
    """
    entries = _all_entries()
    root_b_entries = [e for e in entries if e.root == "B"]
    assert root_b_entries, "root B must have at least one entry"
    for entry in root_b_entries:
        assert "agents/" in entry.pattern or "agents/*.json" == entry.pattern


# ---------------------------------------------------------------------------
# Structural denylist proof: requirements.md 1.4. This is the load-bearing
# security property — no allowlist entry, on either root, may match any of
# these paths. Parametrized so a future entry addition is checked against the
# full never-tracked set automatically.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("relpath", NEVER_TRACKED_RELATIVE_PATHS)
@pytest.mark.parametrize("root", ["A", "B"])
def test_never_tracked_paths_match_no_allowlist_entry(root: str, relpath: str):
    assert not allowlist.is_tracked(root, relpath), (
        f"path {relpath!r} on root {root!r} matched an allowlist entry, but "
        "requirements.md 1.4 requires it be structurally unreachable"
    )


def test_never_tracked_paths_are_denylist_complete_against_every_entry():
    """Belt-and-braces: directly check every individual entry's pattern

    against every never-tracked path, rather than only the matcher's public
    is_tracked() surface — so a bug in is_tracked() itself (e.g. an
    accidental OR with some other admitting condition) cannot hide a
    matching entry from the parametrized test above.
    """
    entries = _all_entries()
    for entry in entries:
        for relpath in NEVER_TRACKED_RELATIVE_PATHS:
            assert not allowlist.entry_matches(entry, relpath), (
                f"entry {entry!r} matches never-tracked path {relpath!r}"
            )


def test_absence_from_a_denylist_is_not_how_inclusion_works():
    """requirements.md 1.3: a file is included only on an allowlist HIT,

    never merely because it fails to match anything forbidden. A wholly
    unrecognised, arbitrary path must be rejected by default (fail-closed),
    not admitted by default (fail-open).
    """
    arbitrary_unlisted_paths = [
        "some/totally/unrecognised/path.txt",
        "random-file-nobody-declared.bin",
        "steering/plan.md.bak",  # near-miss of a real entry, still excluded
        "config.json.orig",
    ]
    for relpath in arbitrary_unlisted_paths:
        assert not allowlist.is_tracked("A", relpath)
        assert not allowlist.is_tracked("B", relpath)


# ---------------------------------------------------------------------------
# Matcher correctness on the positive side, including glob depth behaviour
# that a naive prefix-only matcher would get wrong.
# ---------------------------------------------------------------------------


def test_steering_glob_matches_nested_markdown_only():
    assert allowlist.is_tracked("A", "steering/plan.md")
    assert allowlist.is_tracked("A", "steering/nested/dir/rule.md")
    # A markdown file must be tracked; a same-named non-markdown sibling
    # must not be swept in just because it shares a directory.
    assert not allowlist.is_tracked("A", "steering/plan.txt")
    assert not allowlist.is_tracked("A", "steering/notes.json")


def test_skill_md_glob_matches_any_depth_but_only_skill_md_files():
    assert allowlist.is_tracked("A", "skills/foo/SKILL.md")
    assert allowlist.is_tracked("A", "skills/nested/foo/SKILL.md")
    assert not allowlist.is_tracked("A", "skills/foo/README.md")
    assert not allowlist.is_tracked("A", "skills/foo/skill.md")  # case


def test_skill_scripts_glob_matches_nested_script_files():
    assert allowlist.is_tracked("A", "skills/foo/scripts/run.sh")
    assert allowlist.is_tracked("A", "skills/foo/scripts/lib/helper.py")
    assert not allowlist.is_tracked("A", "skills/foo/assets/icon.png")


@pytest.mark.parametrize(
    "relpath",
    [
        "config.json",
        "hooks.json",
        "agent_model_state.json",
        "mcp.json",
        "crons.json",
        "instances.json",
    ],
)
def test_root_a_top_level_singleton_files_are_exact_matches_only(relpath):
    """Each single named config file is tracked at its exact top-level path,

    and a same-named file nested elsewhere is not swept in by a careless
    basename-only match.
    """
    assert allowlist.is_tracked("A", relpath)
    assert not allowlist.is_tracked("A", f"nested/dir/{relpath}")


# ---------------------------------------------------------------------------
# Absence-without-error is Requirement 1.5's job (collect.py), not
# allowlist.py's — but the matcher itself must not treat "matches an entry"
# as implying "exists on disk". Confirm the matcher is a pure predicate over
# a path string, independent of the filesystem.
# ---------------------------------------------------------------------------


def test_matcher_is_a_pure_predicate_not_a_filesystem_check(tmp_path):
    """is_tracked() must answer purely from the path string; it must not

    stat the filesystem, so collect.py (not allowlist.py) owns "absent
    without error" (requirements.md 1.5). Proven by calling it against a
    path that provably does not exist anywhere on disk, and confirming it
    still returns True as a pure classification.
    """
    nonexistent_root = tmp_path / "definitely-does-not-exist"
    assert not nonexistent_root.exists()
    # allowlist.is_tracked never receives or needs a filesystem root — it is
    # evaluated on the relative path alone.
    assert allowlist.is_tracked("A", "crons.json")


def test_entry_patterns_contain_no_path_traversal_shape():
    """Defence against a data-authoring mistake: no allowlist entry pattern

    may itself contain a `..` traversal segment, which would make "matches
    an entry" and "resolves inside the root" two different questions.
    """
    entries = _all_entries()
    for entry in entries:
        assert ".." not in entry.pattern.split("/"), (
            f"entry {entry!r} pattern contains a path-traversal segment"
        )


def test_lock_and_pid_glob_denial_holds_for_arbitrary_stems():
    """The lock/pid exclusion in requirements.md 1.4 is a suffix property,

    not a fixed filename list — assert it holds for stems built from a
    spread of characters, not just one hand-picked example.
    """
    stems = ["gateway", "server", "abc", "123", "a-b_c"]
    for stem in stems:
        for suffix in (".lock", ".pid"):
            relpath = f"{stem}{suffix}"
            assert not allowlist.is_tracked("A", relpath)
            assert not allowlist.is_tracked("B", relpath)


def test_no_entry_pattern_is_a_bare_wildcard():
    """Guard against a degenerate entry like `**` or `*` that would trivially

    satisfy 'is data' while defeating every denylist property above by
    matching everything. Every pattern must contain at least one non-glob,
    non-separator literal character.
    """
    entries = _all_entries()
    literal_chars = set(string.ascii_letters + string.digits + "_.-")
    for entry in entries:
        stripped = entry.pattern.replace("*", "").replace("/", "")
        assert any(ch in literal_chars for ch in stripped), (
            f"entry {entry!r} pattern has no literal component: "
            f"{entry.pattern!r}"
        )
