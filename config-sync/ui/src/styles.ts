// Small styling helpers shared by every card on the config-sync dashboard.
// KiroCrew theme CSS variables only — no hardcoded colors anywhere in this
// file or its callers.

import type { CSSProperties } from 'react'

export const cardStyle: CSSProperties = {
  background: 'var(--card)',
  color: 'var(--card-fg)',
  border: '1px solid var(--border)',
  borderRadius: 8,
  padding: '1rem',
}

export const mutedStyle: CSSProperties = {
  color: 'var(--muted)',
}

export function calloutStyle(tone: 'warn' | 'danger' | 'ok'): CSSProperties {
  const subtleVar = tone === 'warn' ? '--warn-subtle' : tone === 'danger' ? '--danger-subtle' : '--ok-subtle'
  const fgVar = tone === 'warn' ? '--warn' : tone === 'danger' ? '--danger' : '--ok'
  return {
    background: `var(${subtleVar})`,
    color: `var(${fgVar})`,
    border: `1px solid var(${fgVar})`,
    borderRadius: 6,
    padding: '0.5rem 0.75rem',
    marginTop: '0.5rem',
  }
}

export function chipStyle(): CSSProperties {
  return {
    background: 'var(--accent-subtle)',
    color: 'var(--accent)',
    border: '1px solid var(--border)',
    borderRadius: 999,
    padding: '0.25rem 0.75rem',
    display: 'inline-flex',
    alignItems: 'center',
    gap: '0.375rem',
    fontSize: '0.85rem',
  }
}

export function buttonStyle(): CSSProperties {
  return {
    background: 'var(--accent)',
    color: 'var(--card)',
    border: '1px solid var(--border)',
    borderRadius: 6,
    padding: '0.4rem 0.9rem',
    cursor: 'pointer',
    fontSize: '0.85rem',
  }
}

export function hoverableRowStyle(): CSSProperties {
  return {
    borderRadius: 6,
    padding: '0.4rem 0.5rem',
  }
}
