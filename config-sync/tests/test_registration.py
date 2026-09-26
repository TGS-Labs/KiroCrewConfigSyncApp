"""Tests for backend/registration.py (tasks.md 5.4, amended by 7.5).

Covers design.md's "Agent registration is the one class that is *not* a
single file ... The applier treats those parts as one transaction: every
required part present, or the registration is refused as incomplete." and
requirements.md 5.6-5.7, 5.11-5.14:

- 5.6: WHEN a pulled change adds or modifies an agent registration THEN the
  app SHALL apply every required part of that registration together — the
  ``~/.kiro/agents/<name>.json`` definition, the ``config.json``
  ``agents{}`` entry, the ``agent_model_state.json`` pin, and the prompt
  file the definition references only where 5.11 makes it a required part
  — and WHEN any required part is missing THEN the app SHALL refuse to
  apply that registration and report it as incomplete. A shared part
  (``config.json`` entry, ``agent_model_state.json`` pin) is judged by
  that file's content AS IT EXISTS IN THE COMMIT'S TREE, regardless of
  whether the file is itself among the commit's changed paths (5.14).
- 5.7: an applied registration reports that ``spawn_run``'s roster picks
  it up without a restart, and that the dashboard's agent picker is a
  separate read path that may need its own refresh.
- 5.11: the prompt part is derived from the committed ``agents/<name
  >.json``'s own ``prompt`` value, never from the agent's name. A
  ``file://`` value resolving (5.12) to a tracked relpath is REQUIRED;
  an inline string, or an absent/``null``/empty ``prompt``, is satisfied
  with no file; a ``file://`` value resolving to an untracked relpath or
  to neither root is not required but is reported by agent name as an
  untracked location; a present-but-non-string-non-null ``prompt`` makes
  the registration incomplete.
- 5.12: prompt resolution accepts both the token form
  (``file://${KIROCREW_HOME}/<rel>``) and this host's absolute form
  (``file://<root A or B>/<rel>``); an empty or ``..`` segment, or an
  unparseable ``agents/<name>.json``, fails closed to incomplete.
- 5.13: a registration candidate is named only by a changed
  ``agents/<name>.json``; a changed prompt file with no changed
  referencing definition is an ordinary tracked file, not a registration.
- 5.14: a shared file is read from its path in the commit's checked-out
  tree, regardless of ``changed_paths`` membership; malformed/unreadable
  JSON is treated as absent for every agent (fail closed).

The partial-refusal-doesn't-block-siblings property (design.md's error
table: "Incomplete agent registration | That registration refused and
reported; other files still apply") is exercised directly: a commit that
mixes a broken/incomplete registration with unrelated tracked files must
refuse only the registration while the unrelated files still go through.

This module intentionally imports ``backend.registration``, which does not
yet implement the amended (7.5) rule. Every test below is expected to fail
RED right now against the current module — either a ``TypeError`` on the
``roots`` argument ``check_registrations`` does not yet accept, or an
assertion mismatch (the prompt derivation does not yet consult
``portable``/``allowlist``, and shared-part evaluation still gates on
``changed_paths`` membership instead of tree content) — never as a
silently-passing vacuous assertion.

Real fixture file sets are built on ``tmp_path`` (no mocks): a registration
is represented as a set of real files under a fake commit-scoped directory
tree (plus real root-A/root-B directories distinct from the commit tree,
used only to build a ``file://`` prompt value's absolute-form path — the
prompt file's PRESENCE is always checked against the commit tree per
requirements.md 5.12) so the module under test reads and reasons about
real bytes on disk, matching design.md's coordinated writes:

    <root>/agents/<name>.json                ~/.kiro/agents/<name>.json
    <root>/config.json                        config.json (agents{} entry)
    <root>/agent_model_state.json             the model pin
    <root>/config-bundles/agent-prompts/<name>.md   the tracked prompt file
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
TRACKED_PROMPT_RELPATH = f"config-bundles/agent-prompts/{AGENT_NAME}.md"
AGENT_DEF_RELPATH = f"agents/{AGENT_NAME}.json"
CONFIG_RELPATH = "config.json"
MODEL_STATE_RELPATH = "agent_model_state.json"

# The 4 registration-shaped relpaths this file's fixtures build, in the
# amended (7.5) rule's shape: the prompt part is a TRACKED
# ``config-bundles/agent-prompts/<name>.md`` path (requirements.md 5.11)
# referenced from the agent definition's own ``prompt`` value, not a
# bare ``agent-prompts/<name>.md`` derived from the agent's name.
ALL_FOUR_RELPATHS = (
    TRACKED_PROMPT_RELPATH,
    AGENT_DEF_RELPATH,
    CONFIG_RELPATH,
    MODEL_STATE_RELPATH,
)


def _roots(root_a: Path, root_b: Path) -> dict:
    """The ``{"A": Path, "B": Path}`` shape ``portable.py``/``collect.py``

    use. ``root_a``/``root_b`` are THIS HOST's live root paths -- a
    ``file://`` prompt value is always resolved against these, never
    against the commit-scoped ``root`` (requirements.md 5.12).
    """
    return {"A": root_a, "B": root_b}


def _write_prompt_file(root: Path, agent_name: str = AGENT_NAME) -> Path:
    """Write the TRACKED prompt path in the COMMIT TREE.

    Per requirements.md 5.12, a required prompt part counts as present
    when ``<rel>`` exists as a regular, non-symlink file in the APPROVED
    COMMIT'S TREE -- i.e. under the commit-scoped ``root``
    ``check_registrations`` receives as its first argument, never under
    a live configured root. The parameter here is historically named
    ``root_a`` at call sites for the fixture's live-root-A directory, but
    the prompt file itself must land under the commit tree (``root``) --
    see ``_write_full_registration`` below for how the two are threaded.
    """
    path = root / "config-bundles" / "agent-prompts" / f"{agent_name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {agent_name}\n\nYou are {agent_name}.\n", encoding="utf-8")
    return path


def _write_agent_def(root: Path, agent_name: str = AGENT_NAME) -> Path:
    """Write ``agents/<agent_name>.json`` under the commit root, whose

    ``prompt`` value is a ``file://`` token reference to the TRACKED
    prompt relpath -- the only thing requirements.md 5.11 derives the
    prompt part from.
    """
    path = root / "agents" / f"{agent_name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "name": agent_name,
                "description": "An example agent.",
                "prompt": (
                    "file://${KIROCREW_HOME}/config-bundles/agent-prompts/"
                    f"{agent_name}.md"
                ),
                "tools": ["read", "write"],
                "allowedTools": ["read"],
                "resources": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def _write_agent_def_with_prompt(
    root: Path, agent_name: str, prompt_value: object
) -> Path:
    """Write ``agents/<agent_name>.json`` with an explicit, arbitrary

    ``prompt`` value -- unlike ``_write_agent_def`` above, which always
    hardcodes the standard tracked-prompt reference. Used to build a
    definition whose prompt resolves to an UNTRACKED location.
    """
    path = root / "agents" / f"{agent_name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "name": agent_name,
                "description": "An example agent.",
                "prompt": prompt_value,
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


def _write_full_registration(
    root: Path, root_a: Path, agent_name: str = AGENT_NAME
) -> None:
    """Write all parts of one agent's registration: the tracked prompt

    file, the agent definition, and both shared files, all under the
    commit-scoped ``root`` (requirements.md 5.12: a required prompt part
    counts as present when it exists in the APPROVED COMMIT'S TREE, never
    the live root). ``root_a`` is accepted for call-site compatibility
    with tests that also need a live root A to prove the prompt need NOT
    exist there.
    """
    del root_a  # unused: the prompt file now lives in the commit tree.
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


def _prep_roots(tmp_path: Path) -> tuple[Path, Path, dict]:
    """Build a fresh commit dir plus live root-A/root-B dirs, and the

    ``roots`` mapping ``check_registrations``'s 3rd argument needs.
    Returns ``(root, root_a, roots)``.
    """
    root = tmp_path / "commit"
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    root.mkdir()
    root_a.mkdir()
    root_b.mkdir()
    return root, root_a, _roots(root_a, root_b)


# ---------------------------------------------------------------------------
# (a) All four parts present -> registration is complete.
# ---------------------------------------------------------------------------


class TestCompleteRegistration:
    """All four parts present in the commit -> reported complete."""

    def test_all_four_parts_present_is_complete(self, tmp_path: Path) -> None:
        root, root_a, roots = _prep_roots(tmp_path)
        _write_full_registration(root, root_a)
        changed = _changed_relpaths_for(
            root, AGENT_DEF_RELPATH, CONFIG_RELPATH, MODEL_STATE_RELPATH
        )

        result = registration.check_registrations(root, changed, roots)

        assert result.complete_agents == [AGENT_NAME]
        assert result.incomplete_agents == {}

    def test_complete_registration_names_no_missing_parts(self, tmp_path: Path) -> None:
        root, root_a, roots = _prep_roots(tmp_path)
        _write_full_registration(root, root_a)
        changed = _changed_relpaths_for(
            root, AGENT_DEF_RELPATH, CONFIG_RELPATH, MODEL_STATE_RELPATH
        )

        result = registration.check_registrations(root, changed, roots)

        assert AGENT_NAME not in result.incomplete_agents


# ---------------------------------------------------------------------------
# (b) Each required part missing -> incomplete, naming THAT part.
# ---------------------------------------------------------------------------


class TestSinglePartMissing:
    """Exactly one required part absent -> incomplete, and the report
    names precisely the missing part — not a generic failure and not any
    of the present parts."""

    def test_missing_prompt_file_is_incomplete_and_named(self, tmp_path: Path) -> None:
        """The agent definition's ``prompt`` resolves (5.12) to the

        TRACKED relpath, but no file exists there in the commit tree ->
        the required prompt part is unsatisfied (5.11(a)).
        """
        root, root_a, roots = _prep_roots(tmp_path)
        _write_agent_def(root, AGENT_NAME)
        _write_config_json(root, AGENT_NAME)
        _write_agent_model_state(root, AGENT_NAME)
        # Deliberately do not write the prompt file in the commit tree.
        changed = _changed_relpaths_for(
            root, AGENT_DEF_RELPATH, CONFIG_RELPATH, MODEL_STATE_RELPATH
        )

        result = registration.check_registrations(root, changed, roots)

        assert AGENT_NAME not in result.complete_agents
        assert AGENT_NAME in result.incomplete_agents
        missing = result.incomplete_agents[AGENT_NAME]
        assert "prompt" in " ".join(missing).lower()
        assert not any("agents/" in m and m.endswith(".json") for m in missing)

    def test_missing_agent_definition_is_incomplete_and_named(
        self, tmp_path: Path
    ) -> None:
        root, root_a, roots = _prep_roots(tmp_path)
        _write_prompt_file(root, AGENT_NAME)
        _write_config_json(root, AGENT_NAME)
        _write_agent_model_state(root, AGENT_NAME)
        changed = _changed_relpaths_for(root, CONFIG_RELPATH, MODEL_STATE_RELPATH)

        result = registration.check_registrations(root, changed, roots)

        # With no changed agents/<name>.json, this agent names no
        # candidate at all (5.13) -- there is nothing to report missing
        # or complete for it.
        assert AGENT_NAME not in result.complete_agents
        assert AGENT_NAME not in result.incomplete_agents

    def test_missing_config_json_entry_is_incomplete_and_named(
        self, tmp_path: Path
    ) -> None:
        """5.14: the shared file's CONTENT governs, not changed_paths

        membership -- so "missing" here means the key is genuinely
        absent from ``config.json``'s content, not merely that the path
        is omitted from ``changed``.
        """
        root, root_a, roots = _prep_roots(tmp_path)
        _write_prompt_file(root, AGENT_NAME)
        _write_agent_def(root, AGENT_NAME)
        (root / CONFIG_RELPATH).write_text(json.dumps({"agents": {}}), encoding="utf-8")
        _write_agent_model_state(root, AGENT_NAME)
        changed = _changed_relpaths_for(root, AGENT_DEF_RELPATH, MODEL_STATE_RELPATH)

        result = registration.check_registrations(root, changed, roots)

        assert AGENT_NAME not in result.complete_agents
        assert AGENT_NAME in result.incomplete_agents
        missing = result.incomplete_agents[AGENT_NAME]
        joined = " ".join(missing).lower()
        assert "config.json" in joined or "config" in joined

    def test_missing_model_state_pin_is_incomplete_and_named(
        self, tmp_path: Path
    ) -> None:
        root, root_a, roots = _prep_roots(tmp_path)
        _write_prompt_file(root, AGENT_NAME)
        _write_agent_def(root, AGENT_NAME)
        _write_config_json(root, AGENT_NAME)
        (root / MODEL_STATE_RELPATH).write_text(json.dumps({}), encoding="utf-8")
        changed = _changed_relpaths_for(root, AGENT_DEF_RELPATH, CONFIG_RELPATH)

        result = registration.check_registrations(root, changed, roots)

        assert AGENT_NAME not in result.complete_agents
        assert AGENT_NAME in result.incomplete_agents
        missing = result.incomplete_agents[AGENT_NAME]
        joined = " ".join(missing).lower()
        assert "model_state" in joined or "model state" in joined or "pin" in joined

    @pytest.mark.parametrize(
        "missing_part",
        ("prompt", "config", "pin"),
        ids=["missing_prompt", "missing_config", "missing_pin"],
    )
    def test_exactly_one_missing_part_is_never_reported_as_present(
        self, tmp_path: Path, missing_part: str
    ) -> None:
        """Whichever single part is missing, the others must NOT also be

        reported missing -- the report is precise, not a blanket
        'something is wrong' flag. (The agent-def part is covered
        separately above: omitting it yields no candidate at all, not a
        1-item missing list.)
        """
        root, root_a, roots = _prep_roots(tmp_path)
        _write_agent_def(root, AGENT_NAME)
        if missing_part != "prompt":
            _write_prompt_file(root, AGENT_NAME)
        if missing_part != "config":
            _write_config_json(root, AGENT_NAME)
        else:
            (root / CONFIG_RELPATH).write_text(
                json.dumps({"agents": {}}), encoding="utf-8"
            )
        if missing_part != "pin":
            _write_agent_model_state(root, AGENT_NAME)
        else:
            (root / MODEL_STATE_RELPATH).write_text(json.dumps({}), encoding="utf-8")
        changed = _changed_relpaths_for(
            root, AGENT_DEF_RELPATH, CONFIG_RELPATH, MODEL_STATE_RELPATH
        )

        result = registration.check_registrations(root, changed, roots)

        missing = result.incomplete_agents[AGENT_NAME]
        assert len(missing) == 1


# ---------------------------------------------------------------------------
# (c) Two parts missing -> incomplete, naming BOTH.
# ---------------------------------------------------------------------------


class TestTwoPartsMissing:
    """Two required parts absent -> incomplete, naming both missing parts
    (not just one, not zero)."""

    def test_missing_prompt_and_config_names_both(self, tmp_path: Path) -> None:
        root, root_a, roots = _prep_roots(tmp_path)
        _write_agent_def(root, AGENT_NAME)
        # Prompt file not written under root_a; config.json carries no
        # entry for this agent.
        (root / CONFIG_RELPATH).write_text(json.dumps({"agents": {}}), encoding="utf-8")
        _write_agent_model_state(root, AGENT_NAME)
        changed = _changed_relpaths_for(root, AGENT_DEF_RELPATH, MODEL_STATE_RELPATH)

        result = registration.check_registrations(root, changed, roots)

        assert AGENT_NAME not in result.complete_agents
        assert AGENT_NAME in result.incomplete_agents
        missing = result.incomplete_agents[AGENT_NAME]
        assert len(missing) == 2
        joined = " ".join(missing).lower()
        assert "prompt" in joined
        assert "config" in joined

    def test_missing_config_and_model_state_names_both(self, tmp_path: Path) -> None:
        root, root_a, roots = _prep_roots(tmp_path)
        _write_prompt_file(root, AGENT_NAME)
        _write_agent_def(root, AGENT_NAME)
        (root / CONFIG_RELPATH).write_text(json.dumps({"agents": {}}), encoding="utf-8")
        (root / MODEL_STATE_RELPATH).write_text(json.dumps({}), encoding="utf-8")
        changed = _changed_relpaths_for(root, AGENT_DEF_RELPATH)

        result = registration.check_registrations(root, changed, roots)

        assert AGENT_NAME not in result.complete_agents
        assert AGENT_NAME in result.incomplete_agents
        missing = result.incomplete_agents[AGENT_NAME]
        assert len(missing) == 2
        joined = " ".join(missing).lower()
        assert "config" in joined
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
        root, root_a, roots = _prep_roots(tmp_path)
        _write_full_registration(root, root_a)
        _write_unrelated_file(root, "steering/unrelated.md")
        _write_unrelated_file(root, "skills/some-skill/SKILL.md")
        changed = _changed_relpaths_for(
            root,
            AGENT_DEF_RELPATH,
            CONFIG_RELPATH,
            MODEL_STATE_RELPATH,
            "steering/unrelated.md",
            "skills/some-skill/SKILL.md",
        )

        result = registration.check_registrations(root, changed, roots)

        assert result.complete_agents == [AGENT_NAME]
        assert result.incomplete_agents == {}

    def test_unrelated_files_are_reported_as_passthrough_not_blocked(
        self, tmp_path: Path
    ) -> None:
        root, root_a, roots = _prep_roots(tmp_path)
        _write_full_registration(root, root_a)
        _write_unrelated_file(root, "steering/unrelated.md")
        changed = _changed_relpaths_for(
            root,
            AGENT_DEF_RELPATH,
            CONFIG_RELPATH,
            MODEL_STATE_RELPATH,
            "steering/unrelated.md",
        )

        result = registration.check_registrations(root, changed, roots)

        assert "steering/unrelated.md" in result.unrelated_paths
        assert "steering/unrelated.md" not in result.blocked_paths


# ---------------------------------------------------------------------------
# (e) Incomplete registration PLUS unrelated files -> registration refused
#     BUT unrelated files still pass through. The partial-refusal-doesn't-
#     block-siblings property.
# ---------------------------------------------------------------------------


class TestIncompleteRegistrationDoesNotBlockUnrelatedFiles:
    """design.md's error-handling table: 'Incomplete agent registration |
    That registration refused and reported; other files still apply.' An
    incomplete registration must refuse only the registration's own
    parts -- every unrelated changed path in the same commit must still
    be reported as passing through untouched."""

    def test_incomplete_registration_refused_unrelated_still_pass(
        self, tmp_path: Path
    ) -> None:
        root, root_a, roots = _prep_roots(tmp_path)
        _write_agent_def(root, AGENT_NAME)
        _write_config_json(root, AGENT_NAME)
        _write_agent_model_state(root, AGENT_NAME)
        _write_unrelated_file(root, "steering/unrelated.md")
        _write_unrelated_file(root, "hooks.json")
        # Omit the prompt file from the commit tree -> incomplete registration.
        changed = _changed_relpaths_for(
            root,
            AGENT_DEF_RELPATH,
            CONFIG_RELPATH,
            MODEL_STATE_RELPATH,
            "steering/unrelated.md",
            "hooks.json",
        )

        result = registration.check_registrations(root, changed, roots)

        assert AGENT_NAME not in result.complete_agents
        assert AGENT_NAME in result.incomplete_agents
        assert "steering/unrelated.md" in result.unrelated_paths
        assert "hooks.json" in result.unrelated_paths
        assert "steering/unrelated.md" not in result.blocked_paths
        assert "hooks.json" not in result.blocked_paths

    def test_incomplete_registration_blocks_only_its_own_parts(
        self, tmp_path: Path
    ) -> None:
        root, root_a, roots = _prep_roots(tmp_path)
        _write_prompt_file(root, AGENT_NAME)
        _write_agent_def(root, AGENT_NAME)
        _write_agent_model_state(root, AGENT_NAME)
        # Omit config.json's entry -> incomplete registration.
        (root / CONFIG_RELPATH).write_text(json.dumps({"agents": {}}), encoding="utf-8")
        changed = _changed_relpaths_for(root, AGENT_DEF_RELPATH, MODEL_STATE_RELPATH)

        result = registration.check_registrations(root, changed, roots)

        # The registration's own present/changed parts must be
        # refused/blocked from applying piecemeal.
        assert AGENT_DEF_RELPATH in result.blocked_paths
        assert MODEL_STATE_RELPATH in result.blocked_paths
        assert CONFIG_RELPATH not in result.blocked_paths  # key never present

    def test_two_agents_one_complete_one_incomplete_in_same_commit(
        self, tmp_path: Path
    ) -> None:
        """A commit touching two different agents' registrations, one
        complete and one missing a part, must report each independently:
        the complete one applies, the incomplete one is refused, and
        neither result leaks into the other's report."""
        root, root_a, roots = _prep_roots(tmp_path)
        _write_full_registration(root, root_a, agent_name="agent-one")
        _write_prompt_file(root, agent_name="agent-two")
        _write_agent_def(root, agent_name="agent-two")
        _write_config_json(root, agent_name="agent-two")
        # agent-two's model-state pin is deliberately omitted (fresh
        # shared file with no key for it).
        (root / MODEL_STATE_RELPATH).write_text(
            json.dumps({"agent-one": {"model_managed": False}}), encoding="utf-8"
        )

        changed = _changed_relpaths_for(
            root,
            "agents/agent-one.json",
            "config.json",
            "agent_model_state.json",
            "agents/agent-two.json",
        )

        result = registration.check_registrations(root, changed, roots)

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
        commit tree -- and both carry ``agent-other``'s entry/pin,
        proving the files themselves are genuinely populated -- but
        neither carries ``agent-under-test``'s own key. The correct
        (content-inspecting) implementation must still report both parts
        missing for ``agent-under-test``, because its key is absent from
        both files' content (5.6, 5.14).
        """
        root, root_a, roots = _prep_roots(tmp_path)
        agent_under_test = "agent-under-test"
        agent_other = "agent-other"

        _write_prompt_file(root, agent_name=agent_under_test)
        _write_agent_def(root, agent_name=agent_under_test)
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
            f"agents/{agent_under_test}.json",
            CONFIG_RELPATH,
            MODEL_STATE_RELPATH,
        )

        result = registration.check_registrations(root, changed, roots)

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
    exists in the commit tree but does not parse as JSON must be treated
    the same as carrying no agent's entry at all -- never as if it
    happened to carry one."""

    @pytest.mark.parametrize(
        "shared_relpath",
        (CONFIG_RELPATH, MODEL_STATE_RELPATH),
        ids=["malformed_config_json", "malformed_agent_model_state_json"],
    )
    def test_malformed_shared_file_is_incomplete_and_names_that_part(
        self, tmp_path: Path, shared_relpath: str
    ) -> None:
        root, root_a, roots = _prep_roots(tmp_path)
        _write_full_registration(root, root_a)
        _write_unrelated_file(root, "steering/unrelated.md")

        # Corrupt exactly one shared file with invalid JSON, in place.
        (root / shared_relpath).write_text("{not json", encoding="utf-8")

        changed = _changed_relpaths_for(
            root,
            AGENT_DEF_RELPATH,
            CONFIG_RELPATH,
            MODEL_STATE_RELPATH,
            "steering/unrelated.md",
        )

        result = registration.check_registrations(root, changed, roots)

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
        root, root_a, roots = _prep_roots(tmp_path)
        _write_full_registration(root, root_a)
        changed = _changed_relpaths_for(
            root, AGENT_DEF_RELPATH, CONFIG_RELPATH, MODEL_STATE_RELPATH
        )

        result = registration.check_registrations(root, changed, roots)

        report = result.propagation_report[AGENT_NAME]
        lowered = report.lower()
        assert "restart" not in lowered or "without" in lowered
        assert "spawn_run" in lowered or "roster" in lowered

    def test_complete_registration_reports_dashboard_picker_may_need_refresh(
        self, tmp_path: Path
    ) -> None:
        root, root_a, roots = _prep_roots(tmp_path)
        _write_full_registration(root, root_a)
        changed = _changed_relpaths_for(
            root, AGENT_DEF_RELPATH, CONFIG_RELPATH, MODEL_STATE_RELPATH
        )

        result = registration.check_registrations(root, changed, roots)

        report = result.propagation_report[AGENT_NAME]
        lowered = report.lower()
        assert "dashboard" in lowered
        assert "refresh" in lowered

    def test_incomplete_registration_has_no_propagation_report_entry(
        self, tmp_path: Path
    ) -> None:
        root, root_a, roots = _prep_roots(tmp_path)
        _write_agent_def(root, AGENT_NAME)
        _write_config_json(root, AGENT_NAME)
        _write_agent_model_state(root, AGENT_NAME)
        # Prompt file omitted from the commit tree -> incomplete.
        changed = _changed_relpaths_for(
            root, AGENT_DEF_RELPATH, CONFIG_RELPATH, MODEL_STATE_RELPATH
        )

        result = registration.check_registrations(root, changed, roots)

        assert AGENT_NAME not in result.propagation_report


# ---------------------------------------------------------------------------
# (g) No registration-shaped paths in the commit at all.
# ---------------------------------------------------------------------------


class TestNoRegistrationInCommit:
    """A commit touching no ``agents/<name>.json`` at all must report
    zero complete and zero incomplete agents, and must not treat any
    changed path as blocked (5.13: a candidate is named only by a
    changed agent definition)."""

    def test_only_unrelated_files_yields_no_agents_and_no_blocking(
        self, tmp_path: Path
    ) -> None:
        root, root_a, roots = _prep_roots(tmp_path)
        _write_unrelated_file(root, "steering/unrelated.md")
        _write_unrelated_file(root, "config.json")
        changed = _changed_relpaths_for(root, "steering/unrelated.md", "config.json")

        result = registration.check_registrations(root, changed, roots)

        assert result.complete_agents == []
        assert result.incomplete_agents == {}
        assert result.blocked_paths == []
        assert "steering/unrelated.md" in result.unrelated_paths
        assert "config.json" in result.unrelated_paths


# ===========================================================================
# tasks.md 7.5 — amended registration rule.
#
# requirements.md 5.11-5.14:
#   5.11 prompt part derivation: file:// -> tracked -> REQUIRED;
#        inline/absent/null/empty -> satisfied, no file;
#        file:// -> untracked/neither root -> NOT required, reported;
#        present-but-not-string-or-null -> incomplete.
#   5.12 token form and this-host absolute form both resolve;
#        empty/".." segment or unparseable agents/<name>.json ->
#        incomplete (fail closed).
#   5.13 candidates named only from a changed agents/<name>.json; a
#        changed prompt file with no referencing changed agent def is an
#        ordinary tracked file, not a registration.
#   5.14 shared parts (config.json, agent_model_state.json) judged from
#        the commit TREE'S CONTENT, never from changed_paths membership.
#
# `check_registrations` now takes a THIRD argument, `roots: Mapping[str,
# Path]` (the same {"A": Path, "B": Path} shape portable.py and
# collect.py use), because prompt-part derivation calls
# `portable.resolve_reference(value, roots)`.
# ===========================================================================

_75_AGENT_NAME = "seventy-five-agent"
_75_AGENT_DEF_RELPATH = f"agents/{_75_AGENT_NAME}.json"
_75_TRACKED_PROMPT_RELPATH = f"config-bundles/agent-prompts/{_75_AGENT_NAME}.md"


def _75_roots(root_a: Path, root_b: Path) -> dict:
    """The {"A": Path, "B": Path} shape portable.py/collect.py use.

    `root_a`/`root_b` are THIS HOST's live root paths — distinct from
    `root`, the commit-scoped checkout directory `check_registrations`
    reads shared-file content from. A ``file://`` prompt value is always
    resolved against these live roots (that is the whole point of
    root-independence: the committed JSON names a path that is valid on
    whichever host applies it), never against the commit-scoped `root`.
    """
    return {"A": root_a, "B": root_b}


def _75_write_agent_def(
    root: Path,
    agent_name: str,
    prompt: object,
    *,
    relpath: str | None = None,
) -> str:
    """Write ``agents/<agent_name>.json`` with an arbitrary ``prompt``

    value (which may be a non-string, to exercise 5.11's fail-closed
    case). Returns the relpath written, defaulting to the standard
    ``agents/<agent_name>.json`` shape unless ``relpath`` overrides it
    (used to write deliberately malformed content at a still-matching
    path).
    """
    rel = relpath if relpath is not None else f"agents/{agent_name}.json"
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"name": agent_name, "prompt": prompt}, indent=2),
        encoding="utf-8",
    )
    return rel


def _75_write_tracked_prompt(
    root: Path, agent_name: str = _75_AGENT_NAME, body: str = "v1"
) -> None:
    """Write the TRACKED prompt path (``config-bundles/agent-prompts/``)

    directly under the commit-scoped ``root`` — this is the location
    requirements.md 5.12 checks: a required prompt part counts as
    present when it "exists as a regular, non-symlink file in the
    APPROVED COMMIT'S TREE", never the live configured root.
    """
    path = root / "config-bundles" / "agent-prompts" / f"{agent_name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {agent_name}\n\n{body}\n", encoding="utf-8")


def _75_write_shared_files_with_key(root: Path, agent_name: str) -> None:
    (root / "config.json").write_text(
        json.dumps({"agents": {agent_name: {"source": "local"}}}, indent=2),
        encoding="utf-8",
    )
    (root / "agent_model_state.json").write_text(
        json.dumps({agent_name: {"model_managed": False}}, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# (7.5-a) Tracked file:// prompt, both token and this-host absolute form.
# ---------------------------------------------------------------------------


class TestTrackedPromptPresentInTree:
    """A ``file://`` prompt that resolves to the TRACKED

    ``config-bundles/agent-prompts/<name>.md`` path, and whose target
    file exists in the commit tree, makes the prompt part REQUIRED and
    satisfied -- registration completes when the other three parts are
    also present.
    """

    def test_token_form_prompt_present_is_complete(self, tmp_path: Path) -> None:
        root = tmp_path / "commit"
        root_a = tmp_path / "root-a"
        root_b = tmp_path / "root-b"
        root.mkdir()
        root_a.mkdir()
        root_b.mkdir()

        _75_write_agent_def(
            root,
            _75_AGENT_NAME,
            prompt=f"file://${{KIROCREW_HOME}}/{_75_TRACKED_PROMPT_RELPATH}",
        )
        _75_write_tracked_prompt(root)
        _75_write_shared_files_with_key(root, _75_AGENT_NAME)

        result = registration.check_registrations(
            root, [_75_AGENT_DEF_RELPATH], _75_roots(root_a, root_b)
        )

        assert _75_AGENT_NAME in result.complete_agents
        assert _75_AGENT_NAME not in result.incomplete_agents

    def test_this_host_absolute_form_prompt_present_is_complete(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "commit"
        root_a = tmp_path / "root-a"
        root_b = tmp_path / "root-b"
        root.mkdir()
        root_a.mkdir()
        root_b.mkdir()

        _75_write_agent_def(
            root,
            _75_AGENT_NAME,
            prompt=f"file://{root_a}/{_75_TRACKED_PROMPT_RELPATH}",
        )
        _75_write_tracked_prompt(root)
        _75_write_shared_files_with_key(root, _75_AGENT_NAME)

        result = registration.check_registrations(
            root, [_75_AGENT_DEF_RELPATH], _75_roots(root_a, root_b)
        )

        assert _75_AGENT_NAME in result.complete_agents

    def test_token_and_this_host_forms_are_equivalent(self, tmp_path: Path) -> None:
        """5.12: both forms map to the identical (root, relpath) and so

        must produce the identical completeness verdict for otherwise
        identical fixtures. The prompt file itself lives in each
        fixture's own commit tree (5.12), never under the live root A
        used only to build the ``file://`` value's absolute form.
        """
        root_a = tmp_path / "root-a"
        root_b = tmp_path / "root-b"
        root_a.mkdir()
        root_b.mkdir()
        roots = _75_roots(root_a, root_b)

        token_root = tmp_path / "commit-token"
        token_root.mkdir()
        _75_write_agent_def(
            token_root,
            _75_AGENT_NAME,
            prompt=f"file://${{KIROCREW_HOME}}/{_75_TRACKED_PROMPT_RELPATH}",
        )
        _75_write_tracked_prompt(token_root)
        _75_write_shared_files_with_key(token_root, _75_AGENT_NAME)

        absolute_root = tmp_path / "commit-absolute"
        absolute_root.mkdir()
        _75_write_agent_def(
            absolute_root,
            _75_AGENT_NAME,
            prompt=f"file://{root_a}/{_75_TRACKED_PROMPT_RELPATH}",
        )
        _75_write_tracked_prompt(absolute_root)
        _75_write_shared_files_with_key(absolute_root, _75_AGENT_NAME)

        token_result = registration.check_registrations(
            token_root, [_75_AGENT_DEF_RELPATH], roots
        )
        absolute_result = registration.check_registrations(
            absolute_root, [_75_AGENT_DEF_RELPATH], roots
        )

        assert (_75_AGENT_NAME in token_result.complete_agents) == (
            _75_AGENT_NAME in absolute_result.complete_agents
        )
        assert _75_AGENT_NAME in token_result.complete_agents


class TestTrackedPromptAbsentFromTree:
    """A ``file://`` prompt that resolves to the tracked prompt path, but

    whose target file does NOT exist in the commit tree, makes the
    required prompt part unsatisfied -> incomplete.
    """

    def test_tracked_prompt_absent_from_tree_is_incomplete(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "commit"
        root_a = tmp_path / "root-a"
        root_b = tmp_path / "root-b"
        root.mkdir()
        root_a.mkdir()
        root_b.mkdir()

        _75_write_agent_def(
            root,
            _75_AGENT_NAME,
            prompt=f"file://${{KIROCREW_HOME}}/{_75_TRACKED_PROMPT_RELPATH}",
        )
        # Deliberately do NOT write the prompt file in the commit tree.
        _75_write_shared_files_with_key(root, _75_AGENT_NAME)

        result = registration.check_registrations(
            root, [_75_AGENT_DEF_RELPATH], _75_roots(root_a, root_b)
        )

        assert _75_AGENT_NAME not in result.complete_agents
        assert _75_AGENT_NAME in result.incomplete_agents
        assert "prompt" in " ".join(result.incomplete_agents[_75_AGENT_NAME]).lower()


# ---------------------------------------------------------------------------
# (7.5-a2) requirements.md 5.12 -- commit-tree-vs-live-root divergence.
#
# The real-world case this pins: a commit that ships a NEW agent together
# with its prompt file. The prompt is present in the APPROVED COMMIT'S
# TREE but has never been live before -- that must be COMPLETE, not
# refused as incomplete. The inverse (present live, absent from the
# commit being judged) must be INCOMPLETE: 5.12 names the commit tree as
# the authority, not the live filesystem.
# ---------------------------------------------------------------------------


class TestPromptPresentInCommitTreeButAbsentLiveIsComplete:
    """5.12: the prompt part counts as present when it exists in the

    APPROVED COMMIT'S TREE -- a brand-new agent's prompt file that has
    never been written to the live root before must still satisfy the
    prompt part when it is present in the commit being judged. This is
    the main real-world case: a commit that ships a new agent's
    definition together with its prompt file, before that commit has
    ever been applied to any live root.
    """

    def test_prompt_present_in_tree_but_absent_from_live_root_is_complete(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "commit"
        root_a = tmp_path / "root-a"
        root_b = tmp_path / "root-b"
        root.mkdir()
        root_a.mkdir()
        root_b.mkdir()

        _75_write_agent_def(
            root,
            _75_AGENT_NAME,
            prompt=f"file://${{KIROCREW_HOME}}/{_75_TRACKED_PROMPT_RELPATH}",
        )
        _75_write_tracked_prompt(root)
        _75_write_shared_files_with_key(root, _75_AGENT_NAME)

        # The live root A never receives the prompt file at all -- this
        # is the "brand-new agent, never applied before" case.
        assert not (root_a / _75_TRACKED_PROMPT_RELPATH).exists()

        result = registration.check_registrations(
            root, [_75_AGENT_DEF_RELPATH], _75_roots(root_a, root_b)
        )

        assert _75_AGENT_NAME in result.complete_agents
        assert _75_AGENT_NAME not in result.incomplete_agents


class TestPromptPresentLiveButAbsentFromCommitTreeIsIncomplete:
    """5.12's inverse: a prompt file that exists on the LIVE root (e.g.

    left over from a previous registration, or written out-of-band) but
    is NOT present in the commit tree being judged must still make the
    registration incomplete -- the commit tree is the sole authority,
    never the live filesystem.
    """

    def test_prompt_present_live_but_absent_from_commit_tree_is_incomplete(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "commit"
        root_a = tmp_path / "root-a"
        root_b = tmp_path / "root-b"
        root.mkdir()
        root_a.mkdir()
        root_b.mkdir()

        _75_write_agent_def(
            root,
            _75_AGENT_NAME,
            prompt=f"file://${{KIROCREW_HOME}}/{_75_TRACKED_PROMPT_RELPATH}",
        )
        # The prompt file exists on the LIVE root only -- never written
        # into the commit tree being judged.
        _75_write_tracked_prompt(root_a)
        _75_write_shared_files_with_key(root, _75_AGENT_NAME)

        result = registration.check_registrations(
            root, [_75_AGENT_DEF_RELPATH], _75_roots(root_a, root_b)
        )

        assert _75_AGENT_NAME not in result.complete_agents
        assert _75_AGENT_NAME in result.incomplete_agents
        assert "prompt" in " ".join(result.incomplete_agents[_75_AGENT_NAME]).lower()


class TestPromptAsSymlinkInCommitTreeIsIncomplete:
    """5.12: a required prompt part counts as present only when the

    relpath exists as a regular, NON-SYMLINK file in the commit tree --
    a symlink at that path (even one that resolves to real content) must
    make the registration incomplete.
    """

    def test_prompt_as_symlink_in_commit_tree_is_incomplete(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "commit"
        root_a = tmp_path / "root-a"
        root_b = tmp_path / "root-b"
        root.mkdir()
        root_a.mkdir()
        root_b.mkdir()

        _75_write_agent_def(
            root,
            _75_AGENT_NAME,
            prompt=f"file://${{KIROCREW_HOME}}/{_75_TRACKED_PROMPT_RELPATH}",
        )

        # A real target the symlink points at, elsewhere in the tree --
        # the symlink itself, not its target, sits at the tracked relpath.
        real_target = root / "real-prompt-content.md"
        real_target.write_text(
            f"# {_75_AGENT_NAME}\n\nreal content\n", encoding="utf-8"
        )
        prompt_path = root / _75_TRACKED_PROMPT_RELPATH
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.symlink_to(real_target)

        _75_write_shared_files_with_key(root, _75_AGENT_NAME)

        result = registration.check_registrations(
            root, [_75_AGENT_DEF_RELPATH], _75_roots(root_a, root_b)
        )

        assert _75_AGENT_NAME not in result.complete_agents
        assert _75_AGENT_NAME in result.incomplete_agents
        assert "prompt" in " ".join(result.incomplete_agents[_75_AGENT_NAME]).lower()


# ---------------------------------------------------------------------------
# (7.5-b) inline / absent / null / empty prompt -> satisfied, no file part.
# ---------------------------------------------------------------------------


class TestNonFilePromptSatisfiedWithoutAFile:
    """5.11(b): an inline string, an absent key, a null, or an empty

    string prompt satisfies the prompt part with NO required file --
    registration completes on the other three parts alone.
    """

    @pytest.mark.parametrize(
        "def_body",
        (
            {"name": _75_AGENT_NAME, "prompt": "You are a helpful agent."},
            {"name": _75_AGENT_NAME},
            {"name": _75_AGENT_NAME, "prompt": None},
            {"name": _75_AGENT_NAME, "prompt": ""},
        ),
        ids=["inline_string", "absent_key", "null", "empty_string"],
    )
    def test_prompt_variant_is_satisfied_with_no_file(
        self, tmp_path: Path, def_body: dict
    ) -> None:
        root = tmp_path / "commit"
        root_a = tmp_path / "root-a"
        root_b = tmp_path / "root-b"
        root.mkdir()
        root_a.mkdir()
        root_b.mkdir()

        (root / "agents").mkdir()
        (root / _75_AGENT_DEF_RELPATH).write_text(
            json.dumps(def_body, indent=2), encoding="utf-8"
        )
        _75_write_shared_files_with_key(root, _75_AGENT_NAME)

        result = registration.check_registrations(
            root, [_75_AGENT_DEF_RELPATH], _75_roots(root_a, root_b)
        )

        assert _75_AGENT_NAME in result.complete_agents
        assert _75_AGENT_NAME not in result.incomplete_agents


# ---------------------------------------------------------------------------
# (7.5-c) site-packages prompt -> not required, reported untracked.
# ---------------------------------------------------------------------------


class TestUntrackedPromptLocationNotRequiredButReported:
    """5.11(c): a ``file://`` prompt resolving to neither root (e.g.

    site-packages), or to a path under a root the allowlist does not
    track, makes the prompt NOT a required part -- registration completes
    on the other three parts, and the untracked location is reported by
    agent name.
    """

    def test_site_packages_prompt_not_required_but_reported(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "commit"
        root_a = tmp_path / "root-a"
        root_b = tmp_path / "root-b"
        root.mkdir()
        root_a.mkdir()
        root_b.mkdir()

        site_packages_prompt = (
            "file:///usr/local/lib/python3.12/site-packages/"
            "kiro_crew/config/prompt.md"
        )
        _75_write_agent_def(root, _75_AGENT_NAME, prompt=site_packages_prompt)
        _75_write_shared_files_with_key(root, _75_AGENT_NAME)

        result = registration.check_registrations(
            root, [_75_AGENT_DEF_RELPATH], _75_roots(root_a, root_b)
        )

        assert _75_AGENT_NAME in result.complete_agents
        assert _75_AGENT_NAME not in result.incomplete_agents
        assert _75_AGENT_NAME in result.untracked_prompt_agents

    def test_tracked_root_but_allowlist_untracked_path_not_required(
        self, tmp_path: Path
    ) -> None:
        """A ``file://`` value under root A but at a path the allowlist

        does NOT track (e.g. ``config-bundles/skills/**``, ratified as
        untracked per requirements.md 1.8) also makes the prompt part
        not required, reported as an untracked location -- distinct from
        "outside both roots" but the same reporting outcome per 5.11(c).
        """
        root = tmp_path / "commit"
        root_a = tmp_path / "root-a"
        root_b = tmp_path / "root-b"
        root.mkdir()
        root_a.mkdir()
        root_b.mkdir()

        _75_write_agent_def(
            root,
            _75_AGENT_NAME,
            prompt=(
                "file://${KIROCREW_HOME}/config-bundles/skills/" "some-skill/SKILL.md"
            ),
        )
        _75_write_shared_files_with_key(root, _75_AGENT_NAME)

        result = registration.check_registrations(
            root, [_75_AGENT_DEF_RELPATH], _75_roots(root_a, root_b)
        )

        assert _75_AGENT_NAME in result.complete_agents
        assert _75_AGENT_NAME in result.untracked_prompt_agents


# ---------------------------------------------------------------------------
# (7.5-d) `..` segment, unparseable JSON, non-string prompt -> incomplete.
# ---------------------------------------------------------------------------


class TestFailClosedMalformedOrUnsafePromptReference:
    """5.12: a ``..`` segment, an unparseable ``agents/<name>.json``, or a

    ``prompt`` that is present but neither a string nor ``null`` -- each
    make the registration incomplete (fail closed), never treated as
    "no prompt required".
    """

    def test_dotdot_segment_in_resolved_relpath_is_incomplete(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "commit"
        root_a = tmp_path / "root-a"
        root_b = tmp_path / "root-b"
        root.mkdir()
        root_a.mkdir()
        root_b.mkdir()

        _75_write_agent_def(
            root,
            _75_AGENT_NAME,
            prompt=(
                "file://${KIROCREW_HOME}/config-bundles/agent-prompts/"
                "../../etc/passwd"
            ),
        )
        _75_write_shared_files_with_key(root, _75_AGENT_NAME)

        result = registration.check_registrations(
            root, [_75_AGENT_DEF_RELPATH], _75_roots(root_a, root_b)
        )

        assert _75_AGENT_NAME not in result.complete_agents
        assert _75_AGENT_NAME in result.incomplete_agents

    def test_unparseable_agent_definition_is_incomplete(self, tmp_path: Path) -> None:
        root = tmp_path / "commit"
        root_a = tmp_path / "root-a"
        root_b = tmp_path / "root-b"
        root.mkdir()
        root_a.mkdir()
        root_b.mkdir()

        (root / "agents").mkdir()
        (root / _75_AGENT_DEF_RELPATH).write_text("{not valid json", encoding="utf-8")
        _75_write_shared_files_with_key(root, _75_AGENT_NAME)

        result = registration.check_registrations(
            root, [_75_AGENT_DEF_RELPATH], _75_roots(root_a, root_b)
        )

        assert _75_AGENT_NAME not in result.complete_agents
        assert _75_AGENT_NAME in result.incomplete_agents

    def test_agent_definition_that_is_not_a_json_object_is_incomplete(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "commit"
        root_a = tmp_path / "root-a"
        root_b = tmp_path / "root-b"
        root.mkdir()
        root_a.mkdir()
        root_b.mkdir()

        (root / "agents").mkdir()
        (root / _75_AGENT_DEF_RELPATH).write_text(
            json.dumps(["not", "an", "object"]), encoding="utf-8"
        )
        _75_write_shared_files_with_key(root, _75_AGENT_NAME)

        result = registration.check_registrations(
            root, [_75_AGENT_DEF_RELPATH], _75_roots(root_a, root_b)
        )

        assert _75_AGENT_NAME not in result.complete_agents
        assert _75_AGENT_NAME in result.incomplete_agents

    def test_non_string_non_null_prompt_is_incomplete(self, tmp_path: Path) -> None:
        root = tmp_path / "commit"
        root_a = tmp_path / "root-a"
        root_b = tmp_path / "root-b"
        root.mkdir()
        root_a.mkdir()
        root_b.mkdir()

        _75_write_agent_def(root, _75_AGENT_NAME, prompt=42)
        _75_write_shared_files_with_key(root, _75_AGENT_NAME)

        result = registration.check_registrations(
            root, [_75_AGENT_DEF_RELPATH], _75_roots(root_a, root_b)
        )

        assert _75_AGENT_NAME not in result.complete_agents
        assert _75_AGENT_NAME in result.incomplete_agents
        joined = " ".join(result.incomplete_agents[_75_AGENT_NAME]).lower()
        assert "prompt" in joined


# ---------------------------------------------------------------------------
# (7.5-e) A prompt-only change is unrelated (no candidate).
# ---------------------------------------------------------------------------


class TestPromptOnlyChangeIsUnrelated:
    """5.13: a changed prompt file that no CHANGED agent definition

    references is an ordinary tracked file, not a registration
    candidate -- it must land in ``unrelated_paths``, never in
    ``complete_agents``/``incomplete_agents``/``blocked_paths``, even
    when some OTHER (unchanged) agent definition on disk happens to
    reference it.
    """

    def test_changed_prompt_with_no_changed_referencing_def_is_unrelated(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "commit"
        root_a = tmp_path / "root-a"
        root_b = tmp_path / "root-b"
        root.mkdir()
        root_a.mkdir()
        root_b.mkdir()

        # The prompt file itself changed in the commit ...
        prompt_relpath = _75_TRACKED_PROMPT_RELPATH
        (root / "config-bundles" / "agent-prompts").mkdir(parents=True)
        (root / prompt_relpath).write_text("# updated\n", encoding="utf-8")

        # ... but NO agent definition changed in this commit at all, so
        # there is no candidate for `check_registrations` to name.
        changed = [prompt_relpath]

        result = registration.check_registrations(
            root, changed, _75_roots(root_a, root_b)
        )

        assert result.complete_agents == []
        assert result.incomplete_agents == {}
        assert result.blocked_paths == []
        assert prompt_relpath in result.unrelated_paths

    def test_changed_prompt_referenced_by_an_unchanged_def_on_disk_is_still_unrelated(
        self, tmp_path: Path
    ) -> None:
        """Candidates are named ONLY from a changed agents/<name>.json

        (5.13) -- an agent definition that exists on disk / in the commit
        root but was NOT itself part of ``changed_paths`` must not turn a
        prompt-only change into a registration candidate, even though the
        definition on disk genuinely references that exact prompt path.
        """
        root = tmp_path / "commit"
        root_a = tmp_path / "root-a"
        root_b = tmp_path / "root-b"
        root.mkdir()
        root_a.mkdir()
        root_b.mkdir()

        prompt_relpath = _75_TRACKED_PROMPT_RELPATH
        (root / "config-bundles" / "agent-prompts").mkdir(parents=True)
        (root / prompt_relpath).write_text("# updated\n", encoding="utf-8")

        # The agent definition file exists in the commit root and DOES
        # reference this prompt -- but it is not in `changed_paths`.
        _75_write_agent_def(
            root,
            _75_AGENT_NAME,
            prompt=f"file://${{KIROCREW_HOME}}/{prompt_relpath}",
        )
        _75_write_shared_files_with_key(root, _75_AGENT_NAME)

        changed = [prompt_relpath]  # agents/<name>.json deliberately absent

        result = registration.check_registrations(
            root, changed, _75_roots(root_a, root_b)
        )

        assert _75_AGENT_NAME not in result.complete_agents
        assert _75_AGENT_NAME not in result.incomplete_agents
        assert prompt_relpath in result.unrelated_paths


# ---------------------------------------------------------------------------
# (7.5-f) A prompt shared with a complete agent is not blocked.
# ---------------------------------------------------------------------------


class TestSharedPromptNotBlockedByAnUnrelatedIncompleteAgent:
    """5.13: "A required prompt file SHALL be blocked from applying only

    when every changed agent definition referencing it belongs to an
    incomplete registration." Two agents changed in the same commit both
    reference the SAME tracked prompt file; one is complete, the other is
    missing its model-state pin. The shared prompt file must NOT be
    blocked, because at least one referencing registration (the complete
    one) is not incomplete.
    """

    def test_prompt_shared_with_a_complete_agent_is_not_blocked(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "commit"
        root_a = tmp_path / "root-a"
        root_b = tmp_path / "root-b"
        root.mkdir()
        root_a.mkdir()
        root_b.mkdir()

        shared_prompt_relpath = "config-bundles/agent-prompts/shared.md"
        (root / "config-bundles" / "agent-prompts").mkdir(parents=True)
        (root / shared_prompt_relpath).write_text("# shared\n", encoding="utf-8")

        complete_agent = "complete-agent"
        incomplete_agent = "incomplete-agent"
        prompt_value = f"file://${{KIROCREW_HOME}}/{shared_prompt_relpath}"

        complete_def_relpath = _75_write_agent_def(
            root, complete_agent, prompt=prompt_value
        )
        incomplete_def_relpath = _75_write_agent_def(
            root, incomplete_agent, prompt=prompt_value
        )

        # Both agents' config.json entry present; only complete_agent
        # gets an agent_model_state.json pin.
        (root / "config.json").write_text(
            json.dumps(
                {
                    "agents": {
                        complete_agent: {"source": "local"},
                        incomplete_agent: {"source": "local"},
                    }
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        (root / "agent_model_state.json").write_text(
            json.dumps({complete_agent: {"model_managed": False}}, indent=2),
            encoding="utf-8",
        )

        changed = [complete_def_relpath, incomplete_def_relpath]

        result = registration.check_registrations(
            root, changed, _75_roots(root_a, root_b)
        )

        assert complete_agent in result.complete_agents
        assert incomplete_agent in result.incomplete_agents
        assert shared_prompt_relpath not in result.blocked_paths


# ---------------------------------------------------------------------------
# (7.5-g) Candidates named only from changed agents/<name>.json.
# ---------------------------------------------------------------------------


class TestCandidatesNamedOnlyFromChangedAgentDefinitions:
    """5.13: the ONLY thing that names a registration candidate is a

    changed ``agents/<name>.json`` path -- a changed
    ``config-bundles/agent-prompts/*.md`` file alone (already covered by
    ``TestPromptOnlyChangeIsUnrelated`` above) and a changed shared file
    alone must both yield zero candidates.
    """

    def test_only_shared_files_changed_yields_no_candidates(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "commit"
        root_a = tmp_path / "root-a"
        root_b = tmp_path / "root-b"
        root.mkdir()
        root_a.mkdir()
        root_b.mkdir()

        # config.json and agent_model_state.json both changed and both
        # carry some agent's key -- but no agents/<name>.json changed.
        _75_write_shared_files_with_key(root, _75_AGENT_NAME)

        changed = ["config.json", "agent_model_state.json"]

        result = registration.check_registrations(
            root, changed, _75_roots(root_a, root_b)
        )

        assert result.complete_agents == []
        assert result.incomplete_agents == {}
        assert result.blocked_paths == []
        assert set(result.unrelated_paths) == {"config.json", "agent_model_state.json"}


# ---------------------------------------------------------------------------
# (7.5-h) requirements.md 5.14 — shared parts judged from commit-tree
#         CONTENT regardless of changed_paths.
# ---------------------------------------------------------------------------


class TestSharedPartsJudgedFromTreeContentRegardlessOfChangedPaths:
    """5.14: an agent-JSON-only commit (config.json / agent_model_state.json

    NOT in ``changed_paths`` at all) is still COMPLETE when the commit
    tree's own copies of those shared files already carry the agent's
    key -- and still INCOMPLETE when the tree's copies are missing the
    key, regardless of ``changed_paths`` in both cases. This directly
    supersedes the old presence-in-changed_paths gate the pre-7.5 tests
    in this file encode.
    """

    def test_agent_json_only_commit_with_key_already_in_tree_is_complete(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "commit"
        root_a = tmp_path / "root-a"
        root_b = tmp_path / "root-b"
        root.mkdir()
        root_a.mkdir()
        root_b.mkdir()

        # The commit's checked-out tree already carries this agent's
        # shared-file entries (as it would for an already-registered
        # agent whose registration long predates this specific commit).
        _75_write_agent_def(root, _75_AGENT_NAME, prompt=None)
        _75_write_shared_files_with_key(root, _75_AGENT_NAME)

        # ONLY the agent definition is in changed_paths -- config.json
        # and agent_model_state.json are deliberately absent from the
        # list even though both files exist, with the key, in `root`.
        changed = [_75_AGENT_DEF_RELPATH]

        result = registration.check_registrations(
            root, changed, _75_roots(root_a, root_b)
        )

        assert _75_AGENT_NAME in result.complete_agents
        assert _75_AGENT_NAME not in result.incomplete_agents

    def test_agent_json_only_commit_with_key_missing_from_tree_is_incomplete(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "commit"
        root_a = tmp_path / "root-a"
        root_b = tmp_path / "root-b"
        root.mkdir()
        root_a.mkdir()
        root_b.mkdir()

        _75_write_agent_def(root, _75_AGENT_NAME, prompt=None)
        # Shared files exist in the tree but carry a DIFFERENT agent's
        # key only -- this agent's own key is genuinely absent.
        _75_write_shared_files_with_key(root, "some-other-agent")

        changed = [_75_AGENT_DEF_RELPATH]

        result = registration.check_registrations(
            root, changed, _75_roots(root_a, root_b)
        )

        assert _75_AGENT_NAME not in result.complete_agents
        assert _75_AGENT_NAME in result.incomplete_agents
        missing = " ".join(result.incomplete_agents[_75_AGENT_NAME]).lower()
        assert "config" in missing
        assert "model_state" in missing or "model state" in missing or "pin" in missing

    def test_shared_file_absent_entirely_from_tree_is_incomplete_fail_closed(
        self, tmp_path: Path
    ) -> None:
        """Sanity companion to 5.14's tree-content rule: when a shared

        file does not exist AT ALL in the commit tree (not merely absent
        from ``changed_paths``), the part is absent for every agent --
        fail closed, matching the existing malformed-JSON fail-closed
        behaviour this module already implements for the pre-7.5 rule.
        """
        root = tmp_path / "commit"
        root_a = tmp_path / "root-a"
        root_b = tmp_path / "root-b"
        root.mkdir()
        root_a.mkdir()
        root_b.mkdir()

        _75_write_agent_def(root, _75_AGENT_NAME, prompt=None)
        # Neither config.json nor agent_model_state.json exists anywhere
        # under `root` at all.

        changed = [_75_AGENT_DEF_RELPATH]

        result = registration.check_registrations(
            root, changed, _75_roots(root_a, root_b)
        )

        assert _75_AGENT_NAME not in result.complete_agents
        assert _75_AGENT_NAME in result.incomplete_agents


# ---------------------------------------------------------------------------
# Branch-coverage: duplicate candidate paths, and isolating each half of
# a compound "block this shared part" condition.
# ---------------------------------------------------------------------------


class TestDuplicateCandidatePathIsProcessedOnce:
    """The SAME ``agents/<name>.json`` relpath appearing twice in

    ``changed_paths`` (e.g. a poll/classify pass that double-reports a
    path) must not be treated as two candidates -- the first-pass dedup
    guard (``if agent_def_name not in agent_def_paths``) must skip the
    second occurrence's append while still recording its relpath, and
    the agent must be judged exactly once.
    """

    def test_duplicate_changed_path_for_same_agent_is_judged_once(
        self, tmp_path: Path
    ) -> None:
        root, root_a, roots = _prep_roots(tmp_path)
        _write_prompt_file(root, AGENT_NAME)
        _write_agent_def(root, AGENT_NAME)
        _write_config_json(root, AGENT_NAME)
        _write_agent_model_state(root, AGENT_NAME)
        changed = [
            AGENT_DEF_RELPATH,
            CONFIG_RELPATH,
            MODEL_STATE_RELPATH,
            AGENT_DEF_RELPATH,  # duplicate of the same candidate path
        ]

        result = registration.check_registrations(root, changed, roots)

        assert result.complete_agents == [AGENT_NAME]
        assert result.incomplete_agents == {}
        # Judged once, not twice: exactly one entry for this agent, and
        # its own definition path appears at most once in every list it
        # could show up in.
        assert result.complete_agents.count(AGENT_NAME) == 1

    def test_duplicate_changed_path_for_a_repeatedly_untracked_agent(
        self, tmp_path: Path
    ) -> None:
        """The same duplicate-candidate-path property, but for an agent

        whose prompt resolves to an UNTRACKED location -- proving the
        dedup guard also protects ``untracked_prompt_agents`` (which has
        its own independent, per-agent "already recorded" guard) from a
        double entry when the SAME candidate path is duplicated in
        ``changed_paths``.
        """
        root, root_a, roots = _prep_roots(tmp_path)
        untracked_prompt = "file:///outside/both/roots/prompt.md"
        _write_agent_def_with_prompt(root, AGENT_NAME, untracked_prompt)
        _write_config_json(root, AGENT_NAME)
        _write_agent_model_state(root, AGENT_NAME)
        changed = [
            AGENT_DEF_RELPATH,
            CONFIG_RELPATH,
            MODEL_STATE_RELPATH,
            AGENT_DEF_RELPATH,  # duplicate of the same candidate path
        ]

        result = registration.check_registrations(root, changed, roots)

        assert result.complete_agents == [AGENT_NAME]
        assert result.untracked_prompt_agents == [AGENT_NAME]
        assert result.untracked_prompt_agents.count(AGENT_NAME) == 1


class TestMissingPartsWithoutASharedEntryIsolatesEachBlockCondition:
    """The "block this shared file" guard at each missing-parts site is a

    compound condition: ``<entry-found> and <not already in
    blocked_paths>``. These tests isolate the FALSE side of the first
    half (``config_has_entry`` / ``model_state_has_entry`` false) so the
    append is skipped for the right reason -- the entry was never found
    at all, not merely "already blocked".
    """

    def test_missing_config_with_no_config_json_in_tree_blocks_nothing_for_config(
        self, tmp_path: Path
    ) -> None:
        """No ``config.json`` at all in the commit tree: the config part

        is missing, but since ``config_has_entry`` is False,
        ``config.json`` itself must NOT be added to ``blocked_paths`` --
        there is no config entry to have been "present but refused".
        """
        root, root_a, roots = _prep_roots(tmp_path)
        _write_prompt_file(root, AGENT_NAME)
        _write_agent_def(root, AGENT_NAME)
        _write_agent_model_state(root, AGENT_NAME)
        # config.json does not exist anywhere in the tree.
        changed = _changed_relpaths_for(root, AGENT_DEF_RELPATH, MODEL_STATE_RELPATH)

        result = registration.check_registrations(root, changed, roots)

        assert AGENT_NAME in result.incomplete_agents
        assert CONFIG_RELPATH not in result.blocked_paths
        assert AGENT_DEF_RELPATH in result.blocked_paths

    def test_missing_model_state_with_no_file_in_tree_blocks_nothing_for_it(
        self, tmp_path: Path
    ) -> None:
        """No ``agent_model_state.json`` at all in the commit tree: the

        model-state part is missing, but since ``model_state_has_entry``
        is False, ``agent_model_state.json`` itself must NOT be added to
        ``blocked_paths``.
        """
        root, root_a, roots = _prep_roots(tmp_path)
        _write_prompt_file(root, AGENT_NAME)
        _write_agent_def(root, AGENT_NAME)
        _write_config_json(root, AGENT_NAME)
        # agent_model_state.json does not exist anywhere in the tree.
        changed = _changed_relpaths_for(root, AGENT_DEF_RELPATH, CONFIG_RELPATH)

        result = registration.check_registrations(root, changed, roots)

        assert AGENT_NAME in result.incomplete_agents
        assert MODEL_STATE_RELPATH not in result.blocked_paths
        assert AGENT_DEF_RELPATH in result.blocked_paths


class TestRequiredPromptFileBlockedWhenEveryRequirerIsIncomplete:
    """requirements.md 5.13's blocking rule, positive case: a required,

    present, TRACKED prompt file referenced by exactly one agent, where
    that agent's registration is otherwise incomplete (missing its
    config.json entry) -- the prompt file itself must be blocked from
    applying, since EVERY agent that requires it is incomplete. This is
    the complement of ``TestSharedPromptNotBlockedByAnUnrelatedIncompleteAgent``
    above, which proves the file is NOT blocked when at least one
    requirer is complete; this proves it IS blocked when none are.
    """

    def test_prompt_required_by_one_incomplete_agent_is_blocked(
        self, tmp_path: Path
    ) -> None:
        root, root_a, roots = _prep_roots(tmp_path)
        _write_prompt_file(root, AGENT_NAME)
        _write_agent_def(root, AGENT_NAME)
        _write_agent_model_state(root, AGENT_NAME)
        # config.json exists but carries no entry for this agent ->
        # incomplete, and this is the ONLY agent requiring the prompt.
        (root / CONFIG_RELPATH).write_text(json.dumps({"agents": {}}), encoding="utf-8")
        changed = _changed_relpaths_for(
            root,
            AGENT_DEF_RELPATH,
            MODEL_STATE_RELPATH,
            TRACKED_PROMPT_RELPATH,
        )

        result = registration.check_registrations(root, changed, roots)

        assert AGENT_NAME not in result.complete_agents
        assert AGENT_NAME in result.incomplete_agents
        assert TRACKED_PROMPT_RELPATH in result.blocked_paths
