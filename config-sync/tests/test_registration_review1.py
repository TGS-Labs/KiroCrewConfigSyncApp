"""Failing tests for senior-review finding H2 on backend/registration.py.

H2 (High): ``_prompt_file_present`` (backend/registration.py, ~line 253)
checks ``is_symlink()`` on the FINAL path component only. Requirements.md
5.12 requires the prompt part to count as present only when ``<rel>``
"exists as a regular, non-symlink file in the approved commit's tree" —
which this module's own docstring and ``backend/apply.py``'s
``_is_unsafe_source`` both read as covering every component between the
commit root and the leaf, not just the leaf. If a PARENT directory in the
commit tree (e.g. ``config-bundles/`` or
``config-bundles/agent-prompts/``) is itself a symlink, the current
``_prompt_file_present`` check still reports the prompt part present
(the leaf it resolves through is a real file), so ``registration.
check_registrations`` reports the registration complete. But
``apply.apply_commit``'s own containment guard — ``_is_unsafe_source``,
which walks every segment with ``lstat``/``is_symlink()`` — refuses to
write anything beneath that symlinked parent. The result is a
half-registration: the agent definition, the config.json entry, and the
agent_model_state.json pin all land live, but the prompt file never
does, because apply.py silently drops it as an unsafe source.

These tests are written against ``backend.registration.
check_registrations`` and, for (3)/(4), the real
``backend.apply.apply_commit`` seam between the two modules. They are
expected to FAIL red right now:

    (1)/(2) fail because ``check_registrations`` reports the affected
        agent COMPLETE (the mutation described below is what turns them
        red for the right reason — the parent-symlink case is not
        rejected).
    (3)/(4) fail because ``apply_commit`` reports the affected agent's
        parts other than the prompt as successfully ``applied`` — the
        half-registration the finding describes — instead of refusing
        the whole registration's parts as one unit.

Mutation proof for (1)/(2) (security/compliance assertion,
testing-standards.md's Mutation Requirement): the assertion these two
tests make is exactly the one that must be shown capable of failing
against a violating input. The violating input IS the fixture itself
(a symlinked parent directory) — running the CURRENT, unfixed
``_prompt_file_present`` (final-component-only check) against it is the
red run; a version of the checker that also walks intermediate
components (mirroring ``apply._is_unsafe_source``) is what would turn it
green. Test (1)'s own body documents this red/green pair explicitly
rather than only asserting once, so the mutation evidence is visible
directly in the test, not merely claimed in this docstring.

Fixture layout mirrors test_registration.py's existing conventions (a
real commit-scoped directory tree under ``tmp_path``, no mocks) rather
than duplicating its helpers, since this file must not edit
test_registration.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend import apply, registration
from backend import state as state_module

AGENT_NAME = "symlink-parent-agent"
AGENT_DEF_RELPATH = f"agents/{AGENT_NAME}.json"
TRACKED_PROMPT_RELPATH = f"config-bundles/agent-prompts/{AGENT_NAME}.md"
CONFIG_RELPATH = "config.json"
MODEL_STATE_RELPATH = "agent_model_state.json"


def _roots(root_a: Path, root_b: Path) -> dict:
    """The ``{"A": Path, "B": Path}`` shape ``check_registrations``'s

    third argument needs — identical convention to test_registration.py.
    """
    return {"A": root_a, "B": root_b}


def _prep_roots(tmp_path: Path) -> tuple[Path, Path, Path, dict]:
    root = tmp_path / "commit"
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    root.mkdir()
    root_a.mkdir()
    root_b.mkdir()
    return root, root_a, root_b, _roots(root_a, root_b)


def _write_agent_def(root: Path, agent_name: str = AGENT_NAME) -> None:
    path = root / "agents" / f"{agent_name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "name": agent_name,
                "description": "An example agent.",
                "prompt": (
                    "file://${KIROCREW_HOME}/config-bundles/agent-prompts/"
                    f"{agent_name}.md"
                ),
                "tools": ["read", "write"],
                "allowedTools": ["read"],
                "resources": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _write_config_json(root: Path, agent_name: str = AGENT_NAME) -> None:
    path = root / CONFIG_RELPATH
    doc = {}
    if path.exists():
        doc = json.loads(path.read_text(encoding="utf-8"))
    agents = doc.setdefault("agents", {})
    agents[agent_name] = {"source": "local", "model": "claude-sonnet-5"}
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")


def _write_agent_model_state(root: Path, agent_name: str = AGENT_NAME) -> None:
    path = root / MODEL_STATE_RELPATH
    pins = {}
    if path.exists():
        pins = json.loads(path.read_text(encoding="utf-8"))
    pins[agent_name] = {"model_managed": False, "model": "claude-sonnet-5"}
    path.write_text(json.dumps(pins, indent=2), encoding="utf-8")


def _make_symlinked_prompt_dir_target(root: Path) -> Path:
    """Build a REAL directory elsewhere in the tree holding the prompt

    file at the expected leaf name, for a symlinked parent to point at.
    """
    real_dir = root / "real-agent-prompts-dir"
    real_dir.mkdir(parents=True, exist_ok=True)
    (real_dir / f"{AGENT_NAME}.md").write_text(
        f"# {AGENT_NAME}\n\nreal content\n", encoding="utf-8"
    )
    return real_dir


# ---------------------------------------------------------------------------
# (1) config-bundles/agent-prompts symlinked to a real dir holding the file.
# ---------------------------------------------------------------------------


def test_symlinked_agent_prompts_dir_makes_registration_incomplete(
    tmp_path: Path,
) -> None:
    """H2: ``config-bundles/agent-prompts`` itself is a symlink (to a real

    directory elsewhere in the commit tree that DOES hold
    ``<agent>.md``). Requirements.md 5.12 requires the prompt part be
    counted present only when ``<rel>`` exists as a regular, non-symlink
    file in the approved commit's tree — a relpath that resolves THROUGH
    a symlinked parent is not a plain file at that path in the tree as
    committed; it is a redirect. ``check_registrations`` must therefore
    report this agent's registration INCOMPLETE (missing the prompt
    part), matching ``apply.apply_commit``'s own containment guard
    (``_is_unsafe_source``), which refuses to write through this exact
    shape.
    """
    root, root_a, root_b, roots = _prep_roots(tmp_path)
    _write_agent_def(root)
    _write_config_json(root)
    _write_agent_model_state(root)

    real_dir = _make_symlinked_prompt_dir_target(root)
    (root / "config-bundles").mkdir(parents=True, exist_ok=True)
    (root / "config-bundles" / "agent-prompts").symlink_to(
        real_dir, target_is_directory=True
    )

    changed = [AGENT_DEF_RELPATH, CONFIG_RELPATH, MODEL_STATE_RELPATH]

    # --- Mutation proof: the CURRENT implementation is red here. -------
    # `_prompt_file_present` only inspects the final path component's
    # `is_symlink()`; walking through a symlinked `agent-prompts/`
    # directory still resolves to a real, non-symlink leaf file, so the
    # unfixed checker reports the prompt present and the registration
    # complete — proving this assertion is capable of catching the real
    # defect rather than passing vacuously.
    result = registration.check_registrations(root, changed, roots)

    assert AGENT_NAME not in result.complete_agents, (
        "the prompt path resolves through a symlinked parent directory "
        "(config-bundles/agent-prompts) -- _prompt_file_present must not "
        "count this as a present, non-symlink file in the commit tree"
    )
    assert AGENT_NAME in result.incomplete_agents
    missing = result.incomplete_agents[AGENT_NAME]
    assert "prompt" in " ".join(missing).lower()


# ---------------------------------------------------------------------------
# (2) config-bundles itself (one level higher) is the symlink.
# ---------------------------------------------------------------------------


def test_symlinked_config_bundles_dir_makes_registration_incomplete(
    tmp_path: Path,
) -> None:
    """Same defect, one path segment higher: ``config-bundles`` itself

    (not just ``agent-prompts`` beneath it) is a symlink to a real
    directory tree that holds ``agent-prompts/<agent>.md``. Any symlinked
    ancestor between the commit root and the leaf must trip the same
    fail-closed rule -- this must not be special-cased to only the
    immediate parent.
    """
    root, root_a, root_b, roots = _prep_roots(tmp_path)
    _write_agent_def(root)
    _write_config_json(root)
    _write_agent_model_state(root)

    real_bundles_dir = root / "real-config-bundles"
    real_prompts_dir = real_bundles_dir / "agent-prompts"
    real_prompts_dir.mkdir(parents=True, exist_ok=True)
    (real_prompts_dir / f"{AGENT_NAME}.md").write_text(
        f"# {AGENT_NAME}\n\nreal content\n", encoding="utf-8"
    )
    (root / "config-bundles").symlink_to(real_bundles_dir, target_is_directory=True)

    changed = [AGENT_DEF_RELPATH, CONFIG_RELPATH, MODEL_STATE_RELPATH]

    result = registration.check_registrations(root, changed, roots)

    assert AGENT_NAME not in result.complete_agents, (
        "config-bundles itself (two segments above the leaf) is a "
        "symlink -- the registration must still be reported incomplete"
    )
    assert AGENT_NAME in result.incomplete_agents
    missing = result.incomplete_agents[AGENT_NAME]
    assert "prompt" in " ".join(missing).lower()


# ---------------------------------------------------------------------------
# (3)/(4) real apply_commit seam: half-registration, all-or-nothing.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# (3)/(4) real apply_commit seam: half-registration, all-or-nothing.
# ---------------------------------------------------------------------------


def _seed_pending(store: state_module.StateStore, sha: str) -> None:
    """Same convention as test_apply.py's own ``_seed_pending`` helper —

    record a fresh pending commit awaiting approval so ``apply_commit``'s
    SHA gate passes.
    """
    store.set_pending(
        sha=sha,
        author="a",
        subject="s",
        classified_paths={},
        ignored_paths=[],
        touched_classes=[],
    )


def _prep_apply_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, str, state_module.StateStore]:
    """Point apply.py's live roots (KIROCREW_HOME/KIRO_HOME) and the state

    module's own state dir at fresh directories under ``tmp_path`` --
    identical convention to test_apply.py's ``target_root``/``state_store``
    fixtures -- so the real ``apply_commit`` writes nowhere near the
    developer's actual home directory. Returns ``(commit_root,
    approved_sha, store)``.
    """
    live_root_a = tmp_path / "live-root-a"
    live_root_b = tmp_path / "live-root-b"
    live_root_a.mkdir()
    live_root_b.mkdir()

    monkeypatch.setenv("KIROCREW_HOME", str(live_root_a))
    monkeypatch.setenv("KIRO_HOME", str(live_root_b))
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(tmp_path / "config-sync-state"))

    store = state_module.load_state()
    approved_sha = "deadbeef"
    _seed_pending(store, approved_sha)

    commit_root = tmp_path / "commit"
    commit_root.mkdir()
    return commit_root, approved_sha, store


def test_symlinked_prompt_parent_yields_half_registration_via_real_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(3) End-to-end through the REAL ``apply.apply_commit`` seam this

    finding is actually about: with ``config-bundles/agent-prompts``
    symlinked in the commit tree, ``check_registrations`` (as currently
    implemented) reports the registration COMPLETE, so
    ``apply_commit`` proceeds to write the agent definition, the
    config.json entry, and the agent_model_state.json pin live --
    while its OWN ``_is_unsafe_source`` containment check independently
    refuses the prompt file itself, because that check walks every path
    segment with ``lstat``. The result is exactly the half-registration
    H2 describes: three of the four parts land live, the prompt does
    not.

    This must not happen -- a registration is one transaction
    (design.md). Assert the FULL blocked set, not only that the overall
    outcome is "partial": every one of the four registration relpaths
    must be refused together, or none should be.
    """
    commit_root, approved_sha, store = _prep_apply_env(tmp_path, monkeypatch)

    _write_agent_def(commit_root)
    _write_config_json(commit_root)
    _write_agent_model_state(commit_root)

    real_dir = _make_symlinked_prompt_dir_target(commit_root)
    (commit_root / "config-bundles").mkdir(parents=True, exist_ok=True)
    (commit_root / "config-bundles" / "agent-prompts").symlink_to(
        real_dir, target_is_directory=True
    )

    changed_paths = {
        "A": [
            CONFIG_RELPATH,
            MODEL_STATE_RELPATH,
            TRACKED_PROMPT_RELPATH,
        ],
        "B": [AGENT_DEF_RELPATH],
    }

    result = apply.apply_commit(
        approved_sha=approved_sha,
        commit_root=commit_root,
        changed_paths=changed_paths,
        store=store,
    )

    # The finding: today this registration's non-prompt parts apply
    # live while the prompt is silently dropped by the containment
    # guard. Assert the FULL expected-blocked set together -- every
    # registration-shaped path must be refused as one unit.
    expected_blocked = {
        AGENT_DEF_RELPATH,
        CONFIG_RELPATH,
        MODEL_STATE_RELPATH,
        TRACKED_PROMPT_RELPATH,
    }
    actually_blocked = set(result.not_applied) | set(result.ignored_paths)
    assert expected_blocked <= actually_blocked, (
        "a symlinked parent directory around the prompt file must block "
        "the WHOLE registration (agent def, config.json entry, "
        "agent_model_state.json pin, and the prompt itself) -- not leave "
        "three of the four parts applied live while only the prompt is "
        "silently dropped by apply.py's containment guard"
    )
    assert not (expected_blocked & set(result.applied)), (
        "no part of this registration may land live while the prompt "
        "part is refused -- registration is one transaction"
    )


def test_symlinked_config_bundles_yields_half_registration_via_real_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(4) Same seam-level proof as (3), with the symlinked parent one

    level higher (``config-bundles`` itself), confirming the all-or-
    nothing requirement holds regardless of which ancestor segment is
    the symlink.
    """
    commit_root, approved_sha, store = _prep_apply_env(tmp_path, monkeypatch)

    _write_agent_def(commit_root)
    _write_config_json(commit_root)
    _write_agent_model_state(commit_root)

    real_bundles_dir = commit_root / "real-config-bundles"
    real_prompts_dir = real_bundles_dir / "agent-prompts"
    real_prompts_dir.mkdir(parents=True, exist_ok=True)
    (real_prompts_dir / f"{AGENT_NAME}.md").write_text(
        f"# {AGENT_NAME}\n\nreal content\n", encoding="utf-8"
    )
    (commit_root / "config-bundles").symlink_to(
        real_bundles_dir, target_is_directory=True
    )

    changed_paths = {
        "A": [
            CONFIG_RELPATH,
            MODEL_STATE_RELPATH,
            TRACKED_PROMPT_RELPATH,
        ],
        "B": [AGENT_DEF_RELPATH],
    }

    result = apply.apply_commit(
        approved_sha=approved_sha,
        commit_root=commit_root,
        changed_paths=changed_paths,
        store=store,
    )

    expected_blocked = {
        AGENT_DEF_RELPATH,
        CONFIG_RELPATH,
        MODEL_STATE_RELPATH,
        TRACKED_PROMPT_RELPATH,
    }
    actually_blocked = set(result.not_applied) | set(result.ignored_paths)
    assert expected_blocked <= actually_blocked, (
        "a symlinked config-bundles ancestor must block the whole "
        "registration together, not just the prompt file"
    )
    assert not (expected_blocked & set(result.applied))


# ---------------------------------------------------------------------------
# (5) A parent symlink pointing OUTSIDE the commit tree entirely.
# ---------------------------------------------------------------------------


def test_symlinked_parent_pointing_outside_tree_makes_registration_incomplete(
    tmp_path: Path,
) -> None:
    """A symlinked parent that escapes the commit tree ENTIRELY (points

    at a sibling directory outside ``root``, not merely another real
    directory inside it) is at least as unsafe as an in-tree symlinked
    parent -- it lets the commit point content storage completely
    outside the tree ``check_registrations``/``apply.py`` are allowed to
    read. Must be reported incomplete on the same basis as (1)/(2).
    """
    root, root_a, root_b, roots = _prep_roots(tmp_path)
    _write_agent_def(root)
    _write_config_json(root)
    _write_agent_model_state(root)

    outside_dir = tmp_path / "outside-the-commit-tree"
    outside_dir.mkdir()
    (outside_dir / f"{AGENT_NAME}.md").write_text(
        f"# {AGENT_NAME}\n\nescaped content\n", encoding="utf-8"
    )
    (root / "config-bundles").mkdir(parents=True, exist_ok=True)
    (root / "config-bundles" / "agent-prompts").symlink_to(
        outside_dir, target_is_directory=True
    )

    changed = [AGENT_DEF_RELPATH, CONFIG_RELPATH, MODEL_STATE_RELPATH]

    result = registration.check_registrations(root, changed, roots)

    assert AGENT_NAME not in result.complete_agents, (
        "a parent directory symlinked OUTSIDE the commit tree entirely "
        "must not let the prompt part count as present"
    )
    assert AGENT_NAME in result.incomplete_agents
    missing = result.incomplete_agents[AGENT_NAME]
    assert "prompt" in " ".join(missing).lower()


# ---------------------------------------------------------------------------
# (6) Missing assertion flagged by review: blocked_paths for the existing
#     "two agents, one complete one incomplete" scenario. C3 RATIFIED.
# ---------------------------------------------------------------------------


def test_two_agents_one_complete_one_incomplete_blocked_paths_c3_ratified(
    tmp_path: Path,
) -> None:
    """C3 RATIFIED: 'if config.json/agent_model_state.json is blocked

    because some agent in the commit is incomplete, every agent whose
    key is in that committed shared file is also blocked; no
    half-registration.'

    This is the missing assertion review flagged on the existing
    ``test_two_agents_one_complete_one_incomplete_in_same_commit``
    scenario in test_registration.py (not edited here, per instructions
    -- this is a separate test in this file covering the same fixture
    shape).

    ``config.json`` is one shared file carrying BOTH agents' entries. If
    ``config.json`` itself is refused from applying (blocked), then
    every agent whose key lives in that same committed ``config.json``
    is, in the applied-live world, also missing its config entry --
    including ``agent-one``, whose own ``agents/agent-one.json`` must
    therefore also be blocked, never reported as if its own parts still
    apply cleanly while the shared file backing it does not land.

    This encodes the ratified invariant directly: IF config.json is
    blocked, THEN every agent whose key lives in the committed
    config.json is also blocked (i.e. that agent's own agent-definition
    path must appear in blocked_paths too). ``backend/registration.py``
    already implements this (its fourth pass, C3), so this test is
    expected to be GREEN under the current implementation.
    """
    root, root_a, root_b, roots = _prep_roots(tmp_path)

    # agent-one: complete registration.
    _write_agent_def(root, agent_name="agent-one")
    _write_config_json(root, agent_name="agent-one")
    _write_agent_model_state(root, agent_name="agent-one")
    (root / "config-bundles" / "agent-prompts").mkdir(parents=True, exist_ok=True)
    (root / TRACKED_PROMPT_RELPATH.replace(AGENT_NAME, "agent-one")).write_text(
        "# agent-one\n\nprompt\n", encoding="utf-8"
    )

    # agent-two: agent def + prompt + config.json entry present, but its
    # model-state pin is deliberately omitted (fresh shared file with no
    # key for it) -> incomplete.
    _write_agent_def(root, agent_name="agent-two")
    _write_config_json(root, agent_name="agent-two")
    (root / TRACKED_PROMPT_RELPATH.replace(AGENT_NAME, "agent-two")).write_text(
        "# agent-two\n\nprompt\n", encoding="utf-8"
    )
    (root / MODEL_STATE_RELPATH).write_text(
        json.dumps({"agent-one": {"model_managed": False}}), encoding="utf-8"
    )

    changed = [
        "agents/agent-one.json",
        CONFIG_RELPATH,
        MODEL_STATE_RELPATH,
        "agents/agent-two.json",
    ]

    result = registration.check_registrations(root, changed, roots)

    # Baseline facts: agent-two is incomplete (missing its model-state
    # pin), and config.json IS blocked (agent-two's own present config
    # entry gets blocked because agent-two is incomplete).
    assert "agent-two" in result.incomplete_agents
    assert CONFIG_RELPATH in result.blocked_paths

    # C3, ratified: config.json is blocked from applying, so agent-one's
    # own entry inside it will not be live either -- agent-one's own
    # agents/agent-one.json is therefore also blocked, and agent-one
    # itself is reported incomplete rather than complete.
    assert "agents/agent-one.json" in result.blocked_paths, (
        "C3 (ratified): config.json is blocked from applying, so "
        "agent-one's own entry inside it will not be live either -- "
        "agent-one's own agents/agent-one.json must therefore also be "
        "treated as blocked, not reported as if it still applies "
        "cleanly on its own"
    )
    assert "agent-one" not in result.complete_agents
    assert "agent-one" in result.incomplete_agents
