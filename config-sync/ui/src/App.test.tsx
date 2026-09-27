// @vitest-environment jsdom
/**
 * Failing tests for tasks.md 6.3 — `ui/src/App.tsx`, the config-sync
 * dashboard page.
 *
 * REWRITTEN for the "Option A" status-page design (design.md's
 * "backend/routes.py and the UI" section; requirements.md Requirement 7,
 * including the operator ruling in the Introduction that supersedes the
 * old approve/decline gate). There is NO approve or decline route or
 * control anywhere in this design — the PR merge into
 * `Kiro-Config-Bundles` main is the approval; the box applies
 * automatically on the next 15-minute poll. This file replaces the
 * previous version, which tested an approve/decline UI that requirements.md's
 * Introduction explicitly says no longer exists.
 *
 * `App.tsx` is still the scaffold placeholder from `kirocrew app init`, so
 * every test below is expected to fail red for the right reason: either a
 * missing element the placeholder never renders, or an assertion about a
 * `fetch` call the placeholder never makes. `software-engineer` makes these
 * pass without editing this file.
 *
 * HTTP boundary mocked, not `@kirocrew/app-sdk`'s transport internals:
 * `@kirocrew/app-sdk` is a host-injected global, not an npm-resolvable
 * package, so it is mocked via `vi.mock` to hand back a `useAppApi()` whose
 * `get`/`post` are thin wrappers over global `fetch` against the app's own
 * route table (design.md's route table). Mocking at `fetch` — not mocking
 * `api.get`/`api.post` themselves — is what lets a test assert the exact
 * request path/method/body `App.tsx` sends. (Requirement 7.7's protection
 * is enforced server-side: the gateway HMAC plus browser fetch metadata —
 * the real SDK's `post()` cannot attach a custom header, see
 * realShapedAppApi.ts.)
 *
 * Route table (design.md, "backend/routes.py and the UI") — the
 * SDK-visible paths `App.tsx` calls; the real SDK adds NO prefix:
 *   - GET  /apps/config-sync/api/status
 *   - POST /apps/config-sync/api/push
 *   - POST /apps/config-sync/api/restore/{apply_id}
 *
 * There is deliberately NO approve/decline/pending route in this design.
 *
 * Response field names are copied from the CURRENT `backend/routes.py`
 * (`status()`, `_apply_result_to_dict()`) where the design and the code
 * already agree (`last_push`, `last_seen_sha`, `drift`,
 * `dropped_cron_names`, `paused_cron_names`, `needs_credential`,
 * `not_applied`, `propagation`, `apply_id`, `outcome`) — `routes.py` is
 * mid-refactor toward the auto-apply design (spec commit "auto-apply from
 * main; status-page dashboard" landed ahead of the route code), so a
 * `last_apply` summary field is used here as the documented shape for what
 * `status()` will carry once that refactor lands (design.md: "last-apply
 * summary (outcome, not-applied paths + reasons, changed_commands)"),
 * rather than the stale `pending`/`approve`/`decline` shape the old test
 * asserted against.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'

// ---------------------------------------------------------------------------
// Mock @kirocrew/app-sdk — not an npm package, resolved at runtime from a
// host-injected global. useAppApi().get/post wrap fetch against the app's
// own relative API paths, matching demo-app's bundle (`api.get('/api/agents')`).
// ---------------------------------------------------------------------------
vi.mock('@kirocrew/app-sdk', () => {
  async function request(method: 'GET' | 'POST', path: string, body?: unknown) {
    // Matches the REAL SDK's request shape (see `realShapedAppApi.ts`): the
    // path is fetched exactly as given (no prefix is added), and post()
    // hardcodes only Content-Type, with no caller-supplied header of any
    // kind.
    const headers: Record<string, string> = {}
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json'
    }
    const res = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    })
    const data = await res.json()
    if (!res.ok || data?.status === 'error') {
      throw new Error(data?.reason || `request to ${path} failed`)
    }
    return data
  }

  function useAppApi() {
    return {
      get: (path: string) => request('GET', path),
      post: (path: string, body?: unknown) => request('POST', path, body),
    }
  }

  function useAppEvents(_eventName: string, _handler: (...args: unknown[]) => void) {
    // No-op: no test here depends on a push-driven live event.
  }

  return { useAppApi, useAppEvents }
})

// `@kirocrew/app-sdk/ui` ships the host's own styled primitives
// (PageHeader/Card/CardTitle/StatCard) — not under test here, so the mock
// renders plain, semantics-preserving markup rather than reimplementing the
// host's design system.
vi.mock('@kirocrew/app-sdk/ui', () => {
  const PageHeader = ({ title, subtitle }: { title: string; subtitle?: string }) => (
    <header>
      <h1>{title}</h1>
      {subtitle ? <p>{subtitle}</p> : null}
    </header>
  )
  const Card = ({ children }: { children: ReactNode }) => <section>{children}</section>
  const CardTitle = ({ children }: { children: ReactNode }) => <h2>{children}</h2>
  const StatCard = ({
    label,
    value,
  }: {
    label: string
    value: ReactNode
    accent?: boolean
  }) => (
    <div>
      <span>{label}</span>
      <span>{value}</span>
    </div>
  )
  return { PageHeader, Card, CardTitle, StatCard }
})

// `lucide-react` is a host-provided icon set; App.tsx may import icons for
// buttons. Mock every icon as a trivial span so an unmocked icon name never
// crashes the render.
vi.mock('lucide-react', () => {
  const IconStub = () => <span data-testid="icon" />
  return new Proxy({}, { get: () => IconStub })
})

// ---------------------------------------------------------------------------
// Fixture data — the exact JSON shapes design.md and backend/routes.py
// define, not invented shapes. No `pending`/approve/decline anywhere: the
// operator ruling removed that box-side gate entirely.
// ---------------------------------------------------------------------------

const LAST_APPLY_FULL = {
  apply_id: 'apply-2026-09-26T22-00-00',
  outcome: 'applied',
  applied: ['steering/testing-standards.md', 'crons.json'],
  not_applied: {},
  sha: 'a1b2c3d4e5f60718293a4b5c6d7e8f901a2b3c4d',
  dropped_cron_names: ['unsafe-remote-wipe'],
  paused_cron_names: ['nightly-backup'],
  changed_commands: [
    { file: 'crons.json', name: 'nightly-backup', command: 'tar -czf /backup.tgz /data' },
  ],
  needs_credential: ['mcp.json:servers.github.headers.Authorization'],
  propagation: {
    'steering/testing-standards.md': {
      propagation_class: 'live_in_new_session',
      message: 'live in a new session',
      requires_restart: false,
    },
    'crons.json': {
      propagation_class: 'live_on_next_resolution',
      message: 'live on next resolution',
      requires_restart: false,
    },
  },
}

function statusResponse(overrides: Record<string, unknown> = {}) {
  return {
    status: 'ok',
    last_push: {
      time: '2026-09-20T08:00:00Z',
      branch: 'config-sync/instance-1-abc123',
      pr_url: 'https://github.com/TGS-Labs/Kiro-Config-Bundles/pull/99',
      merged: false,
    },
    last_push_failure: null,
    last_seen_sha: 'f1e2d3c4b5a69788716253748596a0b1c2d3e4f',
    last_poll_failure: null,
    drift: true,
    last_apply: null,
    ...overrides,
  }
}

// ---------------------------------------------------------------------------
// fetch mock — the HTTP boundary. Each test installs its own route table;
// an unrecognized path throws loudly rather than silently returning ok, so a
// mis-addressed request fails visibly.
// ---------------------------------------------------------------------------

type RouteHandler = (init?: RequestInit) => unknown

function installFetchMock(routes: Record<string, RouteHandler>) {
  const calls: Array<{ path: string; init?: RequestInit }> = []

  const mockFetch = vi.fn(async (input: string | URL, init?: RequestInit) => {
    const path = String(input)
    calls.push({ path, init })
    const key = `${init?.method ?? 'GET'} ${path}`
    const handler =
      routes[key] ?? routes[path] /* GET shorthand: route table may omit "GET " */
    if (!handler) {
      throw new Error(`unmocked fetch: ${key}`)
    }
    const body = handler(init)
    return {
      ok: true,
      status: 200,
      json: async () => body,
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

// ---------------------------------------------------------------------------
// Import App AFTER the mocks above are registered (vi.mock is hoisted by
// vitest, so this ordering is safe and matches the project's convention).
// ---------------------------------------------------------------------------
import App from './App'

describe('ConfigSync dashboard — no approve/decline anywhere (operator ruling, requirements.md Introduction + 4.4)', () => {
  it('never renders an approve or decline control, even with a fresh unmerged last_push', async () => {
    installFetchMock({
      'GET /apps/config-sync/api/status': () =>
        statusResponse({ last_apply: LAST_APPLY_FULL }),
    })

    render(<App />)

    await waitFor(() => {
      expect(screen.getByText(/local changes|drift/i)).toBeInTheDocument()
    })

    expect(screen.queryByRole('button', { name: /approve/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /decline/i })).not.toBeInTheDocument()
    expect(screen.queryByText(/approve/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/decline/i)).not.toBeInTheDocument()
  })

  it('never POSTs to a pending/approve or pending/decline path', async () => {
    const { calls } = installFetchMock({
      'GET /apps/config-sync/api/status': () =>
        statusResponse({ last_apply: LAST_APPLY_FULL }),
      'POST /apps/config-sync/api/push': () => ({ status: 'ok', outcome: 'pushed' }),
    })

    render(<App />)

    await waitFor(() => {
      expect(screen.getByText(/local changes|drift/i)).toBeInTheDocument()
    })

    const pushButton = screen.getByRole('button', { name: /push now/i })
    await userEvent.click(pushButton)

    await waitFor(() => {
      expect(calls.some((c) => c.init?.method === 'POST')).toBe(true)
    })

    for (const call of calls) {
      expect(call.path).not.toContain('/pending/')
      expect(call.path).not.toContain('/approve')
      expect(call.path).not.toContain('/decline')
    }
  })
})

describe('ConfigSync dashboard — three stat cards (Requirement 7.1)', () => {
  it('renders the local-changes card with drift state and a "Push now" button', async () => {
    installFetchMock({
      'GET /apps/config-sync/api/status': () => statusResponse({ drift: true }),
    })

    render(<App />)

    await waitFor(() => {
      expect(screen.getByText(/local changes/i)).toBeInTheDocument()
    })
    expect(screen.getByText(/yes/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /push now/i })).toBeInTheDocument()
  })

  it('renders the local-changes card as "no" drift when the hash matches', async () => {
    installFetchMock({
      'GET /apps/config-sync/api/status': () => statusResponse({ drift: false }),
    })

    render(<App />)

    await waitFor(() => {
      expect(screen.getByText(/local changes/i)).toBeInTheDocument()
    })
    expect(screen.getByText(/no/i)).toBeInTheDocument()
  })

  it('renders the last-push card with time, PR link, and unmerged status', async () => {
    installFetchMock({
      'GET /apps/config-sync/api/status': () =>
        statusResponse({
          last_push: {
            time: '2026-09-20T08:00:00Z',
            branch: 'config-sync/instance-1-abc123',
            pr_url: 'https://github.com/TGS-Labs/Kiro-Config-Bundles/pull/99',
            merged: false,
          },
        }),
    })

    render(<App />)

    await waitFor(() => {
      expect(screen.getByText(/last push/i)).toBeInTheDocument()
    })
    expect(
      screen.getByText(/github\.com\/TGS-Labs\/Kiro-Config-Bundles\/pull\/99/),
    ).toBeInTheDocument()
    expect(screen.getByText(/needs merging|open|not.*merged/i)).toBeInTheDocument()
  })

  it('renders the last-push card as merged when the PR has merged', async () => {
    installFetchMock({
      'GET /apps/config-sync/api/status': () =>
        statusResponse({
          last_push: {
            time: '2026-09-20T08:00:00Z',
            branch: 'config-sync/instance-1-abc123',
            pr_url: 'https://github.com/TGS-Labs/Kiro-Config-Bundles/pull/99',
            merged: true,
          },
        }),
    })

    render(<App />)

    await waitFor(() => {
      expect(screen.getByText(/last push/i)).toBeInTheDocument()
    })
    expect(screen.getByText(/merged/i)).toBeInTheDocument()
  })

  it('renders the sync-from-main card with up-to-date state, last-seen sha, and check time', async () => {
    installFetchMock({
      'GET /apps/config-sync/api/status': () =>
        statusResponse({
          last_seen_sha: 'f1e2d3c4b5a69788716253748596a0b1c2d3e4f',
        }),
    })

    render(<App />)

    await waitFor(() => {
      expect(screen.getByText(/from main|sync/i)).toBeInTheDocument()
    })
    expect(screen.getByText(/up to date/i)).toBeInTheDocument()
    expect(screen.getByText(/f1e2d3c/i)).toBeInTheDocument()
  })
})

describe('ConfigSync dashboard — Push now issues the push POST (Requirement 7.1)', () => {
  it('POSTs /apps/config-sync/api/push as JSON via the real post() surface on click', async () => {
    const { calls } = installFetchMock({
      'GET /apps/config-sync/api/status': () => statusResponse({ drift: true }),
      'POST /apps/config-sync/api/push': () => ({ status: 'ok', outcome: 'pushed' }),
    })

    render(<App />)

    const pushButton = await screen.findByRole('button', { name: /push now/i })
    await userEvent.click(pushButton)

    await waitFor(() => {
      const pushCalls = calls.filter(
        (c) => c.path === '/apps/config-sync/api/push' && c.init?.method === 'POST',
      )
      expect(pushCalls).toHaveLength(1)
    })

    const pushCall = calls.find(
      (c) => c.path === '/apps/config-sync/api/push' && c.init?.method === 'POST',
    )
    expect(pushCall?.init?.method).toBe('POST')
    expect(pushCall?.path).toBe('/apps/config-sync/api/push')
  })
})

describe('ConfigSync dashboard — last-apply card and Undo (Requirement 7.2)', () => {
  it('shows the merged PR link, paused crons with their command, and needs-credential keys', async () => {
    installFetchMock({
      'GET /apps/config-sync/api/status': () =>
        statusResponse({ last_apply: LAST_APPLY_FULL }),
    })

    render(<App />)

    await waitFor(() => {
      expect(screen.getByText(/last apply/i)).toBeInTheDocument()
    })
    expect(
      screen.getByText(/github\.com\/TGS-Labs\/Kiro-Config-Bundles\/pull\/99/),
    ).toBeInTheDocument()
    expect(screen.getByText(/nightly-backup/)).toBeInTheDocument()
    expect(screen.getByText(/tar -czf \/backup\.tgz \/data/)).toBeInTheDocument()
    expect(
      screen.getByText(/mcp\.json:servers\.github\.headers\.Authorization/),
    ).toBeInTheDocument()
  })

  it('shows not-applied paths with their real per-path reasons on a partial outcome', async () => {
    installFetchMock({
      'GET /apps/config-sync/api/status': () =>
        statusResponse({
          last_apply: {
            ...LAST_APPLY_FULL,
            outcome: 'partial',
            applied: ['steering/testing-standards.md'],
            not_applied: {
              'mcp.json': 'command vet rejected server "evil": rm -rf ~',
            },
          },
        }),
    })

    render(<App />)

    await waitFor(() => {
      expect(
        screen.getByText(/command vet rejected server "evil"/i),
      ).toBeInTheDocument()
    })
    expect(screen.getAllByText(/mcp\.json/).length).toBeGreaterThan(0)
    // Must not report a partial outcome as a plain success.
    expect(screen.queryByText(/^applied$/i)).not.toBeInTheDocument()
  })

  it('renders an "Undo this apply" button that POSTs restore/<id> via the real post() surface', async () => {
    const { calls } = installFetchMock({
      'GET /apps/config-sync/api/status': () =>
        statusResponse({ last_apply: LAST_APPLY_FULL }),
      [`POST /apps/config-sync/api/restore/${LAST_APPLY_FULL.apply_id}`]: () => ({
        status: 'ok',
        restored: { A: ['steering/testing-standards.md'], B: [] },
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
      `/apps/config-sync/api/restore/${LAST_APPLY_FULL.apply_id}`,
    )
    expect(undoCall?.init?.method).toBe('POST')
  })

  it('does not render a last-apply card or Undo button when nothing has ever been applied', async () => {
    installFetchMock({
      'GET /apps/config-sync/api/status': () => statusResponse({ last_apply: null }),
    })

    render(<App />)

    await waitFor(() => {
      expect(screen.getByText(/local changes/i)).toBeInTheDocument()
    })
    expect(screen.getByText(/no apply yet|not applied yet|never applied/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /undo/i })).not.toBeInTheDocument()
  })
})

describe('ConfigSync dashboard — propagation timing chips render distinctly (Requirement 5.9, 7.3)', () => {
  it('renders all four propagation chips with counts, never collapsed into one status', async () => {
    installFetchMock({
      'GET /apps/config-sync/api/status': () =>
        statusResponse({
          last_apply: {
            ...LAST_APPLY_FULL,
            applied: [
              'skills/foo/SKILL.md',
              'skills/foo/scripts/run.sh',
              'steering/testing-standards.md',
              'config.json',
            ],
            propagation: {
              'skills/foo/SKILL.md': {
                propagation_class: 'live_immediate',
                message: 'live now',
                requires_restart: false,
              },
              'skills/foo/scripts/run.sh': {
                propagation_class: 'live_within_60s',
                message: 'live within 60s',
                requires_restart: false,
              },
              'steering/testing-standards.md': {
                propagation_class: 'live_in_new_session',
                message: 'live in a new session',
                requires_restart: false,
              },
              'config.json': {
                propagation_class: 'live_on_next_resolution',
                message: 'live on next resolution',
                requires_restart: false,
              },
            },
          },
        }),
    })

    render(<App />)

    await waitFor(() => {
      expect(screen.getByText(/live now/i)).toBeInTheDocument()
    })

    const chipTexts = [/live now/i, /live within 60s/i, /live in a new session/i, /live on next resolution/i]
    const rendered = chipTexts.map((re) => screen.getByText(re).textContent)
    // Distinctness: none of the four chip labels collapse into each other.
    expect(new Set(rendered).size).toBe(4)

    // Each chip carries a count of how many applied paths fall in that class
    // (one path per class in this fixture) — assert the digit "1" appears
    // within each chip's own container rather than merely on the page.
    for (const re of chipTexts) {
      const chip = screen.getByText(re).closest('div,section,span') ?? document.body
      expect(within(chip as HTMLElement).getByText(/1/)).toBeInTheDocument()
    }
  })
})

describe('ConfigSync dashboard — Requirement 6 exception call-out', () => {
  it('shows the deliberate-exception call-out when the last-applied range touched crons.json', async () => {
    installFetchMock({
      'GET /apps/config-sync/api/status': () =>
        statusResponse({ last_apply: LAST_APPLY_FULL }),
    })

    render(<App />)

    await waitFor(() => {
      expect(screen.getByText(/last apply/i)).toBeInTheDocument()
    })
    expect(screen.getByText(/deliberate/i)).toBeInTheDocument()
    expect(screen.getByText(/instance isolation|isolation/i)).toBeInTheDocument()
  })

  it('shows the call-out when the last-applied range touched instances.json', async () => {
    installFetchMock({
      'GET /apps/config-sync/api/status': () =>
        statusResponse({
          last_apply: {
            ...LAST_APPLY_FULL,
            applied: ['instances.json'],
            changed_instance_names: ['prod-box'],
            propagation: {
              'instances.json': {
                propagation_class: 'live_on_next_resolution',
                message: 'live on next resolution',
                requires_restart: false,
              },
            },
          },
        }),
    })

    render(<App />)

    await waitFor(() => {
      expect(screen.getByText(/prod-box/i)).toBeInTheDocument()
    })
    expect(screen.getByText(/deliberate/i)).toBeInTheDocument()
  })

  it('does NOT show the exception call-out when the last-applied range touched neither file', async () => {
    installFetchMock({
      'GET /apps/config-sync/api/status': () =>
        statusResponse({
          last_apply: {
            ...LAST_APPLY_FULL,
            applied: ['steering/testing-standards.md'],
            dropped_cron_names: [],
            paused_cron_names: [],
            changed_instance_names: [],
            propagation: {
              'steering/testing-standards.md': {
                propagation_class: 'live_in_new_session',
                message: 'live in a new session',
                requires_restart: false,
              },
            },
          },
        }),
    })

    render(<App />)

    await waitFor(() => {
      expect(screen.getByText(/last apply/i)).toBeInTheDocument()
    })
    expect(screen.queryByText(/deliberate/i)).not.toBeInTheDocument()
  })
})

describe('ConfigSync dashboard — error and empty states in plain language (Requirement 7.4, 4.8)', () => {
  it('shows an error in plain language when the status request fails, rather than swallowing it', async () => {
    const mockFetch = vi.fn(async () => ({
      ok: false,
      status: 500,
      json: async () => ({ status: 'error', reason: 'config-sync is disabled' }),
    })) as unknown as typeof fetch
    vi.stubGlobal('fetch', mockFetch)

    render(<App />)

    await waitFor(() => {
      expect(screen.getByText(/config-sync is disabled|error/i)).toBeInTheDocument()
    })
    expect(screen.queryByText(/your app content goes here/i)).not.toBeInTheDocument()
  })

  it('shows plain-language empty state when there is no drift, no push yet, and nothing applied', async () => {
    installFetchMock({
      'GET /apps/config-sync/api/status': () =>
        statusResponse({
          drift: false,
          last_push: null,
          last_apply: null,
        }),
    })

    render(<App />)

    await waitFor(() => {
      expect(screen.getByText(/local changes/i)).toBeInTheDocument()
    })
    expect(screen.getByText(/no changes yet|nothing to push|no local changes/i)).toBeInTheDocument()
    expect(screen.getByText(/no push yet|never pushed/i)).toBeInTheDocument()
    expect(screen.getByText(/no apply yet|not applied yet|never applied/i)).toBeInTheDocument()
  })

  it('surfaces a push failure in plain language without claiming success', async () => {
    installFetchMock({
      'GET /apps/config-sync/api/status': () =>
        statusResponse({
          last_push_failure: {
            reason: 'secret scan found 1 finding; push refused',
            time: '2026-09-27T07:17:42Z',
          },
        }),
    })

    render(<App />)

    await waitFor(() => {
      expect(
        screen.getByText(/secret scan found 1 finding; push refused/i),
      ).toBeInTheDocument()
    })
  })

  it('surfaces a poll failure in plain language', async () => {
    installFetchMock({
      'GET /apps/config-sync/api/status': () =>
        statusResponse({
          last_poll_failure: {
            reason: 'could not reach bundle repository: timeout',
            time: '2026-09-27T07:17:42Z',
          },
        }),
    })

    render(<App />)

    await waitFor(() => {
      expect(
        screen.getByText(/could not reach bundle repository: timeout/i),
      ).toBeInTheDocument()
    })
  })

  it('shows the consecutive poll-failure count and points at the Schedule page at the host threshold', async () => {
    installFetchMock({
      'GET /apps/config-sync/api/status': () =>
        statusResponse({
          last_poll_failure: {
            reason: 'could not reach bundle repository: timeout',
            time: '2026-09-27T07:17:42Z',
          },
          poll_consecutive_failures: 5,
        }),
    })

    render(<App />)

    await waitFor(() => {
      expect(screen.getByText(/5 consecutive poll failures/i)).toBeInTheDocument()
    })
    expect(screen.getByText(/re-enable config-sync-poll on the Schedule/i)).toBeInTheDocument()
    expect(screen.getByText(/^Poll failing$/)).toBeInTheDocument()
    expect(screen.queryByText(/^Up to date$/)).toBeNull()
    expect(screen.queryByText(/Polling paused/i)).toBeNull()
  })

  it('does not mention the Schedule page below the host threshold', async () => {
    installFetchMock({
      'GET /apps/config-sync/api/status': () =>
        statusResponse({
          last_poll_failure: {
            reason: 'could not reach bundle repository: timeout',
            time: '2026-09-27T07:17:42Z',
          },
          poll_consecutive_failures: 2,
        }),
    })

    render(<App />)

    await waitFor(() => {
      expect(screen.getByText(/2 consecutive poll failures/i)).toBeInTheDocument()
    })
    expect(screen.queryByText(/Schedule page/i)).toBeNull()
  })
})

describe('ConfigSync dashboard — no raw credential-looking values rendered (Requirement 7.5, 3.8, 7.6)', () => {
  it('never renders an unredacted-looking secret value from an unknown status field', async () => {
    const leakedShapedToken = 'ghp_ThisLooksLikeARealGitHubPAT1234567890'

    installFetchMock({
      'GET /apps/config-sync/api/status': () =>
        statusResponse({
          drift: false,
          // Not a documented status field — simulates a backend regression
          // where a raw value leaks onto an unexpected key. The UI must not
          // render arbitrary/unknown response fields verbatim.
          _debug_leak: leakedShapedToken,
        }),
    })

    render(<App />)

    await waitFor(() => {
      expect(screen.getByText(/local changes/i)).toBeInTheDocument()
    })
    expect(screen.queryByText(leakedShapedToken)).not.toBeInTheDocument()
  })

  it('never renders the literal "<redacted>" placeholder as if it were a live credential value', async () => {
    // needs_credential entries are KEY PATHS (server/job name + key), never
    // the placeholder value itself.
    installFetchMock({
      'GET /apps/config-sync/api/status': () =>
        statusResponse({
          last_apply: {
            ...LAST_APPLY_FULL,
            needs_credential: ['mcp.json:servers.newserver.headers.Authorization'],
          },
        }),
    })

    render(<App />)

    await waitFor(() => {
      expect(
        screen.getByText(/mcp\.json:servers\.newserver\.headers\.Authorization/),
      ).toBeInTheDocument()
    })
    expect(screen.queryByText('<redacted>')).not.toBeInTheDocument()
  })
})

describe('ConfigSync dashboard — accessibility (buttons have accessible names)', () => {
  it('every rendered button has an accessible name (queryable by role)', async () => {
    installFetchMock({
      'GET /apps/config-sync/api/status': () =>
        statusResponse({ last_apply: LAST_APPLY_FULL }),
    })

    render(<App />)

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /push now/i })).toBeInTheDocument()
    })

    const buttons = screen.getAllByRole('button')
    expect(buttons.length).toBeGreaterThan(0)
    for (const button of buttons) {
      const accessibleName = button.getAttribute('aria-label') || button.textContent
      expect(accessibleName?.trim()).toBeTruthy()
    }
  })
})
