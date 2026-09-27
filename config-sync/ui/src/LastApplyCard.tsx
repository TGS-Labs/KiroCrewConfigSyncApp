// The last-apply card (requirements.md 7.2): applied sha, merged PR(s),
// "Undo this apply" button, plus warn/danger callouts for paused crons
// (with their vetted command), not-applied paths with reasons, and
// needs-credential keys. Also the Requirement 6 deliberate-exception
// call-out when the applied range touched crons.json or instances.json.

import type { LastApply } from './types'
import { cardStyle, mutedStyle, buttonStyle, calloutStyle } from './styles'
import PropagationChips from './PropagationChips'

export default function LastApplyCard({
  lastApply,
  onUndo,
  undoing,
}: {
  lastApply: LastApply | null
  onUndo: (applyId: string) => void
  undoing: boolean
}) {
  return (
    <div style={cardStyle}>
      <h2 style={{ margin: 0, fontSize: '0.9rem' }}>Last apply</h2>

      {!lastApply ? (
        <p style={mutedStyle}>Never applied yet.</p>
      ) : (
        <>
          <p style={{ margin: '0.5rem 0' }}>
            {lastApply.outcome === 'applied' ? 'Applied' : `Outcome: ${lastApply.outcome}`}
          </p>
          <p style={mutedStyle}>Applied sha: {lastApply.sha.slice(0, 7)}</p>

          <button
            type="button"
            style={buttonStyle()}
            onClick={() => onUndo(lastApply.apply_id)}
            disabled={undoing}
          >
            {undoing ? 'Undoing…' : 'Undo this apply'}
          </button>

          <PropagationChips lastApply={lastApply} />

          {lastApply.paused_cron_names.length > 0 ? (
            <div style={calloutStyle('warn')}>
              <strong>Paused crons</strong>
              <ul style={{ margin: '0.25rem 0 0', paddingLeft: '1.25rem' }}>
                {lastApply.paused_cron_names.map((name) => {
                  const cmd = lastApply.changed_commands.find((c) => c.name === name)
                  return (
                    <li key={name}>
                      {name}
                      {cmd ? <> — <code>{cmd.command}</code></> : null}
                    </li>
                  )
                })}
              </ul>
            </div>
          ) : null}

          {Object.keys(lastApply.not_applied).length > 0 ? (
            <div style={calloutStyle('warn')}>
              <strong>Not applied</strong>
              <ul style={{ margin: '0.25rem 0 0', paddingLeft: '1.25rem' }}>
                {Object.entries(lastApply.not_applied).map(([path, reason]) => (
                  <li key={path}>
                    {path}
                    {reason ? `: ${reason}` : null}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          {(() => {
            // A needs-credential entry for a file that is also listed as
            // not-applied (for a different, already-reported reason) is
            // moot: nothing was attempted for that file, so a stale
            // credential callout would only duplicate the not-applied
            // reason for the same path. Filter those out.
            const notAppliedFiles = new Set(
              Object.keys(lastApply.not_applied).map((path) => path.split(':')[0]),
            )
            const visibleNeedsCredential = lastApply.needs_credential.filter(
              (key) => !notAppliedFiles.has(key.split(':')[0]),
            )
            return visibleNeedsCredential.length > 0 ? (
              <div style={calloutStyle('danger')}>
                <strong>Needs credential</strong>
                <ul style={{ margin: '0.25rem 0 0', paddingLeft: '1.25rem' }}>
                  {visibleNeedsCredential.map((key) => (
                    <li key={key}>{key}</li>
                  ))}
                </ul>
              </div>
            ) : null
          })()}

          {(lastApply.applied.includes('crons.json') ||
            lastApply.applied.includes('instances.json') ||
            (lastApply.changed_instance_names?.length ?? 0) > 0) ? (
            <div style={calloutStyle('ok')}>
              This is a deliberate exception to instance isolation:
              crons.json/instances.json sync by design.
              {(lastApply.changed_instance_names?.length ?? 0) > 0 ? (
                <>
                  {' '}
                  Instances affected: {lastApply.changed_instance_names!.join(', ')}.
                </>
              ) : null}
            </div>
          ) : null}
        </>
      )}
    </div>
  )
}
