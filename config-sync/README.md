# Config Sync

A Kiro Crew app: Config Sync

## Installation

```bash
kirocrew app install /path/to/config-sync
kirocrew app enable config-sync
```

## Development

Edit backend code. Changes to the backend require a restart. This app ships
no agent of its own — the push and poll jobs are zero-token `command` crons,
both `enabled: false` by default until the operator configures the bundle
repository target and credentials.

## Structure

```
config-sync/
├── app.json              ← manifest (defaultEnabled:false, two command crons)
├── assets/
│   └── icon.png          ← store icon
├── backend/               ← push/poll/apply logic
│   └── server.py
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
- Imports every `instances.json` record **disconnected**, regardless of any
  `was_connected` value in the pulled file — no instance is auto-connected as
  a result of an apply.
- Lists every drop, pause, and instance change by name in the apply result, so
  the operator can see exactly what the exception admitted onto this host.
