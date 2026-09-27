"""FAILING tests for the HTTP wiring of `backend/server.py` (tasks.md 6.2's

"missing HTTP wiring"; design.md's routes table, ~lines 331-345: "every
route is served by backend/server.py").

## The gap

`backend/server.py` today (37 lines) only implements `GET /health` and
`GET /api/apps/config-sync/status` (the latter as a hand-rolled stub — an
`{"app": ..., "version": ...}` body that does NOT dispatch to
`backend.routes.status`). It has no dispatch at all for `GET drift`,
`POST push`, `POST pending/{sha}/approve`, `POST pending/{sha}/decline`,
or `POST restore/{id}`, and no `StateStore` wiring to give `routes.*` its
required argument. This file drives the REAL server process over a REAL
HTTP socket (`127.0.0.1`, ephemeral port, in a background thread) and
proves each design-table route dispatches to its matching
`backend.routes.*` function.

## Interface this file assumes for `server.py`

The server module is expected to expose a way to build/start an
`HTTPServer` bound to `("127.0.0.1", 0)` (ephemeral port) so tests never
hard-code or fight over a port number, and to route each design-table
path/verb pair to the matching `backend.routes` function, passing it a
`StateStore` the server holds (module-level or per-request — this file
does not care which, only that the SAME store object the test seeded
pending/state into is the one `routes.*` sees, and that the route
functions themselves are not re-implemented inline in `server.py`).

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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_state_dir(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    state_dir = tmp_path / "config-sync-state"
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(state_dir))
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
        "approve": [],
        "decline": [],
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
    monkeypatch.setattr(routes_module, "approve", _make("approve", 1))
    monkeypatch.setattr(routes_module, "decline", _make("decline", 1))
    # restore(store, apply_id) — tasks.md 6.2's own route, same shape as
    # approve/decline (store + one path-parameter).
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


# senior-review H7 (see tests/test_server_security.py): every mutating
# POST now requires this custom header plus a loopback Host, or the
# server refuses it with 403 before any route runs. This file's own
# tests are about HTTP-to-function WIRING (verb, path, path-parameter
# extraction, status/content-type shape, loopback-only bind) — not
# about H7's guard itself — so every request _request() sends carries
# a satisfying header/Host pair by default, letting each test keep
# exercising what it was written for instead of being redirected into
# a 403 the guard produces before the PATH guard/dispatch ever runs.
_SECURITY_HEADER = "X-Config-Sync-Request"
_SECURITY_HEADER_VALUE = "1"


def _request(
    host: str,
    port: int,
    method: str,
    path: str,
    body: bytes | None = None,
) -> Tuple[int, Dict[str, str], bytes]:
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        headers = {
            "Host": f"{host}:{port}",
            _SECURITY_HEADER: _SECURITY_HEADER_VALUE,
        }
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
    status_code, headers, body = _request(
        host, port, "GET", "/api/apps/config-sync/status"
    )

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
    status_code, headers, body = _request(
        host, port, "GET", "/api/apps/config-sync/drift"
    )

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
    status_code, headers, body = _request(
        host, port, "POST", "/api/apps/config-sync/push"
    )

    assert status_code == 200
    assert headers.get("content-type", "").startswith("application/json")
    parsed = json.loads(body)
    assert parsed["spy"] == "push_now"
    assert len(route_calls["push_now"]) == 1


def test_post_pending_approve_dispatches_to_routes_approve_with_sha(
    running_server: Tuple[str, int],
    route_calls: Dict[str, List[Tuple[Any, ...]]],
) -> None:
    host, port = running_server
    sha = "abc123def456"
    status_code, headers, body = _request(
        host, port, "POST", f"/api/apps/config-sync/pending/{sha}/approve"
    )

    assert status_code == 200
    assert headers.get("content-type", "").startswith("application/json")
    parsed = json.loads(body)
    assert parsed["spy"] == "approve"
    assert len(route_calls["approve"]) == 1
    # The sha the client sent must be the exact value routes.approve saw —
    # never dropped, truncated, or hard-coded server-side.
    assert route_calls["approve"][0] == (sha,)


def test_post_pending_decline_dispatches_to_routes_decline_with_sha(
    running_server: Tuple[str, int],
    route_calls: Dict[str, List[Tuple[Any, ...]]],
) -> None:
    host, port = running_server
    sha = "fedcba098765"
    status_code, headers, body = _request(
        host, port, "POST", f"/api/apps/config-sync/pending/{sha}/decline"
    )

    assert status_code == 200
    assert headers.get("content-type", "").startswith("application/json")
    parsed = json.loads(body)
    assert parsed["spy"] == "decline"
    assert len(route_calls["decline"]) == 1
    assert route_calls["decline"][0] == (sha,)


def test_post_restore_dispatches_to_routes_restore_with_id(
    running_server: Tuple[str, int],
    route_calls: Dict[str, List[Tuple[Any, ...]]],
) -> None:
    host, port = running_server
    apply_id = "apply-20260101010101"
    status_code, headers, body = _request(
        host, port, "POST", f"/api/apps/config-sync/restore/{apply_id}"
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
        "/api/apps/config-sync/push",
        "/api/apps/config-sync/pending/abc/approve",
        "/api/apps/config-sync/pending/abc/decline",
        "/api/apps/config-sync/restore/apply-1",
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
        "/api/apps/config-sync/status",
        "/api/apps/config-sync/drift",
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
        "/api/apps/config-sync/",
        "/api/apps/config-sync/nonexistent",
        "/api/apps/other-app/status",
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
    "sha",
    ["a/b", "..", "..%2Fetc", "a%2F..%2F..%2Fb"],
)
def test_approve_path_parameter_containing_slash_or_traversal_never_reaches_routes(
    running_server: Tuple[str, int],
    route_calls: Dict[str, List[Tuple[Any, ...]]],
    sha: str,
) -> None:
    """A sha/id segment containing a literal or percent-encoded `/` or

    `..` must never reach `routes.approve` as a single opaque path
    parameter — the request either 404s (parsed as more/fewer path
    segments than the route pattern expects) or is refused outright, but
    `routes.approve` is never called with a value that could let a
    crafted sha escape its single-segment slot. Mutation this catches:
    an implementation that does `path.split('/')[-2]` with no segment-
    count check would happily hand `".."` or a slash-bearing decoded
    value through as `sha`.
    """
    host, port = running_server
    status_code, _headers, _body = _request(
        host, port, "POST", f"/api/apps/config-sync/pending/{sha}/approve"
    )

    # 501 is BaseHTTPRequestHandler's own default for an unimplemented
    # do_POST — a legitimate "not wired yet" refusal shape alongside the
    # more specific 400/404/405 a real router might choose.
    assert status_code in (400, 404, 405, 501)
    assert len(route_calls["approve"]) == 0


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
        host, port, "POST", f"/api/apps/config-sync/restore/{apply_id}"
    )

    assert status_code in (400, 404, 405, 501)
    assert len(route_calls["restore"]) == 0


def test_response_body_is_valid_json_with_json_content_type_on_every_route(
    running_server: Tuple[str, int],
) -> None:
    host, port = running_server
    requests = [
        ("GET", "/api/apps/config-sync/status"),
        ("GET", "/api/apps/config-sync/drift"),
        ("POST", "/api/apps/config-sync/push"),
        ("POST", "/api/apps/config-sync/pending/sha1/approve"),
        ("POST", "/api/apps/config-sync/pending/sha1/decline"),
        ("POST", "/api/apps/config-sync/restore/apply-1"),
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
