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

/** KiroCrew's cron runner switches a command cron off after this many
 * consecutive failures (`kiro_crew/cron.py` `_AUTO_PAUSE_THRESHOLD`). The
 * app has no pause of its own; at this count the card points the operator
 * to the Schedule page, where the job is re-enabled. */
const HOST_CRON_AUTO_PAUSE_AFTER = 5

export function FromMainCard({
  lastSeenSha,
  pollFailure,
  consecutiveFailures,
}: {
  lastSeenSha: string | null
  pollFailure: FailureRecord | null
  consecutiveFailures: number
}) {
  const likelyPausedByHost = consecutiveFailures >= HOST_CRON_AUTO_PAUSE_AFTER
  return (
    <div style={cardStyle}>
      <h2 style={{ margin: 0, fontSize: '0.9rem' }}>From main</h2>
      <p style={{ fontSize: '1.1rem', margin: '0.5rem 0' }}>
        {pollFailure ? 'Poll failing' : 'Up to date'}
      </p>
      <p style={mutedStyle}>
        Last-seen sha: {lastSeenSha ? lastSeenSha.slice(0, 7) : 'none'}
      </p>
      {pollFailure ? (
        <div style={{ color: 'var(--danger)' }}>
          <p style={{ margin: '0.25rem 0' }}>
            {pollFailure.reason}
            {pollFailure.time ? ` (${formatTime(pollFailure.time)})` : null}
          </p>
          {consecutiveFailures > 0 ? (
            <p style={{ margin: '0.25rem 0' }}>
              {consecutiveFailures} consecutive poll failure
              {consecutiveFailures === 1 ? '' : 's'}
            </p>
          ) : null}
          {likelyPausedByHost ? (
            <p style={mutedStyle}>
              KiroCrew pauses the poll job after {HOST_CRON_AUTO_PAUSE_AFTER}{' '}
              consecutive failures; re-enable config-sync-poll on the Schedule
              page once the cause is fixed.
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}
