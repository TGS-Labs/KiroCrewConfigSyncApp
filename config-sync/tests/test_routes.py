"""FAILING tests for tasks.md 6.1's `backend/routes.py`.

Covers requirements.md 4.9, 7.1, 7.2, 7.4, 7.5 and design.md's routes
section:

  | Route | Purpose |
  |---|---|
  | GET  /api/apps/config-sync/status  | push state, drift flag, last-seen
  |                                       SHA, pending summary |
  | GET  /api/apps/config-sync/drift   | tree hash vs last pushed + changed
  |                                       files |
  | POST /api/apps/config-sync/push    | push-now |
  | POST .../pending/{sha}/approve     | the ONLY route that applies |
  | POST .../pending/{sha}/decline     | clear pending, change nothing |

`backend/routes.py` does not exist yet (only `backend/server.py`'s bare
scaffold handler exists, with two stub GETs and no approve/decline/drift/
push-now at all) — every test below is expected to fail at collection with
a ModuleNotFoundError until software-engineer adds the module.

Interface pinned for software-engineer (framework-agnostic, matching the
scaffold's own bare-function shape rather than inventing a web framework
dependency `server.py` does not already have):

    status(store: StateStore) -> dict
    drift(store: StateStore) -> dict
    push_now(store: StateStore) -> dict
    approve(store: StateStore, sha: str) -> dict
    decline(store: StateStore, sha: str) -> dict

Each returns a plain JSON-serializable dict with at least a "status" key
("ok" | "error") the tests below key off of, so this module stays
importable and callable directly from tests without spinning up
`http.server`. Enabled-gating is done via a single seam,
`is_app_enabled: Callable[[str], bool]`, matching the platform convention
in `kiro_crew.apps.manager.is_app_enabled` (see `code_review_sage`'s
`backend/routes.py::_require_enabled`) — tests below monkeypatch
`backend.routes.is_app_enabled` directly rather than the real
`kiro_crew.apps.manager` module, since routes.py is what must consult it
on every call.

Every route wraps a real dependency (`apply.apply_commit`, `push.run`) so
these tests use monkeypatch/spy rather than a live filesystem/network
round trip for those; `StateStore` itself is real, backed by tmp_path
(matching test_state.py / test_state_resolve_pending.py's convention) —
`resolve_pending` (tasks.md 6.1's other half) is exercised for real, not
mocked, so a routes.py that fails to call it shows up as a real state
mismatch rather than a satisfied mock expectation.
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


def _seed_pending(
    store: state.StateStore,
    *,
    sha: str = "cccccccccccccccccccccccccccccccccccccc",
) -> None:
    store.set_pending(
        sha=sha,
        author="Author Name <author@example.com>",
        subject="Rotate a token",
        classified_paths={"mcp.json": "LIVE_ON_NEXT_RESOLUTION"},
    )


class _ApplySpy:
    """Records every call to a stand-in for `apply.apply_commit`, and

    returns a canned successful ApplyResult-shaped dict so route tests
    don't need the real apply pipeline (commit_root, changed_paths, a real
    git checkout) wired up -- that pipeline is exercised by test_apply.py.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)

        class _Result:
            outcome = "applied"
            applied: list[str] = []
            not_applied: list[str] = []
            reason = ""

        return _Result()


# ---------------------------------------------------------------------------
# approve is the ONLY route that calls apply
# ---------------------------------------------------------------------------


class TestApproveOnlyRouteCallsApply:
    """Security/data-integrity guard (testing-standards.md Mutation

    Requirement): approve must be the sole path to apply.apply_commit.
    Implementation mutation that would make each "never calls apply" test
    fail: routing decline (or status, or drift) through the same
    apply-dispatch helper approve uses -- e.g. a shared
    `_maybe_apply(action, sha)` that both approve and decline funnel
    through without gating on `action == "approve"`.
    """

    def test_approve_calls_apply_commit_exactly_once(
        self,
        routes_module: Any,
        store: state.StateStore,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        sha = "cccccccccccccccccccccccccccccccccccccc"
        _seed_pending(store, sha=sha)
        spy = _ApplySpy()
        monkeypatch.setattr(routes_module, "apply_commit", spy)
        monkeypatch.setattr(
            routes_module,
            "_materialize_pending_commit",
            lambda _store, _sha: (
                tmp_path,
                {"A": [], "B": []},
                {"A": [], "B": []},
            ),
        )

        routes_module.approve(store, sha)

        assert len(spy.calls) == 1

    def test_decline_never_calls_apply_commit(
        self,
        routes_module: Any,
        store: state.StateStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sha = "cccccccccccccccccccccccccccccccccccccc"
        _seed_pending(store, sha=sha)
        spy = _ApplySpy()
        monkeypatch.setattr(routes_module, "apply_commit", spy)

        routes_module.decline(store, sha)

        assert spy.calls == []

    def test_status_never_calls_apply_commit(
        self,
        routes_module: Any,
        store: state.StateStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_pending(store)
        spy = _ApplySpy()
        monkeypatch.setattr(routes_module, "apply_commit", spy)

        routes_module.status(store)

        assert spy.calls == []

    def test_drift_never_calls_apply_commit(
        self,
        routes_module: Any,
        store: state.StateStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_pending(store)
        spy = _ApplySpy()
        monkeypatch.setattr(routes_module, "apply_commit", spy)

        routes_module.drift(store)

        assert spy.calls == []


# ---------------------------------------------------------------------------
# approve / decline call state.resolve_pending; #65 staleness refusal
# ---------------------------------------------------------------------------


class TestApproveDeclineResolvePending:
    def test_approve_resolves_pending_via_state(
        self,
        routes_module: Any,
        store: state.StateStore,
        isolated_state_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        sha = "cccccccccccccccccccccccccccccccccccccc"
        _seed_pending(store, sha=sha)
        monkeypatch.setattr(routes_module, "apply_commit", _ApplySpy())
        monkeypatch.setattr(
            routes_module,
            "_materialize_pending_commit",
            lambda _store, _sha: (
                tmp_path,
                {"A": [], "B": []},
                {"A": [], "B": []},
            ),
        )

        routes_module.approve(store, sha)

        reloaded = state.load_state()
        assert reloaded.base_sha == sha
        assert reloaded.pending is None

    def test_decline_resolves_pending_via_state(
        self,
        routes_module: Any,
        store: state.StateStore,
        isolated_state_dir: Path,
    ) -> None:
        sha = "cccccccccccccccccccccccccccccccccccccc"
        _seed_pending(store, sha=sha)

        routes_module.decline(store, sha)

        reloaded = state.load_state()
        assert reloaded.base_sha == sha
        assert reloaded.pending is None

    def test_approve_refuses_when_submitted_sha_is_stale(
        self,
        routes_module: Any,
        store: state.StateStore,
        isolated_state_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The #65 case: a poll tick accumulated a NEWER commit into

        `pending` after the operator's UI rendered against an OLDER sha.
        Approving the stale sha must refuse and leave pending/base_sha
        untouched -- otherwise the operator's decision is applied against
        files they never actually reviewed.

        Implementation mutation that would make this test fail: approve
        reading `store.pending["sha"]` fresh but comparing it with `==`
        against the WRONG value (e.g. `store.base_sha` instead of the
        route's own `sha` argument), or approve calling
        `apply.apply_commit`/`state.resolve_pending` unconditionally
        before checking the submitted sha against current pending at all.
        """
        stale_sha_operator_saw = "cccccccccccccccccccccccccccccccccccccc"
        newer_sha_now_pending = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        _seed_pending(store, sha=newer_sha_now_pending)
        spy = _ApplySpy()
        monkeypatch.setattr(routes_module, "apply_commit", spy)

        result = routes_module.approve(store, stale_sha_operator_saw)

        assert result.get("status") == "error"
        assert spy.calls == []  # never reached apply
        reloaded = state.load_state()
        assert reloaded.pending is not None
        assert reloaded.pending["sha"] == newer_sha_now_pending
        assert reloaded.base_sha != stale_sha_operator_saw

    def test_decline_refuses_when_submitted_sha_is_stale(
        self,
        routes_module: Any,
        store: state.StateStore,
        isolated_state_dir: Path,
    ) -> None:
        """Same #65 staleness guard on the decline side: declining a sha

        the operator no longer actually sees pending must not silently
        clear the newer, still-unreviewed pending record -- that commit's
        files would otherwise resurface as if never decided, exactly the
        defect requirements.md 4.9 exists to close.
        """
        stale_sha_operator_saw = "cccccccccccccccccccccccccccccccccccccc"
        newer_sha_now_pending = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        _seed_pending(store, sha=newer_sha_now_pending)

        result = routes_module.decline(store, stale_sha_operator_saw)

        assert result.get("status") == "error"
        reloaded = state.load_state()
        assert reloaded.pending is not None
        assert reloaded.pending["sha"] == newer_sha_now_pending
        assert reloaded.base_sha != stale_sha_operator_saw

    def test_approve_refuses_when_nothing_is_pending(
        self,
        routes_module: Any,
        store: state.StateStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        assert store.pending is None
        spy = _ApplySpy()
        monkeypatch.setattr(routes_module, "apply_commit", spy)

        result = routes_module.approve(store, "cccccccccccccccccccccccccccccccccccccc")

        assert result.get("status") == "error"
        assert spy.calls == []

    def test_decline_refuses_when_nothing_is_pending(
        self, routes_module: Any, store: state.StateStore
    ) -> None:
        assert store.pending is None

        result = routes_module.decline(store, "cccccccccccccccccccccccccccccccccccccc")

        assert result.get("status") == "error"


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

    def test_approve_refuses(
        self,
        routes_module: Any,
        store: state.StateStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_pending(store)
        spy = _ApplySpy()
        monkeypatch.setattr(routes_module, "apply_commit", spy)

        result = routes_module.approve(store, "cccccccccccccccccccccccccccccccccccccc")

        assert result.get("status") == "error"
        assert spy.calls == []

    def test_decline_refuses(self, routes_module: Any, store: state.StateStore) -> None:
        _seed_pending(store)

        result = routes_module.decline(store, "cccccccccccccccccccccccccccccccccccccc")

        assert result.get("status") == "error"


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

    def test_approve_response_never_leaks_the_token(
        self,
        routes_module: Any,
        store: state.StateStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sha = "cccccccccccccccccccccccccccccccccccccc"
        _seed_pending(store, sha=sha)
        monkeypatch.setattr(routes_module, "apply_commit", _ApplySpy())

        self._assert_token_absent(routes_module.approve(store, sha))

    def test_decline_response_never_leaks_the_token(
        self, routes_module: Any, store: state.StateStore
    ) -> None:
        sha = "cccccccccccccccccccccccccccccccccccccc"
        _seed_pending(store, sha=sha)

        self._assert_token_absent(routes_module.decline(store, sha))
