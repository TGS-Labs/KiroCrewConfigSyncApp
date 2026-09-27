import { useAppApi } from '@kirocrew/app-sdk'
import { PageHeader } from '@kirocrew/app-sdk/ui'
import { useEffect, useRef, useState } from 'react'
import type { StatusResponse } from './types'
import { LocalChangesCard, LastPushCard, FromMainCard } from './StatCardsRow'
import LastApplyCard from './LastApplyCard'
import { cardStyle, mutedStyle } from './styles'

/** The browser-visible API base. Must equal `app.json`'s
 * `permissions.api[0]`: the real `@kirocrew/app-sdk` adds NO prefix and
 * refuses any path outside the declared list, and the gateway forwards
 * `/apps/config-sync/api/<x>` to this app's backend as `/api/<x>`. */
const API_BASE = '/apps/config-sync/api'

export default function ConfigSync() {
  const api = useAppApi()
  // `useAppApi()` returns a fresh object every render (see the host SDK
  // mock), so `api` itself is not a stable dependency — read it through a
  // ref instead of putting it in a `useEffect` dependency array, which
  // would otherwise re-run the fetch effect on every render and spin an
  // infinite render loop.
  const apiRef = useRef(api)
  apiRef.current = api
  // Every call site names its route relative to `API_BASE`; this is the
  // one place the full SDK-visible path is composed.
  const client = {
    get: (route: string) => apiRef.current.get(`${API_BASE}${route}`),
    post: (route: string) => apiRef.current.post(`${API_BASE}${route}`),
  }

  const [status, setStatus] = useState<StatusResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [pushing, setPushing] = useState(false)
  const [undoing, setUndoing] = useState(false)

  async function loadStatus() {
    try {
      const data = (await client.get('/status')) as StatusResponse
      setStatus(data)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadStatus()
    // Deliberately empty: `loadStatus` closes over `apiRef` (stable) and
    // setters (stable); it must run exactly once on mount, not on every
    // render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function handlePushNow() {
    setPushing(true)
    try {
      await client.post('/push')
      await loadStatus()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setPushing(false)
    }
  }

  async function handleUndo(applyId: string) {
    setUndoing(true)
    try {
      await client.post(`/restore/${applyId}`)
      await loadStatus()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setUndoing(false)
    }
  }

  return (
    <>
      <PageHeader title="Config bundle status" subtitle="Keeps this box aligned with Kiro-Config-Bundles" />
      <div className="px-6 pb-8 overflow-y-auto flex-1 min-h-0">
        {loading ? (
          <p style={mutedStyle}>Loading…</p>
        ) : error ? (
          <div style={cardStyle}>
            <p style={{ color: 'var(--danger)' }}>{error}</p>
          </div>
        ) : status ? (
          <>
            <div
              style={{
                display: 'grid',
                gap: '0.875rem',
                gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))',
                marginBottom: '1.5rem',
              }}
            >
              <LocalChangesCard drift={status.drift} onPushNow={handlePushNow} pushing={pushing} />
              <LastPushCard lastPush={status.last_push} failure={status.last_push_failure} />
              <FromMainCard
                lastSeenSha={status.last_seen_sha}
                pollFailure={status.last_poll_failure}
                consecutiveFailures={status.poll_consecutive_failures}
                pollPaused={status.poll_paused}
              />
            </div>

            <LastApplyCard lastApply={status.last_apply} onUndo={handleUndo} undoing={undoing} />
          </>
        ) : null}
      </div>
    </>
  )
}
