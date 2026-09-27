"""Deployment 4 live install (Phase 8) failure: both manifest crons rejected.

`kirocrew app enable config-sync` registered no crons:

    cron 'config-sync/config-sync-push' command rejected: Error: cron command
    blocked: only a plain `${NAME}` reference is permitted. Brace expansions
    that COMPOSE a value at run time (`${X:-default}`, ...) assemble strings a
    static check cannot see.

The host vets every app-manifest cron `command` with the same static rules as
`cron_add` (`kiro_crew/mcp_cron.py::_vet_shell_command`): no command
substitution, no brace expansion other than a plain `${NAME}`, only `$HOME`
as a variable reference, no positional parameters, no shell loops, no glob
metacharacters. `${KIROCREW_HOME:-$HOME/.kiro/crew}` fails the second rule.

These tests mirror those rules as text checks over `app.json` so the seam
(manifest -> host vet) is covered without importing the host, plus a parity
test that reads the installed `mcp_cron.py` as text and asserts the two
rules this file relies on are still spelled the way it assumes.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterator

import pytest

APP_ROOT = Path(__file__).resolve().parents[1]
HOST_MCP_CRON = Path("/usr/local/lib/python3.12/site-packages/kiro_crew/mcp_cron.py")

# Mirrors of the host's rules (kiro_crew/mcp_cron.py, verified by the parity
# test below).
_BRACE_EXPANSION_RE = re.compile(r"\$\{(?![A-Za-z_][A-Za-z0-9_]*\})")
_VAR_REF_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
_VAR_REF_ALLOWED = frozenset({"HOME"})
_CMD_SUBST_RE = re.compile(r"\$\(|\$'|`")
_POSITIONAL_RE = re.compile(r"\$[0-9@*#]|\$\{[0-9@*#]")
_SHELL_KEYWORD_RE = re.compile(
    r"(?:^|[;&|]|\bdo\b|\bthen\b)\s*\b(?:for|while|until|case)\b"
)
_GLOB_META_RE = re.compile(r"\[[^]]*\]|[?*]")


def _cron_commands() -> Iterator[tuple[str, str]]:
    manifest = json.loads((APP_ROOT / "app.json").read_text(encoding="utf-8"))
    crons = manifest.get("crons", [])
    assert crons, "app.json declares no crons"
    for cron in crons:
        command = cron.get("command")
        if command:
            yield cron["name"], command


def _commands_for_parametrize() -> list[tuple[str, str]]:
    return list(_cron_commands())


@pytest.mark.parametrize(("name", "command"), _commands_for_parametrize())
def test_cron_command_uses_no_composing_brace_expansion(
    name: str, command: str
) -> None:
    """`${X:-default}` and friends are refused by the host vet."""
    assert not _BRACE_EXPANSION_RE.search(command), (
        f"cron {name!r}: command contains a composing brace expansion the "
        f"host cron vet refuses: {command!r}"
    )


@pytest.mark.parametrize(("name", "command"), _commands_for_parametrize())
def test_cron_command_references_only_allowed_variables(
    name: str, command: str
) -> None:
    """Only `$HOME` / `${HOME}` may be referenced (the sandboxed cron env is
    the gateway's own environment; `KIROCREW_HOME` is an optional override
    that is not guaranteed to exist, and an unset reference expands empty)."""
    refs = set(_VAR_REF_RE.findall(command))
    assert refs <= _VAR_REF_ALLOWED, (
        f"cron {name!r}: command references variables the host vet refuses: "
        f"{sorted(refs - _VAR_REF_ALLOWED)}"
    )


@pytest.mark.parametrize(("name", "command"), _commands_for_parametrize())
def test_cron_command_has_no_other_refused_constructs(name: str, command: str) -> None:
    assert not _CMD_SUBST_RE.search(command), f"{name}: command substitution"
    assert not _POSITIONAL_RE.search(command), f"{name}: positional parameter"
    assert not _SHELL_KEYWORD_RE.search(command), f"{name}: shell loop/compound"
    assert not _GLOB_META_RE.search(command), f"{name}: glob metacharacter"


@pytest.mark.parametrize(("name", "command"), _commands_for_parametrize())
def test_cron_command_runs_the_backend_module_from_the_installed_app_dir(
    name: str, command: str
) -> None:
    """The installed copy lives at `<config dir>/apps/config-sync`; the module
    entrypoint needs that directory as cwd. With no fallback expression
    available, the default config dir under $HOME is the only expressible
    location."""
    assert 'cd "$HOME/.kiro/crew/apps/config-sync"' in command, command
    assert re.search(r"python3 -m backend\.(push|poll)$", command), command


def test_mirrored_rules_match_the_installed_host_vet() -> None:
    """Parity with the host: the two rules this file depends on most."""
    if not HOST_MCP_CRON.is_file():
        pytest.skip("KiroCrew host not installed here; vet parity not checked")
    text = HOST_MCP_CRON.read_text(encoding="utf-8")
    assert (
        '_CRON_BRACE_EXPANSION_RE = re.compile(r"\\$\\{(?![A-Za-z_][A-Za-z0-9_]*\\})")'
        in text
    )
    assert '_CRON_VAR_REF_ALLOWED = frozenset({"HOME"})' in text
