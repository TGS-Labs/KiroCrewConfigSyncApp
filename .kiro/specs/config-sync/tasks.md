# Implementation Plan

## Overview

This plan implements `config-sync` — the KiroCrew App Kit app that puts an
instance's allowlisted configuration under git version control in
`TGS-Labs/Kiro-Config-Bundles`, push (PR-only, redacted) and pull
(notify-and-approve, per-class propagation). It contains **6 top-level tasks /
28 sub-tasks**, mapped onto design.md's **4 deployments**, each deployment being
one PR into `TGS-Labs/KiroCrewConfigSyncApp` that must merge and verify before
the next begins.

Design.md's two Open Design Decisions are **ratified inputs to this plan, not
open questions**:

1. **PR route = Buildo MCP.** The app's own context cannot open a PR
   (`create_pull_request` is disabled on the GitHub MCP PAT; no `gh` on the
   host). `push.py` therefore stops at *branch pushed + PR pending* and hands
   off; a KiroCrew agent context completes the PR via
   `buildo::create_pull_request`, **never passing `merge_method`** (TGS-Labs
   repos disallow squash, and passing it silently fails to arm auto-merge).
   `last_pushed_hash` advances only when that PR creation is confirmed, so
   Requirement 2.6 still holds across the handoff.
2. **Skill-cache lag accepted.** The gateway's `_invalidate_iter_cache()` is
   in-process and unreachable from the app's out-of-process backend. No
   in-gateway refresh path is built. `propagate.py` reports the bounded
   staleness window ("skill index visible within 60s") as its own propagation
   state.

Phase 1 and Phase 4 each span two top-level tasks because no top-level task may
exceed 5 sub-tasks; their deployment boundary is unchanged (see Deployment
Strategy).

## Deployment Strategy

This implementation requires 4 deployments. Each is one PR into
`TGS-Labs/KiroCrewConfigSyncApp`, branched from `main` **after** the previous
deployment has merged, per the folder-scoped PR-first workflow.

1. **Deployment 1 — Foundation.** Branch `feature/config-sync-foundation`
   (from `main`). Ships `allowlist.py`, `collect.py`, `redact.py`, `state.py`,
   `backend/safety/**`, `app.json` (`defaultEnabled:false`, two `command` crons
   `enabled:false`), README. Executed after Tasks 1 and 2 (Phase 1). Verified
   by: `kirocrew app install <dir>` succeeds, the app registers disabled, no
   cron fires, tests + lint + coverage green.
2. **Deployment 2 — Push direction.** Branch `feature/config-sync-push` (from
   `main` after Deployment 1 merges). Ships `push.py`, `pr_handoff.py`,
   `buildo_pr.py`, the PR-completion skill, push cron wiring. Executed after
   Task 3 (Phase 2). Depends on: Deployment 1. Verified by: push preview with no
   local change is a no-op with no network call; with a seeded change it pushes a
   `config-sync/*` branch and the completed PR's diff shows `"<redacted>"` in
   place of every header value, no credential anywhere, and no `merge_method`
   in the Buildo call.
3. **Deployment 3 — Pull detection.** Branch `feature/config-sync-poll` (from
   `main` after Deployment 2 merges). Ships `poll.py`, `classify.py`, the
   pending record, the notification, poll cron wiring. Executed after Task 4
   (Phase 3). Depends on: Deployment 2 (the bundle repo needs a config-sync
   commit to detect). Verified by: the poll detects Deployment 2's merged
   commit, notifies once, does not re-notify next tick, applies nothing.
4. **Deployment 4 — Apply, propagation, and UI.** Branch
   `feature/config-sync-apply` (from `main` after Deployment 3 merges). Ships
   `apply.py`, `sanitize.py`, `propagate.py`, `registration.py`, `routes.py`,
   the dashboard page. Executed after Tasks 5 and 6 (Phase 4). Depends on:
   Deployment 3. Verified by: approving a pending commit touching a steering
   file, a skill and `crons.json` applies all three, reports "live in a new
   session" / "live now" / "skill index visible within 60s", lists the imported
   cron job as paused; declining changes nothing; restore returns prior bytes.

## Tasks

- [ ] 1. Tracked-file definition, safe collection, and durable state

  - [ ] 1.1 `backend/allowlist.py` declares both configuration roots as data, each
        entry carrying its `PropagationClass`, with a matcher that admits a file
        only on an allowlist hit; a structural test asserts no entry can reach
        `.env`, `trust/sel_hmac.key`, `memory.db`, `memory_index.db`,
        `sessions/*.jsonl`, `models/*.gguf`, `scratch/**`, `snapshots/**`,
        `gateway.log`, or any lock/pid file, and a second test fails if any entry
        lacks a propagation classification. Root B tracks `agents/*.json` only.
    → Agent: test-engineer (tests first), then software-engineer
    _Requirements: 1.1, 1.2, 1.3, 1.4, 1.6, 6.1_

  - [ ] 1.2 `backend/collect.py` walks both roots and returns `{relpath: bytes}`
        for allowlist hits only, treating an absent allowlisted path (e.g.
        `crons.json` on an instance with no jobs) as absent without error and
        without creating it.
    → Agent: test-engineer (tests first), then software-engineer
    _Requirements: 1.3, 1.5_

  - [ ] 1.3 `backend/redact.py` replaces every `headers` and `env` *value* with
        `"<redacted>"` while preserving server names, keys, key order and
        document structure, and re-serializes deterministically. Property tests
        cover the two invariants: a structural change (server added/removed)
        changes the output; a value-only change (token rotation) leaves it
        byte-identical.
    → Agent: test-engineer (tests first, with hypothesis-test-writer for the two
      universal properties), then software-engineer
    _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

  - [ ] 1.4 `backend/state.py` persists `last_pushed_hash`, `last_push`,
        `last_seen_sha`, `pending`, bounded `history` and `restore_dirs` in one
        JSON document under the app's own state directory — never inside either
        tracked root, so app state cannot be swept into a commit. Writes are
        atomic and a failed push leaves `last_pushed_hash` untouched.
    → Agent: test-engineer (tests first), then software-engineer
    _Requirements: 2.2, 2.6, 2.7, 4.6, 4.7_

  - [ ] 1.5 Checkpoint — Verify collection and state are complete and deployable
    → Agent: test-engineer
    _Requirements: 1.5, 8.6_

    - Run the test suite; confirm allowlist/denylist and redaction tests pass
    - Run black, flake8, mypy; confirm coverage ≥95% on the modules added
    - Confirm the app's state directory path resolves outside both tracked roots

- [ ] 2. Ported safety modules and the installable, inert manifest

  - [ ] 2.1 `backend/safety/push_policy.py` (ported from
        `auto_improvement/spine/push_policy.py`) refuses a push to `main`, to any
        protected branch, and to an empty or ambiguous target with the policy's
        reason string; `scan_content_for_secrets` refuses — never rewrites —
        content with a finding, reporting refusal code and count only; an
        unimportable or unrunnable scanner fails closed. Tests fail if any of the
        three safety properties is removed.
    → Agent: test-engineer (tests first), then software-engineer
    _Requirements: 2.4, 3.6, 3.7, 8.4, 8.5_

  - [ ] 2.2 `backend/safety/git_safety.py` (ported) routes every git invocation
        through `git_argv` with `GIT_SAFE_CONFIG` so a target repo's
        `core.hooksPath`, `core.fsmonitor`, attributes or excludes file cannot
        execute, and rejects symlink/UNC/TOCTOU paths. A test asserts no git call
        in the app bypasses `git_argv`.
    → Agent: test-engineer (tests first), then software-engineer
    _Requirements: 2.5, 8.4, 8.5_

  - [ ] 2.3 `backend/safety/redact_msg.py` (ported commit-message redaction over
        `kiro_crew.security.redact`) sanitizes every commit message, PR title, PR
        body, log line and UI/API field the app emits, so no unredacted
        credential can appear in any of them.
    → Agent: test-engineer (tests first), then software-engineer
    _Requirements: 3.8, 7.5, 8.4_

  - [ ] 2.4 `app.json` declares `defaultEnabled: false` and exactly two crons —
        one push, one poll — each using `command` with `enabled: false`; the
        scaffold's `agents/sample-agent.json` and `skills/sample-skill/` are
        removed and the placeholder icon replaced. The app README states that
        tracking `crons.json` and `instances.json` is a DELIBERATE, DOCUMENTED
        EXCEPTION to instance isolation and names both concrete failure modes
        (foreign `command`/`script`/paths/`env`; foreign ssh aliases, SSM
        targets, port pairs, `remote_bin`, `was_connected`).
    → Agent: test-engineer (tests first), then software-engineer
    _Requirements: 6.2, 6.3, 8.1, 8.2, 8.3_

  - [ ] 2.5 Checkpoint — Verify Deployment 1 installs inert and is deployable
    → Agent: test-engineer
    _Requirements: 8.1, 8.2, 8.3, 8.5, 8.6_

    - Run the full test suite, black, flake8, mypy; coverage ≥95%
    - `kirocrew app install <dir>` succeeds and the app registers disabled
    - Confirm neither cron is armed and neither has fired
    - Confirm no module under `backend/safety/` imports `driver`,
      `agent_runner`, `ledger`, `proposer` or `bug_gate`

- [ ] 3. Push direction: zero-token hash gate, hardened branch push, Buildo PR handoff

  - [ ] 3.1 `backend/push.py` computes `tree_hash` over the collected,
        post-redaction tree and, when it equals `state.last_pushed_hash`, exits
        successfully having made no network call, no clone and no commit. The job
        is the target of a `command` cron, so a tick consumes no LLM tokens.
    → Agent: test-engineer (tests first), then software-engineer
    _Requirements: 2.1, 2.2_

  - [ ] 3.2 `backend/push.py` change path: scan the redacted content, then clone
        or update the bundle repo through `git_argv`, write the tree, commit with
        a redacted message, and push branch `config-sync/<instance-id>-<hash>`
        explicitly after `authorize_direct_push` clears it — refusing and
        reporting when the target is `main`, protected, empty or ambiguous, and
        refusing the whole push on a scan finding or an unreadable tracked file.
        The working copy's bytes equal the redactor's output.
    → Agent: test-engineer (tests first), then software-engineer
    _Requirements: 2.3, 2.4, 2.5, 3.1, 3.6, 3.8_

  - [ ] 3.3 `backend/pr_handoff.py` records the pushed branch as *PR pending* in
        state, notifies the operator and surfaces the handoff, and advances
        `last_pushed_hash` only once PR creation is confirmed — so a failed push
        or an uncompleted PR retries on the next tick instead of being swallowed,
        and every failure is recorded with its cause for the UI without a
        tight retry loop inside the tick.
    → Agent: test-engineer (tests first), then software-engineer
    _Requirements: 2.3, 2.6, 2.7_

  - [ ] 3.4 `backend/buildo_pr.py` builds the `create_pull_request` payload for
        `TGS-Labs/Kiro-Config-Bundles` (head branch, base `main`, redacted title
        and body) with a test that fails if `merge_method` is ever present in the
        payload; the app skill `skills/complete-pr-handoff/SKILL.md` documents
        the exact completion step a KiroCrew agent context performs via Buildo
        MCP and how to report a failed PR creation back into app state.
    → Agent: test-engineer (tests first), then software-engineer
    _Requirements: 2.3, 2.6, 2.7, 3.8_

  - [ ] 3.5 Checkpoint — Verify Deployment 2 pushes correctly and is deployable
    → Agent: test-engineer
    _Requirements: 2.1, 2.2, 2.4, 8.6_

    - Run tests, black, flake8, mypy; coverage ≥95% on the push path
    - Preview the push job with no local change: confirm no-op, no network call
    - Preview with a seeded change: confirm branch push, and that the prepared
      Buildo payload carries no `merge_method` and no credential
    - Confirm a protected-branch target is refused with the policy's reason

- [ ] 4. Pull detection: head polling, path classification, notify once

  - [ ] 4.1 `backend/poll.py` resolves the bundle repo's default-branch head via
        `git ls-remote` (no full clone) as a `command` cron consuming no LLM
        tokens, exits producing no notification when the head equals
        `last_seen_sha`, and on an `ls-remote` failure exits non-zero leaving
        `last_seen_sha` unchanged and sending no notification.
    → Agent: test-engineer (tests first), then software-engineer
    _Requirements: 4.1, 4.2_

  - [ ] 4.2 `backend/classify.py` maps a new commit's changed paths onto the
        Requirement 1 allowlist and each hit's `PropagationClass`, naming the
        tracked configuration classes the change touches and flagging
        non-allowlisted paths as ignorable; a test fails if any allowlist entry
        is unclassifiable.
    → Agent: test-engineer (tests first), then software-engineer
    _Requirements: 1.6, 4.3, 5.1_

  - [ ] 4.3 The poll writes a `pending` record (sha, author, subject, classified
        paths) and notifies once, keyed on the SHA so the same commit does not
        re-nag on every 15-minute tick; nothing is applied and the instance's
        configuration stays byte-unchanged while a commit is pending or declined,
        and no route or setting exists that would apply it automatically.
    → Agent: test-engineer (tests first), then software-engineer
    _Requirements: 4.3, 4.4, 4.6, 4.9_

  - [ ] 4.4 Checkpoint — Verify Deployment 3 detects and notifies, and is deployable
    → Agent: test-engineer
    _Requirements: 4.1, 4.2, 4.6, 8.6_

    - Run tests, black, flake8, mypy; coverage ≥95% on the poll path
    - Confirm the poll detects Deployment 2's merged commit and notifies once
    - Confirm the next tick does not re-notify and nothing was applied
    - Confirm no automatic-apply route, flag or env var exists

- [ ] 5. Approved apply: backup, allowlist filter, sanitization, honest propagation

  - [ ] 5.1 `backend/apply.py` applies only on an explicit approval whose SHA
        matches the pending record: it first records a restorable copy of every
        file it will overwrite or delete, filters the commit to the allowlist
        while reporting non-allowlisted paths as ignored, writes each file
        atomically, and on partial failure reports applied vs not-applied lists
        rather than success.
    → Agent: test-engineer (tests first), then software-engineer
    _Requirements: 4.4, 4.5, 4.7, 4.8_

  - [ ] 5.2 `backend/sanitize.py` bounds the Requirement 6 exception: a pulled
        cron job whose `command` fails the `cron_add`-time shell vet — or whose
        vet raises — is dropped; every surviving `command` job and every job
        naming a `script` imports user-paused while message-only jobs may import
        live; `instances.json` records import disconnected regardless of
        `was_connected`; and every drop, pause and instance change is listed by
        name in the result.
    → Agent: test-engineer (tests first), then software-engineer
    _Requirements: 6.4, 6.5, 6.6, 6.7_

  - [ ] 5.3 `backend/propagate.py` reports a propagation state per applied file
        and never collapses them: `steering/**` "live in a new session" with no
        gateway restart, a `SKILL.md` body "live now", a skill add/remove/rename
        or trigger change "skill index visible within 60s" (the in-process
        invalidator is unreachable from this out-of-process backend, so the
        bounded staleness window is reported instead of claiming availability),
        `config.json` and model pins "live on next resolution, running sessions
        keep their resolved model".
    → Agent: test-engineer (tests first), then software-engineer
    _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.8, 5.9_

  - [ ] 5.4 `backend/registration.py` treats an agent registration as one
        transaction over its four parts — prompt file, `~/.kiro/agents/<name>.json`,
        the `config.json` `agents{}` entry, and the `agent_model_state.json`
        pin — refusing and reporting the registration as incomplete when any part
        is missing from the commit while other files still apply, and reporting
        that `spawn_run`'s roster picks an applied registration up without a
        restart while the dashboard picker may need its own refresh.
    → Agent: test-engineer (tests first), then software-engineer
    _Requirements: 5.6, 5.7_

  - [ ] 5.5 Checkpoint — Verify the apply path is complete and deployable
    → Agent: test-engineer
    _Requirements: 4.8, 5.9, 6.7, 8.6_

    - Run tests, black, flake8, mypy; coverage ≥95% on the apply path
    - Confirm a three-part registration is refused as incomplete
    - Confirm an apply result carries a distinct propagation state per file
    - Confirm a backup directory exists for every applied file before the write

- [ ] 6. Backend routes, restore, and the dashboard page

  - [ ] 6.1 `backend/routes.py` exposes status (push state, drift flag,
        last-seen SHA, pending summary), drift (tree hash vs last pushed plus the
        changed-file list), push-now, approve and decline — approve being the
        only route from which an apply can begin — with every route refusing
        while the app is disabled and no response field carrying an unredacted
        credential.
    → Agent: test-engineer (tests first), then software-engineer
    _Requirements: 7.1, 7.2, 7.4, 7.5_

  - [ ] 6.2 The restore route returns the instance to the exact bytes recorded
        before a chosen apply, using only the local restore directory with no
        second network round trip, and reports which files it restored.
    → Agent: test-engineer (tests first), then software-engineer
    _Requirements: 4.7, 4.8_

  - [ ] 6.3 `ui/src/App.tsx` replaces the scaffold page with drift / last-push /
        pending stat cards, a pending-commit card carrying approve and decline,
        an apply-result panel that renders the four propagation states
        distinctly, and the Requirement 6 exception call-out shown inline
        whenever a pending change touches `crons.json` or `instances.json`.
    → Agent: test-engineer (tests first), then software-engineer
    _Requirements: 5.9, 6.2, 7.1, 7.2, 7.3_

  - [ ] 6.4 Checkpoint — Verify Deployment 4 is complete and deployable
    → Agent: test-engineer
    _Requirements: 7.4, 8.6_

    - Run the full test suite, black, flake8, mypy; coverage ≥95%
    - Confirm every route refuses while the app is disabled
    - Approve a pending commit touching steering, a skill and `crons.json`;
      confirm the reported propagation states and the paused cron job
    - Confirm decline changes nothing and restore returns the prior bytes

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.3", "1.4", "2.1", "2.2"] },
    { "id": 1, "tasks": ["1.2", "2.3", "2.4"] },
    { "id": 2, "tasks": ["1.5", "2.5"] },
    { "id": 3, "tasks": ["3.1", "3.4"] },
    { "id": 4, "tasks": ["3.2"] },
    { "id": 5, "tasks": ["3.3"] },
    { "id": 6, "tasks": ["3.5"] },
    { "id": 7, "tasks": ["4.1", "4.2"] },
    { "id": 8, "tasks": ["4.3"] },
    { "id": 9, "tasks": ["4.4"] },
    { "id": 10, "tasks": ["5.2", "5.3", "5.4"] },
    { "id": 11, "tasks": ["5.1"] },
    { "id": 12, "tasks": ["5.5"] },
    { "id": 13, "tasks": ["6.1"] },
    { "id": 14, "tasks": ["6.2", "6.3"] },
    { "id": 15, "tasks": ["6.4"] }
  ]
}
```

Wave 0 is the five independent leaf modules (allowlist, redact, state, and the
two ported safety modules — separate files, no collisions). Wave 1 adds the
work that needs them: the collector needs the allowlist, the manifest and README
need the module set. Waves 4-5 serialize because 3.1, 3.2 and 3.3 touch the push
path in dependency order; 3.4 is independent and rides in wave 3. Wave 11
serializes `apply.py` behind the sanitizer, propagation table and registration
transaction it calls. Wave 14 parallelizes the restore route and the UI (different
files). No wave exceeds 5, and no wave spans a deployment boundary.

## Notes

- **All tasks are required.** There is no optional, nice-to-have or deferred
  task in this plan.
- TDD is the method, not a deliverable: every implementation sub-task is
  dispatched as a pair — `test-engineer` writes tests that encode the cited
  acceptance criteria and fail for the right reason, then `software-engineer`
  writes the minimal code that makes them pass. A test is never edited to force
  green; use the `debugging` skill instead.
- Requirement coverage: all 56 original acceptance criteria across
  Requirements 1-8 are cited by at least one sub-task. Requirement 4.9 was
  added after Deployment 3's initial merge attempt (Kiro-Config-Bundles#65 —
  a multi-commit-while-pending data-loss defect 4 review rounds missed because
  no criterion covered the case) and is cited by task 4.3's fix.
- Phase 1 spans Tasks 1-2 and Phase 4 spans Tasks 5-6 because no top-level task
  may carry more than 5 sub-tasks; the four deployment boundaries are unchanged.
- Adding `skills/complete-pr-handoff/SKILL.md` (sub-task 3.4) narrows design.md's
  "this app needs no agent of its own" line: the app still ships no agent, but
  the ratified Buildo PR route requires an operator-facing procedure for the
  handoff step, which is a skill.
