// Shared response shapes for the config-sync dashboard page.
//
// These mirror the REAL `backend/routes.py::status()` payload (verified
// against `ui/src/__fixtures__/status.real.json`, captured from an actual
// backend `status()` call by `tests/test_fixture_status_real.py`) — not the
// pre-auto-apply shape `App.tsx` originally assumed. `status()` returns
// `store.last_apply` verbatim (see `backend/state.py`), so `LastApply` below
// is that raw dict's shape: `sha` (not `merged_sha`), no `pr_urls` at all,
// `not_applied` is a `{relpath: reason}` dict (not a list, and there is no
// separate `not_applied_reasons` map), and there is no `applying` field
// anywhere in the real response.

export interface LastPush {
  time: string
  branch: string
  pr_url: string
  merged: boolean
}

/** `store.last_push_failure` / `store.last_poll_failure` shape — an object,

 * never a bare string (senior review round 3, task 4). */
export interface FailureRecord {
  reason: string
  time?: string
}

export type PropagationClass =
  | 'live_immediate'
  | 'live_within_60s'
  | 'live_in_new_session'
  | 'live_on_next_resolution'

export interface PropagationEntry {
  propagation_class?: PropagationClass
  message: string
  requires_restart?: boolean
}

export interface ChangedCommand {
  file: string
  name: string
  command: string
}

/** `store.last_apply` verbatim — the real shape `status()` returns, per

 * `ui/src/__fixtures__/status.real.json`. */
export interface LastApply {
  /** Id of the most recent apply attempt that actually wrote something (the
   * one Undo restores); null when no attempt has written anything yet. */
  apply_id: string | null
  outcome: 'applied' | 'partial' | string
  applied: string[]
  /** `{relpath: reason}` — NOT a list. The former list-shaped `not_applied`
   * plus a separate `not_applied_reasons` map was never what `status()`
   * actually returns. */
  not_applied: Record<string, string>
  sha: string
  dropped_cron_names: string[]
  paused_cron_names: string[]
  changed_commands: ChangedCommand[]
  needs_credential: string[]
  propagation: Record<string, PropagationEntry>
  changed_instance_names?: string[]
}

export interface StatusResponse {
  status: 'ok' | 'error'
  reason?: string
  last_push: LastPush | null
  last_push_failure: FailureRecord | null
  last_seen_sha: string | null
  last_poll_failure: FailureRecord | null
  /** Consecutive failed poll ticks, reset to 0 on the next success
   * (senior-review round-4 M fix — `backend/state.py`'s
   * `poll_consecutive_failures`). */
  poll_consecutive_failures: number
  drift: boolean
  last_apply: LastApply | null
}
