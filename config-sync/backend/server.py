"""Backend HTTP server for config-sync (tasks.md 6.2's "HTTP wiring";

design.md's routes table, ~lines 331-345) — a KiroCrew app.

Run with: python backend/server.py
Or let KiroCrew manage it via the app manifest backend section.

Dispatches each design-table verb/path pair to its matching
``backend.routes.*`` function, passing a single module-level
``state.StateStore`` every request shares — the route functions
themselves hold no HTTP knowledge (``backend/routes.py``'s own
docstring: they are "plain, framework-agnostic functions"). This module
owns exactly: verb/path matching, path-parameter extraction, JSON
response shape, and the loopback-only bind — never route BEHAVIOUR,
which is `backend.routes`'/`backend.apply`'s job and is tested there.

A path parameter (``sha``/``apply_id``) is extracted only when the
request path has EXACTLY the expected number of ``/``-separated
segments for that route — never by a permissive ``rsplit``/regex that
would let a literal or percent-encoded ``/`` or ``..`` inside the
parameter slot masquerade as extra path structure. The raw path is
percent-decoded via ``urllib.parse.unquote`` before segment-splitting,
so ``%2F`` is caught by the same segment-count check as a literal ``/``.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import unquote, urlsplit

from backend import routes, state

PORT = int(os.environ.get("PORT", 9100))
APP_NAME = os.environ.get("KIROCREW_APP_NAME", "config-sync")

_PREFIX = "/api/apps/config-sync"

#: The single `StateStore` every request shares — module-level so the
#: same store `routes.*` sees is the one the app's cron jobs and any
#: other in-process caller also use, matching `test_server.py`'s own
#: requirement that a test-seeded store's state is what a route call
#: observes. Rebuilt from disk on the FIRST use (`_get_store`), never at
#: import time — `state.load_state()` resolves the state directory
#: (honouring `CONFIG_SYNC_STATE_DIR`) at call time, so a test that sets
#: that env var before the server starts still gets its own store.
_store: Optional["state.StateStore"] = None


def _get_store() -> "state.StateStore":
    global _store
    if _store is None:
        _store = state.load_state()
    return _store


def _reset_store_for_tests() -> None:
    """Drop the cached store so the next `_get_store()` call reloads from

    (a possibly newly-pointed) `CONFIG_SYNC_STATE_DIR` — used only by
    tests that reload this module (`importlib.reload`), since a plain
    module reload does not by itself clear a `global` that already holds
    a live object across the reload.
    """
    global _store
    _store = None


_reset_store_for_tests()


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


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"status": "ok", "app": APP_NAME})
            return
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

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
            and len(rest) == 3
            and rest[0] == "pending"
            and rest[2] in ("approve", "decline")
            and _is_safe_param_segment(rest[1])
        ):
            sha = rest[1]
            handler: Callable[["state.StateStore", str], Dict[str, Any]] = (
                routes.approve if rest[2] == "approve" else routes.decline
            )
            result = handler(_get_store(), sha)
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
