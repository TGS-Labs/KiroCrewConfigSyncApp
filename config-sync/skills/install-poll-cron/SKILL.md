---
name: install-poll-cron
description: >
  Install config-sync's poll as a KiroCrew SCRIPT cron carrying an
  operator-approved vault token. Use after the app is installed and the
  operator has stored a read-only GitHub token for the bundle repo in
  Settings > Secrets. Copies host-crons/config_sync_poll.py into
  ~/.kiro/crew/crons/, creates the job with cron_add(script=...), requests
  the grant with cron_secret_request, and tells the operator to approve it
  on the Schedule page. Never paste the token anywhere.
---

# Install the poll cron (config-sync)

## Why the poll is not a plain app cron

KiroCrew runs cron subprocesses inside its sandbox, which hides
`~/.git-credentials` by design. The bundle repo
(`TGS-Labs/Kiro-Config-Bundles`) is private, so a command cron running
`python3 -m backend.poll` fails every tick at `git ls-remote` (exit 128,
"could not read Username"). The host's one sanctioned path for a cron to
hold a credential is a **vault grant to a SCRIPT cron**:

- the job must be a *script* job (`~/.kiro/crew/crons/<file>.py:function`),
- created by **an agent session** (`cron_add`) — the grant request must come
  from the session that owns the job, and app-manifest jobs are unowned;
- the grant is requested with `cron_secret_request` and **approved by the
  operator on the Schedule page** (no direct grants, by host design);
- at fire time the host injects the secret into the approved script body
  only, pinned to that body's exact bytes — editing the script invalidates
  the grant until it is re-approved.

`host-crons/config_sync_poll.py` is that body. It fetches the bundle repo
into the app's shared clone with the token (by env-var *name* inside git's
credential helper, never the value in argv), then runs `backend.poll` with
the token **removed** and `CONFIG_SYNC_PREFETCHED=1`, so the app's own code
never sees the credential and makes no network call.

## Prerequisites (operator)

1. A GitHub fine-grained token with **read-only Contents** on
   `TGS-Labs/Kiro-Config-Bundles` only. Read-only is sufficient: the poll
   only fetches. Pushing stays with the app backend (the dashboard's
   "Push changes" button), which runs outside the cron sandbox.
2. Store it in the dashboard: Settings > Secrets, under a name of the
   operator's choosing (example below: `KiroCrewSyncGHToken`).

## Steps (agent, in a dashboard chat session)

1. Copy the body into the host's crons directory (read it with the file
   tools and write it with the file tools; do not `cp` through a shell that
   might be denied the path):

   - source: `~/.kiro/crew/apps/config-sync/host-crons/config_sync_poll.py`
   - target: `~/.kiro/crew/crons/config_sync_poll.py`

2. Create the job (every 15 minutes, no LLM, no chat slot):

   ```
   cron_add(
     name="config-sync-poll",
     script="~/.kiro/crew/crons/config_sync_poll.py:run",
     every=900,
     minimal_context=true,
     persistent_session=false,
     hide_in_chat=true,
     timeout=1500,
   )
   ```

   `timeout` must exceed the script's own nested budgets (60 s lock wait +
   300 s git + 900 s poll = 1260 s), or the host can kill a tick mid-apply.

   Note the returned job id.

3. Request the grant, mapping the env-var the script reads to the vault
   secret name the operator chose:

   ```
   cron_secret_request(
     job_id="<job id>",
     secrets={"CONFIG_SYNC_GITHUB_TOKEN": "KiroCrewSyncGHToken"},
   )
   ```

4. Tell the operator: **Schedule page > config-sync-poll > Secrets >
   Approve.** Until approved, every tick fails closed with
   "CONFIG_SYNC_GITHUB_TOKEN is not set" and nothing is fetched or applied.

5. After approval, trigger one tick (`cron_trigger`) and verify:
   - `~/.config-sync/state/bundle-repo` has `refs/remotes/origin/main` at the
     repo's current head;
   - the Config Sync page's "From main" card shows the new `last_seen_sha`;
   - the first tick against a head that touches tracked paths writes a
     restore point and the card offers Undo.

## Updating the script

Any change to `host-crons/config_sync_poll.py` (a new app version) must be
copied to `~/.kiro/crew/crons/` again, and the grant **re-requested and
re-approved** — the host refuses to inject a secret into a body whose bytes
differ from the approved pin. Removing the app does not remove this job;
delete it with `cron_remove` (or on the Schedule page) when uninstalling.
