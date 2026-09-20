"""Tests for backend/push.py's hash-gate no-op path (tasks.md 3.1).

Covers design.md's "backend/push.py" component, step 3 ("If tree_hash ==
state.last_pushed_hash: return no-op. No clone, no network, no tokens.") and
requirements.md 2.1 / 2.2:

- 2.1: the push job runs as a `command`/`script` cron target (never
  `message`), so a tick consumes no LLM tokens. `app.json` already declares
  `"command": "python3 backend/push.py"` — a plain script entrypoint with no
  agent/LLM call in its invocation shape. These tests assert the module
  itself carries no LLM/agent-invocation surface at import time (no
  top-level agent/session/spawn call), which is the property that keeps a
  quiet tick free.
- 2.2: `tree_hash` is computed over the collected (`collect.collect()`),
  post-redaction (`redact.redact()`) tree. WHEN that hash equals
  `state.last_pushed_hash` THEN the job exits successfully having made NO
  network call, no clone, no commit, and no git invocation of any kind.

`git_safety.git_argv` is the single call-site every host-side git invocation
in this app must route through (see backend/safety/git_safety.py's own
docstring and tests/safety/test_git_safety.py's static grep test). The
no-op path must never construct a `git_argv`, and must never touch
`subprocess` directly either — so both are mocked/spied here and asserted
never-called, per the task's explicit instruction.

This module intentionally imports `push` (backend/push.py), which does not
exist yet. All tests below are expected to fail with a collection-time
ImportError / ModuleNotFoundError until software-engineer implements it —
this is the correct TDD starting state, not a test defect.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Iterator
from unittest.mock import MagicMock

import pytest

from backend import push


# ---------------------------------------------------------------------------
# Fixtures — matching test_collect.py's / test_state.py's isolated-roots and
# isolated-state-dir convention, so no test ever touches the real host
# configuration or the real ~/.config-sync state.
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict]:
    """Point KIROCREW_HOME (root A), KIRO_HOME (root B), and the app's own

    state directory at fresh, disposable temp directories, matching
    test_collect.py / test_state.py's convention. No test here may touch the
    real host configuration or the real ~/.config-sync state.
    """
    root_a = tmp_path / "kirocrew_home"
    root_b = tmp_path / "kiro_home"
    root_a.mkdir()
    root_b.mkdir()

    monkeypatch.setenv("KIROCREW_HOME", str(root_a))
    monkeypatch.setenv("KIRO_HOME", str(root_b))
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(tmp_path / "state"))

    yield {"root_a": root_a, "root_b": root_b, "tmp_path": tmp_path}


def _write(root: Path, relpath: str, content: bytes = b"content") -> Path:
    """Write a file at relpath under root, creating parent dirs as needed."""
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.fixture
def no_git_or_network_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[], None]:
    """Mock/spy every host-side git and subprocess surface push.py could

    reach for a clone/commit/push, plus git_safety's own hardened argv
    builder, and return an assertion helper that fails loudly if ANY of them
    was called. Applies to both `backend.push`'s own namespace (in case it
    imports names directly) and the shared `backend.safety.git_safety` /
    `subprocess` modules the design routes every host-side git call through.
    """
    from backend.safety import git_safety

    git_argv_spy = MagicMock(name="git_safety.git_argv", wraps=git_safety.git_argv)
    monkeypatch.setattr(git_safety, "git_argv", git_argv_spy)
    # Also patch the name inside backend.push's own namespace, in case it
    # did `from backend.safety.git_safety import git_argv` rather than
    # `from backend.safety import git_safety`.
    if hasattr(push, "git_argv"):
        monkeypatch.setattr(push, "git_argv", git_argv_spy)
    if hasattr(push, "git_safety"):
        monkeypatch.setattr(push.git_safety, "git_argv", git_argv_spy)

    subprocess_run_spy = MagicMock(name="subprocess.run")
    subprocess_popen_spy = MagicMock(name="subprocess.Popen")
    monkeypatch.setattr(subprocess, "run", subprocess_run_spy)
    monkeypatch.setattr(subprocess, "Popen", subprocess_popen_spy)
    if hasattr(push, "subprocess"):
        monkeypatch.setattr(push.subprocess, "run", subprocess_run_spy)
        monkeypatch.setattr(push.subprocess, "Popen", subprocess_popen_spy)

    def _assert_never_called() -> None:
        git_argv_spy.assert_not_called()
        subprocess_run_spy.assert_not_called()
        subprocess_popen_spy.assert_not_called()

    return _assert_never_called


# ---------------------------------------------------------------------------
# Requirement 2.2 — hash equality is a true no-op: no network, no clone, no
# commit, no git invocation of any kind.
# ---------------------------------------------------------------------------


def test_matching_hash_makes_no_git_or_network_call(
    isolated_roots: dict,
    no_git_or_network_calls: Callable[[], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WHEN the computed tree_hash equals state.last_pushed_hash, push.py's

    entrypoint must make no clone, no network call, no commit, and no git
    invocation of any kind — asserted by spying on git_safety.git_argv and
    both subprocess.run/Popen and confirming neither was ever called
    (requirements.md 2.2, design.md push.py step 3).
    """
    root_a = isolated_roots["root_a"]
    _write(root_a, "config.json", b'{"key": "value"}')

    # Compute the hash the same way push.py must: collect -> redact ->
    # push's own tree_hash function, over the current on-disk tree.
    from backend import collect, redact

    collected = collect.collect()
    redacted = redact.redact(collected)
    current_hash = push.tree_hash(redacted)

    from backend import state

    store = state.load_state()
    monkeypatch.setattr(store, "last_pushed_hash", current_hash, raising=False)
    monkeypatch.setattr(state, "load_state", lambda: store, raising=False)

    push.run()

    no_git_or_network_calls()


def test_matching_hash_returns_a_no_op_result(
    isolated_roots: dict,
    no_git_or_network_calls: Callable[[], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The no-op path exits SUCCESSFULLY — push.run() must not raise, and

    must report a no-op outcome the caller/cron wrapper can distinguish from
    a real push (design.md: "return no-op").
    """
    root_a = isolated_roots["root_a"]
    _write(root_a, "steering/plan.md", b"# Plan")

    from backend import collect, redact, state

    collected = collect.collect()
    redacted = redact.redact(collected)
    current_hash = push.tree_hash(redacted)

    store = state.load_state()
    monkeypatch.setattr(store, "last_pushed_hash", current_hash, raising=False)
    monkeypatch.setattr(state, "load_state", lambda: store, raising=False)

    result = push.run()

    no_git_or_network_calls()
    assert result is not None
    outcome = getattr(result, "outcome", result)
    assert str(outcome).lower() in ("no-op", "noop", "no_op")


def test_matching_hash_on_empty_tree_is_also_a_no_op(
    isolated_roots: dict,
    no_git_or_network_calls: Callable[[], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty tracked tree (nothing on disk under either root) hashes to

    SOME stable value; when that matches last_pushed_hash the no-op path
    still applies — the gate is a pure hash comparison, not conditioned on
    the tree being non-empty.
    """
    from backend import collect, redact, state

    collected = collect.collect()
    assert collected == {}
    redacted = redact.redact(collected)
    current_hash = push.tree_hash(redacted)

    store = state.load_state()
    monkeypatch.setattr(store, "last_pushed_hash", current_hash, raising=False)
    monkeypatch.setattr(state, "load_state", lambda: store, raising=False)

    push.run()

    no_git_or_network_calls()


def test_matching_hash_leaves_last_pushed_hash_unchanged(
    isolated_roots: dict,
    no_git_or_network_calls: Callable[[], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A no-op tick must not rewrite state.last_pushed_hash — it is already

    correct, and the no-op path takes no action that would touch it
    (design.md step 8: only a successful push+PR updates it).
    """
    root_a = isolated_roots["root_a"]
    _write(root_a, "config.json", b'{"key": "value"}')

    from backend import collect, redact, state

    collected = collect.collect()
    redacted = redact.redact(collected)
    current_hash = push.tree_hash(redacted)

    store = state.load_state()
    monkeypatch.setattr(store, "last_pushed_hash", current_hash, raising=False)
    monkeypatch.setattr(state, "load_state", lambda: store, raising=False)

    push.run()

    assert store.last_pushed_hash == current_hash


def test_matching_hash_never_calls_scan_content_for_secrets(
    isolated_roots: dict,
    no_git_or_network_calls: Callable[[], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The secret scan (design.md step 4) only runs on the CHANGE path —

    a true no-op does no work beyond the hash comparison, so the scanner
    must never be invoked either.
    """
    from backend import collect, redact, state
    from backend.safety import push_policy

    scan_spy = MagicMock(name="push_policy.scan_content_for_secrets")
    monkeypatch.setattr(push_policy, "scan_content_for_secrets", scan_spy)
    if hasattr(push, "push_policy"):
        monkeypatch.setattr(push.push_policy, "scan_content_for_secrets", scan_spy)

    root_a = isolated_roots["root_a"]
    _write(root_a, "config.json", b'{"key": "value"}')

    collected = collect.collect()
    redacted = redact.redact(collected)
    current_hash = push.tree_hash(redacted)

    store = state.load_state()
    monkeypatch.setattr(store, "last_pushed_hash", current_hash, raising=False)
    monkeypatch.setattr(state, "load_state", lambda: store, raising=False)

    push.run()

    no_git_or_network_calls()
    scan_spy.assert_not_called()


# ---------------------------------------------------------------------------
# Requirement 2.2 — the hash is computed over the collected, POST-REDACTION
# tree (not raw collected bytes), so the gate is correct even when only
# redacted content changed.
# ---------------------------------------------------------------------------


def test_tree_hash_is_computed_over_redacted_not_raw_content(
    isolated_roots: dict,
) -> None:
    """tree_hash() over the collected+redacted tree must differ from a hash

    over the raw collected tree whenever redaction actually changes bytes
    (e.g. a secret-bearing `headers`/`env` block) — proving push.py's gate
    input is genuinely post-redaction, matching design.md step 1-2
    ("Collect (allowlist) -> redact -> canonical serialize" then hash).
    """
    root_a = isolated_roots["root_a"]
    _write(
        root_a,
        "mcp.json",
        b'{"mcpServers": {"foo": {"headers": {"Authorization": "secret"}}}}',
    )

    from backend import collect, redact

    collected = collect.collect()
    redacted = redact.redact(collected)

    assert collected != redacted, "fixture must actually exercise redaction"
    assert push.tree_hash(collected) != push.tree_hash(redacted), (
        "tree_hash must be sensitive to redaction — the gate has to hash "
        "the POST-redaction tree, per design.md's push.py pipeline"
    )


def test_tree_hash_is_stable_across_repeated_calls_on_identical_input(
    isolated_roots: dict,
) -> None:
    """tree_hash() is a pure, deterministic function of its input: hashing

    the same collected+redacted mapping twice yields the same value, which
    is what makes the hash-equality gate meaningful at all.
    """
    root_a = isolated_roots["root_a"]
    _write(root_a, "config.json", b'{"key": "value"}')
    _write(root_a, "steering/plan.md", b"# Plan")

    from backend import collect, redact

    collected = collect.collect()
    redacted = redact.redact(collected)

    first = push.tree_hash(redacted)
    second = push.tree_hash(dict(redacted))

    assert first == second


def test_tree_hash_is_insensitive_to_input_mapping_key_order(
    isolated_roots: dict,
) -> None:
    """tree_hash() sorts by (relpath, sha256(content)) per design.md step 2,

    so two mappings with identical contents but different insertion order
    must hash identically — otherwise the gate would spuriously fire on a
    collector/redactor that merely iterates in a different order between
    ticks.
    """
    forward = {"a.json": b"one", "b.json": b"two", "c.json": b"three"}
    backward = {"c.json": b"three", "b.json": b"two", "a.json": b"one"}

    assert push.tree_hash(forward) == push.tree_hash(backward)


def test_tree_hash_changes_when_any_file_content_changes(
    isolated_roots: dict,
) -> None:
    """A one-byte content change in any single tracked file changes the

    tree hash — the gate must be sensitive to real drift, not merely to the
    file set.
    """
    before = {"config.json": b'{"key": "value"}'}
    after = {"config.json": b'{"key": "value2"}'}

    assert push.tree_hash(before) != push.tree_hash(after)


# ---------------------------------------------------------------------------
# Requirement 2.1 — the entrypoint is a plain script/command target with no
# LLM/agent-invocation surface, so a quiet tick costs no tokens.
# ---------------------------------------------------------------------------


def test_module_declares_no_llm_or_agent_invocation_names() -> None:
    """backend/push.py must import/define no LLM- or agent-invocation

    surface (no `spawn_run`, no agent/session client, no `message` cron
    payload builder) — the module's own symbol table is the property that
    keeps a no-op tick from ever being capable of spending a token, matching
    requirements.md 2.1's "never `message`" cron-target constraint and
    app.json's `"command": "python3 backend/push.py"` declaration.
    """
    forbidden_substrings = ("spawn_run", "spawn_sub_agents", "agent_runner")
    module_names = set(dir(push))
    for name in module_names:
        lowered = name.lower()
        for forbidden in forbidden_substrings:
            assert forbidden not in lowered, (
                f"backend.push defines/imports {name!r}, which names an "
                f"LLM/agent-invocation surface ({forbidden!r}) — the push "
                "cron target must have zero capability to spend tokens"
            )


def test_run_is_callable_with_no_arguments_matching_a_command_cron_target(
    isolated_roots: dict,
    no_git_or_network_calls: Callable[[], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """push.run() (or an equivalent zero-argument entrypoint the `command`

    cron invokes via `python3 backend/push.py`) must be callable with no
    arguments and no LLM/agent context — proving the entrypoint shape is a
    plain script call, not something that requires an agent session to
    invoke.
    """
    from backend import collect, redact, state

    collected = collect.collect()
    redacted = redact.redact(collected)
    current_hash = push.tree_hash(redacted)

    store = state.load_state()
    monkeypatch.setattr(store, "last_pushed_hash", current_hash, raising=False)
    monkeypatch.setattr(state, "load_state", lambda: store, raising=False)

    import inspect

    signature = inspect.signature(push.run)
    required_params = [
        p
        for p in signature.parameters.values()
        if p.default is inspect.Parameter.empty
        and p.kind
        not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    ]
    assert required_params == []

    push.run()
    no_git_or_network_calls()


# ---------------------------------------------------------------------------
# Sanity: mocking is actually wired up (a meta-test protecting the fixture
# itself from silently mocking nothing, per testing-standards.md's ban on
# assertions that cannot fail).
# ---------------------------------------------------------------------------


def test_git_argv_spy_is_reachable_and_would_be_caught_if_called(
    tmp_path: Path,
) -> None:
    """Proves the `no_git_or_network_calls` fixture's spy mechanism actually

    observes a call: patching `git_safety.git_argv` with a wrapping
    MagicMock and invoking it through the patched reference must register
    on the spy, so a future push.py that calls it would be caught rather
    than silently missed by a spy that was never actually wired to the real
    attribute.
    """
    from backend.safety import git_safety

    spy = MagicMock(wraps=git_safety.git_argv)
    original = git_safety.git_argv
    git_safety.git_argv = spy  # type: ignore[assignment]
    try:
        # A directory with no `.git` is the documented "no-repo" case
        # (git_safety._pin returns "no-repo", not an error) — a safe,
        # side-effect-free call that still registers on the spy.
        git_safety.git_argv(tmp_path, "status")
    finally:
        git_safety.git_argv = original  # type: ignore[assignment]
    assert spy.called
