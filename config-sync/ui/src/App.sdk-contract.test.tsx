// @vitest-environment jsdom
/**
 * FAILING real-SDK-contract harness (senior review round 4, finding C-B).
 *
 * The REAL host SDK factory (`/assets/App-*.js`, function `Dse(e, t, n)` —
 * `e` is `manifest.permissions.api`) adds NO prefix of its own: it only
 * validates the caller-given path against the declared list and passes it
 * to `fetch` UNCHANGED —
 *
 *   let r = n => {
 *     if (absolute-URL or backslash) throw Error(`[app-sdk] Absolute URLs
 *       are not allowed: ${n}`)
 *     let pathname = new URL(n, 'http://localhost').pathname
 *     if (!e.some(e => pathname === e || pathname.startsWith(
 *           e.endsWith('/') ? e : e + '/')))
 *       throw Error(`[app-sdk] App "${t}" not permitted to access
 *         ${pathname}. Declared: [${e.join(', ')}]`)
 *     return pathname + search
 *   }
 *
 * `realShapedAppApi.ts` / `test-stubs/app-sdk.ts` currently violate this:
 * they PREPEND a hardcoded `/api/apps/config-sync` prefix inside the stub
 * itself (a second round of the same over-capable-stub shape — `#86`).
 * This file builds a client from a LOCAL reimplementation of the real
 * `Dse` resolver (never importing `realShapedAppApi.ts`'s prefixing stub),
 * driven by the ACTUAL `app.json` on disk, and renders `App` against it —
 * so it fails today for the SAME two reasons the Python contract test
 * does: app.json has no `permissions.api` entry, and even once one is
 * added, `App.tsx`'s own call sites (`/status`, `/push`,
 * `/restore/<id>`) are bare, unprefixed, and are exactly what the real
 * resolver rejects.
 *
 * Mutation check (recorded in the task report, per
 * testing-standards.md § Mutation Requirement): the permission list this
 * harness reads is dropped to `[]` in a SCRATCH COPY under
 * $KIROCREW_SCRATCH (never in the source tree) to prove the assertion can
 * fail — see the task report for the red/green evidence. This file itself
 * never mutates app.json.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import realStatusFixture from './__fixtures__/status.real.json'

// ---------------------------------------------------------------------------
// app.json is read from disk ONCE at module load — never hand-typed —
// so a change to the manifest's declared permission is picked up by this
// harness automatically rather than silently drifting from what it checks.
// Resolved from `process.cwd()` (vitest runs with cwd == ui/) rather than
// `import.meta.url`, which this Vite/Node combination does not resolve to
// a `file:` URL reliably inside the test runner.
// ---------------------------------------------------------------------------
const APP_JSON_PATH = path.resolve(process.cwd(), '..', 'app.json')

interface AppManifest {
  permissions?: { api?: string[] }
}

function loadDeclaredApiPaths(): string[] {
  const raw = readFileSync(APP_JSON_PATH, 'utf-8')
  const manifest = JSON.parse(raw) as AppManifest
  return manifest.permissions?.api ?? []
}

/**
 * Byte-for-byte port of the real `Dse` resolver's validation branch —
 * never `realShapedAppApi.ts`'s prefixing stub. Adds no prefix; only
 * validates `path` against `declaredApiPaths` and throws the exact real
 * message shape on rejection.
 */
function resolveLikeRealSdk(declaredApiPaths: string[], appName: string, path: string): string {
  if (/^(?:https?:)?\/\//i.test(path) || path.includes('\\')) {
    throw new Error(`[app-sdk] Absolute URLs are not allowed: ${path}`)
  }
  const url = new URL(path, 'http://localhost')
  const pathname = url.pathname
  const permitted = declaredApiPaths.some(
    (declared) => pathname === declared || pathname.startsWith(declared.endsWith('/') ? declared : declared + '/'),
  )
  if (!permitted) {
    throw new Error(
      `[app-sdk] App "${appName}" not permitted to access ${pathname}. Declared: [${declaredApiPaths.join(', ')}]`,
    )
  }
  return pathname + url.search
}

interface RealSdkClient {
  get: (path: string, init?: RequestInit) => Promise<unknown>
  post: (path: string, body?: unknown) => Promise<unknown>
}

/** Build a client using ONLY the real-SDK-shaped resolver above — never

 * `realShapedAppApi.ts`'s prefixing stub. A call to an undeclared path
 * throws before `fetch` is ever reached, matching production. */
function buildRealSdkContractClient(declaredApiPaths: string[]): RealSdkClient {
  async function request(path: string, init?: RequestInit): Promise<unknown> {
    const resolved = resolveLikeRealSdk(declaredApiPaths, 'config-sync', path)
    const res = await fetch(resolved, init)
    if (!res.ok) {
      const text = await res.text().catch(() => res.statusText)
      throw new Error(`API ${res.status}: ${text}`)
    }
    if (res.status === 204 || res.status === 205) return undefined
    const text = await res.text()
    return text.trim() !== '' ? JSON.parse(text) : undefined
  }
  return {
    get: (path, init) => request(path, { ...init, method: 'GET' }),
    post: (path, body) =>
      request(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: body == null ? undefined : JSON.stringify(body),
      }),
  }
}

vi.mock('@kirocrew/app-sdk', () => {
  return {
    useAppApi: () => buildRealSdkContractClient(loadDeclaredApiPaths()),
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

function installFetchMock(routes: Record<string, () => unknown>) {
  const calls: Array<{ path: string; method: string }> = []
  const mockFetch = vi.fn(async (input: string | URL, init?: RequestInit) => {
    const path = String(input)
    const method = init?.method ?? 'GET'
    calls.push({ path, method })
    const key = `${method} ${path}`
    const handler = routes[key] ?? routes[path]
    if (!handler) {
      throw new Error(`unmocked fetch: ${key}`)
    }
    const body = handler()
    return {
      ok: true,
      status: 200,
      json: async () => body,
      text: async () => JSON.stringify(body),
    } as Response
  })
  vi.stubGlobal('fetch', mockFetch)
  return { calls }
}

beforeEach(() => {
  vi.restoreAllMocks()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

import App from './App'

describe('App against the REAL @kirocrew/app-sdk permission contract (round-4 C-B)', () => {
  it('app.json declares a non-empty permissions.api list', () => {
    const declared = loadDeclaredApiPaths()
    expect(declared.length).toBeGreaterThan(0)
  })

  it('app.json declares exactly the gateway-visible prefix /apps/config-sync/api', () => {
    const declared = loadDeclaredApiPaths()
    expect(declared).toEqual(['/apps/config-sync/api'])
  })

  it('renders the dashboard without the SDK throwing a permission error for /status', async () => {
    installFetchMock({
      'GET /apps/config-sync/api/status': () => realStatusFixture,
    })

    render(<App />)

    // If App.tsx still calls the bare, unprefixed `/status`, the real-SDK
    // resolver throws `[app-sdk] App "config-sync" not permitted to
    // access /status...` synchronously inside loadStatus()'s try/catch,
    // and that message — not the dashboard content — is what renders.
    await waitFor(() => {
      const permissionError = screen.queryByText(/not permitted to access/i)
      expect(permissionError).not.toBeInTheDocument()
    })
    await waitFor(() => {
      expect(screen.getByText(/local changes/i)).toBeInTheDocument()
    })
  })

  it('every mutating POST (Push now) reaches the declared-prefixed /push path with no permission error', async () => {
    const { calls } = installFetchMock({
      'GET /apps/config-sync/api/status': () => ({ ...realStatusFixture, drift: true }),
      'POST /apps/config-sync/api/push': () => ({ status: 'ok', outcome: 'pushed' }),
    })

    render(<App />)

    const pushButton = await screen.findByRole('button', { name: /push now/i })
    await userEvent.click(pushButton)

    await waitFor(() => {
      const pushCalls = calls.filter(
        (c) => c.path === '/apps/config-sync/api/push' && c.method === 'POST',
      )
      expect(pushCalls).toHaveLength(1)
    })

    expect(screen.queryByText(/not permitted to access/i)).not.toBeInTheDocument()
  })

  it('a call to an undeclared path throws the real SDK error message shape (mutation-style negative check)', () => {
    const declared = loadDeclaredApiPaths()
    expect(() => resolveLikeRealSdk(declared, 'config-sync', '/apps/some-other-app/api/status')).toThrow(
      /\[app-sdk\] App "config-sync" not permitted to access \/apps\/some-other-app\/api\/status\. Declared: \[/,
    )
  })

  it('a bare unprefixed call like /status (what App.tsx makes today) is rejected once permissions are declared', () => {
    const declared = loadDeclaredApiPaths()
    // This is the exact defect: App.tsx never spells out the declared
    // prefix at its own call sites. Once app.json declares
    // ['/apps/config-sync/api'], a bare '/status' no longer matches it.
    expect(() => resolveLikeRealSdk(declared, 'config-sync', '/status')).toThrow(/not permitted to access \/status/)
  })
})
