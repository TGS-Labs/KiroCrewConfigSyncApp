// Real-SDK-shaped `useAppApi()` client factory (senior review round 3, C-B).
//
// Deliberately OUTSIDE the `@kirocrew/app-sdk` alias target
// (`test-stubs/app-sdk.ts`, which `vitest.config.ts` resolves the bare
// specifier to): `App.real-contract.test.tsx` imports THIS file directly by
// relative path so it can capture a reference to `buildRealShapedAppApi`
// before `vi.mock('@kirocrew/app-sdk', ...)` re-registers that specifier —
// importing it *through* the aliased specifier from inside the mock factory
// would resolve back into the mock being defined. `test-stubs/app-sdk.ts`
// re-exports this same function so ordinary (non-real-contract) test files
// that only need a plain SDK stub still get it via the alias.
//
// Mirrors the REAL host implementation exactly (verified against
// `/usr/local/lib/python3.12/site-packages/kiro_crew/static/dist/assets/
// App-*.js`, function `Dse`, which is what every `useAppApi().api` is built
// from via `MF`'s `AppApiProvider`):
//
//   function Dse(allowedApiPaths, appName, sessionKey) {
//     let resolve = (path) => { ...prefixes/validates against allowedApiPaths... }
//     let request = async (path, init) => {
//       let resolved = resolve(path)
//       let headers = new Headers(init?.headers)
//       if (sessionKey && !headers.has('X-Session-Key')) headers.set(...)
//       let res = await fetch(resolved, { ...init, headers })
//       ...
//     }
//     return {
//       get: (path, init) => request(path, { ...init, method: 'GET' }),
//       post: (path, body) => request(path, {
//         method: 'POST',
//         headers: { 'Content-Type': 'application/json' },
//         body: body == null ? undefined : JSON.stringify(body),
//       }),
//       ...
//     }
//   }
//
// Two behaviours this reproduces EXACTLY, because they are the exact gap the
// PREVIOUS stub (which let a bespoke `post(path, body, { headers })` three-arg
// call through) hid from every test that used it:
//
// 1. Path prefixing: the real client resolves a caller-given path (e.g.
//    `/status`) against ONE base the host already knows (this app's own
//    `/api/apps/config-sync` prefix) — `App.tsx` never spells out that prefix
//    itself; the SDK adds it.
// 2. `post(path, body)` takes exactly TWO positional arguments. There is NO
//    THIRD `init`/`headers` parameter on the real `post` — its headers are
//    hardcoded to `{'Content-Type': 'application/json'}` inside the SDK
//    itself. A caller cannot pass a custom header through `post()`. `get(path,
//    init)` DOES forward a second `init` argument's `headers` into the
//    underlying `fetch`. This is WHY `backend/server.py`'s H7 guard was
//    redesigned around `Sec-Fetch-Site`/`Origin` rather than a custom header
//    (see that module's docstring) — this factory's `post()` intentionally has
//    no way to attach one, matching production.
export const APP_PREFIX = '/api/apps/config-sync'

export interface AppApiClient {
  get: (path: string, init?: RequestInit) => Promise<unknown>
  post: (path: string, body?: unknown) => Promise<unknown>
  put: (path: string, body?: unknown) => Promise<unknown>
  patch: (path: string, body?: unknown) => Promise<unknown>
  del: (path: string) => Promise<unknown>
}

function resolvePath(path: string): string {
  if (/^(?:https?:)?\/\//i.test(path) || path.includes('\\')) {
    throw new Error(`[app-sdk] Absolute URLs are not allowed: ${path}`)
  }
  const url = new URL(path, 'http://localhost')
  const prefixed = url.pathname.startsWith(APP_PREFIX)
    ? url.pathname
    : `${APP_PREFIX}${url.pathname}`
  return `${prefixed}${url.search}`
}

async function request(path: string, init?: RequestInit): Promise<unknown> {
  const resolved = resolvePath(path)
  const res = await fetch(resolved, init)
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText)
    throw new Error(`API ${res.status}: ${text}`)
  }
  if (res.status === 204 || res.status === 205) return undefined
  const text = await res.text()
  return text.trim() !== '' ? JSON.parse(text) : undefined
}

/** Build a client with the REAL SDK's exact surface — no third `headers`

 * argument on `post`/`put`/`patch`, matching production exactly. */
export function buildRealShapedAppApi(): AppApiClient {
  return {
    get: (path, init) => request(path, { ...init, method: 'GET' }),
    post: (path, body) =>
      request(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: body == null ? undefined : JSON.stringify(body),
      }),
    put: (path, body) =>
      request(path, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: body == null ? undefined : JSON.stringify(body),
      }),
    patch: (path, body) =>
      request(path, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: body == null ? undefined : JSON.stringify(body),
      }),
    del: (path) => request(path, { method: 'DELETE' }),
  }
}
