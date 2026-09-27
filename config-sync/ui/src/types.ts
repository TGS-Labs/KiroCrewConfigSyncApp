// Shared response shapes for the config-sync dashboard page.
//
// These mirror requirements.md Requirement 7 / design.md's "backend/routes.py
// and the UI" section, and the field names `backend/routes.py::status()` /
// `_apply_result_to_dict()` already use where the design and the code agree.
// `applying` and `last_apply` are NOT yet emitted by the live `status()`
// route (it still returns the pre-auto-apply `pending` shape) — see
// App.test.tsx's top-of-file comment and the task report for the exact gap.

export interface LastPush {
  time: string
  branch: string
  pr_url: string
  merged: boolean
}

export type PropagationClass =
  | 'live_immediate'
  | 'live_within_60s'
  | 'live_in_new_session'
  | 'live_on_next_resolution'

export interface PropagationEntry {
  propagation_class: PropagationClass
  message: string
  requires_restart: boolean
}

export interface ChangedCommand {
  file: string
  name: string
  command: string
}

export interface LastApply {
  apply_id: string
  outcome: 'applied' | 'partial' | string
  applied: string[]
  not_applied: string[]
  not_applied_reasons: Record<string, string>
  pr_urls: string[]
  merged_sha: string
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
  last_push_failure: string | null
  last_seen_sha: string | null
  last_poll_failure: string | null
  drift: boolean
  applying: boolean
  last_apply: LastApply | null
}
