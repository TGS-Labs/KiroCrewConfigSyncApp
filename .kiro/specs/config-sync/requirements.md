# Requirements Document

## Introduction

`config-sync` is a KiroCrew App Kit app that puts a running KiroCrew instance's
agent configuration under git version control in the existing repository
`TGS-Labs/Kiro-Config-Bundles`, in both directions:

- **Push** — detect local changes to an allowlisted set of configuration files
  across two roots (`KIROCREW_HOME` and `KIRO_HOME`'s agents directory), rewrite
  host-specific absolute paths in agent definitions to portable tokens, redact
  credentials, and open a pull request against the bundle repository.
- **Pull** — poll the bundle repository's default branch and, on every commit
  that reaches it, apply it to the running instance automatically, with the
  cache-invalidation and propagation semantics each configuration class
  actually requires.

The app's own source lives in `TGS-Labs/KiroCrewConfigSyncApp`; the sync target
`TGS-Labs/Kiro-Config-Bundles` is unchanged by this work. The app never edits
the installed `kiro_crew` package.

**OPERATOR RULING (supersedes the original notify-and-approve pull policy).**
The approval gate for a pulled change is the pull request merge into
`TGS-Labs/Kiro-Config-Bundles`'s branch-protected `main` — not a box-side
action. Anything on `main` is production configuration and IS applied to this
box automatically on the next 15-minute poll. There is no approve or decline
route on the box: the poll runs the same apply path (materialize the commit
tree, then `apply_commit` with expand, restore, sanitize, per-file isolation,
and a restore directory) that a box-side approval used to trigger, without
waiting for one. Every box-side safety check on the *content* of a commit
remains in force unchanged — redaction/credential restore (Requirement 4.10),
command vetting for crons/hooks/mcp with fail-closed drop (Requirement 6),
crons importing paused rather than live, and no agent ever ending up
half-registered (Requirement 5.15) — because those checks bound what a
committed change is allowed to do to this box, independent of who or what
triggers the apply. Only the human-in-the-loop gate on the box itself is
removed; the human-in-the-loop gate on `main` (branch protection + required
review before merge) is what now does that job.

Six user decisions are ratified inputs, not open questions: automatic-apply-
on-merge pull policy, a 15-minute poll interval, change-hash-gated push
cadence, PR-only push, a structure-preserving redacted `mcp.json`, and the
deliberate inclusion of `crons.json` and `instances.json` (Requirement 6,
which documents the risk that inclusion accepts).

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
   `agent_model_state.json`, `mcp.json`, `crons.json`, `instances.json`,
   `config-bundles/agent-prompts/*.md`.
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
7. The root A entry `config-bundles/agent-prompts/*.md` SHALL carry the
   `LIVE_IN_NEW_SESSION` propagation class (an agent's prompt is read when a
   session for that agent starts), and SHALL select only a `.md` file directly
   inside `config-bundles/agent-prompts/` — a nested path
   (`config-bundles/agent-prompts/x/y.md`) or a non-`.md` file there SHALL
   NOT be selected.
8. No allowlist entry SHALL select any other path under `config-bundles/` —
   in particular `config-bundles/skills/**` and `config-bundles/sync-bundles.sh`
   SHALL NOT be selected. (An agent `resources` entry is not a registration
   part, so an absent skill degrades an agent without breaking its
   registration; `config-bundles/skills/` is delivered by `sync-bundles.sh`,
   and widening scope to it needs its own operator ruling.)

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
8. AFTER redaction and BEFORE hashing, the push SHALL rewrite every JSON
   string value (at any depth, including list elements; never an object key)
   in every collected file that parses as a JSON object or array — root A's
   `config.json`, `hooks.json`, `mcp.json`, `crons.json`, `instances.json`,
   `agent_model_state.json`, and root B's `agents/*.json` — to its portable
   token form, as follows. Let `R_A` be root A's absolute path and `R_B` root
   B's, each resolved exactly as the collector resolves it (env var or
   default, `~` expanded, no trailing `/`). Strip an optional leading scheme
   prefix `file://` or `skill://` from the value, giving path part `p`. WHEN
   `p == R` or `p` starts with `R + "/"` for `R` in (`R_A`, `R_B`), tried in
   that order (root A first, because the default root A lies inside root B),
   THEN the matched `R` SHALL be replaced by `${KIROCREW_HOME}` (root A) or
   `${KIRO_HOME}` (root B) and the scheme prefix SHALL be kept — e.g.
   `file:///home/u/.kiro/crew/steering/**/*.md` becomes
   `file://${KIROCREW_HOME}/steering/**/*.md`. A value in which `R` appears
   anywhere other than at the start of `p` (e.g. inside inline prompt text),
   or is followed by a character other than `/`, SHALL NOT be rewritten.
   Running tokenization after redaction means the `"<redacted>"` placeholder
   already occupies every `headers`/`env` value by the time tokenization
   walks the tree, so a credential value is never inspected for a root-path
   prefix; a `headers`/`env` object's OTHER keys are absent by construction
   after redaction, so tokenization has nothing left to rewrite there.
9. WHEN a collected file's string value in scope of Requirement 2.8 is an
   absolute path — `p` starts with `/` after the optional
   `file://`/`skill://` prefix is stripped — that lies under neither root
   (e.g.
   `file:///usr/local/lib/python3.12/site-packages/kiro_crew/config/prompt.md`)
   THEN the push SHALL leave the value unchanged and SHALL list it in the push
   result as non-portable (file path and JSON key path); this SHALL NOT refuse
   the push. Relative references (e.g. `file://.kiro/steering/**`) and
   non-path values (e.g. `https://…` URLs) SHALL be left unchanged and not
   listed.
10. Tokenization SHALL be idempotent — applying it to already-tokenized
    content SHALL produce byte-identical output — and SHALL touch no file
    outside the Requirement 2.8 scope. A non-JSON file within that scope
    SHALL pass through unchanged.
11. WHEN two hosts with different root paths hold identical configuration
    (identical file content once each host's own root path is substituted by
    its token) THEN each SHALL produce a byte-identical pushed tree and the
    same tree hash. The first push after this tokenization ships is therefore
    expected to differ from the recorded `last_pushed_hash` (every in-scope
    file changes from absolute to token form where applicable, and prompt
    files are newly tracked) and SHALL run the normal change path — it is not
    an error and SHALL NOT be special-cased.

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

**User Story:** As a KiroCrew operator, I want every commit that lands on the
bundle repository's protected `main` applied to this instance automatically
on the next poll, so that a change I already approved by merging it takes
effect without a second, redundant approval step on the box.

#### Acceptance Criteria

1. The poll SHALL run every 15 minutes as a `command` or `script` cron, so the
   poll itself consumes no LLM tokens.
2. The poll SHALL determine the current head commit of the bundle repository's
   default branch without a full clone (e.g. `git ls-remote`), and WHEN that
   commit equals the last-seen commit THEN it SHALL exit having applied
   nothing and produced no notification.
3. WHEN the head commit differs from the last-seen commit THEN the app SHALL
   materialize the commit tree for the range `base_sha..head` and run it
   through the same apply path that a box-side approval used to trigger —
   `apply_commit` with expand, restore, sanitize, per-file isolation, and a
   restore directory — automatically, with no operator action required. The
   app SHALL notify the user (dashboard notification / `send_message`) of the
   result identifying the commit, its author, its subject, which tracked
   configuration classes the change touched, and the outcome (`applied` or
   `partial`, per Requirement 4.14).
4. The app SHALL apply every polled commit range on `main` automatically; the
   approval gate is the pull request merge into `main` (branch-protected in
   `TGS-Labs/Kiro-Config-Bundles`), not a box-side action. There SHALL be no
   approve or decline route, UI control, or pending-for-operator-decision
   state on the box — the poll IS the apply trigger. Every box-side safety
   check on the CONTENT of a commit (redaction/credential restore, command
   vetting, paused cron import, no half-registration) SHALL still run
   unconditionally on every apply, per Requirements 4.10, 5.15, and 6.
5. WHEN the poll applies a commit range THEN the app SHALL apply only the
   files in that range that match the Requirement 1 allowlist, and SHALL
   report any non-allowlisted path in the range as ignored rather than
   applying it.
6. [Reserved — the box-side decline path this criterion described no longer
   exists under the operator ruling in the Introduction; there is no action
   the operator takes on the box to leave a polled commit unapplied. Kept
   reserved rather than renumbered so citations elsewhere are not broken by a
   shift.]
7. BEFORE applying a commit range THEN the app SHALL record a restorable
   copy of every file it is about to overwrite or delete, so that an apply can
   be reverted without a second network round trip.
8. WHEN an apply fails part-way THEN the app SHALL report which files were
   applied and which were not, and SHALL NOT report the apply as successful.
9. WHEN a poll tick detects a new head commit THEN the app SHALL compute the
   changed-path range from a recorded `base_sha` — the head commit as of the
   last apply outcome that was fully `applied` (or, before any apply has ever
   fully succeeded, the first commit this instance ever polled) — and NOT
   from `last_seen_sha`, which advances on every tick regardless of apply
   outcome. `base_sha` SHALL NOT advance on a `partial` outcome (Requirement
   4.14): the next tick SHALL retry the SAME range `base_sha..head` (extended
   to the new head if one arrived meanwhile), so a not-yet-applied path is
   never dropped from what the next apply attempt covers. `base_sha` SHALL
   advance ONLY when an apply outcome is fully `applied`, to the SHA that was
   just applied.
10. WHEN an applied commit's file contains the redaction placeholder
    `"<redacted>"` as a `headers` or `env` value THEN the app SHALL write the
    live file's existing value at the same key path in its place, so that an
    apply never replaces a real credential with the placeholder. WHEN no live
    value exists at that key path (for example a newly added server) THEN the
    app SHALL write the placeholder and SHALL list that key path, by server or
    job name and key, in the apply result as needing a credential. Every other
    value in the file SHALL be applied from the commit unchanged.
11. WHEN an applied commit's file within the Requirement 2.8 scope is
    applied THEN, BEFORE the Requirement 4.10 placeholder restore, every
    string value whose path part (after an optional `file://`/`skill://`
    prefix) equals `${KIROCREW_HOME}` or `${KIRO_HOME}`, or starts with that
    token followed by `/`, SHALL have the token replaced by this host's own
    root A or root B absolute path (resolved as in Requirement 2.8), keeping
    the scheme prefix. A token appearing anywhere else in a value SHALL NOT be
    expanded. Expanding BEFORE restoring the 4.10 placeholder is required
    because a `headers`/`env` value is always exactly `"<redacted>"` (never a
    token) at this point — running expansion before or after restore is
    therefore observationally identical for those keys — but expanding first
    keeps one fixed step order for every in-scope file rather than a
    per-key-name special case, and matches the push-side order (tokenize is
    the LAST push step, so expand is the FIRST symmetric apply step).
    Expansion SHALL be idempotent, and SHALL touch no file outside that scope;
    for every value it rewrote, expanding the token form that Requirement 2.8
    produced from it on a host SHALL yield that host's original value.
12. WHEN an applied file's value within the Requirement 2.8 scope is an
    absolute path under neither of this host's roots (a product-shipped path,
    or a legacy commit pushed before Requirement 2.8 carrying another host's
    home directory) THEN the app SHALL write the value unchanged and SHALL
    list it (file path and JSON key path) in the apply result as non-portable;
    this SHALL NOT refuse the file.
13. WHEN, after expansion, an applied value within the Requirement 2.8 scope
    is a `file://` or `skill://` reference under one of this host's roots
    whose target does not exist on this host (glob patterns are checked for
    at least one match) THEN the apply result SHALL list it as an unresolved
    reference, and SHALL NOT refuse the file — an agent resource under an
    untracked location such as `config-bundles/skills/` is delivered by that
    location's own mechanism, not by this app.
14. WHEN a poll's automatic apply outcome is `partial` (Requirement 4.8) THEN
    `base_sha` SHALL NOT advance from the range's start; the app SHALL record
    the not-applied paths and their REAL per-path failure reasons (the actual
    cause the apply routine encountered for that path — e.g. the specific
    vet-rejection, the specific missing-parent-directory error, the specific
    permission error — never a generic "failed to apply" placeholder) against
    the current state, and this SHALL be visible in the app's status display
    (Requirement 7.1). The NEXT poll tick SHALL retry applying the SAME
    unresolved range — the apply routine SHALL be idempotent, so a path
    already correctly applied in the failed attempt SHALL NOT be reapplied
    incorrectly or duplicated. WHEN a LATER commit on `main` fixes the
    condition that caused an earlier path's failure (for example a corrected
    file, or a dependency that now exists) THEN the retry on or after that
    later commit's poll SHALL apply that path normally, and the app SHALL
    NOT require any operator action to trigger the retry — it happens on the
    next scheduled poll tick, exactly like every other apply. `base_sha`
    SHALL advance, past the retried range, ONLY WHEN a poll's apply outcome
    for that range is fully `applied` — never on `partial`.

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
   SHALL apply every required part of that registration together — the
   `~/.kiro/agents/<name>.json` definition, the `config.json` `agents{}`
   entry, the `agent_model_state.json` pin, and the prompt file the definition
   references only where Requirement 5.11 makes it a required part — and WHEN
   any required part is missing THEN the app SHALL refuse to apply that
   registration and report it as incomplete rather than leaving a partially
   registered agent. A shared part (the `config.json` entry, the
   `agent_model_state.json` pin) is judged by the CONTENT of that file AS IT
   EXISTS IN THE APPLIED COMMIT'S TREE — present when that file's copy in the
   commit carries the agent's own key — regardless of whether that file is
   itself among the commit's changed paths; an agent's registration is
   therefore NOT refused merely because a given apply's commit left the shared
   files untouched, so long as the commit's tree already carries that agent's
   entry and pin. A new agent whose key is absent from the commit's copy of a
   shared file remains incomplete on that part.
7. WHEN an agent registration is applied THEN the app SHALL report that
   `spawn_run`'s roster picks it up without a restart (the agents directory is
   cached on a file-count and newest-mtime signature) and that the dashboard's
   agent picker is a separate read path that may need its own refresh. This
   applies identically whether the triggering commit changed the agent's own
   definition alone or also touched a shared file — Requirement 5.14 governs
   how the shared parts are judged, not what is reported once judged complete.
8. WHEN a pulled change modifies `config.json` (including a model pin) THEN the
   app SHALL report that the change self-invalidates the config cache and
   applies to the NEXT model resolution, and that sessions already running keep
   the model they already resolved.
9. The apply result SHALL distinguish, per applied file, between "live now",
   "live after cache invalidation", "live in a new session", and "live on next
   resolution" — and SHALL NOT collapse these into a single success message.
10. WHEN a pulled change modifies a `config-bundles/agent-prompts/*.md` file
    THEN the app SHALL report that the prompt takes effect in a NEW session of
    that agent, and SHALL NOT claim it is live in already-running sessions.
11. The prompt part of a registration SHALL be derived from the committed
    `agents/<name>.json`'s top-level `prompt` value, never from the agent's
    name: (a) a `file://` value whose path part resolves (Requirement 5.12) to
    a root-and-relpath that the Requirement 1 allowlist tracks makes that
    relpath a REQUIRED part; (b) an inline string (any value not starting
    with `file://`), or an absent, `null`, or empty `prompt`, satisfies the
    prompt part with no file; (c) a `file://` value resolving to a path the
    allowlist does not track, or to neither root (e.g. site-packages), makes
    the prompt NOT a required part, and the apply result SHALL report that
    agent's prompt as referencing an untracked location. A `prompt` that is
    present but neither a string nor `null` SHALL make the registration
    incomplete.
12. Prompt resolution SHALL accept both the token form
    (`file://${KIROCREW_HOME}/<rel>`, `file://${KIRO_HOME}/<rel>`) and this
    host's own absolute form (`file://<root A or B>/<rel>`), mapping each to
    `(root, rel)` by the Requirement 2.8 boundary rule. A required prompt
    part SHALL count as present when `<rel>` exists as a regular, non-symlink
    file in the applied commit's tree. A `<rel>` with an empty or `..`
    segment, and an `agents/<name>.json` that does not parse as a JSON
    object, SHALL each make that registration incomplete (fail closed).
13. A registration candidate SHALL be named only by a changed
    `agents/<name>.json`. A changed prompt file that no changed agent
    definition references SHALL apply as an ordinary tracked file, not be
    treated as a registration. A required prompt file SHALL be blocked from
    applying only when every changed agent definition referencing it belongs
    to an incomplete registration.
14. Shared-part evaluation (Requirement 5.6) SHALL read each shared file
    (`config.json`, `agent_model_state.json`) from its path WITHIN the
    applied commit's checked-out tree — never from the live host — and
    SHALL count the part present whenever that file parses as a JSON object
    and carries the agent's own key at the required location (`agents.<name>`
    for `config.json`; top-level `<name>` for `agent_model_state.json`),
    whether or not that file's relpath is itself in the commit's changed-path
    list. A shared file that does not exist at all in the commit's tree, or
    that does not parse as a JSON object, SHALL make that part absent for
    every agent (fail closed).
15. WHEN `config.json` or `agent_model_state.json` is blocked from applying
    because the registration of an agent NAMED IN THE COMMIT is incomplete
    (Requirement 5.6) THEN EVERY OTHER agent whose key is ALSO present in
    that same committed shared file SHALL be blocked on that shared file too
    — its own `agents/<name>.json` in the commit, if any, SHALL NOT be
    applied — and the apply result SHALL report each such agent as
    incomplete, naming the blocking agent and the shared file. No agent
    SHALL ever end an apply half-registered — with, for example, its
    `agents/<name>.json` written but its `config.json` entry withheld — as a
    side effect of a DIFFERENT agent's incomplete registration in the same
    commit.

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
4. WHEN an applied commit applies `crons.json` THEN every job it introduces or
   modifies that carries a `command` SHALL be vetted by the same shell-command
   vet used at `cron_add` time, and a job that fails the vet — or whose vet
   raises — SHALL be DROPPED and reported as dropped.
5. WHEN an applied commit applies `crons.json` THEN every surviving job
   carrying a `command`, and every job naming a `script`, SHALL be imported
   PAUSED (user-paused) rather than live, and reported as paused; message-only
   jobs MAY be imported live.
6. WHEN an applied commit applies `instances.json` THEN no instance SHALL be
   auto-connected as a result of the apply, regardless of any `was_connected`
   value in the pulled file.
7. The apply result SHALL list, by name, every cron job dropped and every cron
   job paused, and every instance record added or changed, so the operator can
   see exactly what the exception admitted onto this host.
8. WHEN an applied commit applies `hooks.json` or `mcp.json` THEN every
   command it introduces or changes SHALL be vetted by the same
   shell-command vet used at `cron_add` time (Requirement 6.4). A
   `hooks.json` shell-hook entry has no `args` field — its whole invocation
   is the single `command` string — so the vet SHALL run over that
   `command` string alone. An `mcp.json` server entry's launch `command` and
   `args` SHALL be joined into one command line (`command` followed by each
   `args` element, shell-quoted) before the vet runs, so the vet sees
   exactly what the launcher will execute, matching how `cron_add`'s vet is
   applied to a cron `command`.
9. WHEN a `hooks.json` or `mcp.json` command fails that vet, or the vet
   raises, THEN that entry SHALL be DROPPED from the applied file and
   reported by name (hook name or MCP server name) — fail-closed, the same
   posture as Requirement 6.4's cron drop — and every other entry in that
   file SHALL still apply.
10. Every hook or MCP server command added or changed by an applied commit
    — whether it survives the vet or is dropped under 6.9 — SHALL be listed
    by name in the apply result's `changed_commands` field (a list of
    `{file, name, command}` — `file` is `hooks.json` or `mcp.json`, `name`
    is the hook or server name, `command` is the vetted command string:
    the bare `command` for `hooks.json`, the joined `command`+`args` line
    for `mcp.json`). Because there is no box-side approval step, there is no
    pre-approval summary to populate — `changed_commands` SHALL be surfaced
    once, in the apply result itself, immediately after the automatic apply
    that introduced or changed the command runs, so the operator sees the
    command as soon as it is possible to see it. Message-only cron jobs are
    unaffected by 6.8-6.10 and remain governed solely by Requirement 6.5 (no
    vet applies to a job with no `command`); this is a ratified non-change,
    not an oversight.

### Requirement 7

**User Story:** As a KiroCrew operator, I want a dashboard page that tells me
the current sync state at a glance — including what the last automatic apply
did and a way to undo it — so that I can see drift, the last push, and what
landed from `main` without reading logs.

#### Acceptance Criteria

1. The app SHALL expose a dashboard page showing three status cards: (a) local
   changes — whether the tracked tree currently differs from the last pushed
   hash, with a "Push now" action; (b) last push — its time, branch, PR URL,
   and whether that PR still needs merging (open) or has merged; (c) sync from
   `main` — whether the instance is up to date with or currently applying the
   bundle repository's head, the last-seen SHA, and the time of the last check.
2. The page SHALL show a last-apply card carrying an Undo action, and
   displaying: the merged PR(s) the applied range corresponds to (by URL/SHA),
   every cron job imported paused together with its vetted command, every
   not-applied path from a `partial` outcome together with its real per-path
   reason, and every key path listed as needing a credential.
3. The page SHALL show, for the current apply state, the four propagation
   timing chips from Requirement 5.9: "live now", "live within 60s", "live in
   a new session", and "live on next resolution" — rendered as distinct chips,
   not collapsed into one status.
4. The page SHALL display the Requirement 6 exception call-out wherever
   `crons.json` or `instances.json` appear in the last-applied range.
5. WHEN the app is disabled THEN every backend route SHALL refuse the request,
   so the app is inert until explicitly enabled.
6. No UI field or API response SHALL contain an unredacted credential.
7. Every state-mutating POST route (push-now, undo) SHALL refuse a request
   that a browser marks as cross-site — `Sec-Fetch-Site` other than
   `same-origin`/`none`, or, when that header is absent, an `Origin` whose
   host differs from the request's own loopback `Host` — and SHALL refuse a
   non-loopback `Host`; a request from another site open in the operator's
   browser therefore cannot trigger a mutation. (The dashboard's real
   `@kirocrew/app-sdk` `post()` cannot attach a custom header, so the
   original `X-Config-Sync-Request: 1` rule is superseded by this
   fetch-metadata check.)

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
