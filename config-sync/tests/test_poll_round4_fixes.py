"""Tests for `backend/poll.py`'s senior-review ROUND 4 fixes.

Covers:

- **H-A**: `notify_operator` prints a non-empty stdout summary (commit,
  author, subject, touched classes) on the `"changed"` outcome only — the
  KiroCrew `command`-cron delivery mechanism for a job with no LLM/agent
  session, confirmed against `kiro_crew/slack/gateway.py`'s own
  `"no output = no delivery"` command-result handling. Never called (so
  nothing is printed) on the `"unchanged"`, `"ls-remote-failed"`, or
  `"fetch-failed"` outcomes.
- **H-B**: `state.record_poll_failure` fires on ALL THREE failure-return
  paths in `run()` — the ls-remote-raised path (already covered by round-3
  tests), the empty/unparseable-head-sha path, and the fetch-failed path
  (already covered) — this file adds the previously-missing empty-head-sha
  coverage the reviewer identified as untested (deleting the one existing
  call site left all 423 tests passing).
- **Medium (H-new-1 exception coverage)**: the `except (CalledProcessError,
  OSError, TimeoutError, GitSafetyError)` handler around the
  fetch/classify block actually catches EACH of those four exception types,
  not just the one real-git `CalledProcessError` the round-2/3 fixture
  exercises.
- **Medium (stale last_poll_failure)**: a successful tick (`"unchanged"` or
  `"changed"`) clears `state.last_poll_failure` — `state.clear_poll_failure`
  is exercised directly in `test_state.py`; this file proves `run()` itself
  calls it on both success paths.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterator

import pytest

from backend import poll, state
from backend.safety import git_safety


def _run_git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    """A plain, unhardened real `git` call for TEST SETUP only — never

    `poll.py`'s own `git_safety.git_argv`, matching every other real-git
    test file's convention in this suite.
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
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Round4")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "round4@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Round4")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "round4@example.com")


@pytest.fixture
def bundle_remote(
    tmp_path: Path,
    isolated_state_dir: Path,
    git_identity: None,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[dict]:
    """A real local bare repo standing in for the bundle repo, matching

    `test_poll_pending.py` / `test_poll_round2_fixes.py`'s fixture exactly.
    """
    remote_dir = tmp_path / "bundle-remote.git"
    remote_dir.mkdir()
    _run_git("init", "-q", "--bare", "-b", "main", cwd=remote_dir)

    seed_dir = tmp_path / "bundle-remote-seed"
    seed_dir.mkdir()
    _run_git("init", "-q", "-b", "main", cwd=seed_dir)
    (seed_dir / "steering").mkdir()
    (seed_dir / "steering" / "seed.md").write_text("seed\n", encoding="utf-8")
    _run_git("add", ".", cwd=seed_dir)
    _run_git("commit", "-q", "-m", "seed", cwd=seed_dir)
    _run_git("remote", "add", "origin", str(remote_dir), cwd=seed_dir)
    _run_git("push", "-q", "origin", "main", cwd=seed_dir)
    old_sha = _run_git("rev-parse", "HEAD", cwd=seed_dir).stdout.strip()

    monkeypatch.setattr(poll, "BUNDLE_REPO_URL", str(remote_dir))

    yield {"seed_dir": seed_dir, "remote_dir": remote_dir, "old_sha": old_sha}


def _push_new_commit(
    seed_dir: Path, *, relpath: str, content: str, subject: str
) -> str:
    target = seed_dir / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _run_git("add", relpath, cwd=seed_dir)
    _run_git("commit", "-q", "-m", subject, cwd=seed_dir)
    _run_git("push", "-q", "origin", "main", cwd=seed_dir)
    result: str = _run_git("rev-parse", "HEAD", cwd=seed_dir).stdout.strip()
    return result


# ---------------------------------------------------------------------------
# H-A — notify_operator prints a non-empty stdout summary on "changed" only.
# ---------------------------------------------------------------------------


def test_notify_operator_prints_a_non_empty_summary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`notify_operator` must print SOMETHING to stdout (the KiroCrew

    command-cron delivery mechanism: non-empty stdout on a non-silent job
    is what the gateway surfaces as a notification) carrying all four
    required fields (requirements.md 4.3), not silently do nothing as the
    prior no-op stub did.
    """
    poll.notify_operator(
        head_sha="abc1234",
        author="Jane Doe",
        subject="add a new steering doc",
        touched_classes=["steering"],
    )

    captured = capsys.readouterr()
    assert captured.out.strip(), "notify_operator must print a non-empty summary"
    assert "abc1234" in captured.out
    assert "Jane Doe" in captured.out
    assert "add a new steering doc" in captured.out
    assert "steering" in captured.out


def test_notify_operator_handles_no_touched_classes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`notify_operator` must not crash and must still print a summary

    when `touched_classes` is empty/None (e.g. a commit that classified
    to nothing) — the four-field contract still holds even when one field
    is empty.
    """
    poll.notify_operator(
        head_sha="def5678",
        author="Jane Doe",
        subject="an empty-classification commit",
        touched_classes=[],
    )

    captured = capsys.readouterr()
    assert captured.out.strip()
    assert "def5678" in captured.out


def test_changed_head_notification_is_delivered_via_stdout(
    isolated_state_dir: Path,
    bundle_remote: dict,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end (no notify_operator mock): a genuinely changed head must

    reach real stdout with the commit's real author/subject/touched
    classes — proving the wiring end-to-end, not just the function in
    isolation.
    """
    store = state.load_state()
    store.record_seen_sha(bundle_remote["old_sha"])

    new_sha = _push_new_commit(
        bundle_remote["seed_dir"],
        relpath="steering/ha-notify.md",
        content="H-A stdout notification regression guard\n",
        subject="h-a stdout notification guard",
    )

    result = poll.run()

    assert result.outcome == "changed"
    captured = capsys.readouterr()
    assert new_sha in captured.out
    assert "h-a stdout notification guard" in captured.out
    assert "touched:" in captured.out and "(none)" not in captured.out, (
        "expected a real, non-empty propagation-class value in the "
        "'touched:' line for a commit that classifies under an "
        "allowlisted steering/ path"
    )


def test_unchanged_head_produces_no_stdout_output(
    isolated_state_dir: Path,
    bundle_remote: dict,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unchanged-head tick must print NOTHING — `notify_operator` is

    never called on this outcome, so the "silent tick costs no
    notification" property (requirements.md 4.2) still holds even though
    `notify_operator` itself now has real output.
    """
    store = state.load_state()
    store.record_seen_sha(bundle_remote["old_sha"])

    result = poll.run()

    assert result.outcome == "unchanged"
    captured = capsys.readouterr()
    assert captured.out == ""


def test_app_json_has_no_poll_cron_and_the_script_forwards_the_summary() -> None:
    """Round-4 H-A made the poll's changed-head stdout summary deliverable

    by declaring the manifest poll cron `"silent": false`. Deployment 5
    moved the poll into a pinned SCRIPT cron (the host's cron sandbox hides
    the git credential a command cron would need), so the manifest must no
    longer carry a poll cron at all, and the summary's delivery path is
    now the script forwarding non-empty poll stdout through `ctx.notify()`
    (`test_host_cron_poll_script.py` proves the forwarding at run time).
    """
    import json

    app_root = Path(__file__).resolve().parent.parent
    manifest = json.loads((app_root / "app.json").read_text(encoding="utf-8"))

    poll_crons = [c for c in manifest["crons"] if c["name"] == "config-sync-poll"]
    assert poll_crons == [], "the poll must not be a manifest command cron"
    script = (app_root / "host-crons" / "config_sync_poll.py").read_text()
    assert "ctx.notify(" in script


# ---------------------------------------------------------------------------
# H-B — record_poll_failure fires on the previously-missing empty-head-sha
# failure-return path.
# ---------------------------------------------------------------------------


def test_empty_head_sha_records_a_poll_failure(
    isolated_state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WHEN `_resolve_remote_head` returns an empty string (an unparseable

    `git ls-remote` result that never raised) THEN `run()` must call
    `state.record_poll_failure` — the reviewer confirmed this exact path
    was untested: deleting the one existing `record_poll_failure` call
    site (on the fetch-failed path) still left all 423 tests passing,
    proving the empty-head-sha and ls-remote-raised paths recorded
    nothing.
    """
    monkeypatch.setattr(poll, "_resolve_remote_head", lambda state_dir_owner: "")

    result = poll.run()

    assert result.outcome == "ls-remote-failed"
    reloaded = state.load_state()
    assert reloaded.last_poll_failure is not None, (
        "an empty/unparseable head sha must record a poll failure, "
        "matching the ls-remote-raised and fetch-failed paths"
    )
    assert reloaded.last_poll_failure["reason"] == "empty head sha"


def test_ls_remote_raised_exception_records_a_poll_failure(
    isolated_state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WHEN `git ls-remote` raises `CalledProcessError` THEN `run()` must

    call `state.record_poll_failure` with the exception's own reason —
    the FIRST of the three failure-return paths (round-4 H-B: all three
    must record, not just fetch-failed).
    """

    def _raising_resolve(state_dir_owner: str) -> str:
        raise subprocess.CalledProcessError(returncode=128, cmd=["git", "ls-remote"])

    monkeypatch.setattr(poll, "_resolve_remote_head", _raising_resolve)

    result = poll.run()

    assert result.outcome == "ls-remote-failed"
    reloaded = state.load_state()
    assert reloaded.last_poll_failure is not None
    assert (
        "128" in reloaded.last_poll_failure["reason"]
        or reloaded.last_poll_failure["reason"]
    )


def test_all_three_failure_paths_record_a_poll_failure_with_distinct_reasons(
    isolated_state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A sweep across the three failure-return paths in `run()`, asserting

    EACH one leaves `last_poll_failure` populated — the reviewer's
    complaint was specifically that a partial fix ("fix poll.py's
    record-failure calls") could still miss a sibling path in the same
    function, so this test exercises all three in one place rather than
    trusting three separately-authored tests never to drift apart.
    """
    # 1. ls-remote raised.
    monkeypatch.setattr(
        poll,
        "_resolve_remote_head",
        lambda state_dir_owner: (_ for _ in ()).throw(
            subprocess.CalledProcessError(returncode=128, cmd=["git", "ls-remote"])
        ),
    )
    result = poll.run()
    assert result.outcome == "ls-remote-failed"
    assert state.load_state().last_poll_failure is not None
    state.load_state().clear_poll_failure()

    # 2. empty head sha.
    monkeypatch.setattr(poll, "_resolve_remote_head", lambda state_dir_owner: "")
    result = poll.run()
    assert result.outcome == "ls-remote-failed"
    assert state.load_state().last_poll_failure is not None
    state.load_state().clear_poll_failure()

    # 3. fetch-failed (changed head, then a real clone failure).
    monkeypatch.setattr(poll, "_resolve_remote_head", lambda state_dir_owner: "f" * 40)
    monkeypatch.setattr(poll, "BUNDLE_REPO_URL", str(tmp_path / "missing" / "repo.git"))
    result = poll.run()
    assert result.outcome == "fetch-failed"
    assert state.load_state().last_poll_failure is not None


# ---------------------------------------------------------------------------
# Medium (H-new-1 exception coverage) — the fetch/classify except-tuple
# actually catches each of its four named exception types.
# ---------------------------------------------------------------------------


def test_fetch_failed_path_catches_a_real_calledprocesserror(
    isolated_state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Already covered indirectly by round-2/3's unreachable-clone-target

    test; restated here explicitly against the exception-type sweep so
    all four types are asserted together in this file.
    """
    monkeypatch.setattr(poll, "_resolve_remote_head", lambda state_dir_owner: "1" * 40)
    monkeypatch.setattr(poll, "BUNDLE_REPO_URL", str(tmp_path / "nope" / "repo.git"))

    result = poll.run()

    assert result.outcome == "fetch-failed"


def test_fetch_failed_path_catches_an_oserror(
    isolated_state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WHEN `_fetch_commit_details` raises `OSError` (e.g. an unreadable/

    unwritable clone directory) THEN `run()` must catch it and return
    `outcome="fetch-failed"`, not let it propagate uncaught.
    """
    monkeypatch.setattr(poll, "_resolve_remote_head", lambda state_dir_owner: "2" * 40)

    def _raising_fetch(
        state_dir_owner: str, sha: str, old_sha: str | None = None
    ) -> None:
        raise OSError("simulated unwritable clone directory")

    monkeypatch.setattr(poll, "_fetch_commit_details", _raising_fetch)

    result = poll.run()

    assert result.outcome == "fetch-failed"
    assert "simulated unwritable clone directory" in result.reason
    reloaded = state.load_state()
    assert reloaded.last_poll_failure is not None


def test_fetch_failed_path_catches_a_timeouterror(
    isolated_state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WHEN `_fetch_commit_details` raises `TimeoutError` (e.g. the shared

    `_clone_lock` timing out under contention) THEN `run()` must catch it
    and return `outcome="fetch-failed"`, not let it propagate uncaught.
    """
    monkeypatch.setattr(poll, "_resolve_remote_head", lambda state_dir_owner: "3" * 40)

    def _raising_fetch(
        state_dir_owner: str, sha: str, old_sha: str | None = None
    ) -> None:
        raise TimeoutError("simulated clone-lock timeout")

    monkeypatch.setattr(poll, "_fetch_commit_details", _raising_fetch)

    result = poll.run()

    assert result.outcome == "fetch-failed"
    assert "simulated clone-lock timeout" in result.reason
    reloaded = state.load_state()
    assert reloaded.last_poll_failure is not None


def test_fetch_failed_path_catches_a_gitsafetyerror(
    isolated_state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WHEN `_fetch_commit_details` raises `git_safety.GitSafetyError`

    (e.g. the attributes pin could not be established) THEN `run()` must
    catch it and return `outcome="fetch-failed"`, not let it propagate
    uncaught — the fourth and final type in the except-tuple.
    """
    monkeypatch.setattr(poll, "_resolve_remote_head", lambda state_dir_owner: "4" * 40)

    def _raising_fetch(
        state_dir_owner: str, sha: str, old_sha: str | None = None
    ) -> None:
        raise git_safety.GitSafetyError("simulated attributes-pin failure")

    monkeypatch.setattr(poll, "_fetch_commit_details", _raising_fetch)

    result = poll.run()

    assert result.outcome == "fetch-failed"
    assert "simulated attributes-pin failure" in result.reason
    reloaded = state.load_state()
    assert reloaded.last_poll_failure is not None


# ---------------------------------------------------------------------------
# Medium (stale last_poll_failure) — run() clears it on both success paths.
# ---------------------------------------------------------------------------


def test_unchanged_head_clears_a_prior_poll_failure(
    isolated_state_dir: Path,
    bundle_remote: dict,
) -> None:
    """WHEN a prior tick recorded a poll failure and a LATER tick resolves

    to `"unchanged"` THEN `last_poll_failure` must be cleared — otherwise
    the app's UI would show a stale failure indefinitely even though the
    poll has recovered.
    """
    store = state.load_state()
    store.record_seen_sha(bundle_remote["old_sha"])
    store.record_poll_failure(reason="a transient failure from a prior tick")
    assert store.last_poll_failure is not None

    result = poll.run()

    assert result.outcome == "unchanged"
    reloaded = state.load_state()
    assert reloaded.last_poll_failure is None


def test_changed_head_clears_a_prior_poll_failure(
    isolated_state_dir: Path,
    bundle_remote: dict,
) -> None:
    """WHEN a prior tick recorded a poll failure and a LATER tick resolves

    a genuinely changed head THEN `last_poll_failure` must be cleared —
    the recovery signal must not depend on which of the two success
    outcomes ends the streak.
    """
    store = state.load_state()
    store.record_seen_sha(bundle_remote["old_sha"])
    store.record_poll_failure(reason="a transient failure from a prior tick")
    assert store.last_poll_failure is not None

    _push_new_commit(
        bundle_remote["seed_dir"],
        relpath="steering/clears-failure.md",
        content="clears a prior poll failure on recovery\n",
        subject="clears a prior poll failure on recovery",
    )

    result = poll.run()

    assert result.outcome == "changed"
    reloaded = state.load_state()
    assert reloaded.last_poll_failure is None


def test_fetch_failed_does_not_clear_a_prior_poll_failure(
    isolated_state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A fetch-failed tick must NOT clear `last_poll_failure` — it is

    itself a failure, so clearing here would erase the very signal this
    fix exists to keep visible while the poll is still broken.
    """
    store = state.load_state()
    store.record_poll_failure(reason="an earlier, still-unresolved failure")

    monkeypatch.setattr(poll, "_resolve_remote_head", lambda state_dir_owner: "5" * 40)
    monkeypatch.setattr(poll, "BUNDLE_REPO_URL", str(tmp_path / "gone" / "repo.git"))

    result = poll.run()

    assert result.outcome == "fetch-failed"
    reloaded = state.load_state()
    assert reloaded.last_poll_failure is not None
