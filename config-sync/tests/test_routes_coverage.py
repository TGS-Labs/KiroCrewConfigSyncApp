"""Branch-coverage tests for routes.py additions/gaps (M5 + pre-existing).

Targets the exact lines flagged by ``--cov=backend.routes --cov-branch
--cov-report=term-missing`` after the H4/M5 change: the M5
``_load_json_relpath`` / ``_hook_commands`` / ``_mcp_server_commands`` /
``_changed_commands_for_file`` / ``_pending_changed_commands`` helpers
(malformed/absent-file defaults, non-dict/non-str guards, the no-pending
and no-sha early returns), plus pre-existing gaps in ``is_app_enabled``'s
``ImportError`` branch, ``_relpath_to_root``/``_root_map_for``,
``_is_safe_apply_id``, ``_restore_manifest_relpaths``, and ``restore``'s
missing-backup-file branch. Each behaviour was confirmed to actually
exercise the target line (broken locally, observed red, restored) before
being kept here.
"""

from __future__ import annotations

import builtins
import json
from pathlib import Path
from typing import Any

import pytest

from backend import materialize, routes, state


# ---------------------------------------------------------------------------
# _load_json_relpath: absent file, malformed JSON, non-dict JSON.
# ---------------------------------------------------------------------------


def test_load_json_relpath_missing_file_returns_empty_dict(tmp_path: Path) -> None:
    assert routes._load_json_relpath(tmp_path, "nope.json") == {}


def test_load_json_relpath_malformed_json_returns_empty_dict(tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text("{not valid", encoding="utf-8")
    assert routes._load_json_relpath(tmp_path, "bad.json") == {}


def test_load_json_relpath_non_dict_json_returns_empty_dict(tmp_path: Path) -> None:
    (tmp_path / "list.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert routes._load_json_relpath(tmp_path, "list.json") == {}


def test_load_json_relpath_valid_dict_is_returned(tmp_path: Path) -> None:
    (tmp_path / "ok.json").write_text('{"a": 1}', encoding="utf-8")
    assert routes._load_json_relpath(tmp_path, "ok.json") == {"a": 1}


# ---------------------------------------------------------------------------
# _hook_commands: skip non-dict entries, entries missing name/command.
# ---------------------------------------------------------------------------


def test_hook_commands_skips_non_dict_entry() -> None:
    document = {"hooks": ["not-a-dict", {"name": "h", "command": "echo hi"}]}
    assert routes._hook_commands(document) == {"h": "echo hi"}


def test_hook_commands_skips_entry_with_no_name() -> None:
    document = {"hooks": [{"command": "echo hi"}]}
    assert routes._hook_commands(document) == {}


def test_hook_commands_falls_back_to_id_when_name_absent() -> None:
    document = {"hooks": [{"id": "h1", "command": "echo hi"}]}
    assert routes._hook_commands(document) == {"h1": "echo hi"}


def test_hook_commands_skips_entry_with_no_command() -> None:
    document = {"hooks": [{"name": "h1"}]}
    assert routes._hook_commands(document) == {}


# ---------------------------------------------------------------------------
# _mcp_server_commands: non-dict servers map, non-str name, non-dict
# server value, missing/non-str command, non-list args.
# ---------------------------------------------------------------------------


def test_mcp_server_commands_non_dict_servers_map_returns_empty() -> None:
    assert routes._mcp_server_commands({"mcpServers": "not-a-dict"}) == {}


def test_mcp_server_commands_skips_non_dict_server_value() -> None:
    document = {"mcpServers": {"bad": "not-a-dict"}}
    assert routes._mcp_server_commands(document) == {}


def test_mcp_server_commands_skips_entry_missing_command() -> None:
    document = {"mcpServers": {"srv": {"args": ["-y"]}}}
    assert routes._mcp_server_commands(document) == {}


def test_mcp_server_commands_non_list_args_treated_as_empty() -> None:
    document = {"mcpServers": {"srv": {"command": "npx", "args": "not-a-list"}}}
    assert routes._mcp_server_commands(document) == {"srv": "npx"}


def test_mcp_server_commands_joins_command_and_args() -> None:
    document = {"mcpServers": {"srv": {"command": "npx", "args": ["-y", "pkg"]}}}
    assert routes._mcp_server_commands(document) == {"srv": "npx -y pkg"}


# ---------------------------------------------------------------------------
# _changed_commands_for_file: unchanged entry is excluded, changed/added
# entries are included.
# ---------------------------------------------------------------------------


def test_changed_commands_for_file_excludes_unchanged_entry() -> None:
    live = {"hooks": [{"name": "same", "command": "echo x"}]}
    incoming = {"hooks": [{"name": "same", "command": "echo x"}]}
    result = routes._changed_commands_for_file(
        "hooks.json", live, incoming, routes._hook_commands
    )
    assert result == []


def test_changed_commands_for_file_includes_changed_entry() -> None:
    live = {"hooks": [{"name": "same", "command": "echo old"}]}
    incoming = {"hooks": [{"name": "same", "command": "echo new"}]}
    result = routes._changed_commands_for_file(
        "hooks.json", live, incoming, routes._hook_commands
    )
    assert result == [{"file": "hooks.json", "name": "same", "command": "echo new"}]


# ---------------------------------------------------------------------------
# _pending_changed_commands: no pending record; pending with no/invalid
# sha; materialize failure surfaces as an error string, never raises.
# ---------------------------------------------------------------------------


def test_pending_changed_commands_no_pending_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(tmp_path / "state"))
    store = state.load_state()
    assert routes._pending_changed_commands(store) == ([], None)


def test_pending_changed_commands_pending_with_no_sha_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(tmp_path / "state"))
    store = state.load_state()
    # A pending record with no usable sha (shouldn't happen via set_pending,
    # but _pending_changed_commands must not raise if it ever does).
    store._payload["pending"] = {"sha": None, "classified_paths": {}}
    assert routes._pending_changed_commands(store) == ([], None)


def test_pending_changed_commands_materialize_failure_reports_error_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(tmp_path / "state"))
    store = state.load_state()
    store.set_pending(
        sha="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        author="a",
        subject="s",
        classified_paths={"hooks.json": "live_in_new_session"},
    )

    def _raise_materialize_error(*_args: Any, **_kwargs: Any) -> Any:
        raise routes._MaterializeError("could not materialize commit")

    monkeypatch.setattr(routes, "_materialize_pending_commit", _raise_materialize_error)

    changed_commands, materialize_error = routes._pending_changed_commands(store)
    assert changed_commands == []
    assert materialize_error == "could not materialize commit"


# ---------------------------------------------------------------------------
# is_app_enabled: the ImportError branch (platform import genuinely
# unavailable) returns False rather than raising.
# ---------------------------------------------------------------------------


def test_is_app_enabled_returns_false_when_platform_import_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def _fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "kiro_crew.apps.manager":
            raise ImportError("simulated: kiro_crew not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    assert routes.is_app_enabled("config-sync") is False


# ---------------------------------------------------------------------------
# _relpath_to_root / _root_map_for: moved to backend/materialize.py under
# the auto-apply ruling (routes.py no longer defines these) — None
# changed_paths, an untraceable relpath, and a traceable one.
# ---------------------------------------------------------------------------


def test_relpath_to_root_returns_none_when_changed_paths_is_none() -> None:
    assert materialize._relpath_to_root("x.md", None) is None


def test_relpath_to_root_returns_none_when_relpath_not_in_any_root() -> None:
    changed_paths = {"A": ["a.md"], "B": ["b.md"]}
    assert materialize._relpath_to_root("missing.md", changed_paths) is None


def test_relpath_to_root_finds_the_owning_root() -> None:
    changed_paths = {"A": ["a.md"], "B": ["b.md"]}
    assert materialize._relpath_to_root("b.md", changed_paths) == "B"


def test_root_map_for_omits_untraceable_relpaths() -> None:
    changed_paths = {"A": ["a.md"], "B": []}
    result = materialize._root_map_for(["a.md", "untraceable.md"], changed_paths)
    assert result == {"a.md": "A"}


# ---------------------------------------------------------------------------
# _is_safe_apply_id: the rejecting branch (a "/" or ".." in the id).
# ---------------------------------------------------------------------------


def test_is_safe_apply_id_rejects_a_path_separator() -> None:
    assert routes._is_safe_apply_id("apply-1/../escape") is False


def test_is_safe_apply_id_rejects_dot_dot() -> None:
    assert routes._is_safe_apply_id("..") is False


def test_is_safe_apply_id_accepts_a_plain_segment() -> None:
    assert routes._is_safe_apply_id("apply-20260101-abcdef") is True


# ---------------------------------------------------------------------------
# _restore_manifest_relpaths: malformed JSON returns [].
# ---------------------------------------------------------------------------


def test_restore_manifest_relpaths_malformed_json_returns_empty(
    tmp_path: Path,
) -> None:
    restore_dir = tmp_path / "restore"
    root_dir = restore_dir / "A"
    root_dir.mkdir(parents=True)
    (root_dir / routes._CREATED_MANIFEST_NAME).write_text(
        "{not valid", encoding="utf-8"
    )
    assert routes._restore_manifest_relpaths(restore_dir, "A") == []


def test_restore_manifest_relpaths_non_list_json_returns_empty(
    tmp_path: Path,
) -> None:
    restore_dir = tmp_path / "restore"
    root_dir = restore_dir / "A"
    root_dir.mkdir(parents=True)
    (root_dir / routes._CREATED_MANIFEST_NAME).write_text(
        json.dumps({"not": "a list"}), encoding="utf-8"
    )
    assert routes._restore_manifest_relpaths(restore_dir, "A") == []


# ---------------------------------------------------------------------------
# restore(): a recorded backed-up relpath whose backup file is missing on
# disk is skipped rather than restored.
# ---------------------------------------------------------------------------


def test_restore_skips_a_recorded_relpath_whose_backup_file_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(tmp_path / "state"))
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    root_a.mkdir()
    root_b.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(root_a))
    monkeypatch.setenv("KIRO_HOME", str(root_b))
    monkeypatch.setattr(routes, "is_app_enabled", lambda _name: True)

    store = state.load_state()
    restore_dir = tmp_path / "restore-dir"
    (restore_dir / "A").mkdir(parents=True)
    # Live file exists, but no backup file was ever written for it —
    # _restore_backed_up_relpaths only lists files that DO exist under
    # restore_dir/A, so simulate the gap by writing a live target and
    # then deleting the backup copy after recording it.
    (restore_dir / "A" / "steering").mkdir(parents=True)
    backup_file = restore_dir / "A" / "steering" / "x.md"
    backup_file.write_text("backed up\n", encoding="utf-8")
    (root_a / "steering").mkdir(parents=True)
    (root_a / "steering" / "x.md").write_text("live\n", encoding="utf-8")

    store.record_restore_dir(apply_id="apply-1", restore_dir=str(restore_dir))

    # Remove the backup file right before restore runs, so
    # _restore_backed_up_relpaths (which lists it via rglob at call time)
    # still names the relpath, but the is_file() check inside restore()
    # itself sees it gone.
    original_backed_up = routes._restore_backed_up_relpaths

    def _stale_listing(restore_dir_arg: Path, root: str) -> list[str]:
        listing = original_backed_up(restore_dir_arg, root)
        backup_file.unlink(missing_ok=True)
        return listing

    monkeypatch.setattr(routes, "_restore_backed_up_relpaths", _stale_listing)

    result = routes.restore(store, "apply-1")

    assert result["restored"] == {"A": [], "B": []}
    assert (root_a / "steering" / "x.md").read_text(encoding="utf-8") == "live\n"
