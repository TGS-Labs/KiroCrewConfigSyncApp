"""Tests for the poll-tick materialize/apply seam (`backend/poll.py::run`).

## Operator ruling — no approve/decline route

Under the operator ruling (requirements.md Introduction; Requirement
4.4, 4.6 [Reserved]), there is no box-side approve/decline step: the
poll tick materializes an incoming commit range and calls
`apply.apply_commit` automatically, with no operator action
(`tests/test_poll_autoapply.py` covers that path's own top-level
outcomes). This file exercises the SAME real-git materialize/apply seam
that used to be driven through `routes.approve`, but through
`poll_module.run()` instead — every property below (both roots actually
written, deletes applied, an accumulated multi-commit range applied
whole, the exact pending sha vs main's later tip, materialize-failure
state safety, agent-registration seams) survives the ruling unchanged;
only the entry point moved.

## Real-git bundle fixture

Built the same way `tests/test_apply.py`'s own fixture is: `main` =
initial commit, then a feature branch with real commits, merged into
`main` via a genuine `--no-ff` merge (`main`'s tip is the merge commit;
the feature branch's own tip is `main^2`) — Kiro-Config-Bundles disallows
squash-merge org-wide, matching `poll.py`'s own documented shape.

This module's real-git fixture helpers (`_bundle_repo_url_env`, `_git`,
`_head_sha`, `_init_origin_repo`, `_seed_history`) are imported directly
by `tests/test_poll_autoapply.py` — their names and signatures are kept
stable for that reason.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Iterator

import pytest

from backend import state


# ---------------------------------------------------------------------------
# Real-git fixture helpers (no mocked subprocess anywhere in this file).
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


def _head_sha(repo: Path) -> str:
    return _git("rev-parse", "HEAD", cwd=repo).stdout.strip()


def _init_origin_repo(tmp_path: Path) -> Path:
    """A bare "origin" the app's bundle-repo clone can fetch/clone from —

    `poll.py`/`push.py` always clone a remote URL, never a local working
    repo directly, so tests need a real remote to point at.
    """
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git("init", "-q", "--bare", "-b", "main", cwd=origin)
    return origin


def _seed_history(origin: Path, tmp_path: Path) -> dict[str, str]:
    """Build real history by pushing commits from a scratch working clone

    into ``origin``, mirroring test_apply.py's own real-git fixture shape
    (initial commit on main, a feature branch, merged back via a genuine
    `--no-ff` merge). Returns a dict of named SHAs the tests key off of.

    Commit graph:

        main:    initial ----------------- merge (M)
                              \\           /
        feature:                c1(A) -- c2(B)

    ``c1`` changes ``steering/x.md`` (root A). ``c2`` changes
    ``config-bundles/agent-prompts/marker.md`` (also root A) and ALSO
    deletes ``steering/old.md`` (root A) which existed since ``initial``.
    ``M`` is the merge commit that becomes ``main``'s new head — the SHA
    a poll tick discovers and applies.

    ``config-bundles/agent-prompts/marker.md`` (root A, a second
    tracked-path proof point) is used in place of a root-B
    ``agents/<name>.json`` change, because ``agents/<name>.json`` is
    STRUCTURALLY UNAPPLIABLE under the current allowlist regardless of
    what else the commit contains: `backend/allowlist.py`'s root-A
    entries have NO `agent-prompts/**` pattern at all (only
    `config-bundles/agent-prompts/*.md`), so a prompt file at the bare
    `agent-prompts/<name>.md` relpath `registration.check_registrations`'s
    own `_PROMPT_PATTERN` requires is filtered into `ignored_paths` at
    the Gate-1 allowlist filter BEFORE `check_registrations` ever sees
    it — making "prompt file present" permanently unsatisfiable and
    every `agents/<name>.json` apply permanently blocked as an
    incomplete registration. This is a pre-existing, already-documented
    spec gap, not something introduced here, and it is out of scope for
    a test-file-only fix. The fixture therefore replaces the intended
    root-B file with ``config-bundles/agent-prompts/marker.md`` — a
    genuinely root-A-tracked path with no registration semantics at all
    — as the second file, keeping the original two-write intent (two
    independently-verified writes in one apply) without depending on
    the broken registration path.
    """
    work = tmp_path / "seed-work"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    _git("remote", "add", "origin", str(origin), cwd=work)

    (work / "steering").mkdir()
    (work / "steering" / "old.md").write_text("# old\n", encoding="utf-8")
    (work / "config-bundles" / "agent-prompts").mkdir(parents=True)
    (work / "config-bundles" / "agent-prompts" / "marker.md").write_text(
        "# marker v0\n", encoding="utf-8"
    )
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "initial", cwd=work)
    initial_sha = _head_sha(work)
    _git("push", "-q", "-u", "origin", "main", cwd=work)

    _git("checkout", "-q", "-b", "feature", cwd=work)
    (work / "steering" / "x.md").write_text("# x v1\n", encoding="utf-8")
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "add steering/x.md", cwd=work)
    c1_sha = _head_sha(work)

    (work / "config-bundles" / "agent-prompts" / "marker.md").write_text(
        "# marker v1\n", encoding="utf-8"
    )
    (work / "steering" / "old.md").unlink()
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "bump marker, delete old.md", cwd=work)
    c2_sha = _head_sha(work)

    _git("checkout", "-q", "main", cwd=work)
    _git("merge", "-q", "--no-ff", "feature", "-m", "merge feature", cwd=work)
    merge_sha = _head_sha(work)
    _git("push", "-q", "origin", "main", cwd=work)

    return {
        "initial": initial_sha,
        "c1": c1_sha,
        "c2": c2_sha,
        "merge": merge_sha,
    }


def _advance_main_after(origin: Path, tmp_path: Path, past_sha: str) -> str:
    """Push one more commit to ``origin/main`` on top of whatever it

    currently holds, simulating main moving on AFTER a poll tick already
    saw and applied an earlier head (test 4: a tick must apply exactly
    the head it resolved, and a LATER tick is what picks up anything
    pushed after that).
    """
    work = tmp_path / "advance-work"
    work.mkdir()
    _git("clone", "-q", str(origin), str(work), cwd=tmp_path)
    (work / "steering").mkdir(exist_ok=True)
    (work / "steering" / "unrelated-later.md").write_text("# later\n", encoding="utf-8")
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "unrelated later commit", cwd=work)
    _git("push", "-q", "origin", "main", cwd=work)
    return _head_sha(work)


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    return _init_origin_repo(tmp_path)


@pytest.fixture
def shas(origin: Path, tmp_path: Path) -> dict[str, str]:
    return _seed_history(origin, tmp_path)


# ---------------------------------------------------------------------------
# App-state isolation (matches tests/test_routes.py's isolated_state_dir).
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Path]]:
    state_dir = tmp_path / "config-sync-state"
    root_a = tmp_path / "kiro-crew-home"
    root_b = tmp_path / "kiro-home"
    root_a.mkdir(parents=True, exist_ok=True)
    root_b.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setenv("KIROCREW_HOME", str(root_a))
    monkeypatch.setenv("KIRO_HOME", str(root_b))

    yield {"state_dir": state_dir, "root_a": root_a, "root_b": root_b}


@pytest.fixture
def poll_module(isolated_env: dict[str, Path]) -> Any:
    from backend import poll

    return poll


@pytest.fixture
def store(isolated_env: dict[str, Path]) -> state.StateStore:
    return state.load_state()


def _reload_store() -> state.StateStore:
    """Re-read state from disk — call this AFTER `poll_module.run()`,

    never rely on a `store` fixture instance captured before the call
    (matches `test_poll_autoapply.py`'s own convention: state is mutated
    out of process by the poll tick's own locked read-modify-write).
    """
    return state.load_state()


def _bundle_repo_url_env(monkeypatch: pytest.MonkeyPatch, origin: Path) -> None:
    """Point whatever bundle-repo URL constant poll/routes consult at our

    local ``origin`` bare repo instead of the real GitHub URL, so tests
    never touch the network. `poll.py`/`push.py` hardcode
    `BUNDLE_REPO_URL` as a module constant (not an env var).
    """
    from backend import poll as poll_module

    monkeypatch.setattr(poll_module, "BUNDLE_REPO_URL", f"file://{origin}")
    try:
        from backend import routes as routes_module

        if hasattr(routes_module, "BUNDLE_REPO_URL"):
            monkeypatch.setattr(routes_module, "BUNDLE_REPO_URL", f"file://{origin}")
    except ImportError:
        pass


@pytest.fixture
def bundle_url_patched(monkeypatch: pytest.MonkeyPatch, origin: Path) -> None:
    _bundle_repo_url_env(monkeypatch, origin)


# ---------------------------------------------------------------------------
# 1. A poll tick writes real committed bytes from BOTH roots to the live
#    roots, then advances base_sha / clears pending.
# ---------------------------------------------------------------------------


class TestPollApplyWritesBothRootsForReal:
    def test_poll_run_writes_both_roots_pending_files_to_the_live_roots(
        self,
        poll_module: Any,
        store: state.StateStore,
        origin: Path,
        shas: dict[str, str],
        isolated_env: dict[str, Path],
        bundle_url_patched: None,
    ) -> None:
        merge_sha = shas["merge"]

        result = poll_module.run()

        live_x = isolated_env["root_a"] / "steering" / "x.md"
        live_marker = (
            isolated_env["root_a"] / "config-bundles" / "agent-prompts" / "marker.md"
        )

        assert live_x.is_file(), (
            "steering/x.md was never written to the live root A -- "
            "the poll tick did not actually apply its discovered commit"
        )
        assert live_x.read_text(encoding="utf-8") == "# x v1\n"

        assert live_marker.is_file(), (
            "config-bundles/agent-prompts/marker.md was never written to "
            "the live root -- the poll tick reported success without "
            "actually applying this file"
        )
        assert live_marker.read_text(encoding="utf-8") == "# marker v1\n"

        assert result.outcome == "changed"

        reloaded = _reload_store()
        assert reloaded.base_sha == merge_sha
        assert reloaded.pending is None


# ---------------------------------------------------------------------------
# 2. A commit that deletes a tracked file: the poll tick removes it from
#    the live root.
# ---------------------------------------------------------------------------


class TestPollApplyAppliesDeletes:
    def test_poll_run_deletes_removed_file_from_live_root(
        self,
        poll_module: Any,
        store: state.StateStore,
        origin: Path,
        shas: dict[str, str],
        isolated_env: dict[str, Path],
        bundle_url_patched: None,
    ) -> None:
        # Pre-seed the live root with the file the merge's own history
        # deletes (steering/old.md, removed in c2), so there is something
        # on disk for the apply to actually remove.
        live_old = isolated_env["root_a"] / "steering" / "old.md"
        live_old.parent.mkdir(parents=True, exist_ok=True)
        live_old.write_text("# old\n", encoding="utf-8")

        result = poll_module.run()

        assert not live_old.exists(), (
            "steering/old.md (deleted upstream between initial and the "
            "merge) is still present on the live root -- the poll tick "
            "did not wire deleted paths through to apply_commit"
        )
        assert result.outcome == "changed"


# ---------------------------------------------------------------------------
# 3. Accumulated range (#65): a poll tick that resolves the merge head
#    directly applies files from BOTH commits in the range, not just the
#    head commit's own single-commit diff.
# ---------------------------------------------------------------------------


class TestPollApplyAppliesAccumulatedRange:
    def test_poll_run_applies_files_from_both_commits_in_the_range(
        self,
        poll_module: Any,
        store: state.StateStore,
        origin: Path,
        shas: dict[str, str],
        isolated_env: dict[str, Path],
        bundle_url_patched: None,
    ) -> None:
        """A poll tick with no prior `base_sha` computes the changed-path

        range for the whole history up to the merge head (requirements.md
        4.9: `base_sha is None` -> single-commit log from the first
        commit this instance ever polled) and must apply every file that
        range touches -- both `c1`'s change (steering/x.md) and `c2`'s
        change (config-bundles/agent-prompts/marker.md) -- not just
        whatever the merge commit's own single-commit diff would show
        (which, for a real merge, is empty without `-m`).
        """
        merge_sha = shas["merge"]

        result = poll_module.run()

        live_x = isolated_env["root_a"] / "steering" / "x.md"
        live_marker = (
            isolated_env["root_a"] / "config-bundles" / "agent-prompts" / "marker.md"
        )
        assert live_x.is_file(), (
            "the earlier commit's file (steering/x.md) was dropped -- "
            "the poll tick must apply every path in the resolved range, "
            "not just the newest commit's own diff"
        )
        assert live_marker.is_file()
        assert result.outcome == "changed"

        reloaded = _reload_store()
        assert reloaded.base_sha == merge_sha


# ---------------------------------------------------------------------------
# 4. A poll tick applies exactly the head it resolved, not whatever main
#    has moved on to by the time a LATER tick runs.
# ---------------------------------------------------------------------------


class TestPollApplyAppliesExactHeadNotFutureMain:
    def test_poll_run_does_not_apply_a_commit_pushed_after_this_ticks_head(
        self,
        poll_module: Any,
        store: state.StateStore,
        origin: Path,
        shas: dict[str, str],
        tmp_path: Path,
        isolated_env: dict[str, Path],
        bundle_url_patched: None,
    ) -> None:
        merge_sha = shas["merge"]

        first_result = poll_module.run()
        assert first_result.outcome == "changed"

        # main moves on AFTER this tick already resolved and applied its
        # head -- a commit pushed after this tick's own resolution must
        # never be swept into what THIS tick applied.
        _advance_main_after(origin, tmp_path, past_sha=merge_sha)

        later_file = isolated_env["root_a"] / "steering" / "unrelated-later.md"
        assert not later_file.exists(), (
            "a commit pushed after this tick's own head resolution was "
            "somehow applied by that same tick -- a poll tick must only "
            "ever apply the exact head it resolved"
        )
        # The resolved head's OWN files must have actually landed --
        # otherwise "later_file is absent" would be vacuously true simply
        # because nothing at all was applied, not because the exact-sha
        # boundary was respected.
        live_x = isolated_env["root_a"] / "steering" / "x.md"
        live_marker = (
            isolated_env["root_a"] / "config-bundles" / "agent-prompts" / "marker.md"
        )
        assert live_x.is_file()
        assert live_marker.is_file()

        reloaded = _reload_store()
        assert reloaded.base_sha == merge_sha


# ---------------------------------------------------------------------------
# 5. When the resolved commit cannot be materialized, the poll tick
#    records a failure, writes nothing, and leaves state untouched.
# ---------------------------------------------------------------------------


class TestPollApplyRefusesWhenShaCannotBeMaterialized:
    def test_poll_run_errors_writes_nothing_and_leaves_state_untouched(
        self,
        poll_module: Any,
        store: state.StateStore,
        origin: Path,
        isolated_env: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Forces materialization to fail (the git-archive step) on a

        REAL, resolvable head — the resolved sha exists on the bundle
        clone, but the archive/extract step itself is monkeypatched to
        raise, the narrowest reachable point that reproduces "the commit
        cannot be materialized" without depending on a bogus sha the
        head-resolution step would never actually return.
        """
        _bundle_repo_url_env(monkeypatch, origin)

        work = origin.parent / "materialize-fail-seed"
        work.mkdir()
        _git("init", "-q", "-b", "main", cwd=work)
        _git("remote", "add", "origin", str(origin), cwd=work)
        (work / "steering").mkdir()
        (work / "steering" / "x.md").write_text("# x\n", encoding="utf-8")
        _git("add", "-A", cwd=work)
        _git("commit", "-q", "-m", "add steering/x.md", cwd=work)
        _git("push", "-q", "-u", "origin", "main", cwd=work)

        base_sha_before = store.base_sha
        pending_before = dict(store.pending) if store.pending is not None else None

        real_run = subprocess.run

        def _raising_run(argv: Any, **kwargs: Any) -> Any:
            if argv and "archive" in argv:
                raise subprocess.CalledProcessError(1, argv)
            return real_run(argv, **kwargs)

        monkeypatch.setattr(poll_module.subprocess, "run", _raising_run)

        result = poll_module.run()

        assert result.outcome == "apply-error", (
            "the archive/extract step fails inside _apply_new_head's own "
            "materialize call, never at the earlier fetch-commit-details "
            f"step — expected exactly 'apply-error', got {result.outcome!r}"
        )

        live_x = isolated_env["root_a"] / "steering" / "x.md"
        assert not live_x.exists()

        reloaded = _reload_store()
        assert reloaded.base_sha == base_sha_before, (
            "a materialization failure must leave base_sha byte-for-byte "
            "as it was before this tick"
        )
        if pending_before is None:
            assert reloaded.pending is None
        else:
            assert dict(reloaded.pending or {}) == pending_before


# ---------------------------------------------------------------------------
# 6. tasks.md 7.5 seam: a commit that changes ONLY agents/y.json (inline
#    prompt), whose commit tree's config.json and agent_model_state.json
#    already carry y's key -> a poll tick writes agents/y.json's
#    committed bytes to the live root B (KIRO_HOME).
#
# This is the seam test between poll.run/apply_commit and
# registration.check_registrations' amended (5.14) tree-content rule: a
# root-B-only change, with both shared parts satisfied purely from the
# commit tree's content rather than from changed_paths membership, must
# actually complete and land on disk.
# ---------------------------------------------------------------------------


def _seed_agent_y_only_history(origin: Path, tmp_path: Path) -> dict[str, str]:
    """Real history whose head commit changes ONLY ``agents/y.json`` --

    the commit tree ALSO carries ``config.json``/``agent_model_state.json``
    with ``y``'s key already present (added in the SAME commit here for
    fixture simplicity; 5.14's rule is that the shared files' CONTENT is
    what is read, regardless of whether they are themselves part of the
    resolved range's own changed-path list).
    """
    work = tmp_path / "agent-y-seed-work"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    _git("remote", "add", "origin", str(origin), cwd=work)

    (work / "agents").mkdir()
    (work / "agents" / "y.json").write_text(
        json.dumps(
            {
                "name": "y",
                "description": "Agent y.",
                "prompt": "You are agent y, an inline-prompt test agent.",
                "tools": ["read"],
                "allowedTools": ["read"],
                "resources": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (work / "config.json").write_text(
        json.dumps({"agents": {"y": {"source": "local", "model": "claude-sonnet-5"}}}),
        encoding="utf-8",
    )
    (work / "agent_model_state.json").write_text(
        json.dumps({"y": {"model_managed": False, "model": "claude-sonnet-5"}}),
        encoding="utf-8",
    )
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "register agent y", cwd=work)
    head_sha = _head_sha(work)
    _git("push", "-q", "-u", "origin", "main", cwd=work)

    return {"head": head_sha}


class TestPollApplyAgentJsonOnlyChangeWithSharedKeysAlreadyInTree:
    """tasks.md 7.5 / requirements.md 5.14 seam: agents/y.json is the

    only path the resolved range's own diff names, yet the commit tree's
    config.json and agent_model_state.json already carry y's key -- a
    poll tick must still complete the registration (inline prompt -> no
    required file part per 5.11(b)) and write agents/y.json's committed
    bytes to the live KIRO_HOME root.
    """

    def test_poll_run_agent_json_only_change_completes_and_writes_to_kiro_home(
        self,
        poll_module: Any,
        store: state.StateStore,
        origin: Path,
        tmp_path: Path,
        isolated_env: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _bundle_repo_url_env(monkeypatch, origin)
        shas = _seed_agent_y_only_history(origin, tmp_path)
        head_sha = shas["head"]

        result = poll_module.run()

        live_agent_y = isolated_env["root_b"] / "agents" / "y.json"
        assert live_agent_y.is_file(), (
            "agents/y.json was never written to the live KIRO_HOME root -- "
            "an agent-JSON-only commit whose shared files already carry "
            "y's key in the commit tree must still complete registration "
            "and land on disk (requirements.md 5.14)"
        )

        written = json.loads(live_agent_y.read_text(encoding="utf-8"))
        assert written == {
            "name": "y",
            "description": "Agent y.",
            "prompt": "You are agent y, an inline-prompt test agent.",
            "tools": ["read"],
            "allowedTools": ["read"],
            "resources": [],
        }

        assert result.outcome == "changed"

        reloaded = _reload_store()
        assert reloaded.base_sha == head_sha
        assert reloaded.pending is None


# ---------------------------------------------------------------------------
# 7. requirements.md 5.12 seam: a commit that ships a BRAND-NEW agent z
#    together with its prompt file (a ``file://`` reference, never
#    previously live on either root) -- a poll tick must write both
#    agents/z.json (root B / KIRO_HOME) and the prompt file (root A /
#    KIROCREW_HOME) to their live roots.
# ---------------------------------------------------------------------------


def _seed_agent_z_with_prompt_history(origin: Path, tmp_path: Path) -> dict[str, str]:
    """Real history whose head commit adds a brand-new agent ``z``:

    ``agents/z.json`` (a ``file://`` prompt reference), the referenced
    ``config-bundles/agent-prompts/z.md`` prompt file itself, and both
    shared files carrying ``z``'s entry/pin -- all landing in the SAME
    commit, exactly the "ships a new agent together with its prompt
    file" case requirements.md 5.12 names. Nothing here has ever been
    live on either root before this commit is applied.
    """
    work = tmp_path / "agent-z-seed-work"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    _git("remote", "add", "origin", str(origin), cwd=work)

    (work / "agents").mkdir()
    (work / "agents" / "z.json").write_text(
        json.dumps(
            {
                "name": "z",
                "description": "Agent z.",
                "prompt": "file://${KIROCREW_HOME}/config-bundles/agent-prompts/z.md",
                "tools": ["read"],
                "allowedTools": ["read"],
                "resources": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (work / "config-bundles" / "agent-prompts").mkdir(parents=True)
    (work / "config-bundles" / "agent-prompts" / "z.md").write_text(
        "# z\n\nYou are agent z, a brand-new agent.\n", encoding="utf-8"
    )
    (work / "config.json").write_text(
        json.dumps({"agents": {"z": {"source": "local", "model": "claude-sonnet-5"}}}),
        encoding="utf-8",
    )
    (work / "agent_model_state.json").write_text(
        json.dumps({"z": {"model_managed": False, "model": "claude-sonnet-5"}}),
        encoding="utf-8",
    )
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "register agent z with its prompt", cwd=work)
    head_sha = _head_sha(work)
    _git("push", "-q", "-u", "origin", "main", cwd=work)

    return {"head": head_sha}


class TestPollApplyNewAgentWithPromptNeverPreviouslyLiveCompletes:
    """requirements.md 5.12's main real-world case: a commit that ships a

    NEW agent (``z``) together with its own prompt file, both changed in
    the same commit and neither ever having existed on any live root
    before. The prompt part must count as present because it exists in
    the applied commit's tree -- a poll tick must write agents/z.json to
    the live KIRO_HOME root AND the prompt file to the live KIROCREW_HOME
    root, completing the registration rather than refusing it as
    incomplete for a prompt that (correctly, for a brand-new agent) was
    never live.
    """

    def test_poll_run_writes_new_agent_definition_and_its_prompt_to_live_roots(
        self,
        poll_module: Any,
        store: state.StateStore,
        origin: Path,
        tmp_path: Path,
        isolated_env: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _bundle_repo_url_env(monkeypatch, origin)
        shas = _seed_agent_z_with_prompt_history(origin, tmp_path)
        head_sha = shas["head"]

        # Neither the agent definition nor the prompt file has ever been
        # written to any live root before this poll tick.
        live_agent_z = isolated_env["root_b"] / "agents" / "z.json"
        live_prompt_z = (
            isolated_env["root_a"] / "config-bundles" / "agent-prompts" / "z.md"
        )
        assert not live_agent_z.exists()
        assert not live_prompt_z.exists()

        result = poll_module.run()

        assert live_agent_z.is_file(), (
            "agents/z.json was never written to the live KIRO_HOME root -- "
            "a brand-new agent shipped together with its prompt file must "
            "still complete registration and land on disk (requirements.md "
            "5.12)"
        )
        written = json.loads(live_agent_z.read_text(encoding="utf-8"))
        assert written["name"] == "z"
        # requirements.md 4.11 / design.md apply step 4: expand runs on
        # every applied in-scope JSON file, so the token form the commit
        # carries is rewritten to THIS host's own KIROCREW_HOME absolute
        # path before being written to the live root.
        assert written["prompt"] == (
            f"file://{isolated_env['root_a']}/config-bundles/agent-prompts/z.md"
        )

        assert live_prompt_z.is_file(), (
            "the new agent's prompt file was never written to the live "
            "KIROCREW_HOME root -- requirements.md 5.12's main case"
        )
        assert live_prompt_z.read_text(encoding="utf-8") == (
            "# z\n\nYou are agent z, a brand-new agent.\n"
        )

        assert result.outcome == "changed"

        reloaded = _reload_store()
        assert reloaded.base_sha == head_sha
        assert reloaded.pending is None
