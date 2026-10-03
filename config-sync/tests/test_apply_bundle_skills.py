"""Integration test: config-bundles/skills/** applies to the LIVE skills/

path (tests/test_allowlist_bundle_skills.py covers the pure allowlist
data/function; this proves apply_commit's write-side wiring end to end).

Operator ruling, 2026-10-03: a skill staged by sync-bundles.sh at
``config-bundles/skills/<name>/SKILL.md`` must be applied to
``skills/<name>/SKILL.md`` — the live path skill_search and the gateway's
skill loader actually scan — never left sitting at the staging path.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

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
    )


@pytest.fixture
def bundle_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "bundle-repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    (repo / "config.json").write_text(json.dumps({"agents": {}}), encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "initial", cwd=repo)
    return repo


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


def _head_sha(repo: Path) -> str:
    return _git("rev-parse", "HEAD", cwd=repo).stdout.strip()


def _seed_pending(store: state.StateStore, sha: str) -> None:
    store.set_pending(
        sha=sha,
        author="a",
        subject="s",
        classified_paths={},
        ignored_paths=[],
        touched_classes=[],
    )


def test_config_bundles_skill_md_applies_to_live_skills_path(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """A committed config-bundles/skills/sdlc/SKILL.md must land on disk

    at skills/sdlc/SKILL.md — not at config-bundles/skills/sdlc/SKILL.md
    — so skill_search can actually find it after an apply.
    """
    commit_root = tmp_path / "commit-root"
    (commit_root / "config-bundles" / "skills" / "sdlc").mkdir(parents=True)
    skill_body = "---\nname: sdlc\n---\n# SDLC\nBody.\n"
    (commit_root / "config-bundles" / "skills" / "sdlc" / "SKILL.md").write_text(
        skill_body, encoding="utf-8"
    )

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["config-bundles/skills/sdlc/SKILL.md"], "B": []},
        store=state_store,
    )

    root_a = Path(os.environ["KIROCREW_HOME"])
    live_path = root_a / "skills" / "sdlc" / "SKILL.md"
    staging_path = root_a / "config-bundles" / "skills" / "sdlc" / "SKILL.md"

    assert live_path.is_file(), "the skill must be written to the LIVE skills/ path"
    assert live_path.read_text(encoding="utf-8") == skill_body
    assert not staging_path.exists(), (
        "apply must not ALSO write a copy at the staging config-bundles/ "
        "path — only the live path"
    )
    assert "config-bundles/skills/sdlc/SKILL.md" in result.applied


def test_config_bundles_skill_scripts_apply_to_live_skills_path(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    commit_root = tmp_path / "commit-root"
    scripts_dir = commit_root / "config-bundles" / "skills" / "sdlc" / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "run.sh").write_text("#!/bin/bash\necho hi\n", encoding="utf-8")

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={
            "A": ["config-bundles/skills/sdlc/scripts/run.sh"],
            "B": [],
        },
        store=state_store,
    )

    root_a = Path(os.environ["KIROCREW_HOME"])
    live_path = root_a / "skills" / "sdlc" / "scripts" / "run.sh"
    assert live_path.is_file()
    assert live_path.read_text(encoding="utf-8") == "#!/bin/bash\necho hi\n"


def test_config_bundles_skill_md_backup_is_keyed_by_live_relpath(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """Overwriting an EXISTING live skill backs up the LIVE file's prior

    bytes at the live relpath — restore must be able to find it there,
    not at the staging relpath which was never the live file's location.
    """
    root_a = Path(os.environ["KIROCREW_HOME"])
    (root_a / "skills" / "sdlc").mkdir(parents=True)
    old_body = "---\nname: sdlc\n---\n# Old\n"
    (root_a / "skills" / "sdlc" / "SKILL.md").write_text(old_body, encoding="utf-8")

    commit_root = tmp_path / "commit-root"
    (commit_root / "config-bundles" / "skills" / "sdlc").mkdir(parents=True)
    new_body = "---\nname: sdlc\n---\n# New\n"
    (commit_root / "config-bundles" / "skills" / "sdlc" / "SKILL.md").write_text(
        new_body, encoding="utf-8"
    )

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["config-bundles/skills/sdlc/SKILL.md"], "B": []},
        store=state_store,
    )

    assert result.apply_id is not None
    restore_dir = Path(state_store.restore_dirs[result.apply_id])
    backup_path = restore_dir / "A" / "skills" / "sdlc" / "SKILL.md"
    assert backup_path.is_file()
    assert backup_path.read_text(encoding="utf-8") == old_body

    live_path = root_a / "skills" / "sdlc" / "SKILL.md"
    assert live_path.read_text(encoding="utf-8") == new_body
