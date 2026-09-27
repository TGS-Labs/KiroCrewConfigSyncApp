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
