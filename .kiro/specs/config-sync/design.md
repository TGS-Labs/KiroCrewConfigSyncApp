# Design Document

## Architecture Overview

`config-sync` is a KiroCrew App Kit app with four moving parts and no ambient
behaviour: a **collector** that reads an allowlisted slice of two configuration
roots, a **redactor** that makes that slice safe to publish, a **pusher** that
turns it into a pull request, and an **applier** that takes an approved upstream
commit and lands it with the right cache invalidation per configuration class.
Two zero-LLM crons drive it; one backend process serves state and the approval
action; one dashboard page renders it.

```
                      ┌──────────────── PUSH (cron, command, zero LLM) ────────────────┐
  root A: KIROCREW_HOME│                                                               │
  root B: KIRO_HOME    │  collector ─► redactor ─► tokenize ─► hash ─┬─ unchanged ─► no-op │
  (allowlist.py)       │                                             │                   │
                       │                                 └─ changed ─► secret scan ─►  │
                       │     git_safety.git_argv() clone/commit ─► feature branch ─►    │
                       │     PR (never main) ─► record pushed hash ─► state.json        │
                       └───────────────────────────────────────────────────────────────┘

                      ┌──────────────── PULL (cron, command, zero LLM) ────────────────┐
  TGS-Labs/            │  git ls-remote default branch ─┬─ same head ─► exit 0         │
  Kiro-Config-Bundles  │                                └─ new head ─► classify ─►     │
  (target, unchanged)  │     notify (send_message) ─► pending in state.json            │
                       └───────────────────────────────────────────────────────────────┘
                                                │
                       user approves in UI ─────┘
                                                ▼
                       ┌──────────────── APPLY (backend route, in request) ────────────┐
                       │  backup ─► filter to allowlist ─► expand root tokens ─►       │
                       │  restore redacted values ─► sanitize crons/instances ─►       │
                       │  write ─► invalidate caches ─► per-class propagation report   │
                       └──────────────────────────────────────────────────────────────┘
```

Three invariants shape everything below:

1. **Nothing leaves the host unredacted and unscanned.** Redaction happens in
   memory in the collector's output, before the working copy is written, so
   there is no window in which a live PAT exists inside the target repository's
   working tree or index.
2. **Nothing enters the running instance without a human action.** The poll
   notifies; only a backend route reached from the UI applies.
3. **The app never claims a propagation it has not achieved.** Each
   configuration class has a verified propagation mechanism, and where the
   mechanism is bounded-stale rather than immediate, the apply result says so.

## Components

### `backend/allowlist.py` — the tracked-file definition

Pure data plus a matcher. Two roots, resolved from the environment, never
hard-coded to a home directory:

| Root | Env | Tracked |
|---|---|---|
| A | `KIROCREW_HOME` (default `~/.kiro/crew`) | `steering/**/*.md`, `skills/**/SKILL.md`, `skills/**/scripts/**`, `config.json`, `hooks.json`, `agent_model_state.json`, `mcp.json`, `crons.json`, `instances.json`, `config-bundles/agent-prompts/*.md` |
| B | `KIRO_HOME` (default `~/.kiro`) | `agents/*.json` **only** |

Root B is deliberately narrow: `kiro_home()`'s own docstring states that only
the agents directory follows `KIRO_HOME` today, so treating it as a general
isolation lever would be wrong. Everything else resolves under root A.

**Agent prompts are tracked where they live** (reference host: 16 of 25
definitions use `file://<root A>/config-bundles/agent-prompts/<name>.md`, 8 are
inline, 1 is under site-packages): `config-bundles/agent-prompts/*.md`, single
segment, class `LIVE_IN_NEW_SESSION` (read when an agent session starts).
**`config-bundles/skills/**` is NOT tracked** (Requirement 1.8): a resource is
not a registration part, so an absent one degrades an agent without breaking
registration, and the operator ratified prompts only — widening needs its own
ruling. Resources pointing there are still tokenized and, if absent on the
applying host, reported unresolved (Requirement 4.13); `sync-bundles.sh`
delivers them. `sync-bundles.sh` also derives `agent-prompts/` (and `steering/`)
from the bundle repo's `<bundle>/agents/*.md`, so a push carries a second copy
of that content at a second path — accepted by the ratified decision.

Each entry carries a `PropagationClass` (see below), which is what makes
Requirement 1.6 testable: an entry with no classification fails a test.

Never-tracked paths (`.env`, `trust/sel_hmac.key`, `memory.db`,
`memory_index.db`, `sessions/*.jsonl`, `models/*.gguf`, `scratch/`,
`snapshots/`, `gateway.log`, lock/pid files) are not expressed as a denylist the
collector consults — the allowlist cannot reach them. They are expressed as a
test that asserts no allowlist entry matches them, so the guarantee is
structural and the test proves it stayed structural.

### `backend/redact.py` — structure-preserving redaction

Input: a mapping of relative path → bytes. Output: the same mapping with every
secret-bearing *value* replaced and every *key* and structural element intact.

- JSON documents: walk the parsed tree; for any object named `headers` or `env`,
  replace each value with `"<redacted>"`, preserving key order and the
  surrounding document.
- Re-serialize deterministically (sorted where the source was sorted, fixed
  indent) so an unchanged file produces byte-identical output and therefore no
  commit.

This yields exactly the property decision 5 asked for: adding or removing an MCP
server changes the redacted copy (structure changed), while rotating a PAT does
not (only a value changed, and every value is already the placeholder).

Redaction is applied to *all* collected files, not just `mcp.json` — `config.json`
and `crons.json` can both carry a token in an `env` block, and a redactor that
guards only the file we happened to worry about is not a gate.

### `backend/portable.py` — host-independent configuration

Every tracked JSON file can carry this host's absolute paths (`prompt`,
`resources` in `agents/*.json`; any path-shaped value in `config.json`,
`hooks.json`, `mcp.json`, `crons.json`, `instances.json`,
`agent_model_state.json`) — pushed verbatim they are wrong elsewhere and no
two hosts share a tree hash. One pure module owns the rule so push, apply and
registration cannot drift: `tokenize(doc, roots)` (push, Req 2.8-2.10),
`expand(doc, roots)` (apply, Req 4.11-4.13), `resolve_reference(value, roots)
-> (root, relpath) | None` (registration, Req 5.12). `roots` is resolved
exactly as `collect.py` does. Scope is every file the allowlist tracks that
parses as JSON, not `agents/*.json` alone — `mcp.json`'s `resources` field,
`crons.json`'s `script` path, and `instances.json`'s `remote_bin` are the same
class of host-absolute value as an agent's `prompt`.

- **Token form:** `${KIROCREW_HOME}` / `${KIRO_HOME}` replace the root path,
  scheme kept — `file://${KIROCREW_HOME}/config-bundles/agent-prompts/x.md`,
  `skill://${KIROCREW_HOME}/config-bundles/skills/github-pr/SKILL.md`.
- **Matching:** strip optional `file://`/`skill://` to get `p`; match iff
  `p == R` or `p.startswith(R + "/")`, root A before root B (default A is
  nested in B). Start of path only; string values only, never keys.
- **Outside both roots** (site-packages; another host's home): unchanged,
  returned as non-portable with its key path. URLs and relative refs: neither
  rewritten nor reported.
- **Order relative to redaction:** `tokenize` runs on push AFTER `redact`,
  `expand` runs on apply BEFORE the 4.10 placeholder restore — symmetric
  (tokenize is redact's immediate successor; expand is restore's immediate
  predecessor). A `headers`/`env` value is always `"<redacted>"` by the time
  `tokenize` sees it, so no credential value is ever inspected for a root
  prefix, and `expand` running before restore is observationally identical
  to running after for those same keys (both are the literal placeholder
  either way) — the fixed order avoids a per-key-name special case.
- **Laws:** both directions idempotent; `expand(tokenize(x)) == x` on one host.

### `backend/safety/` — the three ported concerns

Ported from `kiro_crew/apps/builtins/auto_improvement/` (read as reference; this
app's copies live in this repository):

| Ported from | Surface used | Why |
|---|---|---|
| `spine/push_policy.py` | `is_protected_branch`, `authorize_direct_push`, `scan_content_for_secrets` | Refuse a push to `main`/protected/ambiguous targets; refuse (never rewrite) content with a credential; fail closed when the scanner cannot run. Returns a verdict, so it is trivially testable. |
| `spine/git_safety.py` | `GIT_SAFE_CONFIG`, `git_argv`, `pin_attributes`, `require_pinned`, `GitSafetyError` | Every git call goes through `git_argv` so a target repo cannot execute code at us via `core.hooksPath`/`core.fsmonitor`/attributes/excludes, and symlink/UNC/TOCTOU paths are rejected. |
| `backend/commit.py` | its credential-redaction path over `kiro_crew.security.redact` | Commit messages and PR bodies are as permanent as the diff. |

Explicitly **not** ported: `spine/driver.py`, `spine/agent_runner.py`,
`spine/ledger.py`, `spine/proposer.py`, `spine/bug_gate.py`. Those implement an
AI-authored PR pipeline. This app pushes a mechanically generated config diff:
there is no proposal to score, no candidate to gate, no agent run to ledger.

### `backend/push.py` — the push job (cron `command` target)

1. Collect (allowlist) → redact → tokenize every collected JSON file
   (`portable.tokenize`, Requirement 2.8-2.10; non-portable values recorded on
   the push result) → canonical serialize. Redacting first means a
   `headers`/`env` value is already the literal placeholder by the time
   tokenize walks the tree, so it is never inspected for a root-path prefix.
2. `tree_hash` = hash over sorted `(relpath, sha256(content))` pairs. Because
   of step 1 the tree — and so the hash — is identical across hosts with
   identical config (Requirement 2.11). The first tick after upgrade misses
   `last_pushed_hash` once (every in-scope file changes to token form where
   applicable, prompts are newly tracked) and takes the normal change path;
   this is expected.
3. If `tree_hash == state.last_pushed_hash`: return `no-op`. No clone, no
   network, no tokens. This is the common case and it is the whole reason the
   push is a `command` cron rather than an agent prompt.
4. `scan_content_for_secrets` over every file's content. A finding refuses the
   push and reports the scanner's refusal code plus a count — never the matched
   text, because the note is logged.
5. Clone/update the bundle repo into the app's own state directory, write the
   tree, `git add`.
6. Branch: `config-sync/<instance-id>-<short-hash>`. `authorize_direct_push`
   guards the target; a protected, empty, or ambiguous branch refuses.
7. Commit (redacted message), push the named branch explicitly, open the PR.
8. Record `last_pushed_hash` **only after** push and PR both succeed, so a
   failure retries on the next tick instead of being swallowed.

This flow assumes **one change in flight at a time**. If a new tree hash is
pushed while an earlier push's PR is still unconfirmed, the earlier attempt's
pending-PR record is treated as superseded: it is marked stale in state, not
delivered, and not silently discarded. Two truly concurrent unconfirmed pushes
are an edge case outside the ratified 15-minute notify-and-approve design, not
the common path this component is built for.

### `backend/poll.py` — the poll job (cron `command` target, 15 min)

`git ls-remote <bundle-repo> <default-branch>` → head SHA. Unchanged → exit.
Changed → fetch the changed-path list for the range `base_sha..head` (never
`last_seen_sha..head` — see Data Flow: Pull and apply below), classify each
path against the allowlist, and either start or ACCUMULATE INTO the `pending`
record in state (merge, not overwrite, when a commit is already pending), then
notify once. Re-notification is keyed on the head SHA, so a pending commit does
not re-nag every 15 minutes. NOTE: as shipped in Deployment 3, an accumulating
tick's notification carries the same head_sha/author/subject/touched_classes
shape as a fresh one and does not itself distinguish "this replaces an earlier
notification for a still-pending commit" from "this is a brand-new pending
commit" — the operator can tell the two apart only by checking the app's own
pending-record state (`base_sha` vs `pending.sha`). Making the notification
text itself say "accumulated N commits since <base_sha>" is not built and is
tracked as an open enhancement, not a Deployment 3 requirement.

Modelled on the polling shape of
`kiro_crew/apps/builtins/ops_mission_control/backend/providers/github_issues.py`;
no inbound webhook endpoint is assumed to exist.

### `backend/apply.py` — the applier (backend route, human-triggered only)

1. Refuse unless a `pending` record exists and the approved SHA matches it.
2. Back up every file about to be written or deleted into a timestamped
   restore directory.
3. Filter the commit's files to the allowlist; report non-allowlisted paths as
   ignored.
4. Expand every applied JSON file in the Requirement 2.8 scope
   (`portable.expand`, Requirement 4.11-4.13): tokens become this host's root
   paths; absolute values under neither local root are written unchanged and
   listed as non-portable; references whose target is absent locally are
   listed as unresolved. Neither is a refusal. Symmetric with push's
   tokenize-after-redact: a `headers`/`env` value is the literal placeholder
   either way, so running expand before restore is observationally identical
   for those keys to running it after, and keeps one fixed order for every
   file rather than a per-key-name special case.
4a. Restore redacted values (Requirement 4.10), AFTER expand: push writes
    `"<redacted>"` for every `headers`/`env` value, so a pulled file carries
    placeholders, not credentials. For each placeholder in a `headers` or
    `env` object, write the live file's value at the same key path; where the
    live file has no value there, keep the placeholder and list the key path
    in the result as needing a credential. Only `headers`/`env` values are
    restored: a placeholder anywhere else (for example a cron `command`) is
    applied as committed. Without this step every apply would replace every
    live token with the placeholder.
4b. Sanitize `crons.json` / `instances.json` (below), AFTER restore, so the
    vet sees exactly the content that will be written.
5. Write files atomically (temp + rename) per file.
6. Invalidate caches and build the per-class propagation report.
7. On partial failure: report applied vs not-applied; never report success.

### `backend/propagate.py` — per-class propagation, verified

| Class | Mechanism (verified) | What the app does | What the report says |
|---|---|---|---|
| `steering/**` | Loaded in `build_session_context`, reached from `build_message`'s `is_new_session` branch, or a post-compaction warm reinjection. Not live mid-session. | Nothing — no restart needed | "live in a new session" |
| `SKILL.md` body | Agent `cat`s the file at time of use | Nothing | "live now" |
| Skill set / triggers | Discovery `_iter` cache, TTL `_ITER_CACHE_TTL_SECS = 60.0`; an out-of-band write does **not** invalidate it | Invalidate (see open decision 1) | "live after cache invalidation" or, if unreachable, "live within 60s" |
| `~/.kiro/agents/*.json` | `list_agents()` caches on a (file-count, newest-mtime-ns) signature; `spawn_run`'s roster reads it live | Write every required registration part together | "live now for `spawn_run`; dashboard picker may need its own refresh" |
| `config-bundles/agent-prompts/*.md` | Read via the agent definition's `file://` `prompt` when a session for that agent starts | Nothing | "live in a new session" |
| `config.json` (incl. model pin) | Cache keyed on `st_mtime_ns + st_size + st_mode` — self-invalidating | Nothing | "live on next resolution; running sessions keep their resolved model" |
| `hooks.json`, `agent_model_state.json` | Read alongside `config.json` resolution | Nothing | "live on next resolution" |
| `crons.json`, `instances.json` | See sanitizer | Sanitize, then write | "imported paused / not connected" |

Agent registration is the one class that is *not* a single file. A full
registration is the `~/.kiro/agents/<name>.json` definition, the `config.json`
`agents{}` entry, the `agent_model_state.json` pin, and — only when the
definition's `prompt` is a `file://` reference into a tracked path — that
prompt file. The applier treats the required parts as one transaction: all
present, or the registration is refused as incomplete. A partially applied
registration misbehaves in ways that look like a model bug rather than a sync
bug, which is why this is a refusal and not a warning.

`registration.py` (Requirement 5.6, 5.11-5.13): candidates are named only by
changed `agents/<name>.json` (a prompt-only change is an ordinary file). The
prompt part is derived from the committed definition's top-level `prompt`,
never the name: inline / absent / `null` / `""` → satisfied; `file://` that
`portable.resolve_reference` maps to a tracked `(root, rel)` → REQUIRED, `rel`
must be a regular non-symlink file in the commit tree; `file://` to an
untracked path or neither root → not required, reported; `..`/empty segment,
unparseable definition, or non-string `prompt` → incomplete (fail closed).
Token and this-host absolute forms both resolve, so a legacy same-host commit
still works. Shared parts are judged by CONTENT AS IT EXISTS IN THE COMMIT'S
CHECKED-OUT TREE, not by whether that file's relpath is among the commit's
changed paths (Requirement 5.14): `config.json` counts present when its copy
in the commit tree parses as an object carrying `agents.<name>`;
`agent_model_state.json` when its copy carries top-level `<name>` — whether or
not either file changed in this specific commit. This closes a defect in the
prior wording: a commit that edits only `agents/<name>.json` for an
already-registered agent must not be refused merely because the commit leaves
the shared files untouched — the commit's tree still carries that agent's
entry and pin, which is what the registration actually needs. A NEW agent
whose key is absent from the commit tree's copy of a shared file stays
incomplete on that part regardless. An incomplete registration blocks its own
parts; its prompt only when no complete registration in the commit also
references it.

### `backend/sanitize.py` — the Requirement 6 exception, bounded

`crons.json` and `instances.json` are tracked by explicit user ruling. They are a
**deliberate, documented exception** to instance isolation, and the risk is real
and specific: a pulled `crons.json` carries `command`/`script`/`env`/paths that
may only exist on another machine, and a pulled `instances.json` carries ssh
host aliases, SSM targets, local/remote port pairs, `remote_bin` paths, and a
`was_connected` hint that drives lazy reconnect on gateway start.

The design bounds that risk by reusing KiroCrew's own import posture from
`kiro_crew/portability.py::_sanitize_imported_crons` rather than inventing one:

- A job whose `command` fails the same shell-command vet used at `cron_add`
  (deny-list, sensitive-path, credential-path, exfiltration checks) is
  **dropped** — and a vet that *raises* also drops, because an unverifiable
  command must not be scheduled.
- A surviving job with a `command`, and any job naming a `script`, is imported
  **paused** (`user_paused`), not live. A vet bounds what a command may do, not
  whether the operator wanted *this* command on *this* machine.
- Message-only jobs may import live: they prompt an agent, they do not execute.
- `instances.json` records import **disconnected**, regardless of
  `was_connected`. There is no upstream sanitizer for this file, so this app
  owns the rule.

Every drop, pause, and instance change is listed by name in the apply result.

### `backend/state.py` — durable app state

One JSON document under the app's own state directory (never inside either
tracked root, so the app's state is not itself swept into a commit):
`last_pushed_hash`, `last_push` (time/branch/PR URL/outcome), `last_seen_sha`,
`base_sha` (the head as of the operator's last approve/decline, or the
instance's first-ever polled commit before any decision — the range boundary
`poll.py` classifies from; distinct from `last_seen_sha`, which advances every
tick regardless of pending state), `pending` (sha/author/subject/classified
paths, accumulated across ticks since `base_sha`), `history` (bounded), and
`restore_dirs`.

### `backend/routes.py` and the UI

Routes on the scaffolded backend (`backend/server.py`, `port: "auto"`,
`healthCheck: "/health"`), every one wrapped in an enabled check so the app is
inert while disabled:

| Route | Purpose |
|---|---|
| `GET /health` | Scaffold-provided liveness |
| `GET /api/apps/config-sync/status` | Push state, drift flag, last-seen SHA, pending summary |
| `GET /api/apps/config-sync/drift` | Collected-tree hash vs last pushed, with per-file changed list |
| `POST /api/apps/config-sync/push` | Run the push now (same code path as the cron) |
| `POST /api/apps/config-sync/pending/{sha}/approve` | The **only** path that applies |
| `POST /api/apps/config-sync/pending/{sha}/decline` | Clear pending, change nothing |
| `POST /api/apps/config-sync/restore/{id}` | Restore a backup from a previous apply |

`ui/src/App.tsx` replaces the scaffold placeholder with: `StatCard`s for drift /
last push / pending, a pending-commit card carrying approve and decline, an
apply-result panel that renders the four propagation states distinctly, and the
Requirement 6 exception call-out rendered inline whenever a pending change
touches `crons.json` or `instances.json`.

## Data Flow

### Push

```
cron tick (command, 0 tokens)
  └─ collect(allowlist)            root A + root B → {relpath: bytes}
      └─ portable.tokenize()       agents/*.json: <root path> → ${KIROCREW_HOME}/${KIRO_HOME}
          └─ redact()              headers/env values → "<redacted>"
              └─ tree_hash()       identical across hosts for identical config
              ├─ == last_pushed_hash ──► exit 0   (the common case)
              └─ != ──► scan_content_for_secrets()
                          ├─ finding ──► REFUSE, report code+count, exit non-zero
                          └─ clean ──► git_argv(clone/checkout -B config-sync/…)
                                        └─ write tree, add, commit (redacted msg)
                                            └─ authorize_direct_push(branch)
                                                ├─ protected/empty ──► REFUSE
                                                └─ ok ──► push named branch ──► open PR
                                                            └─ state.last_pushed_hash = tree_hash
```

### Pull and apply

```
cron tick (command, 0 tokens, 15 min)
  └─ git ls-remote → head
      ├─ == last_seen_sha ──► exit 0
      └─ != ──► changed paths over range base_sha..head
                  ├─ no existing pending ──► classify ──► state.pending
                  │                                        (base_sha = head)
                  └─ existing pending (base_sha unchanged) ──► classify
                                          ──► ACCUMULATE into state.pending
                                              (pending.sha = head,
                                               base_sha still unchanged)
                     └─► notify once (keyed on head sha)
                                                              │
                                        user clicks Approve or Decline (UI → route)
                                                              ▼
                                        base_sha := the approved/declined sha
                                                              │
                                              (Approve only, continues:)
                                                              ▼
       backup(files) ─► filter(allowlist) ─► registration check (prompt part
         derived from each agents/*.json `prompt`; shared parts judged by
         commit-tree content) ─► portable.expand(every in-scope JSON file)
         ─► restore placeholders ─► sanitize(crons/instances)
         ─► atomic write ─► invalidate(skills cache) ─► propagation report
              ├─ partial failure ──► report applied / not-applied, NOT success
              └─ success ──► per-file: live now | after invalidation | new session | next resolution
```

**Accumulation, not replacement.** A poll tick's changed-path range is always
computed from `base_sha` — the head commit as of the operator's LAST actual
approve/decline decision (or the instance's first-ever polled commit, before
any decision has been made) — never from `last_seen_sha`, which advances on
every tick regardless of whether anything is pending. `base_sha` is a
distinct, durable field in `state.py`, separate from both `last_seen_sha`
(advances every tick) and `pending.sha` (always the newest head seen). While
a commit is pending, a new head arriving on a later tick re-classifies the
range `base_sha..new_head` and **merges** the result into the existing
pending record (`pending.sha` moves to the new head; `pending.classified_paths`
/ `pending.ignored_paths` / `pending.touched_classes` are the union over the
full range, not the new tick's own commits alone) — it does not overwrite the
record with only the newest tick's own changed paths. `base_sha` itself does
not move while anything is pending; it only advances, to the SHA just
decided, when the operator approves or declines. This guarantees the operator
is always shown the full accumulated diff since their last real decision,
never a partial view that silently drops an earlier commit's still-unapplied
changes — the defect this closes (Kiro-Config-Bundles#65): computing the
range from `last_seen_sha` and overwriting `pending` on every changed tick
silently dropped an earlier pending commit's files once a later commit's
record replaced it.

## Correctness and Security Properties

1. **No unredacted byte reaches git.** Redaction is inside the collector's
   output, upstream of the working-copy write. Testable by asserting the working
   copy's bytes equal the redactor's output.
2. **Refuse, never rewrite.** A secret-scan finding aborts the push. Rewriting
   a config file to sneak past a scanner would corrupt the config.
3. **Fail closed.** An unimportable scanner is treated as a finding.
4. **PR-only.** `authorize_direct_push` + `is_protected_branch` mean `main` is
   unreachable by construction, matching the bundle repo's Buildo-required-PR
   protection.
5. **No autonomous mutation of the host.** The only apply entry point requires a
   matching approved SHA from a UI action.
6. **Bounded blast radius on the scope exception.** Imported commands are vetted,
   command/script jobs land paused, instances land disconnected.
7. **Hardened git.** All git calls carry `GIT_SAFE_CONFIG` via `git_argv`.
8. **Honest propagation.** The apply result never reports a class as live when
   its mechanism is bounded-stale or session-scoped.
9. **Host-independent push tree.** No pushed file within the Requirement 2.8
   scope carries this host's root paths; a path outside both roots is
   reported, never guessed at. Testable by collecting identical config under
   two different root paths and asserting byte-identical trees and equal
   `tree_hash`.
10. **Push, apply and registration agree on paths.** All three call
    `portable.py`; the seam test pushes on one root layout, applies on another,
    and asserts registration resolves the same prompt relpath the push emitted.

## Error Handling

| Failure | Behaviour |
|---|---|
| Allowlisted file missing | Treated as absent; not created; not an error |
| Unreadable tracked file | Push refused (an unscannable file is an unscanned file); reported by path |
| Secret-scan finding | Push refused; code + count only; local state unchanged |
| Protected/empty branch | Push refused with the policy's reason string |
| Clone/push/PR failure | `last_pushed_hash` NOT updated; retried next tick; reported in UI |
| `ls-remote` failure | Poll exits non-zero; `last_seen_sha` unchanged; no notification |
| Commit contains non-allowlisted paths | Those paths ignored and reported; the rest applies |
| Incomplete agent registration (a required part missing, unparseable definition, non-string `prompt`, or unsafe prompt path) | That registration refused and reported; other files still apply |
| Agent registration whose shared file (`config.json`/`agent_model_state.json`) did not change in this commit but already carries the agent's key in the commit tree | Shared part counts present; registration proceeds (Requirement 5.14) |
| Agent `prompt` references an untracked location | Prompt not required; reported; registration proceeds on its other parts |
| Absolute path outside both roots in a Requirement 2.8-scoped file | Push: left unchanged, reported as non-portable. Apply: written unchanged, reported as non-portable |
| Expanded reference absent on this host | Written; reported as unresolved |
| Cron `command` fails the vet | Job dropped and reported by name |
| Partial apply | Applied / not-applied lists reported; success NOT claimed; restore offered |
| Skill cache invalidation unreachable | Reported as "live within 60s" rather than "live now" |
| App disabled | Every route refuses |

## Mapping onto the scaffolded app

`kirocrew app init config-sync --backend --ui --cron` already produced the tree;
this design fills it in rather than restructuring it.

```
config-sync/
├── app.json                 ← defaultEnabled:false; 2 crons (command, enabled:false);
│                              agents[]/skills[] emptied of the samples
├── assets/icon.png          ← replace placeholder
├── backend/
│   ├── server.py            ← scaffold; extend with routes.py registration
│   ├── routes.py            ← status / drift / push / approve / decline / restore
│   ├── allowlist.py         ← two roots + PropagationClass (data)
│   ├── collect.py           ├─ walk + match
│   ├── redact.py            ├─ structure-preserving value redaction
│   ├── portable.py          ├─ root-path ⇄ token rewrite for agents/*.json
│   ├── push.py              ├─ the push job
│   ├── poll.py              ├─ the poll job
│   ├── apply.py             ├─ approved-commit applier
│   ├── registration.py      ├─ agent-registration transaction check
│   ├── sanitize.py          ├─ crons/instances import rules
│   ├── propagate.py         ├─ per-class invalidation + report
│   ├── state.py             └─ durable state
│   └── safety/
│       ├── push_policy.py   ← ported
│       ├── git_safety.py    ← ported
│       └── redact_msg.py    ← ported commit-message redaction
├── ui/src/App.tsx           ← replaces the scaffold placeholder page
└── tests/                   ← allowlist/denylist, redaction idempotence,
                               scanner fail-closed, protected-branch refusal,
                               sanitizer drop/pause, propagation table coverage
```

`app.json` cron shape (both `enabled: false` until configured, per Requirement
8.3 — `CronEntry` supports `command`, `script`, `every`, `cron_expr`, and
`enabled`):

```json
"crons": [
  {"name": "config-sync-push", "every": 900,
   "command": "python3 backend/push.py", "enabled": false, "silent": true},
  {"name": "config-sync-poll", "every": 900,
   "command": "python3 backend/poll.py", "enabled": false}
]
```

The scaffold's `agents/sample-agent.json` and `skills/sample-skill/` are removed:
this app needs no agent of its own. Everything on the hot path is deterministic
Python, which is precisely why both crons are zero-token.

## Deployment Strategy

"Deployed" for this app means: installed from its own repository, enabled, its
crons armed, and one round trip proven in each direction. Each deployment is one
phase and one PR into `TGS-Labs/KiroCrewConfigSyncApp`.

### Deployment 1: Foundation
- Phase 1
- Ships `allowlist.py`, `collect.py`, `redact.py`, `backend/safety/**`,
  `state.py`, `app.json` with `defaultEnabled:false` and both crons
  `enabled:false`, and the full test suite for those modules.
- Verified by: `kirocrew app install <dir>` succeeds, the app registers
  disabled, no cron fires, tests + lint green.
- Must complete before Deployment 2.

### Deployment 2: Push direction
- Phase 2
- Ships `push.py` and the push cron wiring.
- Depends on: Deployment 1.
- Verified by: `kirocrew cron preview` on the push job with no local change
  returns a no-op; with a seeded change it opens a PR against
  `TGS-Labs/Kiro-Config-Bundles` whose diff contains `"<redacted>"` in place of
  every header value and no credential anywhere.

### Deployment 3: Pull direction — poll and notify
- Phase 3
- Ships `poll.py`, the notification, the pending record, and the poll cron.
- Depends on: Deployment 2 (the bundle repo must have a config-sync commit to
  detect).
- Verified by: the poll detects the commit merged in Deployment 2, notifies
  once, and does not re-notify on the following tick; nothing is applied.

### Deployment 4: Apply, propagation, and UI
- Phase 4
- Ships `apply.py`, `sanitize.py`, `propagate.py`, `registration.py`,
  `portable.py`, the `config-bundles/agent-prompts/*.md` allowlist entry, push
  tokenization, the routes, and the dashboard page.
- Depends on: Deployment 3.
- Verified by: approving a pending commit that touches a steering file, a skill,
  and `crons.json` applies all three, reports "live in a new session" for the
  steering file, invalidates the skill cache (or reports the ≤60s window), and
  lists the imported cron job as paused; declining changes nothing; restore
  returns the instance to its prior bytes. Additionally: the next push after
  install carries no absolute root path in any tracked JSON file and includes
  the tracked prompt files; applying that commit resolves every `file://`
  prompt into a tracked path and completes the registration, including when
  a follow-up commit changes only an agent's own definition file while its
  already-registered shared entries stay untouched in that commit.

## Open Design Decisions

Decisions 1, 2 and 4 were ratified by the operator and are resolved inputs to
this plan, not open questions. Decision 3 remains unresolved.

1. **Skill-cache invalidation from an out-of-process backend — RESOLVED.**
   The invalidator is an instance method (`_invalidate_iter_cache()`) on the
   gateway-held skills loader, reached in-process by the gateway's own
   handlers. This app's backend is a separate process (`backend.entryPoint`),
   so it cannot call it directly, and no confirmed gateway HTTP endpoint
   exists purely to refresh skill discovery. **Resolution: accept the bounded
   ≤60-second propagation lag for skill index/triggers after a pull.** This is
   a known, accepted limitation reported to the user in the pull-apply flow's
   propagation summary ("live within 60s"), not something to build an
   in-gateway fix for.
2. **Push credential and PR-creation route — RESOLVED.** The push needs a
   credential that can write a branch and open a PR on
   `TGS-Labs/Kiro-Config-Bundles`. On this host the GitHub MCP PAT is
   read-only for PRs (`create_pull_request` / `merge_pull_request` are in its
   disabled-tools list), `gh` is not installed, and Buildo is the sanctioned
   PR tool for TGS-Labs repos. **Resolution: the app's own MCP/agent context
   is read-only for GitHub and has no `gh` CLI, so `push.py` stops at
   branch-pushed and writes a "PR pending" record; a KiroCrew agent context
   completes the PR open via Buildo MCP's `create_pull_request` (no
   `merge_method` passed, since this org's repos disallow squash).**
3. **Dashboard agent-picker refresh.** The picker is a separate read path from
   `spawn_run`'s roster. Whether a pulled agent registration needs an explicit
   picker refresh, or the page re-reads on navigation, is **unverified**; the
   design currently reports it as "may need its own refresh".
4. **Prompt tracking and portable configuration paths — RESOLVED.** RESOLVED:
   track `config-bundles/agent-prompts/*.md` under root A and rewrite host
   root paths to `${KIROCREW_HOME}`/`${KIRO_HOME}` tokens, on push (after
   redact) for every tracked JSON file, expanding on apply (before the 4.10
   restore) — the prior `agent-prompts/<name>.md` convention matched no
   tracked file, so no registration could ever complete, and pushed JSONs
   embedded this host's home directory. `config-bundles/skills/**` stays
   untracked (Requirement 1.8). A registration's shared parts are judged by
   the commit tree's content, not by whether the shared file changed in that
   commit (Requirement 5.14).
