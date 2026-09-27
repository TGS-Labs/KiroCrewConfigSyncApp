// Real-SDK-shaped `useAppApi()` client factory (senior review rounds 3-4, C-B).
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
// App-*.js`, function `Dse(e, t, n)`: `e` = `manifest.permissions.api`,
// `t` = app name — every `useAppApi().api` is built from it):
//
//   let r = n => {
//     if (absolute-URL or backslash) throw Error(`[app-sdk] Absolute URLs
//       are not allowed: ${n}`)
//     let pathname = new URL(n, 'http://localhost').pathname
//     if (!e.some(e => pathname === e || pathname.startsWith(
//           e.endsWith('/') ? e : e + '/')))
//       throw Error(`[app-sdk] App "${t}" not permitted to access
//         ${pathname}. Declared: [${e.join(', ')}]`)
//     return pathname + search
//   }
//   get: (path, init) => request(path, { ...init, method: 'GET' }),
//   post: (path, body) => request(path, { method: 'POST',
//     headers: { 'Content-Type': 'application/json' }, body: ... }),
//
// Three behaviours this reproduces EXACTLY:
//
// 1. NO prefixing. The SDK only VALIDATES the caller-given path against the
//    declared `permissions.api` list and fetches it unchanged. `App.tsx`
//    must therefore spell out `/apps/config-sync/api/...` itself (round 4:
//    the previous stub prepended a prefix the real SDK never adds).
// 2. The declared list is read from the REAL `app.json`, so an undeclared
//    path throws the real error message shape before `fetch` is reached.
// 3. `post(path, body)` takes exactly TWO positional arguments — its headers
//    are hardcoded to `{'Content-Type': 'application/json'}`; a caller can
//    never attach a custom header through it. (This is why the backend's
//    mutation guard is not a custom-header check — see `backend/server.py`.)
import manifest from '../../app.json'

export const APP_NAME: string = manifest.name

/** `app.json`'s declared `permissions.api` prefixes, exactly as the host
 * hands them to `Dse`. */
export const DECLARED_API_PATHS: string[] = manifest.permissions?.api ?? []

export interface AppApiClient {
  get: (path: string, init?: RequestInit) => Promise<unknown>
  post: (path: string, body?: unknown) => Promise<unknown>
  put: (path: string, body?: unknown) => Promise<unknown>
  patch: (path: string, body?: unknown) => Promise<unknown>
  del: (path: string) => Promise<unknown>
}

/** Port of `Dse`'s resolver: validate, never rewrite. */
export function resolvePath(
  path: string,
  declared: string[] = DECLARED_API_PATHS,
  appName: string = APP_NAME,
): string {
  if (/^(?:https?:)?\/\//i.test(path) || path.includes('\\')) {
    throw new Error(`[app-sdk] Absolute URLs are not allowed: ${path}`)
  }
  const url = new URL(path, 'http://localhost')
  const pathname = url.pathname
  const permitted = declared.some(
    (entry) => pathname === entry || pathname.startsWith(entry.endsWith('/') ? entry : entry + '/'),
  )
  if (!permitted) {
    throw new Error(
      `[app-sdk] App "${appName}" not permitted to access ${pathname}. Declared: [${declared.join(', ')}]`,
    )
  }
  return pathname + url.search
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

/** Build a client with the REAL SDK's exact surface — no prefixing, the
 * real permission check, and no third `headers` argument on
 * `post`/`put`/`patch`. */
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
