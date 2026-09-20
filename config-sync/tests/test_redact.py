"""Tests for backend/redact.py — structure-preserving credential redaction.

Covers design.md's `redact.py` component and requirements 3.1-3.5:
  - Redaction is applied to in-memory content (mapping of relpath -> bytes).
  - Every `headers` and `env` object's *values* are replaced with the literal
    placeholder "<redacted>"; keys, key order, and document structure are
    preserved.
  - A structural change (server added/removed in an mcp.json-shaped document)
    changes the redacted output.
  - A value-only change (e.g. a token/credential rotated, same keys) leaves
    the redacted output byte-identical.
  - Re-serialization is deterministic: the same input redacted twice produces
    byte-identical output.

Hypothesis uses the project's two-tier profile convention from
python-standards.md (dev: 10-15 examples, loaded via HYPOTHESIS_PROFILE from a
conftest.py once one exists). A local, file-scoped profile registration is
used here since no repo-wide conftest.py exists yet; it does not conflict with
a future conftest.py registering the same "dev"/"ci" profile names.
"""

import json
import os
from typing import Any

from hypothesis import given, settings, strategies as st

# Feature: config-sync, two-tier Hypothesis profile per python-standards.md.
# Registered locally because no repo-wide conftest.py exists yet (Task 1.3 is
# part of the Wave 0 foundation work). Safe to re-register the same profile
# names later from a conftest.py; settings.register_profile is idempotent per
# name and load_profile only switches which one is active.
settings.register_profile("dev", max_examples=15, deadline=None)
settings.register_profile("ci", max_examples=100, deadline=None)
settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "dev"))

# Import target — intentionally does not exist yet (TDD red phase, Task 1.3).
from backend.redact import redact  # noqa: E402  (import after settings setup)


# --------------------------------------------------------------------------
# Strategies
# --------------------------------------------------------------------------

# Header/env key names: identifier-like tokens, matching real mcp.json shapes
# (e.g. "Authorization", "API_KEY", "X-Custom-Header").
_KEY_STRATEGY = st.from_regex(r"[A-Za-z][A-Za-z0-9_-]{0,20}", fullmatch=True)

# Secret-shaped values: non-empty printable strings standing in for tokens,
# PATs, and credential values.
_VALUE_STRATEGY = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126),
    min_size=1,
    max_size=40,
)

# Server names inside an mcp.json-shaped "mcpServers" map.
_SERVER_NAME_STRATEGY = st.from_regex(r"[a-z][a-z0-9_-]{0,20}", fullmatch=True)


@st.composite
def _headers_map(draw: Any, min_size: int = 1, max_size: int = 5) -> dict[str, str]:
    """A small non-empty dict of header-name -> secret-shaped value."""
    keys = draw(
        st.lists(_KEY_STRATEGY, min_size=min_size, max_size=max_size, unique=True)
    )
    return {k: draw(_VALUE_STRATEGY) for k in keys}


@st.composite
def _mcp_server_entry(draw: Any) -> dict[str, Any]:
    """One mcpServers entry carrying a headers block and/or an env block."""
    entry = {
        "command": draw(st.sampled_from(["npx", "python3", "node", "uvx"])),
        "args": draw(st.lists(st.text(min_size=1, max_size=10), max_size=3)),
    }
    if draw(st.booleans()):
        entry["headers"] = draw(_headers_map())
    if draw(st.booleans()):
        entry["env"] = draw(_headers_map())
    return entry


@st.composite
def _mcp_document(
    draw: Any, min_servers: int = 1, max_servers: int = 4
) -> dict[str, Any]:
    """An mcp.json-shaped document: {"mcpServers": {name: entry, ...}}."""
    names = draw(
        st.lists(
            _SERVER_NAME_STRATEGY,
            min_size=min_servers,
            max_size=max_servers,
            unique=True,
        )
    )
    servers = {name: draw(_mcp_server_entry()) for name in names}
    return {"mcpServers": servers}


def _as_bytes(doc: dict) -> bytes:
    return json.dumps(doc).encode("utf-8")


def _rotate_values(doc: dict, new_value: str) -> dict:
    """Return a deep-ish copy of an mcp.json-shaped doc with every headers/env
    value replaced by `new_value`, keeping every key and server unchanged."""
    rotated: dict[str, Any] = {"mcpServers": {}}
    for name, entry in doc["mcpServers"].items():
        new_entry = dict(entry)
        for block_name in ("headers", "env"):
            if block_name in entry:
                new_entry[block_name] = {k: new_value for k in entry[block_name]}
        rotated["mcpServers"][name] = new_entry
    return rotated


# --------------------------------------------------------------------------
# Property 1: a structural change (server added/removed) changes the output
# --------------------------------------------------------------------------


@given(doc=_mcp_document(min_servers=1, max_servers=4), new_name=_SERVER_NAME_STRATEGY)
def test_adding_a_server_changes_redacted_output(
    doc: dict[str, Any], new_name: str
) -> None:
    """Property: adding a new mcpServers entry changes the redacted output.

    Requirements 3.2, 3.3 — structural drift in mcp.json (a server added)
    must remain visible in the redacted copy so it stays reviewable in a PR
    diff, even though every credential value is masked.
    """
    if new_name in doc["mcpServers"]:
        new_name = new_name + "x"

    before = {"relpath/mcp.json": _as_bytes(doc)}
    added_doc = dict(doc)
    added_doc["mcpServers"] = dict(doc["mcpServers"])
    added_doc["mcpServers"][new_name] = {
        "command": "npx",
        "args": [],
        "headers": {"Authorization": "some-token"},
    }
    after = {"relpath/mcp.json": _as_bytes(added_doc)}

    redacted_before = redact(before)
    redacted_after = redact(after)

    assert redacted_before["relpath/mcp.json"] != redacted_after["relpath/mcp.json"]


@given(doc=_mcp_document(min_servers=2, max_servers=4))
def test_removing_a_server_changes_redacted_output(doc: dict[str, Any]) -> None:
    """Property: removing an mcpServers entry changes the redacted output.

    Requirements 3.2, 3.3 — the inverse of the addition case: a server
    removed from mcp.json must also be visible as a diff after redaction.
    """
    before = {"relpath/mcp.json": _as_bytes(doc)}

    removed_name = next(iter(doc["mcpServers"]))
    shrunk_doc = dict(doc)
    shrunk_doc["mcpServers"] = {
        name: entry for name, entry in doc["mcpServers"].items() if name != removed_name
    }
    after = {"relpath/mcp.json": _as_bytes(shrunk_doc)}

    redacted_before = redact(before)
    redacted_after = redact(after)

    assert redacted_before["relpath/mcp.json"] != redacted_after["relpath/mcp.json"]


# --------------------------------------------------------------------------
# Property 2: a value-only change (token rotation) is byte-identical after
# redaction
# --------------------------------------------------------------------------


@given(doc=_mcp_document(min_servers=1, max_servers=4), rotated_value=_VALUE_STRATEGY)
def test_rotating_header_and_env_values_is_byte_identical_after_redaction(
    doc: dict[str, Any], rotated_value: str
) -> None:
    """Property: rotating header/env values (same keys, same servers) leaves
    the redacted output byte-identical to the original redacted output.

    Requirement 3.4 — a token rotation alone must not produce a diff, since
    every headers/env value is already collapsed to the same placeholder.
    """
    before = {"relpath/mcp.json": _as_bytes(doc)}
    rotated_doc = _rotate_values(doc, rotated_value)
    after = {"relpath/mcp.json": _as_bytes(rotated_doc)}

    redacted_before = redact(before)
    redacted_after = redact(after)

    assert redacted_before["relpath/mcp.json"] == redacted_after["relpath/mcp.json"]


# --------------------------------------------------------------------------
# Non-property tests
# --------------------------------------------------------------------------


def test_every_headers_and_env_value_replaced_with_redacted_literal() -> None:
    """Every value inside a `headers` or `env` object becomes "<redacted>",
    while server names and header/env keys are preserved verbatim."""
    doc = {
        "mcpServers": {
            "github": {
                "command": "npx",
                "args": ["-y", "server-github"],
                "headers": {
                    "Authorization": "Bearer ghp_realtoken123",
                    "X-Org": "TGS-Labs",
                },
                "env": {"GITHUB_TOKEN": "ghp_realtoken123", "DEBUG": "1"},
            }
        }
    }
    collected = {"mcp.json": json.dumps(doc).encode("utf-8")}

    result = redact(collected)
    parsed = json.loads(result["mcp.json"])

    server = parsed["mcpServers"]["github"]
    assert server["headers"]["Authorization"] == "<redacted>"
    assert server["headers"]["X-Org"] == "<redacted>"
    assert server["env"]["GITHUB_TOKEN"] == "<redacted>"
    assert server["env"]["DEBUG"] == "<redacted>"
    # Keys preserved verbatim.
    assert set(server["headers"].keys()) == {"Authorization", "X-Org"}
    assert set(server["env"].keys()) == {"GITHUB_TOKEN", "DEBUG"}
    # Non-secret structure untouched.
    assert server["command"] == "npx"
    assert server["args"] == ["-y", "server-github"]


def test_key_order_and_json_structure_preserved() -> None:
    """Key order within `headers`/`env` and the surrounding document
    structure (server names, nesting, non-secret keys) survive redaction."""
    doc = {
        "mcpServers": {
            "zeta-server": {
                "command": "python3",
                "headers": {
                    "First-Header": "value-1",
                    "Second-Header": "value-2",
                    "Third-Header": "value-3",
                },
            },
            "alpha-server": {
                "command": "node",
                "env": {"Z_VAR": "z", "A_VAR": "a", "M_VAR": "m"},
            },
        }
    }
    collected = {"mcp.json": json.dumps(doc).encode("utf-8")}

    result = redact(collected)
    parsed = json.loads(result["mcp.json"])

    # Server order preserved.
    assert list(parsed["mcpServers"].keys()) == ["zeta-server", "alpha-server"]
    # Header key order preserved (not alphabetized).
    assert list(parsed["mcpServers"]["zeta-server"]["headers"].keys()) == [
        "First-Header",
        "Second-Header",
        "Third-Header",
    ]
    # Env key order preserved.
    assert list(parsed["mcpServers"]["alpha-server"]["env"].keys()) == [
        "Z_VAR",
        "A_VAR",
        "M_VAR",
    ]


def test_relpath_keys_and_non_json_content_are_not_dropped() -> None:
    """The output mapping still carries every input relpath; a file with no
    headers/env block passes through with its content unchanged in shape."""
    doc_no_secrets = {"mcpServers": {"plain": {"command": "npx", "args": []}}}
    collected = {
        "mcp.json": json.dumps(doc_no_secrets).encode("utf-8"),
        "config.json": json.dumps({"schemaVersion": 1}).encode("utf-8"),
    }

    result = redact(collected)

    assert set(result.keys()) == {"mcp.json", "config.json"}
    assert json.loads(result["config.json"]) == {"schemaVersion": 1}
    assert json.loads(result["mcp.json"]) == doc_no_secrets


def test_deterministic_reserialization_same_input_twice_is_byte_identical() -> None:
    """Redacting the exact same input mapping twice produces byte-identical
    output both times, so an unchanged file never produces a spurious diff."""
    doc = {
        "mcpServers": {
            "svc-a": {
                "command": "npx",
                "headers": {"Authorization": "Bearer abc123"},
                "env": {"TOKEN": "abc123"},
            },
            "svc-b": {"command": "node", "env": {"KEY": "xyz789"}},
        }
    }
    collected = {"mcp.json": json.dumps(doc).encode("utf-8")}

    first = redact(dict(collected))
    second = redact(dict(collected))

    assert first == second
    assert first["mcp.json"] == second["mcp.json"]


def test_env_and_headers_both_redacted_in_same_document() -> None:
    """A document with both a `headers` block and an `env` block on the same
    server has every value in both blocks replaced, independently."""
    doc = {
        "mcpServers": {
            "combo": {
                "command": "npx",
                "headers": {"Authorization": "Bearer secret-header-value"},
                "env": {"API_KEY": "secret-env-value"},
            }
        }
    }
    collected = {"mcp.json": json.dumps(doc).encode("utf-8")}

    result = redact(collected)
    parsed = json.loads(result["mcp.json"])
    server = parsed["mcpServers"]["combo"]

    assert server["headers"]["Authorization"] == "<redacted>"
    assert server["env"]["API_KEY"] == "<redacted>"


def test_non_json_content_passes_through_unchanged() -> None:
    """A file whose content is not valid JSON (e.g. a shell script or plain
    text) passes through byte-for-byte unchanged -- redact() only ever
    touches parsed JSON documents; it never guesses at other formats."""
    plain_text = b"#!/bin/bash\necho hello\n"
    not_utf8 = b"\xff\xfe\x00\x01invalid-utf8"
    collected = {
        "scripts/run.sh": plain_text,
        "binary.bin": not_utf8,
        "mcp.json": json.dumps(
            {"mcpServers": {"svc": {"command": "npx", "env": {"K": "v"}}}}
        ).encode("utf-8"),
    }

    result = redact(collected)

    assert result["scripts/run.sh"] == plain_text
    assert result["binary.bin"] == not_utf8
    # The one genuinely JSON file is still redacted normally.
    parsed = json.loads(result["mcp.json"])
    assert parsed["mcpServers"]["svc"]["env"]["K"] == "<redacted>"


def test_headers_or_env_value_that_is_not_a_mapping_passes_through_unchanged() -> None:
    """A `headers`/`env` key whose value is not itself a mapping (e.g. a
    string, number, list, or null placed there by a malformed or unusual
    mcp.json) is returned unchanged rather than guessed at -- only an actual
    mapping's values are replaced with the placeholder."""
    doc = {
        "mcpServers": {
            "weird": {
                "command": "npx",
                "headers": "not-a-mapping",
                "env": None,
            }
        }
    }
    collected = {"mcp.json": json.dumps(doc).encode("utf-8")}

    result = redact(collected)
    parsed = json.loads(result["mcp.json"])
    server = parsed["mcpServers"]["weird"]

    assert server["headers"] == "not-a-mapping"
    assert server["env"] is None
    assert "secret-header-value" not in result["mcp.json"].decode("utf-8")
    assert "secret-env-value" not in result["mcp.json"].decode("utf-8")
