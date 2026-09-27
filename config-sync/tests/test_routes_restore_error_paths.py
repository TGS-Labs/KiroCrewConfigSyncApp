"""Coverage for `backend.routes.restore`'s own defensive branches that

`tests/test_routes_restore.py` (pinned, not to be edited) does not drive:
a live-side resolution failure, a missing backup file, an `OSError` on
the copy-back write, and an `OSError` on the created-file removal. Each
of these is a fail-safe "skip this one entry, keep going" branch inside
`restore()` — this file exists only to prove they are reachable and
behave as a no-op-for-that-entry rather than raising, per
testing-standards.md's coverage requirement on `backend/routes.py`.

Reuses `tests/test_routes_restore.py`'s own fixture shape (isolated env,
a real `apply.apply_commit` seeded via real git) rather than re-deriving
a parallel one, so the restore layout under test is the real one
`apply.py` produces.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Iterator

import pytest

from backend import apply, state

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
    tmp_path: Path, isolated_env: dict[str, Path], store: state.StateStore
) -> str:
    root_a = isolated_env["root_a"]
    (root_a / "steering").mkdir(parents=True, exist_ok=True)
    (root_a / "steering" / "a.md").write_text("# A live-before\n", encoding="utf-8")
    (root_a / "config.json").write_text(
        json.dumps({"agents": {"live": {}}}, indent=2), encoding="utf-8"
    )

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
    return result.apply_id


def test_restore_skips_a_backed_up_relpath_whose_live_root_cannot_resolve(
    tmp_path: Path,
    isolated_env: dict[str, Path],
    store: state.StateStore,
    routes_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When `apply._resolve_target` reports a relpath cannot be safely

    resolved under its live root (containment failure, a symlink loop,
    ...), the backed-up relpath is skipped rather than raising —
    `restore` still returns `status: ok` and simply omits that relpath
    from `restored`. Driven by directly patching `_resolve_target` to
    report the "cannot resolve" outcome for `config.json` only, since
    `apply.py`'s own containment guard already has direct test coverage
    (`tests/test_apply.py`) — this test only proves `routes.restore`'s
    OWN handling of a `None` result, not `_resolve_target`'s internals.
    """
    apply_id = _seed_and_apply(tmp_path, isolated_env, store)

    real_resolve_target: Any = routes_module._resolve_target

    def _resolve_or_none(root_path: Path, relpath: str) -> Path | None:
        if relpath == "config.json":
            return None
        result: Path | None = real_resolve_target(root_path, relpath)
        return result

    monkeypatch.setattr(routes_module, "_resolve_target", _resolve_or_none)

    result = routes_module.restore(store, apply_id)

    assert result["status"] == "ok", result
    assert "config.json" not in result["restored"].get("A", [])
    assert "steering/a.md" in result["restored"].get("A", [])


def test_restore_skips_when_backup_copy_is_missing_on_disk(
    tmp_path: Path,
    isolated_env: dict[str, Path],
    store: state.StateStore,
    routes_module: Any,
) -> None:
    """A restore_dirs mapping can point at a directory whose per-relpath

    backup file was itself removed out-of-band (or never a regular
    file). `restore` must skip that one entry rather than raising on a
    missing/unreadable backup source.
    """
    apply_id = _seed_and_apply(tmp_path, isolated_env, store)

    restore_dir = Path(store.restore_dirs[apply_id])
    backup_file = restore_dir / "A" / "config.json"
    assert backup_file.is_file()
    backup_file.unlink()
    # Replace it with a directory so `is_file()` is False, not merely
    # absent — covers the same branch either way.
    backup_file.mkdir()

    result = routes_module.restore(store, apply_id)

    assert result["status"] == "ok", result
    assert "config.json" not in result["restored"].get("A", [])


def test_restore_reports_ok_when_the_copy_back_write_raises_oserror(
    tmp_path: Path,
    isolated_env: dict[str, Path],
    store: state.StateStore,
    routes_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate the copy-back write itself failing (`Path.replace` raising)

    for one relpath — `restore` must catch it, skip that relpath (leaving
    it out of `restored`), report it in `failed`, and still process every
    OTHER relpath rather than propagating the exception. Per
    senior-review H6, a per-file restore failure is reported as
    `status: "partial"` (never a silent "ok") — see
    `tests/test_routes_review1.py::TestH6RestoreReportsFailureInsteadOfSilentlyContinuing`
    for the finding this test was updated to match.
    """
    apply_id = _seed_and_apply(tmp_path, isolated_env, store)

    real_replace = Path.replace

    def _failing_replace(self: Path, target: "str | os.PathLike[str]") -> Path:
        if self.name.startswith(".config.json."):
            raise OSError("simulated restore write failure")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", _failing_replace)

    result = routes_module.restore(store, apply_id)

    assert result["status"] == "partial", result
    assert "config.json" not in result["restored"].get("A", [])
    assert any("config.json" in entry for entry in result["failed"])
    # The other backed-up file is unaffected by the one failure.
    assert "steering/a.md" in result["restored"].get("A", [])


def test_restore_reports_ok_when_removing_a_created_file_raises_oserror(
    tmp_path: Path,
    isolated_env: dict[str, Path],
    store: state.StateStore,
    routes_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate `Path.unlink` raising for a legitimate (non-tampered)

    created-file manifest entry — `restore` must catch it, leave that
    relpath out of `removed`, report it in `failed`, and still report
    `status: "partial"` overall (senior-review H6 — see
    `tests/test_routes_review1.py::TestH6RestoreReportsFailureInsteadOfSilentlyContinuing`).
    """
    root_a = isolated_env["root_a"]
    (root_a / "steering").mkdir(parents=True, exist_ok=True)
    (root_a / "steering" / "a.md").write_text("# A live-before\n", encoding="utf-8")

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
    apply_id = result.apply_id
    assert apply_id is not None
    assert (root_a / "config.json").exists()

    real_unlink = Path.unlink

    def _failing_unlink(self: Path, missing_ok: bool = False) -> None:
        if self.name == "config.json" and self.parent == root_a:
            raise OSError("simulated removal failure")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _failing_unlink)

    restore_result = routes_module.restore(store, apply_id)

    assert restore_result["status"] == "partial", restore_result
    assert "config.json" not in restore_result["removed"].get("A", [])
    assert any("config.json" in entry for entry in restore_result["failed"])


def test_restore_skips_a_created_relpath_whose_live_root_cannot_resolve(
    tmp_path: Path,
    isolated_env: dict[str, Path],
    store: state.StateStore,
    routes_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same `None`-result handling as the backed-up-relpath loop, for the

    created-file removal loop: when `_resolve_target` reports a created
    relpath cannot be safely resolved, `restore` skips removing it
    rather than raising.
    """
    root_a = isolated_env["root_a"]
    (root_a / "steering").mkdir(parents=True, exist_ok=True)
    (root_a / "steering" / "a.md").write_text("# A live-before\n", encoding="utf-8")

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
    apply_id = result.apply_id
    assert apply_id is not None
    assert (root_a / "config.json").exists()

    real_resolve_target: Any = routes_module._resolve_target

    def _resolve_or_none(root_path: Path, relpath: str) -> Path | None:
        if relpath == "config.json":
            return None
        result: Path | None = real_resolve_target(root_path, relpath)
        return result

    monkeypatch.setattr(routes_module, "_resolve_target", _resolve_or_none)

    restore_result = routes_module.restore(store, apply_id)

    assert restore_result["status"] == "ok", restore_result
    assert "config.json" not in restore_result["removed"].get("A", [])
    # The file is left in place, unresolved — never removed.
    assert (root_a / "config.json").exists()
