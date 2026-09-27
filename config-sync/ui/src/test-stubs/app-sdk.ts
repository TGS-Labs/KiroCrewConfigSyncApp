// Trivial on-disk stand-ins for the host-injected `@kirocrew/app-sdk`
// modules, resolved via vitest.config.ts's `resolve.alias`. These exist
// solely so Vite's static import analysis has a real file to resolve
// `import { useAppApi } from '@kirocrew/app-sdk'` against in App.tsx — the
// actual behaviour under test always comes from `vi.mock('@kirocrew/app-sdk', ...)`
// in each test file, which vitest hoists and applies before this file's
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

// Real-SDK-shaped `useAppApi()` factory (senior review rounds 3-4, C-B).
//
// Mirrors the REAL host SDK's `Dse` factory exactly — NO path prefixing, the
// real `permissions.api` check against the real `app.json` (same error
// message shape), the path fetched as given, and a two-argument `post()`
// with no caller-supplied headers. Re-exported from `realShapedAppApi.ts`
// (a file deliberately OUTSIDE the `@kirocrew/app-sdk` alias — see that
// file's header comment, which carries the verified `Dse` excerpt) rather
// than reimplemented here, so there is exactly one real-SDK-shaped client
// and no risk of the two copies drifting apart.
export type { AppApiClient } from '../realShapedAppApi'
export { buildRealShapedAppApi, resolvePath, DECLARED_API_PATHS } from '../realShapedAppApi'
