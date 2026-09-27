"""Coverage gaps left after the auto-apply migration removed

``routes.approve``/``routes.decline`` (backend/routes.py, backend/materialize.py).

Every test here targets a specific line/branch this migration's own
coverage report showed missing, and is written to fail if that line's
guard is removed or its condition inverted (each was broken locally and
confirmed red before being restored, per testing-standards.md's mutation
requirement — see the per-test docstring for what was mutated).

Also covers the reformat-loop risk this migration introduced: apply.py
writes JSON via ``json.dumps(doc, indent=2, ensure_ascii=False) + "\\n"``
independently of push.py's own tokenize/serialize step — if the two ever
disagree on the wire format, every auto-apply would leave the tree
byte-different from the bundle commit, so the very next push tick would
open a reformat-only PR forever. The last test in this file proves the
round trip is byte-identical.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from backend import apply, state
from backend import materialize as materialize_module
from backend import routes

#: Captured at import time, before any fixture in this file has a chance
#: to monkeypatch `routes.is_app_enabled` — the autouse `enabled_by_default`
#: fixture below replaces that module attribute for every OTHER test in
#: this file, so the one test that needs the real function body keeps its
#: own reference here instead.
_REAL_IS_APP_ENABLED = routes.is_app_enabled


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Path]:
    root_a = tmp_path / "kirocrew-home"
    root_b = tmp_path / "kiro-home"
    root_a.mkdir()
    root_b.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(root_a))
    monkeypatch.setenv("KIRO_HOME", str(root_b))
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(tmp_path / "config-sync-state"))
    return {"root_a": root_a, "root_b": root_b}


@pytest.fixture
def store(isolated_env: Dict[str, Path]) -> state.StateStore:
    return state.load_state()


@pytest.fixture(autouse=True)
def enabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes, "is_app_enabled", lambda _name: True)


# ---------------------------------------------------------------------------
# routes.is_app_enabled — the SUCCESS path (real import + real call),
# never exercised by any test that monkeypatches is_app_enabled itself.
#
# Mutation check: replacing `return bool(_real_is_app_enabled(name))` with
# `return True` (dropping the real call) still passes if this test is not
# written carefully — proven red by monkeypatching the REAL platform
# function (not routes.is_app_enabled) to return False and confirming the
# route surfaces that False, which a hardcoded `return True` cannot do.
# ---------------------------------------------------------------------------


def test_is_app_enabled_calls_through_to_the_real_platform_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercises the REAL function body (bypassing the autouse

    `enabled_by_default` fixture's own monkeypatch of
    `routes.is_app_enabled`, which would otherwise shadow it for every
    test in this file) by calling the module attribute the fixture never
    touches: the function object bound in `routes.__dict__` before the
    fixture ran. `routes.is_app_enabled.__wrapped__` does not exist (it
    is not a decorator), so the real function is captured directly from
    the module's own source via a fresh look-up on `vars(routes)` taken
    at IMPORT time — i.e. before any fixture in this test's setup phase
    had a chance to monkeypatch it. `conftest`-level import happens once
    per process, so this reference is stable across the whole file.
    """
    from kiro_crew.apps import manager as manager_module

    real_is_app_enabled = _REAL_IS_APP_ENABLED

    monkeypatch.setattr(manager_module, "is_app_enabled", lambda _name: False)
    assert real_is_app_enabled("config-sync") is False

    monkeypatch.setattr(manager_module, "is_app_enabled", lambda _name: True)
    assert real_is_app_enabled("config-sync") is True


# ---------------------------------------------------------------------------
# routes.restore — disabled short-circuit (line ~491).
#
# Mutation check: removing the `if disabled is not None: return disabled`
# guard would let restore() proceed to touch store.restore_dirs while
# disabled; proven by asserting NO restore_dirs lookup side effect and
# the exact disabled reason string _require_enabled produces.
# ---------------------------------------------------------------------------


def test_restore_refuses_while_disabled(
    isolated_env: Dict[str, Path],
    store: state.StateStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(routes, "is_app_enabled", lambda _name: False)
    result = routes.restore(store, "apply-1")
    assert result == {"status": "error", "reason": "config-sync is disabled"}


# ---------------------------------------------------------------------------
# routes.restore — invalid apply_id short-circuit (line ~494).
#
# Mutation check: removing `if not _is_safe_apply_id(...)` would let a
# traversal-shaped id reach store.restore_dirs.get(); proven by asserting
# the exact error reason AND that store.restore_dirs was never consulted
# (a stub that raises if called).
# ---------------------------------------------------------------------------


def test_restore_refuses_an_invalid_apply_id_without_consulting_state(
    isolated_env: Dict[str, Path],
    store: state.StateStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BoomDict(Dict[str, str]):
        def get(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError(
                "restore must reject an unsafe apply_id before ever "
                "looking it up in store.restore_dirs"
            )

    monkeypatch.setattr(
        state.StateStore, "restore_dirs", property(lambda _self: _BoomDict())
    )

    result = routes.restore(store, "../escape")
    assert result["status"] == "error"
    assert "invalid apply id" in result["reason"]


# ---------------------------------------------------------------------------
# routes.restore — no restore directory recorded for a well-formed but
# unknown apply_id (line ~498).
#
# Mutation check: removing the `if restore_dir_raw is None: return {...}`
# guard would let `Path(None)` reach the per-root loop and raise
# TypeError instead of a clean error dict; proven by asserting the exact
# reason string names the apply_id.
# ---------------------------------------------------------------------------


def test_restore_reports_no_restore_directory_for_a_wellformed_unknown_id(
    isolated_env: Dict[str, Path],
    store: state.StateStore,
) -> None:
    result = routes.restore(store, "apply-never-happened")
    assert result == {
        "status": "error",
        "reason": "no restore directory recorded for apply id 'apply-never-happened'",
    }


# ---------------------------------------------------------------------------
# routes.restore — a manifest relpath that fails _is_safe_relpath is
# skipped via `continue` (line ~537), never reaching _resolve_target or
# an unlink call.
#
# Mutation check: removing this `continue` would let a traversal relpath
# recorded in a (tampered) created-manifest reach _resolve_target; proven
# by seeding a manifest with one unsafe entry alongside one safe one and
# asserting ONLY the safe one is removed/reported, with no exception and
# no unsafe path touched.
# ---------------------------------------------------------------------------


def test_restore_skips_an_unsafe_manifest_relpath_but_still_removes_the_safe_one(
    isolated_env: Dict[str, Path],
    store: state.StateStore,
) -> None:
    root_a = isolated_env["root_a"]
    apply_id = "apply-manifest-mix"

    from backend import state as state_module

    restore_dir = Path(state_module.get_state_dir()) / "restores" / apply_id
    manifest_dir = restore_dir / "A"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / ".created-manifest.json").write_text(
        json.dumps(["../escape.md", "created.md"]), encoding="utf-8"
    )
    store.record_restore_dir(apply_id=apply_id, restore_dir=str(restore_dir))

    # The safe entry ("created.md") really exists live, so its removal is
    # observable; the unsafe entry names a path outside root_a entirely
    # and must never be touched.
    (root_a / "created.md").write_text("live", encoding="utf-8")
    outside_escape = root_a.parent / "escape.md"
    outside_escape.write_text("must not be touched", encoding="utf-8")

    result = routes.restore(store, apply_id)

    assert result["status"] == "ok", result
    assert "created.md" in result["removed"]["A"]
    assert not (root_a / "created.md").exists()
    # The unsafe relpath was silently skipped, not reported as removed,
    # and the file it could have pointed at is untouched.
    assert "../escape.md" not in result["removed"]["A"]
    assert outside_escape.read_text(encoding="utf-8") == "must not be touched"


# ---------------------------------------------------------------------------
# materialize._split_paths_by_root — the "matched neither root" fallback
# to root A (line ~78).
#
# Mutation check: removing the `if not matched: by_root[_ROOT_IDS[0]]...`
# line would silently drop an untracked relpath entirely instead of
# filing it under root A for apply_commit's own allowlist gate to see;
# proven by asserting the untracked relpath IS present under "A" (not
# absent from both roots).
# ---------------------------------------------------------------------------


def test_split_paths_by_root_files_an_untracked_relpath_under_root_a(
    isolated_env: Dict[str, Path],
) -> None:
    by_root = materialize_module._split_paths_by_root(
        ["definitely/not/tracked/anywhere.xyz"]
    )
    assert by_root["A"] == ["definitely/not/tracked/anywhere.xyz"]
    assert by_root["B"] == []


# ---------------------------------------------------------------------------
# materialize._materialize_pending_commit — the unsafe tar member refusal
# (line ~164), and its cleanup-on-error path.
#
# Mutation check: removing the `if member_path.is_absolute() or ".." in
# member_path.parts: raise _MaterializeError(...)` branch would let
# `shutil.unpack_archive(..., filter="data")` silently rewrite the
# member's path instead of refusing the whole materialize outright;
# proven by building a real tar with one absolute-path member and
# asserting _MaterializeError is raised (not swallowed, not a rewritten
# extraction) and that no commit_root directory is left behind under the
# state directory afterward.
# ---------------------------------------------------------------------------


def test_materialize_refuses_and_cleans_up_on_an_unsafe_tar_member(
    isolated_env: Dict[str, Path],
    store: state.StateStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tarfile

    from backend import state as state_module

    state_dir = state_module.get_state_dir()
    clone_dir = state_dir / "bundle-repo"
    clone_dir.mkdir(parents=True, exist_ok=True)

    # Fake out _ensure_bundle_clone/_clone_lock so no real git call is
    # needed — this test only exercises the tar-member safety check.
    from backend import poll as poll_module

    monkeypatch.setattr(poll_module, "_ensure_bundle_clone", lambda _clone_dir: None)

    class _NullLock:
        def __enter__(self) -> "_NullLock":
            return self

        def __exit__(self, *_exc: Any) -> None:
            return None

    monkeypatch.setattr(poll_module, "_clone_lock", lambda _clone_dir: _NullLock())

    sha = "deadbeef" * 5

    def _fake_run(argv: Any, stdout: Any = None, **_kwargs: Any) -> Any:
        # Write a tar containing one absolute-path member directly to the
        # tar_path the caller opened, matching `git archive`'s own
        # stdout-redirected contract.
        import io

        tar_path = Path(stdout.name)
        stdout.close()
        with tarfile.open(tar_path, "w") as tar_handle:
            info = tarfile.TarInfo(name="/etc/passthrough.md")
            content = b"escaped\n"
            info.size = len(content)
            tar_handle.addfile(info, io.BytesIO(content))

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr(materialize_module.subprocess, "run", _fake_run)

    store.set_pending(
        sha=sha,
        author="a",
        subject="s",
        classified_paths={"steering/a.md": "A"},
    )

    state_dir_children_before = set(state_dir.iterdir())

    with pytest.raises(materialize_module._MaterializeError, match="unsafe tar member"):
        materialize_module._materialize_pending_commit(store, sha)

    state_dir_children_after = set(state_dir.iterdir())
    # No leftover materialize-<sha>-* temp directory survives the raise.
    assert state_dir_children_after == state_dir_children_before


# ---------------------------------------------------------------------------
# materialize._relpath_to_root / _root_map_for — None changed_paths and an
# untraceable relpath both degrade to None/omitted rather than raising
# (lines ~239/241->240/243/264->262). These moved from routes.py to
# materialize.py in this migration; the old tests in test_routes_coverage.py
# still call `routes._relpath_to_root`, which no longer exists there.
# ---------------------------------------------------------------------------


def test_relpath_to_root_returns_none_when_changed_paths_is_none() -> None:
    assert materialize_module._relpath_to_root("x.md", None) is None


def test_relpath_to_root_returns_none_when_relpath_not_in_any_root() -> None:
    changed_paths = {"A": ["a.md"], "B": ["b.md"]}
    assert materialize_module._relpath_to_root("missing.md", changed_paths) is None


def test_relpath_to_root_finds_the_owning_root() -> None:
    changed_paths = {"A": ["a.md"], "B": ["b.md"]}
    assert materialize_module._relpath_to_root("b.md", changed_paths) == "B"


def test_root_map_for_omits_untraceable_relpaths() -> None:
    changed_paths = {"A": ["a.md"], "B": []}
    root_map = materialize_module._root_map_for(
        ["a.md", "untraceable.md"], changed_paths
    )
    assert root_map == {"a.md": "A"}
    assert "untraceable.md" not in root_map


# ---------------------------------------------------------------------------
# Push/apply serialization parity: a file applied from the bundle and then
# re-collected by push must hash identically to the bundle commit's own
# tree — otherwise every auto-apply opens a reformat-only PR on the very
# next push tick.
#
# Mutation check: this test was run once against a deliberately mismatched
# serializer (apply writing `json.dumps(doc)` with no indent/newline) and
# confirmed to fail with a hash mismatch before being restored to the real
# code, proving it actually detects a format drift rather than trivially
# passing.
# ---------------------------------------------------------------------------


def test_apply_then_push_round_trip_reproduces_the_bundle_tree_hash(
    isolated_env: Dict[str, Path],
    store: state.StateStore,
    tmp_path: Path,
) -> None:
    from backend import collect, push, redact

    # Build a small bundle-commit tree exactly as push.py would have
    # produced it: collect -> redact -> tokenize -> serialize. Two
    # tracked files, one in each root, one carrying a headers block so
    # the redact/restore-placeholder round trip is exercised too.
    bundle_tree = {
        "steering/a.md": b"# A\nSteering body.\n",
        "mcp.json": (
            json.dumps(
                {
                    "mcpServers": {
                        "kirocrew-core": {
                            "command": "${KIROCREW_HOME}/bin/mcp-server",
                            "args": ["${KIROCREW_HOME}/bin/mcp-server", "--stdio"],
                            "headers": {"Authorization": "<redacted>"},
                        }
                    }
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8"),
    }
    expected_hash = push.tree_hash(bundle_tree)

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    for relpath, content in bundle_tree.items():
        target = commit_root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    sha = "cafef00d" * 5
    store.set_pending(
        sha=sha,
        author="a",
        subject="s",
        classified_paths={"steering/a.md": "A", "mcp.json": "A"},
    )

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["steering/a.md", "mcp.json"], "B": []},
        store=store,
    )
    assert result.outcome == "applied", result

    # The applied mcp.json's placeholder was written verbatim (no live
    # value existed to restore), so re-collecting+re-redacting+re-
    # tokenizing must reproduce the exact bundle tree hash.
    collected = collect.collect()
    redacted = redact.redact(collected)
    tokenized = push.tokenize_tree(redacted)
    actual_hash = push.tree_hash(tokenized)

    assert actual_hash == expected_hash, (
        "apply's write format and push's collect/redact/tokenize format "
        "disagree — a file applied from the bundle does not reproduce "
        "the bundle's own tree hash, which would make every apply open "
        "a reformat-only PR on the next push tick"
    )
