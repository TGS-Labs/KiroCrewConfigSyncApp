// See app-sdk.ts — same rationale, for the `@kirocrew/app-sdk/ui` entry
// point. Always overridden by vi.mock in App.test.tsx.
export function PageHeader(): never {
  throw new Error(
    '@kirocrew/app-sdk/ui stub used without vi.mock — tests must mock this module',
  )
}
export function Card(): never {
  throw new Error(
    '@kirocrew/app-sdk/ui stub used without vi.mock — tests must mock this module',
  )
}
export function CardTitle(): never {
  throw new Error(
    '@kirocrew/app-sdk/ui stub used without vi.mock — tests must mock this module',
  )
}
export function StatCard(): never {
  throw new Error(
    '@kirocrew/app-sdk/ui stub used without vi.mock — tests must mock this module',
  )
}
