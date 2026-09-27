"""H4 RATIFIED: a partial apply does not resolve pending.

Covers the ratified rule: 'a partial apply does NOT resolve pending;
base_sha not advanced; not-applied paths with reasons stay visible in
status; re-approve is safe; only applied resolves.'

`backend/routes.py::approve` currently resolves pending
(`state.resolve_pending`) whenever `apply_commit` reports EITHER
`outcome="applied"` OR `outcome="partial"` — see its own docstring
and the `if result.outcome not in ("applied", "partial"): ... return`
branch. That is exactly what this file proves wrong: a partial apply must
leave `pending`/`base_sha` untouched (so the operator still sees the
commit as pending, with the not-applied paths and reasons visible via
`status()`), be safe to re-approve, and only ever resolve pending once
every eligible file actually applied.

These tests exercise `routes.approve` through the REAL `apply_commit`
seam, on the real-git fixtures `tests/test_routes_approve_seam.py`
already builds (a bundle-repo clone, `git archive`, real file writes to
isolated KIROCREW_HOME/KIRO_HOME roots) — reusing that file's fixture
helpers directly rather than re-inventing a second real-git harness.

## Expected to be RED right now

Every test in this file that asserts "pending is still set after a
partial apply" is expected to FAIL against the current `routes.py`:
`approve`'s own code resolves pending on `outcome in ("applied",
"partial")`, so a partial apply's `pending`/`base_sha` are wrongly
advanced/cleared today. Making these tests green requires
`routes.approve` to call `state.resolve_pending` only when
`result.outcome == "applied"`, not `"partial"`.

## How the fixture forces a genuine partial outcome

One file in the commit (`mcp.json`) is malformed JSON. `apply.py`'s H1
rule refuses ANY allowlisted `.json` file (outside the crons/instances
Requirement 6 exception) that fails to parse — see `_apply_one_file`'s
``elif relpath.endswith(".json"): ... not_applied.append(relpath);
return``. A second file in the SAME commit (`steering/x.md`, a plain
non-JSON tracked file) has no such defect and applies cleanly. This
combination is what makes `apply_commit` report `outcome="partial"`
with one path in `applied` and one in `not_applied`, without touching
`backend/apply.py` or `backend/routes.py` at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from backend import state

from test_routes_approve_seam import (
    _bundle_repo_url_env,
    _git,
    _head_sha,
)


# ---------------------------------------------------------------------------
# Fixtures — mirrors test_routes_approve_seam.py's own isolated-env shape.
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    state_dir = tmp_path / "config-sync-state"
    root_a = tmp_path / "kiro-crew-home"
    root_b = tmp_path / "kiro-home"
    root_a.mkdir(parents=True, exist_ok=True)
    root_b.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setenv("KIROCREW_HOME", str(root_a))
    monkeypatch.setenv("KIRO_HOME", str(root_b))

    return {"state_dir": state_dir, "root_a": root_a, "root_b": root_b}


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


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    origin_repo = tmp_path / "origin.git"
    origin_repo.mkdir()
    _git("init", "-q", "--bare", "-b", "main", cwd=origin_repo)
    return origin_repo


def _seed_partial_history(origin: Path, tmp_path: Path) -> str:
    """One commit: a clean ``steering/x.md`` (applies) plus a malformed

    ``mcp.json`` (refused by apply.py's H1 JSON-parse guard) -- the exact
    shape that makes ``apply_commit`` report ``outcome="partial"`` with
    one applied path and one not-applied path, on the FIRST approve.
    """
    work = tmp_path / "seed-work"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    _git("remote", "add", "origin", str(origin), cwd=work)

    (work / "steering").mkdir()
    (work / "steering" / "x.md").write_text("# x v1\n", encoding="utf-8")
    # Malformed JSON -- apply.py refuses this outright (H1), never
    # writing it through unparsed.
    (work / "mcp.json").write_text("{not valid json", encoding="utf-8")

    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "add steering/x.md, malformed mcp.json", cwd=work)
    head_sha = _head_sha(work)
    _git("push", "-q", "-u", "origin", "main", cwd=work)
    return head_sha


def _fix_mcp_json_and_push(origin: Path, tmp_path: Path) -> str:
    """Push a second commit on top that fixes ``mcp.json`` to valid JSON,

    simulating "the upstream fix landed" for the re-approve-after-fix
    scenario. Returns the new head sha.
    """
    work = tmp_path / "fix-work"
    work.mkdir()
    _git("clone", "-q", str(origin), str(work), cwd=tmp_path)
    (work / "mcp.json").write_text('{"mcpServers": {}}\n', encoding="utf-8")
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "fix mcp.json", cwd=work)
    _git("push", "-q", "origin", "main", cwd=work)
    return _head_sha(work)


def _seed_pending_for(
    store: state.StateStore,
    *,
    sha: str,
    subject: str = "add steering/x.md, malformed mcp.json",
) -> None:
    store.set_pending(
        sha=sha,
        author="Author <a@example.com>",
        subject=subject,
        classified_paths={
            "steering/x.md": "live_in_new_session",
            "mcp.json": "live_on_next_resolution",
        },
    )


@pytest.fixture
def bundle_url_patched(monkeypatch: pytest.MonkeyPatch, origin: Path) -> None:
    _bundle_repo_url_env(monkeypatch, origin)


# ---------------------------------------------------------------------------
# 1. A partial apply does not resolve pending; base_sha is not advanced.
# ---------------------------------------------------------------------------


class TestPartialApplyDoesNotResolvePending:
    def test_partial_apply_leaves_pending_set_with_the_same_sha(
        self,
        routes_module: Any,
        store: state.StateStore,
        origin: Path,
        tmp_path: Path,
        isolated_env: dict[str, Path],
        bundle_url_patched: None,
    ) -> None:
        head_sha = _seed_partial_history(origin, tmp_path)
        base_sha_before = store.base_sha
        _seed_pending_for(store, sha=head_sha)

        result = routes_module.approve(store, head_sha)

        assert result.get("outcome") == "partial", result

        reloaded = state.load_state()
        assert reloaded.pending is not None, (
            "H4 (ratified): a partial apply must NOT resolve pending -- "
            "the operator still needs to see this commit as pending, "
            "with mcp.json's refusal reason visible"
        )
        assert reloaded.pending["sha"] == head_sha
        assert (
            reloaded.base_sha == base_sha_before
        ), "H4 (ratified): base_sha must NOT advance on a partial apply"

    def test_partial_apply_still_writes_the_files_that_did_succeed(
        self,
        routes_module: Any,
        store: state.StateStore,
        origin: Path,
        tmp_path: Path,
        isolated_env: dict[str, Path],
        bundle_url_patched: None,
    ) -> None:
        """Not resolving pending must not mean nothing happened -- the

        file that genuinely applied (steering/x.md) still lands on disk;
        only the refused file (mcp.json) and the pending record's
        resolution are held back.
        """
        head_sha = _seed_partial_history(origin, tmp_path)
        _seed_pending_for(store, sha=head_sha)

        routes_module.approve(store, head_sha)

        live_x = isolated_env["root_a"] / "steering" / "x.md"
        assert live_x.is_file()
        assert live_x.read_text(encoding="utf-8") == "# x v1\n"

        live_mcp = isolated_env["root_a"] / "mcp.json"
        assert not live_mcp.exists(), (
            "mcp.json must never be written through unparsed -- H1's "
            "refusal must leave no half-written file on the live root"
        )


# ---------------------------------------------------------------------------
# 2. not_applied paths with reasons stay visible in status().
# ---------------------------------------------------------------------------


class TestPartialApplyStaysVisibleInStatus:
    def test_pending_summary_in_status_still_names_the_partial_commit(
        self,
        routes_module: Any,
        store: state.StateStore,
        origin: Path,
        tmp_path: Path,
        isolated_env: dict[str, Path],
        bundle_url_patched: None,
    ) -> None:
        """status() reads store.pending directly (backend/routes.py's own

        `status()` returns `"pending": store.pending` verbatim) -- since a
        partial apply must not resolve pending (test 1 above), the
        operator's next status() call must still show the same sha
        pending, proving the not-applied commit has not silently
        disappeared from view.
        """
        head_sha = _seed_partial_history(origin, tmp_path)
        _seed_pending_for(store, sha=head_sha)

        routes_module.approve(store, head_sha)

        status_result = routes_module.status(store)
        assert status_result.get("status") == "ok"
        assert status_result.get("pending") is not None
        assert status_result["pending"]["sha"] == head_sha

    def test_approve_response_names_the_not_applied_path_and_reason(
        self,
        routes_module: Any,
        store: state.StateStore,
        origin: Path,
        tmp_path: Path,
        isolated_env: dict[str, Path],
        bundle_url_patched: None,
    ) -> None:
        """The approve response itself (not just a later status() call)

        must name mcp.json as not-applied, so the operator sees WHY this
        commit is still pending without a second round trip.
        """
        head_sha = _seed_partial_history(origin, tmp_path)
        _seed_pending_for(store, sha=head_sha)

        result = routes_module.approve(store, head_sha)

        assert "mcp.json" in result.get("not_applied", []), result
        assert "steering/x.md" in result.get("applied", []), result


# ---------------------------------------------------------------------------
# 3. Re-approving after a partial apply is safe -- no duplicate damage.
# ---------------------------------------------------------------------------


class TestReapprovingAfterPartialIsSafe:
    def test_reapproving_the_same_unchanged_partial_commit_is_still_partial(
        self,
        routes_module: Any,
        store: state.StateStore,
        origin: Path,
        tmp_path: Path,
        isolated_env: dict[str, Path],
        bundle_url_patched: None,
    ) -> None:
        """Re-approving the SAME sha again (simulating the operator

        retrying before the upstream fix lands) must be safe: no
        duplicate writes, no crash, and the outcome is still partial
        with pending still set -- exactly the H4 "re-approve is safe"
        clause. The already-applied file's bytes are unchanged by the
        second attempt (a plain re-write of identical content, not
        duplicated content).
        """
        head_sha = _seed_partial_history(origin, tmp_path)
        _seed_pending_for(store, sha=head_sha)

        first = routes_module.approve(store, head_sha)
        assert first.get("outcome") == "partial"

        # Pending must still be set (test 1 above) for a second approve
        # against the identical sha to even be meaningful.
        assert store.pending is not None
        assert store.pending["sha"] == head_sha

        second = routes_module.approve(store, head_sha)

        assert second.get("outcome") == "partial"
        assert "mcp.json" in second.get("not_applied", [])
        assert "steering/x.md" in second.get("applied", [])

        live_x = isolated_env["root_a"] / "steering" / "x.md"
        assert live_x.read_text(encoding="utf-8") == "# x v1\n"

        reloaded = state.load_state()
        assert reloaded.pending is not None
        assert reloaded.pending["sha"] == head_sha

    def test_reapproving_after_the_upstream_fix_lands_fully_applies(
        self,
        routes_module: Any,
        store: state.StateStore,
        origin: Path,
        tmp_path: Path,
        isolated_env: dict[str, Path],
        bundle_url_patched: None,
    ) -> None:
        """H4's own framing: '...re-approve is safe (simulate: approve

        again unchanged) is safe and still partial, no duplicate
        damage; a fully applied approve resolves.' This test provides
        that second half directly: once the upstream fix lands (a new
        commit that corrects mcp.json to valid JSON) and a fresh pending
        record names the FIXED sha, approving it must fully apply and
        resolve pending -- proving the "only applied resolves" half of
        H4 is not merely the absence of resolution, but a genuine
        reachable success path once the defect is actually fixed.
        """
        head_sha = _seed_partial_history(origin, tmp_path)
        _seed_pending_for(store, sha=head_sha)
        partial_result = routes_module.approve(store, head_sha)
        assert partial_result.get("outcome") == "partial"
        assert store.pending is not None  # still pending (H4)

        fixed_sha = _fix_mcp_json_and_push(origin, tmp_path)
        # A fresh poll tick would accumulate the fix into pending, moving
        # pending["sha"] to the fixed commit while classified_paths still
        # names mcp.json (now valid) for reclassification.
        store.accumulate_pending(
            sha=fixed_sha,
            author="Author <a@example.com>",
            subject="fix mcp.json",
            classified_paths={"mcp.json": "live_on_next_resolution"},
        )
        assert store.pending["sha"] == fixed_sha

        final_result = routes_module.approve(store, fixed_sha)

        assert final_result.get("outcome") == "applied", final_result
        assert final_result.get("status") == "ok"

        live_mcp = isolated_env["root_a"] / "mcp.json"
        assert live_mcp.is_file()

        reloaded = state.load_state()
        assert (
            reloaded.pending is None
        ), "H4 (ratified): only a fully applied approve resolves pending"
        assert reloaded.base_sha == fixed_sha
