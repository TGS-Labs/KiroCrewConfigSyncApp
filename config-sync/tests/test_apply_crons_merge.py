"""crons.json is MERGED into the live store, never substituted (Deployment 5,
live-install defect 8).

The incident (2026-10-01 15:59 UTC): the first poll tick after this box's own
push applied the committed ``crons.json`` — a snapshot taken BEFORE the
operator-approved ``config-sync-poll`` script job existed — by writing it over
the live file wholesale. That deleted the poll's own job and its vault grant,
and resurrected two retired jobs. The gateway reloaded from disk; the schedule
was gone.

The rule this file pins (requirements.md 6.11):

* a job that exists live (matched by name OR id) is LEFT EXACTLY AS IT IS —
  its enabled state, its grant, its runtime bookkeeping — whether or not the
  commit also carries it (so fleet-wide updates do not propagate by pull);
* a job the commit carries that is NOT live is ADDED, sanitized (vet, paused)
  as before — UNLESS it was in the ``crons.json`` of the last fully-applied
  commit (``base_sha``): then the operator deleted it locally and pull must
  not resurrect it (review round 3, H2);
* a job that is live but absent from the commit is PRESERVED — pulling a
  ``crons.json`` can never delete a local job (removal stays an operator
  action on the Schedule page);
* a live ``crons.json`` that cannot be parsed makes the file ``not_applied``
  rather than being overwritten by something that cannot be merged into it.

``instances.json`` is unchanged by this file.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from backend import apply, sanitize, state

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    ).stdout.strip()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    root_a = tmp_path / "kirocrew-home"
    root_b = tmp_path / "kiro-home"
    root_a.mkdir()
    root_b.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(root_a))
    monkeypatch.setenv("KIRO_HOME", str(root_b))
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(tmp_path / "config-sync-state"))
    repo = tmp_path / "bundle-repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "initial", cwd=repo)
    sha = _git("rev-parse", "HEAD", cwd=repo)
    store = state.load_state()
    store.set_pending(
        sha=sha,
        author="a",
        subject="s",
        classified_paths={},
        ignored_paths=[],
        touched_classes=[],
    )
    commit_root = tmp_path / "commit-root"
    commit_root.mkdir()
    return {"root_a": root_a, "sha": sha, "store": store, "commit_root": commit_root}


def _job(job_id: str, name: str, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": job_id,
        "name": name,
        "schedule": {"every": 900},
        "enabled": True,
        "user_paused": False,
        "command": "",
        "script": "",
        "message": "",
    }
    base.update(extra)
    return base


# The live poll job as the host stores it: a script job with an ACTIVE grant.
_LIVE_POLL = _job(
    "3535712d",
    "config-sync-poll",
    script="~/.kiro/crew/crons/config_sync_poll.py:run",
    secret_env={"CONFIG_SYNC_GITHUB_TOKEN": "KiroCrewSyncGHToken"},
    secret_env_pin="pin-bytes",
    session_key="dashboard:abc",
    last_status="ok",
    consecutive_failures=0,
)


def _always_clean(command: str) -> str | None:
    return None


def _apply_crons(env: dict[str, Any], commit_doc: dict[str, Any]) -> apply.ApplyResult:
    (env["commit_root"] / "crons.json").write_text(
        json.dumps(commit_doc, indent=2) + "\n", encoding="utf-8"
    )
    return apply.apply_commit(
        approved_sha=env["sha"],
        commit_root=env["commit_root"],
        changed_paths={"A": ["crons.json"], "B": []},
        store=env["store"],
        cron_vet=_always_clean,
    )


def _live(env: dict[str, Any]) -> dict[str, Any]:
    return json.loads((env["root_a"] / "crons.json").read_text(encoding="utf-8"))


# ── the incident, reproduced ─────────────────────────────────────────────


def test_incident_pulled_crons_json_without_the_poll_job_preserves_it_and_its_grant(
    env: dict[str, Any],
) -> None:
    live_doc = {"version": 3, "jobs": [_LIVE_POLL]}
    (env["root_a"] / "crons.json").write_text(json.dumps(live_doc, indent=2) + "\n")
    # The committed snapshot predates the job: two retired app jobs only.
    commit_doc = {
        "version": 3,
        "jobs": [
            _job(
                "aaf2def9",
                "config-sync/config-sync-push",
                command="python3 -m backend.push",
            ),
            _job(
                "7c7c3de0",
                "config-sync/config-sync-poll",
                command="python3 -m backend.poll",
            ),
        ],
    }

    result = _apply_crons(env, commit_doc)

    assert "crons.json" in result.applied, result.not_applied
    written = _live(env)
    by_id = {j["id"]: j for j in written["jobs"]}
    assert "3535712d" in by_id, "pulling crons.json deleted a local job"
    assert by_id["3535712d"] == _LIVE_POLL, "a live job must be preserved byte-for-byte"
    assert by_id["3535712d"]["enabled"] is True
    assert by_id["3535712d"]["secret_env"] == {
        "CONFIG_SYNC_GITHUB_TOKEN": "KiroCrewSyncGHToken"
    }
    # The commit's jobs are added, sanitized (paused), as before.
    for retired in ("aaf2def9", "7c7c3de0"):
        assert (
            by_id[retired]["user_paused"] is True and by_id[retired]["enabled"] is False
        )
    assert set(result.paused_cron_names) == {
        "config-sync/config-sync-push",
        "config-sync/config-sync-poll",
    }


# ── the rule, case by case ───────────────────────────────────────────────


def test_a_job_present_in_both_keeps_the_live_version_and_is_not_paused(
    env: dict[str, Any],
) -> None:
    (env["root_a"] / "crons.json").write_text(
        json.dumps({"version": 3, "jobs": [_LIVE_POLL]}) + "\n"
    )
    # Another box pushed the same job: redacted grant, different bookkeeping,
    # and the sanitizer would force it paused — none of that may land.
    pushed = dict(_LIVE_POLL)
    pushed.update(
        secret_env={}, secret_env_pin="", last_status="error", consecutive_failures=4
    )
    result = _apply_crons(env, {"version": 3, "jobs": [pushed]})

    assert "crons.json" in result.applied
    (only,) = _live(env)["jobs"]
    assert only == _LIVE_POLL
    assert result.paused_cron_names == []


def test_jobs_only_in_the_commit_are_added_sanitized_and_live_order_is_kept(
    env: dict[str, Any],
) -> None:
    local_a = _job("aaaa0001", "local-a")
    local_b = _job("bbbb0002", "local-b", command="echo b")
    (env["root_a"] / "crons.json").write_text(
        json.dumps({"version": 3, "jobs": [local_a, local_b]}) + "\n"
    )
    new_cmd = _job("cccc0003", "fleet-c", command="echo c")
    new_msg = _job("dddd0004", "fleet-d", message="agent job")
    result = _apply_crons(env, {"version": 3, "jobs": [new_cmd, new_msg]})

    written = _live(env)
    assert [j["id"] for j in written["jobs"]] == [
        "aaaa0001",
        "bbbb0002",
        "cccc0003",
        "dddd0004",
    ]
    assert written["version"] == 3
    by_id = {j["id"]: j for j in written["jobs"]}
    assert by_id["bbbb0002"] == local_b, "a live command job is not re-paused by a pull"
    assert (
        by_id["cccc0003"]["user_paused"] is True
        and by_id["cccc0003"]["enabled"] is False
    )
    assert by_id["dddd0004"]["enabled"] is True, "a message-only job is not paused"
    assert result.paused_cron_names == ["fleet-c"]


def test_a_vet_failing_commit_job_that_is_live_leaves_the_live_job_alone(
    env: dict[str, Any],
) -> None:
    live_job = _job("eeee0005", "mine", command="echo mine")
    (env["root_a"] / "crons.json").write_text(
        json.dumps({"version": 3, "jobs": [live_job]}) + "\n"
    )
    bad = _job("eeee0005", "mine", command="echo $(whoami)")
    (env["commit_root"] / "crons.json").write_text(
        json.dumps({"version": 3, "jobs": [bad]}) + "\n"
    )

    def _vet(command: str) -> str | None:
        return "Error: command substitution" if "$(" in command else None

    result = apply.apply_commit(
        approved_sha=env["sha"],
        commit_root=env["commit_root"],
        changed_paths={"A": ["crons.json"], "B": []},
        store=env["store"],
        cron_vet=_vet,
    )
    assert "crons.json" in result.applied
    (only,) = _live(env)["jobs"]
    assert only == live_job


def test_no_live_crons_json_writes_the_sanitized_commit_as_before(
    env: dict[str, Any]
) -> None:
    result = _apply_crons(
        env, {"version": 3, "jobs": [_job("ffff0006", "fleet-f", command="echo f")]}
    )
    assert "crons.json" in result.applied
    (only,) = _live(env)["jobs"]
    assert only["id"] == "ffff0006" and only["user_paused"] is True
    assert result.paused_cron_names == ["fleet-f"]


def test_an_unparsable_live_crons_json_is_refused_not_overwritten(
    env: dict[str, Any]
) -> None:
    live_path = env["root_a"] / "crons.json"
    live_path.write_text("{not json", encoding="utf-8")
    result = _apply_crons(env, {"version": 3, "jobs": [_job("0000aaaa", "x")]})

    assert "crons.json" not in result.applied
    assert "crons.json" in result.not_applied
    assert "merge" in result.not_applied["crons.json"].lower()
    assert live_path.read_text(encoding="utf-8") == "{not json"


def test_a_live_crons_json_of_the_wrong_shape_is_refused_not_overwritten(
    env: dict[str, Any],
) -> None:
    live_path = env["root_a"] / "crons.json"
    live_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    result = _apply_crons(env, {"version": 3, "jobs": [_job("0000bbbb", "y")]})

    assert "crons.json" in result.not_applied
    assert live_path.read_text(encoding="utf-8") == json.dumps([1, 2, 3])


# ── review round 3: three-way merge, duplicate ids, idempotency ──────────


def test_a_job_the_operator_removed_since_the_base_commit_is_not_re_added(
    env: dict[str, Any],
) -> None:
    """Review H2. Pull used to only ever ADD, so a job deleted on the
    Schedule page came back on the next pull that touched crons.json. The
    last fully-applied commit (``base_sha``) is the third side: a commit
    job that is absent live but WAS in the base was removed locally."""
    (env["root_a"] / "crons.json").write_text(
        json.dumps({"version": 3, "jobs": []}) + "\n"
    )
    retired = _job(
        "aaf2def9", "config-sync/config-sync-push", command="python3 -m backend.push"
    )
    fresh = _job("1111aaaa", "fleet-new", command="echo new")
    base_doc = {"version": 3, "jobs": [retired]}
    commit_doc = {"version": 3, "jobs": [retired, fresh]}
    (env["commit_root"] / "crons.json").write_text(json.dumps(commit_doc) + "\n")

    result = apply.apply_commit(
        approved_sha=env["sha"],
        commit_root=env["commit_root"],
        changed_paths={"A": ["crons.json"], "B": []},
        store=env["store"],
        cron_vet=_always_clean,
        base_crons_doc=base_doc,
    )

    assert "crons.json" in result.applied
    ids = [j["id"] for j in _live(env)["jobs"]]
    assert ids == ["1111aaaa"], ids
    assert result.paused_cron_names == ["fleet-new"]


def test_without_a_base_every_unmatched_commit_job_is_added(
    env: dict[str, Any]
) -> None:
    """First-ever tick: no base exists, so nothing can be known as removed."""
    (env["root_a"] / "crons.json").write_text(
        json.dumps({"version": 3, "jobs": []}) + "\n"
    )
    job = _job("2222bbbb", "fleet-x", command="echo x")
    result = _apply_crons(env, {"version": 3, "jobs": [job]})
    assert [j["id"] for j in _live(env)["jobs"]] == ["2222bbbb"]
    assert result.paused_cron_names == ["fleet-x"]


def test_a_commit_job_whose_id_is_live_under_another_name_is_not_added(
    env: dict[str, Any],
) -> None:
    """Review H3. The host keys jobs by id (`jobs_by_id`, `cron:<id>` session
    keys), so a rename on either side must never produce two records with
    one id. Match on EITHER name or id."""
    live = _job("3333cccc", "old-name", command="echo a")
    (env["root_a"] / "crons.json").write_text(
        json.dumps({"version": 3, "jobs": [live]}) + "\n"
    )
    renamed = _job("3333cccc", "new-name", command="echo a")
    same_name_other_id = _job("4444dddd", "old-name", command="echo b")
    result = _apply_crons(env, {"version": 3, "jobs": [renamed, same_name_other_id]})

    assert "crons.json" in result.applied
    (only,) = _live(env)["jobs"]
    assert only == live
    assert result.paused_cron_names == []


def test_applying_the_same_commit_twice_is_byte_identical(env: dict[str, Any]) -> None:
    """Review H4 (#66): the poll's retry path re-applies a still-partial
    range; the second pass must change nothing."""
    (env["root_a"] / "crons.json").write_text(
        json.dumps({"version": 3, "jobs": [_LIVE_POLL]}) + "\n"
    )
    commit_doc = {"version": 3, "jobs": [_job("5555eeee", "fleet-e", command="echo e")]}
    first = _apply_crons(env, commit_doc)
    bytes_after_first = (env["root_a"] / "crons.json").read_bytes()
    assert first.paused_cron_names == ["fleet-e"]

    env["store"].set_pending(
        sha=env["sha"],
        author="a",
        subject="s",
        classified_paths={},
        ignored_paths=[],
        touched_classes=[],
    )
    second = _apply_crons(env, commit_doc)
    assert "crons.json" in second.applied
    assert (env["root_a"] / "crons.json").read_bytes() == bytes_after_first
    assert second.paused_cron_names == []


def test_merge_crons_three_way_contract() -> None:
    live = {"version": 3, "jobs": [_job("L", "local")]}
    base = {"version": 3, "jobs": [_job("R", "removed-locally")]}
    commit = {"version": 3, "jobs": [_job("R", "removed-locally"), _job("N", "new")]}
    merged = sanitize.merge_crons(live, commit, base=base)
    assert [j["id"] for j in merged.merged_store["jobs"]] == ["L", "N"]
    assert merged.added_job_names == ["new"]
    assert merged.removed_locally_job_names == ["removed-locally"]
    # A malformed base is ignored (treated as no base), never fatal.
    merged2 = sanitize.merge_crons(live, commit, base={"nope": 1})
    assert [j["id"] for j in merged2.merged_store["jobs"]] == ["L", "R", "N"]


# ── review round 4 (N1): the base must be what was ACTUALLY applied ──────


def test_a_base_job_the_vet_dropped_was_never_live_so_the_fixed_version_is_added(
    env: dict[str, Any],
) -> None:
    """N1(i). At the base apply, job X failed the vet and was dropped — the
    file still counted as applied. X was therefore never live. When the fleet
    ships a FIXED X, a raw base would class it "removed locally". The base
    must be vetted with the same vet before it is used as the third side."""
    (env["root_a"] / "crons.json").write_text(
        json.dumps({"version": 3, "jobs": []}) + "\n"
    )
    bad = _job("6666ffff", "fleet-x", command="echo $(whoami)")
    fixed = _job("6666ffff", "fleet-x", command="echo fixed")
    (env["commit_root"] / "crons.json").write_text(
        json.dumps({"version": 3, "jobs": [fixed]}) + "\n"
    )

    def _vet(command: str) -> str | None:
        return "Error: command substitution" if "$(" in command else None

    result = apply.apply_commit(
        approved_sha=env["sha"],
        commit_root=env["commit_root"],
        changed_paths={"A": ["crons.json"], "B": []},
        store=env["store"],
        cron_vet=_vet,
        base_crons_doc={"version": 3, "jobs": [bad]},
    )
    assert "crons.json" in result.applied
    assert [j["id"] for j in _live(env)["jobs"]] == ["6666ffff"]
    assert result.paused_cron_names == ["fleet-x"]


def test_the_base_passed_in_is_not_mutated_by_the_vet(env: dict[str, Any]) -> None:
    (env["root_a"] / "crons.json").write_text(
        json.dumps({"version": 3, "jobs": []}) + "\n"
    )
    base = {"version": 3, "jobs": [_job("7777aaaa", "b", command="echo b")]}
    snapshot = json.dumps(base, sort_keys=True)
    (env["commit_root"] / "crons.json").write_text(
        json.dumps({"version": 3, "jobs": []}) + "\n"
    )
    apply.apply_commit(
        approved_sha=env["sha"],
        commit_root=env["commit_root"],
        changed_paths={"A": ["crons.json"], "B": []},
        store=env["store"],
        cron_vet=_always_clean,
        base_crons_doc=base,
    )
    assert json.dumps(base, sort_keys=True) == snapshot


def test_resolve_pending_records_the_fully_applied_sha_and_advance_base_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N1(ii). `base_sha` can point at a commit that was never applied (the
    bootstrap-partial root ancestor). The merge's third side must come from
    the last FULLY applied commit, which only `resolve_pending` records."""
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(tmp_path / "s"))
    store = state.load_state()
    assert store.last_fully_applied_sha is None
    store.advance_base_sha("a" * 40)
    assert state.load_state().last_fully_applied_sha is None
    store.set_pending(
        sha="b" * 40,
        author="a",
        subject="s",
        classified_paths={},
        ignored_paths=[],
        touched_classes=[],
    )
    store.resolve_pending(sha="b" * 40)
    fresh = state.load_state()
    assert fresh.last_fully_applied_sha == "b" * 40
    assert fresh.base_sha == "b" * 40


def test_poll_uses_the_last_fully_applied_commit_not_a_never_applied_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N1(ii) seam. `base_sha` is advanced to a commit that ships job R but
    was NEVER applied (bootstrap partial shape); R is not live. Head still
    carries R. R must be ADDED — it was never removed by anyone."""
    from backend import poll

    root_a = tmp_path / "kirocrew-home"
    root_b = tmp_path / "kiro-home"
    state_dir = tmp_path / "config-sync-state"
    for d in (root_a, root_b, state_dir):
        d.mkdir(parents=True)
    monkeypatch.setenv("KIROCREW_HOME", str(root_a))
    monkeypatch.setenv("KIRO_HOME", str(root_b))
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setenv(poll.PREFETCHED_ENV, "1")
    monkeypatch.setattr(poll, "BUNDLE_REPO_URL", "https://127.0.0.1:9/unreachable.git")

    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    r_job = _job("aaf2def9", "r", command="echo r")
    (work / "crons.json").write_text(json.dumps({"version": 3, "jobs": [r_job]}) + "\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "never-applied-base", cwd=work)
    base_sha = _git("rev-parse", "HEAD", cwd=work)
    n_job = _job("1111aaaa", "n", command="echo n")
    (work / "crons.json").write_text(
        json.dumps({"version": 3, "jobs": [r_job, n_job]}) + "\n"
    )
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "head", cwd=work)
    origin = tmp_path / "origin.git"
    _git("clone", "-q", "--bare", str(work), str(origin), cwd=tmp_path)
    clone_dir = state_dir / poll._BUNDLE_CLONE_DIRNAME
    _git("clone", "-q", str(origin), str(clone_dir), cwd=tmp_path)
    _git(
        "remote",
        "set-url",
        "origin",
        "https://127.0.0.1:9/unreachable.git",
        cwd=clone_dir,
    )

    store = state.load_state()
    store.record_seen_sha(base_sha)
    store.advance_base_sha(base_sha)  # bootstrap shape: base set, never applied
    (root_a / "crons.json").write_text(json.dumps({"version": 3, "jobs": []}) + "\n")

    result = poll.run()

    assert result.outcome == "changed", (result.outcome, result.reason)
    written = json.loads((root_a / "crons.json").read_text(encoding="utf-8"))
    assert [j["id"] for j in written["jobs"]] == ["aaf2def9", "1111aaaa"], written


# ── the merge function itself ────────────────────────────────────────────


def test_poll_tick_passes_the_base_commits_crons_json_into_the_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seam test (testing-standards § Composition): `poll._apply_new_head`
    must hand `apply_commit` the crons.json AT `base_sha`, read from the
    shared clone, or the three-way rule above is dead code on the live
    path. Commit 1 (the base) ships job R; the operator then deletes R
    locally; commit 2 still carries R and adds N. After the tick: N only."""
    from backend import poll

    root_a = tmp_path / "kirocrew-home"
    root_b = tmp_path / "kiro-home"
    state_dir = tmp_path / "config-sync-state"
    for d in (root_a, root_b, state_dir):
        d.mkdir(parents=True)
    monkeypatch.setenv("KIROCREW_HOME", str(root_a))
    monkeypatch.setenv("KIRO_HOME", str(root_b))
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setenv(poll.PREFETCHED_ENV, "1")
    monkeypatch.setattr(poll, "BUNDLE_REPO_URL", "https://127.0.0.1:9/unreachable.git")

    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    retired = _job("aaf2def9", "retired", command="python3 -m backend.push")
    (work / "crons.json").write_text(
        json.dumps({"version": 3, "jobs": [retired]}) + "\n"
    )
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "base", cwd=work)
    base_sha = _git("rev-parse", "HEAD", cwd=work)
    fresh = _job("1111aaaa", "fresh", command="echo new")
    (work / "crons.json").write_text(
        json.dumps({"version": 3, "jobs": [retired, fresh]}) + "\n"
    )
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "head", cwd=work)
    origin = tmp_path / "origin.git"
    _git("clone", "-q", "--bare", str(work), str(origin), cwd=tmp_path)
    clone_dir = state_dir / poll._BUNDLE_CLONE_DIRNAME
    _git("clone", "-q", str(origin), str(clone_dir), cwd=tmp_path)
    _git(
        "remote",
        "set-url",
        "origin",
        "https://127.0.0.1:9/unreachable.git",
        cwd=clone_dir,
    )

    # The base is applied and the operator has since removed R locally.
    store = state.load_state()
    store.record_seen_sha(base_sha)
    store.set_pending(
        sha=base_sha,
        author="a",
        subject="s",
        classified_paths={},
        ignored_paths=[],
        touched_classes=[],
    )
    store.resolve_pending(sha=base_sha)
    (root_a / "crons.json").write_text(json.dumps({"version": 3, "jobs": []}) + "\n")

    result = poll.run()

    assert result.outcome == "changed", (result.outcome, result.reason)
    written = json.loads((root_a / "crons.json").read_text(encoding="utf-8"))
    assert [j["id"] for j in written["jobs"]] == ["1111aaaa"], written
    assert (state.load_state().last_apply or {}).get("outcome") == "applied"


def test_merge_crons_unit_contract() -> None:
    live = {"version": 3, "jobs": [_job("1", "one", enabled=True), _job("2", "two")]}
    commit = {
        "version": 9,
        "jobs": [_job("2", "two", enabled=False), _job("3", "three")],
    }
    merged = sanitize.merge_crons(live, commit)
    assert merged.merged_store["version"] == 3, "the live document's shape wins"
    assert [j["id"] for j in merged.merged_store["jobs"]] == ["1", "2", "3"]
    assert merged.merged_store["jobs"][1] == live["jobs"][1]
    assert merged.added_job_names == ["three"]
    assert merged.preserved_job_names == ["one", "two"]
    # Inputs are not mutated.
    assert [j["id"] for j in live["jobs"]] == ["1", "2"]
    assert [j["id"] for j in commit["jobs"]] == ["2", "3"]


def test_merge_crons_refuses_a_live_store_without_a_jobs_list() -> None:
    with pytest.raises(ValueError):
        sanitize.merge_crons({"version": 3}, {"version": 3, "jobs": []})
    with pytest.raises(ValueError):
        sanitize.merge_crons({"version": 3, "jobs": "nope"}, {"version": 3, "jobs": []})


def test_base_crons_doc_degrades_to_none_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every failure to read the base's crons.json means a two-way merge,
    never a skipped or failed apply."""
    from backend import poll

    state_dir = tmp_path / "config-sync-state"
    state_dir.mkdir()
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(state_dir))
    assert poll._base_crons_doc(None) is None
    assert poll._base_crons_doc("0" * 40) is None  # no clone yet

    clone_dir = state_dir / poll._BUNDLE_CLONE_DIRNAME
    clone_dir.mkdir()
    _git("init", "-q", "-b", "main", cwd=clone_dir)
    (clone_dir / "crons.json").write_text("{not json")
    (clone_dir / "other.json").write_text("[1, 2]")
    _git("add", "-A", cwd=clone_dir)
    _git("commit", "-q", "-m", "c", cwd=clone_dir)
    sha = _git("rev-parse", "HEAD", cwd=clone_dir)
    assert poll._base_crons_doc(sha) is None  # invalid JSON at that path
    assert poll._base_crons_doc("f" * 40) is None  # unknown object
    (clone_dir / "crons.json").write_text("[1, 2]")
    _git("add", "-A", cwd=clone_dir)
    _git("commit", "-q", "-m", "d", cwd=clone_dir)
    assert poll._base_crons_doc(_git("rev-parse", "HEAD", cwd=clone_dir)) is None
    (clone_dir / "crons.json").write_text(json.dumps({"version": 3, "jobs": []}))
    _git("add", "-A", cwd=clone_dir)
    _git("commit", "-q", "-m", "e", cwd=clone_dir)
    assert poll._base_crons_doc(_git("rev-parse", "HEAD", cwd=clone_dir)) == {
        "version": 3,
        "jobs": [],
    }
