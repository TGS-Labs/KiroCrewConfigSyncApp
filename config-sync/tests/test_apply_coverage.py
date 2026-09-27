"""Branch-coverage tests for real behaviours in ``backend/apply.py`` left

uncovered after operator ruling M5 (tests/test_apply_command_vet.py). Each
test below targets one specific branch and asserts the OBSERVABLE behaviour
that branch produces — not the coverage tool's line numbers, which shift.
Every test in this file was confirmed to fail when its target branch is
broken (verified by hand during authoring, per testing-standards.md's
Refactoring Test), then restored to pass — this is the required red->green
proof, not asserted here as a runtime check.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from backend import apply

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


@pytest.fixture
def target_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root_a = tmp_path / "kirocrew-home"
    root_b = tmp_path / "kiro-home"
    root_a.mkdir()
    root_b.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(root_a))
    monkeypatch.setenv("KIRO_HOME", str(root_b))
    return tmp_path


# ---------------------------------------------------------------------------
# _reference_exists_locally: the glob branch (a `file://`/`skill://`
# reference whose leaf name is a glob pattern) — at least one match is
# "exists", zero matches is "does not exist".
# ---------------------------------------------------------------------------


def test_reference_exists_locally_glob_pattern_matches_at_least_one_file(
    tmp_path: Path,
) -> None:
    (tmp_path / "skill-a.md").write_text("x", encoding="utf-8")
    assert apply._reference_exists_locally(str(tmp_path / "skill-*.md")) is True


def test_reference_exists_locally_glob_pattern_matches_nothing(
    tmp_path: Path,
) -> None:
    assert apply._reference_exists_locally(str(tmp_path / "skill-*.md")) is False


def test_reference_exists_locally_glob_pattern_with_unresolvable_parent(
    tmp_path: Path,
) -> None:
    # A glob pattern whose PARENT does not exist must not raise — the
    # `Path.glob` call on the missing parent must be treated as "no
    # match" rather than escaping as an exception.
    missing_parent = tmp_path / "does-not-exist" / "skill-*.md"
    assert apply._reference_exists_locally(str(missing_parent)) is False


def test_reference_exists_locally_glob_oserror_from_parent_glob_is_not_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `Path.glob` itself raising OSError (e.g. a transient I/O error
    # walking the parent directory) must be treated as "does not exist",
    # never escape as an unhandled exception — real-filesystem
    # conditions (missing parent, non-directory parent) do not actually
    # raise from `glob` on this platform, so the raising path is
    # exercised directly against the real `Path.glob` method.
    (tmp_path / "skill-a.md").write_text("x", encoding="utf-8")

    original_glob = Path.glob

    def _raising_glob(self: Path, pattern: str) -> Any:
        if self == tmp_path:
            raise OSError("simulated I/O error")
        return original_glob(self, pattern)

    monkeypatch.setattr(Path, "glob", _raising_glob)
    assert apply._reference_exists_locally(str(tmp_path / "skill-*.md")) is False


def test_reference_exists_locally_literal_path_oserror_from_exists_is_not_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `Path.exists()` itself raising OSError (a literal, non-glob
    # reference whose existence check hits a transient I/O error) must
    # be treated as "does not exist", never escape as an unhandled
    # exception.
    literal_path = tmp_path / "literal.json"
    literal_path.write_text("x", encoding="utf-8")

    original_exists = Path.exists

    def _raising_exists(self: Path) -> bool:
        if self == literal_path:
            raise OSError("simulated I/O error")
        return original_exists(self)

    monkeypatch.setattr(Path, "exists", _raising_exists)
    assert apply._reference_exists_locally(str(literal_path)) is False


# ---------------------------------------------------------------------------
# _resolve_target: the `relative_to` ValueError branch — a resolved
# candidate that escapes the resolved root (a symlinked ROOT component
# whose target lies outside itself) must resolve to None, never raise.
# ---------------------------------------------------------------------------


def test_resolve_target_returns_none_when_candidate_escapes_symlinked_root(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    # A commit-tree-adjacent symlink component whose target lies outside
    # the root: the resolved candidate ends up under `outside`, which is
    # NOT contained in the resolved root, so `relative_to` raises
    # ValueError and `_resolve_target` must return None rather than
    # propagate it.
    escape_link = real_root / "escape"
    escape_link.symlink_to(outside)
    target = apply._resolve_target(real_root, "escape")
    assert target is None


def test_resolve_target_returns_none_when_resolve_raises_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `Path.resolve()` itself raising OSError (e.g. a filesystem loop or
    # a transient I/O error) must degrade to None, never propagate.
    real_root = tmp_path / "real-root"
    real_root.mkdir()

    original_resolve = Path.resolve

    def _raising_resolve(self: Path, strict: bool = False) -> Path:
        if self == real_root / "boom.json":
            raise OSError("simulated I/O error")
        return original_resolve(self, strict)

    monkeypatch.setattr(Path, "resolve", _raising_resolve)
    assert apply._resolve_target(real_root, "boom.json") is None


# ---------------------------------------------------------------------------
# _backup_file: the no-op branch — a live path that does not exist yet
# produces no backup file on disk, and does not raise.
# ---------------------------------------------------------------------------


def test_backup_file_is_a_noop_when_live_path_does_not_exist(tmp_path: Path) -> None:
    live_path = tmp_path / "live" / "does-not-exist.json"
    restore_dir = tmp_path / "restore"
    apply._backup_file(live_path, restore_dir, "A", "does-not-exist.json")
    assert not (restore_dir / "A" / "does-not-exist.json").exists()


# ---------------------------------------------------------------------------
# _write_created_manifest: the OSError branch — a manifest write that
# cannot complete (an existing FILE occupying the manifest's own parent
# directory path) must clean up its temp file and re-raise, never leave a
# silently-swallowed failure.
# ---------------------------------------------------------------------------


def test_write_created_manifest_reraises_and_cleans_up_temp_on_oserror(
    tmp_path: Path,
) -> None:
    restore_dir = tmp_path / "restore"
    root_dir = restore_dir / "A"
    root_dir.mkdir(parents=True)
    # Occupy the exact path the manifest would be written to WITH A
    # DIRECTORY, so `os.replace(tmp_path, manifest_path)` raises OSError
    # (a directory cannot be replaced by a regular file).
    manifest_path = root_dir / apply._CREATED_MANIFEST_NAME
    manifest_path.mkdir()

    with pytest.raises(OSError):
        apply._write_created_manifest(restore_dir, "A", ["some/relpath.json"])

    leftover_temp_files = [
        p
        for p in root_dir.iterdir()
        if p.name.startswith(f".{apply._CREATED_MANIFEST_NAME}.")
    ]
    assert leftover_temp_files == []


# ---------------------------------------------------------------------------
# _restore_redacted_values: the dict-descend else-branches — a value that
# is a dict with no "name" key uses the dict's own KEY as the child
# label; a value that is neither a dict nor inside headers/env keeps the
# PARENT's label. Exercised through the public entry point so the
# behaviour under test is the actual `needs_credential` labelling
# contract, not a private helper's return value in isolation.
# ---------------------------------------------------------------------------


def test_restore_redacted_values_labels_named_dict_child_by_its_own_name() -> None:
    # A dict VALUE that carries its own "name" field (e.g. an mcp.json
    # server object nested one level deeper than the server-name key
    # itself) must be labelled by that "name" field, not by the parent
    # dict's key.
    commit_doc = {
        "wrapper": {
            "inner": {
                "name": "real-name",
                "headers": {"Authorization": "<redacted>"},
            }
        }
    }
    needs_credential: list[str] = []
    apply._restore_redacted_values(commit_doc, None, "mcp.json", needs_credential)
    assert needs_credential == ["mcp.json:real-name.headers.Authorization"]


def test_restore_redacted_values_labels_nameless_dict_child_by_its_own_key() -> None:
    commit_doc = {
        "unnamed_block": {
            "headers": {"Authorization": "<redacted>"},
        }
    }
    needs_credential: list[str] = []
    apply._restore_redacted_values(commit_doc, None, "mcp.json", needs_credential)
    assert needs_credential == ["mcp.json:unnamed_block.headers.Authorization"]


def test_restore_redacted_values_keeps_parent_label_for_non_dict_sibling() -> None:
    # "plain_field" is a non-dict value sitting alongside "headers" in
    # the same object — walking it must not change the owner label away
    # from the file's own bare relpath (there is nothing better to
    # descend into), and must not raise.
    commit_doc = {
        "plain_field": "just-a-string",
        "headers": {"Authorization": "<redacted>"},
    }
    needs_credential: list[str] = []
    result = apply._restore_redacted_values(
        commit_doc, None, "mcp.json", needs_credential
    )
    assert result["plain_field"] == "just-a-string"
    assert needs_credential == ["mcp.json:mcp.json.headers.Authorization"]


# ---------------------------------------------------------------------------
# _frontmatter_changed: the "no live file at all" branch for a SKILL.md
# whose new content HAS no frontmatter either — both sides are "no
# frontmatter", so nothing changed.
# ---------------------------------------------------------------------------


def test_frontmatter_changed_false_when_new_has_none_and_live_is_absent(
    tmp_path: Path,
) -> None:
    live_path = tmp_path / "does-not-exist" / "SKILL.md"
    new_content = b"# just a heading, no frontmatter\n"
    assert apply._frontmatter_changed("skills/x/SKILL.md", new_content, live_path) is (
        False
    )


def test_frontmatter_changed_false_for_a_non_skill_md_relpath(tmp_path: Path) -> None:
    # A relpath that is not a SKILL.md has no frontmatter-change concept
    # at all — always False, regardless of content or live state.
    live_path = tmp_path / "config.json"
    assert apply._frontmatter_changed("config.json", b"---\nfoo\n---\n", live_path) is (
        False
    )


# ---------------------------------------------------------------------------
# _changed_mcp_commands: the three defensive isinstance guards — a
# non-dict `live_doc["mcpServers"]`, a non-dict `final_doc["mcpServers"]`,
# and a servers dict containing a non-dict entry must all be tolerated
# (never raise) rather than crashing on a malformed document.
# ---------------------------------------------------------------------------


def test_changed_mcp_commands_tolerates_non_dict_live_mcp_servers_value() -> None:
    final_doc = {"mcpServers": {"safe": {"command": "npx", "args": ["-y"]}}}
    live_doc: Any = {"mcpServers": "not-a-dict"}
    result = apply._changed_mcp_commands(final_doc, live_doc, "mcp.json")
    names = {entry["name"] for entry in result}
    assert "safe" in names


def test_changed_mcp_commands_returns_empty_when_final_mcp_servers_not_a_dict() -> None:
    final_doc: Any = {"mcpServers": "not-a-dict"}
    result = apply._changed_mcp_commands(final_doc, None, "mcp.json")
    assert result == []


def test_changed_mcp_commands_skips_non_dict_server_entry_without_raising() -> None:
    final_doc = {
        "mcpServers": {
            "malformed": "not-a-dict",
            "safe": {"command": "npx", "args": ["-y"]},
        }
    }
    result = apply._changed_mcp_commands(final_doc, None, "mcp.json")
    names = {entry["name"] for entry in result}
    assert names == {"safe"}


# ---------------------------------------------------------------------------
# apply_commit: the created-manifest OSError branch (989->926-shaped
# gap) — when writing the created-file manifest fails, the apply must
# degrade to `outcome="partial"`, clear `applied`, and carry the
# manifest-error reason; a first-file confirmed-backup failure elsewhere
# in the module already proves the loop's OTHER branches, so this
# targets ONLY the manifest-write failure path via a real apply.
# ---------------------------------------------------------------------------


def _init_bundle_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "bundle-repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "initial", cwd=repo)
    return repo


def test_apply_commit_reports_partial_when_created_manifest_write_fails(
    target_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend import state

    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(tmp_path / "config-sync-state"))
    store = state.load_state()

    bundle_repo = _init_bundle_repo(tmp_path)
    sha = _git("rev-parse", "HEAD", cwd=bundle_repo).stdout.strip()
    store.set_pending(
        sha=sha,
        author="a",
        subject="s",
        classified_paths={},
        ignored_paths=[],
        touched_classes=[],
    )

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    commit_root.joinpath("config.json").write_text(
        json.dumps({"k": "v"}), encoding="utf-8"
    )

    def _boom(restore_dir: Path, root: str, created_relpaths: list) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(apply, "_write_created_manifest", _boom)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["config.json"], "B": []},
        store=store,
    )

    assert result.outcome == "partial"
    assert result.applied == []
    assert "manifest was not recorded" in result.reason


def test_apply_commit_continues_the_loop_after_a_non_first_file_fails(
    target_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H3's per-file try/except: when the SECOND of three eligible files

    raises during `_apply_one_file`, the loop must still process the
    THIRD file rather than stop — `prior_progress` is already True from
    the first file's success, so the failure degrades to a
    `not_applied` entry and the loop continues to its next iteration.
    """
    from backend import state

    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(tmp_path / "config-sync-state"))
    store = state.load_state()

    bundle_repo = _init_bundle_repo(tmp_path)
    sha = _git("rev-parse", "HEAD", cwd=bundle_repo).stdout.strip()
    store.set_pending(
        sha=sha,
        author="a",
        subject="s",
        classified_paths={},
        ignored_paths=[],
        touched_classes=[],
    )

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    commit_root.joinpath("config.json").write_text(
        json.dumps({"k": "v"}), encoding="utf-8"
    )
    commit_root.joinpath("hooks.json").write_text(
        json.dumps({"hooks": []}), encoding="utf-8"
    )
    commit_root.joinpath("mcp.json").write_text(
        json.dumps({"mcpServers": {}}), encoding="utf-8"
    )

    original_apply_one_file = apply._apply_one_file

    def _boom_on_hooks(*, relpath: str, **kwargs: Any) -> None:
        if relpath == "hooks.json":
            raise RuntimeError("simulated crash on the second file")
        return original_apply_one_file(relpath=relpath, **kwargs)

    monkeypatch.setattr(apply, "_apply_one_file", _boom_on_hooks)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["config.json", "hooks.json", "mcp.json"], "B": []},
        store=store,
    )

    assert "config.json" in result.applied
    assert "hooks.json" in result.not_applied
    assert "mcp.json" in result.applied
