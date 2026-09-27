// The row of three stat cards (requirements.md 7.1): local changes, last
// push, and sync-from-main.

import type { FailureRecord, LastPush } from './types'
import { cardStyle, mutedStyle, buttonStyle } from './styles'

function formatTime(iso: string | null | undefined): string {
  if (!iso) return 'never'
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

export function LocalChangesCard({
  drift,
  onPushNow,
  pushing,
}: {
  drift: boolean
  onPushNow: () => void
  pushing: boolean
}) {
  return (
    <div style={cardStyle}>
      <h2 style={{ margin: 0, fontSize: '0.9rem' }}>Local changes</h2>
      <p style={{ fontSize: '1.5rem', margin: '0.5rem 0' }}>
        {drift ? 'Yes' : 'No changes yet'}
      </p>
      {!drift ? (
        <p style={mutedStyle}>Tracked tree matches the last pushed hash.</p>
      ) : (
        <button type="button" style={buttonStyle()} onClick={onPushNow} disabled={pushing}>
          {pushing ? 'Pushing…' : 'Push now'}
        </button>
      )}
    </div>
  )
}

export function LastPushCard({
  lastPush,
  failure,
}: {
  lastPush: LastPush | null
  failure: FailureRecord | null
}) {
  return (
    <div style={cardStyle}>
      <h2 style={{ margin: 0, fontSize: '0.9rem' }}>Last push</h2>
      {lastPush ? (
        <>
          <p style={{ margin: '0.5rem 0' }}>{formatTime(lastPush.time)}</p>
          <p>
            <a href={lastPush.pr_url} target="_blank" rel="noreferrer">
              {lastPush.pr_url}
            </a>
          </p>
          <p style={mutedStyle}>{lastPush.merged ? 'Merged' : 'Open — needs merging'}</p>
        </>
      ) : (
        <p style={mutedStyle}>No push yet — never pushed.</p>
      )}
      {failure ? (
        <p style={{ color: 'var(--danger)' }}>
          {failure.reason}
          {failure.time ? ` (${formatTime(failure.time)})` : null}
        </p>
      ) : null}
    </div>
  )
}

export function FromMainCard({
  lastSeenSha,
  pollFailure,
}: {
  lastSeenSha: string | null
  pollFailure: FailureRecord | null
}) {
  return (
    <div style={cardStyle}>
      <h2 style={{ margin: 0, fontSize: '0.9rem' }}>From main</h2>
      <p style={{ fontSize: '1.1rem', margin: '0.5rem 0' }}>Up to date</p>
      <p style={mutedStyle}>
        Last-seen sha: {lastSeenSha ? lastSeenSha.slice(0, 7) : 'none'}
      </p>
      {pollFailure ? (
        <div style={{ color: 'var(--danger)' }}>
          <p style={{ margin: '0.25rem 0' }}>
            {pollFailure.reason}
            {pollFailure.time ? ` (${formatTime(pollFailure.time)})` : null}
          </p>
          {/* Task 5: the cron runner pauses a job after 5 consecutive
           * failures (~75 min), but `status()`'s `last_poll_failure` is a
           * single {reason, time} record with no consecutive-failure
           * count or paused-state field (see backend/state.py) — there is
           * nothing in the live payload to derive that count from. Per
           * task scope, routes.py/state.py are not edited to add one; this
           * surfaces the best available signal (the poll is actively
           * failing, with when and why) and names the gap rather than
           * fabricating a count or a "paused" label the backend never
           * reports. */}
          <p style={mutedStyle}>
            The poll job is failing — see the reason above. Repeated
            failures may pause the poll job (KiroCrew pauses a cron after
            5 consecutive failures); the status response has no field
            reporting a consecutive-failure count or paused state.
          </p>
        </div>
      ) : null}
    </div>
  )
}
