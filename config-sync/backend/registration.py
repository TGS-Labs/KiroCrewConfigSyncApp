"""Agent registration as one transaction over its required parts.

Covers design.md's registration section ("Agent registration is the one
class that is *not* a single file. The full registration is coordinated
writes — the agent definition, ``~/.kiro/agents/<name>.json``, the
``config.json`` ``agents{}`` entry, the ``agent_model_state.json`` pin,
and the prompt file where Requirement 5.11 makes it a required part. The
applier treats those parts as one transaction: every required part
present, or the registration is refused as incomplete.") and
requirements.md 5.6, 5.7, 5.11-5.14 (tasks.md 5.4, amended by 7.5).

This module never reimplements path matching or allowlist classification:
it consumes a commit's already-collected changed-path list and reasons
about it structurally, delegating prompt-reference resolution to
``backend.portable.resolve_reference`` and tracked-ness to
``backend.allowlist.is_tracked``.

    agents/<name>.json                 ~/.kiro/agents/<name>.json (per-agent)
    config.json                        shared; entry keyed by agent name
                                        under its top-level "agents" object
    agent_model_state.json             shared; pin keyed by agent name at
                                        the top level
    config-bundles/agent-prompts/<name>.md
                                        the agent's prompt file — required
                                        only when the definition's own
                                        ``prompt`` value is a ``file://``
                                        reference resolving to a tracked
                                        relpath (requirements.md 5.11)

A registration candidate is named ONLY by a changed ``agents/<name>.json``
(requirements.md 5.13) — never by a changed prompt file alone, and never
by an agent definition that exists on disk but was not itself part of
``changed_paths``.

The prompt part's derivation (requirements.md 5.11, 5.12):
    (a) a ``file://`` value resolving to a relpath the Requirement 1
        allowlist tracks is REQUIRED — present only when that relpath
        exists as a regular, non-symlink file in the APPROVED COMMIT'S
        TREE (i.e. under ``root`` — this module's own first argument,
        the same tree the agent definition and the two shared files are
        read from). The ``file://`` reference is still resolved against
        the live configured roots (``roots``) to map it to
        ``(root_id, relpath)`` and to check the allowlist, but that
        resolution never decides where the file itself must exist.
    (b) an inline string (anything not starting with ``file://``), or an
        absent, ``null``, or empty ``prompt``, satisfies the prompt part
        with no required file.
    (c) a ``file://`` value resolving to an untracked relpath, or to
        neither root, makes the prompt part NOT required — reported by
        agent name in ``untracked_prompt_agents``.
    A ``prompt`` present but neither a string nor ``null``, an
    unresolvable reference containing an empty or ``..`` segment, or an
    ``agents/<name>.json`` that does not parse as a JSON object, each
    make the registration incomplete (fail closed).

Shared-part evaluation (requirements.md 5.6, 5.14) reads ``config.json``
and ``agent_model_state.json`` from their path WITHIN the commit's
checked-out tree — never gated on ``changed_paths`` membership — and
counts a part present only when the file parses as a JSON object AND
carries the agent's own key at the required location. A shared file that
does not exist, or does not parse as a JSON object, makes that part
absent for every agent (fail closed) — an unparseable or missing shared
file cannot be trusted to carry anyone's entry.

A required prompt file is blocked from applying only when EVERY changed
agent definition referencing it belongs to an incomplete registration
(requirements.md 5.13) — a prompt shared by a complete and an incomplete
registration in the same commit is not blocked.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Pattern

from backend import allowlist, portable

_AGENT_DEF_PATTERN: Pattern[str] = re.compile(r"^agents/(?P<name>[^/]+)\.json$")
_CONFIG_RELPATH = "config.json"
_MODEL_STATE_RELPATH = "agent_model_state.json"

_PART_PROMPT = "prompt file (config-bundles/agent-prompts/<name>.md)"
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
        complete_agents: Names of agents whose registration has all
            required parts present in the commit, in first-seen order.
        incomplete_agents: Mapping of agent name to the human-readable
            names of exactly the parts missing from the commit — never
            naming a part that is actually present.
        blocked_paths: The changed paths that belong to an incomplete
            registration's *present* parts — refused from applying as a
            partial registration, even though the bytes exist in the
            commit. A shared file is blocked for an incomplete agent
            only when that agent's own key is genuinely inside it. A
            prompt file is blocked only when EVERY changed agent
            definition referencing it belongs to an incomplete
            registration.
        unrelated_paths: Changed paths that are not part of any
            registration this module recognises.
        propagation_report: Mapping of agent name to the requirements.md
            5.7 reporting string — present only for agents in
            ``complete_agents``.
        untracked_prompt_agents: Names of agents (regardless of complete
            or incomplete) whose definition's ``prompt`` is a ``file://``
            reference resolving to an untracked relpath, or to neither
            configured root (requirements.md 5.11(c)).
    """

    complete_agents: List[str] = field(default_factory=list)
    incomplete_agents: Dict[str, List[str]] = field(default_factory=dict)
    blocked_paths: List[str] = field(default_factory=list)
    unrelated_paths: List[str] = field(default_factory=list)
    propagation_report: Dict[str, str] = field(default_factory=dict)
    untracked_prompt_agents: List[str] = field(default_factory=list)


def _agent_name_from_agent_def(relpath: str) -> Optional[str]:
    match = _AGENT_DEF_PATTERN.match(relpath)
    return match.group("name") if match else None


def _load_json_object(path: Path) -> dict:
    """Read ``path`` as a JSON object; any read/parse failure -> ``{}``.

    Fail closed: a file that cannot be read, or does not parse as a JSON
    object, is treated the same as one carrying no agent's entry at all.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _config_has_agent_entry(root: Path, agent_name: str) -> bool:
    """Does ``config.json``'s top-level ``agents`` object carry the key?

    Read from the commit tree unconditionally (requirements.md 5.14) —
    never gated on whether ``config.json`` is itself a changed path.
    """
    document = _load_json_object(root / _CONFIG_RELPATH)
    agents = document.get("agents")
    return isinstance(agents, dict) and agent_name in agents


def _model_state_has_agent_entry(root: Path, agent_name: str) -> bool:
    """Does ``agent_model_state.json``'s top-level object carry the key?

    Read from the commit tree unconditionally (requirements.md 5.14).
    """
    document = _load_json_object(root / _MODEL_STATE_RELPATH)
    return agent_name in document


@dataclass
class _PromptOutcome:
    """The prompt part's derivation outcome for one agent definition.

    Attributes:
        required_root_id: The root id ("A"/"B") the required relpath
            resolved against, or ``None`` when no file is required.
        required_relpath: The tracked relpath the prompt part requires to
            exist, or ``None`` when no file is required (inline/absent/
            null/empty prompt, or an untracked/out-of-root reference).
        untracked: True when the prompt is a ``file://`` reference that
            resolved to neither root, or to a root but an untracked
            relpath (requirements.md 5.11(c)).
        incomplete: True when the prompt value itself makes the
            registration incomplete regardless of file presence (a
            non-string/non-null value, or a resolvable reference with an
            empty/``..`` segment — requirements.md 5.11/5.12 fail-closed
            cases).
    """

    required_root_id: Optional[str] = None
    required_relpath: Optional[str] = None
    untracked: bool = False
    incomplete: bool = False


def _has_unsafe_segment(relpath: str) -> bool:
    """True when ``relpath`` contains an empty or ``..`` path segment."""
    return any(segment in ("", "..") for segment in relpath.split("/"))


def _resolve_prompt_outcome(
    prompt_value: object, roots: Mapping[str, Path]
) -> _PromptOutcome:
    """Derive the prompt part's requirement from the definition's own

    ``prompt`` value (requirements.md 5.11, 5.12). Never looks at the
    agent's name.
    """
    if prompt_value is None:
        return _PromptOutcome()
    if not isinstance(prompt_value, str):
        # Present but neither a string nor null -> incomplete (5.11).
        return _PromptOutcome(incomplete=True)
    if prompt_value == "" or not prompt_value.startswith("file://"):
        # Inline string (including empty string) -> satisfied, no file.
        return _PromptOutcome()

    resolved = portable.resolve_reference(prompt_value, roots)
    if resolved is None:
        # Outside both roots (e.g. site-packages) -> not required,
        # reported as an untracked location.
        return _PromptOutcome(untracked=True)

    root_id, relpath = resolved
    if _has_unsafe_segment(relpath):
        # Empty or ".." segment -> fail closed to incomplete.
        return _PromptOutcome(incomplete=True)

    if not allowlist.is_tracked(root_id, relpath):
        # Resolves to a root, but the allowlist does not track that
        # relpath (e.g. config-bundles/skills/**) -> not required,
        # reported as an untracked location.
        return _PromptOutcome(untracked=True)

    return _PromptOutcome(required_root_id=root_id, required_relpath=relpath)


def _has_symlinked_component(root: Path, relpath: str) -> bool:
    """True if any component of ``relpath``, walked from ``root``, is a

    symlink. Mirrors ``backend.apply._is_unsafe_source``'s own per-
    segment ``lstat``/``is_symlink()`` walk exactly -- reimplemented
    here rather than imported, because ``backend.apply`` already imports
    ``backend.registration`` (registration handling is one step inside
    ``apply_commit``), and importing ``apply`` back would create a
    cycle. ``is_symlink()`` itself never follows the link (it is an
    ``lstat``, not a ``stat``), so a symlinked ancestor anywhere between
    ``root`` and the leaf -- pointing inside or entirely outside the
    commit tree -- is caught before the final component is even reached.
    """
    current = root
    for segment in relpath.split("/"):
        current = current / segment
        if current.is_symlink():
            return True
    return False


def _prompt_file_present(root: Path, relpath: str) -> bool:
    """Does ``relpath`` exist as a regular, non-symlink file in the

    APPROVED COMMIT'S TREE (requirements.md 5.12)? The ``file://``
    reference is resolved against the live configured roots (to map it
    to ``(root_id, relpath)`` and to check the Requirement 1 allowlist),
    but the required part's presence is checked against ``root`` --
    ``check_registrations``'s own first argument, the commit-scoped tree
    that also holds the agent definition and the two shared files --
    never against a live configured root.

    "Regular, non-symlink file" covers every path component, not only
    the leaf (H2): a relpath resolving through a symlinked ANCESTOR
    directory (``config-bundles/`` or ``config-bundles/agent-prompts/``
    itself being a symlink) is a redirect, not a plain file at that path
    in the tree as committed, even when the leaf it resolves through is
    itself a real, non-symlink file.
    """
    if _has_symlinked_component(root, relpath):
        return False
    target = root / relpath
    return target.is_file() and not target.is_symlink()


def check_registrations(
    root: Path, changed_paths: List[str], roots: Mapping[str, Path]
) -> Result:
    """Check every agent registration touched by ``changed_paths``.

    Args:
        root: The commit-scoped directory the paths are relative to, and
            the tree shared-file content (Requirement 5.14) and prompt
            file presence (Requirement 5.12) are read from.
        changed_paths: The commit's changed-path list, each already
            relative to ``root`` and ``/``-separated. A registration
            candidate is named ONLY by a changed ``agents/<name>.json``
            entry here (requirements.md 5.13).
        roots: ``{"A": Path, "B": Path}`` — this host's live configured
            roots, the same shape ``backend.portable``/``backend.collect``
            use. A ``file://`` prompt reference is resolved against
            these, never against ``root``.

    Returns:
        A ``Result`` partitioning every registration-shaped path across
        ``complete_agents`` / ``incomplete_agents`` / ``blocked_paths``,
        every other changed path into ``unrelated_paths``, a
        requirements.md 5.7 report for each complete registration, and
        the set of agents whose prompt references an untracked location.
    """
    changed_set = set(changed_paths)

    # First pass: discover every agent name referenced by a changed
    # agents/<name>.json, in first-seen order, so results are
    # deterministic. Only a changed agent definition names a candidate
    # (requirements.md 5.13) -- a changed prompt file never does.
    agent_names: List[str] = []
    agent_def_paths: Dict[str, str] = {}

    for relpath in changed_paths:
        agent_def_name = _agent_name_from_agent_def(relpath)
        if agent_def_name is not None:
            if agent_def_name not in agent_def_paths:
                agent_names.append(agent_def_name)
            agent_def_paths[agent_def_name] = relpath

    result = Result()
    config_claimed = False
    model_state_claimed = False

    # Second pass: for each candidate, derive its prompt requirement,
    # check every part, and record the outcome. Track, per prompt
    # relpath, which agents require it, so blocking can be resolved only
    # after every candidate is judged (requirements.md 5.13: a prompt
    # shared by a complete and an incomplete registration must not be
    # blocked).
    prompt_requirers: Dict[str, List[str]] = {}

    # C3 (fail-closed rule, pending operator confirmation): every agent
    # whose OWN key is present in a shared file (config.json /
    # agent_model_state.json) that ends up blocked must itself be
    # treated as blocked too -- its agents/<name>.json is refused from
    # applying alongside the shared file, and it is reported incomplete
    # naming the agent whose incompleteness caused the block. Tracked
    # per shared relpath so this can be resolved only after every
    # candidate in the commit has been judged (mirrors the prompt
    # third-pass shape above).
    config_keyholders: Dict[str, List[str]] = {}
    model_state_keyholders: Dict[str, List[str]] = {}
    blocking_agent_by_shared_relpath: Dict[str, str] = {}

    for agent_name in agent_names:
        agent_def_path = agent_def_paths[agent_name]
        missing: List[str] = []

        # `_load_json_object` fails closed to `{}` for an unparseable or
        # non-object definition, so parsing must be checked separately
        # from content -- an unparseable `agents/<name>.json` makes the
        # registration incomplete (requirements.md 5.12), it is not
        # merely "an object with no prompt key".
        if not _definition_parses_as_object(root / agent_def_path):
            missing.append(_PART_AGENT_DEF)
            result.incomplete_agents[agent_name] = missing
            if agent_def_path not in result.blocked_paths:
                result.blocked_paths.append(agent_def_path)
            continue

        definition = _load_json_object(root / agent_def_path)
        prompt_value = definition.get("prompt")
        prompt_outcome = _resolve_prompt_outcome(prompt_value, roots)

        if prompt_outcome.untracked:
            if agent_name not in result.untracked_prompt_agents:
                result.untracked_prompt_agents.append(agent_name)

        if prompt_outcome.incomplete:
            missing.append(_PART_PROMPT)
        elif prompt_outcome.required_relpath is not None:
            relpath = prompt_outcome.required_relpath
            prompt_requirers.setdefault(relpath, []).append(agent_name)
            if not _prompt_file_present(root, relpath):
                missing.append(_PART_PROMPT)

        config_has_entry = _config_has_agent_entry(root, agent_name)
        model_state_has_entry = _model_state_has_agent_entry(root, agent_name)

        if config_has_entry:
            config_claimed = True
            config_keyholders.setdefault(_CONFIG_RELPATH, []).append(agent_name)
        if model_state_has_entry:
            model_state_claimed = True
            model_state_keyholders.setdefault(_MODEL_STATE_RELPATH, []).append(
                agent_name
            )

        if not config_has_entry:
            missing.append(_PART_CONFIG)
        if not model_state_has_entry:
            missing.append(_PART_MODEL_STATE)

        if missing:
            result.incomplete_agents[agent_name] = missing
        else:
            result.complete_agents.append(agent_name)
            result.propagation_report[agent_name] = _ROSTER_PICKUP_REPORT

        # Block this agent's own present, changed parts -- the agent
        # definition itself, plus each shared file ONLY when this
        # agent's own key is genuinely inside it.
        if missing:
            if agent_def_path not in result.blocked_paths:
                result.blocked_paths.append(agent_def_path)
            if config_has_entry and _CONFIG_RELPATH not in result.blocked_paths:
                result.blocked_paths.append(_CONFIG_RELPATH)
                blocking_agent_by_shared_relpath.setdefault(_CONFIG_RELPATH, agent_name)
            if (
                model_state_has_entry
                and _MODEL_STATE_RELPATH not in result.blocked_paths
            ):
                result.blocked_paths.append(_MODEL_STATE_RELPATH)
                blocking_agent_by_shared_relpath.setdefault(
                    _MODEL_STATE_RELPATH, agent_name
                )

    # Third pass: block a required prompt file only when EVERY agent
    # that requires it is incomplete (requirements.md 5.13).
    for relpath, requirers in prompt_requirers.items():
        if relpath not in changed_set:
            continue
        if all(requirer in result.incomplete_agents for requirer in requirers):
            if relpath not in result.blocked_paths:
                result.blocked_paths.append(relpath)

    # Fourth pass (C3, fail-closed interim ruling pending operator
    # confirmation): if config.json or agent_model_state.json is blocked
    # from applying (because some agent in the commit is incomplete),
    # then EVERY agent whose key is present in that committed shared
    # file is also treated as blocked -- even an otherwise-complete
    # agent -- because that shared file's entry for it will not be live
    # either. Its own agents/<name>.json is added to blocked_paths and
    # it moves from complete_agents to incomplete_agents, reported with
    # a reason naming the agent whose incompleteness blocked the shared
    # file. This prevents an agent ever being left half-registered:
    # config.json refused, but a picker/planner reading complete_agents
    # believing that agent's shared-file entry is live.
    for shared_relpath, keyholders in (
        (_CONFIG_RELPATH, config_keyholders.get(_CONFIG_RELPATH, [])),
        (_MODEL_STATE_RELPATH, model_state_keyholders.get(_MODEL_STATE_RELPATH, [])),
    ):
        if shared_relpath not in result.blocked_paths:
            continue
        blocking_agent = blocking_agent_by_shared_relpath.get(shared_relpath, "")
        part_name = (
            _PART_CONFIG if shared_relpath == _CONFIG_RELPATH else _PART_MODEL_STATE
        )
        for keyholder_name in keyholders:
            if keyholder_name in result.incomplete_agents:
                continue
            reason = (
                f"{part_name} is blocked because agent "
                f"'{blocking_agent}' in the same commit is incomplete"
            )
            result.incomplete_agents[keyholder_name] = [reason]
            if keyholder_name in result.complete_agents:
                result.complete_agents.remove(keyholder_name)
            result.propagation_report.pop(keyholder_name, None)
            keyholder_def_path = agent_def_paths.get(keyholder_name)
            if (
                keyholder_def_path is not None
                and keyholder_def_path not in result.blocked_paths
            ):
                result.blocked_paths.append(keyholder_def_path)

    # A changed path is "unrelated" only when no registration this commit
    # touches claims it: every changed agent definition path is claimed
    # by its own agent's check above; a changed prompt file is claimed
    # only when at least one changed agent definition in this commit
    # actually requires it; a shared file is claimed only if at least one
    # checked agent's own entry was actually found inside it.
    attributed = set(agent_def_paths.values())
    for relpath, requirers in prompt_requirers.items():
        if requirers:
            attributed.add(relpath)
    if config_claimed:
        attributed.add(_CONFIG_RELPATH)
    if model_state_claimed:
        attributed.add(_MODEL_STATE_RELPATH)

    for relpath in changed_paths:
        if relpath not in attributed:
            result.unrelated_paths.append(relpath)

    return result


def _definition_parses_as_object(path: Path) -> bool:
    """True iff ``path`` exists and parses as a JSON object.

    Distinct from ``_load_json_object``, which fails closed to ``{}`` for
    both "parses as an empty object" and "did not parse at all" — this
    module must tell those two apart to know whether the agent-definition
    part itself is present (requirements.md 5.12: an unparseable
    ``agents/<name>.json`` makes the registration incomplete).
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict)
