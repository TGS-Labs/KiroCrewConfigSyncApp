"""Tests for the pinned host-cron script ``host-crons/config_sync_poll.py``.

KiroCrew injects an operator-approved vault secret into a SCRIPT cron only,
executes the approved body from a private temp copy with an EMPTY private
dir as ``sys.path[0]`` (no sibling imports), under ``python -I``. The grant
pins that body — "the grant authorizes THIS approved body, not the binaries
it calls" (``kiro_crew/cron_script.py``). So the script must:

* import only the standard library (nothing from ``crons/`` or the app);
* hand the token to ``git`` ONLY, by env-var NAME inside the credential
  helper (never the value in argv, which error messages echo);
* strip the token from the environment it runs ``backend.poll`` with, and
  tell the poll it is prefetched (``CONFIG_SYNC_PREFETCHED=1``);
* fail closed when the token is absent — no git call, no poll run.

The install story (an agent creates the cron with ``cron_add(script=...)``,
requests the grant with ``cron_secret_request``, the operator approves on
the Schedule page) is documented in ``skills/install-poll-cron/SKILL.md``
and pinned by the manifest test at the bottom of this file.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

APP_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = APP_ROOT / "host-crons" / "config_sync_poll.py"
SKILL_PATH = APP_ROOT / "skills" / "install-poll-cron" / "SKILL.md"
TOKEN_VALUE = "ghp_" + "x" * 36  # synthetic fixture, never a real credential


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_config_sync_poll_script", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def script() -> ModuleType:
    return _load_script()


def _git(*args: str, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.fixture()
def local_origin(tmp_path: Path) -> dict[str, Path]:
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    (work / "steering").mkdir()
    (work / "steering" / "a.md").write_text("# a\n")
    _git("add", ".", cwd=work)
    _git("commit", "-q", "-m", "one", cwd=work)
    _git("clone", "-q", "--bare", str(work), str(origin), cwd=tmp_path)
    return {"origin": origin, "work": work}


# ── the body is self-contained ───────────────────────────────────────────


def test_script_exists_and_imports_only_the_standard_library() -> None:
    assert SCRIPT_PATH.is_file(), SCRIPT_PATH
    tree = ast.parse(SCRIPT_PATH.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    non_stdlib = sorted(imported - set(sys.stdlib_module_names))
    assert non_stdlib == [], f"pinned body must be stdlib-only, found {non_stdlib}"


def test_script_exposes_the_cron_entrypoint(script: ModuleType) -> None:
    assert callable(getattr(script, "run", None))
    assert script.TOKEN_ENV == "CONFIG_SYNC_GITHUB_TOKEN"


# ── token handling ───────────────────────────────────────────────────────


def test_git_env_carries_the_token_and_poll_env_does_not(script: ModuleType) -> None:
    base = {
        "HOME": "/home/x",
        "PATH": "/usr/bin",
        script.TOKEN_ENV: TOKEN_VALUE,
        "CONFIG_SYNC_STATE_DIR": "/s",
        "KIROCREW_HOME": "/crew",
        "LC_ALL": "C.UTF-8",
        # Host plumbing the granted process is seeded with: the unpinned app
        # code must not inherit the job's identity (review M1).
        "KIROCREW_SESSION_KEY": "cron:abc",
        "_KIROCREW_DIAL_PORT": "9000",
        "UNRELATED": "drop",
    }
    git_env = script.git_env(base)
    poll_env = script.poll_env(base)

    assert git_env[script.TOKEN_ENV] == TOKEN_VALUE
    assert git_env["GIT_TERMINAL_PROMPT"] == "0"
    assert set(git_env) == {"HOME", "PATH", script.TOKEN_ENV, "GIT_TERMINAL_PROMPT"}

    assert script.TOKEN_ENV not in poll_env
    assert poll_env["CONFIG_SYNC_PREFETCHED"] == "1"
    for kept in ("HOME", "PATH", "CONFIG_SYNC_STATE_DIR", "KIROCREW_HOME", "LC_ALL"):
        assert poll_env[kept] == base[kept], kept
    for dropped in ("KIROCREW_SESSION_KEY", "_KIROCREW_DIAL_PORT", "UNRELATED"):
        assert dropped not in poll_env, dropped


def test_credential_helper_references_the_variable_by_name_never_the_value(
    script: ModuleType,
) -> None:
    argv = script.fetch_argv(Path("/tmp/clone"), "https://example.invalid/r.git")
    joined = " ".join(argv)
    assert f"${script.TOKEN_ENV}" in joined
    assert TOKEN_VALUE not in joined
    # The helper replaces, never appends to, any configured helper so the
    # sandbox's (hidden) store is never consulted and no prompt can hang.
    assert "credential.helper=" in argv
    assert any(a.startswith("credential.helper=!") for a in argv)
    assert argv[0] == "git" and "fetch" in argv


def test_fetch_argv_carries_the_same_hardening_flags_as_the_app(
    script: ModuleType,
) -> None:
    argv = script.fetch_argv(Path("/tmp/clone"), "https://example.invalid/r.git")
    for flag in (
        "core.hooksPath=",
        "core.fsmonitor=false",
        "push.recurseSubmodules=no",
    ):
        assert any(a.startswith(flag) for a in argv), flag
    assert "core.askPass=" in argv


def _credential_input() -> str:
    return "protocol=https\nhost=github.com\npath=TGS-Labs/x.git\n\n"


def _approve_input() -> str:
    return (
        _credential_input().rstrip("\n")
        + f"\nusername=x-access-token\npassword={TOKEN_VALUE}\n\n"
    )


def test_token_helper_wins_and_a_global_store_helper_never_writes_the_token(
    script: ModuleType, tmp_path: Path
) -> None:
    """Review H3. This host's own ``~/.gitconfig`` has
    ``credential.helper=store``. Run REAL git against a HOME with that exact
    config: ``credential fill`` must return the token (our inline helper is
    live), and ``credential approve`` must write NO ``.git-credentials``
    (the empty ``credential.helper=`` reset cleared the store helper before
    ours was added). Mutation: dropping the empty reset makes the approve
    step write the token to disk and this test red."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[credential]\n\thelper = store\n")
    env = script.git_env(
        {"HOME": str(home), "PATH": "/usr/bin:/bin", script.TOKEN_ENV: TOKEN_VALUE}
    )
    flags = script.git_config_flags()

    filled = subprocess.run(
        ["git", *flags, "credential", "fill"],
        input=_credential_input(),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert f"password={TOKEN_VALUE}" in filled
    assert "username=x-access-token" in filled

    subprocess.run(
        ["git", *flags, "credential", "approve"],
        input=_approve_input(),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert not (
        home / ".git-credentials"
    ).exists(), "the global store helper was still consulted: token written to disk"
    # Control: the SAME HOME without our flags DOES write the store file, so
    # the assertion above is load-bearing rather than vacuous.
    subprocess.run(
        ["git", "credential", "approve"],
        input=_approve_input(),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert (home / ".git-credentials").exists()


def test_git_failure_label_names_the_subcommand(script: ModuleType) -> None:
    assert script._subcommand(script.fetch_argv(Path("/c"), "u")) == "fetch"
    assert script._subcommand(script.clone_argv(Path("/c"), "u")) == "clone"


def test_missing_token_fails_closed_before_any_git_or_poll_call(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def _spy(argv: list[str], **_: Any) -> subprocess.CompletedProcess:
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(script.subprocess, "run", _spy)
    monkeypatch.delenv(script.TOKEN_ENV, raising=False)
    with pytest.raises(RuntimeError, match=script.TOKEN_ENV):
        script.run(ctx=None)
    assert calls == []


# ── the credentialed fetch ───────────────────────────────────────────────


def test_sync_clone_clones_then_fetches_a_real_origin(
    script: ModuleType, local_origin: dict[str, Path], tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    clone_dir = state_dir / "bundle-repo"
    env = script.git_env(
        {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", script.TOKEN_ENV: "t"}
    )

    script.sync_clone(clone_dir, str(local_origin["origin"]), env)
    first = _git("rev-parse", "refs/remotes/origin/main", cwd=clone_dir)
    assert first == _git("rev-parse", "main", cwd=local_origin["origin"])

    work = local_origin["work"]
    (work / "steering" / "b.md").write_text("# b\n")
    _git("add", ".", cwd=work)
    _git("commit", "-q", "-m", "two", cwd=work)
    _git("push", "-q", str(local_origin["origin"]), "main", cwd=work)

    script.sync_clone(clone_dir, str(local_origin["origin"]), env)
    second = _git("rev-parse", "refs/remotes/origin/main", cwd=clone_dir)
    assert second == _git("rev-parse", "main", cwd=local_origin["origin"])
    assert second != first
    # The lock file sits NEXT TO the clone, exactly where the app's own
    # clone lock lives, so the two never race.
    assert (state_dir / "bundle-repo.lock").exists()


# ── running the poll ─────────────────────────────────────────────────────


def test_run_fetches_then_runs_the_poll_prefetched_and_tokenless(
    script: ModuleType,
    local_origin: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "state"
    app_root = tmp_path / "app"
    app_root.mkdir()
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setenv("CONFIG_SYNC_APP_ROOT", str(app_root))
    monkeypatch.setenv(script.TOKEN_ENV, TOKEN_VALUE)
    monkeypatch.setattr(script, "BUNDLE_REPO_URL", str(local_origin["origin"]))

    real_run = subprocess.run
    seen: dict[str, Any] = {}

    def _spy(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        if argv[:1] == ["git"]:
            return real_run(argv, **kwargs)
        seen["argv"] = list(argv)
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, "poll ok\n", "")

    monkeypatch.setattr(script.subprocess, "run", _spy)

    script.run(ctx=None)

    assert (state_dir / "bundle-repo" / ".git").is_dir()
    assert seen["argv"][1:] == ["-m", "backend.poll"]
    assert Path(seen["kwargs"]["cwd"]) == app_root
    env = seen["kwargs"]["env"]
    assert script.TOKEN_ENV not in env
    assert env["CONFIG_SYNC_PREFETCHED"] == "1"
    assert env["CONFIG_SYNC_STATE_DIR"] == str(state_dir)


def test_run_surfaces_a_failing_poll_as_an_error(
    script: ModuleType,
    local_origin: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CONFIG_SYNC_APP_ROOT", str(tmp_path))
    monkeypatch.setenv(script.TOKEN_ENV, TOKEN_VALUE)
    monkeypatch.setattr(script, "BUNDLE_REPO_URL", str(local_origin["origin"]))
    real_run = subprocess.run

    def _spy(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        if argv[:1] == ["git"]:
            return real_run(argv, **kwargs)
        return subprocess.CompletedProcess(argv, 3, "", "boom: " + TOKEN_VALUE)

    monkeypatch.setattr(script.subprocess, "run", _spy)
    with pytest.raises(RuntimeError) as excinfo:
        script.run(ctx=None)
    message = str(excinfo.value)
    assert "exit 3" in message or "exited 3" in message
    # Whatever the child printed, the token value never reaches a message
    # the host stores or displays.
    assert TOKEN_VALUE not in message


# ── install story ────────────────────────────────────────────────────────


class _FakeCtx:
    def __init__(self) -> None:
        self.notified: list[str] = []

    def notify(self, text: str, **_: Any) -> dict[str, Any]:
        self.notified.append(text)
        return {"ok": True}


def _prepared_env(
    script: ModuleType,
    local_origin: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CONFIG_SYNC_APP_ROOT", str(tmp_path))
    monkeypatch.setenv(script.TOKEN_ENV, TOKEN_VALUE)
    monkeypatch.setattr(script, "BUNDLE_REPO_URL", str(local_origin["origin"]))


def test_run_forwards_a_non_empty_poll_summary_through_ctx_notify(
    script: ModuleType,
    local_origin: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requirement 4.3: a changed head notifies once. The old command cron
    relied on non-silent stdout delivery; the script must forward it."""
    _prepared_env(script, local_origin, tmp_path, monkeypatch)
    real_run = subprocess.run

    def _spy(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        if argv[:1] == ["git"]:
            return real_run(argv, **kwargs)
        return subprocess.CompletedProcess(argv, 0, "config-sync: head changed\n", "")

    monkeypatch.setattr(script.subprocess, "run", _spy)
    ctx = _FakeCtx()
    script.run(ctx=ctx)
    assert ctx.notified == ["config-sync: head changed"]


def test_run_stays_quiet_when_the_poll_prints_nothing(
    script: ModuleType,
    local_origin: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepared_env(script, local_origin, tmp_path, monkeypatch)
    real_run = subprocess.run

    def _spy(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        if argv[:1] == ["git"]:
            return real_run(argv, **kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(script.subprocess, "run", _spy)
    ctx = _FakeCtx()
    script.run(ctx=ctx)
    assert ctx.notified == []


def test_default_paths_mirror_the_app_s_own_resolution_rules(
    script: ModuleType,
) -> None:
    env = {"HOME": "/home/u"}
    assert script.state_dir(env) == Path("/home/u/.config-sync/state")
    assert script.app_root(env) == Path("/home/u/.kiro/crew/apps/config-sync")
    env_crew = {"HOME": "/home/u", "KIROCREW_HOME": "/srv/crew"}
    assert script.app_root(env_crew) == Path("/srv/crew/apps/config-sync")
    env_over = {
        "HOME": "/home/u",
        "CONFIG_SYNC_STATE_DIR": "/s",
        "CONFIG_SYNC_APP_ROOT": "/a",
    }
    assert script.state_dir(env_over) == Path("/s")
    assert script.app_root(env_over) == Path("/a")


def test_a_failing_fetch_surfaces_git_s_stderr_not_the_token(
    script: ModuleType, tmp_path: Path
) -> None:
    clone_dir = tmp_path / "state" / "bundle-repo"
    env = script.git_env(
        {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", script.TOKEN_ENV: TOKEN_VALUE}
    )
    with pytest.raises(RuntimeError) as excinfo:
        script.sync_clone(clone_dir, str(tmp_path / "does-not-exist.git"), env)
    message = str(excinfo.value)
    assert "exited" in message
    assert TOKEN_VALUE not in message


def test_sync_clone_is_excluded_by_the_app_s_own_clone_lock(
    script: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review M3: not just "same path" — hold the APP's real
    ``git_safety.clone_lock`` and prove the script's fetch waits on it."""
    from backend.safety import git_safety

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    clone_dir = state_dir / "bundle-repo"
    monkeypatch.setattr(script, "_LOCK_TIMEOUT_SECS", 0.3)
    monkeypatch.setattr(script, "_LOCK_POLL_SECS", 0.05)
    with git_safety.clone_lock(clone_dir):
        with pytest.raises(TimeoutError):
            script.sync_clone(clone_dir, "unused", {"PATH": "/usr/bin"})
    # And the converse: while the script holds its lock, the app's lock waits.
    import fcntl

    lock_path = state_dir / "bundle-repo.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(TimeoutError):
            with git_safety.clone_lock(
                clone_dir, timeout_secs=0.3, poll_interval_secs=0.05
            ):
                pass
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_manifest_no_longer_declares_a_command_poll_cron() -> None:
    """A command cron cannot reach the private bundle repo from the host's
    sandbox, so declaring one only produces a job that fails every tick
    until the host auto-pauses it."""
    manifest = json.loads((APP_ROOT / "app.json").read_text())
    names = [c.get("name") for c in manifest.get("crons", [])]
    assert "config-sync-poll" not in names


def test_install_skill_documents_the_granted_cron_steps() -> None:
    text = SKILL_PATH.read_text()
    for needle in (
        "cron_add",
        "cron_secret_request",
        "CONFIG_SYNC_GITHUB_TOKEN",
        "host-crons/config_sync_poll.py",
        "Schedule",
    ):
        assert needle in text, needle
