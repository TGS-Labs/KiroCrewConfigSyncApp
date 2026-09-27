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

## Mutation-request guard (senior-review H7, redesigned round 3 finding C-B)

Every mutating request (``POST``) must pass BOTH a same-site check and a
loopback ``Host`` check — checked in ``_dispatch`` BEFORE any route
runs. The server binds loopback-only, but that alone only stops a
remote attacker; a plain unauthenticated HTTP server is still reachable
by `fetch`/a form submit from ANY page open in a browser on the same
machine.

### Why this is NOT a custom-header check

H7 originally required a custom header (``X-Config-Sync-Request: 1``)
on every mutating request, reasoning that a simple cross-origin request
cannot set one. That is true of a bare cross-origin ``fetch``/``<form>``
— but this app is served *through the KiroCrew gateway's own app UI
host*, and the UI can only reach its own backend via the real
``@kirocrew/app-sdk``'s ``useAppApi()``. Reading the actual SDK
implementation (the host bundle's ``Dse`` factory, which every
``useAppApi().api`` is built from) shows:

- ``get(path, init)`` merges ``init.headers`` into the request — a
  custom header CAN reach ``fetch`` through ``get()``.
- ``post(path, body)``/``put``/``patch`` take **no headers parameter at
  all** — their headers are hardcoded to
  ``{"Content-Type": "application/json"}`` inside the SDK itself. There
  is no way for this app's UI code to attach a custom header to a real
  mutating request, because the SDK's own ``post()`` does not expose
  that capability.

So a header-based guard on ``POST`` cannot be satisfied by the real SDK
at all — it would permanently 403 every legitimate mutation from the
shipped UI, and Requirement 7.7 (refuse a forged cross-site POST while
still accepting the app's own proxied requests) has to be met a
different way.

### The redesigned checks

1. **Same-site check via `Sec-Fetch-Site`.** Every modern browser sets
   this fetch-metadata header on every request and — critically — it is
   a "forbidden header name": no page JavaScript can set, override, or
   suppress it via ``fetch()``/``XMLHttpRequest``, unlike a custom
   header, which a same-origin script could always set anyway. A
   cross-site page's `fetch`/`<form>` POST to this loopback server
   always carries ``Sec-Fetch-Site: cross-site`` (or ``same-site`` for a
   different-but-related site — also refused, since nothing legitimately
   calls this backend from another site at all); the app's own
   same-origin UI call carries ``same-origin`` (or, for a top-level
   navigation-triggered request, ``none``). Refuse any value other than
   ``same-origin``/``none``.
2. **`Origin` fallback when `Sec-Fetch-Site` is absent.** Older browsers
   and non-browser HTTP clients (``curl``, ``requests``, this project's
   own test suite, the gateway's own health probe) never send
   ``Sec-Fetch-Site`` at all. When it is missing, fall back to requiring
   an ``Origin`` header whose host matches the request's own loopback
   ``Host`` — a genuine cross-site browser *fetch* still always sends
   ``Origin`` (also a forbidden header name), so a forged browser
   request cannot spoof its way past this fallback either. A request
   with **neither** header (e.g. a bare loopback CLI/script call with no
   ``Origin`` at all) is treated as same-site: this mirrors H7's original
   scope — the guard defends against a *browser* page reaching this
   server, not against arbitrary local process access, which loopback
   binding plus the gateway's own proxy already gate for the shipped
   deployment path (the gateway signs every proxied request with
   ``X-KiroCrew-Proxy``; a caller that reaches this backend directly
   without going through the gateway already had to be on the same
   machine).
3. **Loopback-only `Host` header**, unchanged from the original H7
   design: catches DNS rebinding, where a page's JS can cause the
   browser to send a request to ``127.0.0.1`` while the ``Host`` header
   it sends still reflects whatever hostname resolved there.
4. **`GET` requests are unaffected** — this remains a mutation guard, not
   a blanket auth layer the design does not otherwise call for.

This still satisfies every original H7 property: a bare cross-origin
`fetch`/`<form>` POST (no `Origin`-matching-`Host`, and
`Sec-Fetch-Site: cross-site`) is refused; DNS rebinding is refused via
`Host`; `GET` is untouched — while remaining satisfiable by the real
SDK's `post()`, which sends neither a custom header nor any control over
`Sec-Fetch-Site`/`Origin` (both are ordinary same-origin `fetch` calls
from the app's own UI origin, so the browser sets them correctly with no
code in this app needing to do anything).
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

#: H7 (redesigned, round 3 C-B) — `Sec-Fetch-Site` values that count as
#: same-site. `same-origin` is the ordinary case for this app's own UI
#: calling its own backend; `none` covers a top-level navigation (not a
#: fetch at all) and a bare loopback CLI/script call, which never sets
#: fetch metadata. Both are "forbidden header names" a page's JS cannot
#: set, override, or suppress — unlike the custom header this guard used
#: to require, which the real `@kirocrew/app-sdk`'s `post()` has no way
#: to attach at all (see the module docstring's "Why this is NOT a
#: custom-header check").
_SAME_SITE_VALUES = ("same-origin", "none")

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


def _origin_host(origin_header: Optional[str]) -> Optional[str]:
    """Extract the host[:port] authority from an ``Origin`` header value,

    or ``None`` if it is missing/unparseable. ``Origin`` is always
    ``scheme://host[:port]`` with no path — `urlsplit` on it yields the
    authority in `.netloc` directly.
    """
    if not origin_header:
        return None
    netloc = urlsplit(origin_header).netloc
    return netloc or None


def _is_same_site(headers: "Any") -> bool:
    """H7 (redesigned) — whether a mutating request's fetch-metadata

    headers indicate it originated same-site, per the module docstring's
    "The redesigned checks" section. Checked BEFORE `Host`, since a
    request that fails this can never be same-site regardless of what
    `Host` says.
    """
    sec_fetch_site = headers.get("Sec-Fetch-Site")
    if sec_fetch_site is not None:
        return sec_fetch_site in _SAME_SITE_VALUES
    origin = headers.get("Origin")
    if origin is None:
        # Neither fetch-metadata header present: a bare loopback
        # CLI/script call (curl, requests, this project's own tests, the
        # gateway's health probe) rather than a browser fetch. Browsers
        # always send at least one of these two on a cross-site request,
        # so treat "neither present" as same-site — see the module
        # docstring for why this matches H7's original scope.
        return True
    origin_host = _origin_host(origin)
    host_header = headers.get("Host")
    if origin_host is None or host_header is None:
        return False
    return bool(origin_host == host_header)


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
        """H7 (redesigned) — refuse a mutating request with 403 before it

        ever reaches `_dispatch`/any `routes.*` function, unless it
        passes BOTH the same-site check and the loopback-`Host` check.
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
    all-interfaces address — because `approve` triggers a real apply and
    every other route reads or mutates this instance's own configuration;
    exposing this server beyond loopback would let any host that can
    reach this machine drive it with no authentication at all.
    """
    return HTTPServer((host, port), Handler)


if __name__ == "__main__":
    print(f"{APP_NAME} backend on port {PORT}")
    build_server(host="127.0.0.1", port=PORT).serve_forever()
