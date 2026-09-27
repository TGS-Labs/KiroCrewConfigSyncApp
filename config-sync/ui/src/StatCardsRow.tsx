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
  consecutiveFailures,
  pollPaused,
}: {
  lastSeenSha: string | null
  pollFailure: FailureRecord | null
  consecutiveFailures: number
  pollPaused: boolean
}) {
  return (
    <div style={cardStyle}>
      <h2 style={{ margin: 0, fontSize: '0.9rem' }}>From main</h2>
      <p style={{ fontSize: '1.1rem', margin: '0.5rem 0' }}>
        {pollPaused ? 'Polling paused' : 'Up to date'}
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
          {pollPaused ? (
            <p style={mutedStyle}>
              Polling is paused after repeated failures; Push now or Undo
              resumes it.
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}
