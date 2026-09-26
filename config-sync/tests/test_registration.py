"""Tests for backend/registration.py (tasks.md 5.4).

Covers design.md's "Agent registration is the one class that is *not* a
single file. The full registration is four coordinated writes ... The
applier treats those four as one transaction: all four present, or the
registration is refused as incomplete." and requirements.md:

- 5.6: WHEN a pulled change adds or modifies an agent registration THEN the
  app SHALL apply all four parts of that registration together — the
  prompt file, the ``~/.kiro/agents/<name>.json`` definition, the
  ``config.json`` ``agents{}`` entry, and the ``agent_model_state.json``
  pin — and WHEN any one of the four is missing from the commit THEN the
  app SHALL refuse to apply that registration and report it as incomplete
  rather than leaving a partially registered agent.
- 5.7: WHEN an agent registration is applied THEN the app SHALL report that
  ``spawn_run``'s roster picks it up without a restart (the agents
  directory is cached on a file-count and newest-mtime signature) and that
  the dashboard's agent picker is a separate read path that may need its
  own refresh.

The partial-refusal-doesn't-block-siblings property (design.md's error
table: "Incomplete agent registration (fewer than 4 parts) | That
registration refused and reported; other files still apply") is exercised
directly: a commit that mixes a broken/incomplete registration with
unrelated tracked files must refuse only the registration while the
unrelated files still go through.

This module intentionally imports ``backend.registration``, which does not
exist yet. Every test below is expected to fail at collection time with a
``ModuleNotFoundError`` until software-engineer implements it — this is the
correct TDD starting state, not a test defect.

Real fixture file sets are built on ``tmp_path`` (no mocks): a registration
is represented as a set of real files under a fake commit-scoped directory
tree so the module under test reads and reasons about real bytes on disk,
matching design.md's four coordinated writes:

    <root>/prompt-file>                      the agent's prompt file
    <root>/agents/<name>.json                ~/.kiro/agents/<name>.json
    <root>/config.json                       config.json (agents{} entry)
    <root>/agent_model_state.json            the model pin
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend import registration


# ---------------------------------------------------------------------------
# Fixture helpers — build a real four-part registration file set on disk.
# ---------------------------------------------------------------------------


AGENT_NAME = "example-agent"
PROMPT_RELPATH = f"agent-prompts/{AGENT_NAME}.md"
AGENT_DEF_RELPATH = f"agents/{AGENT_NAME}.json"
CONFIG_RELPATH = "config.json"
MODEL_STATE_RELPATH = "agent_model_state.json"

ALL_FOUR_RELPATHS = (
    PROMPT_RELPATH,
    AGENT_DEF_RELPATH,
    CONFIG_RELPATH,
    MODEL_STATE_RELPATH,
)


def _write_prompt_file(root: Path, agent_name: str = AGENT_NAME) -> Path:
    path = root / "agent-prompts" / f"{agent_name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {agent_name}\n\nYou are {agent_name}.\n", encoding="utf-8")
    return path


def _write_agent_def(root: Path, agent_name: str = AGENT_NAME) -> Path:
    path = root / "agents" / f"{agent_name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "name": agent_name,
                "description": "An example agent.",
                "prompt": f"file://agent-prompts/{agent_name}.md",
                "tools": ["read", "write"],
                "allowedTools": ["read"],
                "resources": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def _read_json_if_exists(path: Path) -> dict:
    if not path.exists():
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _write_config_json(root: Path, agent_name: str = AGENT_NAME) -> Path:
    """Add/update ``agent_name``'s ``agents{}`` entry in the SHARED
    ``config.json``, merging into whatever is already on disk rather than
    overwriting it -- ``config.json`` is one file every agent's
    registration shares (requirements.md 5.6 names the ``agents{}``
    *entry*, not the file's mere presence), so a second call for a
    different agent in the same fixture must not erase the first
    agent's entry.
    """
    path = root / "config.json"
    doc = _read_json_if_exists(path)
    agents: dict = doc.setdefault("agents", {})
    agents[agent_name] = {"source": "local", "model": "claude-sonnet-5"}
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return path


def _write_agent_model_state(root: Path, agent_name: str = AGENT_NAME) -> Path:
    """Add/update ``agent_name``'s pin in the SHARED
    ``agent_model_state.json``, merging into whatever is already on disk
    rather than overwriting it -- same shared-file rationale as
    ``_write_config_json`` above.
    """
    path = root / "agent_model_state.json"
    pins = _read_json_if_exists(path)
    pins[agent_name] = {"model_managed": False, "model": "claude-sonnet-5"}
    path.write_text(json.dumps(pins, indent=2), encoding="utf-8")
    return path


def _write_config_json_without_agent_key(
    root: Path, present_agent_name: str, missing_agent_name: str
) -> Path:
    """Write a SHARED ``config.json`` that exists in the commit and
    carries ``present_agent_name``'s ``agents{}`` entry, but deliberately
    does NOT carry ``missing_agent_name``'s entry.

    This is the case that distinguishes presence-only semantics from
    content-inspection semantics (requirements.md 5.6): the file is
    present and changed, but the SPECIFIC agent's entry inside it is
    not. A presence-only implementation reports ``missing_agent_name``'s
    config part as satisfied merely because ``config.json`` is in
    ``changed_paths``; a content-inspecting implementation must still
    report it missing because the key itself is absent.
    """
    path = root / "config.json"
    doc = _read_json_if_exists(path)
    agents: dict = doc.setdefault("agents", {})
    agents[present_agent_name] = {"source": "local", "model": "claude-sonnet-5"}
    agents.pop(missing_agent_name, None)
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return path


def _write_agent_model_state_without_agent_key(
    root: Path, present_agent_name: str, missing_agent_name: str
) -> Path:
    """Write a SHARED ``agent_model_state.json`` that exists in the
    commit and carries ``present_agent_name``'s pin, but deliberately
    does NOT carry ``missing_agent_name``'s pin. See
    ``_write_config_json_without_agent_key`` for the rationale.
    """
    path = root / "agent_model_state.json"
    pins = _read_json_if_exists(path)
    pins[present_agent_name] = {"model_managed": False, "model": "claude-sonnet-5"}
    pins.pop(missing_agent_name, None)
    path.write_text(json.dumps(pins, indent=2), encoding="utf-8")
    return path


def _write_full_registration(root: Path, agent_name: str = AGENT_NAME) -> None:
    """Write all four parts of one agent's registration under ``root``."""
    _write_prompt_file(root, agent_name)
    _write_agent_def(root, agent_name)
    _write_config_json(root, agent_name)
    _write_agent_model_state(root, agent_name)


def _write_unrelated_file(root: Path, relpath: str = "steering/unrelated.md") -> Path:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Unrelated\n\nNot part of any registration.\n", encoding="utf-8")
    return path


def _changed_relpaths_for(root: Path, *relpaths: str) -> list[str]:
    """The subset of ``relpaths`` that exist under ``root`` (the "commit")."""
    return [rp for rp in relpaths if (root / rp).exists()]


# ---------------------------------------------------------------------------
# (a) All four parts present -> registration is complete.
# ---------------------------------------------------------------------------


class TestCompleteRegistration:
    """All four parts present in the commit -> reported complete."""

    def test_all_four_parts_present_is_complete(self, tmp_path: Path) -> None:
        root = tmp_path / "commit"
        _write_full_registration(root)
        changed = _changed_relpaths_for(root, *ALL_FOUR_RELPATHS)

        result = registration.check_registrations(root, changed)

        assert result.complete_agents == [AGENT_NAME]
        assert result.incomplete_agents == {}

    def test_complete_registration_names_no_missing_parts(self, tmp_path: Path) -> None:
        root = tmp_path / "commit"
        _write_full_registration(root)
        changed = _changed_relpaths_for(root, *ALL_FOUR_RELPATHS)

        result = registration.check_registrations(root, changed)

        assert AGENT_NAME not in result.incomplete_agents


# ---------------------------------------------------------------------------
# (b) Each single part missing (4 cases) -> incomplete, naming THAT part.
# ---------------------------------------------------------------------------


class TestSinglePartMissing:
    """Exactly one of the four parts absent from the commit -> incomplete,
    and the report names precisely the missing part — not a generic
    failure and not any of the three present parts."""

    def test_missing_prompt_file_is_incomplete_and_named(self, tmp_path: Path) -> None:
        root = tmp_path / "commit"
        _write_full_registration(root)
        changed = _changed_relpaths_for(
            root, AGENT_DEF_RELPATH, CONFIG_RELPATH, MODEL_STATE_RELPATH
        )

        result = registration.check_registrations(root, changed)

        assert AGENT_NAME not in result.complete_agents
        assert AGENT_NAME in result.incomplete_agents
        missing = result.incomplete_agents[AGENT_NAME]
        assert "prompt" in " ".join(missing).lower()
        assert not any("agents/" in m and m.endswith(".json") for m in missing)

    def test_missing_agent_definition_is_incomplete_and_named(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "commit"
        _write_full_registration(root)
        changed = _changed_relpaths_for(
            root, PROMPT_RELPATH, CONFIG_RELPATH, MODEL_STATE_RELPATH
        )

        result = registration.check_registrations(root, changed)

        assert AGENT_NAME not in result.complete_agents
        assert AGENT_NAME in result.incomplete_agents
        missing = result.incomplete_agents[AGENT_NAME]
        joined = " ".join(missing).lower()
        assert "agent" in joined and (
            "definition" in joined or "agents/" in joined or ".json" in joined
        )

    def test_missing_config_json_entry_is_incomplete_and_named(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "commit"
        _write_full_registration(root)
        changed = _changed_relpaths_for(
            root, PROMPT_RELPATH, AGENT_DEF_RELPATH, MODEL_STATE_RELPATH
        )

        result = registration.check_registrations(root, changed)

        assert AGENT_NAME not in result.complete_agents
        assert AGENT_NAME in result.incomplete_agents
        missing = result.incomplete_agents[AGENT_NAME]
        joined = " ".join(missing).lower()
        assert "config.json" in joined or "config" in joined

    def test_missing_model_state_pin_is_incomplete_and_named(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "commit"
        _write_full_registration(root)
        changed = _changed_relpaths_for(
            root, PROMPT_RELPATH, AGENT_DEF_RELPATH, CONFIG_RELPATH
        )

        result = registration.check_registrations(root, changed)

        assert AGENT_NAME not in result.complete_agents
        assert AGENT_NAME in result.incomplete_agents
        missing = result.incomplete_agents[AGENT_NAME]
        joined = " ".join(missing).lower()
        assert "model_state" in joined or "model state" in joined or "pin" in joined

    @pytest.mark.parametrize(
        "missing_relpath",
        ALL_FOUR_RELPATHS,
        ids=["missing_prompt", "missing_agent_def", "missing_config", "missing_pin"],
    )
    def test_exactly_one_missing_part_is_never_reported_as_present(
        self, tmp_path: Path, missing_relpath: str
    ) -> None:
        """Whichever single part is missing, the other three must NOT also
        be reported missing -- the report is precise, not a blanket
        'something is wrong' flag."""
        root = tmp_path / "commit"
        _write_full_registration(root)
        present = [rp for rp in ALL_FOUR_RELPATHS if rp != missing_relpath]
        changed = _changed_relpaths_for(root, *present)

        result = registration.check_registrations(root, changed)

        missing = result.incomplete_agents[AGENT_NAME]
        assert len(missing) == 1


# ---------------------------------------------------------------------------
# (c) Two parts missing -> incomplete, naming BOTH.
# ---------------------------------------------------------------------------


class TestTwoPartsMissing:
    """Two of the four parts absent -> incomplete, naming both missing
    parts (not just one, not zero)."""

    def test_missing_prompt_and_config_names_both(self, tmp_path: Path) -> None:
        root = tmp_path / "commit"
        _write_full_registration(root)
        changed = _changed_relpaths_for(root, AGENT_DEF_RELPATH, MODEL_STATE_RELPATH)

        result = registration.check_registrations(root, changed)

        assert AGENT_NAME not in result.complete_agents
        assert AGENT_NAME in result.incomplete_agents
        missing = result.incomplete_agents[AGENT_NAME]
        assert len(missing) == 2
        joined = " ".join(missing).lower()
        assert "prompt" in joined
        assert "config" in joined

    def test_missing_agent_def_and_model_state_names_both(self, tmp_path: Path) -> None:
        root = tmp_path / "commit"
        _write_full_registration(root)
        changed = _changed_relpaths_for(root, PROMPT_RELPATH, CONFIG_RELPATH)

        result = registration.check_registrations(root, changed)

        assert AGENT_NAME not in result.complete_agents
        assert AGENT_NAME in result.incomplete_agents
        missing = result.incomplete_agents[AGENT_NAME]
        assert len(missing) == 2
        joined = " ".join(missing).lower()
        assert "agent" in joined
        assert "model_state" in joined or "model state" in joined or "pin" in joined


# ---------------------------------------------------------------------------
# (d) Complete registration PLUS unrelated files -> registration complete
#     AND unrelated files still pass through.
# ---------------------------------------------------------------------------


class TestCompleteRegistrationWithUnrelatedFiles:
    """A commit carrying a complete registration alongside unrelated
    tracked files must report the registration complete and must not
    swallow or otherwise flag the unrelated files as part of the
    registration check."""

    def test_complete_registration_plus_unrelated_files_is_complete(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "commit"
        _write_full_registration(root)
        _write_unrelated_file(root, "steering/unrelated.md")
        _write_unrelated_file(root, "skills/some-skill/SKILL.md")
        changed = _changed_relpaths_for(
            root,
            *ALL_FOUR_RELPATHS,
            "steering/unrelated.md",
            "skills/some-skill/SKILL.md",
        )

        result = registration.check_registrations(root, changed)

        assert result.complete_agents == [AGENT_NAME]
        assert result.incomplete_agents == {}

    def test_unrelated_files_are_reported_as_passthrough_not_blocked(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "commit"
        _write_full_registration(root)
        _write_unrelated_file(root, "steering/unrelated.md")
        changed = _changed_relpaths_for(
            root, *ALL_FOUR_RELPATHS, "steering/unrelated.md"
        )

        result = registration.check_registrations(root, changed)

        assert "steering/unrelated.md" in result.unrelated_paths
        assert "steering/unrelated.md" not in result.blocked_paths


# ---------------------------------------------------------------------------
# (e) Incomplete registration PLUS unrelated files -> registration refused
#     BUT unrelated files still pass through. The partial-refusal-doesn't-
#     block-siblings property.
# ---------------------------------------------------------------------------


class TestIncompleteRegistrationDoesNotBlockUnrelatedFiles:
    """design.md's error-handling table: 'Incomplete agent registration
    (fewer than 4 parts) | That registration refused and reported; other
    files still apply.' An incomplete registration must refuse only the
    registration's own four paths -- every unrelated changed path in the
    same commit must still be reported as passing through untouched."""

    def test_incomplete_registration_refused_unrelated_still_pass(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "commit"
        _write_full_registration(root)
        _write_unrelated_file(root, "steering/unrelated.md")
        _write_unrelated_file(root, "hooks.json")
        # Omit the prompt file -> incomplete registration.
        changed = _changed_relpaths_for(
            root,
            AGENT_DEF_RELPATH,
            CONFIG_RELPATH,
            MODEL_STATE_RELPATH,
            "steering/unrelated.md",
            "hooks.json",
        )

        result = registration.check_registrations(root, changed)

        assert AGENT_NAME not in result.complete_agents
        assert AGENT_NAME in result.incomplete_agents
        assert "steering/unrelated.md" in result.unrelated_paths
        assert "hooks.json" in result.unrelated_paths
        assert "steering/unrelated.md" not in result.blocked_paths
        assert "hooks.json" not in result.blocked_paths

    def test_incomplete_registration_blocks_only_its_own_parts(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "commit"
        _write_full_registration(root)
        # Omit config.json -> incomplete registration.
        changed = _changed_relpaths_for(
            root, PROMPT_RELPATH, AGENT_DEF_RELPATH, MODEL_STATE_RELPATH
        )

        result = registration.check_registrations(root, changed)

        # The registration's own present parts must be refused/blocked from
        # applying as a partial registration -- never applied piecemeal.
        assert PROMPT_RELPATH in result.blocked_paths
        assert AGENT_DEF_RELPATH in result.blocked_paths
        assert MODEL_STATE_RELPATH in result.blocked_paths
        assert CONFIG_RELPATH not in result.blocked_paths  # never present

    def test_two_agents_one_complete_one_incomplete_in_same_commit(
        self, tmp_path: Path
    ) -> None:
        """A commit touching two different agents' registrations, one
        complete and one missing a part, must report each independently:
        the complete one applies, the incomplete one is refused, and
        neither result leaks into the other's report."""
        root = tmp_path / "commit"
        _write_full_registration(root, agent_name="agent-one")
        _write_prompt_file(root, agent_name="agent-two")
        _write_agent_def(root, agent_name="agent-two")
        _write_config_json(root, agent_name="agent-two")
        # agent-two's model-state pin is deliberately omitted.

        changed = _changed_relpaths_for(
            root,
            "agent-prompts/agent-one.md",
            "agents/agent-one.json",
            "config.json",
            "agent_model_state.json",
            "agent-prompts/agent-two.md",
            "agents/agent-two.json",
        )

        result = registration.check_registrations(root, changed)

        assert "agent-one" in result.complete_agents
        assert "agent-two" not in result.complete_agents
        assert "agent-two" in result.incomplete_agents
        assert "agent-one" not in result.incomplete_agents

    def test_shared_file_present_but_missing_this_agents_key_is_incomplete(
        self, tmp_path: Path
    ) -> None:
        """The case that distinguishes presence-only semantics from
        content-inspection semantics (requirements.md 5.6 names the
        ``agents{}`` ENTRY and the model-pin PIN -- entries inside a
        shared file, not the file's mere presence).

        ``config.json`` and ``agent_model_state.json`` both exist in the
        commit -- and both carry ``agent-other``'s entry/pin, proving the
        files themselves are legitimately changed -- but neither carries
        ``agent-under-test``'s own key. A presence-only implementation
        would see ``config.json``/``agent_model_state.json`` in
        ``changed_paths`` and wrongly credit ``agent-under-test`` with
        those two parts; the correct (content-inspecting) implementation
        must still report both parts missing for ``agent-under-test``,
        because its key is absent from both files' content.
        """
        root = tmp_path / "commit"
        agent_under_test = "agent-under-test"
        agent_other = "agent-other"

        _write_prompt_file(root, agent_name=agent_under_test)
        _write_agent_def(root, agent_name=agent_under_test)
        # config.json / agent_model_state.json exist and are genuinely
        # changed -- but only for agent_other, never for agent_under_test.
        _write_config_json_without_agent_key(
            root,
            present_agent_name=agent_other,
            missing_agent_name=agent_under_test,
        )
        _write_agent_model_state_without_agent_key(
            root,
            present_agent_name=agent_other,
            missing_agent_name=agent_under_test,
        )

        changed = _changed_relpaths_for(
            root,
            f"agent-prompts/{agent_under_test}.md",
            f"agents/{agent_under_test}.json",
            CONFIG_RELPATH,
            MODEL_STATE_RELPATH,
        )

        result = registration.check_registrations(root, changed)

        assert agent_under_test not in result.complete_agents
        assert agent_under_test in result.incomplete_agents
        missing = result.incomplete_agents[agent_under_test]
        joined = " ".join(missing).lower()
        assert "config" in joined
        assert "model_state" in joined or "model state" in joined or "pin" in joined


# ---------------------------------------------------------------------------
# (f2) Malformed/unreadable shared file -> fail closed, never counted
#      present. backend.registration._load_json_object returns {} on any
#      (OSError, ValueError) -- a corrupted shared file must never let a
#      registration appear complete.
# ---------------------------------------------------------------------------


class TestMalformedSharedFileFailsClosed:
    """A shared file (``config.json`` / ``agent_model_state.json``) that
    exists in the commit but does not parse as JSON must be treated the
    same as carrying no agent's entry at all -- never as if it happened
    to carry one."""

    @pytest.mark.parametrize(
        "shared_relpath",
        (CONFIG_RELPATH, MODEL_STATE_RELPATH),
        ids=["malformed_config_json", "malformed_agent_model_state_json"],
    )
    def test_malformed_shared_file_is_incomplete_and_names_that_part(
        self, tmp_path: Path, shared_relpath: str
    ) -> None:
        root = tmp_path / "commit"
        _write_full_registration(root)
        _write_unrelated_file(root, "steering/unrelated.md")

        # Corrupt exactly one shared file with invalid JSON, in place.
        (root / shared_relpath).write_text("{not json", encoding="utf-8")

        changed = _changed_relpaths_for(
            root,
            *ALL_FOUR_RELPATHS,
            "steering/unrelated.md",
        )

        result = registration.check_registrations(root, changed)

        assert AGENT_NAME not in result.complete_agents
        assert AGENT_NAME in result.incomplete_agents
        missing = result.incomplete_agents[AGENT_NAME]
        joined = " ".join(missing).lower()
        config_missing = "config" in joined
        model_state_missing = (
            "model_state" in joined or "model state" in joined or "pin" in joined
        )
        if shared_relpath == CONFIG_RELPATH:
            assert config_missing
            assert not model_state_missing
        else:
            assert model_state_missing
            assert not config_missing
        assert len(missing) == 1

        # Unrelated files in the same commit still pass through untouched.
        assert "steering/unrelated.md" in result.unrelated_paths
        assert "steering/unrelated.md" not in result.blocked_paths


# ---------------------------------------------------------------------------
# (f) requirements.md 5.7 — roster-pickup / dashboard-refresh reporting.
# ---------------------------------------------------------------------------


class TestRosterPickupReporting:
    """requirements.md 5.7: an applied registration must report that
    spawn_run's roster picks it up without a restart, while the dashboard
    picker is a separate read path that may need its own refresh. This is
    reported only for a registration that actually completed -- an
    incomplete/refused registration was never applied, so it must not
    claim any propagation state."""

    def test_complete_registration_reports_roster_pickup_without_restart(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "commit"
        _write_full_registration(root)
        changed = _changed_relpaths_for(root, *ALL_FOUR_RELPATHS)

        result = registration.check_registrations(root, changed)

        report = result.propagation_report[AGENT_NAME]
        lowered = report.lower()
        assert "restart" not in lowered or "without" in lowered
        assert "spawn_run" in lowered or "roster" in lowered

    def test_complete_registration_reports_dashboard_picker_may_need_refresh(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "commit"
        _write_full_registration(root)
        changed = _changed_relpaths_for(root, *ALL_FOUR_RELPATHS)

        result = registration.check_registrations(root, changed)

        report = result.propagation_report[AGENT_NAME]
        lowered = report.lower()
        assert "dashboard" in lowered
        assert "refresh" in lowered

    def test_incomplete_registration_has_no_propagation_report_entry(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "commit"
        _write_full_registration(root)
        changed = _changed_relpaths_for(
            root, AGENT_DEF_RELPATH, CONFIG_RELPATH, MODEL_STATE_RELPATH
        )

        result = registration.check_registrations(root, changed)

        assert AGENT_NAME not in result.propagation_report


# ---------------------------------------------------------------------------
# (g) No registration-shaped paths in the commit at all.
# ---------------------------------------------------------------------------


class TestNoRegistrationInCommit:
    """A commit touching none of the four registration path shapes must
    report zero complete and zero incomplete agents, and must not treat
    any changed path as blocked."""

    def test_only_unrelated_files_yields_no_agents_and_no_blocking(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "commit"
        _write_unrelated_file(root, "steering/unrelated.md")
        _write_unrelated_file(root, "config.json")
        changed = _changed_relpaths_for(root, "steering/unrelated.md", "config.json")

        result = registration.check_registrations(root, changed)

        assert result.complete_agents == []
        assert result.incomplete_agents == {}
        assert result.blocked_paths == []
        assert "steering/unrelated.md" in result.unrelated_paths
        assert "config.json" in result.unrelated_paths
