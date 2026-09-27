"""FAILING tests for the restore route (tasks.md 6.2; requirements.md 4.7,

4.8; design.md's routes table, `POST /api/restore/{id}` —
"Restore a backup from a previous apply").

## Interface pinned for software-engineer

`backend/routes.py` does not export a `restore` function yet. This file
pins:

    restore(store: StateStore, apply_id: str) -> dict[str, Any]

Returns a plain JSON-serializable dict carrying at least a ``"status"``
key (``"ok"`` | ``"error"``), matching every other route in this module
(`status`, `drift`, `push_now`, `approve`, `decline`). On success it also
carries ``"restored"``: ``{"A": [...], "B": [...]}`` — the relpaths
restored per root, mirroring `apply.ApplyResult`'s own per-root-free but
root-aware shape used elsewhere in this module (`_apply_result_to_dict`,
`_split_paths_by_root`).

## What "restore" means here (scoped by what `apply.py` actually records)

`apply_commit` backs up, BEFORE any overwrite/delete, the current live
bytes of every file it is about to touch, into
``restore_dir/<root>/<relpath>`` (`apply.py::_backup_file`), and records
the mapping ``apply_id -> restore_dir`` via
``state.StateStore.record_restore_dir``.

UPDATE (this task): requirements.md 4.7 says restore returns the
instance to the EXACT pre-apply state — a file the apply CREATED (no
prior live bytes, so `_backup_file`'s no-op-on-creation branch never
backed it up) must be REMOVED by restore, not left behind, or the
post-restore state is not actually the pre-apply state. The mechanism
pinned for this (see `test_apply.py`'s
``test_apply_records_a_created_file_manifest_per_root_restore_dir``) is
a per-root manifest file at ``restore_dir/<root>/.created-manifest.json``
— a JSON list of relpaths this apply created — that `routes.restore`
reads to know which relpaths to remove versus put back from real backed-
up bytes. `routes.restore`'s dict return therefore also carries a
``"removed"`` key (``{"A": [...], "B": [...]}``), separate from
``"restored"``, naming exactly the relpaths removed this way. This
supersedes the earlier "state the gap, do not invent behaviour" framing
below — the gap is exactly what this task closes, and
`test_restore_does_not_invent_deletion_of_apply_created_files` is kept
below with its assertions updated to the new contract rather than
deleted, so the history of why the gap existed stays visible.

## No network/git call

Restore reads only the local ``restore_dir`` tree the apply already
wrote to disk (design.md: "using only the local restore directory with
no second network round trip") — `subprocess.run` is monkeypatched to
raise in every test in this file, proving restore never shells out.

## Real apply_commit, real git

`apply_id`/`restore_dir` layout must be the REAL one `apply_commit`
produces, not a hand-rolled fixture — every test drives a real
`apply.apply_commit` call against a real git-archived commit tree
(reusing `tests/test_apply.py`'s bundle-repo / real-git fixture shape)
so the backup layout under test is exactly what `apply.py` actually
writes, including the ``<root>/<relpath>`` namespacing
(`apply.py::_backup_file`'s own docstring: "Namespaced by `root`
("A"/"B") because root A and root B can each carry a file at the
identical `relpath`").
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Iterator

import pytest

from backend import apply, state

# ---------------------------------------------------------------------------
# Real-git fixture helpers (no mocked subprocess for git — matches
# test_apply.py's own convention). subprocess.run itself is monkeypatched
# to raise ONLY inside the tests that must prove no-network-call; the
# fixtures below run with the real subprocess before that patch is armed.
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
        timeout=120,
        stdin=subprocess.DEVNULL,
    )


def _init_bundle_repo(tmp_path: Path) -> Path:
    """A real bundle-repo-shaped commit history: an initial commit, then

    a second commit that modifies a root-A file and adds a root-B-shaped
    file — matching `test_apply.py`'s real-git fixture convention.
    """
    repo = tmp_path / "bundle-repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)

    (repo / "steering").mkdir()
    (repo / "steering" / "a.md").write_text("# A v0\n", encoding="utf-8")
    (repo / "config.json").write_text(
        json.dumps({"agents": {}}, indent=2), encoding="utf-8"
    )
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "initial", cwd=repo)

    (repo / "steering" / "a.md").write_text("# A v1\n", encoding="utf-8")
    (repo / "config.json").write_text(
        json.dumps({"agents": {"x": {}}}, indent=2), encoding="utf-8"
    )
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "modify a and config", cwd=repo)

    return repo


def _checkout_head_tree(repo: Path, dest: Path) -> None:
    """Materialize the repo's current HEAD tree into ``dest`` via a real

    ``git archive`` — the same commit-materialization mechanism
    `test_apply.py` and `routes._materialize_pending_commit` both use.
    """
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(
        ["git", "archive", "HEAD"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        env=_GIT_ENV,
        timeout=120,
        stdin=subprocess.DEVNULL,
    )
    tar_path = dest.parent / f"{dest.name}.tar"
    tar_path.write_bytes(archive.stdout)
    subprocess.run(
        ["tar", "-xf", str(tar_path), "-C", str(dest)],
        check=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
    )
    tar_path.unlink()


def _head_sha(repo: Path) -> str:
    return _git("rev-parse", "HEAD", cwd=repo).stdout.strip()


@pytest.fixture
def isolated_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Path]]:
    state_dir = tmp_path / "config-sync-state"
    root_a = tmp_path / "kirocrew-home"
    root_b = tmp_path / "kiro-home"
    root_a.mkdir(parents=True, exist_ok=True)
    root_b.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setenv("KIROCREW_HOME", str(root_a))
    monkeypatch.setenv("KIRO_HOME", str(root_b))

    yield {"state_dir": state_dir, "root_a": root_a, "root_b": root_b}


@pytest.fixture
def routes_module(isolated_env: dict[str, Path]) -> Any:
    from backend import routes

    return routes


@pytest.fixture(autouse=True)
def enabled_by_default(routes_module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes_module, "is_app_enabled", lambda _name: True)


@pytest.fixture
def store(isolated_env: dict[str, Path]) -> state.StateStore:
    return state.load_state()


def _seed_and_apply(
    tmp_path: Path,
    isolated_env: dict[str, Path],
    store: state.StateStore,
) -> tuple[str, dict[str, bytes]]:
    """Seed live pre-apply bytes, run a REAL `apply.apply_commit` against

    a real git-archived commit tree, and return ``(apply_id,
    pre_apply_bytes)`` where ``pre_apply_bytes`` maps
    ``"<root>/<relpath>"`` to the exact bytes that existed live BEFORE
    the apply — the ground truth every restore assertion in this file
    checks against.
    """
    root_a = isolated_env["root_a"]
    (root_a / "steering").mkdir(parents=True, exist_ok=True)
    (root_a / "steering" / "a.md").write_text("# A live-before\n", encoding="utf-8")
    pre_a = (root_a / "steering" / "a.md").read_bytes()

    (root_a / "config.json").write_text(
        json.dumps({"agents": {"live": {}}}, indent=2), encoding="utf-8"
    )
    pre_config = (root_a / "config.json").read_bytes()

    repo = _init_bundle_repo(tmp_path)
    sha = _head_sha(repo)
    commit_root = tmp_path / "commit-root"
    _checkout_head_tree(repo, commit_root)

    store.set_pending(
        sha=sha,
        author="a",
        subject="s",
        classified_paths={"steering/a.md": "A", "config.json": "A"},
    )

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["steering/a.md", "config.json"], "B": []},
        store=store,
    )
    assert result.outcome == "applied", result
    assert result.apply_id is not None
    assert set(result.applied) == {"steering/a.md", "config.json"}

    return result.apply_id, {
        "A/steering/a.md": pre_a,
        "A/config.json": pre_config,
    }


def _assert_no_network_or_git(
    monkeypatch: pytest.MonkeyPatch, routes_module: Any
) -> None:
    """Arms a guard proving the CALL MADE AFTER this point invokes no

    `subprocess.run` — armed inside each test, AFTER its fixture setup
    (which runs real git directly via the bare `subprocess` module) has
    already completed, rather than as an autouse fixture. The one git
    call site reachable from a materialize/apply path
    (`backend.materialize`'s `git archive` call, per that module's own
    docstring: "the one additional git call this function needs") is
    what this guard patches — `backend/routes.py` itself no longer
    imports `subprocess` at all (the git call moved out of `routes.py`
    into `backend/materialize.py` under the auto-apply ruling), so
    patching `routes_module.subprocess` is no longer meaningful; patching
    `backend.materialize`'s own `subprocess.run` still catches a
    mutation that adds a git/network call inside `restore`, since that
    is the only module in this app's materialize/apply seam that ever
    shells out to git.
    """
    from backend import materialize as materialize_module

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError(
            "restore must not invoke subprocess.run (no network/git call)"
        )

    monkeypatch.setattr(materialize_module.subprocess, "run", _boom)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_restore_puts_back_exact_pre_apply_bytes_for_every_backed_up_file(
    tmp_path: Path,
    isolated_env: dict[str, Path],
    store: state.StateStore,
    routes_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    apply_id, pre_bytes = _seed_and_apply(tmp_path, isolated_env, store)

    root_a = isolated_env["root_a"]
    # Confirm the apply actually changed the live bytes first, so the
    # restore assertion below is not vacuously true.
    assert (root_a / "steering" / "a.md").read_bytes() != pre_bytes["A/steering/a.md"]
    assert (root_a / "config.json").read_bytes() != pre_bytes["A/config.json"]

    _assert_no_network_or_git(monkeypatch, routes_module)
    result = routes_module.restore(store, apply_id)

    assert result["status"] == "ok", result
    assert (root_a / "steering" / "a.md").read_bytes() == pre_bytes["A/steering/a.md"]
    assert (root_a / "config.json").read_bytes() == pre_bytes["A/config.json"]


def test_restore_reports_restored_relpaths_per_root(
    tmp_path: Path,
    isolated_env: dict[str, Path],
    store: state.StateStore,
    routes_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    apply_id, _pre_bytes = _seed_and_apply(tmp_path, isolated_env, store)

    _assert_no_network_or_git(monkeypatch, routes_module)
    result = routes_module.restore(store, apply_id)

    assert result["status"] == "ok", result
    restored = result["restored"]
    assert set(restored.get("A", [])) == {"steering/a.md", "config.json"}
    assert restored.get("B", []) == []


def test_restore_both_roots_restores_each_roots_own_backed_up_file(
    tmp_path: Path,
    isolated_env: dict[str, Path],
    store: state.StateStore,
    routes_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A commit touching both root A and root B: each root's backed-up

    file is restored to its OWN root, not cross-applied — proving the
    ``<root>/<relpath>`` namespacing `apply.py::_backup_file` uses is
    respected on the way back, not merely on the way in.
    """
    root_a = isolated_env["root_a"]
    root_b = isolated_env["root_b"]

    (root_a / "steering").mkdir(parents=True, exist_ok=True)
    (root_a / "steering" / "a.md").write_text("# A live-before\n", encoding="utf-8")
    pre_a = (root_a / "steering" / "a.md").read_bytes()

    # Prior live bytes for the two shared registration files too, so
    # they are genuinely BACKED-UP-and-restored by this apply (like
    # steering/a.md) rather than pure creations with no backup entry
    # (`apply.py::_backup_file`'s no-op-on-creation branch) — keeping
    # this test's restored-set assertions below simple and matching its
    # original intent (each root's own file(s) restored to their own
    # pre-apply bytes).
    (root_a / "config.json").write_text(
        json.dumps({"agents": {}}, indent=2), encoding="utf-8"
    )
    pre_config = (root_a / "config.json").read_bytes()
    (root_a / "agent_model_state.json").write_text(
        json.dumps({}, indent=2), encoding="utf-8"
    )
    pre_model_state = (root_a / "agent_model_state.json").read_bytes()

    (root_b / "agents").mkdir(parents=True, exist_ok=True)
    (root_b / "agents" / "x.json").write_text(
        json.dumps({"name": "x-live"}, indent=2), encoding="utf-8"
    )
    pre_b = (root_b / "agents" / "x.json").read_bytes()

    repo = tmp_path / "bundle-repo-both"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    (repo / "steering").mkdir()
    (repo / "steering" / "a.md").write_text("# A committed\n", encoding="utf-8")
    # A COMPLETE registration for agent "x" — registration.py
    # (backend/registration.py) refuses a registration missing any of
    # its required parts (requirements.md 5.6/5.14) and blocks that
    # agent's own present parts from applying at all, so a bare
    # `agents/x.json` with no config.json agents{"x"} entry and no
    # agent_model_state.json pin is not eligible to apply — there would
    # be nothing for this test's restore assertion to restore. The two
    # shared parts (config.json, agent_model_state.json) are classified
    # root A here alongside steering/a.md, keeping root B's own changed
    # set to exactly agents/x.json — the file this test's restore
    # assertion is about — so the shared files don't introduce a
    # second, unrelated "created in root B" case this test isn't
    # checking.
    #
    # The prompt part is satisfied by `agents/x.json`'s inline `prompt`
    # string (requirements.md 5.11), so no prompt file is needed.
    (repo / "agents").mkdir()
    (repo / "agents" / "x.json").write_text(
        json.dumps(
            {
                "name": "x",
                "description": "Agent x.",
                "prompt": "You are agent x.",
                "tools": ["read", "write"],
                "allowedTools": ["read"],
                "resources": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (repo / "config.json").write_text(
        json.dumps({"agents": {"x": {"model": "claude-sonnet-5"}}}, indent=2),
        encoding="utf-8",
    )
    (repo / "agent_model_state.json").write_text(
        json.dumps({"x": {"model_managed": False}}, indent=2), encoding="utf-8"
    )
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "initial", cwd=repo)
    sha = _head_sha(repo)
    commit_root = tmp_path / "commit-root-both"
    _checkout_head_tree(repo, commit_root)

    store.set_pending(
        sha=sha,
        author="a",
        subject="s",
        classified_paths={
            "steering/a.md": "A",
            "config.json": "A",
            "agent_model_state.json": "A",
            "agents/x.json": "B",
        },
    )

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={
            "A": [
                "steering/a.md",
                "config.json",
                "agent_model_state.json",
            ],
            "B": ["agents/x.json"],
        },
        store=store,
    )
    assert result.outcome == "applied", result
    assert result.apply_id is not None

    restore_result = None
    _assert_no_network_or_git(monkeypatch, routes_module)
    restore_result = routes_module.restore(store, result.apply_id)

    assert restore_result["status"] == "ok", restore_result
    assert (root_a / "steering" / "a.md").read_bytes() == pre_a
    assert (root_a / "config.json").read_bytes() == pre_config
    assert (root_a / "agent_model_state.json").read_bytes() == pre_model_state
    assert (root_b / "agents" / "x.json").read_bytes() == pre_b
    restored = restore_result["restored"]
    assert set(restored.get("A", [])) == {
        "steering/a.md",
        "config.json",
        "agent_model_state.json",
    }
    assert restored.get("B", []) == ["agents/x.json"]


def test_restore_does_not_invent_deletion_of_apply_created_files(
    tmp_path: Path,
    isolated_env: dict[str, Path],
    store: state.StateStore,
    routes_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SUPERSEDED CONTRACT (kept for history — see the module docstring's

    "UPDATE (this task)" note): this test originally pinned that restore
    must NOT delete a created file, because no mechanism recorded which
    relpaths an apply created. That gap is exactly what this task closes
    via the ``restore_dir/<root>/.created-manifest.json`` mechanism
    (`test_apply.py`'s recording-half test) plus `routes.restore` acting
    on it. This test now asserts the NEW contract: the created file
    (`config.json`, which never existed live before the apply) IS
    removed by restore, reported under ``"removed"``, and the backed-up
    file (`steering/a.md`) is still put back to its exact pre-apply
    bytes. This duplicates
    `test_restore_removes_a_file_the_apply_created_and_reports_it_
    separately` above by design — that test is the primary spec for the
    new behaviour; this one stays to make the history of the reversal
    (old contract -> new contract, same scenario) legible in one diff.
    """
    root_a = isolated_env["root_a"]

    # Only steering/a.md has prior live bytes; config.json does NOT exist
    # live before the apply, so config.json is a pure creation with no
    # backup entry (`_backup_file`'s no-op branch).
    (root_a / "steering").mkdir(parents=True, exist_ok=True)
    (root_a / "steering" / "a.md").write_text("# A live-before\n", encoding="utf-8")
    pre_a = (root_a / "steering" / "a.md").read_bytes()
    assert not (root_a / "config.json").exists()

    repo = _init_bundle_repo(tmp_path)
    sha = _head_sha(repo)
    commit_root = tmp_path / "commit-root"
    _checkout_head_tree(repo, commit_root)

    store.set_pending(
        sha=sha,
        author="a",
        subject="s",
        classified_paths={"steering/a.md": "A", "config.json": "A"},
    )

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["steering/a.md", "config.json"], "B": []},
        store=store,
    )
    assert result.outcome == "applied", result
    assert result.apply_id is not None

    _assert_no_network_or_git(monkeypatch, routes_module)
    restore_result = routes_module.restore(store, result.apply_id)

    assert restore_result["status"] == "ok", restore_result
    # The backed-up file goes back to its pre-apply bytes.
    assert (root_a / "steering" / "a.md").read_bytes() == pre_a
    # The created file is now REMOVED — this is the new contract this
    # task establishes; the pre-apply state truly had no config.json.
    assert not (root_a / "config.json").exists()
    # And it DOES appear in the removed-relpaths report, distinct from
    # restored (which only ever names backed-up files).
    assert "config.json" in restore_result["removed"].get("A", [])
    assert "config.json" not in restore_result["restored"].get("A", [])


def test_restore_refuses_an_unknown_apply_id(
    tmp_path: Path,
    isolated_env: dict[str, Path],
    store: state.StateStore,
    routes_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_no_network_or_git(monkeypatch, routes_module)
    result = routes_module.restore(store, "apply-does-not-exist")

    assert result["status"] == "error"
    root_a = isolated_env["root_a"]
    root_b = isolated_env["root_b"]
    # Nothing on disk changed as a side effect of a refused restore.
    assert list(root_a.rglob("*")) == []
    assert list(root_b.rglob("*")) == []


@pytest.mark.parametrize("traversal_id", ["../x", "..%2Fx", "a/../../etc"])
def test_restore_refuses_a_path_traversal_apply_id(
    tmp_path: Path,
    isolated_env: dict[str, Path],
    store: state.StateStore,
    routes_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    traversal_id: str,
) -> None:
    """Mutation this catches: an implementation that does

    ``restore_dirs.get(apply_id)`` with no format validation, or that
    joins ``apply_id`` onto a base directory without rejecting ``..``
    segments, would let a crafted ``apply_id`` walk outside the app's own
    restores directory. This test fails (goes green on a bad
    implementation) the moment such a join is attempted with no
    containment check — i.e. removing the traversal guard is exactly the
    mutation this test is designed to catch.
    """
    _assert_no_network_or_git(monkeypatch, routes_module)
    result = routes_module.restore(store, traversal_id)

    assert result["status"] == "error"
    root_a = isolated_env["root_a"]
    root_b = isolated_env["root_b"]
    assert list(root_a.rglob("*")) == []
    assert list(root_b.rglob("*")) == []


def _seed_apply_with_one_created_file(
    tmp_path: Path,
    isolated_env: dict[str, Path],
    store: state.StateStore,
) -> tuple[str, bytes]:
    """Seed one file with prior live bytes (backed up) and one file with

    NO prior live bytes (a pure creation, per `apply.py::_backup_file`'s
    documented no-op-on-creation behaviour), run a real `apply_commit`,
    and return ``(apply_id, pre_apply_bytes_for_the_backed_up_file)``.
    Mirrors `test_apply.py`'s new
    ``test_apply_records_a_created_file_manifest_per_root_restore_dir``
    fixture shape so both halves (recording, restoring) exercise the
    identical apply.
    """
    root_a = isolated_env["root_a"]
    (root_a / "steering").mkdir(parents=True, exist_ok=True)
    (root_a / "steering" / "a.md").write_text("# A live-before\n", encoding="utf-8")
    pre_a = (root_a / "steering" / "a.md").read_bytes()
    assert not (root_a / "config.json").exists()

    repo = _init_bundle_repo(tmp_path)
    sha = _head_sha(repo)
    commit_root = tmp_path / "commit-root"
    _checkout_head_tree(repo, commit_root)

    store.set_pending(
        sha=sha,
        author="a",
        subject="s",
        classified_paths={"steering/a.md": "A", "config.json": "A"},
    )

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["steering/a.md", "config.json"], "B": []},
        store=store,
    )
    assert result.outcome == "applied", result
    assert result.apply_id is not None

    return result.apply_id, pre_a


def test_restore_removes_a_file_the_apply_created_and_reports_it_separately(
    tmp_path: Path,
    isolated_env: dict[str, Path],
    store: state.StateStore,
    routes_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Req 4.7's exact-pre-apply-state gap this task exists to close:

    ``config.json`` had no prior live bytes, so `apply.py::_backup_file`
    never backed it up — it is a pure creation. `restore` must REMOVE it
    (returning the instance to its true pre-apply state, where the file
    did not exist) and report it under a SEPARATE ``"removed"`` key,
    distinct from ``"restored"`` (which names files put back from real
    backed-up bytes). This is a DELIBERATE narrowing of this test file's
    own prior gap note (`test_restore_does_not_invent_deletion_of_apply_
    created_files` in the sibling file, which pinned the absence of any
    created-file record) — it is superseded once `apply.py` writes the
    ``.created-manifest.json`` this task's `test_apply.py` half pins as
    the mechanism.

    EXPECTED TO FAIL until BOTH halves land: `apply.py` writing the
    manifest, and `routes.restore` reading it to remove + report.
    """
    apply_id, pre_a = _seed_apply_with_one_created_file(tmp_path, isolated_env, store)
    root_a = isolated_env["root_a"]
    created_bytes_after_apply = (root_a / "config.json").read_bytes()
    assert (root_a / "config.json").exists()

    _assert_no_network_or_git(monkeypatch, routes_module)
    result = routes_module.restore(store, apply_id)

    assert result["status"] == "ok", result
    # The backed-up file is put back to its exact pre-apply bytes.
    assert (root_a / "steering" / "a.md").read_bytes() == pre_a
    # The created file is GONE — restore actually returns the instance to
    # its pre-apply state, not merely "whatever had a backup".
    assert not (root_a / "config.json").exists()

    restored = result["restored"]
    removed = result["removed"]
    assert restored.get("A", []) == ["steering/a.md"]
    assert removed.get("A", []) == ["config.json"]
    # A removed path must never ALSO be double-counted as restored.
    assert "config.json" not in restored.get("A", [])
    # Sanity: the removed file really is gone, not merely reported gone.
    assert created_bytes_after_apply  # (non-empty; the apply really wrote it)
    assert not (root_a / "config.json").exists()


def test_restore_removed_manifest_tampered_with_traversal_relpath_is_refused(
    tmp_path: Path,
    isolated_env: dict[str, Path],
    store: state.StateStore,
    routes_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Security guard: `restore` must NEVER remove a path outside the

    root, even when the ``.created-manifest.json`` itself has been
    tampered with to name a `..`-relative or absolute path. This proves
    the removal step re-validates each manifest entry with the SAME
    containment discipline `apply.py::_resolve_target` uses for writes —
    a manifest is data read off disk, not a trusted instruction.

    MUTATION NOTE: the real narrowing dimension is "every relpath read
    from the manifest is re-validated for containment before any
    filesystem removal happens, exactly like a relpath arriving through
    `changed_paths` is". The deliberately violating input is the
    tampered manifest entry itself (``"../outside-marker.txt"``) — this
    turns RED the moment removal is implemented as a naive
    ``(root_path / entry).unlink()`` with no revalidation, because that
    naive join resolves outside the root and the outside marker file
    would then be gone; it turns GREEN once removal re-runs the
    containment check and refuses that one entry while still restoring
    the legitimately backed-up file.
    """
    apply_id, pre_a = _seed_apply_with_one_created_file(tmp_path, isolated_env, store)
    root_a = isolated_env["root_a"]

    outside_marker = root_a.parent / "outside-marker.txt"
    outside_marker.write_text("must never be deleted", encoding="utf-8")

    # Tamper with the manifest apply.py wrote: replace its contents with
    # a path-traversal payload. This directly exercises the mechanism
    # test_apply.py's recording-half test pins
    # (`restore_dir/<root>/.created-manifest.json`) — if that file is
    # not present yet (apply.py has not implemented it), write it
    # ourselves at the pinned location so THIS test isolates the
    # restore-side guard rather than depending on the recording half
    # already landing.
    restore_dir = Path(store.restore_dirs[apply_id])
    manifest_path = restore_dir / "A" / ".created-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(["../outside-marker.txt"]), encoding="utf-8")

    _assert_no_network_or_git(monkeypatch, routes_module)
    result = routes_module.restore(store, apply_id)

    # The traversal entry must never be honoured.
    assert outside_marker.exists(), "restore must never remove a path outside its root"
    assert outside_marker.read_text(encoding="utf-8") == "must never be deleted"
    # The legitimately backed-up file still restores correctly — a
    # tampered manifest entry must not abort the whole restore.
    assert (root_a / "steering" / "a.md").read_bytes() == pre_a
    removed = result.get("removed", {})
    assert "../outside-marker.txt" not in removed.get("A", [])


def test_restore_removed_manifest_tampered_with_absolute_path_is_refused(
    tmp_path: Path,
    isolated_env: dict[str, Path],
    store: state.StateStore,
    routes_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same guard, the other traversal shape: an ABSOLUTE path in a

    tampered manifest must be refused rather than resolved as-is (an
    absolute path passed to ``Path.__truediv__`` silently discards the
    left operand in `pathlib`, which is exactly the kind of naive-join
    mutation this test is designed to catch).
    """
    apply_id, pre_a = _seed_apply_with_one_created_file(tmp_path, isolated_env, store)
    root_a = isolated_env["root_a"]

    outside_marker = root_a.parent / "abs-outside-marker.txt"
    outside_marker.write_text("must never be deleted", encoding="utf-8")

    restore_dir = Path(store.restore_dirs[apply_id])
    manifest_path = restore_dir / "A" / ".created-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps([str(outside_marker)]), encoding="utf-8")

    _assert_no_network_or_git(monkeypatch, routes_module)
    result = routes_module.restore(store, apply_id)

    assert outside_marker.exists(), "restore must never remove an absolute-path entry"
    assert outside_marker.read_text(encoding="utf-8") == "must never be deleted"
    assert (root_a / "steering" / "a.md").read_bytes() == pre_a
    removed = result.get("removed", {})
    assert str(outside_marker) not in removed.get("A", [])


def test_restore_after_a_restore_is_safe_and_idempotent(
    tmp_path: Path,
    isolated_env: dict[str, Path],
    store: state.StateStore,
    routes_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling `restore` a SECOND time for the same ``apply_id`` must be

    safe — this test pins IDEMPOTENT as the chosen behaviour (as opposed
    to "clearly refused"): the second call still returns ``status: ok``,
    still restores the same backed-up bytes (a no-op re-write, since
    they are already in place), and still removes the same created files
    (a no-op, since they are already gone) rather than erroring because
    a created file is missing on the second pass. This is the stated
    per-task choice; a `software-engineer` implementing removal must not
    let a `FileNotFoundError` on the already-removed created file
    propagate on a second call.

    MUTATION NOTE: the real narrowing dimension is "restore tolerates a
    created-file entry that is already absent on disk". A mutation that
    calls ``path.unlink()`` with no ``missing_ok=True`` (or no
    surrounding existence check) makes the SECOND call raise, and this
    test's second-call assertion would then fail with an uncaught
    exception rather than a clean ``status: ok``.
    """
    apply_id, pre_a = _seed_apply_with_one_created_file(tmp_path, isolated_env, store)
    root_a = isolated_env["root_a"]

    _assert_no_network_or_git(monkeypatch, routes_module)
    first = routes_module.restore(store, apply_id)
    assert first["status"] == "ok", first
    assert (root_a / "steering" / "a.md").read_bytes() == pre_a
    assert not (root_a / "config.json").exists()

    # A second restore call against the SAME apply_id, after the first
    # already restored/removed everything.
    second = routes_module.restore(store, apply_id)

    assert second["status"] == "ok", second
    assert (root_a / "steering" / "a.md").read_bytes() == pre_a
    assert not (root_a / "config.json").exists()
    # Idempotent: repeating reports the same restored set, not an error
    # and not an empty/degraded report.
    assert second["restored"].get("A", []) == first["restored"].get("A", [])


def test_restore_refuses_while_disabled(
    tmp_path: Path,
    isolated_env: dict[str, Path],
    store: state.StateStore,
    routes_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    apply_id, pre_bytes = _seed_and_apply(tmp_path, isolated_env, store)

    monkeypatch.setattr(routes_module, "is_app_enabled", lambda _name: False)

    root_a = isolated_env["root_a"]
    live_before_restore_attempt = (root_a / "steering" / "a.md").read_bytes()

    _assert_no_network_or_git(monkeypatch, routes_module)
    result = routes_module.restore(store, apply_id)

    assert result["status"] == "error"
    assert result["reason"] == "config-sync is disabled"
    # Mutation this catches: an implementation that checks `is_app_enabled`
    # AFTER already restoring files would still leave the refusal
    # response, hiding the fact that a mutation (writing) happened. This
    # asserts the live file is UNCHANGED by the refused call — still
    # whatever the apply left it as, never reverted to `pre_bytes` — so a
    # disabled restore truly did nothing.
    assert live_before_restore_attempt != pre_bytes["A/steering/a.md"]
    assert (root_a / "steering" / "a.md").read_bytes() == live_before_restore_attempt
