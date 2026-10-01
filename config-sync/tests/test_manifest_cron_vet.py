"""Manifest crons and the host's cron sandbox.

Deployment 4's live install (Phase 8) found both manifest crons rejected by
the host's static command vet (`${KIROCREW_HOME:-...}` composes a value at
run time). Deployment 5's live install then found the deeper constraint:
KiroCrew runs cron subprocesses inside its sandbox, which hides
``~/.git-credentials`` by design, and the bundle repo is private — so NO
command cron of this app can reach it, however well-formed its command.
The poll moved to a vault-granted SCRIPT cron (``host-crons/`` and
``skills/install-poll-cron``); the push stays on the dashboard button, whose
backend process runs outside the cron sandbox.

So ``app.json`` now declares **no crons**, and this file pins that. The
mirrored vet rules are kept as a guard: if a command cron is ever re-added,
it must still satisfy the host's static vet (`kiro_crew/mcp_cron.py::
_vet_shell_command`): no command substitution, no brace expansion other than
a plain `${NAME}`, only `$HOME` as a variable reference, no positional
parameters, no shell loops, no glob metacharacters.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

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


def _manifest() -> dict:
    return json.loads((APP_ROOT / "app.json").read_text(encoding="utf-8"))


def test_manifest_declares_no_crons_because_the_sandbox_hides_git_credentials() -> None:
    """Both jobs need the private bundle repo; a manifest command cron runs in
    the host sandbox with no credential and fails every tick until the host
    auto-pauses it (live install 2026-10-01, `git ls-remote` exit 128)."""
    crons = _manifest().get("crons", [])
    assert crons == [], (
        "app.json must not declare command crons that need the private bundle "
        f"repo; the poll is a vault-granted script cron. Found: {crons!r}"
    )


def vet_command(name: str, command: str) -> list[str]:
    """Return the host-vet violations for one cron command (empty = clean)."""
    problems: list[str] = []
    if _BRACE_EXPANSION_RE.search(command):
        problems.append("composing brace expansion")
    refs = set(_VAR_REF_RE.findall(command))
    if not refs <= _VAR_REF_ALLOWED:
        problems.append(f"variables refused: {sorted(refs - _VAR_REF_ALLOWED)}")
    if _CMD_SUBST_RE.search(command):
        problems.append("command substitution")
    if _POSITIONAL_RE.search(command):
        problems.append("positional parameter")
    if _SHELL_KEYWORD_RE.search(command):
        problems.append("shell loop/compound")
    if _GLOB_META_RE.search(command):
        problems.append("glob metacharacter")
    return [f"{name}: {p}" for p in problems]


def test_any_future_command_cron_must_pass_the_host_vet() -> None:
    """Guard, not a tautology: runs over whatever the manifest declares, so a
    re-added command cron is vetted here before the live install does it."""
    problems: list[str] = []
    for cron in _manifest().get("crons", []):
        command = cron.get("command")
        if command:
            problems.extend(vet_command(cron["name"], command))
    assert problems == [], problems


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ('cd "$HOME/.kiro/crew/apps/config-sync" && python3 -m backend.push', []),
        ('cd "${KIROCREW_HOME:-$HOME/.kiro/crew}/apps/x" && python3 -m b', ["brace"]),
        ("cd $(pwd) && python3 -m b", ["command substitution"]),
        ("cd $KIROCREW_HOME && python3 -m b", ["variables refused"]),
        ("for f in *; do echo $f; done", ["shell loop", "glob"]),
    ],
)
def test_vet_mirror_bites_on_the_constructs_the_host_refuses(
    command: str, expected: list[str]
) -> None:
    """The mirror itself is proven to fail on each refused construct (the
    Deployment-4 `${X:-default}` case included), so the guard above is
    load-bearing rather than vacuous when the manifest has no crons."""
    problems = vet_command("probe", command)
    assert bool(problems) == bool(expected), problems
    for needle in expected:
        assert any(needle in p for p in problems), (needle, problems)


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
