"""Agent registration as one transaction over four coordinated files.

Covers design.md's registration section ("Agent registration is the one
class that is *not* a single file. The full registration is four
coordinated writes — prompt file, ``~/.kiro/agents/<name>.json``, the
``config.json`` ``agents{}`` entry, and the ``agent_model_state.json``
pin. The applier treats those four as one transaction: all four present,
or the registration is refused as incomplete.") and requirements.md 5.6,
5.7.

This module never reimplements path matching or allowlist classification:
it consumes a commit's already-collected changed-path list and reasons
about it structurally. The two per-agent paths (prompt file, agent
definition) are identified by path shape alone. The two SHARED files
(``config.json``, ``agent_model_state.json``) are shared across every
agent's registration, so requirements.md 5.6 names the *entry inside*
each file — the ``agents{}`` entry, the model pin — not the file's mere
presence in the commit: a commit that changes ``config.json`` only for
agent B must not make agent A's registration look complete merely
because the shared file was touched. A shared part therefore counts as
present for a given agent only when the relpath is in the commit's
changed paths AND the file at that path parses as JSON AND that JSON
carries the agent's own key. Unreadable or malformed JSON is treated as
the part being absent for every agent (fail closed) — an unparseable
shared file cannot be trusted to carry anyone's entry.

    agent-prompts/<name>.md          the agent's prompt file (per-agent)
    agents/<name>.json                ~/.kiro/agents/<name>.json (per-agent)
    config.json                       shared; entry keyed by agent name
                                       under its top-level "agents" object
    agent_model_state.json            shared; pin keyed by agent name at
                                       the top level

Because the two shared files are shared, a set of changed paths only
ever names a candidate registration when at least one of the two
per-agent-named paths (the prompt file or the agent definition file) is
present in the commit — a commit that touches only the two shared files,
naming no agent via a per-agent path, describes no registration at all
under this module's contract.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Pattern

_PROMPT_PATTERN: Pattern[str] = re.compile(r"^agent-prompts/(?P<name>[^/]+)\.md$")
_AGENT_DEF_PATTERN: Pattern[str] = re.compile(r"^agents/(?P<name>[^/]+)\.json$")
_CONFIG_RELPATH = "config.json"
_MODEL_STATE_RELPATH = "agent_model_state.json"

_PART_PROMPT = "prompt file (agent-prompts/<name>.md)"
_PART_AGENT_DEF = "agent definition (agents/<name>.json)"
_PART_CONFIG = "config.json agents{} entry"
_PART_MODEL_STATE = "agent_model_state.json pin"

_ROSTER_PICKUP_REPORT = (
    "spawn_run's roster picks up this registration without a restart "
    "(the agents directory is cached on a file-count and newest-mtime "
    "signature); the dashboard's agent picker is a separate read path "
    "that may need its own refresh."
)


@dataclass
class Result:
    """The registration-check outcome for one commit's changed paths.

    Attributes:
        complete_agents: Names of agents whose registration has all four
            parts present in the commit, in first-seen order.
        incomplete_agents: Mapping of agent name to the human-readable
            names of exactly the parts missing from the commit — never
            naming a part that is actually present.
        blocked_paths: The changed paths that belong to an incomplete
            registration's *present* parts — refused from applying as a
            partial registration, even though the bytes exist in the
            commit. A shared file is blocked for an incomplete agent
            only when that agent's own key is genuinely inside it; it is
            never blocked merely for being present in the commit.
        unrelated_paths: Changed paths that are not part of any
            registration this module recognises (neither a per-agent
            path shape nor a shared file carrying at least one checked
            agent's key).
        propagation_report: Mapping of agent name to the requirements.md
            5.7 reporting string — present only for agents in
            ``complete_agents``; an incomplete/refused registration was
            never applied and so carries no propagation state.
    """

    complete_agents: List[str] = field(default_factory=list)
    incomplete_agents: Dict[str, List[str]] = field(default_factory=dict)
    blocked_paths: List[str] = field(default_factory=list)
    unrelated_paths: List[str] = field(default_factory=list)
    propagation_report: Dict[str, str] = field(default_factory=dict)


def _agent_name_from_prompt(relpath: str) -> str | None:
    match = _PROMPT_PATTERN.match(relpath)
    return match.group("name") if match else None


def _agent_name_from_agent_def(relpath: str) -> str | None:
    match = _AGENT_DEF_PATTERN.match(relpath)
    return match.group("name") if match else None


def _load_json_object(path: Path) -> dict:
    """Read ``path`` as a JSON object; any read/parse failure -> ``{}``.

    Fail closed: a shared file that cannot be read, or does not parse as
    a JSON object, is treated the same as a shared file carrying no
    agent's entry at all — never as if it happened to carry one.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _config_has_agent_entry(root: Path, agent_name: str) -> bool:
    """Does ``config.json``'s top-level ``agents`` object carry the key?"""
    document = _load_json_object(root / _CONFIG_RELPATH)
    agents = document.get("agents")
    return isinstance(agents, dict) and agent_name in agents


def _model_state_has_agent_entry(root: Path, agent_name: str) -> bool:
    """Does ``agent_model_state.json``'s top-level object carry the key?"""
    document = _load_json_object(root / _MODEL_STATE_RELPATH)
    return agent_name in document


def check_registrations(root: Path, changed_paths: List[str]) -> Result:
    """Check every agent registration touched by ``changed_paths``.

    Args:
        root: The commit-scoped directory the paths are relative to.
            ``config.json`` and ``agent_model_state.json`` are shared
            across every agent's registration, so their on-disk content
            under ``root`` is read to confirm a specific agent's own
            entry/pin is genuinely inside — path presence in
            ``changed_paths`` alone is not sufficient (requirements.md
            5.6 names the entry, not the file).
        changed_paths: The commit's changed-path list, each already
            relative to ``root`` and ``/``-separated.

    Returns:
        A ``Result`` partitioning every registration-shaped path across
        ``complete_agents`` / ``incomplete_agents`` / ``blocked_paths``,
        every other changed path into ``unrelated_paths``, and a
        requirements.md 5.7 report for each complete registration.
    """
    changed_set = set(changed_paths)
    config_changed = _CONFIG_RELPATH in changed_set
    model_state_changed = _MODEL_STATE_RELPATH in changed_set

    # First pass: discover every agent name referenced by a per-agent path,
    # in first-seen order, so results are deterministic.
    agent_names: List[str] = []
    prompt_paths: Dict[str, str] = {}
    agent_def_paths: Dict[str, str] = {}

    for relpath in changed_paths:
        prompt_name = _agent_name_from_prompt(relpath)
        if prompt_name is not None:
            if prompt_name not in prompt_paths:
                agent_names.append(prompt_name)
            prompt_paths[prompt_name] = relpath
            continue

        agent_def_name = _agent_name_from_agent_def(relpath)
        if agent_def_name is not None:
            if agent_def_name not in agent_def_paths:
                if agent_def_name not in prompt_paths:
                    agent_names.append(agent_def_name)
            agent_def_paths[agent_def_name] = relpath

    result = Result()
    config_claimed = False
    model_state_claimed = False

    for agent_name in agent_names:
        prompt_path = prompt_paths.get(agent_name)
        agent_def_path = agent_def_paths.get(agent_name)
        config_has_entry = config_changed and _config_has_agent_entry(root, agent_name)
        model_state_has_entry = model_state_changed and _model_state_has_agent_entry(
            root, agent_name
        )

        missing: List[str] = []
        present_paths: List[str] = []

        if prompt_path is not None:
            present_paths.append(prompt_path)
        else:
            missing.append(_PART_PROMPT)

        if agent_def_path is not None:
            present_paths.append(agent_def_path)
        else:
            missing.append(_PART_AGENT_DEF)

        if config_has_entry:
            config_claimed = True
        else:
            missing.append(_PART_CONFIG)

        if model_state_has_entry:
            model_state_claimed = True
        else:
            missing.append(_PART_MODEL_STATE)

        if missing:
            result.incomplete_agents[agent_name] = missing
            # Block this agent's own present per-agent paths, plus each
            # shared file ONLY when this agent's own key is genuinely
            # inside it — never merely because the shared file is
            # present in the commit, since it may carry a different,
            # complete, registration's entry instead.
            for path in present_paths:
                if path not in result.blocked_paths:
                    result.blocked_paths.append(path)
            if config_has_entry and _CONFIG_RELPATH not in result.blocked_paths:
                result.blocked_paths.append(_CONFIG_RELPATH)
            if (
                model_state_has_entry
                and _MODEL_STATE_RELPATH not in result.blocked_paths
            ):
                result.blocked_paths.append(_MODEL_STATE_RELPATH)
        else:
            result.complete_agents.append(agent_name)
            result.propagation_report[agent_name] = _ROSTER_PICKUP_REPORT

    # A changed path is "unrelated" only when no registration this commit
    # touches claims it: every per-agent prompt/definition path is always
    # claimed by its own agent's check above; a shared file (config.json /
    # agent_model_state.json) is claimed only if at least one checked
    # agent's own entry was actually found inside it.
    attributed = set(prompt_paths.values()) | set(agent_def_paths.values())
    if config_claimed:
        attributed.add(_CONFIG_RELPATH)
    if model_state_claimed:
        attributed.add(_MODEL_STATE_RELPATH)
    for relpath in changed_paths:
        if relpath not in attributed:
            result.unrelated_paths.append(relpath)

    return result
