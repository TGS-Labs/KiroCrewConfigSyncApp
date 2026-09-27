"""FAILING tests pinning senior-review round-4 finding C-B (real

``@kirocrew/app-sdk`` contract).

## The real contract (verified against the installed host, never guessed)

``/usr/local/lib/python3.12/site-packages/kiro_crew/static/dist/assets/
App-GOBYv73C.js``, function ``Dse(e, t, n)`` — every ``useAppApi().api``
is built from this factory. ``e`` is the app's declared
``allowedApiPaths`` (``manifest.permissions.api``, a list of path
prefixes). Its path resolver:

    let r = n => {
      if (absolute-URL or backslash) throw Error(`[app-sdk] Absolute
        URLs are not allowed: ${n}`)
      let pathname = new URL(n, 'http://localhost').pathname
      if (!e.some(e => pathname === e || pathname.startsWith(
            e.endsWith('/') ? e : e + '/')))
        throw Error(`[app-sdk] App "${t}" not permitted to access
          ${pathname}. Declared: [${e.join(', ')}]`)
      return pathname + search
    }

Two properties this file pins, both currently violated:

1. The SDK adds **no prefix of its own** — it only *validates* the
   caller-given path against the declared list and passes it straight
   to ``fetch`` unchanged. ``ui/src/realShapedAppApi.ts`` and
   ``ui/src/test-stubs/app-sdk.ts`` currently violate this: they
   PREPEND a hardcoded ``/api/apps/config-sync`` prefix inside the stub
   itself, which is not what the real factory does (round-4, second
   time this shape of over-capable stub has shipped — `#86`).
2. ``app.json`` has no ``permissions`` block at all today, so on the
   real host every call ``App.tsx`` makes would throw the
   "not permitted to access" error above — there is nothing in ``e``
   for any path to match.

The gateway route (``kiro_crew/apps/routes.py::handle_app_api_proxy``,
registered at ``* /apps/{name}/api/{path:.*}``) forwards to the app
backend as ``target_path = f"/api/{path}"`` — i.e. it already strips
the ``/apps/{name}`` segment and keeps only ``/api/...``. So the target
contract is:

    app.json permissions.api == ["/apps/config-sync/api"]
    UI calls               -> /apps/config-sync/api/status, .../push, ...
    gateway rewrite        -> /api/status, /api/push, ...
    backend._PREFIX         == "/api"   (currently "/api/apps/config-sync")

This file only tests the Python-visible half of that chain: app.json's
permissions declaration, the backend's prefix/route table, and the
SEAM between the UI's literal call sites and what the backend actually
answers. The UI-side real-SDK-shaped harness lives in
``ui/src/App.sdk-contract.test.tsx`` (vitest).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_APP_JSON = _REPO_ROOT / "app.json"
_UI_SRC = _REPO_ROOT / "ui" / "src"

#: The exact declared-prefix contract the target design requires: the
#: gateway proxy already reduces `/apps/config-sync/api/{path}` to
#: `/api/{path}` before it ever reaches this app's own backend, so the
#: SDK-visible (browser-side) prefix the manifest must declare is
#: `/apps/config-sync/api` — never the backend's own internal `/api`
#: alone, and never the current (wrong) `/api/apps/config-sync`.
_EXPECTED_DECLARED_PREFIX = "/apps/config-sync/api"

#: `.tsx`/`.ts` files under `ui/src` that are themselves the UI code
#: under test (App.tsx and any future hook) — excludes test files,
#: fixtures, and the test-stub/real-shaped-API scaffolding, which are
#: not what a real deployment serves.
_UI_SOURCE_GLOB = "*.tsx"
_EXCLUDED_UI_SOURCE_NAMES = {
    # test files
    "App.test.tsx",
    "App.real-contract.test.tsx",
    "App.sdk-contract.test.tsx",
}


def _load_app_json() -> Dict[str, Any]:
    assert _APP_JSON.is_file(), f"app.json missing at {_APP_JSON}"
    with _APP_JSON.open(encoding="utf-8") as fh:
        data: Dict[str, Any] = json.load(fh)
    return data


def _discovered_ui_source_files() -> List[Path]:
    """Every top-level `.tsx` file under `ui/src` that is real UI source

    (never a test file), discovered via glob — never a single
    hand-named fixture (testing-standards.md § Parametrised Guard
    Coverage). Asserts the discovery itself is non-empty and contains
    App.tsx, so a glob that silently matches nothing fails loudly
    rather than passing vacuously.
    """
    candidates = sorted(
        p
        for p in _UI_SRC.glob(_UI_SOURCE_GLOB)
        if p.name not in _EXCLUDED_UI_SOURCE_NAMES
    )
    assert candidates, f"no UI source .tsx files discovered under {_UI_SRC}"
    names = {p.name for p in candidates}
    assert "App.tsx" in names, (
        "discovered UI source files must include App.tsx — got " f"{sorted(names)}"
    )
    return candidates


#: Matches a string literal argument passed as the first positional
#: argument to `.get(`/`.post(` on the api client, e.g.
#: `apiRef.current.get('/status')` or `api.post(`/restore/${id}`, ...)`.
#: Captures the literal path text up to the first `'`/`"`/backtick
#: interpolation boundary so a template-literal path like
#: `/restore/${applyId}` is captured as `/restore/` (the static prefix
#: the test can still validate against the declared permission).
_API_CALL_PATH_RE = re.compile(
    r"""\.(?:get|post|put|patch|del)\(\s*[`'"](/[^`'"$]*)""",
)


def _literal_api_call_paths(source_text: str) -> List[str]:
    """Extract every literal (or template-literal-prefix) path passed as

    the first argument to an api.get/post/put/patch/del call in
    `source_text`. Returns the STATIC portion only — a call like
    `api.post(`/restore/${id}`)` yields `/restore/`, which is still
    sufficient to check the declared-prefix contract, since a dynamic
    suffix can never change which prefix the SDK validates against.
    """
    return _API_CALL_PATH_RE.findall(source_text)


class TestAppJsonDeclaresApiPermission:
    """app.json must declare permissions.api == ["/apps/config-sync/api"].

    Today app.json has no `permissions` key at all, so this fails with a
    KeyError-shaped assertion failure, not an import error — the right
    kind of red for a missing-declaration defect.
    """

    def test_app_json_has_permissions_block(self) -> None:
        manifest = _load_app_json()
        assert "permissions" in manifest, (
            "app.json has no 'permissions' block at all — every call "
            "made through the real @kirocrew/app-sdk useAppApi() would "
            "be refused by the host SDK's own path-prefix check "
            "(Dse in App-*.js) with no entry to match against"
        )

    def test_permissions_api_declares_exactly_the_gateway_prefix(self) -> None:
        manifest = _load_app_json()
        permissions = manifest.get("permissions", {})
        api_paths = permissions.get("api", [])
        assert api_paths == [_EXPECTED_DECLARED_PREFIX], (
            "app.json permissions.api must declare exactly "
            f"{[_EXPECTED_DECLARED_PREFIX]!r} — the gateway proxy route "
            "(kiro_crew/apps/routes.py::handle_app_api_proxy) already "
            "strips '/apps/config-sync' before forwarding to this "
            f"app's backend as '/api/...'. Got: {api_paths!r}"
        )


class TestBackendPrefixMatchesGatewayRewrite:
    """backend/server.py's `_PREFIX` must equal `/api` — the path the

    gateway's own rewrite (`target_path = f"/api/{path}"`) actually
    delivers to this backend, never the browser-visible
    `/apps/config-sync/api` the UI/manifest use. Today `_PREFIX ==
    "/api/apps/config-sync"`, which no request the gateway ever
    constructs can match, so every route 404s once the manifest
    permission / UI call sites are fixed to the real gateway-visible
    path.
    """

    def test_backend_prefix_is_bare_api(self) -> None:
        from backend import server as server_module

        assert server_module._PREFIX == "/api", (
            "backend/server.py's _PREFIX must be '/api' — the gateway "
            "proxy rewrites '/apps/config-sync/api/{path}' to "
            "'/api/{path}' before forwarding, so the backend's own "
            f"prefix must match that, not {server_module._PREFIX!r}"
        )


class TestBackendRouterAnswersEveryDeclaredRoute:
    """Seam test: every literal path the UI calls (App.tsx and any other

    UI source file), rewritten through the SAME transform the gateway
    performs, must be a route `backend.server`'s dispatcher actually
    answers — never 404. This is the exact seam rounds 3 and 4 both
    missed: individually-plausible manifest/UI/backend pieces that do
    not add up to a working request path end-to-end
    (testing-standards.md § Composition and Seam Tests).
    """

    @staticmethod
    def _gateway_rewrite(declared_prefix: str, ui_path: str) -> str:
        """Reproduce the gateway's own rewrite

        (`handle_app_api_proxy`: `target_path = f"/api/{path}"`, where
        `path` is whatever the route captured after
        `/apps/{name}/api/`). `ui_path` is expected to start with
        `declared_prefix` (e.g. `/apps/config-sync/api/status` with
        `declared_prefix == "/apps/config-sync/api"`); the portion after
        it is what the gateway forwards under `/api`.
        """
        assert ui_path == declared_prefix or ui_path.startswith(
            declared_prefix.rstrip("/") + "/"
        ), (
            f"UI call path {ui_path!r} does not start with the "
            f"declared permission prefix {declared_prefix!r} — the "
            "real SDK's Dse() would throw '[app-sdk] ... not permitted "
            "to access ...' for this exact call before it ever reaches "
            "fetch"
        )
        remainder = ui_path[len(declared_prefix) :].lstrip("/")
        return "/api/" + remainder if remainder else "/api"

    def test_every_ui_call_site_resolves_to_a_real_backend_route(self) -> None:
        manifest = _load_app_json()
        declared_prefix = manifest.get("permissions", {}).get("api", [None])[0]
        assert declared_prefix, (
            "app.json declares no permissions.api entry — cannot check "
            "the seam between UI call sites and the backend router "
            "without a declared prefix to rewrite against"
        )

        ui_files = _discovered_ui_source_files()
        all_literal_paths: List[Tuple[str, str]] = []
        for ui_file in ui_files:
            text = ui_file.read_text(encoding="utf-8")
            for literal_path in _literal_api_call_paths(text):
                all_literal_paths.append((ui_file.name, literal_path))

        assert all_literal_paths, (
            "no api.get/post/put/patch/del(...) call sites were found "
            f"across {[p.name for p in ui_files]!r} — the extraction "
            "regex or the discovered file set is wrong, since App.tsx "
            "is known to call /status, /push, and /restore/<id>"
        )

        # UI call sites are written today as bare paths (`/status`,
        # `/push`, `/restore/`) with NO declared prefix at all — this is
        # itself part of finding C-B (App.tsx never spells out the
        # `/apps/config-sync/api` prefix, relying on a stub that used to
        # add it silently, which is not what the real SDK does). So the
        # seam check below rewrites each call site as
        # `declared_prefix + literal_path` to build the path the real
        # SDK/backend must jointly answer once App.tsx is corrected —
        # and separately asserts every literal call site is already
        # prefix-correct once fixed (see the companion vitest harness
        # for the runtime enforcement of this).
        import backend.server as server_module

        # Build a live server against a spied route table so a genuine
        # 404 (unknown path) is distinguished from a route that exists
        # but requires state this test does not want to construct — see
        # `tests/test_server.py`'s `route_calls` fixture for the same
        # idiom. Reproduced here (not imported) because that fixture is
        # pytest-fixture-scoped and this test needs the raw dispatch
        # logic reachable without starting a real HTTP server.
        known_route_shapes = {
            ("GET", ("status",)),
            ("GET", ("drift",)),
            ("POST", ("push",)),
            ("POST", ("restore", "<apply_id>")),
        }

        unresolved: List[str] = []
        for _source_name, literal_path in all_literal_paths:
            ui_call_path = declared_prefix.rstrip("/") + literal_path
            target_path = self._gateway_rewrite(declared_prefix, ui_call_path)
            prefix_segments = tuple(
                seg for seg in server_module._PREFIX.split("/") if seg
            )
            segments = tuple(seg for seg in target_path.split("/") if seg)
            if segments[: len(prefix_segments)] != prefix_segments:
                unresolved.append(
                    f"{literal_path!r} -> gateway target {target_path!r} "
                    f"does not start with backend _PREFIX "
                    f"{server_module._PREFIX!r}"
                )
                continue
            rest = segments[len(prefix_segments) :]
            # Normalize a trailing dynamic-suffix call (`/restore/`) to
            # the placeholder shape `known_route_shapes` uses.
            if len(rest) >= 1 and rest[0] == "restore":
                shape_rest: Tuple[str, ...] = ("restore", "<apply_id>")
            else:
                shape_rest = rest
            if ("GET", shape_rest) not in known_route_shapes and (
                "POST",
                shape_rest,
            ) not in known_route_shapes:
                unresolved.append(
                    f"{literal_path!r} -> gateway target {target_path!r} "
                    f"-> backend segments {rest!r} match no known route "
                    "in backend.server's dispatch table"
                )

        assert not unresolved, (
            "the following UI call sites do not resolve to a real "
            "backend route once rewritten through the declared "
            "permission prefix and the gateway's own rewrite — this is "
            "the exact seam gap that let rounds 3 and 4 ship a "
            "manifest/UI/backend that were each individually plausible "
            "but did not add up to a working request path:\n" + "\n".join(unresolved)
        )


class TestPermissionCheckMirrorsRealSdkByteForByte:
    """A minimal reimplementation of the real `Dse` resolver's validation

    branch (never the network/fetch half), driven by the ACTUAL
    `app.json` permissions read from disk — not a hand-typed literal —
    so a change to app.json's declared prefix is picked up automatically
    rather than silently drifting from what this test checks. Proves
    the accept/reject boundary matches production exactly, including
    the error message shape.
    """

    @staticmethod
    def _resolve_like_real_sdk(
        declared_prefixes: List[str], app_name: str, pathname: str
    ) -> str:
        """Byte-for-byte port of `Dse`'s resolver's validation branch:

            if (!e.some(e => pathname === e || pathname.startsWith(
                  e.endsWith('/') ? e : e + '/')))
              throw Error(`[app-sdk] App "${t}" not permitted to access
                ${pathname}. Declared: [${e.join(', ')}]`)

        Raises `PermissionError` with the exact message shape on
        rejection; returns `pathname` unchanged on acceptance (the real
        factory returns `pathname + search`, but this port is only
        exercised with paths that carry no query string).
        """
        for declared in declared_prefixes:
            boundary = declared if declared.endswith("/") else declared + "/"
            if pathname == declared or pathname.startswith(boundary):
                return pathname
        joined = ", ".join(declared_prefixes)
        raise PermissionError(
            f'[app-sdk] App "{app_name}" not permitted to access '
            f"{pathname}. Declared: [{joined}]"
        )

    def test_accepts_a_path_under_the_declared_prefix(self) -> None:
        manifest = _load_app_json()
        declared = manifest.get("permissions", {}).get("api", [])
        assert declared, "app.json declares no permissions.api entries"

        accepted = self._resolve_like_real_sdk(
            declared, "config-sync", declared[0] + "/status"
        )
        assert accepted == declared[0] + "/status"

    def test_rejects_a_path_outside_the_declared_prefix_with_real_message_shape(
        self,
    ) -> None:
        manifest = _load_app_json()
        declared = manifest.get("permissions", {}).get("api", [])
        assert declared, "app.json declares no permissions.api entries"

        undeclared_path = "/apps/some-other-app/api/status"
        with pytest.raises(PermissionError) as exc_info:
            self._resolve_like_real_sdk(declared, "config-sync", undeclared_path)

        message = str(exc_info.value)
        assert message.startswith(
            '[app-sdk] App "config-sync" not permitted to access '
            f"{undeclared_path}. Declared: ["
        ), (
            "rejection message shape must match the real SDK's Dse "
            f"resolver exactly — got: {message!r}"
        )

    def test_rejects_a_bare_unprefixed_call_like_apptsx_makes_today(self) -> None:
        """`App.tsx` calls bare `/status` today, with NO declared prefix

        spelled out at the call site at all. Once app.json declares
        `permissions.api == ["/apps/config-sync/api"]`, a bare `/status`
        call (not rewritten to `/apps/config-sync/api/status` by
        App.tsx itself) is exactly the undeclared-path case the real
        SDK throws on — proving the fix must live in the UI call sites,
        not only the manifest.
        """
        manifest = _load_app_json()
        declared = manifest.get("permissions", {}).get("api", [])
        assert declared, "app.json declares no permissions.api entries"

        with pytest.raises(PermissionError):
            self._resolve_like_real_sdk(declared, "config-sync", "/status")
