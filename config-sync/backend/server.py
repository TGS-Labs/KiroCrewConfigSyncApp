"""Backend HTTP server for config-sync (tasks.md 6.2's "HTTP wiring";

design.md's routes table) — a KiroCrew app.

Run with: python backend/server.py
Or let KiroCrew manage it via the app manifest backend section.

Dispatches each design-table verb/path pair to its matching
``backend.routes.*`` function, passing a fresh ``state.StateStore`` built
per request (see ``_get_store`` below) — the route functions themselves
hold no HTTP knowledge (``backend/routes.py``'s own docstring: they are
"plain, framework-agnostic functions"). This module owns exactly:
verb/path matching, path-parameter extraction, gateway-signature
verification, the mutation-request guard, JSON response shape, and the
loopback-only bind — never route BEHAVIOUR, which is
`backend.routes`'/`backend.apply`'s job and is tested there.

A path parameter (``sha``/``apply_id``) is extracted only when the
request path has EXACTLY the expected number of ``/``-separated
segments for that route — never by a permissive ``rsplit``/regex that
would let a literal or percent-encoded ``/`` or ``..`` inside the
parameter slot masquerade as extra path structure. The raw path is
percent-decoded via ``urllib.parse.unquote`` before segment-splitting,
so ``%2F`` is caught by the same segment-count check as a literal ``/``.

## Request path contract (round 4, finding C-B)

The browser reaches this backend only through the real
``@kirocrew/app-sdk``'s ``useAppApi()``. That SDK adds NO prefix: it
refuses any path not under an entry of ``app.json``'s
``permissions.api`` (``["/apps/config-sync/api"]``) and fetches the path
as given. The gateway route ``/apps/{name}/api/{path}`` then forwards to
this backend as ``/api/{path}``. So ``_PREFIX`` is ``/api`` — the only
prefix a gateway-forwarded request can ever carry.

## Cross-process staleness (senior-review C1)

``poll.py`` and ``push.py`` run as SEPARATE cron processes, each building
its own ``StateStore`` and writing straight to the shared on-disk state
file (``backend/state.py`` now holds a cross-process file lock around
every one of its own read-modify-writes). A server that built ONE
``StateStore`` at first use and cached it for the process's lifetime
would never see a concurrent poll/push process's writes, and its own
read-modify-writes (`resolve_pending` via approve/decline) would run the
#65 staleness check against a stale in-memory ``pending`` and would
clobber concurrently-written fields on save. ``_get_store`` therefore
builds a FRESH ``StateStore`` on every call — one per request — so every
route sees the newest on-disk state and every mutation
(`state.StateStore._locked_rmw`) re-reads under the lock before writing.
This trades one extra JSON parse per request for correctness; the state
document is small and local disk I/O, not a real cost at this app's
request volume.

## Request authentication and mutation guard (H7; round 4 fix B)

Loopback binding stops remote hosts, but NOT another local process (a
different app's backend, a compromised tool, a prompt-injected agent)
connecting straight to this socket and bypassing the gateway's token
auth and per-app scope enforcement (CWE-306). Loopback alone is
therefore never treated as authentication. Every request is checked in
this order, BEFORE any ``routes.*`` function runs:

1. **Gateway HMAC on every route except ``GET /health``.** The gateway
   signs each forwarded request as
   ``X-KiroCrew-Proxy: <ts>:<hex hmac_sha256(secret,
   "<ts>:<method>:<target>:<sha256(body)>")>`` where ``target`` is the
   raw request-target (``self.path``, query included) and the secret is
   the per-app ``KIROCREW_PROXY_SECRET`` env var injected at spawn.
   ``verify_proxy_request`` re-implements the host's
   ``kiro_crew.apps.proxy_auth`` semantics with the stdlib only (this
   backend runs standalone and cannot import ``kiro_crew``): fail closed
   on an empty secret, a missing/malformed header, a timestamp outside
   ±60 s, or a digest mismatch (constant-time compare). Failure -> 401
   ``{"status": "error", "reason": ...}``. ``/health`` stays unsigned
   because the gateway's own health probe calls it directly.
2. **Mutating requests (``POST``) additionally pass a same-site check**:
   a present ``Sec-Fetch-Site`` (a browser-set, script-unforgeable
   header) must be ``same-origin`` or ``none``; any other value 403s. An
   absent ``Sec-Fetch-Site`` is not refused — the HMAC above is the
   authentication. There is deliberately NO ``Origin``-vs-``Host``
   fallback: the gateway rewrites ``Host`` to this backend's loopback
   address but forwards the dashboard's ``Origin`` unchanged, so on any
   non-localhost dashboard the two can never match and the fallback
   would 403 every legitimate mutation.
3. **Loopback-only ``Host`` on mutating requests** (DNS-rebinding
   defence): a ``Host`` that is missing or not a loopback name 403s.

No custom request header is required: the real SDK's ``post()`` has no
headers parameter, so a header rule could never be satisfied by the
shipped UI (superseded H7 design).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import unquote, urlsplit

from backend import routes, state

PORT = int(os.environ.get("PORT", 9100))
APP_NAME = os.environ.get("KIROCREW_APP_NAME", "config-sync")

#: The prefix every gateway-forwarded request carries: the gateway maps
#: `/apps/config-sync/api/{path}` to `/api/{path}` (module docstring,
#: "Request path contract").
_PREFIX = "/api"

#: Gateway proxy-signature header and the env var carrying its secret —
#: identical to `kiro_crew.apps.proxy_auth`.
_PROXY_HEADER = "X-KiroCrew-Proxy"
_PROXY_SECRET_ENV = "KIROCREW_PROXY_SECRET"
_MAX_SKEW_SECONDS = 60

#: Upper bound on a request body read for signature verification. No
#: route consumes a body; this only stops an oversized upload from being
#: buffered before it is refused.
_MAX_BODY_BYTES = 1024 * 1024

#: `Sec-Fetch-Site` values that count as same-site. `same-origin` is the
#: app's own UI calling through the gateway; `none` is a user-initiated
#: navigation. Both are browser-set "forbidden header names" a page's JS
#: cannot forge.
_SAME_SITE_VALUES = ("same-origin", "none")

#: Loopback hostnames/addresses a mutating request's `Host` header must
#: match (with or without a trailing `:<port>`) — DNS-rebinding defence.
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")


def verify_proxy_request(
    header_value: str,
    *,
    method: str,
    target: str,
    body: bytes,
    secret: str,
    now: Optional[float] = None,
) -> bool:
    """Return whether ``header_value`` is a valid, fresh gateway signature

    for (``method``, ``target``, ``body``) under ``secret`` — the same
    semantics as the host's `kiro_crew.apps.proxy_auth
    .verify_proxy_request`. Fails closed: an empty secret, an
    absent/malformed header, a non-numeric or stale (±60 s) timestamp, or
    a signature mismatch all return ``False``.
    """
    if not secret or not header_value or ":" not in header_value:
        return False
    ts_str, _, sig = header_value.partition(":")
    if not ts_str.isdigit() or not sig:
        return False
    clock = time.time() if now is None else now
    if abs(clock - int(ts_str)) > _MAX_SKEW_SECONDS:
        return False
    body_hash = hashlib.sha256(body or b"").hexdigest()
    msg = f"{ts_str}:{method}:{target}:{body_hash}"
    expected = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def _get_store() -> "state.StateStore":
    """Build a FRESH `StateStore` for this call — never a cached,

    process-lifetime instance (senior-review C1: see the module
    docstring's "Cross-process staleness" section). `state.load_state()`
    resolves the state directory (honouring `CONFIG_SYNC_STATE_DIR`) at
    call time, so a test that sets that env var before making a request
    still gets a store pointed at its own isolated directory.
    """
    return state.load_state()


def _segments(path: str) -> Tuple[str, ...]:
    """Percent-decode ``path`` and split it into non-empty ``/``-separated

    segments, so ``%2F``/literal ``/`` and empty segments (a trailing or
    doubled slash) are both normalized before any route pattern is
    matched against segment COUNT.
    """
    decoded = unquote(path)
    return tuple(segment for segment in decoded.split("/") if segment)


def _is_safe_param_segment(segment: str) -> bool:
    """Reject a decoded path-parameter segment that is itself a dot

    component (``.``/``..``). A literal ``..`` segment (or its
    percent-encoded form, already decoded by `_segments` before this
    runs) sitting in a parameter slot is exactly the shape
    `http.client`/a raw socket sends with NO path normalization applied
    (unlike a browser or `requests`, which collapse `..` before the
    request ever goes out) — `/pending/../approve` decodes to the
    3-segment tuple ``("pending", "..", "approve")``, which matches this
    route's segment-COUNT pattern even though the middle segment is not
    a real sha/apply-id. This is the guard that refuses it anyway.
    """
    return segment not in (".", "..")


def _is_loopback_host(host_header: Optional[str]) -> bool:
    """H7 — return whether ``host_header`` (the raw `Host` header value,

    which may carry a trailing ``:<port>``) names a loopback
    host/address. Missing entirely is NOT loopback — a request with no
    `Host` header at all gets the same refusal as a hostile one, since
    there is nothing to validate.
    """
    if not host_header:
        return False
    if host_header in _LOOPBACK_HOSTS:
        # Exact match first: catches a bare, unbracketed IPv6 literal
        # (`::1`) up front, before the generic ":"-based port-strip
        # below would otherwise misparse it as `host:port` and strip
        # everything after the first colon.
        return True
    # Strip a trailing :port, but not the brackets/colons that are part
    # of a literal IPv6 address itself (`[::1]:9100` -> `[::1]`, `[::1]`
    # stays `[::1]` with no port to strip at all).
    if host_header.startswith("["):
        host_only = host_header.rsplit("]", 1)[0] + "]"
    else:
        host_only = host_header.rsplit(":", 1)[0] if ":" in host_header else host_header
    return host_only in _LOOPBACK_HOSTS


def _is_same_site(headers: "Any") -> bool:
    """H7 — whether a mutating request's `Sec-Fetch-Site` permits it.

    A present value must be one of `_SAME_SITE_VALUES`; an absent one is
    not refused (the gateway HMAC is the authentication, and there is no
    `Origin` fallback — see the module docstring).
    """
    sec_fetch_site = headers.get("Sec-Fetch-Site")
    if sec_fetch_site is None:
        return True
    return bool(sec_fetch_site in _SAME_SITE_VALUES)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"status": "ok", "app": APP_NAME})
            return
        if not self._passes_proxy_auth("GET"):
            return
        self._dispatch("GET")

    def do_POST(self) -> None:
        if not self._passes_proxy_auth("POST"):
            return
        if not self._passes_mutation_guard():
            return
        self._dispatch("POST")

    def _read_body(self) -> Optional[bytes]:
        """Read the request body per `Content-Length` (empty if absent).

        Returns ``None`` after writing a 400 when the header is not a
        non-negative integer no larger than `_MAX_BODY_BYTES`.
        """
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return b""
        stripped = raw_length.strip()
        if not stripped.isdigit() or int(stripped) > _MAX_BODY_BYTES:
            self._json(400, {"status": "error", "reason": "invalid content-length"})
            return None
        return self.rfile.read(int(stripped))

    def _passes_proxy_auth(self, method: str) -> bool:
        """Refuse with 401 unless the request carries a valid gateway

        signature (module docstring, check 1). Returns whether the
        request may proceed; on refusal the response is already written.
        """
        body = self._read_body()
        if body is None:
            return False
        if not verify_proxy_request(
            self.headers.get(_PROXY_HEADER) or "",
            method=method,
            target=self.path,
            body=body,
            secret=os.environ.get(_PROXY_SECRET_ENV, ""),
        ):
            self._json(
                401,
                {"status": "error", "reason": "missing or invalid proxy signature"},
            )
            return False
        return True

    def _passes_mutation_guard(self) -> bool:
        """H7 — refuse a mutating request with 403 before it reaches

        `_dispatch` unless it passes BOTH the `Sec-Fetch-Site` check and
        the loopback-`Host` check (module docstring, checks 2-3).
        Returns whether the request may proceed; on refusal it has
        already written the 403 response.
        """
        if not _is_same_site(self.headers):
            self._json(403, {"error": "forbidden"})
            return False
        if not _is_loopback_host(self.headers.get("Host")):
            self._json(403, {"error": "forbidden"})
            return False
        return True

    def _dispatch(self, method: str) -> None:
        parsed = urlsplit(self.path)
        segments = _segments(parsed.path)
        prefix_segments = tuple(seg for seg in _PREFIX.split("/") if seg)

        if segments[: len(prefix_segments)] != prefix_segments:
            self._json(404, {"error": "not found"})
            return
        rest = segments[len(prefix_segments) :]

        result: Optional[Dict[str, Any]] = None

        if method == "GET" and rest == ("status",):
            result = routes.status(_get_store())
        elif method == "GET" and rest == ("drift",):
            result = routes.drift(_get_store())
        elif method == "POST" and rest == ("push",):
            result = routes.push_now(_get_store())
        elif (
            method == "POST"
            and len(rest) == 2
            and rest[0] == "restore"
            and _is_safe_param_segment(rest[1])
        ):
            apply_id = rest[1]
            result = routes.restore(_get_store(), apply_id)

        if result is None:
            # Either an unknown path, or a known path reached with the
            # wrong verb (e.g. GET on a POST-only route, or a path
            # parameter slot that did not resolve to exactly one
            # segment). `BaseHTTPRequestHandler` has no built-in 405, and
            # design.md pins the verb per route but not the exact refusal
            # code, so a bare 404 is a legitimate refusal shape for both
            # cases — never dispatching into `routes.*` either way.
            self._json(404, {"error": "not found"})
            return

        self._json(200, result)

    def _json(self, code: int, data: Dict[str, Any]) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


def build_server(host: str = "127.0.0.1", port: int = 0) -> HTTPServer:
    """Build (never starts) an `HTTPServer` bound to loopback only.

    ``host`` defaults to `127.0.0.1` — never `0.0.0.0` or any other
    all-interfaces address. Loopback binding keeps remote hosts out; the
    gateway HMAC (module docstring) keeps out other local processes.
    """
    return HTTPServer((host, port), Handler)


if __name__ == "__main__":
    print(f"{APP_NAME} backend on port {PORT}")
    build_server(host="127.0.0.1", port=PORT).serve_forever()
