// @vitest-environment jsdom
/**
 * FAILING real-contract seam test (senior review round 3, finding C-B).
 *
 * `App.test.tsx`'s own `vi.mock('@kirocrew/app-sdk', ...)` reimplements
 * `useAppApi()` inline with whatever request shape it wants (custom
 * headers threaded through `post()`'s third argument, no path
 * prefixing) — so passing tests there prove nothing about what the REAL
 * host SDK (`/assets/App-*.js`'s `Dse`/`MF`, exposed via
 * `@kirocrew/app-sdk`'s `useAppApi()`) actually does. This file instead:
 *
 *   1. Renders `App` against `test-stubs/app-sdk.ts`'s
 *      `buildRealShapedAppApi()` — a stub with the REAL SDK's exact
 *      surface (`post(path, body)` takes NO third `headers`/`init`
 *      argument; `get(path, init)` DOES forward `init.headers`; every
 *      path is prefixed with the app's own `/api/apps/config-sync`
 *      base) — not the ad-hoc mock `App.test.tsx` writes for itself.
 *   2. Feeds it the REAL `status()` JSON captured from an actual poll
 *      tick (`tests/test_fixture_status_real.py`,
 *      `ui/src/__fixtures__/status.real.json`), not hand-written
 *      fixture data.
 *
 * It MUST fail today (before `App.tsx`/`types.ts`/`LastApplyCard.tsx` are
 * fixed to the real field names): they assume fields the real payload does
 * not have (`merged_sha`, `pr_urls`, a list-shaped `not_applied`, a string
 * `last_push_failure`, a top-level `applying` boolean) — see the real
 * fixture's actual shape: `not_applied` is `{relpath: reason}`, the applied
 * sha is `sha`, there is no `pr_urls` field at all, `last_push_failure` is
 * `{reason, time}`, and `status()` never returns `applying`.
 *
 * Does NOT prescribe how `App.tsx`/`types.ts`/component fixes land — only
 * proves the current/fixed code is right (or wrong) against the real
 * contract, including the real two-argument `post()` signature the previous
 * harness's bespoke mock let slide (see `realShapedAppApi.ts`).
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import realStatusFixture from './__fixtures__/status.real.json'
import * as appSdkStub from './realShapedAppApi'

// ---------------------------------------------------------------------------
// Mock @kirocrew/app-sdk with the REAL-SHAPED client, not a bespoke one.
// `useAppApi()` returns exactly `buildRealShapedAppApi()`'s object —
// `get(path, init)` / `post(path, body)` with the real two-arg `post`
// signature — and `fetch` is the only boundary this test mocks, matching
// how the real SDK itself ultimately calls global `fetch`.
//
// The stub module is imported ABOVE, before `vi.mock` runs, and captured
// in `appSdkStub` (rather than re-imported inside the mock factory):
// `vitest.config.ts` aliases the bare specifier `@kirocrew/app-sdk` to
// this exact stub file, so a dynamic `import('./test-stubs/app-sdk')`
// *inside* the mock factory resolves through vitest's own mock registry
// back to the mock being defined — an infinite/undefined self-reference.
// Capturing the real module object via the top-level import (which
// vitest hoists `vi.mock` above, but which still resolves the concrete
// file on disk once, before the aliasing applies to the mock target
// itself) avoids that.
vi.mock('@kirocrew/app-sdk', () => {
  return {
    useAppApi: () => appSdkStub.buildRealShapedAppApi(),
    useAppEvents: (_eventName: string, _handler: (...args: unknown[]) => void) => {
      // No-op: no test here depends on a push-driven live event.
    },
  }
})

vi.mock('@kirocrew/app-sdk/ui', () => {
  const PageHeader = ({ title, subtitle }: { title: string; subtitle?: string }) => (
    <header>
      <h1>{title}</h1>
      {subtitle ? <p>{subtitle}</p> : null}
    </header>
  )
  const Card = ({ children }: { children: ReactNode }) => <section>{children}</section>
  const CardTitle = ({ children }: { children: ReactNode }) => <h2>{children}</h2>
  const StatCard = ({ label, value }: { label: string; value: ReactNode; accent?: boolean }) => (
    <div>
      <span>{label}</span>
      <span>{value}</span>
    </div>
  )
  return { PageHeader, Card, CardTitle, StatCard }
})

vi.mock('lucide-react', () => {
  const IconStub = () => <span data-testid="icon" />
  return new Proxy({}, { get: () => IconStub })
})

type RouteHandler = (init?: RequestInit) => unknown

function installFetchMock(routes: Record<string, RouteHandler>) {
  const calls: Array<{ path: string; init?: RequestInit }> = []

  const mockFetch = vi.fn(async (input: string | URL, init?: RequestInit) => {
    const path = String(input)
    calls.push({ path, init })
    const key = `${init?.method ?? 'GET'} ${path}`
    const handler = routes[key] ?? routes[path]
    if (!handler) {
      throw new Error(`unmocked fetch: ${key}`)
    }
    const body = handler(init)
    return {
      ok: true,
      status: 200,
      json: async () => body,
      text: async () => JSON.stringify(body),
    } as Response
  })

  vi.stubGlobal('fetch', mockFetch)
  return { calls, mockFetch }
}

beforeEach(() => {
  vi.restoreAllMocks()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

import App from './App'

describe('ConfigSync dashboard against the REAL status() payload + REAL-shaped SDK (round-3 C-B)', () => {
  it('renders without crashing on the real fixture and shows the three stat cards', async () => {
    installFetchMock({
      'GET /api/apps/config-sync/status': () => realStatusFixture,
    })

    render(<App />)

    await waitFor(() => {
      expect(screen.getByText(/local changes/i)).toBeInTheDocument()
    })
    expect(screen.getByText(/last push/i)).toBeInTheDocument()
    expect(screen.getByText(/from main|sync/i)).toBeInTheDocument()
  })

  it('renders the last-apply card using the REAL not_applied dict shape and its real reason string', async () => {
    installFetchMock({
      'GET /api/apps/config-sync/status': () => realStatusFixture,
    })

    render(<App />)

    await waitFor(() => {
      expect(screen.getByText(/last apply/i)).toBeInTheDocument()
    })

    const realLastApply = (realStatusFixture as { last_apply: Record<string, unknown> })
      .last_apply
    const notApplied = realLastApply.not_applied as Record<string, string>
    const [relpath, reason] = Object.entries(notApplied)[0]

    // The real not-applied path and its REAL per-path reason must both
    // render — not a placeholder, not an empty list rendering nothing.
    expect(screen.getByText(new RegExp(relpath.replace('.', '\\.')))).toBeInTheDocument()
    expect(screen.getByText(new RegExp(reason.slice(0, 20).replace(/[-[\]{}()*+?.,\\^$|#\s]/g, '.')))).toBeInTheDocument()
  })

  it('renders the applied sha from the real `sha` field, not a nonexistent `merged_sha`', async () => {
    installFetchMock({
      'GET /api/apps/config-sync/status': () => realStatusFixture,
    })

    render(<App />)

    await waitFor(() => {
      expect(screen.getByText(/last apply/i)).toBeInTheDocument()
    })

    const realLastApply = (realStatusFixture as { last_apply: Record<string, unknown> })
      .last_apply
    const sha = realLastApply.sha as string

    expect(screen.getByText(new RegExp(sha.slice(0, 7)))).toBeInTheDocument()
  })

  it('renders the real last_push_failure object ({reason, time}), not a bare string', async () => {
    installFetchMock({
      'GET /api/apps/config-sync/status': () => realStatusFixture,
    })

    render(<App />)

    const failure = (realStatusFixture as { last_push_failure: { reason: string } })
      .last_push_failure

    await waitFor(() => {
      expect(screen.getByText(new RegExp(failure.reason))).toBeInTheDocument()
    })
  })

  it('does not crash or render "undefined"/"[object Object]" anywhere on the real payload', async () => {
    installFetchMock({
      'GET /api/apps/config-sync/status': () => realStatusFixture,
    })

    render(<App />)

    await waitFor(() => {
      expect(screen.getByText(/local changes/i)).toBeInTheDocument()
    })

    expect(screen.queryByText(/undefined/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/\[object Object\]/)).not.toBeInTheDocument()
  })

  it('every mutating POST (Push now) hits the real prefixed path — using the REAL post() signature', async () => {
    const { calls } = installFetchMock({
      'GET /api/apps/config-sync/status': () => ({ ...realStatusFixture, drift: true }),
      'POST /api/apps/config-sync/push': () => ({ status: 'ok', outcome: 'pushed' }),
    })

    render(<App />)

    const pushButton = await screen.findByRole('button', { name: /push now/i })
    await userEvent.click(pushButton)

    await waitFor(() => {
      const pushCalls = calls.filter(
        (c) => c.path === '/api/apps/config-sync/push' && c.init?.method === 'POST',
      )
      expect(pushCalls).toHaveLength(1)
    })

    const pushCall = calls.find(
      (c) => c.path === '/api/apps/config-sync/push' && c.init?.method === 'POST',
    )
    // C-B: the real SDK's post(path, body) has no headers parameter at
    // all, so App.tsx cannot and does not attach a custom header here —
    // backend/server.py's H7 guard was redesigned to rely on
    // Sec-Fetch-Site/Origin fetch-metadata instead (see that module's
    // docstring), neither of which any app UI code can or needs to set:
    // a real browser adds them automatically on every same-origin fetch.
    // This test only proves the call reaches the correctly-prefixed path
    // with the real two-arg post() signature.
    expect(pushCall?.path).toBe('/api/apps/config-sync/push')
  })

  it('every mutating POST (Undo) hits the real restore/<apply_id> path — using the REAL post() signature', async () => {
    const realLastApply = (realStatusFixture as { last_apply: { apply_id: string } })
      .last_apply
    const { calls } = installFetchMock({
      'GET /api/apps/config-sync/status': () => realStatusFixture,
      [`POST /api/apps/config-sync/restore/${realLastApply.apply_id}`]: () => ({
        status: 'ok',
        restored: { A: [], B: [] },
        removed: { A: [], B: [] },
        failed: [],
      }),
    })

    render(<App />)

    const undoButton = await screen.findByRole('button', { name: /undo this apply/i })
    await userEvent.click(undoButton)

    await waitFor(() => {
      const undoCalls = calls.filter(
        (c) => c.path.includes('/restore/') && c.init?.method === 'POST',
      )
      expect(undoCalls).toHaveLength(1)
    })

    const undoCall = calls.find(
      (c) => c.path.includes('/restore/') && c.init?.method === 'POST',
    )
    expect(undoCall?.path).toBe(
      `/api/apps/config-sync/restore/${realLastApply.apply_id}`,
    )
  })
})
