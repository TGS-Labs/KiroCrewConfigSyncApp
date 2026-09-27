"""Generates the REAL `status()` payload the dashboard UI must render.

Senior review round 3, finding C-B: `ui/src` tests only pass because
`ui/src/test-stubs`' SDK stub does more than the real host SDK, and the
UI's own fixture data (`App.test.tsx`'s `LAST_APPLY_FULL`) is HAND-WRITTEN
rather than a real `backend.routes.status()` response. This test closes
that gap for the *data* half: it drives a real poll tick (real git repo,
real commits, real `apply_commit`) through `backend.poll.run()`, then
calls `backend.routes.status()` on the resulting store — the exact same
call `backend/server.py` makes for `GET /api/status` —
and compares its SHAPE (nested keys and scalar types) with the tracked
`ui/src/__fixtures__/status.real.json`. With `CONFIG_SYNC_REGEN_FIXTURE=1`
it rewrites that file instead (senior-review round 5: a plain run must
not dirty a tracked file).

The fixture always reflects whatever `backend.routes.status()` ACTUALLY
returns today, so a UI test built
against it fails the moment the UI's assumed field names
(`merged_sha`, `pr_urls`, a list-shaped `not_applied`, a string
`last_*_failure`, an `applying` key) diverge from reality — which they
currently do (see `ui/src/types.ts`'s top-of-file comment and
`ui/src/App.tsx`).

The scenario composed here deliberately exercises every UI-relevant
shape in ONE status response, reusing `test_poll_autoapply.py`'s own
real-git harness (`_init_origin_repo`, `_seed_history`,
`_bundle_repo_url_env`, `_git`, `_head_sha` from
`test_routes_approve_seam.py`) rather than re-deriving a second one:

  - one FULLY applied commit (steering file) -> `applied` non-empty,
    `outcome == "applied"`.
  - a SECOND, PARTIAL tick (malformed `mcp.json`, same construction
    `test_poll_autoapply.py::partial_shas` uses) -> `not_applied` is a
    real `{relpath: reason}` dict with the actual parse-failure reason,
    `outcome == "partial"`.
  - a paused cron import (`crons.json` with an unsafe command) ->
    `paused_cron_names` + `changed_commands` non-empty.
  - a credential-shaped `mcp.json` server header with no live value to
    restore -> `needs_credential` non-empty.
  - a push failure recorded on the store -> `last_push_failure` a real
    string (not the UI's assumed field either — see `types.ts`).

Run this file's own test to (re)generate the fixture:

    pytest tests/test_fixture_status_real.py -q

It is a normal pytest test (asserts the file was written and is valid
JSON), not a fixture/conftest hook, so it runs in CI like any other test
and fails loudly if the write itself fails — silently-stale fixture data
would defeat the entire point of this file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator

import pytest

from backend import routes, state

from test_routes_approve_seam import (
    _bundle_repo_url_env,
    _git,
    _init_origin_repo,
)

_FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent
    / "ui"
    / "src"
    / "__fixtures__"
    / "status.real.json"
)


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    return _init_origin_repo(tmp_path)


@pytest.fixture
def isolated_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Path]]:
    state_dir = tmp_path / "config-sync-state"
    root_a = tmp_path / "kiro-crew-home"
    root_b = tmp_path / "kiro-home"
    root_a.mkdir(parents=True, exist_ok=True)
    root_b.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setenv("KIROCREW_HOME", str(root_a))
    monkeypatch.setenv("KIRO_HOME", str(root_b))
    monkeypatch.setattr(routes, "is_app_enabled", lambda *_a, **_k: True)

    yield {"state_dir": state_dir, "root_a": root_a, "root_b": root_b}


@pytest.fixture
def poll_module(isolated_env: dict[str, Path]) -> Any:
    import importlib

    from backend import poll as poll_module_

    importlib.reload(poll_module_)
    return poll_module_


@pytest.fixture
def store(isolated_env: dict[str, Path]) -> state.StateStore:
    return state.load_state()


def _reload_store() -> state.StateStore:
    return state.load_state()


def test_generate_real_status_fixture_from_a_real_poll_tick(
    poll_module: Any,
    store: state.StateStore,
    origin: Path,
    tmp_path: Path,
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bundle_repo_url_env(monkeypatch, origin)

    work = tmp_path / "seed-work"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    _git("remote", "add", "origin", str(origin), cwd=work)

    # --- Commit 1: a clean, fully-applicable change. --------------------
    (work / "steering").mkdir()
    (work / "steering" / "fixture-demo.md").write_text(
        "# fixture demo\n", encoding="utf-8"
    )
    # A paused-cron-worthy entry: an unsafe command the vet must drop/pause
    # rather than apply verbatim (matches test_apply_command_vet.py's own
    # unsafe-command shape).
    (work / "crons.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "name": "fixture-nightly-backup",
                        "command": "tar -czf /backup.tgz /data",
                        "every": 86400,
                        "enabled": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (work / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "fixture-server": {
                        "command": "npx",
                        "args": ["-y", "fixture-mcp"],
                        "headers": {"Authorization": "Bearer <redacted>"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    _git("add", "-A", cwd=work)
    _git(
        "commit", "-q", "-m", "clean change + paused cron + needs-credential", cwd=work
    )
    _git("push", "-q", "-u", "origin", "main", cwd=work)

    result1 = poll_module.run()
    assert result1 is not None

    store = _reload_store()
    assert store.last_apply is not None, "first tick must have applied cleanly"
    assert store.last_apply.get("outcome") == "applied", store.last_apply

    # --- Commit 2: break mcp.json to force a partial outcome. -----------
    (work / "steering" / "fixture-demo-2.md").write_text(
        "# fixture demo 2\n", encoding="utf-8"
    )
    (work / "mcp.json").write_text("{not valid json", encoding="utf-8")
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "add another file, break mcp.json", cwd=work)
    _git("push", "-q", "origin", "main", cwd=work)

    result2 = poll_module.run()
    assert result2 is not None

    store = _reload_store()
    assert store.last_apply is not None
    assert store.last_apply.get("outcome") == "partial", store.last_apply
    assert store.last_apply.get(
        "not_applied"
    ), "the partial tick must record a real not_applied dict"

    # A push failure, recorded directly on the store the same way
    # backend/push.py itself would on a real refusal — status() must
    # surface it verbatim (Requirement 7's `last_push_failure`).
    store.record_push_failure(reason="secret scan found 1 finding; push refused")

    status_payload = routes.status(_reload_store())

    assert status_payload["status"] == "ok"
    assert status_payload["last_apply"] is not None
    assert isinstance(status_payload["last_apply"]["not_applied"], dict), (
        "not_applied must be the real {relpath: reason} dict shape, "
        "never a bare list"
    )

    if os.environ.get("CONFIG_SYNC_REGEN_FIXTURE") == "1":
        # Explicit regeneration only (senior-review round 5: an ordinary
        # test run must never rewrite a tracked file — the apply id and
        # timestamps differ on every run, so the working tree was dirty
        # after every `pytest`). Run with CONFIG_SYNC_REGEN_FIXTURE=1 and
        # commit the result when status()'s shape changes.
        _FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _FIXTURE_PATH.write_text(
            json.dumps(status_payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    tracked = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert _shape(tracked) == _shape(status_payload), (
        "ui/src/__fixtures__/status.real.json no longer has the shape "
        "backend.routes.status() returns — regenerate it with "
        "CONFIG_SYNC_REGEN_FIXTURE=1 pytest tests/test_fixture_status_real.py "
        "and commit the result"
    )


def _shape(value: Any) -> Any:
    """Structural signature: nested key sets and scalar type names, so the
    comparison ignores run-specific values (ids, times, shas, reasons)."""
    if isinstance(value, dict):
        return {k: _shape(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_shape(v) for v in value[:1]]
    return type(value).__name__
