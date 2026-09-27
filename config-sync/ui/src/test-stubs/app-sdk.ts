// Trivial on-disk stand-ins for the host-injected `@kirocrew/app-sdk`
// modules, resolved via vitest.config.ts's `resolve.alias`. These exist
// solely so Vite's static import analysis has a real file to resolve
// `import { useAppApi } from '@kirocrew/app-sdk'` against in App.tsx — the
// actual behaviour under test always comes from `vi.mock('@kirocrew/app-sdk', ...)`
// in App.test.tsx, which vitest hoists and applies before this file's
// exports are ever used. Never imported directly by test code.
export function useAppApi(): never {
  throw new Error(
    '@kirocrew/app-sdk stub used without vi.mock — tests must mock this module',
  )
}
export function useAppEvents(): never {
  throw new Error(
    '@kirocrew/app-sdk stub used without vi.mock — tests must mock this module',
  )
}

// ---------------------------------------------------------------------------
// Real-SDK-shaped `useAppApi()` factory (senior review round 3, C-B).
//
// The REAL host SDK (`/assets/App-*.js`, function `Dse` building the value
// returned by `MF`'s `AppApiProvider`, exposed as `useAppApi()` -> `.api`)
// builds its client as:
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
// Two behaviours this stub reproduces EXACTLY, because they are the exact
// gap the previous stub hid:
//
// 1. Path prefixing: the real client resolves a caller-given path (e.g.
//    `/status`) against ONE base the host already knows (this app's own
//    `/api/apps/config-sync` prefix) — App.tsx never spells out the
//    `/api/apps/config-sync` prefix itself, the SDK adds it.
// 2. `post(path, body)` takes exactly TWO positional arguments. There is
//    NO THIRD `init`/`headers` parameter on the real `post` — its headers
//    are hardcoded to `{'Content-Type': 'application/json'}` inside the
//    SDK itself. A caller cannot pass a custom header through `post()`.
//    `get(path, init)` DOES forward a second `init` argument's `headers`
//    into the underlying `fetch`. This is why `backend/server.py`'s H7
//    mutation guard was redesigned around `Sec-Fetch-Site`/`Origin`
//    fetch-metadata instead of a custom header (see that module's
//    docstring) — this stub's `post()` intentionally has no way to attach
//    one, matching production, so any test relying on a header reaching
//    `fetch` via `post()`'s options fails here exactly as it would
//    against the real SDK.
export type { AppApiClient } from './realShapedAppApi'

// Re-exported from `realShapedAppApi.ts` (a file deliberately OUTSIDE the
// `@kirocrew/app-sdk` alias — see that file's header comment) rather than
// reimplemented here, so there is exactly one real-SDK-shaped client and
// no risk of the two copies drifting apart.
export { buildRealShapedAppApi } from './realShapedAppApi'
