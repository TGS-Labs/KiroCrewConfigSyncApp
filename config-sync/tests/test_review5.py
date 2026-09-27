"""Senior review round 5 — behaviour tests for the remaining findings.

High: the app-level poll pause duplicated the host's own cron auto-pause
(``kiro_crew/cron.py`` ``_AUTO_PAUSE_THRESHOLD = 5`` switches a command
cron off after 5 consecutive failures). Two pauses at the same threshold
fought each other: Push now cleared the app flag while the cron stayed
off, and a re-enabled cron then exited 0 as "paused" forever. The app
keeps the failure COUNTER (a real signal for the page) and drops its own
pause and every "resume" path.

Medium: a stuck partial retried at an unchanged head must not leave one
restore directory per tick on disk, and must not re-notify the operator
every tick with the same not-applied set.

Low: the Python seam test derived the UI prefix itself; ``API_BASE`` in
``App.tsx`` is now diffed against ``app.json`` directly.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from backend import state
from test_review4_poll import (
    _reload_store,
    bundle_url_patched,
    isolated_env,
    origin,
    poll_module,
    routes_module,
    shas,
    store,
)
from test_routes_approve_seam import (
    _advance_main_after,
    _init_origin_repo,
    _seed_history,
)

APP_ROOT = Path(__file__).resolve().parents[1]

#: Fixtures re-exported from test_review4_poll so this module's tests can
#: request them by name (pytest discovers fixtures in the module namespace).
REUSED_FIXTURES = (
    bundle_url_patched,
    isolated_env,
    origin,
    poll_module,
    routes_module,
    shas,
    store,
)


def _on_disk_restore_dirs(state_dir: Path) -> int:
    restores = state_dir / "restores"
    if not restores.is_dir():
        return 0
    return sum(1 for p in restores.iterdir() if p.is_dir())


def _block_every_write(root_a: Path) -> None:
    """Make every eligible write in the seeded commit fail identically."""
    (root_a / "steering").write_text("blocking file, not a dir", encoding="utf-8")
    blocking = root_a / "config-bundles"
    blocking.mkdir(parents=True, exist_ok=True)
    (blocking / "agent-prompts").write_text(
        "blocking file, not a dir", encoding="utf-8"
    )


class TestNoAppLevelPause:
    def test_run_keeps_attempting_after_many_failures(
        self,
        poll_module: Any,
        routes_module: Any,
        store: state.StateStore,
        isolated_env: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Seven failing ticks in a row: every one still attempts
        ``git ls-remote`` (outcome ``ls-remote-failed``, never a synthetic
        ``paused``), the counter reaches 7, and ``status()`` carries no
        ``poll_paused`` key — the host's cron auto-pause is the only pause."""
        monkeypatch.setattr(routes_module, "is_app_enabled", lambda _name: True)
        unreachable = isolated_env["state_dir"].parent / "does-not-exist.git"
        monkeypatch.setattr(poll_module, "BUNDLE_REPO_URL", str(unreachable))

        for expected in range(1, 8):
            result = poll_module.run()
            assert result.outcome == "ls-remote-failed"
            payload = routes_module.status(_reload_store())
            assert payload["poll_consecutive_failures"] == expected
            assert "poll_paused" not in payload

        assert not hasattr(poll_module, "POLL_PAUSE_AFTER")
        assert not hasattr(state.StateStore, "resume_polling")

    def test_a_successful_tick_still_resets_the_counter(
        self,
        poll_module: Any,
        routes_module: Any,
        store: state.StateStore,
        isolated_env: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(routes_module, "is_app_enabled", lambda _name: True)
        unreachable = isolated_env["state_dir"].parent / "does-not-exist.git"
        monkeypatch.setattr(poll_module, "BUNDLE_REPO_URL", str(unreachable))
        for _ in range(6):
            assert poll_module.run().outcome == "ls-remote-failed"

        real_work = isolated_env["state_dir"].parent / "real-work"
        real_work.mkdir(parents=True, exist_ok=True)
        real_origin = _init_origin_repo(real_work)
        _seed_history(real_origin, isolated_env["state_dir"].parent)
        monkeypatch.setattr(poll_module, "BUNDLE_REPO_URL", str(real_origin))

        assert poll_module.run().outcome == "changed"
        assert routes_module.status(_reload_store())["poll_consecutive_failures"] == 0


class TestStuckPartialRetry:
    def test_three_stuck_ticks_leave_at_most_one_restore_dir_on_disk(
        self,
        poll_module: Any,
        store: state.StateStore,
        origin: Path,
        shas: dict[str, str],
        bundle_url_patched: None,
        isolated_env: dict[str, Path],
    ) -> None:
        """Round 4 dropped the STATE entry for a no-write attempt but left
        the directory itself; this counts directories on disk."""
        _block_every_write(isolated_env["root_a"])
        for _ in range(3):
            assert poll_module.run().outcome == "changed"
        assert _on_disk_restore_dirs(isolated_env["state_dir"]) <= 1

    def test_identical_stuck_partial_notifies_the_operator_once(
        self,
        poll_module: Any,
        store: state.StateStore,
        origin: Path,
        shas: dict[str, str],
        bundle_url_patched: None,
        isolated_env: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The first partial outcome is announced (Req 4.3). A retry at the
        same head whose not-applied set is unchanged is not news and must
        not page the operator again every 15 minutes."""
        _block_every_write(isolated_env["root_a"])
        calls: list[dict[str, Any]] = []

        def _capture(**kwargs: Any) -> None:
            calls.append(kwargs)

        monkeypatch.setattr(poll_module, "notify_operator", _capture)
        for _ in range(3):
            assert poll_module.run().outcome == "changed"
        assert len(calls) == 1, f"stuck partial re-notified: {len(calls)} calls"
        assert calls[0]["not_applied"]

    def test_a_changed_not_applied_set_at_the_same_head_is_announced_again(
        self,
        poll_module: Any,
        store: state.StateStore,
        origin: Path,
        shas: dict[str, str],
        bundle_url_patched: None,
        isolated_env: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Control: when a retry's outcome differs (one blocker removed, so
        the not-applied set shrinks), that IS news and is notified."""
        root_a = isolated_env["root_a"]
        _block_every_write(root_a)
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            poll_module, "notify_operator", lambda **kw: calls.append(kw)
        )
        assert poll_module.run().outcome == "changed"
        # Unblock one target: the retry applies it, the remaining blocker
        # keeps the outcome partial, but with a smaller not-applied set.
        (root_a / "steering").unlink()
        assert poll_module.run().outcome == "changed"
        assert len(calls) == 2
        assert set(calls[1]["not_applied"]) < set(calls[0]["not_applied"])


class TestUndoAfterStuckPartial:
    """Senior review round 6 (Medium, present since round 4): a no-write
    partial attempt discards its restore directory but ``last_apply`` still
    named that attempt's apply id, so the page's Undo button posted an id
    with no restore point and the earlier REAL restore point was
    unreachable. ``last_apply.apply_id`` must keep pointing at the most
    recent attempt that actually wrote something (or be null when there is
    none), and Undo on it must succeed."""

    def test_last_apply_keeps_the_restore_bearing_apply_id(
        self,
        poll_module: Any,
        routes_module: Any,
        store: state.StateStore,
        origin: Path,
        shas: dict[str, str],
        bundle_url_patched: None,
        isolated_env: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(routes_module, "is_app_enabled", lambda _name: True)
        # Tick 1: a real apply that writes files -> a restore-bearing id.
        assert poll_module.run().outcome == "changed"
        first = _reload_store().last_apply
        assert first is not None and first["outcome"] == "applied"
        real_id = first["apply_id"]
        assert real_id in _reload_store().restore_dirs

        # main moves on with one new file under steering/; make that
        # directory read-only so the write fails per-path (recorded as
        # not_applied, nothing written) while tick 1's files and restore
        # point stay intact.
        _advance_main_after(origin, tmp_path, shas["merge"])
        steering = isolated_env["root_a"] / "steering"
        steering.chmod(0o555)
        try:
            assert poll_module.run().outcome == "changed"
        finally:
            steering.chmod(0o755)

        current = _reload_store()
        assert current.last_apply is not None
        assert current.last_apply["outcome"] == "partial"
        assert current.last_apply["apply_id"] == real_id, (
            "last_apply names a discarded apply id; Undo would fail with "
            "'no restore directory recorded'"
        )
        result = routes_module.restore(current, real_id)
        assert result["status"] == "ok", result

    def test_last_apply_apply_id_is_null_when_nothing_was_ever_written(
        self,
        poll_module: Any,
        store: state.StateStore,
        origin: Path,
        shas: dict[str, str],
        bundle_url_patched: None,
        isolated_env: dict[str, Path],
    ) -> None:
        _block_every_write(isolated_env["root_a"])
        assert poll_module.run().outcome == "changed"
        last = _reload_store().last_apply
        assert last is not None and last["outcome"] == "partial"
        assert last["apply_id"] is None


def test_ui_api_base_equals_the_declared_permission_prefix() -> None:
    """Seam: the UI's ``API_BASE`` literal must be exactly the prefix
    ``app.json`` declares under ``permissions.api`` — the Python seam test
    in test_review4_contract derives the prefix itself and so would not
    notice ``API_BASE`` drifting."""
    manifest = json.loads((APP_ROOT / "app.json").read_text(encoding="utf-8"))
    declared = manifest["permissions"]["api"]
    app_tsx = (APP_ROOT / "ui" / "src" / "App.tsx").read_text(encoding="utf-8")
    match = re.search(r"const API_BASE = '([^']+)'", app_tsx)
    assert match, "App.tsx no longer defines `const API_BASE = '...'`"
    assert match.group(1) in declared
    assert len(declared) == 1
