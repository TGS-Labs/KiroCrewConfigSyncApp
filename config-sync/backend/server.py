"""Backend HTTP server for config-sync (tasks.md 6.2's "HTTP wiring";

design.md's routes table, ~lines 331-345) — a KiroCrew app.

Run with: python backend/server.py
Or let KiroCrew manage it via the app manifest backend section.

Dispatches each design-table verb/path pair to its matching
``backend.routes.*`` function, passing a fresh ``state.StateStore`` built
per request (see ``_get_store`` below) — the route functions themselves
hold no HTTP knowledge (``backend/routes.py``'s own docstring: they are
"plain, framework-agnostic functions"). This module owns exactly:
verb/path matching, path-parameter extraction, the mutation-request guard,
JSON response shape, and the loopback-only bind — never route BEHAVIOUR,
which is `backend.routes`'/`backend.apply`'s job and is tested there.

A path parameter (``sha``/``apply_id``) is extracted only when the
request path has EXACTLY the expected number of ``/``-separated
segments for that route — never by a permissive ``rsplit``/regex that
would let a literal or percent-encoded ``/`` or ``..`` inside the
parameter slot masquerade as extra path structure. The raw path is
percent-decoded via ``urllib.parse.unquote`` before segment-splitting,
so ``%2F`` is caught by the same segment-count check as a literal ``/``.

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

## Mutation-request guard (senior-review H7)

Every mutating request (``POST``) must carry ``X-Config-Sync-Request: 1``
and a loopback ``Host`` header (``127.0.0.1``/``localhost``/``[::1]``,
optionally with a port) — checked in ``_dispatch`` BEFORE any route runs.
The server binds loopback-only, but that alone only stops a remote
attacker; a plain unauthenticated HTTP server is still reachable by
`fetch`/a form submit from ANY page open in a browser on the same
machine. A required custom header is a real CSRF barrier because a
simple cross-origin request (one that skips CORS preflight, including a
form submission) cannot set one; pinning ``Host`` additionally catches
DNS rebinding, where a page's JS can cause the browser to send a request
to ``127.0.0.1`` while the ``Host`` header it sends still reflects
whatever hostname resolved there. ``GET`` requests are unaffected — this
is a mutation guard, not a blanket auth layer the design does not
otherwise call for.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import unquote, urlsplit

from backend import routes, state

PORT = int(os.environ.get("PORT", 9100))
APP_NAME = os.environ.get("KIROCREW_APP_NAME", "config-sync")

_PREFIX = "/api/apps/config-sync"

#: H7 — every mutating (POST) request must carry this header with this
#: exact value. A cross-origin `fetch`/`<form>` submission — including
#: the "simple request" shapes that skip CORS preflight entirely — has
#: no way to set an arbitrary custom header, so requiring one is a real
#: CSRF barrier rather than a check an attacker's request trivially
#: satisfies.
_REQUIRED_HEADER = "X-Config-Sync-Request"
_REQUIRED_HEADER_VALUE = "1"

#: H7 — loopback hostnames/addresses a mutating request's `Host` header
#: must match (with or without a trailing `:<port>`). Pinning `Host`
#: catches DNS rebinding: an attacker's page can cause the victim's
#: browser to send a request to `127.0.0.1`, but cannot make the browser
#: send a `Host` header other than the one reflecting whatever hostname
#: it believes it navigated to, unless that hostname itself already
#: resolves to loopback.
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")


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


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"status": "ok", "app": APP_NAME})
            return
        self._dispatch("GET")

    def do_POST(self) -> None:
        if not self._passes_mutation_guard():
            return
        self._dispatch("POST")

    def _passes_mutation_guard(self) -> bool:
        """H7 — refuse a mutating request with 403 before it ever reaches

        `_dispatch`/any `routes.*` function, unless it carries BOTH the
        required custom header and a loopback `Host`. Returns whether the
        request may proceed; on refusal it has already written the 403
        response.
        """
        header_value = self.headers.get(_REQUIRED_HEADER)
        if header_value != _REQUIRED_HEADER_VALUE:
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
    all-interfaces address — because `approve` triggers a real apply and
    every other route reads or mutates this instance's own configuration;
    exposing this server beyond loopback would let any host that can
    reach this machine drive it with no authentication at all.
    """
    return HTTPServer((host, port), Handler)


if __name__ == "__main__":
    print(f"{APP_NAME} backend on port {PORT}")
    build_server(host="127.0.0.1", port=PORT).serve_forever()
