# Config Sync

A Kiro Crew app: Config Sync

## Installation

### From the App Store (recommended)

This repository doubles as a Kiro Crew app registry (`app-registry.json` at
the repo root). Add it once, then install and update the app from the
dashboard like any other store app:

1. Dashboard → **App Store** → **Registries** → add a registry with
   name `tgs-labs`, repo `https://github.com/TGS-Labs/KiroCrewConfigSyncApp`,
   branch `main`.
2. Refresh the store and install **Config Sync**.

The repository is private: the gateway clones it with its own git identity
(the same credential that lets it push config-sync branches), which the host
permits because the app lives in the very repo you typed as the registry.

### From a local checkout

```bash
kirocrew app install /path/to/config-sync
kirocrew app enable config-sync
```

## Development

Edit backend code. Changes to the backend require a restart. This app ships
no agent of its own and **no manifest crons**: KiroCrew runs cron
subprocesses in a sandbox that hides the git credential store, and the bundle
repo is private, so a command cron for either job can only fail. The two
scheduled/triggered jobs are zero-token Python all the same:

- **Poll** — `host-crons/config_sync_poll.py`, a stdlib-only script body an
  agent registers as a KiroCrew *script* cron with an operator-approved vault
  grant holding a read-only bundle-repo token. Install and update it per
  `skills/install-poll-cron/SKILL.md`. **Uninstalling the app does not remove
  that job** (it is owned by the agent session that created it, not by the
  app): remove it with `cron_remove` or on the dashboard's Schedule page.
- **Push** — `backend/push.py`, run in the backend process by the dashboard's
  "Push changes" button (the backend runs outside the cron sandbox and has
  the box's git credential). A scheduled push is deferred until a write-scoped
  grant is decided.

Developer tooling (pytest, black, flake8, mypy) is configured in the
**repository-root** `pyproject.toml`, deliberately outside `config-sync/`:
the App Store runs `pip install .` in any app directory that contains a
`pyproject.toml`, and this stdlib app is run in place, never installed as a
package (`tests/test_store_install_contract.py` guards this). Run the tools
from the repository root so flake8 and mypy find their config:

```bash
source config-sync/.venv/bin/activate && pip install -e ".[dev]"
source config-sync/.venv/bin/activate && pytest
source config-sync/.venv/bin/activate && black . && flake8 config-sync/backend config-sync/tests && mypy config-sync/backend
```

## Structure

```
config-sync/
├── app.json              ← manifest (defaultEnabled:false, no crons — see above)
├── assets/
│   └── icon.png          ← store icon
├── backend/               ← push/poll/apply logic
│   └── server.py
├── host-crons/
│   └── config_sync_poll.py ← pinned script body for the vault-granted poll cron
├── skills/
│   ├── complete-pr-handoff/
│   └── install-poll-cron/  ← how an agent registers + grants the poll cron
└── README.md
```

## The `crons.json` / `instances.json` exception

This app tracks `crons.json` and `instances.json`, by explicit user ruling.
This is a **deliberate, documented exception** to KiroCrew's general
instance-isolation principle: an instance's scheduled jobs and remote-instance
definitions are normally NOT meant to be replicated across workspaces. It is
not presented as consistent with general KiroCrew practice — it is a scoped
exception whose risk is written down here so the operator can audit it rather
than mistake it for routine.

The risk has two concrete failure modes:

1. **Foreign cron definitions.** A pulled `crons.json` can import jobs whose
   `command`, `script`, paths, or `env` reference resources that exist only on
   a **different machine** than this one.
2. **Foreign instance definitions.** A pulled `instances.json` can import ssh
   host aliases, SSM targets, local/remote port pairs, and `remote_bin` paths
   belonging to a **different machine**, including a `was_connected` hint that
   would otherwise drive a lazy reconnect on gateway start.

To bound that risk, the applier:

- Vets every `command` in a pulled cron job with the same shell-command vet
  used at `cron_add` time; a job that fails the vet — or whose vet raises — is
  **dropped**, not imported.
- Imports every surviving `command` job, and every job naming a `script`,
  **paused** (`user_paused`) rather than live; message-only jobs may import
  live, since they only prompt an agent and do not execute anything.
- **Merges** a pulled `crons.json` into the live one rather than replacing
  it, three ways against the `crons.json` of the last fully-applied commit:
  every live job is kept exactly as it is (enabled state, vault grant,
  bookkeeping; matched by name or id); a pulled job with no live match is
  added — unless it was in that last-applied commit, meaning the operator
  deleted it here since, in which case it stays deleted; a live job the
  commit no longer carries is never removed. Two accepted consequences: a
  fleet-wide **update** to an existing job does not propagate by pull, and a
  fleet job sharing a local job's name is not added. The third side is the
  last *fully* applied commit (vetted the same way it was when applied), so a
  job dropped by the vet back then, or a commit that was never applied, is
  never mistaken for a local deletion. One known edge: a job added by a
  *partial* apply and then deleted locally before the retry tick is re-added
  by that retry (the base does not yet include it); delete it again after
  the range fully applies. A live `crons.json`
  that cannot be parsed is left alone and the file is reported not-applied.
  (The first live tick after this box's own push used to write the committed
  snapshot over the live file and delete the poll's own job.)
- Imports every `instances.json` record **disconnected**, regardless of any
  `was_connected` value in the pulled file — no instance is auto-connected as
  a result of an apply.
- Lists every drop, pause, and instance change by name in the apply result, so
  the operator can see exactly what the exception admitted onto this host.
