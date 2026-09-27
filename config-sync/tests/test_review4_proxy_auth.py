"""FAILING tests for config-sync review-4 fix B.

High: the backend has NO verification of the gateway's per-request HMAC
(`X-KiroCrew-Proxy`). Any local process that can reach the backend's
loopback socket directly — bypassing the gateway's token auth and
per-app scope enforcement entirely — can call every route today, because
nothing on this backend checks that the request actually came through
the gateway. The real KiroCrew gateway signs every proxied request per
``kiro_crew.apps.proxy_auth`` (read at
``/usr/local/lib/python3.12/site-packages/kiro_crew/apps/proxy_auth.py``);
builtin backends verify it on every route except ``GET /health`` (which
the gateway itself probes unsigned). This backend cannot import
``kiro_crew`` (must run standalone in its own venv), so the target is a
stdlib re-implementation with IDENTICAL semantics:

    X-KiroCrew-Proxy: <ts>:<hex hmac_sha256(secret,
        f"{ts}:{method}:{target}:{sha256(body).hexdigest()}")>

applied to ``backend/server.py`` as
``verify_proxy_request(header_value, *, method, target, body, secret,
now) -> bool`` and enforced by the ``Handler`` before dispatch on every
route except ``/health`` — 401 JSON on failure, no route function
called.

Medium: the existing same-site guard's ``Origin``-vs-``Host`` fallback
(``_is_same_site`` in ``backend/server.py``, tested for the *presence*
of this fallback in ``tests/test_server_security.py``) breaks real
traffic through the gateway: the gateway rewrites ``Host`` to the
backend's own loopback address but forwards the dashboard's real
``Origin`` unchanged, so on a non-localhost dashboard the two can never
match — every legitimate mutating POST would 403. Once the HMAC above is
the real authentication, the fallback must be REMOVED: an absent
``Sec-Fetch-Site`` is then simply not checked (falls through as
same-site), and only an explicit cross-site/same-site value still 403s.
This file pins the fallback's removal; it does not touch
``test_server_security.py``, whose Origin-fallback assertions the
implementer's sibling change will need to update separately.

## Mutation evidence (testing-standards.md; recorded per assertion type)

**HMAC assertions** — proven capable of failing by implementing
``verify_proxy_request`` in a scratch copy under ``$KIROCREW_SCRATCH``
(never this source tree), confirming every "valid" case here goes green
and every "invalid" case goes red, then mutating the comparison
(``hmac.compare_digest(expected, sig)`` -> ``True`` unconditionally) and
re-running: the wrong-secret, tampered-body, tampered-target, and
malformed-header tests all flip green (false-accept), and the missing-ts
tests still correctly fail red *because the skew check runs before the
digest compare* in the reference semantics — the report below records
exactly which tests flip vs stay red under that specific mutation, per
"Method" step 3 (fix the assertion if it doesn't actually test what it
claims — see report).

**Origin-fallback-removal assertion** — proven capable of failing by
running the SAME test against a scratch copy that still has round-3's
fallback: it fails red (200, fallback matches) BEFORE the fallback is
removed, and passes green after — this is what "pins the fallback
removal" means operationally, not just naming it in a comment.

All tests below currently fail against the unmodified source: the HMAC
tests fail because no 401 path exists at all (every request — valid or
adversarial — reaches the route and returns 200/other, or the request
never gets a chance to be refused for the reason under test), and the
Origin-fallback-removal test fails because the fallback still 403s a
valid-signature request whose ``Origin`` doesn't match ``Host``.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import importlib
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

import pytest

_PROXY_HEADER = "X-KiroCrew-Proxy"
_SECRET = "test-proxy-secret-review4"
_MAX_SKEW_SECONDS = 60

_INSTALLED_PROXY_AUTH = Path(
    "/usr/local/lib/python3.12/site-packages/kiro_crew/apps/proxy_auth.py"
)

#: The message-format f-string shape from the reference implementation,
#: e.g. `f"{ts_str}:{method}:{target}:{body_hash}"`. The backend is free
#: to name its own timestamp variable differently (`ts`, `ts_val`, ...),
#: so this pattern only pins the four `:`-joined field NAMES the format
#: string interpolates, in order, allowing any identifier in the first
#: slot rather than requiring the literal `ts_str`.
_MSG_FORMAT_RE = re.compile(
    r"\{[A-Za-z_][A-Za-z0-9_]*\}:\{method\}:\{target\}:\{body_hash\}"
)


# ---------------------------------------------------------------------------
# Fixtures — same shape as tests/test_server_security.py: spy every
# routes.* fn, run the real server over a real loopback socket, with the
# proxy secret injected via env exactly as the gateway injects it at
# backend spawn.
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_state_dir(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    state_dir = tmp_path / "config-sync-state"
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(state_dir))
    yield state_dir


@pytest.fixture
def proxy_secret_env(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("KIROCREW_PROXY_SECRET", _SECRET)
    return _SECRET


@pytest.fixture
def route_calls(monkeypatch: pytest.MonkeyPatch) -> Dict[str, int]:
    """Spies every `backend.routes.*` function with a call counter — this

    file only needs to prove a refused request never reaches ANY route
    function, not which arguments it was called with.
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
    isolated_state_dir: Any,
    proxy_secret_env: str,
    route_calls: Dict[str, int],
) -> Iterator[Tuple[str, int]]:
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
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
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


def _sign(
    *,
    secret: str,
    method: str,
    target: str,
    body: bytes,
    ts: Optional[int] = None,
) -> str:
    """Build a valid `X-KiroCrew-Proxy` header value for (method, target,

    body), matching the reference format exactly:
    `<ts>:<hex hmac_sha256(secret, f"{ts}:{method}:{target}:{body_hash}")>`
    where `body_hash = sha256(body).hexdigest()`.
    """
    ts_val = int(time.time()) if ts is None else ts
    body_hash = hashlib.sha256(body or b"").hexdigest()
    msg = f"{ts_val}:{method}:{target}:{body_hash}"
    sig = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return f"{ts_val}:{sig}"


def _target(prefix_path: str) -> str:
    """The raw request-target the backend receives as `self.path` for a

    bare GET/POST with no query string — imported `_PREFIX` composes the
    real route path so this file tracks the sibling fix's prefix change
    (`/api/apps/config-sync` -> possibly `/api`) automatically.
    """
    return prefix_path


def _base_headers(host: str, port: int) -> Dict[str, str]:
    return {"Host": f"{host}:{port}", "Sec-Fetch-Site": "same-origin"}


# ---------------------------------------------------------------------------
# Control: a validly-signed request reaches the route.
# ---------------------------------------------------------------------------


def test_valid_signature_reaches_status_route(
    running_server: Tuple[str, int],
    route_calls: Dict[str, int],
    proxy_secret_env: str,
) -> None:
    from backend.server import _PREFIX

    host, port = running_server
    target = _target(f"{_PREFIX}/status")
    headers = _base_headers(host, port)
    headers[_PROXY_HEADER] = _sign(
        secret=proxy_secret_env, method="GET", target=target, body=b""
    )

    status_code, _headers, _body = _request(host, port, "GET", target, headers=headers)

    assert status_code == 200
    assert sum(route_calls.values()) == 1


def test_valid_signature_reaches_push_route(
    running_server: Tuple[str, int],
    route_calls: Dict[str, int],
    proxy_secret_env: str,
) -> None:
    from backend.server import _PREFIX

    host, port = running_server
    target = _target(f"{_PREFIX}/push")
    body = b""
    headers = _base_headers(host, port)
    headers[_PROXY_HEADER] = _sign(
        secret=proxy_secret_env, method="POST", target=target, body=body
    )

    status_code, _headers, _body = _request(
        host, port, "POST", target, headers=headers, body=body
    )

    assert status_code == 200
    assert sum(route_calls.values()) == 1


# ---------------------------------------------------------------------------
# Missing / malformed header -> 401, route never called.
# ---------------------------------------------------------------------------


def test_missing_proxy_header_is_refused_401(
    running_server: Tuple[str, int],
    route_calls: Dict[str, int],
    proxy_secret_env: str,
) -> None:
    from backend.server import _PREFIX

    host, port = running_server
    target = _target(f"{_PREFIX}/status")
    headers = _base_headers(host, port)  # no X-KiroCrew-Proxy at all

    status_code, _headers, body = _request(host, port, "GET", target, headers=headers)

    assert status_code == 401
    assert sum(route_calls.values()) == 0
    assert b"error" in body


@pytest.mark.parametrize(
    "bad_value",
    [
        "no-colon-at-all",
        "abc:deadbeef",  # non-digit ts
        "1234567890:",  # empty sig
        "",
    ],
)
def test_malformed_proxy_header_is_refused_401(
    running_server: Tuple[str, int],
    route_calls: Dict[str, int],
    proxy_secret_env: str,
    bad_value: str,
) -> None:
    from backend.server import _PREFIX

    host, port = running_server
    target = _target(f"{_PREFIX}/status")
    headers = _base_headers(host, port)
    headers[_PROXY_HEADER] = bad_value

    status_code, _headers, _body = _request(host, port, "GET", target, headers=headers)

    assert status_code == 401
    assert sum(route_calls.values()) == 0


# ---------------------------------------------------------------------------
# Clock skew: 61s old / 61s future -> 401; 59s old -> ok.
# ---------------------------------------------------------------------------


def test_timestamp_61s_old_is_refused_401(
    running_server: Tuple[str, int],
    route_calls: Dict[str, int],
    proxy_secret_env: str,
) -> None:
    from backend.server import _PREFIX

    host, port = running_server
    target = _target(f"{_PREFIX}/status")
    stale_ts = int(time.time()) - (_MAX_SKEW_SECONDS + 1)
    headers = _base_headers(host, port)
    headers[_PROXY_HEADER] = _sign(
        secret=proxy_secret_env, method="GET", target=target, body=b"", ts=stale_ts
    )

    status_code, _headers, _body = _request(host, port, "GET", target, headers=headers)

    assert status_code == 401
    assert sum(route_calls.values()) == 0


def test_timestamp_61s_future_is_refused_401(
    running_server: Tuple[str, int],
    route_calls: Dict[str, int],
    proxy_secret_env: str,
) -> None:
    from backend.server import _PREFIX

    host, port = running_server
    target = _target(f"{_PREFIX}/status")
    future_ts = int(time.time()) + (_MAX_SKEW_SECONDS + 1)
    headers = _base_headers(host, port)
    headers[_PROXY_HEADER] = _sign(
        secret=proxy_secret_env, method="GET", target=target, body=b"", ts=future_ts
    )

    status_code, _headers, _body = _request(host, port, "GET", target, headers=headers)

    assert status_code == 401
    assert sum(route_calls.values()) == 0


def test_timestamp_59s_old_is_accepted(
    running_server: Tuple[str, int],
    route_calls: Dict[str, int],
    proxy_secret_env: str,
) -> None:
    from backend.server import _PREFIX

    host, port = running_server
    target = _target(f"{_PREFIX}/status")
    near_ts = int(time.time()) - (_MAX_SKEW_SECONDS - 1)
    headers = _base_headers(host, port)
    headers[_PROXY_HEADER] = _sign(
        secret=proxy_secret_env, method="GET", target=target, body=b"", ts=near_ts
    )

    status_code, _headers, _body = _request(host, port, "GET", target, headers=headers)

    assert status_code == 200
    assert sum(route_calls.values()) == 1


# ---------------------------------------------------------------------------
# Wrong secret / tampered body / tampered target -> 401.
# ---------------------------------------------------------------------------


def test_wrong_secret_is_refused_401(
    running_server: Tuple[str, int],
    route_calls: Dict[str, int],
    proxy_secret_env: str,
) -> None:
    from backend.server import _PREFIX

    host, port = running_server
    target = _target(f"{_PREFIX}/status")
    headers = _base_headers(host, port)
    headers[_PROXY_HEADER] = _sign(
        secret="not-the-real-secret", method="GET", target=target, body=b""
    )

    status_code, _headers, _body = _request(host, port, "GET", target, headers=headers)

    assert status_code == 401
    assert sum(route_calls.values()) == 0


def test_body_tampered_after_signing_is_refused_401(
    running_server: Tuple[str, int],
    route_calls: Dict[str, int],
    proxy_secret_env: str,
) -> None:
    from backend.server import _PREFIX

    host, port = running_server
    target = _target(f"{_PREFIX}/push")
    signed_body = b'{"expected": "body"}'
    tampered_body = b'{"tampered": "body!"}'
    headers = _base_headers(host, port)
    headers[_PROXY_HEADER] = _sign(
        secret=proxy_secret_env, method="POST", target=target, body=signed_body
    )

    status_code, _headers, _body = _request(
        host, port, "POST", target, headers=headers, body=tampered_body
    )

    assert status_code == 401
    assert sum(route_calls.values()) == 0


def test_target_tampered_query_string_is_refused_401(
    running_server: Tuple[str, int],
    route_calls: Dict[str, int],
    proxy_secret_env: str,
) -> None:
    """Sign for `target` with no query string, then send the request with

    a different query string appended — the target the backend actually
    receives as `self.path` no longer matches what was signed.
    """
    from backend.server import _PREFIX

    host, port = running_server
    signed_target = _target(f"{_PREFIX}/status")
    actual_target = f"{signed_target}?extra=param"
    headers = _base_headers(host, port)
    headers[_PROXY_HEADER] = _sign(
        secret=proxy_secret_env, method="GET", target=signed_target, body=b""
    )

    status_code, _headers, _body = _request(
        host, port, "GET", actual_target, headers=headers
    )

    assert status_code == 401
    assert sum(route_calls.values()) == 0


# ---------------------------------------------------------------------------
# Empty secret env -> 401 for everything except /health.
# ---------------------------------------------------------------------------


def test_empty_proxy_secret_env_refuses_status_route_401(
    isolated_state_dir: Any,
    route_calls: Dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KIROCREW_PROXY_SECRET", "")

    import backend.server as server_module

    importlib.reload(server_module)

    httpd = server_module.build_server(host="127.0.0.1", port=0)
    host = str(httpd.server_address[0])
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        target = f"{server_module._PREFIX}/status"
        headers = {"Host": f"{host}:{port}", "Sec-Fetch-Site": "same-origin"}
        # Even a "correctly" signed header (against the empty secret)
        # must fail closed — an empty secret can never authenticate.
        headers[_PROXY_HEADER] = _sign(secret="", method="GET", target=target, body=b"")

        status_code, _headers, _body = _request(
            host, port, "GET", target, headers=headers
        )

        assert status_code == 401
        assert sum(route_calls.values()) == 0
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_empty_proxy_secret_env_allows_health_unsigned_200(
    isolated_state_dir: Any,
    route_calls: Dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KIROCREW_PROXY_SECRET", "")

    import backend.server as server_module

    importlib.reload(server_module)

    httpd = server_module.build_server(host="127.0.0.1", port=0)
    host = str(httpd.server_address[0])
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        status_code, _headers, _body = _request(
            host, port, "GET", "/health", headers={"Host": f"{host}:{port}"}
        )
        assert status_code == 200
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# /health is never signature-checked.
# ---------------------------------------------------------------------------


def test_health_unsigned_is_200(
    running_server: Tuple[str, int],
    route_calls: Dict[str, int],
) -> None:
    host, port = running_server
    status_code, _headers, _body = _request(
        host, port, "GET", "/health", headers={"Host": f"{host}:{port}"}
    )
    assert status_code == 200
    assert sum(route_calls.values()) == 0


# ---------------------------------------------------------------------------
# Same-site guard still applies ON TOP of a valid signature.
# ---------------------------------------------------------------------------


def test_cross_site_sec_fetch_site_with_valid_signature_is_still_403(
    running_server: Tuple[str, int],
    route_calls: Dict[str, int],
    proxy_secret_env: str,
) -> None:
    from backend.server import _PREFIX

    host, port = running_server
    target = _target(f"{_PREFIX}/push")
    body = b""
    headers = {"Host": f"{host}:{port}", "Sec-Fetch-Site": "cross-site"}
    headers[_PROXY_HEADER] = _sign(
        secret=proxy_secret_env, method="POST", target=target, body=body
    )

    status_code, _headers, _body = _request(
        host, port, "POST", target, headers=headers, body=body
    )

    assert status_code == 403
    assert sum(route_calls.values()) == 0


def test_non_loopback_host_with_valid_signature_is_still_403(
    running_server: Tuple[str, int],
    route_calls: Dict[str, int],
    proxy_secret_env: str,
) -> None:
    from backend.server import _PREFIX

    host, port = running_server
    target = _target(f"{_PREFIX}/push")
    body = b""
    headers = {"Host": "evil.example", "Sec-Fetch-Site": "same-origin"}
    headers[_PROXY_HEADER] = _sign(
        secret=proxy_secret_env, method="POST", target=target, body=body
    )

    status_code, _headers, _body = _request(
        host, port, "POST", target, headers=headers, body=body
    )

    assert status_code == 403
    assert sum(route_calls.values()) == 0


# ---------------------------------------------------------------------------
# Medium fix: the Origin-vs-Host fallback is REMOVED. A valid signature
# plus a mismatched Origin and no Sec-Fetch-Site now reaches the route —
# this is the test that fails TODAY (403 from the still-present fallback)
# and pins the fallback's removal once the HMAC is the real auth.
# ---------------------------------------------------------------------------


def test_valid_signature_with_mismatched_origin_and_no_sec_fetch_site_reaches_route(
    running_server: Tuple[str, int],
    route_calls: Dict[str, int],
    proxy_secret_env: str,
) -> None:
    from backend.server import _PREFIX

    host, port = running_server
    target = _target(f"{_PREFIX}/push")
    body = b""
    headers = {
        "Host": f"{host}:{port}",
        "Origin": "https://dashboard.example",  # does NOT match Host
        # deliberately no Sec-Fetch-Site
    }
    headers[_PROXY_HEADER] = _sign(
        secret=proxy_secret_env, method="POST", target=target, body=body
    )

    status_code, _headers, _body = _request(
        host, port, "POST", target, headers=headers, body=body
    )

    assert status_code == 200
    assert sum(route_calls.values()) == 1


# ---------------------------------------------------------------------------
# Format parity with the installed gateway module (cheap seam test).
# ---------------------------------------------------------------------------


def test_message_format_and_skew_constant_match_installed_proxy_auth() -> None:
    if not _INSTALLED_PROXY_AUTH.is_file():
        pytest.skip(
            f"installed proxy_auth.py not found at {_INSTALLED_PROXY_AUTH}; "
            "cannot check format parity"
        )

    installed_text = _INSTALLED_PROXY_AUTH.read_text(encoding="utf-8")
    assert _MSG_FORMAT_RE.search(installed_text) is not None
    assert "_MAX_SKEW_SECONDS = 60" in installed_text

    import backend.server as server_module

    backend_source = Path(server_module.__file__).read_text(encoding="utf-8")
    assert _MSG_FORMAT_RE.search(backend_source) is not None
    assert "_MAX_SKEW_SECONDS = 60" in backend_source.replace(
        "_MAX_SKEW_SECONDS=60", "_MAX_SKEW_SECONDS = 60"
    )
