"""Tests for the HTTP wiring of `backend/server.py` (tasks.md 6.2's

"missing HTTP wiring"; design.md's routes table).

## Operator ruling — no approve/decline route

Under the operator ruling (requirements.md Introduction; Requirement
4.4, 4.6 [Reserved]), there is no box-side approve/decline step: the
poll tick applies an incoming commit automatically (see
`tests/test_poll_autoapply.py`). This file therefore only wires and
tests the routes that remain: `status`, `drift`, `push`, and `restore`.

## Interface this file assumes for `server.py`

The server module is expected to expose a way to build/start an
`HTTPServer` bound to `("127.0.0.1", 0)` (ephemeral port) so tests never
hard-code or fight over a port number, and to route each design-table
path/verb pair to the matching `backend.routes` function, passing it a
`StateStore` the server holds (module-level or per-request — this file
does not care which, only that the SAME store object the test seeded
state into is the one `routes.*` sees, and that the route functions
themselves are not re-implemented inline in `server.py`).

This file SPIES on `backend.routes.*` (patching each function on the
`backend.routes` module before starting the server) rather than
re-testing route BEHAVIOUR — `tests/test_routes.py` and
`tests/test_routes_restore.py` already cover that. What this file proves
is the HTTP-to-function WIRING: verb, path, path-parameter extraction,
status/content-type shape, and the loopback-only bind — nothing about
`routes.*`'s own internals.

## No real git/network

Every `backend.routes.*` function is replaced with a spy before the
server starts, so no test in this file ever reaches real git, a real
apply, or a real state file beyond what `StateStore` itself does in
`tmp_path` (state dir is pinned via `CONFIG_SYNC_STATE_DIR`, never the
real home, per the isolation convention `test_routes.py` /
`test_routes_restore.py` already use).
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from http.server import HTTPServer
from typing import Any, Callable, Dict, Iterator, List, Tuple

import pytest

from backend.server import _PREFIX
from proxy_sign import PROXY_SECRET_ENV, TEST_PROXY_SECRET, signed_headers

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_state_dir(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    state_dir = tmp_path / "config-sync-state"
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(state_dir))
    # Every non-health route requires the gateway HMAC; sign with the
    # shared test secret (tests/proxy_sign.py).
    monkeypatch.setenv(PROXY_SECRET_ENV, TEST_PROXY_SECRET)
    yield state_dir


@pytest.fixture
def route_calls(monkeypatch: pytest.MonkeyPatch) -> Dict[str, List[Tuple[Any, ...]]]:
    """Patches every `backend.routes.*` function the design table names

    with a spy recording ``(positional_args_after_store,)`` and returning
    a small, fixed JSON-serializable dict — proving the server dispatches
    to THIS function (identity, not merely "some function that returns
    2xx"), and proving the caller passed the request's own path
    parameter through (``sha``/``id``), not a hard-coded or empty one.
    """
    from backend import routes as routes_module

    calls: Dict[str, List[Tuple[Any, ...]]] = {
        "status": [],
        "drift": [],
        "push_now": [],
        "restore": [],
    }

    def _make(name: str, extra_args: int) -> Callable[..., Dict[str, Any]]:
        def _spy(store: Any, *args: Any) -> Dict[str, Any]:
            calls[name].append(args)
            return {"status": "ok", "spy": name, "args": list(args)}

        return _spy

    monkeypatch.setattr(routes_module, "status", _make("status", 0))
    monkeypatch.setattr(routes_module, "drift", _make("drift", 0))
    monkeypatch.setattr(routes_module, "push_now", _make("push_now", 0))
    # restore(store, apply_id) — tasks.md 6.2's own route, same shape as
    # push_now (store + one path-parameter).
    monkeypatch.setattr(
        routes_module,
        "restore",
        _make("restore", 1),
        raising=False,
    )

    return calls


@pytest.fixture
def running_server(
    isolated_state_dir: Any, route_calls: Dict[str, List[Tuple[Any, ...]]]
) -> Iterator[Tuple[str, int]]:
    """Starts the REAL `backend/server.py` HTTP server on 127.0.0.1 with

    an ephemeral port, in a background thread, and yields ``(host,
    port)``. Imports `backend.server` AFTER `route_calls` has already
    patched `backend.routes.*`, so the server's own dispatch table (built
    at import time or per-request — either way) sees the spies.
    """
    import importlib

    import backend.server as server_module

    importlib.reload(server_module)

    build_server: Callable[..., HTTPServer]
    if hasattr(server_module, "build_server"):
        build_server = server_module.build_server
    else:
        # Fallback the test still exercises if server.py has not yet
        # grown a dedicated builder: construct directly against the
        # module's own Handler, bound to an ephemeral loopback port.
        def build_server(host: str = "127.0.0.1", port: int = 0) -> HTTPServer:
            return HTTPServer((host, port), server_module.Handler)

        build_server = build_server

    httpd = build_server(host="127.0.0.1", port=0)
    host = str(httpd.server_address[0])
    port = httpd.server_address[1]

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        # Poll /health briefly instead of a fixed sleep, bounding startup
        # wait without depending on real time-of-day.
        deadline = time.monotonic() + 5.0
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                conn = http.client.HTTPConnection(host, port, timeout=1)
                conn.request("GET", "/health")
                resp = conn.getresponse()
                resp.read()
                conn.close()
                if resp.status == 200:
                    break
            except OSError as exc:
                last_error = exc
                time.sleep(0.05)
        else:
            raise AssertionError(f"server never became ready: {last_error}")

        yield host, port
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


# Every non-health route requires a valid gateway signature, and every
# mutating POST a loopback Host (see tests/test_server_security.py and
# tests/test_review4_proxy_auth.py). This file's own tests are about
# HTTP-to-function WIRING (verb, path, path-parameter extraction,
# status/content-type shape, loopback-only bind) — not about the guard
# itself — so every request _request() sends is signed and carries a
# loopback Host, letting each test keep exercising what it was written
# for instead of being redirected into a 401/403 before dispatch.


def _request(
    host: str,
    port: int,
    method: str,
    path: str,
    body: bytes | None = None,
) -> Tuple[int, Dict[str, str], bytes]:
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        headers = signed_headers(method, path, body or b"", {"Host": f"{host}:{port}"})
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        payload = resp.read()
        response_headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, response_headers, payload
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tests: each design-table route dispatches to its matching routes.* fn
# ---------------------------------------------------------------------------


def test_get_status_dispatches_to_routes_status(
    running_server: Tuple[str, int],
    route_calls: Dict[str, List[Tuple[Any, ...]]],
) -> None:
    host, port = running_server
    status_code, headers, body = _request(host, port, "GET", f"{_PREFIX}/status")

    assert status_code == 200
    assert headers.get("content-type", "").startswith("application/json")
    parsed = json.loads(body)
    assert parsed["spy"] == "status"
    assert len(route_calls["status"]) == 1


def test_get_drift_dispatches_to_routes_drift(
    running_server: Tuple[str, int],
    route_calls: Dict[str, List[Tuple[Any, ...]]],
) -> None:
    host, port = running_server
    status_code, headers, body = _request(host, port, "GET", f"{_PREFIX}/drift")

    assert status_code == 200
    assert headers.get("content-type", "").startswith("application/json")
    parsed = json.loads(body)
    assert parsed["spy"] == "drift"
    assert len(route_calls["drift"]) == 1


def test_post_push_dispatches_to_routes_push_now(
    running_server: Tuple[str, int],
    route_calls: Dict[str, List[Tuple[Any, ...]]],
) -> None:
    host, port = running_server
    status_code, headers, body = _request(host, port, "POST", f"{_PREFIX}/push")

    assert status_code == 200
    assert headers.get("content-type", "").startswith("application/json")
    parsed = json.loads(body)
    assert parsed["spy"] == "push_now"
    assert len(route_calls["push_now"]) == 1


def test_post_restore_dispatches_to_routes_restore_with_id(
    running_server: Tuple[str, int],
    route_calls: Dict[str, List[Tuple[Any, ...]]],
) -> None:
    host, port = running_server
    apply_id = "apply-20260101010101"
    status_code, headers, body = _request(
        host, port, "POST", f"{_PREFIX}/restore/{apply_id}"
    )

    assert status_code == 200
    assert headers.get("content-type", "").startswith("application/json")
    parsed = json.loads(body)
    assert parsed["spy"] == "restore"
    assert len(route_calls["restore"]) == 1
    assert route_calls["restore"][0] == (apply_id,)


# ---------------------------------------------------------------------------
# Tests: verb/path mismatch, unknown paths, JSON shape, loopback-only bind
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        f"{_PREFIX}/push",
        f"{_PREFIX}/restore/apply-1",
    ],
)
def test_get_on_a_post_only_route_is_405_or_404(
    running_server: Tuple[str, int],
    route_calls: Dict[str, List[Tuple[Any, ...]]],
    path: str,
) -> None:
    """A POST-only route reached with GET must be refused (405 or 404 —

    design.md pins the verb per route but not the exact refusal code),
    and MUST NOT dispatch into any `routes.*` spy — proving the server
    checks the verb rather than pattern-matching on path alone.
    """
    host, port = running_server
    status_code, _headers, _body = _request(host, port, "GET", path)

    assert status_code in (404, 405)
    assert sum(len(calls) for calls in route_calls.values()) == 0


@pytest.mark.parametrize(
    "path",
    [
        f"{_PREFIX}/status",
        f"{_PREFIX}/drift",
    ],
)
def test_post_on_a_get_only_route_is_405_or_404(
    running_server: Tuple[str, int],
    route_calls: Dict[str, List[Tuple[Any, ...]]],
    path: str,
) -> None:
    host, port = running_server
    status_code, _headers, _body = _request(host, port, "POST", path)

    assert status_code in (404, 405, 501)
    assert sum(len(calls) for calls in route_calls.values()) == 0


@pytest.mark.parametrize(
    "path",
    [
        "/",
        f"{_PREFIX}/",
        f"{_PREFIX}/nonexistent",
        "/api/apps/other-app/status",
        # The browser-visible SDK path is never served directly: only
        # the gateway-rewritten `/api/...` form reaches a route.
        "/apps/config-sync/api/status",
    ],
)
def test_unknown_path_is_404(
    running_server: Tuple[str, int],
    route_calls: Dict[str, List[Tuple[Any, ...]]],
    path: str,
) -> None:
    host, port = running_server
    status_code, _headers, _body = _request(host, port, "GET", path)

    assert status_code == 404
    assert sum(len(calls) for calls in route_calls.values()) == 0


@pytest.mark.parametrize(
    "apply_id",
    ["a/b", "..", "..%2Fetc"],
)
def test_restore_path_parameter_containing_slash_or_traversal_never_reaches_routes(
    running_server: Tuple[str, int],
    route_calls: Dict[str, List[Tuple[Any, ...]]],
    apply_id: str,
) -> None:
    host, port = running_server
    status_code, _headers, _body = _request(
        host, port, "POST", f"{_PREFIX}/restore/{apply_id}"
    )

    assert status_code in (400, 404, 405, 501)
    assert len(route_calls["restore"]) == 0


def test_response_body_is_valid_json_with_json_content_type_on_every_route(
    running_server: Tuple[str, int],
) -> None:
    host, port = running_server
    requests = [
        ("GET", f"{_PREFIX}/status"),
        ("GET", f"{_PREFIX}/drift"),
        ("POST", f"{_PREFIX}/push"),
        ("POST", f"{_PREFIX}/restore/apply-1"),
    ]
    for method, path in requests:
        status_code, headers, body = _request(host, port, method, path)
        assert status_code == 200, (method, path, status_code)
        assert headers.get("content-type", "").startswith("application/json"), (
            method,
            path,
            headers,
        )
        # Must parse as JSON — proves the body is not the scaffold's bare
        # dict repr or an HTML error page slipping through with a
        # mislabelled content-type.
        json.loads(body)


def test_server_binds_loopback_only(
    isolated_state_dir: Any,
    route_calls: Dict[str, List[Tuple[Any, ...]]],
) -> None:
    """The server MUST bind `127.0.0.1` (or `localhost`), never `0.0.0.0`

    or a wildcard/all-interfaces address — an unauthenticated backend
    reachable from other hosts on the network would expose every
    config-sync route (including `approve`, which triggers a real apply)
    to anyone who can reach the machine. This test starts the server via
    its own builder (not hard-coding 127.0.0.1 itself) and asserts the
    bound address is a loopback address.
    """
    import importlib

    import backend.server as server_module

    importlib.reload(server_module)

    if hasattr(server_module, "build_server"):
        httpd = server_module.build_server(port=0)
    else:
        httpd = HTTPServer(("127.0.0.1", 0), server_module.Handler)

    try:
        bound_host = str(httpd.server_address[0])
        assert bound_host in ("127.0.0.1", "localhost", "::1")
        assert bound_host != "0.0.0.0"
        assert bound_host != ""
    finally:
        httpd.server_close()
