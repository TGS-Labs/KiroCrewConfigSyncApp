---
name: complete-pr-handoff
description: >
  Complete a config-sync PR handoff by calling Buildo MCP's
  create_pull_request with the payload backend/buildo_pr.py built, and
  record the result (pending, opened, or failed) in app state. Use when a
  config-sync push job has produced a payload dict from
  build_pull_request_payload and needs it turned into a real PR. Never pass
  merge_method to Buildo.
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
sync job's state record. The provisional field name for this "PR pending /
opened" state is:

```
pending_pr
```

This name is **provisional**. Task 3.3 (`pr_handoff.py`) has not landed yet
as of this writing — when it does, reconcile this skill's field name and
`buildo_pr.py`'s callers against whatever `pr_handoff.py` actually defines,
rather than assuming `pending_pr` is final. Do not block current work on
this reconciliation; it is a follow-up once 3.3 exists.

**On failure:** do not retry silently and do not swallow the error.
Requirements.md 2.7 ("WHEN a push fails for any reason THEN the failure
SHALL be recorded with its cause and surfaced in the app's UI") applies
here exactly as it does to a push failure — a failed PR creation is a
failure of the same class. Record:

- The cause (the tool's error message/code, redacted through
  `backend/safety/redact_msg.py`'s `redact_message()` before storage, since
  a Buildo error payload could echo back caller-supplied text).
- The head branch and payload's `repo`/`base`/`head` fields, so the failure
  is traceable to a specific attempted PR.
- That the last-pushed hash is **not** advanced (requirements.md 2.6: the
  recorded last-pushed hash updates only after push AND PR creation both
  succeed) — a failed PR creation must leave the sync job retryable on its
  next run rather than being treated as done.

Surface the recorded failure in the app's UI exactly as any other push
failure is surfaced; this skill does not introduce a second failure-display
path.

## What this skill does not cover

- Building the payload itself — see `backend/buildo_pr.py`.
- Retrying a failed push/PR cycle — that is the push job's own retry
  policy, not this skill.
- Anything about `pr_handoff.py`'s eventual shape beyond the provisional
  field name above.
