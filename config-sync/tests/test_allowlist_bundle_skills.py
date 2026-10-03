"""Tests for tracking config-bundles/skills/** with a live-path remap.

Operator ruling (2026-10-03, superseding requirements.md 1.8's prior
exclusion): a skill authored under a sub-bundle's own ``skills/`` directory
(e.g. ``tgs-labs/skills/sdlc/``) is flattened by ``sync-bundles.sh`` into
``config-bundles/skills/<name>/`` on this host, but that staging path was
never tracked by Config Sync's own allowlist and is NOT where
``skill_search``/the gateway read skills from (``~/.kiro/crew/skills/``).
A skill could sit in ``config-bundles/skills/`` indefinitely without ever
becoming loadable.

This widens root A's allowlist to track ``config-bundles/skills/**/
SKILL.md`` and ``config-bundles/skills/**/scripts/**`` (mirroring the
existing ``skills/**/SKILL.md`` / ``skills/**/scripts/**`` entries), and
teaches the entry to WRITE at a different relpath than it was READ at:
collected (push-side) under its real on-disk path (``config-bundles/
skills/<name>/SKILL.md``), but APPLIED (pull-side) at ``skills/<name>/
SKILL.md`` — the live path the gateway and skill_search actually scan.
``skills/**`` itself is unaffected: identity remap, no behaviour change.
"""

from __future__ import annotations

from backend import allowlist
from backend.allowlist import PropagationClass


# ---------------------------------------------------------------------------
# New entries exist and are tracked.
# ---------------------------------------------------------------------------


def test_config_bundles_skill_md_is_tracked_on_root_a() -> None:
    assert allowlist.is_tracked("A", "config-bundles/skills/sdlc/SKILL.md")


def test_config_bundles_skill_scripts_are_tracked_on_root_a() -> None:
    assert allowlist.is_tracked("A", "config-bundles/skills/sdlc/scripts/run.sh")


def test_config_bundles_skill_md_entry_has_live_immediate_class() -> None:
    matching = [
        entry
        for entry in allowlist.ALLOWLIST
        if entry.root == "A"
        and allowlist.entry_matches(entry, "config-bundles/skills/sdlc/SKILL.md")
    ]
    assert matching, "no root-A entry matched the new config-bundles/skills/ path"
    for entry in matching:
        assert entry.propagation_class is PropagationClass.LIVE_IMMEDIATE


# ---------------------------------------------------------------------------
# Never-tracked exclusions still apply even nested under the new entry.
# ---------------------------------------------------------------------------


def test_config_bundles_skill_scripts_never_readmit_never_tracked_shapes() -> None:
    assert allowlist.is_tracked("A", "config-bundles/skills/sdlc/scripts/run.sh")
    never_tracked_nested = [
        "config-bundles/skills/sdlc/scripts/.env",
        "config-bundles/skills/sdlc/scripts/id_rsa",
        "config-bundles/skills/sdlc/scripts/some.lock",
        "config-bundles/skills/sdlc/scripts/some.pid",
    ]
    for relpath in never_tracked_nested:
        assert not allowlist.is_tracked("A", relpath), (
            f"{relpath!r} must stay structurally unreachable even under the "
            "new config-bundles/skills/**/scripts/** entry"
        )


def test_config_bundles_skill_md_is_case_sensitive() -> None:
    assert not allowlist.is_tracked("A", "config-bundles/skills/sdlc/skill.md")


# ---------------------------------------------------------------------------
# Non-skill config-bundles/ paths stay untracked (requirements.md 1.8's
# other half — only skills/** under config-bundles/ is now tracked, not
# sync-bundles.sh or an arbitrary other path).
# ---------------------------------------------------------------------------


def test_config_bundles_sync_script_still_not_tracked() -> None:
    assert not allowlist.is_tracked("A", "config-bundles/sync-bundles.sh")


def test_config_bundles_agent_prompts_entry_is_unaffected() -> None:
    assert allowlist.is_tracked("A", "config-bundles/agent-prompts/senior-reviewer.md")


# ---------------------------------------------------------------------------
# The write-relpath remap: collected identity vs. applied (live) path.
# ---------------------------------------------------------------------------


def test_write_relpath_for_ordinary_skills_entry_is_identity() -> None:
    """Every PRE-EXISTING entry (skills/**, steering/**, etc.) must resolve

    its write relpath to the SAME string it was matched on — this remap
    is scoped to exactly the new config-bundles/skills/** entries, and
    must not silently change behaviour for anything else.
    """
    ordinary_paths = [
        "skills/sdlc/SKILL.md",
        "skills/sdlc/scripts/run.sh",
        "steering/plan.md",
        "config.json",
        "config-bundles/agent-prompts/senior-reviewer.md",
    ]
    for relpath in ordinary_paths:
        assert allowlist.write_relpath("A", relpath) == relpath


def test_write_relpath_for_config_bundles_skill_md_strips_the_staging_prefix() -> None:
    """A collected config-bundles/skills/<name>/SKILL.md must resolve to

    the LIVE skills/<name>/SKILL.md path when applied — never written to
    config-bundles/skills/ itself, which skill_search never scans.
    """
    assert (
        allowlist.write_relpath("A", "config-bundles/skills/sdlc/SKILL.md")
        == "skills/sdlc/SKILL.md"
    )


def test_write_relpath_for_config_bundles_skill_scripts_strips_the_staging_prefix() -> (
    None
):
    assert (
        allowlist.write_relpath("A", "config-bundles/skills/sdlc/scripts/run.sh")
        == "skills/sdlc/scripts/run.sh"
    )


def test_write_relpath_for_nested_skill_name_strips_only_the_fixed_prefix() -> None:
    """Only the literal ``config-bundles/`` prefix is stripped — a skill

    name that itself contains slashes-shaped segments (there are none in
    practice, since skill names are flat directory basenames, but the
    remap must not accidentally eat more than the fixed prefix) resolves
    correctly for the deepest case this entry's glob admits.
    """
    assert (
        allowlist.write_relpath(
            "A", "config-bundles/skills/kirocrew-dev/writing-tests/SKILL.md"
        )
        == "skills/kirocrew-dev/writing-tests/SKILL.md"
    )


def test_write_relpath_on_root_b_is_always_identity() -> None:
    """Root B has no config-bundles/skills/ entry at all; its one entry

    (agents/*.json) must resolve to itself unchanged.
    """
    assert allowlist.write_relpath("B", "agents/my-agent.json") == (
        "agents/my-agent.json"
    )


def test_write_relpath_for_untracked_path_is_identity_fallback() -> None:
    """A path matching no entry at all (should never reach apply.py in

    practice, since apply.py gates on is_tracked first) still returns a
    safe identity fallback rather than raising, so a caller that calls
    write_relpath before checking is_tracked does not crash.
    """
    assert allowlist.write_relpath("A", "totally/unrecognised/path.txt") == (
        "totally/unrecognised/path.txt"
    )
