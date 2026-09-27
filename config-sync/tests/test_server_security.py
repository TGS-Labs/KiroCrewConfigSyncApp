"""FAILING/mutation tests for senior-review H7: `backend/server.py`'s mutating

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

## Redesigned guard (round 3 C-B; round 4 fix B)

The original H7 design required a custom header
(``X-Config-Sync-Request: 1``) on every mutating request. Reading the
REAL ``@kirocrew/app-sdk`` host implementation showed its ``post()`` has
no headers parameter at all, so that rule is superseded. Round 4 made
the gateway HMAC (``X-KiroCrew-Proxy``, see
``tests/test_review4_proxy_auth.py``) the authentication on every
non-health route and REMOVED round 3's ``Origin``-vs-``Host`` fallback:
the gateway rewrites ``Host`` to the backend's loopback address but
forwards the dashboard's ``Origin`` unchanged, so that fallback 403'd
every legitimate mutation on a non-localhost dashboard. Every request
this file sends is validly signed unless a test says otherwise, so each
assertion isolates the same-site/Host guard layered on top.

## Pinned interfaces this file requires `backend/server.py` to implement

1. **Same-site check.** A mutating request carrying ``Sec-Fetch-Site``
   must carry ``same-origin``/``none``; any other value is refused with
   ``403``. An absent ``Sec-Fetch-Site`` is not refused, whatever
   ``Origin`` says — there is no ``Origin`` fallback.
2. **Loopback-only `Host` header.** A request whose `Host` header is not
   a loopback name/address (``127.0.0.1``, ``localhost``, ``[::1]``, each
   optionally with a port) is refused with ``403`` — this is DNS
   rebinding protection: a page on `evil.example` can still cause the
   VICTIM'S browser to send a request to `127.0.0.1`, but it cannot make
   the browser send a request with a `Host` header of anything but
   `evil.example` unless it already resolved to loopback, so pinning
   `Host` catches the rebinding case the loopback bind alone does not.
3. **GET routes are unaffected by checks 1-2** (they still require the
   gateway signature).
4. **Cross-origin `Origin` + form content-type is still caught.** A
   cross-origin `fetch` sending
   `Content-Type: application/x-www-form-urlencoded` (a "simple request"
   that skips CORS preflight) with a real browser's `Sec-Fetch-Site:
   cross-site` is still refused — proving the guard does not
   special-case content-type.
5. **An unsigned request never reaches a route**, whatever its
   fetch-metadata headers — a local process bypassing the gateway gets
   ``401``.

This file does not prescribe HOW the check is implemented inside
`Handler._dispatch` (or wherever) — only the request/response contract
above. Every assertion is a MUTATION: each test shows the guard actually
turning red under a violating input (a missing/cross-site fetch-metadata
header, a bad Host), per `testing-standards.md`'s mutation requirement
for security assertions.
"""

from __future__ import annotations

import http.client
import threading
import time
from typing import Any, Dict, Iterator, List, Tuple

import pytest

from backend.server import _PREFIX
from proxy_sign import PROXY_SECRET_ENV, TEST_PROXY_SECRET, signed_headers

_MUTATING_REQUESTS: List[Tuple[str, str]] = [
    ("POST", f"{_PREFIX}/push"),
    ("POST", f"{_PREFIX}/restore/apply-1"),
]

_GET_REQUESTS: List[Tuple[str, str]] = [
    ("GET", f"{_PREFIX}/status"),
    ("GET", f"{_PREFIX}/drift"),
]


# ---------------------------------------------------------------------------
# Fixtures — same shape as tests/test_server.py: spy every routes.* fn,
# run the real server over a real loopback socket.
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_state_dir(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    state_dir = tmp_path / "config-sync-state"
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setenv(PROXY_SECRET_ENV, TEST_PROXY_SECRET)
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
    *,
    signed: bool = True,
) -> Tuple[int, Dict[str, str], bytes]:
    """Send one request; validly gateway-signed unless ``signed=False``."""
    out = dict(headers or {})
    if signed:
        out = signed_headers(method, path, body or b"", out)
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=out)
        resp = conn.getresponse()
        payload = resp.read()
        response_headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, response_headers, payload
    finally:
        conn.close()


def _valid_headers(host: str, port: int) -> Dict[str, str]:
    """The full set of headers a LEGITIMATE same-origin request carries —

    loopback Host, `Sec-Fetch-Site: same-origin` (what a real browser
    sends for the app's own UI calling its own backend). Used as the
    control in every mutation test: flip exactly one field off this
    baseline per test.
    """
    return {
        "Host": f"{host}:{port}",
        "Sec-Fetch-Site": "same-origin",
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
# H7.a (redesigned) — cross-site Sec-Fetch-Site -> 403, route never called
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,path", _MUTATING_REQUESTS)
def test_mutating_request_with_cross_site_sec_fetch_site_is_refused_403(
    running_server: Tuple[str, int],
    route_calls: Dict[str, int],
    method: str,
    path: str,
) -> None:
    """Mutation: take the valid-header baseline and change ONLY

    `Sec-Fetch-Site` from `same-origin` to `cross-site` (Host stays
    loopback-valid) -> must flip from 200/ok to 403, and the route spy
    must never fire. `Sec-Fetch-Site` is a forbidden header name a
    malicious page's JavaScript cannot set or override, so a real
    cross-site browser `fetch`/`<form>` submit always carries this exact
    value.
    """
    host, port = running_server
    headers = {"Host": f"{host}:{port}", "Sec-Fetch-Site": "cross-site"}

    status_code, _headers, _body = _request(host, port, method, path, headers=headers)

    assert status_code == 403
    assert sum(route_calls.values()) == 0


@pytest.mark.parametrize("method,path", _MUTATING_REQUESTS)
def test_mutating_request_with_same_site_sec_fetch_site_is_refused_403(
    running_server: Tuple[str, int],
    route_calls: Dict[str, int],
    method: str,
    path: str,
) -> None:
    """`Sec-Fetch-Site: same-site` (a different-but-related site, e.g. a

    sibling subdomain) is also refused — nothing legitimately calls this
    backend from another site at all, only from its own same-origin UI.
    """
    host, port = running_server
    headers = {"Host": f"{host}:{port}", "Sec-Fetch-Site": "same-site"}

    status_code, _headers, _body = _request(host, port, method, path, headers=headers)

    assert status_code == 403
    assert sum(route_calls.values()) == 0


@pytest.mark.parametrize("method,path", _MUTATING_REQUESTS)
def test_mutating_request_with_mismatched_origin_and_no_sec_fetch_site_is_allowed(
    running_server: Tuple[str, int],
    route_calls: Dict[str, int],
    method: str,
    path: str,
) -> None:
    """Round 4 fix B: the `Origin`-vs-`Host` fallback is REMOVED. A

    validly-signed request with no `Sec-Fetch-Site` and an `Origin` that
    does not match `Host` — exactly what the gateway forwards from a
    non-localhost dashboard — reaches the route. (Previously 403.)
    """
    host, port = running_server
    headers = {"Host": f"{host}:{port}", "Origin": "https://evil.example"}

    status_code, _headers, _body = _request(host, port, method, path, headers=headers)

    assert status_code == 200
    assert sum(route_calls.values()) == 1


@pytest.mark.parametrize("method,path", _MUTATING_REQUESTS)
def test_mutating_request_with_mismatched_origin_unsigned_is_refused_401(
    running_server: Tuple[str, int],
    route_calls: Dict[str, int],
    method: str,
    path: str,
) -> None:
    """The HMAC, not `Origin`, is what refuses a caller that bypassed the

    gateway: the same headers as above WITHOUT a signature -> 401, route
    never called.
    """
    host, port = running_server
    headers = {"Host": f"{host}:{port}", "Origin": "https://evil.example"}

    status_code, _headers, _body = _request(
        host, port, method, path, headers=headers, signed=False
    )

    assert status_code == 401
    assert sum(route_calls.values()) == 0


@pytest.mark.parametrize("method,path", _MUTATING_REQUESTS)
def test_mutating_request_with_matching_origin_and_no_sec_fetch_site_reaches_the_route(
    running_server: Tuple[str, int],
    route_calls: Dict[str, int],
    method: str,
    path: str,
) -> None:
    """A signed request whose `Origin` matches `Host`, with no

    `Sec-Fetch-Site`, reaches the route (unchanged by the fallback's
    removal).
    """
    host, port = running_server
    headers = {"Host": f"{host}:{port}", "Origin": f"http://{host}:{port}"}

    status_code, _headers, _body = _request(host, port, method, path, headers=headers)

    assert status_code == 200
    assert sum(route_calls.values()) == 1


@pytest.mark.parametrize("method,path", _MUTATING_REQUESTS)
def test_mutating_request_with_neither_fetch_metadata_header_reaches_the_route(
    running_server: Tuple[str, int],
    route_calls: Dict[str, int],
    method: str,
    path: str,
) -> None:
    """A signed request with no `Sec-Fetch-Site` and no `Origin` passes

    the same-site check — the shape of a gateway-forwarded non-browser
    call.
    """
    host, port = running_server
    headers = {"Host": f"{host}:{port}"}

    status_code, _headers, _body = _request(host, port, method, path, headers=headers)

    assert status_code == 200
    assert sum(route_calls.values()) == 1


@pytest.mark.parametrize("method,path", _MUTATING_REQUESTS + _GET_REQUESTS)
def test_unsigned_request_with_neither_fetch_metadata_header_is_refused_401(
    running_server: Tuple[str, int],
    route_calls: Dict[str, int],
    method: str,
    path: str,
) -> None:
    """A bare local-process call straight to the loopback socket (no

    signature, no fetch-metadata) is refused 401 before any route runs —
    loopback reachability is not authentication.
    """
    host, port = running_server
    headers = {"Host": f"{host}:{port}"}

    status_code, _headers, _body = _request(
        host, port, method, path, headers=headers, signed=False
    )

    assert status_code == 401
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

    header to a non-loopback value, keeping `Sec-Fetch-Site: same-origin`
    -> must flip from 200/ok to 403. This is the DNS-rebinding case: a
    victim's browser can be made to send a request to `127.0.0.1` while
    the attacker's page believes it is talking to `evil.example`, but the
    `Host` header the browser actually sends reflects the origin the page
    thinks it's on unless rebinding has already redirected the hostname
    itself to loopback — pinning `Host` here catches that.
    """
    host, port = running_server
    headers = {"Host": bad_host, "Sec-Fetch-Site": "same-origin"}

    status_code, _headers, _body = _request(host, port, method, path, headers=headers)

    assert status_code == 403
    assert sum(route_calls.values()) == 0


# ---------------------------------------------------------------------------
# H7.c — GET routes are unaffected by either check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,path", _GET_REQUESTS)
def test_get_route_succeeds_with_no_fetch_metadata_headers(
    running_server: Tuple[str, int],
    route_calls: Dict[str, int],
    method: str,
    path: str,
) -> None:
    host, port = running_server
    headers = {"Host": f"{host}:{port}"}  # no Sec-Fetch-Site, no Origin

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
    preflight entirely — a real browser still sends
    `Sec-Fetch-Site: cross-site` on it unconditionally (page JS cannot
    suppress or override fetch-metadata headers), so the guard must
    still refuse it, with no special-casing of content-type letting it
    through.
    """
    host, port = running_server
    headers = {
        "Host": f"{host}:{port}",
        "Origin": "https://evil.example",
        "Sec-Fetch-Site": "cross-site",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    status_code, _headers, _body = _request(host, port, method, path, headers=headers)

    assert status_code == 403
    assert sum(route_calls.values()) == 0


# ---------------------------------------------------------------------------
# The signature covers the body, so the body is read (bounded) before the
# signature is checked; an unusable Content-Length is refused outright.
# ---------------------------------------------------------------------------


def _raw_post(host: str, port: int, path: str, content_length: str) -> int:
    """POST with a hand-set `Content-Length` (http.client would compute
    its own from the body) and return the status code."""
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.putrequest("POST", path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", f"{host}:{port}")
        for name, value in signed_headers("POST", path, b"").items():
            conn.putheader(name, value)
        conn.putheader("Content-Length", content_length)
        conn.endheaders()
        resp = conn.getresponse()
        resp.read()
        return resp.status
    finally:
        conn.close()


@pytest.mark.parametrize("content_length", ["abc", "-1", str(2 * 1024 * 1024)])
def test_unusable_content_length_is_refused_400_before_any_route(
    running_server: Tuple[str, int],
    route_calls: Dict[str, int],
    content_length: str,
) -> None:
    host, port = running_server

    status_code = _raw_post(host, port, f"{_PREFIX}/push", content_length)

    assert status_code == 400
    assert sum(route_calls.values()) == 0


def test_signed_request_body_is_read_and_verified(
    running_server: Tuple[str, int],
    route_calls: Dict[str, int],
) -> None:
    """A body the signature covers is accepted; the same request with one
    byte changed is refused 401 — proves the body is actually hashed."""
    host, port = running_server
    path = f"{_PREFIX}/push"
    body = b'{"k": "v"}'
    base = {"Host": f"{host}:{port}", "Sec-Fetch-Site": "same-origin"}

    ok_status, _h, _b = _request(host, port, "POST", path, headers=base, body=body)
    tampered = signed_headers("POST", path, body, base)
    bad_status, _h2, _b2 = _request(
        host, port, "POST", path, headers=tampered, body=b'{"k": "w"}', signed=False
    )

    assert ok_status == 200
    assert bad_status == 401
    assert sum(route_calls.values()) == 1
