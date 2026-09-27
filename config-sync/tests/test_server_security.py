"""FAILING tests for senior-review H7: `backend/server.py`'s mutating

POST routes (`push`, `restore/{id}`) have no CSRF/Host protection at all.
Because the server binds loopback-only (`127.0.0.1`) but is a plain,
unauthenticated HTTP server, ANY web page open in a browser on the same
machine can POST to it via `fetch`/a form submit — the loopback bind
stops a remote attacker, not a malicious page the operator merely has
open in a tab.

**Operator ruling (requirements.md Introduction; Requirement 4.4, 4.6
[Reserved]): there is no `pending/{sha}/approve`/`.../decline` route —
the poll tick applies automatically with no box-side decision step.
`push` and `restore` remain as the two mutation-capable POST targets
this guard is proven against.**

## Pinned interfaces this file requires `backend/server.py` to implement

1. **Required custom header.** Every mutating request MUST carry
   ``X-Config-Sync-Request: 1``. A simple cross-origin form submission
   or `fetch` with `mode: "no-cors"` cannot set a custom header, so
   requiring one is a real CSRF barrier — not merely checked-and-ignored.
   Missing or wrong value -> ``403``, and the request MUST NOT reach
   `backend.routes.*` (proven via a spy).
2. **Loopback-only `Host` header.** A request whose `Host` header is not
   a loopback name/address (``127.0.0.1``, ``localhost``, ``[::1]``, each
   optionally with a port) is refused with ``403`` — this is DNS
   rebinding protection: a page on `evil.example` can still cause the
   VICTIM'S browser to send a request to `127.0.0.1`, but it cannot make
   the browser send a request with a `Host` header of anything but
   `evil.example` unless it already resolved to loopback, so pinning
   `Host` catches the rebinding case the loopback bind alone does not.
3. **GET routes are unaffected.** Neither check applies to `GET status`
   or `GET drift` — this is a mutation guard, not a blanket auth layer
   the design does not otherwise call for.
4. **Cross-origin `Origin` + form content-type is still caught by the
   same header check.** A cross-origin `fetch` sending
   `Content-Type: application/x-www-form-urlencoded` (a "simple request"
   that skips CORS preflight) still lacks the required custom header —
   proving the guard does not special-case content-type.

This file does not prescribe HOW the check is implemented inside
`Handler._dispatch` (or wherever) — only the request/response contract
above. Every assertion is a MUTATION: each test shows the guard actually
turning red under a violating input (a missing header, a bad Host), per
`testing-standards.md`'s mutation requirement for security assertions.
"""

from __future__ import annotations

import http.client
import threading
import time
from typing import Any, Dict, Iterator, List, Tuple

import pytest

_REQUIRED_HEADER = "X-Config-Sync-Request"
_REQUIRED_HEADER_VALUE = "1"

_MUTATING_REQUESTS: List[Tuple[str, str]] = [
    ("POST", "/api/apps/config-sync/push"),
    ("POST", "/api/apps/config-sync/restore/apply-1"),
]

_GET_REQUESTS: List[Tuple[str, str]] = [
    ("GET", "/api/apps/config-sync/status"),
    ("GET", "/api/apps/config-sync/drift"),
]


# ---------------------------------------------------------------------------
# Fixtures — same shape as tests/test_server.py: spy every routes.* fn,
# run the real server over a real loopback socket.
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_state_dir(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    state_dir = tmp_path / "config-sync-state"
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(state_dir))
    yield state_dir


@pytest.fixture
def route_calls(monkeypatch: pytest.MonkeyPatch) -> Dict[str, int]:
    """Spies every `backend.routes.*` function with a call counter — this

    file only needs to prove a blocked request never reaches ANY route
    function, not which arguments it was called with (`tests/test_server.py`
    already covers wiring/argument-passing).
    """
    from backend import routes as routes_module

    calls: Dict[str, int] = {
        "status": 0,
        "drift": 0,
        "push_now": 0,
        "restore": 0,
    }

    def _make(name: str) -> Any:
        def _spy(store: Any, *args: Any) -> Dict[str, Any]:
            calls[name] += 1
            return {"status": "ok", "spy": name}

        return _spy

    for name in calls:
        monkeypatch.setattr(routes_module, name, _make(name), raising=False)

    return calls


@pytest.fixture
def running_server(
    isolated_state_dir: Any, route_calls: Dict[str, int]
) -> Iterator[Tuple[str, int]]:
    import importlib

    import backend.server as server_module

    importlib.reload(server_module)

    httpd = server_module.build_server(host="127.0.0.1", port=0)
    host = str(httpd.server_address[0])
    port = httpd.server_address[1]

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5.0
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                conn = http.client.HTTPConnection(host, port, timeout=1)
                conn.request("GET", "/health", headers={"Host": f"{host}:{port}"})
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


def _request(
    host: str,
    port: int,
    method: str,
    path: str,
    headers: Dict[str, str] | None = None,
    body: bytes | None = None,
) -> Tuple[int, Dict[str, str], bytes]:
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        payload = resp.read()
        response_headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, response_headers, payload
    finally:
        conn.close()


def _valid_headers(host: str, port: int) -> Dict[str, str]:
    """The full set of headers a LEGITIMATE same-origin request carries —

    correct custom header, loopback Host. Used as the control in every
    mutation test: flip exactly one field off this baseline per test.
    """
    return {
        "Host": f"{host}:{port}",
        _REQUIRED_HEADER: _REQUIRED_HEADER_VALUE,
    }


# ---------------------------------------------------------------------------
# Control: a fully-valid request DOES reach the route (proves the guard
# isn't just refusing everything).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,path", _MUTATING_REQUESTS)
def test_valid_header_and_loopback_host_reaches_the_route(
    running_server: Tuple[str, int],
    route_calls: Dict[str, int],
    method: str,
    path: str,
) -> None:
    host, port = running_server
    status_code, _headers, _body = _request(
        host, port, method, path, headers=_valid_headers(host, port)
    )
    assert status_code == 200
    assert sum(route_calls.values()) == 1


# ---------------------------------------------------------------------------
# H7.a — missing/wrong custom header -> 403, route never called
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,path", _MUTATING_REQUESTS)
def test_mutating_request_missing_required_header_is_refused_403(
    running_server: Tuple[str, int],
    route_calls: Dict[str, int],
    method: str,
    path: str,
) -> None:
    """Mutation: take the valid-header baseline and remove ONLY the

    custom header (Host stays loopback-valid) -> must flip from 200/ok
    to 403, and the route spy must never fire. This is exactly the shape
    of request a cross-origin `<form>` submit or a simple `fetch` can
    send with no custom header at all.
    """
    host, port = running_server
    headers = {"Host": f"{host}:{port}"}  # no X-Config-Sync-Request

    status_code, _headers, _body = _request(host, port, method, path, headers=headers)

    assert status_code == 403
    assert sum(route_calls.values()) == 0


@pytest.mark.parametrize("method,path", _MUTATING_REQUESTS)
def test_mutating_request_with_wrong_header_value_is_refused_403(
    running_server: Tuple[str, int],
    route_calls: Dict[str, int],
    method: str,
    path: str,
) -> None:
    host, port = running_server
    headers = {"Host": f"{host}:{port}", _REQUIRED_HEADER: "0"}

    status_code, _headers, _body = _request(host, port, method, path, headers=headers)

    assert status_code == 403
    assert sum(route_calls.values()) == 0


# ---------------------------------------------------------------------------
# H7.b — non-loopback Host -> 403, route never called (DNS-rebinding guard)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,path", _MUTATING_REQUESTS)
@pytest.mark.parametrize(
    "bad_host",
    ["evil.example", "evil.example:9100", "169.254.169.254", "0.0.0.0"],
)
def test_mutating_request_with_non_loopback_host_is_refused_403(
    running_server: Tuple[str, int],
    route_calls: Dict[str, int],
    method: str,
    path: str,
    bad_host: str,
) -> None:
    """Mutation: take the valid-header baseline and flip ONLY the `Host`

    header to a non-loopback value, keeping the correct custom header ->
    must flip from 200/ok to 403. This is the DNS-rebinding case: a
    victim's browser can be made to send a request to `127.0.0.1` while
    the attacker's page believes it is talking to `evil.example`, but the
    `Host` header the browser actually sends reflects the origin the page
    thinks it's on unless rebinding has already redirected the hostname
    itself to loopback — pinning `Host` here catches that.
    """
    host, port = running_server
    headers = {"Host": bad_host, _REQUIRED_HEADER: _REQUIRED_HEADER_VALUE}

    status_code, _headers, _body = _request(host, port, method, path, headers=headers)

    assert status_code == 403
    assert sum(route_calls.values()) == 0


# ---------------------------------------------------------------------------
# H7.c — GET routes are unaffected by either check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,path", _GET_REQUESTS)
def test_get_route_succeeds_with_no_custom_header(
    running_server: Tuple[str, int],
    route_calls: Dict[str, int],
    method: str,
    path: str,
) -> None:
    host, port = running_server
    headers = {"Host": f"{host}:{port}"}  # no X-Config-Sync-Request

    status_code, _headers, _body = _request(host, port, method, path, headers=headers)

    assert status_code == 200
    assert sum(route_calls.values()) == 1


@pytest.mark.parametrize("method,path", _GET_REQUESTS)
def test_get_route_succeeds_even_with_non_loopback_host(
    running_server: Tuple[str, int],
    route_calls: Dict[str, int],
    method: str,
    path: str,
) -> None:
    """Pinning the mutation guard to POST-only per the finding's own

    framing ("any web page can POST"): a GET carries no side effect, so
    this file does not require the Host check to apply to it. If a future
    change extends Host-pinning to GET as well this test's expectation
    changes with it — it exists to pin TODAY's scope, not to forbid a
    stricter future.
    """
    host, port = running_server
    headers = {"Host": "evil.example"}

    status_code, _headers, _body = _request(host, port, method, path, headers=headers)

    assert status_code == 200
    assert sum(route_calls.values()) == 1


# ---------------------------------------------------------------------------
# H7.d — cross-origin Origin + form-style content-type is still caught
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,path", _MUTATING_REQUESTS)
def test_cross_origin_form_style_request_without_header_is_still_refused(
    running_server: Tuple[str, int],
    route_calls: Dict[str, int],
    method: str,
    path: str,
) -> None:
    """A cross-origin `fetch`/`<form>` POST using

    `Content-Type: application/x-www-form-urlencoded` and an `Origin`
    header is exactly the "simple request" shape that skips CORS
    preflight entirely — the browser sends it with no custom header
    unconditionally, so the guard must still refuse it on the missing
    header alone, with no special-casing of `Origin` or content-type
    letting it through.
    """
    host, port = running_server
    headers = {
        "Host": f"{host}:{port}",
        "Origin": "https://evil.example",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    status_code, _headers, _body = _request(host, port, method, path, headers=headers)

    assert status_code == 403
    assert sum(route_calls.values()) == 0
