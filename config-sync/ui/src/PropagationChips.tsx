// The four propagation-timing chips (requirements.md 5.9, 7.3). Each chip
// is rendered distinctly — never collapsed into one status — with a count
// of how many applied paths in the last apply fall into that timing class.

import type { LastApply, PropagationClass } from './types'
import { chipStyle } from './styles'

const CHIP_ORDER: Array<{ cls: PropagationClass; label: string }> = [
  { cls: 'live_immediate', label: 'live now' },
  { cls: 'live_within_60s', label: 'live within 60s' },
  { cls: 'live_in_new_session', label: 'live in a new session' },
  { cls: 'live_on_next_resolution', label: 'live on next resolution' },
]

export default function PropagationChips({ lastApply }: { lastApply: LastApply }) {
  const counts: Record<PropagationClass, number> = {
    live_immediate: 0,
    live_within_60s: 0,
    live_in_new_session: 0,
    live_on_next_resolution: 0,
  }
  for (const entry of Object.values(lastApply.propagation ?? {})) {
    // Real `status()` payloads have been observed with a propagation entry
    // that carries `message`/`requires_restart` but no `propagation_class`
    // at all (ui/src/__fixtures__/status.real.json) — skip rather than
    // index `counts` with `undefined`, which would silently corrupt every
    // other chip's count via an `undefined` object key.
    if (entry.propagation_class === undefined) continue
    counts[entry.propagation_class] = (counts[entry.propagation_class] ?? 0) + 1
  }

  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.5rem', marginTop: '0.75rem' }}>
      {CHIP_ORDER.map(({ cls, label }) => (
        <span key={cls} style={chipStyle()}>
          {label}
          <span>{counts[cls]}</span>
        </span>
      ))}
    </div>
  )
}
