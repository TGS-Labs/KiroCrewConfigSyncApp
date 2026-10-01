"""Tests for backend/collect.py.

Covers design.md's "backend/collect.py" component and tasks.md 1.2:

- The collector walks BOTH configuration roots (root A `KIROCREW_HOME`, root
  B `KIRO_HOME`) and returns a mapping of `{relpath: bytes}` containing only
  allowlist hits, using `backend.allowlist.is_tracked` (or an equivalent call
  into the Wave 0 allowlist module) as the sole admission test
  (requirements.md 1.3, 1.5).
- An allowlisted path that does not exist on the host (e.g. `crons.json` on
  an instance with no scheduled jobs) is simply absent from the result: no
  error is raised, and the collector does not create the file as a side
  effect (requirements.md 1.5).

These tests define the acceptance criteria for the not-yet-written
`backend/collect.py`. They are expected to fail with an ImportError /
ModuleNotFoundError until that module exists — this is the correct TDD
starting state, not a test defect.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend import collect


# ---------------------------------------------------------------------------
# Fixture: isolated root A / root B trees, matching test_allowlist.py's and
# test_state.py's KIROCREW_HOME / KIRO_HOME convention.
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Point KIROCREW_HOME (root A) and KIRO_HOME (root B) at fresh, empty

    temp directories so collector tests never touch the real host
    configuration.
    """
    root_a = tmp_path / "kirocrew_home"
    root_b = tmp_path / "kiro_home"
    root_a.mkdir()
    root_b.mkdir()

    monkeypatch.setenv("KIROCREW_HOME", str(root_a))
    monkeypatch.setenv("KIRO_HOME", str(root_b))

    return {"root_a": root_a, "root_b": root_b, "tmp_path": tmp_path}


def _write(root: Path, relpath: str, content: bytes = b"content") -> Path:
    """Write a file at relpath under root, creating parent dirs as needed."""
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


# ---------------------------------------------------------------------------
# Core contract: walks both roots, returns {relpath: bytes}, allowlist hits
# only.
# ---------------------------------------------------------------------------


def test_collects_allowlisted_files_from_root_a(
    isolated_roots: dict[str, Path],
) -> None:
    """A tracked root-A file (e.g. config.json) is present in the result

    with its exact byte content.
    """
    root_a = isolated_roots["root_a"]
    _write(root_a, "config.json", b'{"key": "value"}')

    result = collect.collect()

    assert "config.json" in result
    assert result["config.json"] == b'{"key": "value"}'


def test_collects_allowlisted_files_from_root_b(
    isolated_roots: dict[str, Path],
) -> None:
    """A tracked root-B file (agents/*.json) is present in the result."""
    root_b = isolated_roots["root_b"]
    _write(root_b, "agents/my-agent.json", b'{"name": "my-agent"}')

    result = collect.collect()

    assert "agents/my-agent.json" in result
    assert result["agents/my-agent.json"] == b'{"name": "my-agent"}'


def test_collects_from_both_roots_in_one_call(
    isolated_roots: dict[str, Path],
) -> None:
    """A single collect() call walks BOTH roots and merges their allowlist

    hits into one mapping (requirements.md: the collector spans root A and
    root B).
    """
    root_a = isolated_roots["root_a"]
    root_b = isolated_roots["root_b"]
    _write(root_a, "config.json", b"a-content")
    _write(root_a, "steering/plan.md", b"# Plan")
    _write(root_b, "agents/senior-reviewer.json", b"b-content")

    result = collect.collect()

    assert result["config.json"] == b"a-content"
    assert result["steering/plan.md"] == b"# Plan"
    assert result["agents/senior-reviewer.json"] == b"b-content"


def test_nested_steering_markdown_is_collected_with_correct_relpath(
    isolated_roots: dict[str, Path],
) -> None:
    """A nested allowlisted path (steering/**/*.md) is keyed by its path

    relative to its OWN root, using '/' as the separator, matching
    allowlist.py's path convention.
    """
    root_a = isolated_roots["root_a"]
    _write(root_a, "steering/nested/dir/rule.md", b"# Rule")

    result = collect.collect()

    assert "steering/nested/dir/rule.md" in result
    assert result["steering/nested/dir/rule.md"] == b"# Rule"


def test_non_allowlisted_files_are_excluded_from_both_roots(
    isolated_roots: dict[str, Path],
) -> None:
    """A file that exists on disk but matches no allowlist entry is NOT in

    the result, on either root — the collector must consult the allowlist,
    not merely enumerate every file present (requirements.md 1.3).
    """
    root_a = isolated_roots["root_a"]
    root_b = isolated_roots["root_b"]
    _write(root_a, ".env", b"SECRET=abc123")
    _write(root_a, "memory.db", b"binary-db-content")
    _write(root_a, "sessions/abc123.jsonl", b'{"turn": 1}')
    _write(root_a, "gateway.log", b"log line")
    _write(root_a, "steering/plan.md.bak", b"backup, not tracked")
    _write(root_b, "config.json", b"root B never tracks config.json")
    _write(root_b, "agents/nested/deep.json", b"not directly under agents/")

    result = collect.collect()

    never_present = [
        ".env",
        "memory.db",
        "sessions/abc123.jsonl",
        "gateway.log",
        "steering/plan.md.bak",
        "config.json",
        "agents/nested/deep.json",
    ]
    for relpath in never_present:
        assert (
            relpath not in result
        ), f"{relpath!r} matched no allowlist entry but was collected anyway"


def test_result_contains_only_allowlist_hits_end_to_end(
    isolated_roots: dict[str, Path],
) -> None:
    """Every key in the result actually satisfies allowlist.is_tracked() for

    its own root — the collector's admission rule is exactly the allowlist,
    not a superset or a hand-rolled approximation of it. This exercises the
    Wave 0 allowlist module's own API directly, per the task's requirement
    to reuse allowlist.is_tracked (or an equivalent).
    """
    from backend import allowlist

    root_a = isolated_roots["root_a"]
    root_b = isolated_roots["root_b"]
    _write(root_a, "config.json")
    _write(root_a, "steering/plan.md")
    _write(root_a, "random-untracked-file.bin")
    _write(root_b, "agents/foo.json")
    _write(root_b, "not-json.txt")

    result = collect.collect()

    for relpath in result:
        assert allowlist.is_tracked("A", relpath) or allowlist.is_tracked(
            "B", relpath
        ), (
            f"collected {relpath!r} does not satisfy is_tracked() on either "
            "root — the collector admitted something the allowlist would not"
        )


def test_empty_roots_produce_an_empty_result(
    isolated_roots: dict[str, Path],
) -> None:
    """With nothing on disk under either root, the collector returns an

    empty mapping rather than erroring or fabricating entries.
    """
    result = collect.collect()

    assert result == {}


# ---------------------------------------------------------------------------
# Absence-without-error: requirements.md 1.5 / tasks.md 1.2.
# ---------------------------------------------------------------------------


def test_absent_allowlisted_path_is_simply_missing_from_result(
    isolated_roots: dict[str, Path],
) -> None:
    """crons.json is allowlisted but does not exist on an instance with no

    scheduled jobs configured: the collector must not raise, and the
    resulting mapping must simply have no 'crons.json' key.
    """
    # Deliberately do NOT create crons.json anywhere under root A.
    result = collect.collect()

    assert "crons.json" not in result


def test_absent_allowlisted_path_causes_no_side_effect_of_creation(
    isolated_roots: dict[str, Path],
) -> None:
    """Calling collect() when an allowlisted file is absent must not create

    that file on disk as a side effect (requirements.md 1.5: 'SHALL NOT
    create it').
    """
    root_a = isolated_roots["root_a"]
    crons_path = root_a / "crons.json"
    assert not crons_path.exists()

    collect.collect()

    assert not crons_path.exists(), (
        "collect() must never create an absent allowlisted file as a side " "effect"
    )


def test_multiple_absent_allowlisted_paths_all_omitted_without_error(
    isolated_roots: dict[str, Path],
) -> None:
    """Several allowlisted-but-absent files across both roots are all

    simply missing from the result in a single call, with no error raised
    for any of them.
    """
    # Nothing written under either root at all.
    result = collect.collect()

    for relpath in ("crons.json", "instances.json", "hooks.json", "mcp.json"):
        assert relpath not in result


def test_mixed_present_and_absent_allowlisted_paths(
    isolated_roots: dict[str, Path],
) -> None:
    """When some allowlisted files exist and others don't, the present ones

    are collected and the absent ones are simply omitted — no error, no
    partial-failure signal, just a smaller mapping.
    """
    root_a = isolated_roots["root_a"]
    _write(root_a, "config.json", b"present")
    # crons.json, instances.json, hooks.json, mcp.json intentionally absent.

    result = collect.collect()

    assert result["config.json"] == b"present"
    for relpath in ("crons.json", "instances.json", "hooks.json", "mcp.json"):
        assert relpath not in result


# ---------------------------------------------------------------------------
# Byte-fidelity: the collector must not transform content (redaction is a
# separate, later stage per design.md's pipeline).
# ---------------------------------------------------------------------------


def test_collected_bytes_are_unmodified_raw_file_content(
    isolated_roots: dict[str, Path],
) -> None:
    """The collector returns the file's raw bytes verbatim — no redaction,

    no re-serialization, no encoding transform. That is redact.py's job
    downstream, not collect.py's.
    """
    root_a = isolated_roots["root_a"]
    raw = b'{"mcpServers": {"foo": {"headers": {"Authorization": "secret"}}}}'
    _write(root_a, "mcp.json", raw)

    result = collect.collect()

    assert result["mcp.json"] == raw


def test_collected_result_type_is_a_plain_bytes_mapping(
    isolated_roots: dict[str, Path],
) -> None:
    """Sanity check on the return shape: {relpath: bytes} — keys are str,

    values are bytes, matching redact.py's documented input contract
    ("a mapping of relative path -> bytes").
    """
    root_a = isolated_roots["root_a"]
    _write(root_a, "config.json", b"x")

    result = collect.collect()

    assert isinstance(result, dict)
    for key, value in result.items():
        assert isinstance(key, str)
        assert isinstance(value, bytes)
