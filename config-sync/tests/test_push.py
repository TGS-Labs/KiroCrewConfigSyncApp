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


# ---------------------------------------------------------------------------
# Change-path tests (tasks.md 3.2, requirements.md 2.3, 2.4, 2.5, 3.1, 3.6,
# 3.8) — appended after the no-op-path tests above, which are untouched.
#
# `backend/push.py` currently raises NotImplementedError at the TODO(3.2)
# seam whenever `tree_hash` differs from `state.last_pushed_hash`. Every test
# below drives `push.run()` down that seam and is expected to fail RED
# against the current module with that NotImplementedError — not a
# collection error, not an AttributeError from a wrong mock target. That is
# the correct TDD starting state for this path.
#
# The change path's four collaborators are mocked at `backend.push`'s own
# module namespace (`push.push_policy`, `push.git_safety`, `push.redact`),
# matching this file's existing `from backend.safety import git_safety,
# push_policy` import shape and the no-op tests' dual-patch convention (the
# real modules AND the names inside `backend.push`, in case it imports
# names directly rather than the module).
# ---------------------------------------------------------------------------


def _seed_changed_hash(monkeypatch: pytest.MonkeyPatch) -> tuple:
    """Seed state so the computed tree_hash differs from last_pushed_hash,

    driving push.run() down the change-path seam rather than the no-op
    early return. Returns (store, current_hash, redacted_tree) so a test
    can assert against the exact bytes push.py must have written.
    """
    from backend import collect, redact, state

    collected = collect.collect()
    redacted = redact.redact(collected)
    current_hash = push.tree_hash(redacted)

    store = state.load_state()
    monkeypatch.setattr(store, "last_pushed_hash", "a-completely-different-hash")
    monkeypatch.setattr(state, "load_state", lambda: store, raising=False)

    return store, current_hash, redacted


@pytest.fixture
def change_path_collaborators(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[dict]:
    """Patch every change-path collaborator (scan, git_argv, authorize, and

    message redaction) at BOTH the real module and `backend.push`'s own
    namespace, so the test controls each decision point independently
    regardless of which import form push.py uses. Defaults are permissive
    (clean scan, push authorized, git_argv/redact_message pass-through) so
    an individual test only needs to override the one collaborator whose
    behaviour it is asserting on.
    """
    from backend.safety import git_safety, push_policy, redact_msg

    scan_mock = MagicMock(
        name="push_policy.scan_content_for_secrets", return_value=(True, "ok")
    )
    authorize_mock = MagicMock(
        name="push_policy.authorize_direct_push",
        return_value=(True, "push authorized"),
    )
    git_argv_mock = MagicMock(
        name="git_safety.git_argv",
        side_effect=lambda cwd, *args: ["git", "-C", str(cwd), *args],
    )
    redact_message_mock = MagicMock(
        name="redact_msg.redact_message", side_effect=lambda text: text
    )
    subprocess_run_mock = MagicMock(name="subprocess.run")

    monkeypatch.setattr(push_policy, "scan_content_for_secrets", scan_mock)
    monkeypatch.setattr(push_policy, "authorize_direct_push", authorize_mock)
    monkeypatch.setattr(git_safety, "git_argv", git_argv_mock)
    monkeypatch.setattr(redact_msg, "redact_message", redact_message_mock)
    monkeypatch.setattr(subprocess, "run", subprocess_run_mock)
    monkeypatch.setattr(subprocess, "Popen", MagicMock(name="subprocess.Popen"))

    # Mirror onto backend.push's own namespace for whichever import form it
    # actually uses (module-attribute access vs a direct name import).
    if hasattr(push, "push_policy"):
        monkeypatch.setattr(push.push_policy, "scan_content_for_secrets", scan_mock)
        monkeypatch.setattr(push.push_policy, "authorize_direct_push", authorize_mock)
    if hasattr(push, "scan_content_for_secrets"):
        monkeypatch.setattr(push, "scan_content_for_secrets", scan_mock)
    if hasattr(push, "authorize_direct_push"):
        monkeypatch.setattr(push, "authorize_direct_push", authorize_mock)
    if hasattr(push, "git_safety"):
        monkeypatch.setattr(push.git_safety, "git_argv", git_argv_mock)
    if hasattr(push, "git_argv"):
        monkeypatch.setattr(push, "git_argv", git_argv_mock)
    if hasattr(push, "redact_msg"):
        monkeypatch.setattr(push.redact_msg, "redact_message", redact_message_mock)
    if hasattr(push, "redact_message"):
        monkeypatch.setattr(push, "redact_message", redact_message_mock)
    if hasattr(push, "subprocess"):
        monkeypatch.setattr(push.subprocess, "run", subprocess_run_mock)

    yield {
        "scan": scan_mock,
        "authorize": authorize_mock,
        "git_argv": git_argv_mock,
        "redact_message": redact_message_mock,
        "subprocess_run": subprocess_run_mock,
    }


# ---------------------------------------------------------------------------
# Requirements 2.4 / 3.6 — authorize_direct_push refusals on main/protected/
# empty/ambiguous targets, each with a non-empty reason string, and no git
# subprocess call happens when it refuses.
# ---------------------------------------------------------------------------


class TestChangePathBranchAuthorizationRefusals:
    """The change path must call authorize_direct_push and honor a refusal —

    never push when the target branch is main, protected, empty, or
    ambiguous, and always surface the policy's own reason string rather
    than a generic error.
    """

    @pytest.mark.parametrize(
        "refusal_reason",
        [
            "branch 'main' is protected/shared — push is refused "
            "(the protected-branch denylist is non-overridable)",
            "branch 'develop' is protected/shared — push is refused "
            "(the protected-branch denylist is non-overridable)",
            "no branch configured — refusing to push to an empty/ambiguous target",
        ],
        ids=["main", "protected", "empty-or-ambiguous"],
    )
    def test_refuses_and_reports_reason_when_target_is_unauthorized(
        self,
        isolated_roots: dict,
        change_path_collaborators: dict,
        monkeypatch: pytest.MonkeyPatch,
        refusal_reason: str,
    ) -> None:
        root_a = isolated_roots["root_a"]
        _write(root_a, "config.json", b'{"key": "value"}')

        store, current_hash, _redacted = _seed_changed_hash(monkeypatch)
        change_path_collaborators["authorize"].return_value = (False, refusal_reason)

        result = push.run()

        outcome = getattr(result, "outcome", result)
        assert "refus" in str(outcome).lower() or str(outcome).lower() not in (
            "no-op",
            "noop",
            "no_op",
        ), "an authorization refusal must not be reported as a no-op"
        rendered = " ".join(
            str(getattr(result, attr, "")) for attr in ("outcome", "reason", "note")
        )
        assert refusal_reason in rendered or refusal_reason in str(result), (
            "the policy's own refusal reason string must be surfaced, not a "
            "generic error message"
        )

    def test_refuses_all_four_authorization_targets_with_distinct_reasons(
        self,
        isolated_roots: dict,
        change_path_collaborators: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A degenerate implementation that returns one constant refusal

        string for every target must not pass — main/protected/empty must
        each surface authorize_direct_push's OWN distinct reason.
        """
        root_a = isolated_roots["root_a"]
        _write(root_a, "config.json", b'{"key": "value"}')

        reasons = {
            "main": "branch 'main' is protected/shared — push is refused "
            "(the protected-branch denylist is non-overridable)",
            "protected": "branch 'trunk' is protected/shared — push is "
            "refused (the protected-branch denylist is non-overridable)",
            "ambiguous": "no branch configured — refusing to push to an "
            "empty/ambiguous target",
        }
        rendered_by_case = {}
        for case, reason in reasons.items():
            _store, _hash, _redacted = _seed_changed_hash(monkeypatch)
            change_path_collaborators["authorize"].return_value = (False, reason)
            result = push.run()
            rendered_by_case[case] = str(result)

        assert len(set(rendered_by_case.values())) == len(rendered_by_case), (
            "each authorization-refusal target must surface a DISTINCT "
            f"reason string, got: {rendered_by_case}"
        )

    def test_no_git_subprocess_call_when_authorization_refuses(
        self,
        isolated_roots: dict,
        change_path_collaborators: dict,
        no_git_or_network_calls: Callable[[], None],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An authorization refusal must happen BEFORE any git invocation —

        no clone, no checkout, no commit, no push subprocess call of any
        kind once authorize_direct_push says no.
        """
        root_a = isolated_roots["root_a"]
        _write(root_a, "config.json", b'{"key": "value"}')

        _store, _hash, _redacted = _seed_changed_hash(monkeypatch)
        change_path_collaborators["authorize"].return_value = (
            False,
            "branch 'main' is protected/shared — push is refused "
            "(the protected-branch denylist is non-overridable)",
        )

        push.run()

        no_git_or_network_calls()
        change_path_collaborators["subprocess_run"].assert_not_called()

    def test_last_pushed_hash_unchanged_on_authorization_refusal(
        self,
        isolated_roots: dict,
        change_path_collaborators: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A refused push must never advance last_pushed_hash (design.md

        step 8: only a push+PR success does) — a refusal is not a partial
        success.
        """
        root_a = isolated_roots["root_a"]
        _write(root_a, "config.json", b'{"key": "value"}')

        store, _current_hash, _redacted = _seed_changed_hash(monkeypatch)
        seeded_hash = store.last_pushed_hash
        change_path_collaborators["authorize"].return_value = (
            False,
            "no branch configured — refusing to push to an empty/ambiguous target",
        )

        push.run()

        assert store.last_pushed_hash == seeded_hash


# ---------------------------------------------------------------------------
# Requirement 3.6 — a scan finding refuses the WHOLE push, reporting only
# the code/count (never matched text), and makes NO git subprocess call at
# all — not even a clone probe.
# ---------------------------------------------------------------------------


class TestChangePathSecretScanRefusal:
    def test_refuses_on_scan_finding_with_code_and_count_only(
        self,
        isolated_roots: dict,
        change_path_collaborators: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root_a = isolated_roots["root_a"]
        _write(root_a, "config.json", b'{"key": "value"}')

        _store, _hash, _redacted = _seed_changed_hash(monkeypatch)
        change_path_collaborators["scan"].return_value = (False, "hit: 3 finding(s)")

        result = push.run()

        rendered = str(result)
        assert (
            "3 finding" in rendered or "hit" in rendered.lower()
        ), "the scan refusal's code/count must be surfaced to the caller"
        # Never the matched text — the scanner's own contract is to return
        # only a code and a count, so nothing resembling secret content can
        # appear even if a defective caller tried to interpolate it.
        assert "secret" not in rendered.lower() or "finding" in rendered.lower()

    def test_no_git_subprocess_call_of_any_kind_on_scan_finding(
        self,
        isolated_roots: dict,
        change_path_collaborators: dict,
        no_git_or_network_calls: Callable[[], None],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Requirement 3.6's 'not rewritten, not partially committed': a scan

        finding must refuse BEFORE any clone/checkout probe — not just
        before the final push. Not even a read-only `git ls-remote`/clone
        of the bundle repo may occur once the scan is dirty.
        """
        root_a = isolated_roots["root_a"]
        _write(root_a, "config.json", b'{"key": "value"}')

        _store, _hash, _redacted = _seed_changed_hash(monkeypatch)
        change_path_collaborators["scan"].return_value = (False, "hit: 1 finding(s)")

        push.run()

        no_git_or_network_calls()
        change_path_collaborators["git_argv"].assert_not_called()
        change_path_collaborators["subprocess_run"].assert_not_called()

    def test_authorize_direct_push_never_called_when_scan_finds_a_hit(
        self,
        isolated_roots: dict,
        change_path_collaborators: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The scan gate comes first: a dirty scan must short-circuit before

        branch authorization is even consulted, since there is nothing to
        authorize once the whole push is refused.
        """
        root_a = isolated_roots["root_a"]
        _write(root_a, "config.json", b'{"key": "value"}')

        _store, _hash, _redacted = _seed_changed_hash(monkeypatch)
        change_path_collaborators["scan"].return_value = (False, "hit: 2 finding(s)")

        push.run()

        change_path_collaborators["authorize"].assert_not_called()

    def test_last_pushed_hash_unchanged_on_scan_finding(
        self,
        isolated_roots: dict,
        change_path_collaborators: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root_a = isolated_roots["root_a"]
        _write(root_a, "config.json", b'{"key": "value"}')

        store, _current_hash, _redacted = _seed_changed_hash(monkeypatch)
        seeded_hash = store.last_pushed_hash
        change_path_collaborators["scan"].return_value = (False, "hit: 1 finding(s)")

        push.run()

        assert store.last_pushed_hash == seeded_hash

    def test_refuses_when_scanner_unavailable_fail_closed(
        self,
        isolated_roots: dict,
        change_path_collaborators: dict,
        no_git_or_network_calls: Callable[[], None],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Requirement 3.7: an unimportable/unrunnable scanner fails CLOSED —

        SCAN_NO_SCANNER must refuse the push exactly like a real finding,
        never proceed as if the content were clean.
        """
        root_a = isolated_roots["root_a"]
        _write(root_a, "config.json", b'{"key": "value"}')

        _store, _hash, _redacted = _seed_changed_hash(monkeypatch)
        change_path_collaborators["scan"].return_value = (False, "no_scanner")

        push.run()

        no_git_or_network_calls()
        change_path_collaborators["authorize"].assert_not_called()


# ---------------------------------------------------------------------------
# Requirement 3.1 / fail-closed on I/O — an unreadable tracked file refuses
# the whole operation rather than committing a partial tree.
# ---------------------------------------------------------------------------


class TestChangePathUnreadableFileFailsClosed:
    def test_refuses_with_no_partial_operation_on_unreadable_tracked_file(
        self,
        isolated_roots: dict,
        change_path_collaborators: dict,
        no_git_or_network_calls: Callable[[], None],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A tracked file that raises OSError when its bytes are written into

        the working copy (design.md step 5, AFTER the scan/authorize gates
        both clear) must refuse the whole operation — no partial commit, no
        push, and last_pushed_hash left untouched. This targets the
        change-path's own write-to-working-copy stage specifically (not
        collect.collect(), which already propagates I/O errors today and
        would pass against the unimplemented seam) — an unreadable/
        unwritable tracked file discovered while materializing the working
        copy must not leave a half-written tree behind.
        """
        root_a = isolated_roots["root_a"]
        _write(root_a, "config.json", b'{"key": "value"}')
        _write(root_a, "steering/plan.md", b"# Plan")

        store, _current_hash, _redacted = _seed_changed_hash(monkeypatch)
        seeded_hash = store.last_pushed_hash

        unwritable_error = OSError("Permission denied: steering/plan.md")

        def _spy_write_bytes(self: Path, data: bytes) -> int:
            if self.name == "plan.md":
                raise unwritable_error
            return len(data)

        monkeypatch.setattr(Path, "write_bytes", _spy_write_bytes)

        with pytest.raises(OSError):
            push.run()

        no_git_or_network_calls()
        change_path_collaborators["subprocess_run"].assert_not_called()
        assert store.last_pushed_hash == seeded_hash


# ---------------------------------------------------------------------------
# Requirement 3.8 / design.md step 5 — the working copy's bytes must equal
# redact.redact()'s ACTUAL output, byte-for-byte, compared against the real
# call rather than a value the test re-derives independently.
# ---------------------------------------------------------------------------


class TestChangePathWorkingCopyBytesMatchRedactOutput:
    def test_written_bytes_equal_actual_redact_output_byte_for_byte(
        self,
        isolated_roots: dict,
        change_path_collaborators: dict,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Capture every file-write push.py performs during the change path

        (via a MagicMock wrapping Path.write_bytes) and assert each write's
        content is IDENTICAL to what the real `redact.redact()` call
        produced for that path — never re-derived by the test, but read
        back from the actual redact() call's return value, per the task's
        explicit instruction not to re-derive the expected bytes.
        """
        from backend import redact as redact_module

        root_a = isolated_roots["root_a"]
        _write(
            root_a,
            "mcp.json",
            b'{"mcpServers": {"foo": {"headers": {"Authorization": "secret"}}}}',
        )
        _write(root_a, "steering/plan.md", b"# Plan\ncontent")

        _store, _hash, _redacted = _seed_changed_hash(monkeypatch)

        actual_redacted_by_path: dict = {}
        real_redact = redact_module.redact

        def _capturing_redact(collected: dict) -> dict:
            result = real_redact(collected)
            actual_redacted_by_path.clear()
            actual_redacted_by_path.update(result)
            return result

        monkeypatch.setattr(redact_module, "redact", _capturing_redact)
        if hasattr(push, "redact"):
            monkeypatch.setattr(push, "redact", redact_module)

        write_calls: list[tuple[Path, bytes]] = []
        original_write_bytes = Path.write_bytes

        def _spy_write_bytes(self: Path, data: bytes) -> int:
            write_calls.append((self, bytes(data)))
            return original_write_bytes(self, data)

        monkeypatch.setattr(Path, "write_bytes", _spy_write_bytes)

        push.run()

        assert actual_redacted_by_path, (
            "redact.redact() must actually have been called on the change "
            "path — nothing to compare against otherwise"
        )
        assert write_calls, (
            "the change path must write the redacted tree to a working " "copy on disk"
        )
        for written_path, written_bytes in write_calls:
            matches = [
                content
                for relpath, content in actual_redacted_by_path.items()
                if written_path.name == Path(relpath).name
            ]
            if not matches:
                continue
            assert written_bytes in matches, (
                f"bytes written to {written_path} do not byte-for-byte match "
                "the actual redact.redact() output for that file — the "
                "working copy must contain redact()'s real return value, "
                "not a re-derived or re-serialized approximation"
            )


# ---------------------------------------------------------------------------
# Requirement 2.6 / review-fix C1+C2 — a bare successful branch push must
# NOT advance `last_pushed_hash` on its own, and `run()`'s real production
# entrypoint must actually reach `pr_handoff.handle_pushed_branch` with the
# real `StateStore` (not just be testable in isolation via a fake, per
# tests/test_pr_handoff.py). `last_pushed_hash` only advances once
# `pr_handoff.confirm_pr_created` is called out-of-band — never at push
# time, matching design.md step 8.
# ---------------------------------------------------------------------------


class TestChangePathSuccessDoesNotConfirmPrItself:
    """A successful branch push through `push.run()`'s REAL entrypoint

    (real `StateStore`, real `pr_handoff` module — only the git subprocess
    calls and policy checks are mocked via `change_path_collaborators`)
    must record the push, hand off to `pr_handoff`, and leave
    `last_pushed_hash` unchanged — because no PR has been confirmed yet.
    """

    def test_last_pushed_hash_does_not_advance_on_bare_branch_push(
        self,
        isolated_roots: dict,
        change_path_collaborators: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root_a = isolated_roots["root_a"]
        _write(root_a, "config.json", b'{"key": "value"}')

        store, _current_hash, _redacted = _seed_changed_hash(monkeypatch)
        seeded_hash = store.last_pushed_hash

        # Let the real pr_handoff.handle_pushed_branch run against the real
        # StateStore, but stub its own external calls (payload build +
        # notify) so the test stays offline — this is the seam
        # test_pr_handoff.py itself exercises in isolation; here we only
        # need push.run()'s real entrypoint to actually reach it.
        from backend import pr_handoff

        monkeypatch.setattr(
            pr_handoff,
            "build_pull_request_payload",
            MagicMock(
                return_value={
                    "repo": "TGS-Labs/Kiro-Config-Bundles",
                    "base": "main",
                    "head": "irrelevant",
                    "title": "chore: sync",
                    "body": "Automated config sync.",
                }
            ),
        )
        monkeypatch.setattr(pr_handoff, "notify_operator", MagicMock())

        result = push.run()

        assert getattr(result, "outcome", None) == "pushed"
        assert store.last_pushed_hash == seeded_hash, (
            "a bare branch push must never advance last_pushed_hash — only "
            "pr_handoff.confirm_pr_created (a confirmed PR) may do that"
        )

    def test_run_reaches_pr_handoff_with_the_real_state_store(
        self,
        isolated_roots: dict,
        change_path_collaborators: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """push.run()'s production path must actually call

        `pr_handoff.handle_pushed_branch` — not merely be structured so a
        test COULD call it in isolation. Spies on the real `pr_handoff`
        module's `handle_pushed_branch` (imported inside `push.run()`) and
        asserts it was invoked with the real `StateStore` instance `run()`
        loaded, proving the wiring exists in production, not only in
        `tests/test_pr_handoff.py`'s per-module fakes.
        """
        root_a = isolated_roots["root_a"]
        _write(root_a, "config.json", b'{"key": "value"}')

        store, current_hash, _redacted = _seed_changed_hash(monkeypatch)

        from backend import pr_handoff
        from backend.state import StateStore

        handoff_spy = MagicMock(wraps=pr_handoff.handle_pushed_branch)
        monkeypatch.setattr(pr_handoff, "handle_pushed_branch", handoff_spy)
        monkeypatch.setattr(
            pr_handoff,
            "build_pull_request_payload",
            MagicMock(
                return_value={
                    "repo": "TGS-Labs/Kiro-Config-Bundles",
                    "base": "main",
                    "head": "irrelevant",
                    "title": "chore: sync",
                    "body": "Automated config sync.",
                }
            ),
        )
        monkeypatch.setattr(pr_handoff, "notify_operator", MagicMock())

        result = push.run()

        handoff_spy.assert_called_once()
        _args, kwargs = handoff_spy.call_args
        called_result = handoff_spy.call_args.args[0]
        called_state = kwargs.get("state") or handoff_spy.call_args.args[1]
        assert called_result is result
        assert isinstance(called_state, StateStore), (
            "push.run() must hand off to pr_handoff with the REAL "
            "StateStore instance it loaded, not a fake or a re-derived one"
        )
        assert called_state is store
        assert called_state.last_pushed_hash != current_hash

    def test_record_branch_pushed_is_called_instead_of_record_push_success(
        self,
        isolated_roots: dict,
        change_path_collaborators: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The success path must call the NEW non-advancing

        `record_branch_pushed` rather than `record_push_success` directly
        — spying on both against the real `StateStore` class proves which
        one `run()` actually calls.
        """
        root_a = isolated_roots["root_a"]
        _write(root_a, "config.json", b'{"key": "value"}')

        store, current_hash, _redacted = _seed_changed_hash(monkeypatch)

        from backend import pr_handoff

        branch_pushed_spy = MagicMock(wraps=store.record_branch_pushed)
        push_success_spy = MagicMock(wraps=store.record_push_success)
        monkeypatch.setattr(store, "record_branch_pushed", branch_pushed_spy)
        monkeypatch.setattr(store, "record_push_success", push_success_spy)
        monkeypatch.setattr(
            pr_handoff,
            "build_pull_request_payload",
            MagicMock(
                return_value={
                    "repo": "TGS-Labs/Kiro-Config-Bundles",
                    "base": "main",
                    "head": "irrelevant",
                    "title": "chore: sync",
                    "body": "Automated config sync.",
                }
            ),
        )
        monkeypatch.setattr(pr_handoff, "notify_operator", MagicMock())

        push.run()

        branch_pushed_spy.assert_called_once()
        assert branch_pushed_spy.call_args.kwargs["tree_hash"] == current_hash
        push_success_spy.assert_not_called()


# ---------------------------------------------------------------------------
# Requirement 2.7 / review-fix H2 — a git subprocess or working-copy write
# failure on the change path must be recorded via `record_push_failure`
# with its cause BEFORE propagating, not silently lost.
# ---------------------------------------------------------------------------


class TestChangePathFailuresAreRecordedBeforeRaising:
    def test_unreadable_tracked_file_is_recorded_via_record_push_failure(
        self,
        isolated_roots: dict,
        change_path_collaborators: dict,
        no_git_or_network_calls: Callable[[], None],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Requirement 2.7: the failure cause must be RECORDED, not merely

        raised. Extends the existing unreadable-file test (which only
        asserted the bare raise) to also assert `record_push_failure` was
        called with the OSError's message before the exception propagates.
        """
        root_a = isolated_roots["root_a"]
        _write(root_a, "config.json", b'{"key": "value"}')
        _write(root_a, "steering/plan.md", b"# Plan")

        store, _current_hash, _redacted = _seed_changed_hash(monkeypatch)
        seeded_hash = store.last_pushed_hash

        failure_spy = MagicMock(wraps=store.record_push_failure)
        monkeypatch.setattr(store, "record_push_failure", failure_spy)

        unwritable_error = OSError("Permission denied: steering/plan.md")

        def _spy_write_bytes(self: Path, data: bytes) -> int:
            if self.name == "plan.md":
                raise unwritable_error
            return len(data)

        monkeypatch.setattr(Path, "write_bytes", _spy_write_bytes)

        with pytest.raises(OSError):
            push.run()

        no_git_or_network_calls()
        change_path_collaborators["subprocess_run"].assert_not_called()
        failure_spy.assert_called_once()
        assert "Permission denied" in failure_spy.call_args.kwargs["reason"]
        assert store.last_pushed_hash == seeded_hash

    def test_git_subprocess_failure_is_recorded_via_record_push_failure(
        self,
        isolated_roots: dict,
        change_path_collaborators: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A `CalledProcessError` from any git subprocess call (clone,

        fetch, checkout, add, commit, push) must be recorded with its
        cause via `record_push_failure` and then re-raised — never silently
        swallowed and never left unrecorded.
        """
        root_a = isolated_roots["root_a"]
        _write(root_a, "config.json", b'{"key": "value"}')

        store, _current_hash, _redacted = _seed_changed_hash(monkeypatch)
        seeded_hash = store.last_pushed_hash

        failure_spy = MagicMock(wraps=store.record_push_failure)
        monkeypatch.setattr(store, "record_push_failure", failure_spy)

        git_error = subprocess.CalledProcessError(
            returncode=128, cmd=["git", "push"], output=b"", stderr=b"remote rejected"
        )
        change_path_collaborators["subprocess_run"].side_effect = git_error

        with pytest.raises(subprocess.CalledProcessError):
            push.run()

        failure_spy.assert_called_once()
        assert store.last_pushed_hash == seeded_hash
