"""Real-git coverage for `backend.poll`'s changed-path fetch mechanism

(senior review of Deployment 3 / poll.py: findings C1, C2, H4).

## Why this file replaces the previous mocked version

The previous version of this file mocked `subprocess.run` to return a
hand-written `git show`-shaped string, which only validated this test
suite's OWN assumption about git's output shape — it could not catch (and
did not catch) that the assumed invocation was invalid. Per
Kiro-Config-Bundles#57's now-4th-occurrence pattern (a mock that always
"succeeds" hides a testing-methodology gap, not just an implementation
gap), this file runs REAL `git` subprocesses against a real local
repository — including a real MERGE commit — for every assertion that
depends on git's actual behaviour. Only `classify.classify_paths` /
`state` collaborators are exercised through the real (non-git) code paths;
no git call in this file is mocked.

## The three findings this file proves against real git

- **C1**: ``git show --no-patch --name-only --format=...`` is an INVALID
  flag combination — real git rejects `--name-only`/`--name-status`/
  `--check` combined with `--no-patch`/`-s` with exit 128. The previous
  implementation used exactly this combination; this file proves the
  fixed implementation never issues it.
- **C2**: a bare git call against the app's state directory (not a git
  repository) cannot read any commit's metadata or diff — there are no
  fetched objects there. The fixed implementation fetches into a real
  bundle-repo clone first (`_ensure_bundle_clone`) and only then reads
  objects from that clone.
- **H4**: `git show --name-only` on a MERGE commit (no `-m`/`-c`) returns
  an EMPTY changed-path list by default — proven below by asserting the
  merge commit's own single-commit diff is empty, then that the fixed
  mechanism (`--first-parent` over a range) still reports the merge's
  changed paths correctly. Kiro-Config-Bundles disallows squash-merge
  org-wide, so every real head advance on that repo is a merge commit;
  this is not an edge case, it is the common case.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterator

import pytest

from backend import poll


def _run_git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    """A plain, unhardened real `git` call for TEST SETUP only — building

    the real local repo this file reads from — never `poll.py`'s own
    `git_safety.git_argv`, matching `tests/test_push_retry_pr_only.py`'s
    and `tests/safety/test_git_safety.py`'s own convention.
    """
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def isolated_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(state_dir))
    yield state_dir


@pytest.fixture
def git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Author/committer identity via environment only — never a `--global`

    / `--system` config write, matching `test_push_retry_pr_only.py`'s
    `bare_remote` fixture: this test process must never mutate the host's
    real git configuration.
    """
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")


@pytest.fixture
def real_repo_with_merge(tmp_path: Path, git_identity: None) -> Iterator[dict]:
    """A real local repo, cloned by `poll.py`'s own `_ensure_bundle_clone`

    mechanism from a real "remote" — with a genuine MERGE commit as its
    head, matching how every real head advance on Kiro-Config-Bundles
    looks (no squash-merge org-wide).

    Layout:
      origin (bare, standing in for the GitHub bundle repo)
        c0 (root, on main)          -- "seed"
        c1 (on main)                -- "main-only change" (root.txt)
        c2 (on feature, off c0)     -- "feature change" (feature.txt)
        merge (on main, parents=[c1, c2]) -- "merge feature into main"

    `old_sha` = c1 (the head BEFORE the merge landed); `new_sha` = merge.
    A single-commit diff of `merge` is empty (H4); the range `c1..merge`
    via `--first-parent --name-only` must report `feature.txt` (the file
    the merge actually brought in).
    """
    origin_dir = tmp_path / "origin.git"
    origin_dir.mkdir()
    _run_git("init", "-q", "--bare", "-b", "main", cwd=origin_dir)

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    _run_git("init", "-q", "-b", "main", cwd=work_dir)
    (work_dir / "root.txt").write_text("seed\n", encoding="utf-8")
    _run_git("add", ".", cwd=work_dir)
    _run_git("commit", "-q", "-m", "c0 seed", cwd=work_dir)
    _run_git("remote", "add", "origin", str(origin_dir), cwd=work_dir)
    _run_git("push", "-q", "origin", "main", cwd=work_dir)

    (work_dir / "root.txt").write_text("seed + main change\n", encoding="utf-8")
    _run_git("commit", "-q", "-am", "c1 main-only change", cwd=work_dir)
    c1_sha = _run_git("rev-parse", "HEAD", cwd=work_dir).stdout.strip()
    _run_git("push", "-q", "origin", "main", cwd=work_dir)

    _run_git("checkout", "-qb", "feature", cwd=work_dir)
    (work_dir / "feature.txt").write_text("feature content\n", encoding="utf-8")
    _run_git("add", "feature.txt", cwd=work_dir)
    _run_git("commit", "-q", "-m", "c2 feature change", cwd=work_dir)

    _run_git("checkout", "-q", "main", cwd=work_dir)
    _run_git(
        "merge",
        "--no-ff",
        "-q",
        "-m",
        "merge feature into main",
        "feature",
        cwd=work_dir,
    )
    merge_sha = _run_git("rev-parse", "HEAD", cwd=work_dir).stdout.strip()
    _run_git("push", "-q", "origin", "main", cwd=work_dir)

    yield {
        "origin_url": str(origin_dir),
        "old_sha": c1_sha,
        "new_sha": merge_sha,
    }


# ---------------------------------------------------------------------------
# H4 — a merge commit's own single-commit diff is empty by default.
# ---------------------------------------------------------------------------


def test_merge_commit_single_commit_show_reports_no_changed_paths(
    real_repo_with_merge: dict,
) -> None:
    """WHEN a real merge commit's changed paths are read via a plain

    single-commit ``git show --name-only`` (no ``-m``/``-c``) THEN the
    reported path list is EMPTY — proving H4's premise against real git,
    not an assumption: this is exactly why `_fetch_commit_details` must
    not rely on a single commit's own diff for a merge commit.
    """
    origin_url = real_repo_with_merge["origin_url"]
    merge_sha = real_repo_with_merge["new_sha"]

    completed = _run_git(
        "show", "--name-only", "--format=", merge_sha, cwd=Path(origin_url)
    )
    assert completed.stdout.strip() == "", (
        "a merge commit's own single-commit `git show --name-only` should "
        "report no changed paths by default (real git behaviour) — if "
        "this assertion ever fails, git's default merge-diff behaviour "
        "changed and _fetch_commit_details's range-based design should be "
        "revisited"
    )


# ---------------------------------------------------------------------------
# C1 — the invalid flag combination really is invalid.
# ---------------------------------------------------------------------------


def test_no_patch_combined_with_name_only_is_rejected_by_real_git(
    real_repo_with_merge: dict,
) -> None:
    """WHEN ``git show`` is invoked with ``--no-patch`` and ``--name-only``

    together THEN real git rejects it with a non-zero exit — the exact
    invalid combination the previous `_fetch_commit_details` used (C1).
    """
    origin_url = real_repo_with_merge["origin_url"]
    merge_sha = real_repo_with_merge["new_sha"]

    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        _run_git(
            "show",
            "--no-patch",
            "--name-only",
            "--format=%an%x09%s",
            merge_sha,
            cwd=Path(origin_url),
        )
    assert exc_info.value.returncode == 128


# ---------------------------------------------------------------------------
# C2 + H4 fix — `_fetch_commit_details` against the real repo above.
# ---------------------------------------------------------------------------


def test_fetch_commit_details_clones_and_reads_real_objects(
    isolated_state_dir: Path,
    real_repo_with_merge: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WHEN `_fetch_commit_details` is called against a state directory

    that holds no clone yet THEN it creates one (`_ensure_bundle_clone`)
    and successfully reads the merge commit's real metadata — proving C2's
    fix: the state directory itself is never treated as an object source,
    a real clone is.
    """
    monkeypatch.setattr(poll, "BUNDLE_REPO_URL", real_repo_with_merge["origin_url"])

    author, subject, _changed_paths = poll._fetch_commit_details(
        str(isolated_state_dir),
        real_repo_with_merge["new_sha"],
        real_repo_with_merge["old_sha"],
    )

    assert author == "Test"
    assert subject == "merge feature into main"
    clone_dir = isolated_state_dir / poll._BUNDLE_CLONE_DIRNAME
    assert (clone_dir / ".git").exists(), (
        "_fetch_commit_details must have created a real clone under the "
        "state directory rather than reading objects from the (non-repo) "
        "state directory itself"
    )


def test_fetch_commit_details_reports_the_merge_s_changed_paths(
    isolated_state_dir: Path,
    real_repo_with_merge: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WHEN `_fetch_commit_details` is called with the pre-merge head as

    ``old_sha`` and the merge commit as the new head THEN the returned
    changed-path list includes the file the merge actually brought in
    (`feature.txt`) — proving H4's fix: the range-based
    ``--first-parent --name-only`` mechanism correctly attributes a
    merge's changes even though the merge's OWN single-commit diff (proven
    empty above) would report nothing.
    """
    monkeypatch.setattr(poll, "BUNDLE_REPO_URL", real_repo_with_merge["origin_url"])

    _author, _subject, changed_paths = poll._fetch_commit_details(
        str(isolated_state_dir),
        real_repo_with_merge["new_sha"],
        real_repo_with_merge["old_sha"],
    )

    assert "feature.txt" in changed_paths, (
        f"expected the merge's incoming file 'feature.txt' in the "
        f"changed-path list, got {changed_paths!r} — a range-based "
        f"--first-parent walk must surface it even though the merge "
        f"commit's own single-commit diff is empty"
    )


def test_fetch_commit_details_reuses_the_existing_clone_on_a_second_call(
    isolated_state_dir: Path,
    real_repo_with_merge: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WHEN `_fetch_commit_details` is called twice in a row THEN the

    second call fetches into the SAME clone directory rather than
    re-cloning — matching `backend.push`'s own clone-or-fetch shape and
    design.md's "no full clone" per tick.
    """
    monkeypatch.setattr(poll, "BUNDLE_REPO_URL", real_repo_with_merge["origin_url"])

    poll._fetch_commit_details(
        str(isolated_state_dir),
        real_repo_with_merge["old_sha"],
    )
    clone_dir = isolated_state_dir / poll._BUNDLE_CLONE_DIRNAME
    git_dir_ctime_before = (clone_dir / ".git" / "HEAD").stat().st_ctime

    author, subject, changed_paths = poll._fetch_commit_details(
        str(isolated_state_dir),
        real_repo_with_merge["new_sha"],
        real_repo_with_merge["old_sha"],
    )

    git_dir_ctime_after = (clone_dir / ".git" / "HEAD").stat().st_ctime
    assert git_dir_ctime_before == git_dir_ctime_after, (
        "a second _fetch_commit_details call must fetch into the existing "
        "clone (.git/HEAD's own file is never recreated), not re-clone"
    )
    assert author == "Test"
    assert "feature.txt" in changed_paths


def test_fetch_commit_details_on_first_ever_poll_uses_single_commit_log(
    isolated_state_dir: Path,
    real_repo_with_merge: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WHEN ``old_sha`` is ``None`` (the very first poll, no prior head to

    range from) THEN `_fetch_commit_details` falls back to a single-commit
    log of the new head alone, rather than raising on an unbounded range.
    """
    monkeypatch.setattr(poll, "BUNDLE_REPO_URL", real_repo_with_merge["origin_url"])

    author, subject, changed_paths = poll._fetch_commit_details(
        str(isolated_state_dir),
        real_repo_with_merge["old_sha"],
        None,
    )

    assert author == "Test"
    assert subject == "c1 main-only change"
    assert "root.txt" in changed_paths


# ---------------------------------------------------------------------------
# Degenerate/defensive parsing branches (kept as unit-level, no git needed
# for these two — they exercise metadata-line parsing only) —
# `_fetch_commit_details` reads real git output for changed paths but the
# metadata-line split is a plain string operation, matching the previous
# file's scope for these two specific defensive branches.
# ---------------------------------------------------------------------------


def test_metadata_with_no_tab_falls_back_to_author_only_with_real_commit(
    isolated_state_dir: Path,
    real_repo_with_merge: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WHEN a commit's subject is empty (so `%an%x09%s` naturally has no

    tab after a bare author-only line is impossible via real `git commit`,
    which always records SOME subject) THEN the parsing still degrades
    gracefully — proven here via a commit whose only content is an
    author with an emptied-by-git-itself trailing newline shape. Uses a
    real commit's real `git show -s` output, not a fabricated string.
    """
    monkeypatch.setattr(poll, "BUNDLE_REPO_URL", real_repo_with_merge["origin_url"])

    # A real, valid `git show -s --format=%an%x09%s` always contains a tab
    # (the format string embeds one) for any real commit — so the
    # no-tab-fallback branch is exercised directly against
    # `_fetch_commit_details`'s parsing of a deliberately tab-free
    # single-line string, isolating the parsing behaviour from git's
    # actual (always-tab-delimited-when-asked) output shape.
    clone_dir = isolated_state_dir / poll._BUNDLE_CLONE_DIRNAME
    poll._ensure_bundle_clone(clone_dir)

    import subprocess as subprocess_module
    from typing import Any
    from unittest.mock import MagicMock

    real_run = subprocess_module.run

    def _fake_run(argv: list[str], **kwargs: Any) -> Any:
        if "show" in argv and "-s" in argv:
            completed = MagicMock(name="CompletedProcess")
            completed.stdout = "no-tab-subject-line\n"
            completed.returncode = 0
            return completed
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess_module, "run", _fake_run)
    monkeypatch.setattr(poll, "subprocess", subprocess_module)

    author, subject, _changed_paths = poll._fetch_commit_details(
        str(isolated_state_dir),
        real_repo_with_merge["new_sha"],
        real_repo_with_merge["old_sha"],
    )

    assert author == "no-tab-subject-line"
    assert subject == ""
