// See app-sdk.ts — same rationale, for `lucide-react`. Always overridden
// by vi.mock in App.test.tsx (which returns a Proxy handing back an icon
// stub for any imported name), so this file's own exports are never
// actually exercised.
export {}
