"""Failing tests for senior-review findings C2, H1, H3, and a Low on

``backend/apply.py`` (branch ``feature/config-sync-apply``, HEAD
``f2c6aca``). Covers requirements.md 4.6-4.10 and design.md's apply
step-by-step (steps 2-4a).

- C2 (Critical): ``_restore_redacted_values`` restores a list-shaped
  value (``crons.json``'s ``jobs[]``) by LIST INDEX against the live
  file, not by a stable identity (``name``, then ``id``). If entries are
  reordered or a new one is inserted upstream, a job at a given index can
  receive a DIFFERENT job's live credential — a credential leak across
  identities with no ``needs_credential`` signal.
- H1 (High): a non-crons/instances allowlisted JSON file that fails to
  parse (``_load_json_or_none`` returns ``None``) is written through RAW
  — skipping placeholder-restore (4.10) entirely — instead of being
  refused. A malformed commit file silently destroys the live
  credentials that restore would otherwise have preserved.
- H3 (High): an exception escaping the per-file loop (the backup-not-
  confirmed ``RuntimeError``, an ``OSError`` from the backup step, or a
  sanitize crash on a top-level-list ``crons.json``) after earlier files
  in the same apply were already written leaves the restore directory
  unrecorded — those earlier writes become unrestorable, and the apply
  never returns a result the caller can act on.
- Low: ``apply_id`` is generated at one-second resolution
  (``time.strftime("%Y%m%d%H%M%S")``), so two applies started within the
  same wall-clock second collide on both ``apply_id`` and restore
  directory.

Real git throughout, matching ``tests/test_apply.py``'s fixtures — no
mocked subprocess for git (project lesson: mocked git subprocess has
produced defects five times here).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from backend import apply, redact, state

# ---------------------------------------------------------------------------
# Real-git fixture helpers (mirrors tests/test_apply.py exactly).
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


def _init_minimal_repo(tmp_path: Path) -> Path:
    """A real one-commit repo — apply.py never reads repo history itself

    (``commit_root`` is a pre-checked-out tree), but ``approved_sha`` must
    equal a real HEAD for the approval gate, so every test resolves it
    from a genuine ``git`` commit rather than a hand-typed string.
    """
    repo = tmp_path / "bundle-repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    (repo / "config.json").write_text(json.dumps({"agents": {}}), encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "initial", cwd=repo)
    return repo


def _head_sha(repo: Path) -> str:
    return _git("rev-parse", "HEAD", cwd=repo).stdout.strip()


@pytest.fixture
def bundle_repo(tmp_path: Path) -> Path:
    return _init_minimal_repo(tmp_path)


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


def _root_a() -> Path:
    return Path(os.environ["KIROCREW_HOME"])


_R = redact.REDACTED


# ---------------------------------------------------------------------------
# C2 (Critical) — list-shaped restore must match by stable identity,
# never by list index.
# ---------------------------------------------------------------------------


def test_crons_restore_matches_reordered_jobs_by_name_not_index(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """Live ``crons.json`` has jobs [A(token=a), B(token=b)] at indices

    [0, 1]. The committed file has the SAME jobs reordered to
    [B('<redacted>'), A('<redacted>')] at indices [0, 1]. Restoring by
    index would give index-0 (B) the live index-0 value (a) — A's
    credential leaking into B. Restoring by name/id must instead give A
    back "a" and B back "b".

    SECURITY MUTATION: this is the exact defect under test. If
    ``_restore_redacted_values`` is (as currently written) matching lists
    positionally, index 0 in the commit (job B) zips against live index 0
    (job A) and receives token "a" instead of "b" — this assertion fails
    red against the real code, proving it bites.
    """
    root_a = _root_a()
    (root_a / "crons.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {"name": "A", "env": {"TOKEN": "a"}},
                    {"name": "B", "env": {"TOKEN": "b"}},
                ]
            }
        ),
        encoding="utf-8",
    )

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "crons.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {"name": "B", "env": {"TOKEN": _R}},
                    {"name": "A", "env": {"TOKEN": _R}},
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
        changed_paths={"A": ["crons.json"], "B": []},
        store=state_store,
    )

    assert "crons.json" in result.applied
    written = json.loads((root_a / "crons.json").read_text(encoding="utf-8"))
    by_name = {job["name"]: job for job in written["jobs"]}
    assert by_name["A"]["env"]["TOKEN"] == "a"
    assert by_name["B"]["env"]["TOKEN"] == "b"
    assert result.needs_credential == []


def test_crons_restore_gives_inserted_leading_job_no_ones_credential(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """A NEW job C is inserted FIRST in the commit, ahead of A and B, and

    redacted. Index-based restoration would hand C the live index-0
    value (A's token). Name-based restoration must leave C's placeholder
    in place (no live job named "C" exists) and list it in
    ``needs_credential`` — never substitute anyone else's value.
    """
    root_a = _root_a()
    (root_a / "crons.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {"name": "A", "env": {"TOKEN": "a"}},
                    {"name": "B", "env": {"TOKEN": "b"}},
                ]
            }
        ),
        encoding="utf-8",
    )

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "crons.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {"name": "C", "env": {"TOKEN": _R}},
                    {"name": "A", "env": {"TOKEN": _R}},
                    {"name": "B", "env": {"TOKEN": _R}},
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
        changed_paths={"A": ["crons.json"], "B": []},
        store=state_store,
    )

    assert "crons.json" in result.applied
    written = json.loads((root_a / "crons.json").read_text(encoding="utf-8"))
    by_name = {job["name"]: job for job in written["jobs"]}
    assert by_name["C"]["env"]["TOKEN"] == _R
    assert by_name["A"]["env"]["TOKEN"] == "a"
    assert by_name["B"]["env"]["TOKEN"] == "b"
    assert any(
        entry.startswith("crons.json:C.") or entry == "crons.json:C.env.TOKEN"
        for entry in result.needs_credential
    )


def test_crons_restore_matches_by_id_when_name_is_absent(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """Pin the fallback rule: when a job entry has no ``name`` key, match

    live-vs-commit entries by ``id`` instead of falling back to index.
    """
    root_a = _root_a()
    (root_a / "crons.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {"id": "job-1", "env": {"TOKEN": "one"}},
                    {"id": "job-2", "env": {"TOKEN": "two"}},
                ]
            }
        ),
        encoding="utf-8",
    )

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "crons.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {"id": "job-2", "env": {"TOKEN": _R}},
                    {"id": "job-1", "env": {"TOKEN": _R}},
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
        changed_paths={"A": ["crons.json"], "B": []},
        store=state_store,
    )

    assert "crons.json" in result.applied
    written = json.loads((root_a / "crons.json").read_text(encoding="utf-8"))
    by_id = {job["id"]: job for job in written["jobs"]}
    assert by_id["job-1"]["env"]["TOKEN"] == "one"
    assert by_id["job-2"]["env"]["TOKEN"] == "two"


def test_instances_restore_matches_reordered_entries_by_name_not_index(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """Same C2 defect shape, pinned for ``instances.json`` (a list of

    instance records carrying their own ``headers``/``env`` block).
    """
    root_a = _root_a()
    (root_a / "instances.json").write_text(
        json.dumps(
            {
                "instances": [
                    {"name": "host-1", "headers": {"Authorization": "secret-1"}},
                    {"name": "host-2", "headers": {"Authorization": "secret-2"}},
                ]
            }
        ),
        encoding="utf-8",
    )

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "instances.json").write_text(
        json.dumps(
            {
                "instances": [
                    {"name": "host-2", "headers": {"Authorization": _R}},
                    {"name": "host-1", "headers": {"Authorization": _R}},
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
        changed_paths={"A": ["instances.json"], "B": []},
        store=state_store,
    )

    assert "instances.json" in result.applied
    written = json.loads((root_a / "instances.json").read_text(encoding="utf-8"))
    by_name = {rec["name"]: rec for rec in written["instances"]}
    assert by_name["host-1"]["headers"]["Authorization"] == "secret-1"
    assert by_name["host-2"]["headers"]["Authorization"] == "secret-2"
    assert result.needs_credential == []


# ---------------------------------------------------------------------------
# H1 (High) — an unparsable allowlisted JSON file must be refused, not
# written through raw (skipping the 4.10 restore entirely).
# ---------------------------------------------------------------------------

_TRACKED_JSON_RELPATHS = (
    "config.json",
    "hooks.json",
    "mcp.json",
    "crons.json",
    "instances.json",
    "agent_model_state.json",
)


@pytest.mark.parametrize("relpath", _TRACKED_JSON_RELPATHS)
def test_malformed_json_commit_file_is_refused_not_written_raw(
    relpath: str,
    bundle_repo: Path,
    target_root: Path,
    state_store: state.StateStore,
    tmp_path: Path,
) -> None:
    """A malformed commit file for every tracked JSON relpath must be

    refused (reported not-applied) — never written through with its
    placeholders (or arbitrary bytes) intact, which would silently
    destroy whatever live credential restore would otherwise have kept.
    """
    root_a = _root_a()
    live_doc = {"mcpServers": {"github": {"headers": {"Authorization": "live-pat"}}}}
    root_a.joinpath(relpath).write_text(json.dumps(live_doc), encoding="utf-8")
    live_bytes_before = root_a.joinpath(relpath).read_bytes()

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    # Malformed JSON: unterminated object.
    commit_root.joinpath(relpath).write_text('{"mcpServers": ', encoding="utf-8")

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": [relpath], "B": []},
        store=state_store,
    )

    assert (
        relpath in result.not_applied
    ), f"{relpath}: a malformed commit file must be refused, not applied"
    assert relpath not in result.applied
    assert (
        root_a.joinpath(relpath).read_bytes() == live_bytes_before
    ), f"{relpath}: live bytes must be untouched by a refused apply"


def test_malformed_mcp_json_does_not_block_other_files_in_same_apply(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """A malformed ``mcp.json`` in the commit is refused, but an

    unrelated well-formed allowlisted file in the SAME apply still
    applies (design.md's error table: one bad file never sinks the rest).
    """
    root_a = _root_a()

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    commit_root.joinpath("mcp.json").write_text("{not valid json", encoding="utf-8")
    commit_root.joinpath("hooks.json").write_text(
        json.dumps({"hooks": []}), encoding="utf-8"
    )

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["mcp.json", "hooks.json"], "B": []},
        store=state_store,
    )

    assert "mcp.json" in result.not_applied
    assert "hooks.json" in result.applied
    assert json.loads(root_a.joinpath("hooks.json").read_text(encoding="utf-8")) == {
        "hooks": []
    }


# ---------------------------------------------------------------------------
# H3 (High) — an exception escaping the per-file loop after earlier files
# were written must still leave a recorded, usable restore point.
# ---------------------------------------------------------------------------


def _write_three_tracked_files(root_a: Path, commit_root: Path) -> None:
    for name, content in (
        ("config.json", {"agents": {"one": True}}),
        ("hooks.json", {"hooks": ["h1"]}),
        ("agent_model_state.json", {"pins": {"x": "sonnet"}}),
    ):
        commit_root.joinpath(name).write_text(json.dumps(content), encoding="utf-8")


def test_backup_not_confirmed_failure_on_second_file_still_records_restore_dir(
    bundle_repo: Path,
    target_root: Path,
    state_store: state.StateStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Force the "backup was not confirmed on disk" ``RuntimeError`` (the

    ~628-635 guard) on the SECOND of three files, after the first file has
    already been backed up and written. The apply must NOT raise out to
    the caller, must NOT report ``outcome == "applied"``, must leave file
    1's write in place, AND — the H3 defect — must still have recorded a
    restore dir that covers file 1's backup, so ``routes.restore`` can
    undo it.
    """
    root_a = _root_a()
    root_a.joinpath("config.json").write_text(
        json.dumps({"agents": {"old": True}}), encoding="utf-8"
    )
    root_a.joinpath("hooks.json").write_text(
        json.dumps({"hooks": ["old"]}), encoding="utf-8"
    )

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    _write_three_tracked_files(root_a, commit_root)

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    real_backup = apply._backup_file
    call_count = {"n": 0}

    def _flaky_backup(
        live_path: Path, restore_dir: Path, root: str, relpath: str
    ) -> None:
        call_count["n"] += 1
        if call_count["n"] == 2:
            # Simulate the backup silently not landing on disk for the
            # SECOND file only — the real code's own confirmation check
            # then raises RuntimeError.
            return None
        return real_backup(live_path, restore_dir, root, relpath)

    monkeypatch.setattr(apply, "_backup_file", _flaky_backup)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={
            "A": ["config.json", "hooks.json", "agent_model_state.json"],
            "B": [],
        },
        store=state_store,
    )

    assert result.outcome != "applied"
    assert result.apply_id is not None
    assert result.apply_id in state_store.restore_dirs, (
        "H3: an exception after file 1 was already written must still "
        "leave a restore dir recorded so file 1's backup is reachable"
    )
    restore_dir = Path(state_store.restore_dirs[result.apply_id])
    assert (
        restore_dir / "A" / "config.json"
    ).is_file(), "file 1's backup must exist under the recorded restore dir"


def test_oserror_during_backup_still_records_restore_dir_for_prior_writes(
    bundle_repo: Path,
    target_root: Path,
    state_store: state.StateStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same shape, forcing a raw ``OSError`` out of the backup step itself

    (e.g. a disk error) on the second file, rather than the confirmation
    ``RuntimeError``.
    """
    root_a = _root_a()
    root_a.joinpath("config.json").write_text(
        json.dumps({"agents": {"old": True}}), encoding="utf-8"
    )
    root_a.joinpath("hooks.json").write_text(
        json.dumps({"hooks": ["old"]}), encoding="utf-8"
    )

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    _write_three_tracked_files(root_a, commit_root)

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    real_backup = apply._backup_file
    call_count = {"n": 0}

    def _failing_backup(
        live_path: Path, restore_dir: Path, root: str, relpath: str
    ) -> None:
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise OSError("simulated disk error during backup")
        return real_backup(live_path, restore_dir, root, relpath)

    monkeypatch.setattr(apply, "_backup_file", _failing_backup)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={
            "A": ["config.json", "hooks.json", "agent_model_state.json"],
            "B": [],
        },
        store=state_store,
    )

    assert result.outcome != "applied"
    assert result.apply_id is not None
    assert result.apply_id in state_store.restore_dirs, (
        "H3: an OSError mid-loop after file 1 was written must still "
        "leave a restore dir recorded"
    )
    restore_dir = Path(state_store.restore_dirs[result.apply_id])
    assert (restore_dir / "A" / "config.json").is_file()


def test_sanitize_crash_on_top_level_list_crons_still_records_restore_dir(
    bundle_repo: Path,
    target_root: Path,
    state_store: state.StateStore,
    tmp_path: Path,
) -> None:
    """A ``crons.json`` committed as a bare top-level LIST (instead of the

    expected ``{"jobs": [...]}`` object) crashes ``sanitize.sanitize_crons``
    while an earlier file in the same apply (``config.json``) has already
    been backed up and written. The apply must not raise, must not report
    "applied", and must still record a restore dir covering the earlier
    write.
    """
    root_a = _root_a()
    root_a.joinpath("config.json").write_text(
        json.dumps({"agents": {"old": True}}), encoding="utf-8"
    )

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    commit_root.joinpath("config.json").write_text(
        json.dumps({"agents": {"new": True}}), encoding="utf-8"
    )
    # Top-level list, not the expected {"jobs": [...]} mapping.
    commit_root.joinpath("crons.json").write_text(
        json.dumps([{"name": "bad"}]), encoding="utf-8"
    )

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["config.json", "crons.json"], "B": []},
        store=state_store,
    )

    assert result.outcome != "applied"
    assert result.apply_id is not None
    assert result.apply_id in state_store.restore_dirs, (
        "H3: a sanitize crash on a malformed crons.json must not lose "
        "the restore dir covering config.json's already-written backup"
    )
    restore_dir = Path(state_store.restore_dirs[result.apply_id])
    assert (restore_dir / "A" / "config.json").is_file()


def test_apply_that_only_creates_new_files_records_restore_dir_with_created_manifest(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """An apply whose files are all newly-created (no live file existed,

    so ``_backup_file`` never wrote a real backup, so ``backup_made``
    stays ``False`` under the current implementation) must still record a
    restore dir — restore's job for pure creations is to remove them, and
    that needs both the apply_id AND the created-manifest, not just a
    directory ``routes.restore`` never learns about because no backup
    happened.
    """
    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    commit_root.joinpath("hooks.json").write_text(
        json.dumps({"hooks": ["brand-new"]}), encoding="utf-8"
    )

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["hooks.json"], "B": []},
        store=state_store,
    )

    assert "hooks.json" in result.applied
    assert result.apply_id is not None
    assert result.apply_id in state_store.restore_dirs, (
        "an apply that only creates new files must still record a "
        "restore dir so its created-manifest is reachable by restore"
    )
    restore_dir = Path(state_store.restore_dirs[result.apply_id])
    manifest_path = restore_dir / "A" / ".created-manifest.json"
    assert manifest_path.is_file()
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == ["hooks.json"]


# ---------------------------------------------------------------------------
# Low — apply_id must be unique per apply even within the same second.
# ---------------------------------------------------------------------------


def test_two_applies_in_the_same_second_get_distinct_apply_ids_and_dirs(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """Two back-to-back applies whose wall-clock second is identical (we

    don't rely on real timing races — this pins the CONTRACT rather than
    trying to hit a race window) must never collide on ``apply_id`` or on
    their restore directory. Runs two real applies in immediate
    succession; if they land in the same second the ids/dirs must still
    differ, and if apply.py truly only has one-second resolution with no
    disambiguator, this fails deterministically rather than flakily.
    """
    commit_root_1 = tmp_path / "commit-root-1"
    commit_root_1.mkdir()
    commit_root_1.joinpath("hooks.json").write_text(
        json.dumps({"hooks": ["first"]}), encoding="utf-8"
    )

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result_1 = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root_1,
        changed_paths={"A": ["hooks.json"], "B": []},
        store=state_store,
    )

    # Re-seed pending so the second apply's SHA gate passes again — a
    # fresh pending record models a second, independent approved apply.
    _seed_pending(state_store, sha)

    commit_root_2 = tmp_path / "commit-root-2"
    commit_root_2.mkdir()
    commit_root_2.joinpath("hooks.json").write_text(
        json.dumps({"hooks": ["second"]}), encoding="utf-8"
    )

    result_2 = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root_2,
        changed_paths={"A": ["hooks.json"], "B": []},
        store=state_store,
    )

    assert result_1.apply_id is not None
    assert result_2.apply_id is not None
    assert result_1.apply_id != result_2.apply_id, (
        "two applies must never share an apply_id even within the same "
        "wall-clock second"
    )
    dir_1 = state_store.restore_dirs.get(result_1.apply_id)
    dir_2 = state_store.restore_dirs.get(result_2.apply_id)
    assert dir_1 is not None and dir_2 is not None
    assert dir_1 != dir_2, "two applies must never share a restore directory"
