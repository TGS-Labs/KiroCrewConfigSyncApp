# Requirements Document

## Introduction

`config-sync` is a KiroCrew App Kit app that puts a running KiroCrew instance's
agent configuration under git version control in the existing repository
`TGS-Labs/Kiro-Config-Bundles`, in both directions:

- **Push** — detect local changes to an allowlisted set of configuration files
  across two roots (`KIROCREW_HOME` and `KIRO_HOME`'s agents directory), redact
  credentials, and open a pull request against the bundle repository.
- **Pull** — poll the bundle repository's default branch, notify the user of a
  new commit, and — only after explicit approval — apply it to the running
  instance with the cache-invalidation and propagation semantics each
  configuration class actually requires.

The app's own source lives in `TGS-Labs/KiroCrewConfigSyncApp`; the sync target
`TGS-Labs/Kiro-Config-Bundles` is unchanged by this work. The app never edits
the installed `kiro_crew` package.

Six user decisions are ratified inputs, not open questions: notify-and-approve
pull policy, a 15-minute poll interval, change-hash-gated push cadence,
PR-only push, a structure-preserving redacted `mcp.json`, and the deliberate
inclusion of `crons.json` and `instances.json` (Requirement 6, which documents
the risk that inclusion accepts).

## Requirements

### Requirement 1

**User Story:** As a KiroCrew operator, I want the set of tracked configuration
files defined as an explicit allowlist covering both configuration roots, so
that no credential store, database, or session log can ever be swept into a
public-facing commit by accident.

#### Acceptance Criteria

1. The allowlist SHALL be declarative data (not control flow), enumerating for
   root A (`KIROCREW_HOME`, default `~/.kiro/crew`): `steering/**/*.md`,
   `skills/**/SKILL.md`, `skills/**/scripts/**`, `config.json`, `hooks.json`,
   `agent_model_state.json`, `mcp.json`, `crons.json`, `instances.json`.
2. The allowlist SHALL enumerate for root B (`KIRO_HOME`, default `~/.kiro`):
   `agents/*.json` only. The app SHALL NOT use `KIRO_HOME` to resolve any other
   path, because `kiro_home()`'s own contract is that only the agents directory
   follows it.
3. WHEN the collector walks either root THEN it SHALL include a file only if
   that file matches an allowlist entry, and SHALL NOT include a file merely
   because it is absent from a denylist.
4. The app SHALL carry an explicit, tested denylist assertion that `.env`,
   `trust/sel_hmac.key`, `memory.db`, `memory_index.db`, `sessions/*.jsonl`,
   `models/*.gguf`, `scratch/**`, `snapshots/**`, `gateway.log`, and all lock
   and pid files are NOT selected by any allowlist entry.
5. WHEN an allowlisted path does not exist on the host (e.g. `crons.json` on an
   instance with no scheduled jobs) THEN the collector SHALL treat it as absent
   without error, and SHALL NOT create it.
6. WHEN a new configuration file class is added to the allowlist THEN the
   pull-side propagation table (Requirement 5) SHALL have a matching entry, and
   a test SHALL fail if any allowlist entry has no propagation classification.

### Requirement 2

**User Story:** As a KiroCrew operator, I want local configuration changes
pushed to the bundle repository as a pull request on a schedule that costs
nothing when nothing changed, so that my config history is captured without
unreviewed writes to `main` and without burning tokens on idle ticks.

#### Acceptance Criteria

1. The push SHALL run as a scheduled cron declared in `app.json` using
   `command` or `script` (never `message`), so that a tick consumes no LLM
   tokens.
2. The push job SHALL compute a stable content hash over the collected,
   post-redaction tracked tree, and WHEN that hash equals the hash recorded for
   the last successful push THEN the job SHALL exit successfully having made no
   network call, no clone, and no commit.
3. WHEN the hash differs THEN the job SHALL clone or update a working copy of
   the bundle repository, write the collected tree, commit, push to a named
   feature branch, and open a pull request.
4. The job SHALL NOT push to `main` or to any branch classified as protected by
   the ported protected-branch policy; WHEN the computed target branch is
   protected, empty, or ambiguous THEN the job SHALL refuse the push and report
   the refusal reason.
5. All git invocations SHALL run with the ported hardened git configuration, so
   that a `core.hooksPath`, `core.fsmonitor`, attributes, or excludes file
   written into the target repository cannot execute during clone, commit, or
   push.
6. WHEN the push completes THEN the recorded last-pushed hash SHALL be updated
   only after the push and PR creation both succeed, so that a failed push is
   retried on the next tick rather than silently skipped.
7. WHEN a push fails for any reason THEN the failure SHALL be recorded with its
   cause and surfaced in the app's UI, and the job SHALL NOT retry in a tight
   loop within the same tick.

### Requirement 3

**User Story:** As a KiroCrew operator, I want every byte redacted before it
reaches the git index, so that a live PAT cannot be written into the bundle
repository even transiently, while structural drift in `mcp.json` stays
reviewable in a diff.

#### Acceptance Criteria

1. Redaction SHALL be applied to file content in memory BEFORE any `git add`,
   and SHALL NOT be implemented as a post-commit or post-push scrub.
2. WHEN `mcp.json` is collected THEN the emitted copy SHALL preserve the
   document's structure — every server name, every configuration key, and the
   presence of each `headers` key — and SHALL replace every `headers` value with
   the literal placeholder `"<redacted>"`.
3. WHEN a server is added to or removed from `mcp.json` THEN the redacted copy
   SHALL differ, so that the drift is visible in the pull request diff.
4. WHEN a `headers` value changes but no structure changes THEN the redacted
   copy SHALL be byte-identical to the previous redacted copy, so that a token
   rotation alone does not produce a commit.
5. Redaction SHALL also replace every environment-variable value in any
   collected file's `env` object with the placeholder, on the same
   structure-preserving rule.
6. After redaction and before commit, the collected content SHALL be scanned by
   the ported secret scanner; WHEN the scanner reports a finding THEN the push
   SHALL be REFUSED — not rewritten, not partially committed — and the refusal
   SHALL be reported using the scanner's refusal codes and a count only, never
   the matched text.
7. WHEN the secret scanner cannot be imported or run THEN the push SHALL fail
   closed (treated as unsafe), because an unscannable push is indistinguishable
   from an unscanned one.
8. No log line, commit message, PR title, PR body, or UI field produced by this
   app SHALL contain an unredacted credential; commit messages SHALL pass
   through the ported credential-redaction path.

### Requirement 4

**User Story:** As a KiroCrew operator, I want to be told when the bundle
repository gains a new commit and to decide myself whether it is applied, so
that a change made on another machine can never mutate this instance's
behaviour behind my back.

#### Acceptance Criteria

1. The poll SHALL run every 15 minutes as a `command` or `script` cron, so the
   poll itself consumes no LLM tokens.
2. The poll SHALL determine the current head commit of the bundle repository's
   default branch without a full clone (e.g. `git ls-remote`), and WHEN that
   commit equals the last-seen commit THEN it SHALL exit having produced no
   notification.
3. WHEN the head commit differs from the last-seen commit THEN the app SHALL
   notify the user (dashboard notification / `send_message`) identifying the
   commit, its author, its subject, and which tracked configuration classes the
   change touches.
4. The app SHALL NOT apply any pulled change without an explicit user approval
   action; there SHALL be no configuration option, environment variable, or
   route that enables automatic application.
5. WHEN the user approves a specific pending commit THEN the app SHALL apply
   only the files in that commit that match the Requirement 1 allowlist, and
   SHALL report any non-allowlisted path in the commit as ignored rather than
   applying it.
6. WHEN the user declines, or takes no action THEN the instance's configuration
   SHALL remain byte-unchanged, and the pending commit SHALL remain pending
   (re-notification SHALL NOT repeat on every 15-minute tick for the same
   commit).
7. BEFORE applying an approved commit THEN the app SHALL record a restorable
   copy of every file it is about to overwrite or delete, so that an apply can
   be reverted without a second network round trip.
8. WHEN an apply fails part-way THEN the app SHALL report which files were
   applied and which were not, and SHALL NOT report the apply as successful.
9. WHEN a poll tick detects a new head commit WHILE an earlier commit is
   already pending operator approval/decline THEN the app SHALL ACCUMULATE the
   new commit's changed paths into the existing pending record rather than
   replacing it, so that the earlier commit's changed files are never dropped
   from what the operator is shown. The pending record's changed-path range
   SHALL be computed from a recorded `base_sha` — the head commit that was
   current the LAST TIME the operator actually approved or declined a pending
   commit (or, before any decision has ever been made, the first commit this
   instance ever polled) — and NOT from `last_seen_sha`, which advances on
   every tick regardless of pending state. `base_sha` SHALL NOT advance while
   a commit is pending — including at the moment a fresh pending record is
   first created, where it is initialized to that record's own head rather
   than "advanced" from a prior value — and SHALL advance ONLY when the
   operator approves or declines, to the SHA that was just approved or
   declined. The pending
   record's `sha` field SHALL always reflect the newest head seen, so the
   operator is always shown the full accumulated diff since their last actual
   decision, never a partial view that silently drops an earlier unapplied
   change.
10. WHEN an approved commit's file contains the redaction placeholder
    `"<redacted>"` as a `headers` or `env` value THEN the app SHALL write the
    live file's existing value at the same key path in its place, so that an
    apply never replaces a real credential with the placeholder. WHEN no live
    value exists at that key path (for example a newly added server) THEN the
    app SHALL write the placeholder and SHALL list that key path, by server or
    job name and key, in the apply result as needing a credential. Every other
    value in the file SHALL be applied from the commit unchanged.

### Requirement 5

**User Story:** As a KiroCrew operator, I want an applied pull to actually take
effect — and to be told plainly when it will not take effect until I do
something — so that I never reason about my instance from a configuration it is
not really running.

#### Acceptance Criteria

1. The app SHALL hold a propagation classification for every tracked
   configuration class, and SHALL report the applicable classification to the
   user in the apply result.
2. WHEN a pulled change modifies `steering/**` THEN the app SHALL report that
   steering loads at session start (or on a post-compaction warm reinjection)
   and therefore takes effect in a NEW session; it SHALL NOT claim the change is
   live in already-running sessions, and it SHALL NOT require or trigger a
   gateway restart for steering alone.
3. WHEN a pulled change modifies a `SKILL.md` body THEN the app SHALL report
   that the body is live immediately, because an agent reads the file at time of
   use.
4. WHEN a pulled change adds, removes, or renames a skill, or changes a skill's
   frontmatter triggers THEN the app SHALL invalidate the skill-discovery
   iteration cache, because an out-of-band file write does not invalidate it and
   a new or changed skill can otherwise remain undiscoverable for up to the
   60-second cache TTL.
5. WHEN the app cannot reach an in-process invalidation path THEN it SHALL
   report the bounded staleness window (at most 60 seconds) explicitly in the
   apply result rather than reporting the skill as immediately available.
6. WHEN a pulled change adds or modifies an agent registration THEN the app
   SHALL apply all four parts of that registration together — the prompt file,
   the `~/.kiro/agents/<name>.json` definition, the `config.json` `agents{}`
   entry, and the `agent_model_state.json` pin — and WHEN any one of the four is
   missing from the commit THEN the app SHALL refuse to apply that registration
   and report it as incomplete rather than leaving a partially registered agent.
7. WHEN an agent registration is applied THEN the app SHALL report that
   `spawn_run`'s roster picks it up without a restart (the agents directory is
   cached on a file-count and newest-mtime signature) and that the dashboard's
   agent picker is a separate read path that may need its own refresh.
8. WHEN a pulled change modifies `config.json` (including a model pin) THEN the
   app SHALL report that the change self-invalidates the config cache and
   applies to the NEXT model resolution, and that sessions already running keep
   the model they already resolved.
9. The apply result SHALL distinguish, per applied file, between "live now",
   "live after cache invalidation", "live in a new session", and "live on next
   resolution" — and SHALL NOT collapse these into a single success message.

### Requirement 6

**User Story:** As the operator who ruled on scope, I want `crons.json` and
`instances.json` tracked and the risk of doing so written down, so that this
deliberate exception to instance isolation is auditable rather than mistaken
for normal practice.

#### Acceptance Criteria

1. `crons.json` and `instances.json` SHALL be in the tracked allowlist, by
   explicit user ruling.
2. The spec, the app's README, and the app's UI SHALL state that this is a
   DELIBERATE, DOCUMENTED EXCEPTION to KiroCrew's general instance-isolation
   principle — an instance's scheduled jobs and remote-instance definitions are
   normally NOT meant to be replicated across workspaces — and SHALL NOT present
   it as consistent with general KiroCrew practice.
3. The documented risk SHALL name both concrete failure modes: a pulled
   `crons.json` can import jobs whose `command`, `script`, paths, or `env`
   reference resources that exist only on a DIFFERENT machine; and a pulled
   `instances.json` can import ssh host aliases, SSM targets, local/remote port
   pairs, and `remote_bin` paths belonging to a different machine, including a
   `was_connected` hint that drives lazy reconnect.
4. WHEN an approved commit applies `crons.json` THEN every job it introduces or
   modifies that carries a `command` SHALL be vetted by the same shell-command
   vet used at `cron_add` time, and a job that fails the vet — or whose vet
   raises — SHALL be DROPPED and reported as dropped.
5. WHEN an approved commit applies `crons.json` THEN every surviving job
   carrying a `command`, and every job naming a `script`, SHALL be imported
   PAUSED (user-paused) rather than live, and reported as paused; message-only
   jobs MAY be imported live.
6. WHEN an approved commit applies `instances.json` THEN no instance SHALL be
   auto-connected as a result of the apply, regardless of any `was_connected`
   value in the pulled file.
7. The apply result SHALL list, by name, every cron job dropped and every cron
   job paused, and every instance record added or changed, so the operator can
   see exactly what the exception admitted onto this host.

### Requirement 7

**User Story:** As a KiroCrew operator, I want a dashboard page that tells me
the current sync state at a glance, so that I can see drift, the last push, and
any pending pull without reading logs.

#### Acceptance Criteria

1. The app SHALL expose a dashboard page showing: the last successful push
   (time, branch, PR URL), the current local drift state (tracked tree hash
   differs from last pushed hash: yes/no), the last-seen bundle-repo commit, and
   any pending unapproved commit.
2. WHEN a pending commit exists THEN the page SHALL offer an approve action and
   a decline action, and the approve action SHALL be the only route by which an
   apply can begin.
3. The page SHALL display the Requirement 6 exception call-out wherever
   `crons.json` or `instances.json` appear in a pending change.
4. WHEN the app is disabled THEN every backend route SHALL refuse the request,
   so the app is inert until explicitly enabled.
5. No UI field or API response SHALL contain an unredacted credential.

### Requirement 8

**User Story:** As a KiroCrew operator, I want this shipped as a normal,
opt-in installable app whose source I control, so that upgrading KiroCrew never
overwrites it and enabling it is my decision.

#### Acceptance Criteria

1. The app SHALL be installed from its own repository via
   `kirocrew app install <local-dir>` and `kirocrew app enable config-sync`, and
   SHALL NOT be added to, or edited inside, the installed `kiro_crew` package.
2. `app.json` SHALL declare `defaultEnabled: false`, so a first registration
   leaves the app off.
3. `app.json` SHALL declare exactly two crons — one push, one poll — each using
   `command` or `script`, and each SHALL be declared with `enabled: false` so
   that neither fires before the operator has configured the bundle repository
   target and credentials.
4. The app SHALL reuse the three named safety concerns by porting them —
   protected-branch authorization and the pre-push secret scan, the hardened git
   configuration and its symlink/TOCTOU defences, and commit-message credential
   redaction — and SHALL NOT port or depend on the AI-authored-PR pipeline
   modules (`driver`, `agent_runner`, `ledger`, `proposer`, `bug_gate`).
5. Every module the app ports SHALL have tests that fail if the safety property
   is removed (a push to a protected branch is refused; a seeded credential in
   collected content refuses the push; an unimportable scanner fails closed).
6. All app code SHALL pass the project's linting and type checks, and business
   logic SHALL reach at least 95% coverage.
