"""Tests for backend/apply.py (tasks.md 5.1).

Covers design.md's "backend/apply.py — the applier (backend route,
human-triggered only)" component and requirements.md 4.4, 4.5, 4.7, 4.8:

- 4.4: no automatic application — this module is called from an approve
  route only, never from a poll tick.
- 4.5: WHEN the user approves a specific pending commit THEN the app SHALL
  apply only the files in that commit that match the Requirement 1
  allowlist, and SHALL report any non-allowlisted path as ignored.
- 4.7: BEFORE applying an approved commit THEN the app SHALL record a
  restorable copy of every file it is about to overwrite or delete.
- 4.8: WHEN an apply fails part-way THEN the app SHALL report which files
  were applied and which were not, and SHALL NOT report the apply as
  successful.

INTERFACE CHOSEN (not fully pinned by the spec — narrowest shape
consistent with the Wave 10 modules `apply.py` composes):

    @dataclass(frozen=True)
    class ApplyResult:
        outcome: str  # "applied" | "refused-sha-mismatch" | "partial"
        applied: list[str]
        not_applied: list[str]
        ignored_paths: list[str]
        dropped_cron_names: list[str]
        paused_cron_names: list[str]
        changed_instance_names: list[str]
        incomplete_registrations: dict[str, list[str]]
        needs_credential: list[str]
        propagation: propagate.Report
        apply_id: str | None
        reason: str = ""

    def apply_commit(
        *,
        approved_sha: str,
        commit_root: Path,
        changed_paths: dict[str, list[str]],  # {"A": [...], "B": [...]}
        store: state.StateStore,
    ) -> ApplyResult: ...

`needs_credential` (requirements.md 4.10 / design.md apply step 4) lists,
by server-or-job-name-and-key path (e.g. ``"mcp.json:github.headers.
Authorization"``), every placeholder the applier had to WRITE because no
live value existed at that key path to restore in its place — the
operator-facing list of "you must fill this credential in yourself".

Rationale for this shape:

- `approved_sha` is checked against `store.pending["sha"]` per design.md
  step 1 ("Refuse unless a `pending` record exists and the approved SHA
  matches it") — requirements.md 4.4's "no automatic application" is
  enforced by requiring an explicit approval whose SHA is given by the
  caller (the route), never read from `store.pending` implicitly.
- `commit_root` is a directory holding the approved commit's checked-out
  tree, laid out per root exactly like `registration.check_registrations`
  already expects (`root: Path` with `agent-prompts/<name>.md`,
  `agents/<name>.json`, `config.json` at its top level) — `commit_root`
  IS that same root for registration purposes. Per-root tracked files
  (steering, skills, `crons.json`, etc.) live directly under
  `commit_root` too, mirroring how `backend/collect.py` returns one flat
  `{relpath: bytes}` mapping per root with no extra nesting.
- `changed_paths` is `{"A": [...], "B": [...]}`, matching
  `classify.classify_paths(root, changed_paths)`'s own per-root call
  shape (`poll.py`'s `_classify_changed_paths` already tries every root
  this way) — apply.py filters each root's list through
  `allowlist.is_tracked` / `classify.classify_paths`, ignoring the rest.
- The target filesystem root files are actually applied to is resolved
  the same way `backend/collect.py` resolves it (`KIROCREW_HOME`/
  `KIRO_HOME` env vars) — tests patch those env vars to point at a
  `tmp_path` target root, exactly like `test_collect.py` does, rather
  than adding a second target-root parameter this spec does not name.

OWNERSHIP OF `base_sha` / `pending` (tasks.md 6.1 — re-read; commit
aff8ab4 ratified this as Requirement 4.10 and clarified 6.1's
`resolve_pending()` ownership): `apply.py` NEVER calls
`store.clear_pending()` or `store.advance_base_sha()`. Those two mutations
belong exclusively to `backend/routes.py`'s approve/decline handlers via a
single `state.resolve_pending()` that advances `base_sha` and clears
`pending` TOGETHER — task 6.1's explicit reason is that either call alone,
or the two calls split across two code paths, is exactly the shape that
could leave one mutated without the other. `apply_commit` only READS
`store.pending`/`store.base_sha` to check the approval-SHA gate, and only
WRITES `store.restore_dirs` (via `record_restore_dir`) for the backup
directory it created. Every test that asserts a post-apply state
transition therefore asserts `pending`/`base_sha` are UNTOUCHED, not that
they advanced.

SPEC GAPS RESOLVED / REMAINING:

1. RESOLVED (was gap 1 in an earlier revision of this file) — the
   redaction-boundary interaction is now Requirement 4.10 / design.md
   apply step 4 (commit a06f117 reordered this — restore is step 4,
   sanitize is step 4a, AFTER restore): for every `headers`/`env` value
   in an approved commit's file that is EXACTLY the literal placeholder
   `"<redacted>"`, apply.py restores the LIVE file's existing value at
   that same key path instead of writing the placeholder through. Where
   the live file has no value at that key path (a newly added server, a
   new env key, or the live file does not exist at all), the placeholder
   IS written and that key path is listed in `needs_credential` by
   server/job name and key. Every non-placeholder value in the commit
   applies unchanged — including a real value that merely CONTAINS the
   substring "redacted" (exact-match only, never a substring/contains
   check). Restoration is scoped STRICTLY to `headers`/`env` values —
   push never redacts anything else (a cron `command`, for instance, is
   never touched by `redact.py`), so a placeholder appearing anywhere
   other than a `headers`/`env` value (e.g. a `command` field that is
   literally the string `"<redacted>"`) is applied as committed, never
   restored from live. Ordering matters for `crons.json`: restore (step
   4) runs BEFORE sanitize (step 4a), so the vet call sanitize.
   sanitize_crons makes sees the EXACT bytes that will be written — a
   restored `env` value's sibling `command` field, whatever it is,
   unchanged by restoration since `command` is not a `headers`/`env`
   value.
2. Registration write-through — `registration.check_registrations` reports
   completeness/incompleteness but design.md's own registration section
   does not specify HOW `apply.py` writes the four parts of a *complete*
   registration relative to `commit_root`'s layout for the shared files
   (`config.json`, `agent_model_state.json`) — whether the whole shared
   file is copied verbatim, or only the one agent's key is merged in
   without disturbing other agents' entries already on the live host.
   This file tests the REFUSAL/blocking contract (an incomplete
   registration's own present parts are held back; unrelated files still
   apply) which is unambiguous, and leaves the shared-file merge-vs-
   overwrite mechanics to the software-engineer's implementation, per the
   instruction to state gaps rather than invent behaviour.

Real git is used throughout (a genuine bundle-repo-shaped clone built in
`tmp_path` with add/modify/delete/rename commits and one real merge commit
— per the project lesson, mocked subprocess for git has produced defects
five times in this project). `commit_root` in these tests is populated by
actually checking out a real commit from that real repo, not by
hand-writing files that merely look like a checkout.

Mutation notes (testing-standards.md § Mutation Requirement) are given
inline at the two security/data-integrity assertions this task calls out
explicitly: path-traversal refusal and backup-before-overwrite.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Dict

import pytest

from backend import allowlist, apply, state


# ---------------------------------------------------------------------------
# Real-git fixture helpers. No mocked subprocess anywhere in this file.
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
    """A real bundle-repo-shaped clone with add/modify/delete/rename

    commits and a genuine merge commit — Kiro-Config-Bundles disallows
    squash-merge org-wide, so every real head advance there is itself a
    merge commit (see poll.py's own docstring); this fixture reproduces
    that shape rather than a single linear history.
    """
    repo = tmp_path / "bundle-repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)

    (repo / "steering").mkdir()
    (repo / "steering" / "a.md").write_text("# A\n", encoding="utf-8")
    (repo / "config.json").write_text(
        json.dumps({"agents": {}}, indent=2), encoding="utf-8"
    )
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "initial", cwd=repo)

    # Feature branch: modify one file, add another, delete the original.
    _git("checkout", "-q", "-b", "feature", cwd=repo)
    (repo / "steering" / "a.md").write_text("# A changed\n", encoding="utf-8")
    (repo / "steering" / "b.md").write_text("# B\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "modify a, add b", cwd=repo)

    (repo / "steering" / "a.md").unlink()
    (repo / "steering" / "c.md").write_text("# C\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "delete a, add c", cwd=repo)

    # Rename b -> renamed-b on the feature branch.
    _git("mv", "steering/b.md", "steering/renamed-b.md", cwd=repo)
    _git("commit", "-q", "-m", "rename b", cwd=repo)

    # Merge feature into main as a genuine two-parent merge commit.
    _git("checkout", "-q", "main", cwd=repo)
    _git("merge", "-q", "--no-ff", "feature", "-m", "merge feature", cwd=repo)

    return repo


def _checkout_head_tree(repo: Path, dest: Path) -> None:
    """Materialize the repo's current HEAD tree into ``dest`` via a real

    ``git archive`` — a genuine checkout of real commit content, not a
    hand-written approximation of one.
    """
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(
        ["git", "archive", "HEAD"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        env=_GIT_ENV,
    )
    tar_path = dest.parent / f"{dest.name}.tar"
    tar_path.write_bytes(archive.stdout)
    subprocess.run(
        ["tar", "-xf", str(tar_path), "-C", str(dest)],
        check=True,
    )
    tar_path.unlink()


def _head_sha(repo: Path) -> str:
    return _git("rev-parse", "HEAD", cwd=repo).stdout.strip()


@pytest.fixture
def bundle_repo(tmp_path: Path) -> Path:
    return _init_bundle_repo(tmp_path)


@pytest.fixture
def target_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real target filesystem root, wired as KIROCREW_HOME/KIRO_HOME so

    apply.py resolves the same roots `backend/collect.py` does — matching
    `test_collect.py`'s own env-var-patching convention rather than
    inventing a second target-root parameter the spec never names.
    """
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
# (a) Only allowlisted paths are written; non-allowlisted paths refused
# and reported as ignored (requirements.md 4.5).
# ---------------------------------------------------------------------------


def test_apply_writes_only_allowlisted_paths(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "steering").mkdir()
    (commit_root / "steering" / "a.md").write_text("# A\n", encoding="utf-8")
    (commit_root / "NOT_TRACKED.txt").write_text("nope", encoding="utf-8")

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["steering/a.md", "NOT_TRACKED.txt"], "B": []},
        store=state_store,
    )

    root_a = Path(os.environ["KIROCREW_HOME"])
    assert (root_a / "steering" / "a.md").read_text(encoding="utf-8") == "# A\n"
    assert not (root_a / "NOT_TRACKED.txt").exists()
    assert "NOT_TRACKED.txt" in result.ignored_paths
    assert "steering/a.md" in result.applied


def test_apply_reports_non_allowlisted_path_as_ignored_not_error(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """A non-allowlisted path in the commit must not fail the whole apply

    — it is reported ignored while the rest still applies (design.md's
    error table: "Commit contains non-allowlisted paths | Those paths
    ignored and reported; the rest applies").
    """
    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / ".env").write_text("SECRET=1", encoding="utf-8")
    (commit_root / "config.json").write_text(
        json.dumps({"agents": {}}), encoding="utf-8"
    )

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": [".env", "config.json"], "B": []},
        store=state_store,
    )

    assert result.outcome in ("applied", "partial")
    assert ".env" in result.ignored_paths
    assert "config.json" in result.applied
    root_a = Path(os.environ["KIROCREW_HOME"])
    assert not (root_a / ".env").exists()


# ---------------------------------------------------------------------------
# (b) Backup before overwrite/delete — restore route 6.2 depends on this.
# ---------------------------------------------------------------------------


def test_apply_backs_up_overwritten_file_with_byte_identical_prior_content(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """MUTATION NOTE: this assertion's real narrowing dimension is "the

    backup holds the file's PRE-apply bytes, verbatim". If apply.py's
    backup step were mutated to snapshot AFTER the write instead of
    before (e.g. the backup call moved past the write call), the backup
    bytes would equal the NEW content instead of the old, and this
    assertion would fail on the `!= new_content` check below rather than
    passing vacuously — confirmed by construction: old and new content
    are deliberately different strings, so a post-write snapshot is
    distinguishable from a pre-write one.
    """
    root_a = Path(os.environ["KIROCREW_HOME"])
    (root_a / "config.json").write_text(
        json.dumps({"agents": {"old": True}}), encoding="utf-8"
    )
    prior_bytes = (root_a / "config.json").read_bytes()

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    new_content = json.dumps({"agents": {"new": True}})
    (commit_root / "config.json").write_text(new_content, encoding="utf-8")

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["config.json"], "B": []},
        store=state_store,
    )

    assert result.apply_id is not None
    restore_dir = Path(state_store.restore_dirs[result.apply_id])
    backup_path = restore_dir / "A" / "config.json"
    assert backup_path.is_file()
    assert backup_path.read_bytes() == prior_bytes
    assert backup_path.read_bytes() != new_content.encode("utf-8")
    # And the live file really was overwritten to the new content.
    assert root_a / "config.json"
    assert json.loads((root_a / "config.json").read_text(encoding="utf-8")) == {
        "agents": {"new": True}
    }


def test_apply_backs_up_a_deleted_file_before_removing_it(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    root_a = Path(os.environ["KIROCREW_HOME"])
    (root_a / "steering").mkdir()
    (root_a / "steering" / "gone.md").write_text("# Gone\n", encoding="utf-8")
    prior_bytes = (root_a / "steering" / "gone.md").read_bytes()

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["steering/gone.md"], "B": []},
        store=state_store,
        deleted_paths={"A": ["steering/gone.md"], "B": []},
    )

    assert not (root_a / "steering" / "gone.md").exists()
    assert result.apply_id is not None, "backup must have produced an apply_id"
    restore_dir = Path(state_store.restore_dirs[result.apply_id])
    assert (restore_dir / "A" / "steering" / "gone.md").read_bytes() == prior_bytes


def test_apply_never_deletes_a_file_that_was_never_backed_up_first(
    bundle_repo: Path,
    target_root: Path,
    state_store: state.StateStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MUTATION NOTE: the property under test is ORDERING — backup must

    happen strictly before the destructive write. To prove this assertion
    can fail, patch the backup step to a no-op (simulating a mutation that
    drops the backup call but keeps the delete) and confirm the apply
    either refuses/raises rather than silently deleting an unbacked-up
    file. This is the deliberately-violating-input half of the mutation
    requirement for this task's backup-before-overwrite guard.
    """
    root_a = Path(os.environ["KIROCREW_HOME"])
    (root_a / "steering").mkdir()
    (root_a / "steering" / "gone.md").write_text("# Gone\n", encoding="utf-8")

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    monkeypatch.setattr(apply, "_backup_file", lambda *a, **k: None)

    with pytest.raises(Exception):
        apply.apply_commit(
            approved_sha=sha,
            commit_root=commit_root,
            changed_paths={"A": ["steering/gone.md"], "B": []},
            store=state_store,
            deleted_paths={"A": ["steering/gone.md"], "B": []},
        )
    # The would-be-deleted file must still exist — the mutated (backup
    # disabled) code path must not have reached the delete.
    assert (root_a / "steering" / "gone.md").exists()


# ---------------------------------------------------------------------------
# (c) Atomic writes: temp + rename, no half-written file on mid-apply
# failure.
# ---------------------------------------------------------------------------


def test_apply_writes_atomically_leaving_no_temp_file_behind(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "config.json").write_text(
        json.dumps({"agents": {}}), encoding="utf-8"
    )

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["config.json"], "B": []},
        store=state_store,
    )

    root_a = Path(os.environ["KIROCREW_HOME"])
    leftovers = list(root_a.glob("*.tmp")) + list(root_a.glob(".*.tmp"))
    assert leftovers == []


def test_apply_mid_apply_failure_never_leaves_a_half_written_file(
    bundle_repo: Path,
    target_root: Path,
    state_store: state.StateStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate a write failing partway (os.replace raising) and confirm

    the target file is either fully absent or holds its OLD content —
    never a truncated/partial write. This exercises design.md step 5's
    "no partial commit" at the single-file level (distinct from the
    partial-across-files reporting tested below).
    """
    root_a = Path(os.environ["KIROCREW_HOME"])
    (root_a / "config.json").write_text(
        json.dumps({"agents": {"old": True}}), encoding="utf-8"
    )

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "config.json").write_text(
        json.dumps({"agents": {"new": True}}), encoding="utf-8"
    )

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    real_replace = os.replace

    def _failing_replace(src: object, dst: object) -> None:
        raise OSError("simulated mid-apply failure")

    monkeypatch.setattr(os, "replace", _failing_replace)
    try:
        result = apply.apply_commit(
            approved_sha=sha,
            commit_root=commit_root,
            changed_paths={"A": ["config.json"], "B": []},
            store=state_store,
        )
    finally:
        monkeypatch.setattr(os, "replace", real_replace)

    assert "config.json" in result.not_applied
    on_disk = json.loads((root_a / "config.json").read_text(encoding="utf-8"))
    assert on_disk == {"agents": {"old": True}}  # untouched, not truncated


# ---------------------------------------------------------------------------
# (d) Partial failure reported per file, except registration is atomic
# per registration.
# ---------------------------------------------------------------------------


def test_apply_reports_applied_and_not_applied_lists_on_partial_failure(
    bundle_repo: Path,
    target_root: Path,
    state_store: state.StateStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "steering").mkdir()
    (commit_root / "steering" / "a.md").write_text("# A\n", encoding="utf-8")
    (commit_root / "config.json").write_text(
        json.dumps({"agents": {}}), encoding="utf-8"
    )

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    real_replace = os.replace
    target_config = Path(os.environ["KIROCREW_HOME"]) / "config.json"

    def _selective_failure(
        src: "str | os.PathLike[str]", dst: "str | os.PathLike[str]"
    ) -> None:
        if str(dst) == str(target_config):
            raise OSError("simulated failure for config.json only")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _selective_failure)
    try:
        result = apply.apply_commit(
            approved_sha=sha,
            commit_root=commit_root,
            changed_paths={"A": ["steering/a.md", "config.json"], "B": []},
            store=state_store,
        )
    finally:
        monkeypatch.setattr(os, "replace", real_replace)

    assert result.outcome == "partial"
    assert "steering/a.md" in result.applied
    assert "config.json" in result.not_applied
    assert result.outcome != "applied"  # never reported as success (4.8)


def test_apply_never_reports_success_outcome_when_anything_failed(
    bundle_repo: Path,
    target_root: Path,
    state_store: state.StateStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requirements.md 4.8: a part-way failure must never be reported as

    successful. Even a single failing file among many must flip the
    overall outcome away from "applied".
    """
    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "config.json").write_text(
        json.dumps({"agents": {}}), encoding="utf-8"
    )

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    def _always_fail(src: object, dst: object) -> None:
        raise OSError("simulated total failure")

    monkeypatch.setattr(os, "replace", _always_fail)
    try:
        result = apply.apply_commit(
            approved_sha=sha,
            commit_root=commit_root,
            changed_paths={"A": ["config.json"], "B": []},
            store=state_store,
        )
    finally:
        monkeypatch.setattr(os, "replace", os.replace)

    assert result.outcome != "applied"
    assert "config.json" not in result.applied


def test_apply_incomplete_registration_refused_but_unrelated_files_still_apply(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """design.md's error table: "Incomplete agent registration (fewer than

    4 parts) | That registration refused and reported; other files still
    apply" — a registration missing the model-state pin must be refused
    as one atomic unit while an unrelated steering file in the SAME
    commit still applies.
    """
    commit_root = tmp_path / "commit-root"
    (commit_root / "agent-prompts").mkdir(parents=True)
    (commit_root / "agent-prompts" / "new-agent.md").write_text(
        "# new-agent\n", encoding="utf-8"
    )
    (commit_root / "agents").mkdir()
    (commit_root / "agents" / "new-agent.json").write_text(
        json.dumps({"name": "new-agent"}), encoding="utf-8"
    )
    (commit_root / "config.json").write_text(
        json.dumps({"agents": {"new-agent": {"model": "claude-sonnet-5"}}}),
        encoding="utf-8",
    )
    # Deliberately omit agent_model_state.json entirely -> incomplete.
    (commit_root / "steering").mkdir()
    (commit_root / "steering" / "unrelated.md").write_text(
        "# Unrelated\n", encoding="utf-8"
    )

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={
            "A": [
                "agent-prompts/new-agent.md",
                "config.json",
                "steering/unrelated.md",
            ],
            "B": ["agents/new-agent.json"],
        },
        store=state_store,
    )

    assert "new-agent" in result.incomplete_registrations
    assert "agent_model_state.json pin" in " ".join(
        result.incomplete_registrations["new-agent"]
    )
    root_b = Path(os.environ["KIRO_HOME"])
    assert not (root_b / "agents" / "new-agent.json").exists()
    root_a = Path(os.environ["KIROCREW_HOME"])
    assert (root_a / "steering" / "unrelated.md").exists()
    assert "steering/unrelated.md" in result.applied


# ---------------------------------------------------------------------------
# (e) crons.json / instances.json go through sanitize.
# ---------------------------------------------------------------------------


def test_apply_sanitizes_crons_json_dropping_and_pausing_per_sanitize_module(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "crons.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {"name": "safe-message-job", "message": "hello"},
                    {"name": "unsafe-cmd-job", "command": "rm -rf /"},
                    {"name": "ok-cmd-job", "command": "echo hi"},
                ]
            }
        ),
        encoding="utf-8",
    )

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    def _fake_vet(command: str) -> str | None:
        return "Error: refused" if "rm -rf" in command else None

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["crons.json"], "B": []},
        store=state_store,
        cron_vet=_fake_vet,
    )

    assert "unsafe-cmd-job" in result.dropped_cron_names
    assert "ok-cmd-job" in result.paused_cron_names
    assert "safe-message-job" not in result.paused_cron_names
    assert "safe-message-job" not in result.dropped_cron_names

    root_a = Path(os.environ["KIROCREW_HOME"])
    written = json.loads((root_a / "crons.json").read_text(encoding="utf-8"))
    names = {job["name"] for job in written["jobs"]}
    assert names == {"safe-message-job", "ok-cmd-job"}
    ok_job = next(j for j in written["jobs"] if j["name"] == "ok-cmd-job")
    assert ok_job["user_paused"] is True
    assert ok_job["enabled"] is False


def test_apply_sanitizes_instances_json_forcing_disconnected(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "instances.json").write_text(
        json.dumps({"instances": [{"name": "other-host", "was_connected": True}]}),
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

    assert "other-host" in result.changed_instance_names
    root_a = Path(os.environ["KIROCREW_HOME"])
    written = json.loads((root_a / "instances.json").read_text(encoding="utf-8"))
    assert written["instances"][0]["was_connected"] is False


# ---------------------------------------------------------------------------
# (f) Deleted files (Req 4.7): changed-file list distinguishes D/A/M/R via
# `git diff --name-status` / `log --name-status`; a deleted upstream file
# is removed locally after backup, never re-written.
# ---------------------------------------------------------------------------


def test_deleted_upstream_file_is_removed_locally_never_rewritten(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """The bundle repo's own real history deletes ``steering/a.md`` on the

    feature branch (see `_init_bundle_repo`). Compute the real
    name-status for that range via `git diff --name-status` (never
    `--name-only`, which cannot distinguish D from M/A) and confirm
    apply.py, given that a path is status ``D``, removes the local file
    after backup rather than attempting to write (non-existent) new
    content over it.

    ``_init_bundle_repo`` merges ``feature`` INTO ``main`` via
    ``git checkout main && git merge --no-ff feature``, so on the
    resulting merge commit the FIRST parent (``main^1``) is main's
    pre-merge tip (the ``initial`` commit) and the SECOND parent
    (``main^2``) is the feature branch's own tip (the ``rename b``
    commit) — the reverse of a merge performed the other way around.
    ``main^2`` is therefore the ref whose own ancestry actually contains
    the add/modify/delete/rename commits this fixture built, and
    ``main^2~3`` is the ``initial`` commit relative to that lineage —
    never ``main~3``, which does not exist off the two-parent merge tip
    at all.
    """
    name_status = _git(
        "diff", "--name-status", "main^2~3", "main^2", cwd=bundle_repo
    ).stdout
    statuses: Dict[str, str] = {}
    for line in name_status.strip().splitlines():
        parts = line.split("\t")
        statuses[parts[-1]] = parts[0][0]
    assert statuses.get("steering/a.md") == "D"

    root_a = Path(os.environ["KIROCREW_HOME"])
    (root_a / "steering").mkdir()
    (root_a / "steering" / "a.md").write_text("# A changed\n", encoding="utf-8")

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()  # deleted path has no content in the new tree

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["steering/a.md"], "B": []},
        store=state_store,
        deleted_paths={"A": ["steering/a.md"], "B": []},
    )

    assert not (root_a / "steering" / "a.md").exists()
    assert "steering/a.md" in result.applied


def test_added_and_modified_and_renamed_paths_use_real_name_status(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """Confirm the real bundle fixture's rename (b.md -> renamed-b.md) is

    reported as status R by real git, distinct from A/M/D — apply.py must
    be given (or itself derive) a changed-path list built from
    `--name-status`, not `--name-only`, so a rename is not misread as an
    add-plus-delete pair with no continuity. This test only pins the git
    fact the implementation must rely on; apply.py's own name-status
    parsing is exercised implicitly by every other test in this file that
    passes `deleted_paths` explicitly.

    Uses ``main^2`` (the feature branch's own tip — see the docstring on
    ``test_deleted_upstream_file_is_removed_locally_never_rewritten`` for
    why the merge's first vs. second parent is reversed here), so
    ``main^2~2`` reaches the "modify a, add b" commit, immediately before
    the rename.
    """
    name_status = _git(
        "diff", "--name-status", "-M", "main^2~2", "main^2", cwd=bundle_repo
    ).stdout
    kinds = {
        line.split("\t")[-1]: line.split("\t")[0][0]
        for line in name_status.strip().splitlines()
    }
    assert any(k == "R" for k in kinds.values())


# ---------------------------------------------------------------------------
# (g) apply.py supplies propagate.build_report with ChangeKind and
# frontmatter_changed per file.
# ---------------------------------------------------------------------------


def test_apply_result_carries_a_propagation_entry_per_applied_file(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "steering").mkdir()
    (commit_root / "steering" / "a.md").write_text("# A\n", encoding="utf-8")

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["steering/a.md"], "B": []},
        store=state_store,
    )

    assert "steering/a.md" in result.propagation.entries
    entry = result.propagation.entries["steering/a.md"]
    assert entry.message  # a non-empty, honest per-file message


def test_apply_reports_skill_md_frontmatter_change_as_live_within_60s(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """A `SKILL.md` whose frontmatter TRIGGERS changed must be reported

    `LIVE_WITHIN_60S`, not the body-only `LIVE_IMMEDIATE` baseline — this
    is exactly `propagate.build_report`'s `frontmatter_changed` refinement
    (requirements.md 5.4), so apply.py must actually detect and pass that
    flag rather than always defaulting it False.
    """
    from backend.allowlist import PropagationClass

    commit_root = tmp_path / "commit-root"
    (commit_root / "skills" / "example").mkdir(parents=True)
    old_skill = '---\ntriggers: ["old-trigger"]\n---\n# Example\nBody.\n'
    new_skill = '---\ntriggers: ["new-trigger"]\n---\n# Example\nBody.\n'
    (commit_root / "skills" / "example" / "SKILL.md").write_text(
        new_skill, encoding="utf-8"
    )

    root_a = Path(os.environ["KIROCREW_HOME"])
    (root_a / "skills" / "example").mkdir(parents=True)
    (root_a / "skills" / "example" / "SKILL.md").write_text(old_skill, encoding="utf-8")

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["skills/example/SKILL.md"], "B": []},
        store=state_store,
    )

    entry = result.propagation.entries["skills/example/SKILL.md"]
    assert entry.propagation_class == PropagationClass.LIVE_WITHIN_60S


# ---------------------------------------------------------------------------
# (h) Redaction boundary — requirements.md 4.10 / design.md apply step 4.
# For every placeholder, restore the LIVE value at the same key path; where
# no live value exists, keep the placeholder and list it in
# `needs_credential`. Non-placeholder commit values always win.
# ---------------------------------------------------------------------------


def test_apply_restores_live_headers_value_over_pulled_placeholder_in_mcp_json(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """MUTATION NOTE: the real narrowing dimension is "the WRITTEN value at

    this key path equals the LIVE value that was on disk before apply,
    never the commit's own placeholder". A mutation that skips the
    restore step entirely (writes the commit's `mcp.json` bytes through
    verbatim) would leave `Authorization` as the literal string
    `"<redacted>"` instead of `"Bearer REALTOKEN"` — the assertion below
    is exactly what such a mutation falsifies.
    """
    root_a = Path(os.environ["KIROCREW_HOME"])
    root_a.joinpath("mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "github": {"headers": {"Authorization": "Bearer REALTOKEN"}}
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"github": {"headers": {"Authorization": "<redacted>"}}}},
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
    )

    written = json.loads(root_a.joinpath("mcp.json").read_text(encoding="utf-8"))
    assert written["mcpServers"]["github"]["headers"]["Authorization"] == (
        "Bearer REALTOKEN"
    )
    assert result.needs_credential == []


def test_apply_restores_live_env_value_in_config_json_and_crons_json(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    root_a = Path(os.environ["KIROCREW_HOME"])
    root_a.joinpath("config.json").write_text(
        json.dumps({"agents": {}, "env": {"API_KEY": "live-config-secret"}}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    root_a.joinpath("crons.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "name": "j1",
                        "command": "echo hi",
                        "env": {"TOKEN": "live-cron-secret"},
                    }
                ]
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "config.json").write_text(
        json.dumps({"agents": {}, "env": {"API_KEY": "<redacted>"}}, indent=2) + "\n",
        encoding="utf-8",
    )
    (commit_root / "crons.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {"name": "j1", "command": "echo hi", "env": {"TOKEN": "<redacted>"}}
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
        changed_paths={"A": ["config.json", "crons.json"], "B": []},
        store=state_store,
    )

    config_written = json.loads(
        root_a.joinpath("config.json").read_text(encoding="utf-8")
    )
    assert config_written["env"]["API_KEY"] == "live-config-secret"
    crons_written = json.loads(
        root_a.joinpath("crons.json").read_text(encoding="utf-8")
    )
    assert crons_written["jobs"][0]["env"]["TOKEN"] == "live-cron-secret"
    assert result.needs_credential == []


def test_apply_writes_placeholder_and_lists_needs_credential_for_new_server(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """A newly added server has no live value at that key path at all — the

    placeholder IS written (there is nothing to restore) and the key path
    is listed in `needs_credential` by server name and key, per
    design.md apply step 4a / requirements.md 4.10's second sentence.
    """
    root_a = Path(os.environ["KIROCREW_HOME"])
    root_a.joinpath("mcp.json").write_text(
        json.dumps({"mcpServers": {}}, indent=2) + "\n", encoding="utf-8"
    )

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "new-server": {"headers": {"Authorization": "<redacted>"}}
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
    )

    written = json.loads(root_a.joinpath("mcp.json").read_text(encoding="utf-8"))
    assert written["mcpServers"]["new-server"]["headers"]["Authorization"] == (
        "<redacted>"
    )
    assert any(
        "new-server" in entry and "Authorization" in entry
        for entry in result.needs_credential
    )


def test_apply_restores_current_live_value_not_the_prior_commits_value(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """Proves restoration reads the CURRENT live file, not some stale value

    remembered from an earlier push/commit — the live value here was
    rotated locally to something the ORIGINAL commit never had.
    """
    root_a = Path(os.environ["KIROCREW_HOME"])
    root_a.joinpath("mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "github": {"headers": {"Authorization": "Bearer ROTATED-LOCALLY"}}
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"github": {"headers": {"Authorization": "<redacted>"}}}},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["mcp.json"], "B": []},
        store=state_store,
    )

    written = json.loads(root_a.joinpath("mcp.json").read_text(encoding="utf-8"))
    assert written["mcpServers"]["github"]["headers"]["Authorization"] == (
        "Bearer ROTATED-LOCALLY"
    )


def test_apply_commit_value_wins_when_it_is_not_the_placeholder(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """A non-placeholder value in the commit must apply UNCHANGED even when

    it differs from the live value — restoration only ever fires on an
    EXACT match against the placeholder, never as a general "prefer live"
    rule.
    """
    root_a = Path(os.environ["KIROCREW_HOME"])
    root_a.joinpath("mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"github": {"headers": {"Authorization": "Bearer OLD"}}}},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "github": {"headers": {"Authorization": "Bearer FROM-COMMIT"}}
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["mcp.json"], "B": []},
        store=state_store,
    )

    written = json.loads(root_a.joinpath("mcp.json").read_text(encoding="utf-8"))
    assert written["mcpServers"]["github"]["headers"]["Authorization"] == (
        "Bearer FROM-COMMIT"
    )


def test_apply_does_not_treat_a_value_merely_containing_redacted_as_the_placeholder(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """Exact-match only: a real value that happens to CONTAIN the substring

    "redacted" (but is not the literal placeholder string) must apply
    from the commit unchanged, never be mistaken for the sentinel and
    trigger a restore.
    """
    root_a = Path(os.environ["KIROCREW_HOME"])
    root_a.joinpath("mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"github": {"headers": {"Authorization": "Bearer LIVE"}}}},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    substring_value = "token-was-redacted-by-someone-else-not-the-sentinel"
    (commit_root / "mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"github": {"headers": {"Authorization": substring_value}}}},
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
    )

    written = json.loads(root_a.joinpath("mcp.json").read_text(encoding="utf-8"))
    assert written["mcpServers"]["github"]["headers"]["Authorization"] == (
        substring_value
    )
    assert result.needs_credential == []


def test_apply_restores_crons_json_env_value_from_live_vet_sees_committed_command(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """Ordering guard (design.md step 4 restore, THEN step 4a sanitize):

    a `crons.json` job's `env` value that is the placeholder
    `"<redacted>"` is restored from the live file (requirements.md 4.10
    is scoped to `headers`/`env` values only). The job's own `command`
    is untouched by restoration — it was never redacted by push in the
    first place — so the vet callable must see exactly the COMMITTED
    command unchanged, proving restore ran before sanitize's vet call
    without restoration reaching into a field it has no business
    touching.
    """
    root_a = Path(os.environ["KIROCREW_HOME"])
    root_a.joinpath("crons.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "name": "j1",
                        "command": "echo old-live-command",
                        "env": {"TOKEN": "live-cron-secret"},
                    }
                ]
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "crons.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "name": "j1",
                        "command": "echo new-committed-command",
                        "env": {"TOKEN": "<redacted>"},
                    }
                ]
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    seen_commands: list[str] = []

    def _recording_vet(command: str) -> str | None:
        seen_commands.append(command)
        return None

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["crons.json"], "B": []},
        store=state_store,
        cron_vet=_recording_vet,
    )

    assert seen_commands == ["echo new-committed-command"]
    written = json.loads(root_a.joinpath("crons.json").read_text(encoding="utf-8"))
    assert written["jobs"][0]["command"] == "echo new-committed-command"
    assert written["jobs"][0]["env"]["TOKEN"] == "live-cron-secret"
    assert result.needs_credential == []


def test_apply_never_restores_a_command_field_even_when_it_is_the_placeholder(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """Requirements.md 4.10 restores ONLY `headers`/`env` values — push

    never redacts a cron `command`, so a `command` field that happens to
    be the literal string `"<redacted>"` is NOT a placeholder restoration
    ever fires on. It applies exactly as committed, the vet sees that
    literal string (not a restored live value), and — surviving the vet
    — the job imports user-paused like any other command job. It must
    never appear in `needs_credential`, since `needs_credential` names
    only `headers`/`env` key paths the applier had no live value to
    restore.
    """
    root_a = Path(os.environ["KIROCREW_HOME"])
    root_a.joinpath("crons.json").write_text(
        json.dumps(
            {"jobs": [{"name": "j1", "command": "echo the-real-live-command"}]},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "crons.json").write_text(
        json.dumps({"jobs": [{"name": "j1", "command": "<redacted>"}]}, indent=2)
        + "\n",
        encoding="utf-8",
    )

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    seen_commands: list[str] = []

    def _recording_vet(command: str) -> str | None:
        seen_commands.append(command)
        return None

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["crons.json"], "B": []},
        store=state_store,
        cron_vet=_recording_vet,
    )

    assert seen_commands == ["<redacted>"]
    written = json.loads(root_a.joinpath("crons.json").read_text(encoding="utf-8"))
    written_job = written["jobs"][0]
    assert written_job["command"] == "<redacted>"
    assert written_job["user_paused"] is True
    assert written_job["enabled"] is False
    assert not any("j1" in entry for entry in result.needs_credential)


# ---------------------------------------------------------------------------
# Approval-SHA gate (design.md step 1 / requirements.md 4.4).
# ---------------------------------------------------------------------------


def test_apply_refuses_when_approved_sha_does_not_match_pending(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "config.json").write_text(
        json.dumps({"agents": {}}), encoding="utf-8"
    )
    _seed_pending(state_store, "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")

    result = apply.apply_commit(
        approved_sha="0000000000000000000000000000000000000000",
        commit_root=commit_root,
        changed_paths={"A": ["config.json"], "B": []},
        store=state_store,
    )

    assert result.outcome == "refused-sha-mismatch"
    assert result.applied == []
    root_a = Path(os.environ["KIROCREW_HOME"])
    assert not (root_a / "config.json").exists()


def test_apply_refuses_when_no_pending_record_exists_at_all(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "config.json").write_text(
        json.dumps({"agents": {}}), encoding="utf-8"
    )
    assert state_store.pending is None

    sha = _head_sha(bundle_repo)
    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["config.json"], "B": []},
        store=state_store,
    )

    assert result.outcome == "refused-sha-mismatch"


# ---------------------------------------------------------------------------
# Adversarial: path traversal.
# ---------------------------------------------------------------------------


def test_apply_refuses_a_path_traversal_relpath(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """MUTATION NOTE: the real narrowing dimension is "a relpath containing

    `..` components is refused/ignored, never resolved against the
    target root". To prove this can fail: a mutation that resolves the
    relpath naively (``root / relpath`` with no traversal check) would
    let ``steering/../../../escape`` (or the bare ``../escape`` used
    below) write outside `KIROCREW_HOME`; the assertion that the escape
    target file does NOT exist is exactly what such a mutation would
    falsify — that path would exist afterward if the guard were removed.
    """
    root_a = Path(os.environ["KIROCREW_HOME"])
    escape_target = root_a.parent / "escape"
    assert not escape_target.exists()

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    # A same-shaped payload the allowlist would (if traversal were not
    # blocked first) otherwise treat as a `steering/**/*.md` hit.
    (commit_root / "escape.md").write_text("pwned", encoding="utf-8")

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["../escape.md", "steering/../../escape.md"], "B": []},
        store=state_store,
    )

    assert not escape_target.exists()
    assert not (root_a.parent / "escape.md").exists()
    assert "../escape.md" in result.ignored_paths or "../escape.md" in (
        result.not_applied
    )
    assert "steering/../../escape.md" in result.ignored_paths or (
        "steering/../../escape.md" in result.not_applied
    )


def test_apply_refuses_a_deeply_nested_traversal_relpath(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    root_a = Path(os.environ["KIROCREW_HOME"])
    outside_marker = tmp_path / "outside-marker.md"

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "steering").mkdir()
    nested = commit_root / "steering" / "sub" / "deep"
    nested.mkdir(parents=True)
    (nested / "x.md").write_text("pwned", encoding="utf-8")

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    traversal_path = "steering/sub/deep/../../../../../outside-marker.md"
    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": [traversal_path], "B": []},
        store=state_store,
    )

    assert not outside_marker.exists()
    assert traversal_path in result.ignored_paths or traversal_path in (
        result.not_applied
    )
    assert root_a.exists()  # target root itself untouched/still present


# ---------------------------------------------------------------------------
# Adversarial: a symlink in the commit pointing outside the root.
# ---------------------------------------------------------------------------


def test_apply_refuses_a_symlink_in_commit_pointing_outside_root(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """A `commit_root` entry that is itself a SYMLINK pointing outside the

    commit tree (e.g. at `/etc/passwd` or another part of the host) must
    never be followed and copied through — apply.py must detect the
    checked-out entry is a symlink (not a regular file) and refuse it,
    the same fail-closed posture `git_safety.py` takes for `.git`
    components.
    """
    outside_secret = tmp_path / "outside-secret.txt"
    outside_secret.write_text("host secret, must not leak", encoding="utf-8")

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "config.json").symlink_to(outside_secret)

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["config.json"], "B": []},
        store=state_store,
    )

    root_a = Path(os.environ["KIROCREW_HOME"])
    written_path = root_a / "config.json"
    if written_path.exists():
        assert written_path.read_text(encoding="utf-8") != "host secret, must not leak"
    assert "config.json" not in result.applied


def test_apply_refuses_a_symlinked_directory_in_commit(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    (outside_dir / "leaked.md").write_text("leaked", encoding="utf-8")

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "steering").symlink_to(outside_dir)

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["steering/leaked.md"], "B": []},
        store=state_store,
    )

    root_a = Path(os.environ["KIROCREW_HOME"])
    assert not (root_a / "steering" / "leaked.md").exists()
    assert "steering/leaked.md" not in result.applied


# ---------------------------------------------------------------------------
# base_sha / pending ownership (tasks.md 6.1): apply.py must NEVER call
# store.clear_pending() or store.advance_base_sha() — those belong solely
# to routes.py's approve/decline handlers via a single resolve_pending()
# that moves both together. apply.py only reads pending/base_sha for the
# approval-SHA gate and only writes restore_dirs.
# ---------------------------------------------------------------------------


def test_apply_success_leaves_pending_and_base_sha_untouched(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """A successful apply must NOT clear `pending` or advance `base_sha` —

    tasks.md 6.1 gives that transition to the approve/decline ROUTE's
    single `resolve_pending()`, never to apply.py itself. If apply.py
    called `store.clear_pending()`/`store.advance_base_sha()` directly, a
    route that called BOTH `apply.apply_commit` and its own
    `resolve_pending()` would double-advance/no-op harmlessly here, but a
    route that (per design) calls `resolve_pending()` separately depends
    on apply.py having left the record alone — this test pins that
    apply.py's own responsibility ends at reading, not mutating, this
    state.
    """
    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "config.json").write_text(
        json.dumps({"agents": {}}), encoding="utf-8"
    )

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)
    base_before = state_store.base_sha

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["config.json"], "B": []},
        store=state_store,
    )

    assert result.outcome == "applied"
    assert state_store.pending is not None
    assert state_store.pending["sha"] == sha
    assert state_store.base_sha == base_before


def test_apply_refused_sha_mismatch_does_not_touch_pending_or_base_sha(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    pending_sha = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    _seed_pending(state_store, pending_sha)
    base_before = state_store.base_sha

    apply.apply_commit(
        approved_sha="0000000000000000000000000000000000000000",
        commit_root=commit_root,
        changed_paths={"A": [], "B": []},
        store=state_store,
    )

    assert state_store.pending is not None
    assert state_store.pending["sha"] == pending_sha
    assert state_store.base_sha == base_before


def test_apply_never_calls_store_clear_pending_or_advance_base_sha(
    bundle_repo: Path,
    target_root: Path,
    state_store: state.StateStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct ownership guard: instrument `state.StateStore.clear_pending`

    and `advance_base_sha` to raise if called at all, and confirm a normal
    successful apply completes without tripping either — the strongest
    available confirmation, short of reading apply.py's own source, that
    it never reaches for either mutation.
    """

    def _forbidden(*_a: object, **_k: object) -> None:
        raise AssertionError(
            "apply.py must not call clear_pending/advance_base_sha "
            "directly — that belongs to routes.py's resolve_pending()"
        )

    monkeypatch.setattr(state.StateStore, "clear_pending", _forbidden)
    monkeypatch.setattr(state.StateStore, "advance_base_sha", _forbidden)

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "config.json").write_text(
        json.dumps({"agents": {}}), encoding="utf-8"
    )

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["config.json"], "B": []},
        store=state_store,
    )

    assert result.outcome == "applied"


# ---------------------------------------------------------------------------
# Empty / absent-on-disk edge cases.
# ---------------------------------------------------------------------------


def test_apply_with_no_changed_paths_is_a_clean_no_op_success(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": [], "B": []},
        store=state_store,
    )

    assert result.outcome == "applied"
    assert result.applied == []
    assert result.not_applied == {}


def test_apply_deleting_a_path_absent_on_disk_locally_is_not_an_error(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """An allowlisted path that never existed locally (e.g. `crons.json`

    on an instance with no jobs, per requirements.md 1.5's absence rule)
    being deleted upstream must not raise — there is nothing to back up
    or remove.
    """
    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["crons.json"], "B": []},
        store=state_store,
        deleted_paths={"A": ["crons.json"], "B": []},
    )

    assert "crons.json" in result.applied
    root_a = Path(os.environ["KIROCREW_HOME"])
    assert not (root_a / "crons.json").exists()


# ---------------------------------------------------------------------------
# Error-path tests (requirements.md 4.7, 4.8). These characterize behaviour
# the implementer's coverage-driven cleanup pass deleted along with the
# error handling it existed to exercise — testing-standards.md: coverage
# must come from tests, not from removing the behaviour a requirement
# names. Several of these are EXPECTED TO FAIL against the current
# backend/apply.py; see this task's final report for which and why.
# ---------------------------------------------------------------------------


def test_apply_unlink_oserror_on_delete_reports_not_applied_partial_and_continues(
    bundle_repo: Path,
    target_root: Path,
    state_store: state.StateStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requirements.md 4.8: a delete that fails part-way must be reported,

    not silently swallowed or allowed to abort the whole apply.

    MUTATION NOTE: the real narrowing dimension is "an `OSError` raised
    by `Path.unlink` on the specific file being deleted lands that ONE
    relpath in `not_applied`, flips `outcome` away from `applied`, and
    does not stop a LATER file in the same commit from still applying".
    A mutation that reverts this handling to the current code (an
    unguarded `live_target.unlink()` with no `try`/`except`) makes this
    test fail with an uncaught `OSError` propagating out of
    `apply_commit` instead of being captured in the result — this is the
    deliberately-violating-input half of the mutation requirement: the
    narrowest change (removing the guard) flips this from a clean
    pass/fail assertion to an unhandled exception, which pytest reports
    as an error, not a silent pass.
    """
    root_a = Path(os.environ["KIROCREW_HOME"])
    (root_a / "steering").mkdir()
    (root_a / "steering" / "gone.md").write_text("# Gone\n", encoding="utf-8")
    (root_a / "config.json").write_text(
        json.dumps({"agents": {"old": True}}), encoding="utf-8"
    )

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "config.json").write_text(
        json.dumps({"agents": {"new": True}}), encoding="utf-8"
    )

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    real_unlink = Path.unlink
    failing_target = root_a / "steering" / "gone.md"

    def _failing_unlink(self: Path, missing_ok: bool = False) -> None:
        if self == failing_target:
            raise OSError("simulated unlink failure")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _failing_unlink)
    try:
        result = apply.apply_commit(
            approved_sha=sha,
            commit_root=commit_root,
            changed_paths={"A": ["steering/gone.md", "config.json"], "B": []},
            store=state_store,
            deleted_paths={"A": ["steering/gone.md"], "B": []},
        )
    finally:
        monkeypatch.setattr(Path, "unlink", real_unlink)

    assert "steering/gone.md" in result.not_applied
    assert result.outcome == "partial"
    assert result.outcome != "applied"
    # The later file in the same commit still applied.
    assert "config.json" in result.applied
    assert json.loads((root_a / "config.json").read_text(encoding="utf-8")) == {
        "agents": {"new": True}
    }


def test_apply_source_read_bytes_oserror_reports_not_applied_others_still_apply(
    bundle_repo: Path,
    target_root: Path,
    state_store: state.StateStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A commit-side source file that raises `OSError` on `read_bytes`

    (e.g. a permissions error, a race with something removing the
    checked-out file) must land that path in `not_applied` without
    raising, and must not prevent another file in the same commit from
    applying.

    MUTATION NOTE: the real narrowing dimension is "a read failure on
    ONE source file is caught and reported, never propagated". The
    current implementation calls ``source.read_bytes()`` with no
    surrounding `try`/`except` — a mutation that reverts this handling
    to that shape makes this test fail with an uncaught `OSError`
    instead of a clean `not_applied` entry.
    """
    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "steering").mkdir()
    (commit_root / "steering" / "a.md").write_text("# A\n", encoding="utf-8")
    (commit_root / "config.json").write_text(
        json.dumps({"agents": {}}), encoding="utf-8"
    )

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    real_read_bytes = Path.read_bytes
    failing_source = commit_root / "steering" / "a.md"

    def _failing_read_bytes(self: Path, *a: object, **k: object) -> bytes:
        if self == failing_source:
            raise OSError("simulated read failure")
        return real_read_bytes(self, *a, **k)

    monkeypatch.setattr(Path, "read_bytes", _failing_read_bytes)
    try:
        result = apply.apply_commit(
            approved_sha=sha,
            commit_root=commit_root,
            changed_paths={"A": ["steering/a.md", "config.json"], "B": []},
            store=state_store,
        )
    finally:
        monkeypatch.setattr(Path, "read_bytes", real_read_bytes)

    assert "steering/a.md" in result.not_applied
    assert result.outcome != "applied"
    assert "config.json" in result.applied
    root_a = Path(os.environ["KIROCREW_HOME"])
    assert (root_a / "config.json").exists()


def test_apply_record_restore_dir_failure_is_not_silently_swallowed(
    bundle_repo: Path,
    target_root: Path,
    state_store: state.StateStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If `store.record_restore_dir` raises, the restore route has no

    recorded restore directory for this apply, so this apply's changes
    cannot be undone. Requirements.md 4.7 exists precisely so a restore
    is always possible after an apply — an apply that silently proceeds
    to report `outcome="applied"` here would claim a success the operator
    cannot actually roll back.

    OBSERVABLE ASSERTION (narrowest one available): `outcome` must NOT be
    `"applied"` when the restore-dir bookkeeping failed on a commit that
    genuinely backed up a file, and `result.reason` (or an equivalent
    explicit signal) must be non-empty, naming the failure. This does not
    assert anything about whether the live file was written or rolled
    back — only that the result cannot claim an unqualified success
    while leaving the restore path unrecorded.

    MUTATION NOTE: the current implementation wraps this call in a bare
    ``try: ... except OSError: pass`` that discards the failure
    unconditionally and still returns `outcome="applied"`. Against that
    code, this test is EXPECTED TO FAIL — that is exactly the defect this
    test exists to surface, per this task's instruction to report which
    of the added tests fail against current `apply.py` and why.
    """
    root_a = Path(os.environ["KIROCREW_HOME"])
    (root_a / "config.json").write_text(
        json.dumps({"agents": {"old": True}}), encoding="utf-8"
    )

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "config.json").write_text(
        json.dumps({"agents": {"new": True}}), encoding="utf-8"
    )

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    def _failing_record_restore_dir(*a: object, **k: object) -> None:
        raise OSError("simulated restore-dir bookkeeping failure")

    monkeypatch.setattr(
        state.StateStore, "record_restore_dir", _failing_record_restore_dir
    )

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["config.json"], "B": []},
        store=state_store,
    )

    assert result.outcome != "applied"
    assert result.reason != ""


def test_apply_same_relpath_changed_under_root_a_and_root_b_gets_two_backups(
    bundle_repo: Path,
    target_root: Path,
    state_store: state.StateStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two roots can both carry a file at the identical relpath — nothing

    in `apply.py`'s backup step (`_backup_file(live_path, restore_dir,
    relpath)`) keys the restore destination by ROOT, only by `relpath`
    (`restore_dir / relpath`), so a commit changing the same relpath
    under both root A and root B in one apply must still produce two
    DISTINCT, independently byte-for-byte restorable backups.

    SPEC GAP: `backend/allowlist.py`'s root A and root B entries are
    disjoint by construction (root A has no `agents/**` entry; root B's
    only entry is `agents/*.json`), so no single relpath is ever tracked
    by BOTH roots' allowlists today, and this collision cannot be driven
    through the public `changed_paths` argument alone. This test reaches
    the collision the way a future allowlist change could still produce
    it — by patching `allowlist.is_tracked` to admit the same relpath
    under both roots for this test only — because the property under
    test is about `apply_commit`'s OWN backup-path construction, not
    about what the allowlist happens to admit today; the allowlist being
    disjoint is a fact about configuration, not a guarantee `apply.py`'s
    backup step relies on.

    MUTATION NOTE: the current `_backup_file` call site passes only
    `relpath` (no root) into `restore_dir / relpath`, so backing up root
    A's and root B's file at the same relpath in one apply writes the
    SECOND backup over the FIRST at an identical destination path.
    Against that code, this test is EXPECTED TO FAIL: the assertion that
    both prior contents are independently recoverable finds one root's
    backup overwritten by the other's bytes.
    """
    real_is_tracked = allowlist.is_tracked

    def _both_roots_track_shared(root: str, relpath: str) -> bool:
        if relpath == "hooks.json":
            return True
        return real_is_tracked(root, relpath)

    monkeypatch.setattr(allowlist, "is_tracked", _both_roots_track_shared)

    root_a = Path(os.environ["KIROCREW_HOME"])
    root_b = Path(os.environ["KIRO_HOME"])
    # Root A and root B both already carry a live file at the identical
    # relpath "hooks.json" — distinct prior bytes on each side. "hooks.json"
    # (rather than an `agents/*.json`-shaped name) deliberately avoids
    # `registration.check_registrations`'s agent-definition path pattern,
    # which would otherwise treat this collision as an unrelated
    # incomplete-registration case and block it before backup ever runs.
    (root_a / "hooks.json").write_text(
        json.dumps({"side": "A", "value": "root-a-prior"}), encoding="utf-8"
    )
    (root_b / "hooks.json").write_text(
        json.dumps({"side": "B", "value": "root-b-prior"}), encoding="utf-8"
    )

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "hooks.json").write_text(
        json.dumps({"side": "either", "value": "new-from-commit"}), encoding="utf-8"
    )

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["hooks.json"], "B": ["hooks.json"]},
        store=state_store,
    )

    assert result.apply_id is not None
    restore_dir = Path(state_store.restore_dirs[result.apply_id])
    root_a_backup = restore_dir / "A" / "hooks.json"
    root_b_backup = restore_dir / "B" / "hooks.json"
    assert root_a_backup.is_file()
    assert root_b_backup.is_file()
    assert json.loads(root_a_backup.read_text(encoding="utf-8")) == {
        "side": "A",
        "value": "root-a-prior",
    }
    assert json.loads(root_b_backup.read_text(encoding="utf-8")) == {
        "side": "B",
        "value": "root-b-prior",
    }


def test_apply_successful_root_b_agent_definition_lands_at_kiro_home(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """A COMPLETE registration (all four parts present in the commit) must

    actually write its root-B part -- `agents/<name>.json` -- under
    `KIRO_HOME`. No existing test in this file exercised the SUCCESS path
    for a registration at all; every prior registration test only covers
    the refusal branch (`test_apply_incomplete_registration_refused_but_
    unrelated_files_still_apply` omits `agent_model_state.json` on
    purpose), so a defect in the success write-through -- wrong root,
    wrong relpath, or the write simply never reached -- would pass the
    whole suite today.

    SUPERSEDED-BY-7.5 NOTE (this test was rewritten, not left as-is): the
    prior version built the prompt part at the bare relpath
    `agent-prompts/<name>.md` and asserted the registration was
    permanently incomplete because that path has no root-A allowlist
    entry -- true of the old rule, where "a same-named sibling file
    under `agent-prompts/`" was treated as the prompt part regardless of
    what the agent's own JSON said. Requirements.md 5.11(b) retires
    that: an inline `prompt` string (or an absent/`null`/empty one)
    satisfies the prompt part with NO required file at all, so a
    registration can reach COMPLETE without depending on the
    `agent-prompts/**` allowlist gap. This rewrite uses an inline
    `prompt` for exactly that reason, and is what finally closes the "no
    success-path test exists" gap the prior version could only name, not
    close.
    """
    commit_root = tmp_path / "commit-root"
    (commit_root / "agents").mkdir(parents=True)
    (commit_root / "agents" / "new-agent.json").write_text(
        json.dumps(
            {"name": "new-agent", "prompt": "You are new-agent, a helpful agent."}
        ),
        encoding="utf-8",
    )
    (commit_root / "config.json").write_text(
        json.dumps({"agents": {"new-agent": {"model": "claude-sonnet-5"}}}),
        encoding="utf-8",
    )
    (commit_root / "agent_model_state.json").write_text(
        json.dumps({"new-agent": {"model_managed": False}}), encoding="utf-8"
    )

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={
            "A": ["config.json", "agent_model_state.json"],
            "B": ["agents/new-agent.json"],
        },
        store=state_store,
    )

    # requirements.md 5.11(b): an inline prompt string satisfies the
    # prompt part with no required file, so the three remaining parts
    # (agent definition, config.json entry, model-state pin) being
    # present makes this registration COMPLETE.
    assert "new-agent" not in result.incomplete_registrations
    root_b = Path(os.environ["KIRO_HOME"])
    written = root_b / "agents" / "new-agent.json"
    assert written.is_file(), (
        "a complete registration's root-B agent definition was never "
        "written through to KIRO_HOME"
    )
    assert json.loads(written.read_text(encoding="utf-8")) == json.loads(
        (commit_root / "agents" / "new-agent.json").read_text(encoding="utf-8")
    )
    assert "agents/new-agent.json" in result.applied


@pytest.mark.parametrize(
    "traversal_relpath",
    ["/etc/x", ""],
    ids=["absolute-relpath", "empty-relpath"],
)
def test_apply_refuses_absolute_and_empty_relpaths_never_written(
    bundle_repo: Path,
    target_root: Path,
    state_store: state.StateStore,
    tmp_path: Path,
    traversal_relpath: str,
) -> None:
    """An absolute relpath (`/etc/x`) or an empty relpath must be refused

    and reported — never resolved against the target root or written.
    Distinct from the `..`-component traversal tests above: neither of
    these strings contains a `..` segment, so they exercise the OTHER
    two conditions `_is_safe_relpath` checks (no leading `/`, non-empty).
    """
    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "x").write_text("pwned", encoding="utf-8")

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": [traversal_relpath], "B": []},
        store=state_store,
    )

    assert traversal_relpath not in result.applied
    assert not Path("/etc/x").exists() or True  # never assert on real /etc
    assert traversal_relpath in result.ignored_paths or traversal_relpath in (
        result.not_applied
    )


def test_apply_malformed_skill_md_frontmatter_still_applies_reported_conservative(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """A `SKILL.md` with malformed frontmatter (an unterminated `---`

    block, or non-UTF-8 bytes) must still apply — apply.py never refuses
    a file for having unparsable frontmatter — and propagation must
    report the conservative `LIVE_WITHIN_60S` classification rather than
    the immediate-body-edit baseline, since the frontmatter's own change
    cannot be confirmed either way when it fails to parse. Requirements.md
    is silent on this exact edge case (a malformed, not merely absent,
    frontmatter block); this test pins the CONSERVATIVE reading of
    design.md's existing `LIVE_WITHIN_60S` fallback as the one that
    cannot understate propagation risk to the operator.
    """
    from backend.allowlist import PropagationClass

    commit_root = tmp_path / "commit-root"
    (commit_root / "skills" / "example").mkdir(parents=True)
    # Unterminated frontmatter: only one `---` delimiter, never closed.
    malformed = '---\ntriggers: ["broken\n# Example\nBody.\n'
    (commit_root / "skills" / "example" / "SKILL.md").write_bytes(
        malformed.encode("utf-8")
    )

    root_a = Path(os.environ["KIROCREW_HOME"])
    (root_a / "skills" / "example").mkdir(parents=True)
    old_skill = '---\ntriggers: ["old-trigger"]\n---\n# Example\nBody.\n'
    (root_a / "skills" / "example" / "SKILL.md").write_text(old_skill, encoding="utf-8")

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["skills/example/SKILL.md"], "B": []},
        store=state_store,
    )

    assert "skills/example/SKILL.md" in result.applied
    written = (root_a / "skills" / "example" / "SKILL.md").read_bytes()
    assert written == malformed.encode("utf-8")
    entry = result.propagation.entries["skills/example/SKILL.md"]
    assert entry.propagation_class == PropagationClass.LIVE_WITHIN_60S


# ---------------------------------------------------------------------------
# Security gap 1: live-side symlink escape. `_resolve_target`'s containment
# check runs only at the END of the per-file loop, guarding the WRITE path
# (`target = _resolve_target(...)`). The backup step and the delete branch
# both act on the unresolved `live_target = root_path / relpath` earlier in
# the loop, before that check ever runs — a symlinked live-side directory
# component lets a delete or a backup reach outside the root.
# ---------------------------------------------------------------------------


def test_apply_delete_through_live_side_symlinked_dir_leaves_target_untouched(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """MUTATION NOTE: the containment check (`_resolve_target`) is applied

    only on the WRITE path, at the bottom of the per-file loop — never on
    the delete branch, which acts on `live_target = root_path / relpath`
    directly (`if live_target.exists(): live_target.unlink()`). Here
    `KIROCREW_HOME/steering` is itself a symlink to a directory OUTSIDE
    the root, so `live_target` resolves through it to `outside/gone.md` —
    a real path the root never contains. Against the current code this
    test is EXPECTED TO FAIL: `live_target.unlink()` deletes
    `outside/gone.md` for real, `outside/gone.md` no longer exists, and
    `steering/gone.md` is (wrongly) reported `applied`. The fix is to
    apply the SAME containment check `_resolve_target` uses to the
    delete/backup live_target, not only to the write target.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "gone.md").write_text("# Gone (outside root)\n", encoding="utf-8")
    (outside / "kept.md").write_text("# Kept (outside root)\n", encoding="utf-8")

    root_a = Path(os.environ["KIROCREW_HOME"])
    root_a.joinpath("steering").symlink_to(outside)

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()  # deleted path has no content in the new tree

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["steering/gone.md"], "B": []},
        store=state_store,
        deleted_paths={"A": ["steering/gone.md"], "B": []},
    )

    assert (outside / "gone.md").exists(), "delete must never reach outside the root"
    assert (outside / "gone.md").read_text(encoding="utf-8") == (
        "# Gone (outside root)\n"
    )
    assert "steering/gone.md" in result.not_applied
    assert "steering/gone.md" not in result.applied
    # No backup taken from outside the root: no apply_id's restore dir may
    # carry a copy of the outside file.
    for restore_dir_str in state_store.restore_dirs.values():
        backup_candidate = Path(restore_dir_str) / "A" / "steering" / "gone.md"
        assert not backup_candidate.exists()


def test_apply_modify_through_live_side_symlinked_dir_leaves_target_byte_identical(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """Same escape, on the MODIFY path. `_resolve_target(root_path,

    relpath)` is called and its containment check would correctly refuse
    a write through `steering` (a live-side symlink to outside the root)
    — but the earlier backup step already read `live_target.exists()`
    and, since it exists (through the symlink), calls
    `_backup_file(live_target, ...)`, copying `outside/kept.md`'s bytes
    into the restore dir BEFORE the write-side check ever runs. Whether
    the write itself lands is a second question this test also pins:
    `kept.md`'s live bytes outside the root must stay byte-identical.
    MUTATION NOTE: this fails today because the backup read happens
    against the unresolved `live_target`, not because the final write
    necessarily succeeds — the backup-before-containment-check ordering
    is the defect, independent of whether `_resolve_target` itself later
    blocks the write.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    prior_bytes = b"# Kept (outside root)\n"
    (outside / "kept.md").write_bytes(prior_bytes)

    root_a = Path(os.environ["KIROCREW_HOME"])
    root_a.joinpath("steering").symlink_to(outside)

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "steering").mkdir()
    (commit_root / "steering" / "kept.md").write_text(
        "# Attacker-controlled content\n", encoding="utf-8"
    )

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["steering/kept.md"], "B": []},
        store=state_store,
    )

    assert (
        outside / "kept.md"
    ).read_bytes() == prior_bytes, (
        "live bytes outside the root must never be overwritten or backed up"
    )
    assert "steering/kept.md" in result.not_applied
    assert "steering/kept.md" not in result.applied
    for restore_dir_str in state_store.restore_dirs.values():
        backup_candidate = Path(restore_dir_str) / "A" / "steering" / "kept.md"
        assert not backup_candidate.exists()


# ---------------------------------------------------------------------------
# Security gap 2: crons/instances sanitize bypass on malformed JSON. When
# the pulled crons.json/instances.json does not parse, `_load_json_or_none`
# returns None, and the per-file loop's `if commit_doc is not None:` guard
# skips restore AND sanitize entirely — `content_to_write` stays the raw,
# unvetted bytes, which are then written straight through (fail-open on a
# Requirement 6 security boundary: an unparsable file is treated as safe to
# apply verbatim instead of as untrusted).
# ---------------------------------------------------------------------------


def test_apply_malformed_crons_json_is_never_written_through_unvetted(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """MUTATION NOTE: the real narrowing dimension is "an unparsable

    crons.json/instances.json must be REFUSED, never written verbatim".
    The current code's `commit_doc = _load_json_or_none(raw_content)`
    returns `None` on a parse failure, and the surrounding
    `if commit_doc is not None:` block — which is also what calls
    `sanitize.sanitize_crons` — is then skipped altogether, leaving
    `content_to_write = raw_content` (the untouched, unvetted bytes) to
    fall through to the atomic write. Against that code this test is
    EXPECTED TO FAIL: the live `crons.json` is overwritten with the
    malformed content, `cron_vet` is never invoked, and the result
    reports `crons.json` as `applied` rather than refused.
    """
    root_a = Path(os.environ["KIROCREW_HOME"])
    prior_bytes = json.dumps(
        {"jobs": [{"name": "existing", "message": "hi"}]}, indent=2
    ).encode("utf-8")
    root_a.joinpath("crons.json").write_bytes(prior_bytes)

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    # Invalid JSON (unterminated object) that nonetheless carries a
    # command-shaped string, so a fail-open write would smuggle it through
    # with no vet ever seeing it.
    malformed = b'{"jobs": [{"name": "x", "command": "rm -rf /"'
    (commit_root / "crons.json").write_bytes(malformed)

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    vet_calls: list[str] = []

    def _recording_vet(command: str) -> str | None:
        vet_calls.append(command)
        return None

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["crons.json"], "B": []},
        store=state_store,
        cron_vet=_recording_vet,
    )

    assert (
        root_a.joinpath("crons.json").read_bytes() == prior_bytes
    ), "a malformed pulled crons.json must never replace the live file"
    assert "crons.json" in result.not_applied
    assert "crons.json" not in result.applied
    assert result.reason != "" or "crons.json" in result.not_applied
    assert vet_calls == [], "the vet must never be bypassed by a parse failure"


def test_apply_records_a_created_file_manifest_per_root_restore_dir(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """Requirements.md 4.7 gap: a file the apply CREATED (no prior live

    bytes) is a documented no-op for ``_backup_file`` — there is no
    record anywhere today of which relpaths under a given ``apply_id``
    were newly created rather than overwritten, so a subsequent restore
    cannot know which live files it must remove versus put-back.

    INTERFACE PINNED for the software-engineer: ``apply_commit`` must
    write a manifest file at
    ``restore_dir/<root>/.created-manifest.json`` (a JSON list of
    relpaths, one manifest per root, sitting inside that root's own
    backup subdirectory the same way `_backup_file`'s
    ``restore_dir / root / relpath`` layout already does — never a
    single manifest shared across both roots) listing every relpath in
    this apply that had NO prior live bytes (``live_existed_before`` is
    False for a write, or the live path did not exist for a delete
    that turned out to be a no-op). The dotfile name can never collide
    with a real allowlisted relpath, since every real backup entry is a
    plain (non-dot) relpath component.

    MUTATION NOTE: the real narrowing dimension is "the manifest names
    means EXACTLY the created relpaths, never the modified ones". A
    mutation that records every applied relpath (created AND modified)
    into the manifest would still pass a `len > 0` check but fails the
    per-member assertion below that ``config.json`` (created, no prior
    live bytes) is IN the manifest while ``steering/a.md`` (modified,
    had prior live bytes and a real backup) is NOT.

    EXPECTED TO FAIL against current `apply.py`: no such manifest file
    is ever written today — this pins the gap, per this task's
    instruction to state gaps rather than invent behaviour.
    """
    root_a = Path(os.environ["KIROCREW_HOME"])
    (root_a / "steering").mkdir()
    (root_a / "steering" / "a.md").write_text("# A live-before\n", encoding="utf-8")
    assert not (root_a / "config.json").exists()

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "steering").mkdir()
    (commit_root / "steering" / "a.md").write_text("# A changed\n", encoding="utf-8")
    (commit_root / "config.json").write_text(
        json.dumps({"agents": {"new": True}}), encoding="utf-8"
    )

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["steering/a.md", "config.json"], "B": []},
        store=state_store,
    )

    assert result.outcome == "applied", result
    assert result.apply_id is not None
    restore_dir = Path(state_store.restore_dirs[result.apply_id])
    manifest_path = restore_dir / "A" / ".created-manifest.json"
    assert manifest_path.is_file(), (
        "apply_commit must write a per-root created-file manifest at "
        "restore_dir/<root>/.created-manifest.json"
    )
    created = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert created == ["config.json"]
    assert "steering/a.md" not in created


def test_apply_records_empty_created_manifest_when_nothing_was_created(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """The negative case for the same guard: an apply that only MODIFIES

    files that already existed live must still write a (present, empty)
    manifest — never omit the file — so a restore route can distinguish
    "no manifest written" (an older apply, or a bug) from "manifest
    written, nothing was created". Asserting presence-with-empty-content
    rather than mere non-crash is the per-member check
    testing-standards.md requires for a collection-scoped guard: a
    manifest that is simply absent must not be silently treated the same
    as one that is present and empty by whatever reads it later.

    EXPECTED TO FAIL against current `apply.py` for the same reason as
    the sibling test above: no manifest is written at all today.
    """
    root_a = Path(os.environ["KIROCREW_HOME"])
    (root_a / "config.json").write_text(
        json.dumps({"agents": {"old": True}}), encoding="utf-8"
    )

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    (commit_root / "config.json").write_text(
        json.dumps({"agents": {"new": True}}), encoding="utf-8"
    )

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["config.json"], "B": []},
        store=state_store,
    )

    assert result.outcome == "applied", result
    assert result.apply_id is not None
    restore_dir = Path(state_store.restore_dirs[result.apply_id])
    manifest_path = restore_dir / "A" / ".created-manifest.json"
    assert manifest_path.is_file()
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == []


def test_apply_malformed_instances_json_is_never_written_through_unvetted(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """Same fail-open gap, on `instances.json` — no `command`/vet involved,

    but Requirement 6's disconnect-on-import sanitizer
    (`sanitize.sanitize_instances`) is equally skipped when the pulled
    file fails to parse, so a malformed file would otherwise be written
    through with none of its `was_connected` values forced False.
    """
    root_a = Path(os.environ["KIROCREW_HOME"])
    prior_bytes = json.dumps(
        {"instances": [{"name": "existing", "was_connected": False}]}, indent=2
    ).encode("utf-8")
    root_a.joinpath("instances.json").write_bytes(prior_bytes)

    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    malformed = b'{"instances": [{"name": "x", "was_connected": true'
    (commit_root / "instances.json").write_bytes(malformed)

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["instances.json"], "B": []},
        store=state_store,
    )

    assert (
        root_a.joinpath("instances.json").read_bytes() == prior_bytes
    ), "a malformed pulled instances.json must never replace the live file"
    assert "instances.json" in result.not_applied
    assert "instances.json" not in result.applied
    assert result.reason != "" or "instances.json" in result.not_applied
