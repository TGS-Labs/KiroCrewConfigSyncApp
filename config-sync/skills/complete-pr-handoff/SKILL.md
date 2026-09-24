---
name: complete-pr-handoff
description: >
  Complete a config-sync PR handoff by calling Buildo MCP's
  create_pull_request with the payload backend/buildo_pr.py built, and
  report the result back via backend/pr_handoff.py's confirm_pr_created
  (success) or report_pr_creation_failed (failure). Use when a config-sync
  push job has produced a payload dict from build_pull_request_payload and
  needs it turned into a real PR. Never pass merge_method to Buildo.
---

# Complete PR Handoff (config-sync)

`backend/buildo_pr.py`'s `build_pull_request_payload()` only builds a plain
dict — it never calls a tool itself, because this app's own MCP/agent
context is read-only for GitHub and has no `gh` CLI. This skill is the step
that takes that dict and actually opens the pull request, and how the
KiroCrew agent context reports the outcome back into app state.

## The completion step

A KiroCrew agent context (the one running the config-sync push job) does
exactly this, in order:

1. Call `backend.buildo_pr.build_pull_request_payload(head_branch, title,
   body)` to get the payload dict (keys: `repo`, `base`, `head`, `title`,
   `body`).
2. Pass that dict's fields to Buildo MCP's `create_pull_request` tool
   (`buildo-prod-mcp::create_pull_request`, or the authorised
   `backup-buildo` fallback while the primary is unreachable — see the
   repo's own operational steering for which is live).
3. **Never pass a `merge_method` argument.** Omit it entirely so Buildo's
   own tool default (`merge`) applies. This is a hard rule: TGS-Labs repos,
   including `Kiro-Config-Bundles`, disallow `squash`, and passing
   `merge_method="squash"` causes auto-merge to silently fail to arm with
   no retry path (GitHub allows only one open PR per head/base branch
   pair, so a retry on the same branch 422s). This matches design.md's Open
   Design Decision 1 and the user's ratified decision 7: config-sync PRs
   never specify a merge method.
4. Read the tool result. A successful call returns the PR number and URL
   with auto-merge armed (Buildo arms it unconditionally at creation). A
   failed call is a real failure — Buildo has no partial-success shape for
   this tool.

## Reporting the outcome into app state

**On success:** record the PR number, URL, and head branch against the
sync job's state record. `backend/pr_handoff.py`'s `confirm_pr_created`
is the entry point that does this — call it with the tree hash, branch,
and PR URL once Buildo has actually opened the PR. This is the ONLY call
that advances the sync job's recorded `last_pushed_hash` for the
PR-handoff flow (requirements.md 2.6): a push whose branch never reaches
this call leaves the hash unchanged, so the next tick retries.

**On failure:** do not retry silently and do not swallow the error.
Requirements.md 2.7 ("WHEN a push fails for any reason THEN the failure
SHALL be recorded with its cause and surfaced in the app's UI") applies
here exactly as it does to a push failure — a failed PR creation is a
failure of the same class. Call `backend/pr_handoff.py`'s
`report_pr_creation_failed(reason=..., tree_hash=..., branch=..., state=...)`
with:

- The cause (the tool's error message/code, redacted through
  `backend/safety/redact_msg.py`'s `redact_message()` before storage, since
  a Buildo error payload could echo back caller-supplied text).
- `tree_hash` and `branch`: the SAME values this agent context was
  originally handed when the push happened (from the payload's `head`
  field and the pushed tree hash) — required, not optional. `state.py`
  checks these against the CURRENT `pending_pr` before mutating anything:
  if a NEWER push has already superseded this attempt (its `pending_pr`
  now names a different tree_hash), this report is recorded to
  `pending_pr_stale` instead, and the current, still-genuinely-pending
  attempt is left untouched. Passing the wrong or stale values here is
  exactly the failure mode this check exists to catch — always use the
  values from THIS attempt's own payload, never re-derive them from
  whatever `pending_pr` happens to hold at report time.

`report_pr_creation_failed` never advances `last_pushed_hash`
(requirements.md 2.6: the recorded last-pushed hash updates only after
push AND PR creation both succeed) — a failed PR creation leaves the sync
job retryable on its next run rather than being treated as done.

Surface the recorded failure in the app's UI exactly as any other push
failure is surfaced; this skill does not introduce a second failure-display
path.

## What this skill does not cover

- Building the payload itself — see `backend/buildo_pr.py`.
- Retrying a failed push/PR cycle — that is the push job's own retry
  policy, not this skill.
- The pending-PR record's exact on-disk shape — see `backend/state.py`'s
  `pending_pr` / `pending_pr_failure` / `pending_pr_stale` fields, owned by
  `pr_handoff.py`.
