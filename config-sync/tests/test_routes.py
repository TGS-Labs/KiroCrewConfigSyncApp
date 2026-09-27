"""Tests for `backend/routes.py`'s surviving routes (tasks.md 6.1).

Covers requirements.md 7.1, 7.2, 7.4, 7.5 and design.md's routes section.

**Operator ruling (requirements.md Introduction; Requirement 4.4, 4.6
[Reserved], 4.14): there is no approve/decline route on the box.** The
poll tick applies an incoming commit automatically — see
`tests/test_poll_autoapply.py` for that path's own coverage (poll calls
`apply.apply_commit` directly; there is no operator decision step, no
`resolve_pending`, and no `#65` staleness race on a per-request `sha`
argument, since nothing external ever supplies one). The routes this
file covers are the ones that remain:

  | Route | Purpose |
  |---|---|
  | GET  /api/status  | push state, drift flag, last-seen
  |                     SHA |
  | GET  /api/drift   | tree hash vs last pushed + changed
  |                     files |
  | POST /api/push    | push-now |

(Backend paths as the gateway forwards them; the browser calls
`/apps/config-sync/api/...`.)

Interface:

    status(store: StateStore) -> dict
    drift(store: StateStore) -> dict
    push_now(store: StateStore) -> dict

Each returns a plain JSON-serializable dict with at least a "status" key
("ok" | "error"). Enabled-gating is done via a single seam,
`is_app_enabled: Callable[[str], bool]`, matching the platform convention
in `kiro_crew.apps.manager.is_app_enabled` — tests below monkeypatch
`backend.routes.is_app_enabled` directly rather than the real
`kiro_crew.apps.manager` module, since routes.py is what must consult it
on every call.

Every route wraps a real dependency (`push.run`) so these tests use
monkeypatch/spy rather than a live filesystem/network round trip for
that; `StateStore` itself is real, backed by tmp_path (matching
test_state.py's convention).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest

from backend import state


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Isolate ALL THREE filesystem surfaces this suite can touch: the

    app's own state dir, and both tracked configuration roots
    (KIROCREW_HOME / KIRO_HOME). status()/drift() walk collect.collect()
    under the hood, which defaults to the real ~/.kiro/crew and ~/.kiro
    when these env vars are unset -- on a real dev machine that can be a
    large, slow tree (and status()/drift() tests would hang or leak real
    file content into assertions). Point both roots at empty, disposable
    tmp_path directories so every route test collects nothing but what a
    given test explicitly seeds (e.g. TestNoUnredactedCredentialInAnyResponse's
    own seeded_mcp_json fixture, which overrides these same two env vars
    with its own root A content).
    """
    state_dir = tmp_path / "config-sync-state"
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(state_dir))

    root_a = tmp_path / "kiro-crew-home"
    root_b = tmp_path / "kiro-home"
    root_a.mkdir(parents=True, exist_ok=True)
    root_b.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("KIROCREW_HOME", str(root_a))
    monkeypatch.setenv("KIRO_HOME", str(root_b))

    yield state_dir


@pytest.fixture
def store(isolated_state_dir: Path) -> state.StateStore:
    return state.load_state()


@pytest.fixture
def routes_module() -> Any:
    """Import backend.routes lazily inside the fixture (not at module

    import time) so every OTHER test module in this suite still collects
    even while routes.py does not exist yet -- only tests that actually
    need it fail, and they fail with a clear ModuleNotFoundError.
    """
    from backend import routes

    return routes


@pytest.fixture(autouse=True)
def enabled_by_default(routes_module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every test to "app enabled" so tests that are not

    specifically about the disabled-gate don't have to think about it.
    The disabled-gate test class below overrides this per-test.
    """
    monkeypatch.setattr(routes_module, "is_app_enabled", lambda _name: True)


# ---------------------------------------------------------------------------
# Every route refuses while the app is disabled
# ---------------------------------------------------------------------------


class TestEveryRouteRefusesWhileDisabled:
    """Requirements.md 7.4: "WHEN the app is disabled THEN every backend

    route SHALL refuse the request." Overrides the autouse
    `enabled_by_default` fixture per-test via monkeypatch, matching the
    real gate seam (`routes_module.is_app_enabled`).

    Implementation mutation that would make each of these fail: a route
    checking `is_app_enabled` only on SOME code paths (e.g. approve checks
    it but status/drift/push_now do not), or checking it once at import
    time instead of on every call.
    """

    @pytest.fixture(autouse=True)
    def disabled(self, routes_module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(routes_module, "is_app_enabled", lambda _name: False)

    def test_status_refuses(self, routes_module: Any, store: state.StateStore) -> None:
        result = routes_module.status(store)
        assert result.get("status") == "error"

    def test_drift_refuses(self, routes_module: Any, store: state.StateStore) -> None:
        result = routes_module.drift(store)
        assert result.get("status") == "error"

    def test_push_now_refuses(
        self,
        routes_module: Any,
        store: state.StateStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # push.run must never even be reached while disabled.
        calls: list[Any] = []
        monkeypatch.setattr(routes_module, "push_run", lambda: calls.append(1))
        result = routes_module.push_now(store)
        assert result.get("status") == "error"
        assert calls == []


# ---------------------------------------------------------------------------
# No response body contains an unredacted credential
# ---------------------------------------------------------------------------


class TestNoUnredactedCredentialInAnyResponse:
    """Requirements.md 7.5 / 3.8: "No UI field or API response SHALL

    contain an unredacted credential." Security assertion, mutation-tested
    per testing-standards.md: seed a LIVE mcp.json (root A) carrying a real
    header token, drive every route, and assert the raw token string
    appears in NONE of the serialized response bodies.

    Implementation mutation that would make this test fail: a route that
    includes raw file bytes or an unredacted `pending`/`drift` changed-file
    preview sourced from `collect.collect()` directly rather than from
    `redact.redact()`'s output -- e.g. a "drift" route that reads the live
    mcp.json off disk to build its changed-file summary instead of reusing
    the redacted collected tree push.py already computes.
    """

    LIVE_TOKEN = "sk-live-CONFIDENTIAL-DO-NOT-LEAK-9f8e7d6c5b4a"

    @pytest.fixture(autouse=True)
    def seeded_mcp_json(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root_a = tmp_path / "kiro-crew-home"
        root_a.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("KIROCREW_HOME", str(root_a))
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro-home"))
        (root_a / "mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "example": {
                            "command": "npx",
                            "headers": {"Authorization": self.LIVE_TOKEN},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

    def _assert_token_absent(self, payload: dict[str, Any]) -> None:
        serialized = json.dumps(payload)
        assert self.LIVE_TOKEN not in serialized

    def test_status_never_leaks_the_token(
        self, routes_module: Any, store: state.StateStore
    ) -> None:
        self._assert_token_absent(routes_module.status(store))

    def test_drift_never_leaks_the_token(
        self, routes_module: Any, store: state.StateStore
    ) -> None:
        self._assert_token_absent(routes_module.drift(store))

    def test_push_now_never_leaks_the_token(
        self,
        routes_module: Any,
        store: state.StateStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Stub the underlying push so this test isolates the response-body
        # redaction question from push.py's own network/git behaviour
        # (that pipeline is exercised in test_push.py). The stub still
        # returns content shaped like a real PushResult so a route that
        # forwarded raw collected bytes into its response would still be
        # caught if it read them independently.
        class _Result:
            outcome = "no-op"
            tree_hash = "deadbeef"
            reason = ""

        monkeypatch.setattr(routes_module, "push_run", lambda: _Result())
        self._assert_token_absent(routes_module.push_now(store))
