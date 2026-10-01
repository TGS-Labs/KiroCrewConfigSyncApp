"""config-sync poll — KiroCrew SCRIPT cron with an operator-granted token.

WHY THIS FILE EXISTS. KiroCrew runs cron subprocesses inside its sandbox,
which hides ``~/.git-credentials`` by design, and the bundle repo
(``TGS-Labs/Kiro-Config-Bundles``) is private — so the app's plain command
cron (``python3 -m backend.poll``) can never resolve the remote head (live
install, 2026-10-01: ``git ls-remote`` exit 128, "could not read Username").
The host's only sanctioned way for a cron to hold a credential is a vault
grant to a SCRIPT cron: an agent requests it (``cron_secret_request``), the
operator approves it on the Schedule page, and at fire time the host injects
the secret into THIS process's environment — pinned to this exact file body,
executed from a private temp copy under ``python -I`` with no sibling
imports. "The grant authorizes THIS approved body, not the binaries it
calls" (``kiro_crew/cron_script.py``). This body therefore keeps the token to
itself:

1. ``git fetch`` (or the first-ever ``git clone``) of the bundle repo into the
   app's shared ``bundle-repo`` clone, with the token handed to git by
   env-var NAME inside an inline credential helper — the value never appears
   in an argv, and argv is what error messages echo.
2. ``python3 -m backend.poll`` in the installed app, with the token REMOVED
   from its environment and ``CONFIG_SYNC_PREFETCHED=1`` set, so the poll
   reads the head from the clone and makes no network call of its own.

Install: copy this file to ``~/.kiro/crew/crons/config_sync_poll.py`` and
follow ``skills/install-poll-cron/SKILL.md`` (``cron_add`` with
``script=~/.kiro/crew/crons/config_sync_poll.py:run``, then
``cron_secret_request`` mapping ``CONFIG_SYNC_GITHUB_TOKEN`` to the vault
secret holding a read-only token for the bundle repo, then approve).

Standard library only — a granted body cannot import siblings or the app.
"""

from __future__ import annotations

import errno
import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping

TOKEN_ENV = "CONFIG_SYNC_GITHUB_TOKEN"
BUNDLE_REPO_URL = "https://github.com/TGS-Labs/Kiro-Config-Bundles.git"
BUNDLE_DEFAULT_BRANCH = "main"
CLONE_DIRNAME = "bundle-repo"
PREFETCHED_ENV = "CONFIG_SYNC_PREFETCHED"

# Mirrors of the app's own resolution rules (backend/state.py,
# backend/collect.py) — kept literal here because this body cannot import them.
_STATE_DIR_ENV = "CONFIG_SYNC_STATE_DIR"
_STATE_DIR_DEFAULT = (".config-sync", "state")
_APP_ROOT_ENV = "CONFIG_SYNC_APP_ROOT"
_CREW_HOME_ENV = "KIROCREW_HOME"
_CREW_HOME_DEFAULT = (".kiro", "crew")
_APP_DIRNAME = "config-sync"

_LOCK_TIMEOUT_SECS = 60.0
_LOCK_POLL_SECS = 0.2
_GIT_TIMEOUT_SECS = 300
_POLL_TIMEOUT_SECS = 900
_TAIL_CHARS = 1500


def state_dir(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    override = env.get(_STATE_DIR_ENV)
    if override:
        return Path(override)
    return Path(env.get("HOME") or Path.home()).joinpath(*_STATE_DIR_DEFAULT)


def app_root(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    override = env.get(_APP_ROOT_ENV)
    if override:
        return Path(override)
    crew_home = env.get(_CREW_HOME_ENV)
    base = (
        Path(crew_home)
        if crew_home
        else Path(env.get("HOME") or Path.home()).joinpath(*_CREW_HOME_DEFAULT)
    )
    return base / "apps" / _APP_DIRNAME


def git_env(base: Mapping[str, str]) -> dict[str, str]:
    """The minimal environment git gets: path, home, the token, no prompts."""
    env = {k: v for k, v in base.items() if k in ("HOME", "PATH", TOKEN_ENV)}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


#: What ``backend.poll`` is allowed to inherit. An allowlist, not "everything
#: minus the token": the host seeds the granted process with its own plumbing
#: (``KIROCREW_SESSION_KEY=cron:<id>``, ``_KIROCREW_DIAL_PORT``, ...) and the
#: unpinned app code has no business holding the job's identity.
_POLL_ENV_EXACT = frozenset({"HOME", "PATH", "LANG", "TZ", "TMPDIR", "USER", "LOGNAME"})
_POLL_ENV_PREFIXES = ("LC_", "CONFIG_SYNC_", "KIROCREW_HOME", "KIRO_HOME")


def poll_env(base: Mapping[str, str]) -> dict[str, str]:
    """The environment ``backend.poll`` runs with: the allowlisted basics and
    the app's own variables, never the token, plus the prefetched flag."""
    env = {
        k: v
        for k, v in base.items()
        if k != TOKEN_ENV and (k in _POLL_ENV_EXACT or k.startswith(_POLL_ENV_PREFIXES))
    }
    env[PREFETCHED_ENV] = "1"
    return env


def _credential_helper() -> str:
    # By NAME: git's shell expands it from git's own environment at run time,
    # so the value is never part of this process's argv.
    return (
        "!f() { echo username=x-access-token; "
        'echo "password=$' + TOKEN_ENV + '"; }; f'
    )


def git_config_flags() -> list[str]:
    """The ``-c`` flags every git call here carries.

    Same hardening backend/safety/git_safety.py applies to the app's own git
    calls, plus the credential shape: the EMPTY ``credential.helper=`` first
    resets the helper list (so a global ``credential.helper=store`` can never
    see — or write — the token), then the inline helper is the only one;
    ``core.askPass=`` clears any askpass program for the same reason.
    """
    return [
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.attributesFile=/dev/null",
        "-c",
        "core.excludesFile=/dev/null",
        "-c",
        "push.recurseSubmodules=no",
        "-c",
        "core.askPass=",
        "-c",
        "credential.helper=",
        "-c",
        "credential.helper=" + _credential_helper(),
    ]


def fetch_argv(clone_dir: Path, remote_url: str) -> list[str]:
    branch = BUNDLE_DEFAULT_BRANCH
    refspec = f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
    return [
        "git",
        "-C",
        str(clone_dir),
        *git_config_flags(),
        "fetch",
        "--quiet",
        remote_url,
        refspec,
    ]


def clone_argv(clone_dir: Path, remote_url: str) -> list[str]:
    return [
        "git",
        "-C",
        str(clone_dir.parent),
        *git_config_flags(),
        "clone",
        "--quiet",
        "--branch",
        BUNDLE_DEFAULT_BRANCH,
        remote_url,
        str(clone_dir),
    ]


def _subcommand(argv: list[str]) -> str:
    """The git subcommand in *argv* (the first token after the -c pairs)."""
    i = 1
    while i < len(argv):
        if argv[i] in ("-C", "-c"):
            i += 2
            continue
        return argv[i]
    return "?"


def _run_git(argv: list[str], env: Mapping[str, str]) -> None:
    completed = subprocess.run(
        argv, env=dict(env), capture_output=True, text=True, timeout=_GIT_TIMEOUT_SECS
    )
    if completed.returncode != 0:
        # argv is safe to name (the helper references the token by name) but
        # keep the message short; git's stderr is the useful part.
        raise RuntimeError(
            f"git {_subcommand(argv)} exited {completed.returncode}: "
            f"{(completed.stderr or '')[-_TAIL_CHARS:].strip()}"
        )


def sync_clone(clone_dir: Path, remote_url: str, env: Mapping[str, str]) -> None:
    """Clone once, then fetch — under the SAME sibling lock file the app's own
    ``git_safety`` clone lock uses (``<clone_dir>.lock``), so a concurrent
    push tick and this fetch never race on the shared clone."""
    clone_dir.parent.mkdir(parents=True, exist_ok=True)
    lock_path = clone_dir.parent / f"{clone_dir.name}.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for {lock_path}") from exc
                time.sleep(_LOCK_POLL_SECS)
        try:
            if (clone_dir / ".git").exists():
                _run_git(fetch_argv(clone_dir, remote_url), env)
            else:
                _run_git(clone_argv(clone_dir, remote_url), env)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def run(ctx: Any = None) -> None:
    """Cron entrypoint: credentialed fetch, then the token-less poll."""
    base = dict(os.environ)
    if not base.get(TOKEN_ENV):
        raise RuntimeError(
            f"{TOKEN_ENV} is not set: the vault grant for this job is missing, "
            "not yet approved on the Schedule page, or pinned to an older body"
        )
    clone_dir = state_dir(base) / CLONE_DIRNAME
    sync_clone(clone_dir, BUNDLE_REPO_URL, git_env(base))

    root = app_root(base)
    completed = subprocess.run(
        [sys.executable, "-m", "backend.poll"],
        cwd=str(root),
        env=poll_env(base),
        capture_output=True,
        text=True,
        timeout=_POLL_TIMEOUT_SECS,
    )
    if completed.returncode != 0:
        tail = ((completed.stderr or "") + (completed.stdout or ""))[-_TAIL_CHARS:]
        tail = tail.replace(base[TOKEN_ENV], "[REDACTED]")
        raise RuntimeError(
            f"backend.poll exited {completed.returncode}: {tail.strip()}"
        )
    # requirements.md 4.3: a changed head notifies once. The poll prints a
    # summary ONLY on that outcome (nothing on "unchanged"), and a script
    # cron's stdout is not delivered by the host the way a non-silent
    # command cron's was — so forward it explicitly.
    summary = (completed.stdout or "").strip()
    if summary and ctx is not None and hasattr(ctx, "notify"):
        ctx.notify(summary)
