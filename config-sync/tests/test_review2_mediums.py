"""Failing tests for senior-review round-2 Medium findings.

Covers three Medium findings from the config-sync senior review round 2:

(1) ``backend/sanitize.py``: a ``hooks.json`` hook whose ``command`` is not a
    string, or an ``mcp.json`` server whose ``command`` is not a string or
    whose ``args`` is not a list of strings, is currently KEPT without being
    vetted at all — ``_hook_shell_command``/``_mcp_server_shell_command``
    return ``""`` for a non-string/malformed shape, and both
    ``sanitize_hooks``/``sanitize_mcp_servers`` only call the vet when the
    derived command string is truthy. A non-string ``command`` (or a
    non-list ``args``) is exactly the shape a hand-crafted malicious commit
    would use to smuggle a launcher past the vet entirely (the vet never
    even runs), so this must be DROPPED and named — fail-closed, per
    Requirement 6.9's posture for any other vet-failing entry.

(2) ``backend/apply.py`` (``_changed_hook_commands``/``_changed_mcp_commands``,
    called from ``_apply_one_file`` around lines 887-899): ``ApplyResult.
    changed_commands`` is built ONLY from ``command_final_doc`` — the
    sanitized, KEPT-ONLY document — so a command the vet DROPPED never
    appears in ``changed_commands`` at all. Requirements.md 6.10 is explicit
    that every added/changed command is listed "whether it survives the vet
    or is dropped under 6.9" — a dropped command must still be listed (and
    is also separately named in ``dropped_cron_names`` per 6.9's own
    reporting duty).

(3) ``backend/registration.py`` (fourth pass, C3 cascade, ~line 447): the C3
    cascade that blocks every keyholder of a blocked shared file
    (``config.json``/``agent_model_state.json``) runs AFTER the earlier
    per-agent pass that already decided ``missing`` (and therefore whether
    to apply the model pin / prompt) for each agent. An agent later swept
    into the C3 cascade — because ANOTHER agent's incompleteness blocked the
    shared file carrying this agent's own key — is moved into
    ``incomplete_agents`` and its own ``agents/<name>.json`` is added to
    ``blocked_paths``, but nothing blocks the *shared files themselves*
    (``config.json``/``agent_model_state.json``) from applying their
    already-decided bytes, and nothing in ``apply.py``'s per-file loop
    re-consults ``incomplete_agents`` before writing a file that is not
    itself in ``blocked_paths``. Per Requirement 5.15, a blocked shared file
    must block EVERY agent whose key it carries — including that agent's
    model pin and prompt, not only its own ``agents/<name>.json`` — but the
    model pin (``agent_model_state.json``) and prompt file for the
    C3-swept-in agent are never added to ``blocked_paths``, so they still
    apply live for an agent this module itself now calls incomplete.

Expected RED state (TDD, testing-standards.md's Mutation Requirement — each
assertion below is checked against a fixture engineered to violate exactly
the property it claims):

    (1) fails because the malformed-type entry survives in the sanitized
        store / is absent from ``dropped_names`` — the vet is never
        consulted for it.
    (2) fails because a dropped command is absent from
        ``ApplyResult.changed_commands`` (only the AttributeError-free
        ``getattr(..., [])`` empty-list path, or a genuinely empty list).
    (3) fails because the C3-swept-in agent's model pin and/or prompt file
        still land live (present in ``result.applied``) despite the agent
        being reported incomplete.

Fixture conventions follow this project's own standing lesson (five prior
defects from mocked git/subprocess) and the sibling files' own patterns:
``tests/test_apply_command_vet.py`` for the real-git bundle-repo + state
fixtures and the hooks/mcp JSON shapes; ``tests/test_registration_review1.py``
for the multi-agent registration fixture helpers. No mocks of git or of
apply/registration internals — this file drives the REAL
``backend.apply.apply_commit`` / ``backend.sanitize.sanitize_hooks`` /
``backend.sanitize.sanitize_mcp_servers`` / ``backend.registration.
check_registrations`` seams end to end.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from backend import apply, registration, sanitize
from backend import state as state_module

# ---------------------------------------------------------------------------
# Real-git fixture helpers — identical convention to test_apply_command_vet.py
# (this project's standing lesson: mocked subprocess for git has produced
# defects here five times).
# ---------------------------------------------------------------------------

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    )


def _init_bundle_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "bundle-repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "initial", cwd=repo)
    return repo


def _head_sha(repo: Path) -> str:
    return _git("rev-parse", "HEAD", cwd=repo).stdout.strip()


@pytest.fixture
def bundle_repo(tmp_path: Path) -> Path:
    return _init_bundle_repo(tmp_path)


@pytest.fixture
def target_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root_a = tmp_path / "kirocrew-home"
    root_b = tmp_path / "kiro-home"
    root_a.mkdir()
    root_b.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(root_a))
    monkeypatch.setenv("KIRO_HOME", str(root_b))
    return tmp_path


@pytest.fixture
def state_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> state_module.StateStore:
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(tmp_path / "config-sync-state"))
    return state_module.load_state()


def _seed_pending(store: state_module.StateStore, sha: str) -> None:
    store.set_pending(
        sha=sha,
        author="a",
        subject="s",
        classified_paths={},
        ignored_paths=[],
        touched_classes=[],
    )


def _fake_vet(command: str) -> str | None:
    if "$(" in command or "`" in command:
        return "Error: command blocked: command substitution"
    return None


# ---------------------------------------------------------------------------
# (1) sanitize.py: non-string command / non-list-of-strings args must be
#     dropped and named, not kept unvetted.
# ---------------------------------------------------------------------------


def test_sanitize_hooks_drops_hook_with_non_string_command() -> None:
    """A hooks.json entry whose ``command`` is not a string (here an int)

    is currently returned by ``_hook_shell_command`` as ``""``, so
    ``sanitize_hooks`` never calls the vet at all and keeps the entry
    verbatim — smuggling a non-string command straight past the vet.
    Requirement 6.9's fail-closed posture must apply: an un-vettable
    command shape is DROPPED and named, exactly like a vet rejection.
    """
    store = {
        "hooks": [
            {
                "id": "h1",
                "name": "malformed-command-hook",
                "event": "Stop",
                "matcher": "*",
                "command": 12345,
            },
            {
                "id": "h2",
                "name": "safe-hook",
                "event": "Stop",
                "matcher": "*",
                "command": "echo hi",
            },
        ]
    }

    result = sanitize.sanitize_hooks(store, vet=_fake_vet)

    assert "malformed-command-hook" in result.dropped_names, (
        "a hook whose command is not a string must be dropped and named, "
        "not silently kept because _hook_shell_command treats it as "
        "having no command to vet"
    )
    kept_names = {h["name"] for h in result.sanitized_store["hooks"]}
    assert "malformed-command-hook" not in kept_names
    assert "safe-hook" in kept_names


def test_sanitize_mcp_servers_drops_server_with_non_string_command() -> None:
    """An mcp.json server whose ``command`` is not a string (here a list)

    is currently treated by ``_mcp_server_shell_command`` as carrying no
    command (returns ``""``), so the vet never runs and the malformed
    entry survives untouched.
    """
    store = {
        "mcpServers": {
            "malformed-command-server": {
                "command": ["python3", "-c", "evil"],
                "args": ["-y"],
            },
            "safe-server": {"command": "npx", "args": ["-y", "@example/server"]},
        }
    }

    result = sanitize.sanitize_mcp_servers(store, vet=_fake_vet)

    assert "malformed-command-server" in result.dropped_names, (
        "an mcp server whose command is not a string must be dropped and "
        "named, not silently kept because _mcp_server_shell_command "
        "treats a non-string command as absent"
    )
    kept = result.sanitized_store["mcpServers"]
    assert "malformed-command-server" not in kept
    assert "safe-server" in kept


def test_sanitize_mcp_servers_drops_server_with_non_list_args() -> None:
    """An mcp.json server whose ``command`` IS a valid string but whose

    ``args`` is not a list of strings (here a bare string, a shape that
    could carry an unvetted shell-injection payload of its own) is
    currently accepted by ``_mcp_server_shell_command``: a non-list
    ``args`` falls back to an empty ``args_list``, so the vet only ever
    sees the bare ``command`` and the malformed ``args`` value is
    dropped from vetting entirely rather than the whole entry being
    refused. Requirement 6.8 requires ``args`` to be joined into the
    vetted line precisely so nothing hides there — a malformed ``args``
    shape must fail closed the same way a vet rejection does, not be
    silently ignored.
    """
    store = {
        "mcpServers": {
            "malformed-args-server": {
                "command": "npx",
                "args": "-y $(whoami)",
            },
            "safe-server": {"command": "npx", "args": ["-y", "@example/server"]},
        }
    }

    result = sanitize.sanitize_mcp_servers(store, vet=_fake_vet)

    assert "malformed-args-server" in result.dropped_names, (
        "an mcp server whose args is not a list of strings must be "
        "dropped and named -- a non-list args must not be silently "
        "treated as an empty args list, leaving the vet blind to it"
    )
    kept = result.sanitized_store["mcpServers"]
    assert "malformed-args-server" not in kept
    assert "safe-server" in kept


def test_sanitize_mcp_servers_drops_server_with_non_string_args_elements() -> None:
    """An mcp.json server whose ``args`` IS a list, but contains an

    element that is not itself a string (here a nested dict with no
    substitution-shaped text at all), is a second malformed-``args``
    shape Requirement 6.8 must also fail closed on — the current
    implementation coerces every element with ``str(item)``, which never
    fails and silently stringifies whatever structure is present into
    the vetted line instead of refusing the entry outright for carrying
    a non-string args element. The dict's own content is deliberately
    vet-clean (no ``$(``/backtick substring anywhere) so this test can
    only pass for the RIGHT reason — the type check catching the
    non-string element — never because the stringified value happens to
    also trip the command-substitution pattern.
    """
    store = {
        "mcpServers": {
            "malformed-args-element-server": {
                "command": "npx",
                "args": ["-y", {"nested": "clean-value"}],
            },
            "safe-server": {"command": "npx", "args": ["-y", "@example/server"]},
        }
    }

    result = sanitize.sanitize_mcp_servers(store, vet=_fake_vet)

    assert "malformed-args-element-server" in result.dropped_names, (
        "an mcp server whose args list contains a non-string element "
        "must be dropped and named -- coercing it with str() instead of "
        "refusing the entry hides a non-string args element from being "
        "treated as a vet-fail shape"
    )
    kept = result.sanitized_store["mcpServers"]
    assert "malformed-args-element-server" not in kept
    assert "safe-server" in kept


def test_sanitize_hooks_keeps_non_dict_hook_entry_unchanged() -> None:
    """A ``hooks.json`` ``hooks`` list entry that is not itself a dict

    (e.g. a bare string) carries no ``command`` shape to vet at all, so
    it must pass through unchanged rather than crash ``sanitize_hooks``
    or be misreported as dropped -- a shape defect in one entry must
    not block every other entry in the same file.
    """
    store = {
        "hooks": [
            "not-a-dict-entry",
            {"id": "h1", "name": "safe-hook", "command": "echo hi"},
        ]
    }

    result = sanitize.sanitize_hooks(store, vet=_fake_vet)

    assert "not-a-dict-entry" in result.sanitized_store["hooks"]
    assert result.dropped_names == []
    kept_names = {
        h["name"] for h in result.sanitized_store["hooks"] if isinstance(h, dict)
    }
    assert "safe-hook" in kept_names


def test_sanitize_hooks_keeps_hook_with_no_command_key_unvetted() -> None:
    """A hook that carries no ``command`` key at all (distinct from a

    ``command`` present but empty/``None``) is never vetted and always
    kept -- exercises ``_hook_shell_command``'s ``"command" not in
    hook`` early return, taken before the ``None``/type checks.
    """
    store = {"hooks": [{"id": "h1", "name": "no-command-hook", "event": "Stop"}]}

    result = sanitize.sanitize_hooks(store, vet=_fake_vet)

    assert result.dropped_names == []
    kept_names = {h["name"] for h in result.sanitized_store["hooks"]}
    assert "no-command-hook" in kept_names


def test_sanitize_hooks_keeps_hook_with_none_command_unvetted() -> None:
    """A hook whose ``command`` is explicitly ``None`` is treated the

    same as "no command at all" -- never vetted, always kept -- exactly
    like a missing key, exercising ``_hook_shell_command``'s
    ``command is None`` branch distinctly from the missing-key branch.
    """
    store = {"hooks": [{"id": "h1", "name": "none-command-hook", "command": None}]}

    result = sanitize.sanitize_hooks(store, vet=_fake_vet)

    assert result.dropped_names == []
    kept_names = {h["name"] for h in result.sanitized_store["hooks"]}
    assert "none-command-hook" in kept_names


def test_sanitize_mcp_servers_treats_non_dict_mcpservers_as_empty() -> None:
    """A ``mcp.json`` document whose top-level ``mcpServers`` value is not

    a dict (e.g. a list) is treated as carrying no servers at all,
    rather than crashing on ``.items()``.
    """
    store = {"mcpServers": ["not-a-dict"]}

    result = sanitize.sanitize_mcp_servers(store, vet=_fake_vet)

    assert result.sanitized_store["mcpServers"] == {}
    assert result.dropped_names == []


def test_sanitize_mcp_servers_keeps_non_dict_server_entry_unchanged() -> None:
    """An ``mcpServers`` entry whose value is not itself a dict (e.g. a

    bare string) carries no ``command``/``args`` shape to vet, so it
    must pass through unchanged rather than crash or be misreported as
    dropped.
    """
    store = {
        "mcpServers": {
            "not-a-dict-server": "oops",
            "safe-server": {"command": "npx", "args": ["-y", "@example/server"]},
        }
    }

    result = sanitize.sanitize_mcp_servers(store, vet=_fake_vet)

    kept = result.sanitized_store["mcpServers"]
    assert kept["not-a-dict-server"] == "oops"
    assert "safe-server" in kept
    assert result.dropped_names == []


def test_sanitize_mcp_servers_keeps_server_with_no_command_key_unvetted() -> None:
    """An mcp server that carries no ``command`` key at all is never

    vetted and always kept -- ``_mcp_server_shell_command``'s
    ``"command" not in server`` early return, distinct from a
    ``command`` present but ``None`` or empty.
    """
    store = {"mcpServers": {"no-command-server": {"args": ["-y"]}}}

    result = sanitize.sanitize_mcp_servers(store, vet=_fake_vet)

    assert result.dropped_names == []
    assert "no-command-server" in result.sanitized_store["mcpServers"]


def test_sanitize_mcp_servers_keeps_server_with_none_command_unvetted() -> None:
    """An mcp server whose ``command`` is explicitly ``None`` is treated

    like "no command at all" -- never vetted, always kept.
    """
    store = {"mcpServers": {"none-command-server": {"command": None}}}

    result = sanitize.sanitize_mcp_servers(store, vet=_fake_vet)

    assert result.dropped_names == []
    assert "none-command-server" in result.sanitized_store["mcpServers"]


def test_sanitize_mcp_servers_keeps_server_with_empty_command_unvetted() -> None:
    """An mcp server whose ``command`` is the empty string is treated as

    carrying no command to vet -- never vetted, always kept. Distinct
    from a non-empty-but-non-string ``command``, which is malformed and
    dropped.
    """
    store = {"mcpServers": {"empty-command-server": {"command": "", "args": []}}}

    result = sanitize.sanitize_mcp_servers(store, vet=_fake_vet)

    assert result.dropped_names == []
    assert "empty-command-server" in result.sanitized_store["mcpServers"]


def test_sanitize_hooks_drops_hook_whose_vet_raises() -> None:
    """A hook whose command is well-formed but whose vet call itself

    raises must be dropped and named -- fail-closed on a vet exception,
    exactly like an explicit vet rejection.
    """

    def _raising_vet(command: str) -> str | None:
        raise RuntimeError("vet exploded")

    store = {"hooks": [{"id": "h1", "name": "raises-hook", "command": "echo hi"}]}

    result = sanitize.sanitize_hooks(store, vet=_raising_vet)

    assert "raises-hook" in result.dropped_names
    kept_names = {h["name"] for h in result.sanitized_store["hooks"]}
    assert "raises-hook" not in kept_names


def test_sanitize_mcp_servers_drops_server_whose_vet_raises() -> None:
    """An mcp server whose joined command is well-formed but whose vet

    call itself raises must be dropped and named -- fail-closed on a
    vet exception, exactly like an explicit vet rejection.
    """

    def _raising_vet(command: str) -> str | None:
        raise RuntimeError("vet exploded")

    store = {"mcpServers": {"raises-server": {"command": "npx", "args": ["-y"]}}}

    result = sanitize.sanitize_mcp_servers(store, vet=_raising_vet)

    assert "raises-server" in result.dropped_names
    assert "raises-server" not in result.sanitized_store["mcpServers"]


# ---------------------------------------------------------------------------
# (2) apply.py: a dropped command must still appear in changed_commands.
# ---------------------------------------------------------------------------


def test_apply_changed_commands_lists_a_dropped_hook_command(
    bundle_repo: Path,
    target_root: Path,
    state_store: state_module.StateStore,
    tmp_path: Path,
) -> None:
    """Requirements.md 6.10: every hook/server command added or changed —

    "whether it survives the vet or is dropped under 6.9" — is listed by
    name in ``changed_commands``. The current implementation builds
    ``changed_commands`` only from the sanitized, kept-only document
    (``_changed_hook_commands(command_final_doc, ...)``), so a dropped
    entry is entirely absent from it — visible only in
    ``dropped_cron_names``, never in the pending-summary command list
    6.10 requires.
    """
    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    commit_root.joinpath("hooks.json").write_text(
        json.dumps(
            {
                "hooks": [
                    {
                        "id": "h1",
                        "name": "unsafe-hook",
                        "event": "Stop",
                        "matcher": "*",
                        "command": "echo $(whoami) > /tmp/pwn",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["hooks.json"], "B": []},
        store=state_store,
        cron_vet=_fake_vet,
    )

    assert "unsafe-hook" in result.dropped_cron_names

    changed_names = {entry["name"] for entry in result.changed_commands}
    assert "unsafe-hook" in changed_names, (
        "requirements.md 6.10: a hook command dropped under 6.9 must "
        "still be listed in changed_commands, not only in "
        "dropped_cron_names -- the operator must see it at approve time"
    )
    entry = next(e for e in result.changed_commands if e["name"] == "unsafe-hook")
    assert entry["file"] == "hooks.json"
    assert entry["command"] == "echo $(whoami) > /tmp/pwn"


def test_apply_changed_commands_lists_a_dropped_mcp_server_command(
    bundle_repo: Path,
    target_root: Path,
    state_store: state_module.StateStore,
    tmp_path: Path,
) -> None:
    """Same 6.10 gap, on the mcp.json side: a server command dropped by

    the vet (command substitution smuggled through ``args``) must still
    be listed in ``changed_commands`` alongside the surviving
    ``safe-server`` entry.
    """
    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    commit_root.joinpath("mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "unsafe-server": {
                        "command": "python3",
                        "args": ["-c", "echo $(whoami)"],
                    },
                    "safe-server": {
                        "command": "npx",
                        "args": ["-y", "@example/server"],
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["mcp.json"], "B": []},
        store=state_store,
        cron_vet=_fake_vet,
    )

    assert "unsafe-server" in result.dropped_cron_names

    changed_names = {entry["name"] for entry in result.changed_commands}
    assert "unsafe-server" in changed_names, (
        "requirements.md 6.10: an mcp server command dropped under 6.9 "
        "must still be listed in changed_commands"
    )
    assert "safe-server" in changed_names

    dropped_entry = next(
        e for e in result.changed_commands if e["name"] == "unsafe-server"
    )
    assert dropped_entry["file"] == "mcp.json"
    assert "$(whoami)" in dropped_entry["command"]


# ---------------------------------------------------------------------------
# (3) registration.py: C3-swept-in agent's model pin / prompt must not
#     still apply live once that agent is reported incomplete.
# ---------------------------------------------------------------------------

AGENT_X = "agent-x-complete"
AGENT_Y = "agent-y-incomplete"
CONFIG_RELPATH = "config.json"
MODEL_STATE_RELPATH = "agent_model_state.json"


def _write_agent_def(root: Path, agent_name: str) -> None:
    path = root / "agents" / f"{agent_name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "name": agent_name,
                "description": "An example agent.",
                "prompt": "inline prompt, no file required",
                "tools": ["read", "write"],
                "allowedTools": ["read"],
                "resources": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _write_agent_def_with_file_prompt(root: Path, agent_name: str) -> None:
    """Same as ``_write_agent_def`` but with a tracked ``file://`` prompt

    reference (a REQUIRED prompt part, per requirements.md 5.11) instead
    of an inline string that needs no file.
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


def _prep_roots_for_c3(tmp_path: Path) -> tuple[Path, Path, Path, dict]:
    """Same ``{"A": Path, "B": Path}`` convention as

    ``test_registration_review1.py``'s own ``_prep_roots`` -- a
    commit-scoped tree plus two live-root stand-ins ``check_registrations``
    resolves ``file://`` prompt references against.
    """
    root = tmp_path / "commit"
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    root.mkdir()
    root_a.mkdir()
    root_b.mkdir()
    return root, root_a, root_b, {"A": root_a, "B": root_b}


def _write_shared_config(root: Path, agents_and_models: dict) -> None:
    path = root / CONFIG_RELPATH
    doc: dict = {"agents": {}}
    for name in agents_and_models:
        doc["agents"][name] = {"source": "local", "model": "claude-sonnet-5"}
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")


def _write_shared_model_state(root: Path, agents_and_models: dict) -> None:
    path = root / MODEL_STATE_RELPATH
    doc: dict = {}
    for name, include in agents_and_models.items():
        if include:
            doc[name] = {"model_managed": False, "model": "claude-sonnet-5"}
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")


def test_c3_swept_agent_prompt_file_not_blocked_from_applying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Req 5.15 / C3 ordering gap: the THIRD pass (prompt-file blocking,

    ``check_registrations``'s own prose: "block a required prompt file
    only when EVERY agent that requires it is incomplete") runs BEFORE
    the FOURTH pass (the C3 cascade that sweeps an otherwise-complete
    agent into ``incomplete_agents`` because a shared file carrying its
    key was blocked by ANOTHER agent's incompleteness). Agent X's own
    registration is complete on every part the SECOND pass checks
    (agent def + config.json entry + agent_model_state.json pin +
    prompt file, all present) -- so at the moment the THIRD pass runs,
    X is not yet in ``incomplete_agents`` and X's required prompt file
    is correctly judged "not blocked" (X, its only requirer, looks
    complete). Only the FOURTH pass, which runs AFTER, discovers that
    agent Y's incompleteness blocks ``agent_model_state.json`` --
    sweeping X into ``incomplete_agents`` retroactively. Nothing then
    revisits the prompt-blocking decision the third pass already made,
    so X's own required prompt file is never added to ``blocked_paths``
    even though X itself is now reported incomplete -- exactly the
    half-registration Requirement 5.15 forbids, on the prompt part
    specifically (distinct from the agent-def part, which the fourth
    pass DOES already add to ``blocked_paths`` directly).
    """
    root, root_a, root_b, roots = _prep_roots_for_c3(tmp_path)

    # Agent X: complete except its prompt is a tracked file:// reference
    # (required part), present in the commit tree.
    _write_agent_def_with_file_prompt(root, AGENT_X)
    (root / "config-bundles" / "agent-prompts").mkdir(parents=True, exist_ok=True)
    (root / "config-bundles" / "agent-prompts" / f"{AGENT_X}.md").write_text(
        f"# {AGENT_X}\n\nprompt body\n", encoding="utf-8"
    )
    _write_agent_def(root, AGENT_Y)

    _write_shared_config(root, {AGENT_X: True, AGENT_Y: True})
    # Y has no key in agent_model_state.json -> Y incomplete -> the
    # shared file's block sweeps X in too (C3), even though X's own
    # agent-def/config/prompt parts are all independently present.
    _write_shared_model_state(root, {AGENT_X: True, AGENT_Y: False})

    changed = [
        CONFIG_RELPATH,
        MODEL_STATE_RELPATH,
        f"agents/{AGENT_X}.json",
        f"agents/{AGENT_Y}.json",
        f"config-bundles/agent-prompts/{AGENT_X}.md",
    ]

    result = registration.check_registrations(root, changed, roots)

    assert AGENT_X in result.incomplete_agents, (
        "precondition: agent X must be swept into incomplete_agents by "
        "the C3 cascade because agent_model_state.json -- which carries "
        "X's own key -- is blocked by Y's incompleteness"
    )
    assert f"config-bundles/agent-prompts/{AGENT_X}.md" in result.blocked_paths, (
        "Req 5.15: agent X's required prompt file must be blocked once "
        "X is reported incomplete by the C3 cascade -- the third pass "
        "(prompt blocking) ran before the fourth pass (C3) discovered "
        "X's incompleteness, so X's prompt file was already judged "
        "'not blocked' and nothing revisits that decision afterward, "
        "leaving X's prompt free to apply live while X itself is named "
        "incomplete"
    )
