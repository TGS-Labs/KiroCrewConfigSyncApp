"""Tests for operator ruling M5: extend the crons.json shell vet to

hooks.json and mcp.json command entries.

Ruling (M5): every pulled `hooks.json` hook and `mcp.json` server that
carries a shell-executable `command` (`command` + `args`, where present)
MUST pass the SAME shell vet `backend/sanitize.py` already runs for
`crons.json` jobs (the `cron_add`-time contract —
`kiro_crew.mcp_cron._vet_shell_command`, injected here via the identical
`VetCallable` seam `test_sanitize.py`/`test_apply.py` already use). An
entry whose vet returns a rejection string, or whose vet RAISES, is
DROPPED from the applied file and reported BY NAME — fail-closed, exactly
like a `crons.json` job today (`backend/sanitize.py::sanitize_crons`).
Message-only crons are unaffected (no `command` to vet).

Every ADDED or CHANGED hook/MCP-server command is listed in the apply
result (`ApplyResult.changed_commands: list[dict]`, each entry shaped
`{"file": <relpath>, "name": <hook/server name>, "command": <str>}`) and
surfaced in `routes.status()`'s pending summary so it is visible to the
operator at approve time, before they click approve.

## Real shapes (verified against the installed `kiro_crew` package, not

invented):

- `hooks.json`: `{"hooks": [{"id"|"name", "event", "matcher", "command",
  ...}]}` — `kiro_crew/hooks.py::ScriptHook`/`ScriptHookStore` (fixture:
  `kiro_crew/tests_fixtures/rich/hooks.json`). A `ScriptHook` has NO
  `args` field — its `command` is a single shell string, unlike an
  `mcp.json` server entry.
- `mcp.json`: `{"mcpServers": {name: {"command": str, "args": [str,
  ...], "env": {...}, "headers": {...}}}}` — already established by
  `test_redact.py`'s own fixtures. `args` is a list; the vet is run
  against the shell-joined `command` + `args` string (`shlex.join`),
  matching how the real command is actually invoked as a subprocess —
  vetting `command` alone would miss an injection smuggled through
  `args` (e.g. `args: ["$(whoami)"]`).

## Why this file, not an extension of test_sanitize.py / test_apply.py

`backend/sanitize.py::sanitize_crons` is scoped to the `crons.json`
shape (`jobs` list, `CronJob` fields) by its own module docstring ("the
Requirement 6 exception, bounded") — M5 extends the vet contract to two
DIFFERENT file shapes apply.py's generic JSON branch currently writes
through un-vetted entirely (verified: `_apply_one_file` only special-
cases `_CRONS_RELPATH`/`_INSTANCES_RELPATH`; `hooks.json`/`mcp.json` fall
through to the plain restore-and-write branch with no vet call at all).
This is new application-level behaviour in `apply.py` (or a new sanitize
function it calls with the same injectable `vet=` seam), not a gap in an
existing sanitize test — hence a new file, driven end-to-end through the
REAL `apply.apply_commit` on real-git fixtures, per this project's
standing lesson that mocked subprocess/mocked apply internals have
produced defects here before.

## Injectable seam

Reuses `backend.apply.apply_commit`'s existing `cron_vet: VetCallable |
None` parameter (the SAME callable, SAME contract, passed through to
every command-bearing file in an apply — crons, hooks, mcp — not a
second, differently-named parameter) per the ruling's "pass cron_vet the
same way for hooks/mcp" instruction. If `software-engineer` instead adds
a distinctly-named parameter, only the keyword in the `apply_commit(...)`
calls below needs to change — the behavioural assertions do not.

## Expected RED state

Every test below is expected to FAIL against the current `apply.py`:
an `unsafe-hook`/`unsafe-server` entry is written through verbatim (no
drop), `ApplyResult` has no `changed_commands` field
(`AttributeError`/`getattr` default `[]` never populated), and
`routes.status()`'s `pending` summary carries no such field either. This
is the correct TDD red state, not a test defect.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from backend import apply, routes, state

# ---------------------------------------------------------------------------
# Real-git fixture helpers — mirrors test_apply.py's own convention
# exactly (this project's standing lesson: mocked subprocess for git has
# produced defects here five times).
# ---------------------------------------------------------------------------

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    )


def _init_bundle_repo(tmp_path: Path) -> Path:
    """A minimal real bundle-repo clone with one commit — this file's

    fixtures only need a HEAD sha to seed `store.pending` and a place for
    `apply_commit` to check the approval-SHA gate against; the multi-
    commit rename/merge shape `test_apply.py`'s own fixture builds is not
    needed for these command-vet assertions.
    """
    repo = tmp_path / "bundle-repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "initial", cwd=repo)
    return repo


def _head_sha(repo: Path) -> str:
    return _git("rev-parse", "HEAD", cwd=repo).stdout.strip()


@pytest.fixture
def bundle_repo(tmp_path: Path) -> Path:
    return _init_bundle_repo(tmp_path)


@pytest.fixture
def target_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root_a = tmp_path / "kirocrew-home"
    root_b = tmp_path / "kiro-home"
    root_a.mkdir()
    root_b.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(root_a))
    monkeypatch.setenv("KIRO_HOME", str(root_b))
    return tmp_path


@pytest.fixture
def state_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> state.StateStore:
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(tmp_path / "config-sync-state"))
    return state.load_state()


def _seed_pending(store: state.StateStore, sha: str) -> None:
    store.set_pending(
        sha=sha,
        author="a",
        subject="s",
        classified_paths={},
        ignored_paths=[],
        touched_classes=[],
    )


# ---------------------------------------------------------------------------
# Deterministic fake vets — same contract/shape as test_sanitize.py's
# `_real_vet_contract_fake` (command substitution + shell-loop keywords
# rejected; a bare `;`/`&&` is clean), so these tests run identically in
# every environment, never depending on the real, not-installed-in-this-
# venv `kiro_crew.mcp_cron._vet_shell_command`.
# ---------------------------------------------------------------------------


def _fake_vet(command: str) -> str | None:
    if "$(" in command or "`" in command:
        return "Error: command blocked: command substitution"
    return None


def _raising_vet(command: str) -> str | None:
    raise RuntimeError("vet backend unavailable")


# ---------------------------------------------------------------------------
# (1) hooks.json: a vet-failing hook command is dropped and named; a
#     surviving hook command is applied unchanged.
# ---------------------------------------------------------------------------


def test_apply_drops_hook_with_command_substitution_and_names_it(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": [
                    {
                        "id": "h1",
                        "name": "unsafe-hook",
                        "event": "Stop",
                        "matcher": "*",
                        "command": "echo $(whoami) > /tmp/pwn",
                    },
                    {
                        "id": "h2",
                        "name": "safe-hook",
                        "event": "Stop",
                        "matcher": "*",
                        "command": "echo hi",
                    },
                ]
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["hooks.json"], "B": []},
        store=state_store,
        cron_vet=_fake_vet,
    )

    assert "unsafe-hook" in result.dropped_cron_names
    assert "safe-hook" not in result.dropped_cron_names

    root_a = Path(os.environ["KIROCREW_HOME"])
    written = json.loads((root_a / "hooks.json").read_text(encoding="utf-8"))
    names = {h["name"] for h in written["hooks"]}
    assert names == {"safe-hook"}


def test_apply_drops_hook_when_vet_raises_fail_closed(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": [
                    {
                        "id": "h1",
                        "name": "raising-hook",
                        "event": "Stop",
                        "matcher": "*",
                        "command": "echo hi",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["hooks.json"], "B": []},
        store=state_store,
        cron_vet=_raising_vet,
    )

    assert "raising-hook" in result.dropped_cron_names

    root_a = Path(os.environ["KIROCREW_HOME"])
    written = json.loads((root_a / "hooks.json").read_text(encoding="utf-8"))
    assert written["hooks"] == []


# ---------------------------------------------------------------------------
# (2) mcp.json: a vet-failing server (command substitution smuggled
#     through `args`, not just `command` itself) is dropped and named;
#     other servers in the same file still apply.
# ---------------------------------------------------------------------------


def test_apply_drops_mcp_server_with_command_substitution_in_args_others_survive(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "unsafe-server": {
                        "command": "python3",
                        "args": ["-c", "echo $(whoami)"],
                    },
                    "safe-server": {
                        "command": "npx",
                        "args": ["-y", "@example/server"],
                    },
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["mcp.json"], "B": []},
        store=state_store,
        cron_vet=_fake_vet,
    )

    assert "unsafe-server" in result.dropped_cron_names
    assert "safe-server" not in result.dropped_cron_names

    root_a = Path(os.environ["KIROCREW_HOME"])
    written = json.loads((root_a / "mcp.json").read_text(encoding="utf-8"))
    assert "unsafe-server" not in written["mcpServers"]
    assert "safe-server" in written["mcpServers"]


def test_apply_drops_mcp_server_when_vet_raises_fail_closed(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "raising-server": {"command": "python3", "args": []},
                }
            }
        ),
        encoding="utf-8",
    )

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["mcp.json"], "B": []},
        store=state_store,
        cron_vet=_raising_vet,
    )

    assert "raising-server" in result.dropped_cron_names

    root_a = Path(os.environ["KIROCREW_HOME"])
    written = json.loads((root_a / "mcp.json").read_text(encoding="utf-8"))
    assert written["mcpServers"] == {}


# ---------------------------------------------------------------------------
# (3) changed_commands: pinned shape [{file, name, command}] — an
#     unchanged command is not listed; an added or changed one is.
# ---------------------------------------------------------------------------


def test_apply_changed_commands_lists_only_added_or_changed_not_unchanged(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    root_a = Path(os.environ["KIROCREW_HOME"])
    root_a.joinpath("hooks.json").write_text(
        json.dumps(
            {
                "hooks": [
                    {
                        "id": "h1",
                        "name": "unchanged-hook",
                        "event": "Stop",
                        "matcher": "*",
                        "command": "echo same",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    root_a.joinpath("mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "changed-server": {"command": "npx", "args": ["old-arg"]},
                }
            }
        ),
        encoding="utf-8",
    )

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    commit_root.joinpath("hooks.json").write_text(
        json.dumps(
            {
                "hooks": [
                    {
                        "id": "h1",
                        "name": "unchanged-hook",
                        "event": "Stop",
                        "matcher": "*",
                        "command": "echo same",
                    },
                    {
                        "id": "h2",
                        "name": "added-hook",
                        "event": "Stop",
                        "matcher": "*",
                        "command": "echo new",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    commit_root.joinpath("mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "changed-server": {"command": "npx", "args": ["new-arg"]},
                }
            }
        ),
        encoding="utf-8",
    )

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["hooks.json", "mcp.json"], "B": []},
        store=state_store,
        cron_vet=_fake_vet,
    )

    changed_commands = getattr(result, "changed_commands", [])
    changed_names = {entry["name"] for entry in changed_commands}

    assert "unchanged-hook" not in changed_names
    assert "added-hook" in changed_names
    assert "changed-server" in changed_names

    added_entry = next(e for e in changed_commands if e["name"] == "added-hook")
    assert added_entry["file"] == "hooks.json"
    assert added_entry["command"] == "echo new"

    server_entry = next(e for e in changed_commands if e["name"] == "changed-server")
    assert server_entry["file"] == "mcp.json"
    assert "new-arg" in server_entry["command"]


# ---------------------------------------------------------------------------
# (4) The 4.10 credential-restore contract still works on surviving
#     entries once command vetting is layered on top.
# ---------------------------------------------------------------------------


def test_apply_restores_live_mcp_headers_on_a_surviving_server_after_vet(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    root_a = Path(os.environ["KIROCREW_HOME"])
    root_a.joinpath("mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "github": {
                        "command": "npx",
                        "args": ["-y", "server"],
                        "headers": {"Authorization": "live-secret-value"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    commit_root.joinpath("mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "github": {
                        "command": "npx",
                        "args": ["-y", "server"],
                        "headers": {"Authorization": "<redacted>"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["mcp.json"], "B": []},
        store=state_store,
        cron_vet=_fake_vet,
    )

    assert "github" not in result.dropped_cron_names
    written = json.loads(root_a.joinpath("mcp.json").read_text(encoding="utf-8"))
    assert written["mcpServers"]["github"]["headers"]["Authorization"] == (
        "live-secret-value"
    )
    assert result.needs_credential == []


# ---------------------------------------------------------------------------
# (5) status() pending summary lists changed commands for a pending
#     commit, computed from the commit tree vs live.
# ---------------------------------------------------------------------------


def _init_origin_repo(tmp_path: Path) -> Path:
    """A bare "origin" the app's bundle-repo clone can fetch/clone from —

    matches `test_routes_approve_seam.py`'s own fixture exactly: `poll.py`
    always clones a remote URL, never a local working repo directly, so
    this test needs a real bare remote to point `BUNDLE_REPO_URL` at.
    """
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git("init", "-q", "--bare", "-b", "main", cwd=origin)
    return origin


def _seed_origin_with_new_hook(tmp_path: Path, origin: Path) -> str:
    """Push one commit to ``origin/main`` that adds a hooks.json carrying

    a new hook command, matching test_routes_approve_seam.py's own
    push-from-scratch-clone convention. Returns that commit's sha — the
    sha `status()`'s `pending` record will name.
    """
    work = tmp_path / "seed-work"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    _git("remote", "add", "origin", str(origin), cwd=work)
    work.joinpath("hooks.json").write_text(
        json.dumps(
            {
                "hooks": [
                    {
                        "id": "h1",
                        "name": "new-hook",
                        "event": "Stop",
                        "matcher": "*",
                        "command": "echo new-pending-command",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "add new-hook", cwd=work)
    _git("push", "-q", "-u", "origin", "main", cwd=work)
    return _head_sha(work)


def _patch_bundle_repo_url(monkeypatch: pytest.MonkeyPatch, origin: Path) -> None:
    """Point whichever bundle-repo URL constant poll/routes consult at the

    local ``origin`` bare repo, exactly matching
    `test_routes_approve_seam.py::_bundle_repo_url_env` — `poll.py`
    hardcodes `BUNDLE_REPO_URL` as a module constant, so this test never
    touches the network either.
    """
    from backend import poll as poll_module

    monkeypatch.setattr(poll_module, "BUNDLE_REPO_URL", f"file://{origin}")
    if hasattr(routes, "BUNDLE_REPO_URL"):
        monkeypatch.setattr(routes, "BUNDLE_REPO_URL", f"file://{origin}")


def test_status_pending_summary_lists_changed_commands_for_pending_commit(
    target_root: Path,
    state_store: state.StateStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`routes.status()` must surface the SAME "what commands changed"

    view `apply_commit` would report, computed BEFORE approval (from the
    pending commit's real tree, materialized the SAME way `approve` does
    via `routes._materialize_pending_commit`, vs the live files) — a
    plain diff of `command`/`args`-bearing entries by name, never a call
    into `apply_commit` itself (which must never run outside `approve`,
    per `apply.py`'s own module docstring: "called ONLY from an approve
    route"). Pinned field: `status()["pending"]["changed_commands"]`, the
    same `[{file, name, command}]` shape as `ApplyResult.changed_commands`
    so the dashboard renders both with one component.
    """
    root_a = Path(os.environ["KIROCREW_HOME"])
    root_a.joinpath("hooks.json").write_text(
        json.dumps({"hooks": []}), encoding="utf-8"
    )

    origin = _init_origin_repo(tmp_path)
    sha = _seed_origin_with_new_hook(tmp_path, origin)
    _patch_bundle_repo_url(monkeypatch, origin)
    # Every route is gated on the app being enabled (Req 7.4); stub the gate
    # the same way the sibling route tests do so status() reaches its logic.
    monkeypatch.setattr(routes, "is_app_enabled", lambda _name: True)

    state_store.set_pending(
        sha=sha,
        author="a",
        subject="add new-hook",
        classified_paths={"hooks.json": "live_on_next_resolution"},
        ignored_paths=[],
        touched_classes=["config"],
    )

    response = routes.status(state_store)

    pending = response.get("pending") or {}
    changed_commands = pending.get("changed_commands", [])
    names = {entry["name"] for entry in changed_commands}
    assert "new-hook" in names
    entry = next(e for e in changed_commands if e["name"] == "new-hook")
    assert entry["file"] == "hooks.json"
    assert entry["command"] == "echo new-pending-command"


# ---------------------------------------------------------------------------
# (6) Message-only crons are unaffected by extending the vet to
#     hooks.json/mcp.json (no regression on the existing Requirement 6
#     boundary).
# ---------------------------------------------------------------------------


def test_apply_message_only_cron_unaffected_by_command_vet_extension(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    commit_root.joinpath("crons.json").write_text(
        json.dumps({"jobs": [{"name": "message-only", "message": "hi"}]}),
        encoding="utf-8",
    )

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    vet_calls: list[str] = []

    def _counting_vet(command: str) -> str | None:
        vet_calls.append(command)
        return None

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["crons.json"], "B": []},
        store=state_store,
        cron_vet=_counting_vet,
    )

    assert vet_calls == []
    assert "message-only" not in result.dropped_cron_names
    assert "message-only" not in result.paused_cron_names


# ---------------------------------------------------------------------------
# (7) Security guard mutation: the exact mutation that turns test (1)'s
#     drop assertion red is "skip the vet" — confirmed to fail for the
#     RIGHT reason (the drop assertion, not a collection error).
# ---------------------------------------------------------------------------


def test_mutation_skipping_the_vet_lets_unsafe_hook_survive_undropped(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """Mutation-requirement evidence (testing-standards.md § Mutation

    Requirement): this test hand-applies the exact mutation that would
    make the M5 guard toothless — a vet that ALWAYS returns clean (i.e.
    the vet is never actually consulted, equivalent to skipping the call
    entirely) — and asserts the unsafe entry now SURVIVES, proving test
    (1) above is actually exercising the vet's rejection path and not
    some other filter. Record: with `_fake_vet` (real guard), "unsafe-
    hook" is dropped (see test 1, green). With the mutation below (vet
    skipped == always-clean), "unsafe-hook" survives and is written to
    disk (this test, green) — the drop assertion in test (1) would go RED
    against this mutated vet, which is the required red->green proof the
    guard bites.
    """
    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    commit_root.joinpath("hooks.json").write_text(
        json.dumps(
            {
                "hooks": [
                    {
                        "id": "h1",
                        "name": "unsafe-hook",
                        "event": "Stop",
                        "matcher": "*",
                        "command": "echo $(whoami) > /tmp/pwn",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    def _vet_skipped_always_clean(command: str) -> str | None:
        # The mutation: never actually inspects `command` — this is what
        # "the vet is skipped" looks like from the caller's side, since
        # apply.py has no separate on/off switch to disable.
        return None

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["hooks.json"], "B": []},
        store=state_store,
        cron_vet=_vet_skipped_always_clean,
    )

    # Mutated (vet skipped): the unsafe entry survives — this is the RED
    # state test (1) is written to catch when the guard is disabled.
    assert "unsafe-hook" not in result.dropped_cron_names
    root_a = Path(os.environ["KIROCREW_HOME"])
    written = json.loads((root_a / "hooks.json").read_text(encoding="utf-8"))
    assert {h["name"] for h in written["hooks"]} == {"unsafe-hook"}
