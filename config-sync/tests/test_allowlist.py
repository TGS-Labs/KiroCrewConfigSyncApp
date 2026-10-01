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
import re
from pathlib import Path

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
    # Nested spellings under skills/**/scripts/** — H2 regression guard.
    # skills/**/scripts/** would otherwise admit these through its
    # trailing "**" segment; requirements.md 1.4 must hold even nested
    # under an otherwise-matching allowlist entry.
    "skills/my-skill/scripts/.env",
    "skills/my-skill/scripts/trust/sel_hmac.key",
    "skills/my-skill/scripts/id_rsa",
    "skills/my-skill/scripts/lib/id_rsa",
    "skills/nested/dir/my-skill/scripts/.env",
    "skills/my-skill/scripts/sessions/abc123.jsonl",
    "skills/my-skill/scripts/some.lock",
    "skills/my-skill/scripts/some.pid",
]


def _all_entries() -> list:
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


def test_allowlist_is_a_nonempty_data_collection() -> None:
    """ALLOWLIST is an iterable/sequence of entries, not executable logic."""
    entries = _all_entries()
    assert len(entries) > 0


def test_every_entry_declares_a_root() -> None:
    """Each entry names which configuration root (A or B) it belongs to."""
    entries = _all_entries()
    for entry in entries:
        assert hasattr(entry, "root"), (
            f"entry {entry!r} has no 'root' attribute; every allowlist "
            "entry must declare which root it tracks"
        )
        assert entry.root in ("A", "B")


def test_every_entry_has_a_propagation_class() -> None:
    """requirements.md 1.6: a test SHALL fail if any entry lacks a

    PropagationClass. This is that test.
    """
    entries = _all_entries()
    missing = [
        entry for entry in entries if getattr(entry, "propagation_class", None) is None
    ]
    assert not missing, (
        "the following allowlist entries have no PropagationClass: " f"{missing!r}"
    )


def test_propagation_class_is_one_of_the_defined_kinds() -> None:
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


# ---------------------------------------------------------------------------
# Exact per-entry classification: design.md's propagation table (see also
# requirements.md 5.2, 5.7, 5.8, 5.9) names exactly four non-collapsible
# propagation classes, and every entry must land in the one class design.md
# assigns it — not merely *some* defined class. A prior version of this test
# only checked "every entry has a class" (vacuous: it passed even when an
# entry carried the wrong class), so it could not catch a misclassification
# like tracking config.json as immediate or SKILL.md as new-session-only.
# This table pins every entry, by (root, pattern), to its exact class.
# ---------------------------------------------------------------------------

EXPECTED_CLASSIFICATION = {
    ("A", "steering/**/*.md"): allowlist.PropagationClass.LIVE_IN_NEW_SESSION,
    ("A", "skills/**/SKILL.md"): allowlist.PropagationClass.LIVE_IMMEDIATE,
    ("A", "skills/**/scripts/**"): allowlist.PropagationClass.LIVE_IMMEDIATE,
    ("A", "config.json"): allowlist.PropagationClass.LIVE_ON_NEXT_RESOLUTION,
    ("A", "hooks.json"): allowlist.PropagationClass.LIVE_ON_NEXT_RESOLUTION,
    (
        "A",
        "agent_model_state.json",
    ): allowlist.PropagationClass.LIVE_ON_NEXT_RESOLUTION,
    ("A", "mcp.json"): allowlist.PropagationClass.LIVE_ON_NEXT_RESOLUTION,
    ("A", "crons.json"): allowlist.PropagationClass.LIVE_ON_NEXT_RESOLUTION,
    ("A", "instances.json"): allowlist.PropagationClass.LIVE_ON_NEXT_RESOLUTION,
    (
        "A",
        "config-bundles/agent-prompts/*.md",
    ): allowlist.PropagationClass.LIVE_IN_NEW_SESSION,
    ("B", "agents/*.json"): allowlist.PropagationClass.LIVE_ON_NEXT_RESOLUTION,
}


def test_every_entry_is_pinned_to_its_exact_expected_propagation_class() -> None:
    """Pin every allowlist entry, by (root, pattern), to the exact

    PropagationClass design.md's propagation table assigns it — not merely
    to some defined class. Fails loudly on any future misclassification
    (e.g. re-introducing a fifth/renamed class, or moving an entry to the
    wrong one of the four).
    """
    entries = _all_entries()
    seen_keys = {(entry.root, entry.pattern) for entry in entries}

    assert seen_keys == set(EXPECTED_CLASSIFICATION), (
        "EXPECTED_CLASSIFICATION and the live ALLOWLIST have drifted apart — "
        f"missing from ALLOWLIST: {set(EXPECTED_CLASSIFICATION) - seen_keys}; "
        "missing from EXPECTED_CLASSIFICATION: "
        f"{seen_keys - set(EXPECTED_CLASSIFICATION)}"
    )

    for entry in entries:
        key = (entry.root, entry.pattern)
        expected = EXPECTED_CLASSIFICATION[key]
        assert entry.propagation_class is expected, (
            f"entry {key!r} is classified {entry.propagation_class!r}, but "
            f"design.md's propagation table requires {expected!r}"
        )


def test_exactly_four_propagation_classes_are_defined() -> None:
    """design.md's propagation table names exactly four non-collapsible

    states. A fifth class (or a collapse to fewer) is a taxonomy drift this
    test catches even before checking any individual entry.
    """
    assert {member.name for member in allowlist.PropagationClass} == {
        "LIVE_IN_NEW_SESSION",
        "LIVE_IMMEDIATE",
        "LIVE_WITHIN_60S",
        "LIVE_ON_NEXT_RESOLUTION",
    }


def test_root_a_entries_cover_every_required_config_class() -> None:
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
        "config-bundles/agent-prompts/senior-reviewer.md": True,
    }
    for relpath in required_hits:
        assert allowlist.is_tracked("A", relpath), (
            f"root A path {relpath!r} should be tracked per requirements.md "
            "1.1 but no allowlist entry admits it"
        )


def test_root_b_admits_only_agents_json_files() -> None:
    """requirements.md 1.2: root B tracks `agents/*.json` only.

    kiro_home()'s own contract is that only the agents directory follows
    KIRO_HOME; the allowlist must not widen that.
    """
    assert allowlist.is_tracked("B", "agents/my-agent.json")
    assert allowlist.is_tracked("B", "agents/senior-reviewer.json")

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


def test_root_b_entries_declare_root_b_only() -> None:
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
def test_never_tracked_paths_match_no_allowlist_entry(root: str, relpath: str) -> None:
    assert not allowlist.is_tracked(root, relpath), (
        f"path {relpath!r} on root {root!r} matched an allowlist entry, but "
        "requirements.md 1.4 requires it be structurally unreachable"
    )


def test_never_tracked_paths_are_denylist_complete_against_every_entry() -> None:
    """Belt-and-braces: directly check every individual entry's pattern

    against every never-tracked path, rather than only the matcher's public
    is_tracked() surface — so a bug in is_tracked() itself (e.g. an
    accidental OR with some other admitting condition) cannot hide a
    matching entry from the parametrized test above.
    """
    entries = _all_entries()
    for entry in entries:
        for relpath in NEVER_TRACKED_RELATIVE_PATHS:
            assert not allowlist.entry_matches(
                entry, relpath
            ), f"entry {entry!r} matches never-tracked path {relpath!r}"


def test_absence_from_a_denylist_is_not_how_inclusion_works() -> None:
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


def test_steering_glob_matches_nested_markdown_only() -> None:
    assert allowlist.is_tracked("A", "steering/plan.md")
    assert allowlist.is_tracked("A", "steering/nested/dir/rule.md")
    # A markdown file must be tracked; a same-named non-markdown sibling
    # must not be swept in just because it shares a directory.
    assert not allowlist.is_tracked("A", "steering/plan.txt")
    assert not allowlist.is_tracked("A", "steering/notes.json")


def test_skill_md_glob_matches_any_depth_but_only_skill_md_files() -> None:
    assert allowlist.is_tracked("A", "skills/foo/SKILL.md")
    assert allowlist.is_tracked("A", "skills/nested/foo/SKILL.md")
    assert not allowlist.is_tracked("A", "skills/foo/README.md")
    assert not allowlist.is_tracked("A", "skills/foo/skill.md")  # case


def test_skill_scripts_glob_matches_nested_script_files() -> None:
    assert allowlist.is_tracked("A", "skills/foo/scripts/run.sh")
    assert allowlist.is_tracked("A", "skills/foo/scripts/lib/helper.py")
    assert not allowlist.is_tracked("A", "skills/foo/assets/icon.png")


def test_skill_scripts_glob_never_readmits_never_tracked_shapes() -> None:
    """H2 regression guard: skills/**/scripts/**'s trailing "**" segment

    spans arbitrarily many nested components, so a naive translation of
    that glob re-admits a requirements.md-1.4 never-tracked path merely
    because it happens to live under some skill's scripts/ tree. A
    legitimate script must still match; a credential/secret/log-shaped
    basename nested at any depth underneath must not.
    """
    assert allowlist.is_tracked("A", "skills/foo/scripts/run.sh")
    never_tracked_nested = [
        "skills/my-skill/scripts/.env",
        "skills/my-skill/scripts/trust/sel_hmac.key",
        "skills/my-skill/scripts/id_rsa",
        "skills/my-skill/scripts/lib/id_rsa",
        "skills/nested/dir/my-skill/scripts/.env",
        "skills/my-skill/scripts/sessions/abc123.jsonl",
        "skills/my-skill/scripts/some.lock",
        "skills/my-skill/scripts/some.pid",
    ]
    for relpath in never_tracked_nested:
        assert not allowlist.is_tracked("A", relpath), (
            f"{relpath!r} matched skills/**/scripts/** but requirements.md "
            "1.4 requires it be structurally unreachable"
        )


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
def test_root_a_top_level_singleton_files_are_exact_matches_only(relpath: str) -> None:
    """Each single named config file is tracked at its exact top-level path,

    and a same-named file nested elsewhere is not swept in by a careless
    basename-only match.
    """
    assert allowlist.is_tracked("A", relpath)
    assert not allowlist.is_tracked("A", f"nested/dir/{relpath}")


# ---------------------------------------------------------------------------
# tasks.md 7.1 / requirements.md 1.1, 1.6, 1.7, 1.8, 5.10: root A tracks a
# direct `.md` child of `config-bundles/agent-prompts/` with
# LIVE_IN_NEW_SESSION, and nothing else under `config-bundles/` is admitted.
# ---------------------------------------------------------------------------


def test_agent_prompt_direct_child_is_tracked_with_live_in_new_session() -> None:
    """requirements.md 1.1, 1.7: a `.md` file directly inside

    `config-bundles/agent-prompts/` is tracked on root A, and carries the
    LIVE_IN_NEW_SESSION propagation class (an agent's prompt is read when a
    session for that agent starts — requirements.md 5.10).
    """
    relpath = "config-bundles/agent-prompts/senior-reviewer.md"
    assert allowlist.is_tracked("A", relpath)

    matching_entries = [
        entry
        for entry in _all_entries()
        if entry.root == "A" and allowlist.entry_matches(entry, relpath)
    ]
    assert matching_entries, f"no root-A entry matched {relpath!r}"
    for entry in matching_entries:
        assert (
            entry.propagation_class is allowlist.PropagationClass.LIVE_IN_NEW_SESSION
        ), (
            f"entry {entry!r} matched {relpath!r} but is classified "
            f"{entry.propagation_class!r}, not LIVE_IN_NEW_SESSION"
        )


@pytest.mark.parametrize(
    "relpath",
    [
        # requirements.md 1.7: a nested path under agent-prompts/ is not a
        # direct child and must not be selected.
        "config-bundles/agent-prompts/sub/x.md",
        # requirements.md 1.7: a non-.md file directly in agent-prompts/
        # must not be selected.
        "config-bundles/agent-prompts/x.txt",
        # requirements.md 1.8: nothing under config-bundles/skills/** is
        # selected, including a nested SKILL.md that would otherwise match
        # the unrelated skills/**/SKILL.md entry by basename alone.
        "config-bundles/skills/foo/SKILL.md",
        "config-bundles/skills/foo/scripts/run.sh",
        # requirements.md 1.8: config-bundles/sync-bundles.sh is not
        # selected — it is delivered by its own mechanism.
        "config-bundles/sync-bundles.sh",
    ],
)
def test_config_bundles_non_agent_prompt_paths_are_not_tracked_on_root_a(
    relpath: str,
) -> None:
    assert not allowlist.is_tracked("A", relpath), (
        f"path {relpath!r} must NOT be tracked on root A per requirements.md " "1.7/1.8"
    )


def test_agent_prompt_path_is_not_tracked_on_root_b() -> None:
    """The same prompt-shaped path under root B (KIRO_HOME) is not tracked —

    root B admits only `agents/*.json` (requirements.md 1.2), and this
    class belongs to root A only.
    """
    assert not allowlist.is_tracked(
        "B", "config-bundles/agent-prompts/senior-reviewer.md"
    )


# ---------------------------------------------------------------------------
# Absence-without-error is Requirement 1.5's job (collect.py), not
# allowlist.py's — but the matcher itself must not treat "matches an entry"
# as implying "exists on disk". Confirm the matcher is a pure predicate over
# a path string, independent of the filesystem.
# ---------------------------------------------------------------------------


def test_matcher_is_a_pure_predicate_not_a_filesystem_check(tmp_path: Path) -> None:
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


def test_entry_patterns_contain_no_path_traversal_shape() -> None:
    """Defence against a data-authoring mistake: no allowlist entry pattern

    may itself contain a `..` traversal segment, which would make "matches
    an entry" and "resolves inside the root" two different questions.
    """
    entries = _all_entries()
    for entry in entries:
        assert ".." not in entry.pattern.split(
            "/"
        ), f"entry {entry!r} pattern contains a path-traversal segment"


def test_lock_and_pid_glob_denial_holds_for_arbitrary_stems() -> None:
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


def test_no_entry_pattern_is_a_bare_wildcard() -> None:
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
            f"entry {entry!r} pattern has no literal component: " f"{entry.pattern!r}"
        )


class TestHostGeneratedAgentAliasesAreNeverTracked:
    """``~/.kiro/agents/kirocrew-skill-view-<hash>.json`` files are NOT operator
    configuration: the gateway derives them per launch from the skills
    directory (``kiro_crew.acp.skill_projection``,
    ``NATIVE_SKILL_ALIAS_PREFIX``) and rewrites them on every session. The
    first store-installed push (2026-10-01) swept 145 of them into the
    bundle PR, a different set on every box and every day. They are excluded
    structurally, like ``.env``: an excluded path was never an allowlist hit.
    """

    @pytest.mark.parametrize(
        "relpath",
        [
            "agents/kirocrew-skill-view-0079e507ed2296900727ad77.json",
            "agents/kirocrew-skill-view-ffffffffffffffffffffffff.json",
        ],
    )
    def test_skill_view_alias_is_not_tracked_under_root_b(self, relpath: str) -> None:
        assert allowlist.is_tracked("B", relpath) is False

    @pytest.mark.parametrize(
        "relpath",
        [
            "agents/senior-reviewer.json",
            "agents/kirocrew-skill-viewer.json",
            "agents/kirocrew-custom.json",
            "agents/my-kirocrew.json",
        ],
    )
    def test_real_agent_definitions_stay_tracked(self, relpath: str) -> None:
        assert allowlist.is_tracked("B", relpath) is True


_HOST_AGENT_FILES = Path(
    "/usr/local/lib/python3.12/site-packages/kiro_crew/agent_files.py"
)
_SHIPPED_AGENT_FILENAMES = (
    "kirocrew.json",
    "kirocrew-lite.json",
    "kirocrew-guest.json",
    "kirocrew-conductor.json",
    "kirocrew-pipeline-conductor.json",
    "kirocrew-ledger-conductor.json",
    "kirocrew-security-conductor.json",
    "kirocrew-worker.json",
    "kirocrew-knowledge.json",
    "kirocrew-research.json",
    "kirocrew-heartbeat.json",
)


class TestHostShippedAgentsAreNeverTracked:
    """The gateway writes these agent files itself and owns their
    registration. Synced, they failed the registration check and blocked the
    shared ``config.json``/``agent_model_state.json`` for every other agent
    (first live tick, 2026-10-01: 47 files refused). Excluded structurally,
    like the skill-view aliases."""

    @pytest.mark.parametrize("filename", _SHIPPED_AGENT_FILENAMES)
    def test_shipped_agent_file_is_not_tracked(self, filename: str) -> None:
        assert allowlist.is_tracked("B", f"agents/{filename}") is False

    def test_list_matches_the_hosts_own_agent_filename_constants(self) -> None:
        """A new host-shipped agent must fail here, not on a live tick."""
        if not _HOST_AGENT_FILES.is_file():
            pytest.skip("KiroCrew host not installed here")
        text = _HOST_AGENT_FILES.read_text(encoding="utf-8")
        host = set(re.findall(r'^[A-Z_]*AGENT_FILENAME = "([^"]+)"', text, re.M))
        assert host, "no *_AGENT_FILENAME constants found in the host"
        assert host == set(_SHIPPED_AGENT_FILENAMES)
