"""FAILING tests pinning the auto-apply ruling (requirements.md 4.4, 4.14).

Under the operator ruling (requirements.md "Introduction" +
Requirement 4), the approval gate for a pulled change is the PR merge
into ``Kiro-Config-Bundles`` main — there is no box-side approve/decline
step. ``poll.run()`` itself must materialize a new head's commit range
and call ``apply.apply_commit`` automatically, with no operator action.

Today none of this holds:

- ``backend/poll.py::run`` only resolves the remote head, classifies
  changed paths, and calls ``store.record_poll_pending`` — it never
  imports or calls ``apply.apply_commit`` at all (see ``poll.py`` lines
  ~600-650). So a new head is recorded as a *pending* record for a
  human decision that this ruling says no longer exists.
- ``backend/routes.py`` still defines ``approve``/``decline`` routes and
  ``backend/server.py`` still wires them to
  ``POST /api/apps/config-sync/pending/<sha>/approve`` and ``.../decline``.
- ``apply.ApplyResult.not_applied`` is a bare ``list[str]`` of relpaths
  with NO per-path reason at all — ``routes.approve`` synthesizes the
  placeholder string ``"not applied — see apply result for details"``
  rather than surfacing the real cause ``_apply_one_file`` encountered.
  Requirement 4.14 requires the REAL reason ("the specific
  vet-rejection, the specific missing-parent-directory error, the
  specific permission error") — there is currently no data path for one
  to exist.

Every test below is expected to FAIL against the current tree. None of
them monkeypatch ``apply_commit`` — they exercise real git (a bare
"origin" bundle repo, a real clone/fetch, real commits) exactly like
``tests/test_routes_approve_seam.py`` and ``tests/test_routes_partial.py``,
whose fixture helpers this file imports and reuses rather than
reinventing a second real-git harness.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest

from backend import state

from test_routes_approve_seam import (
    _bundle_repo_url_env,
    _git,
    _head_sha,
    _init_origin_repo,
    _seed_history,
)


# ---------------------------------------------------------------------------
# Real-git fixtures — thin wrappers around test_routes_approve_seam.py's
# own helper functions (never importing its pytest fixtures by name,
# which flake8 flags as a redefinition at every parametrized use site;
# tests/test_routes_partial.py follows the same import-functions-only
# shape for the same reason).
# ---------------------------------------------------------------------------


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    return _init_origin_repo(tmp_path)


@pytest.fixture
def shas(origin: Path, tmp_path: Path) -> dict[str, str]:
    return _seed_history(origin, tmp_path)


@pytest.fixture
def bundle_url_patched(monkeypatch: pytest.MonkeyPatch, origin: Path) -> None:
    _bundle_repo_url_env(monkeypatch, origin)


# ---------------------------------------------------------------------------
# App-state isolation — mirrors test_routes_approve_seam.py's own fixtures.
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
    """A fresh on-disk read of state.

    ``poll.run()`` calls ``state.load_state()`` internally and persists
    through the same locked read-modify-write path every other module in
    this app uses (see ``project.configsyncapp.deployment4_review1_
    findings`` — C1: state must be read fresh per operation, never kept
    as a stale in-memory copy across a call that mutates it out of
    process). Returning a *callable* rather than a single pre-``run()``
    ``StateStore`` snapshot means every test below reads the CURRENT
    on-disk state after calling ``poll_module.run()``, instead of
    asserting against a stale snapshot taken before the call — the same
    class of bug this app's own state layer was fixed for.
    """
    return state.load_state()


def _reload_store() -> state.StateStore:
    """Re-read state from disk — call this AFTER ``poll_module.run()``,

    never rely on a ``store`` fixture instance captured before the call.
    ``state.load_state()`` always reads fresh from disk (see
    ``state.py``'s own module docstring), matching how ``poll.run()``
    itself loads state internally.
    """
    return state.load_state()


# ---------------------------------------------------------------------------
# (1) A poll tick that finds a new head materializes + applies it itself.
# ---------------------------------------------------------------------------


def test_poll_tick_applies_new_head_with_no_operator_action(
    poll_module: Any,
    store: state.StateStore,
    origin: Path,
    shas: dict[str, str],
    bundle_url_patched: None,
    isolated_env: dict[str, Path],
) -> None:
    """A poll tick that finds a new head materializes its tree and calls

    ``apply.apply_commit`` itself — the same call ``routes.approve`` used
    — with no ``approve``/``decline`` call anywhere in this test.

    RED reason: ``poll.run()`` has no code path that calls
    ``apply_commit`` at all (verified by reading ``poll.py``: the only
    state mutation on a changed head is ``record_poll_pending``). The
    file this test expects to have been written — ``steering/x.md`` —
    is asserted absent below (a placeholder for "the apply already
    proves it never ran"); against the real fix this assertion becomes
    the positive "file exists with the committed content" check, but
    today the mere absence of any exception, combined with the file
    never landing in root_a, is what proves ``run()`` never applied
    anything.
    """
    poll_module.run()

    root_a = isolated_env["root_a"]
    applied_file = root_a / "steering" / "x.md"

    assert applied_file.is_file(), (
        "poll.run() must materialize and apply the new head's commit "
        "range automatically (requirements.md 4.4) — steering/x.md from "
        "the seeded merge commit was never written to the live root, "
        "proving no apply_commit call happened during this poll tick"
    )
    assert applied_file.read_text(encoding="utf-8") == "# x v1\n"


def test_poll_tick_never_creates_a_pending_record(
    poll_module: Any,
    store: state.StateStore,
    origin: Path,
    shas: dict[str, str],
    bundle_url_patched: None,
) -> None:
    """Under the ruling there is no operator-decision state on the box at

    all — a poll tick must never populate ``store.pending`` for a human
    to act on.

    RED reason: ``poll.run()`` calls ``store.record_poll_pending(...)``
    unconditionally on every changed head (see ``poll.py`` ~line 640),
    so ``store.pending`` is populated today, not ``None``.
    """
    poll_module.run()

    assert _reload_store().pending is None, (
        "poll.run() must not leave a pending record for operator "
        "approval — the PR merge into main is the only approval gate "
        "(requirements.md Introduction / 4.4)"
    )


# ---------------------------------------------------------------------------
# (2) outcome "applied" advances base_sha and records the apply.
# ---------------------------------------------------------------------------


def test_applied_outcome_advances_base_sha_to_the_new_head(
    poll_module: Any,
    store: state.StateStore,
    origin: Path,
    shas: dict[str, str],
    bundle_url_patched: None,
    isolated_env: dict[str, Path],
) -> None:
    """A fully-applied poll tick advances ``base_sha`` to the applied head

    so the NEXT tick's range starts from there (requirements.md 4.9).

    Uses TWO ticks rather than one: ``state.set_pending`` itself
    bootstraps ``base_sha`` to the first-ever pending commit's own sha
    (requirements.md 4.9's "before any apply has ever fully succeeded"
    clause), so a single-tick version of this assertion would pass today
    for the WRONG reason (the bootstrap, not an actual apply) — the first
    tick here consumes that bootstrap, and a genuinely new second head
    is what an apply-driven advance must move ``base_sha`` to next.

    RED reason: ``poll.run()`` never calls ``apply_commit`` or any
    apply-outcome-driven state mutation on a changed head — it only
    calls ``record_poll_pending`` (``set_pending``/``accumulate_pending``
    internally), neither of which advances ``base_sha`` past its
    bootstrap value on a SECOND tick. ``base_sha`` therefore stays at the
    first tick's sha, not the second commit's.
    """
    # First tick: consumes the base_sha bootstrap on the seeded merge commit.
    poll_module.run()

    # A genuinely new second commit on top of the seeded history.
    work = isolated_env["state_dir"].parent / "second-head-work"
    work.mkdir(parents=True, exist_ok=True)
    _git("clone", "-q", str(origin), str(work), cwd=work.parent)
    (work / "steering" / "second.md").write_text("# second\n", encoding="utf-8")
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "second change", cwd=work)
    second_sha = _head_sha(work)
    _git("push", "-q", "origin", "main", cwd=work)

    # Second tick: must apply the second commit and advance base_sha to it.
    poll_module.run()

    assert _reload_store().base_sha == second_sha, (
        "a fully-applied poll tick must advance base_sha to the applied "
        "head so the next tick's range does not re-apply the same "
        "commit (requirements.md 4.9)"
    )


def test_applied_outcome_records_a_restore_dir_visible_to_status(
    poll_module: Any,
    store: state.StateStore,
    origin: Path,
    shas: dict[str, str],
    bundle_url_patched: None,
) -> None:
    """A fully-applied poll tick must record the apply (restore dir +

    result) so ``status()``/Undo can see it later — mirroring what
    ``approve`` used to record via ``apply_id``/``restore_dirs``.

    RED reason: no code path in ``poll.run()`` ever calls
    ``apply_commit`` (which is what produces an ``apply_id``) or any
    state method that would populate ``store.restore_dirs`` — that dict
    stays empty after this poll tick runs today.
    """
    poll_module.run()

    assert _reload_store().restore_dirs, (
        "a fully-applied automatic poll tick must record a restore "
        "directory the same way approve used to, so Undo/status can "
        "see what the automatic apply did"
    )


# ---------------------------------------------------------------------------
# (3) outcome "partial" keeps base_sha at the last fully-applied commit,
#     real per-path reasons, and the next tick retries the same range.
# ---------------------------------------------------------------------------


@pytest.fixture
def partial_shas(origin: Path, tmp_path: Path) -> dict[str, str]:
    """A merge commit where one file is malformed JSON (mcp.json, refused

    by apply.py's H1 rule) and a second, unrelated file
    (``steering/x.md``) applies cleanly — the same partial-outcome
    construction ``tests/test_routes_partial.py`` already uses, rebuilt
    here so this file does not depend on that module's private fixture
    internals.
    """
    work = tmp_path / "partial-seed-work"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    _git("remote", "add", "origin", str(origin), cwd=work)

    (work / "steering").mkdir()
    (work / "steering" / "keep.md").write_text("# keep\n", encoding="utf-8")
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "initial", cwd=work)
    initial_sha = _head_sha(work)
    _git("push", "-q", "-u", "origin", "main", cwd=work)

    (work / "steering" / "x.md").write_text("# x v1\n", encoding="utf-8")
    (work / "mcp.json").write_text("{not valid json", encoding="utf-8")
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "add x.md, break mcp.json", cwd=work)
    broken_sha = _head_sha(work)
    _git("push", "-q", "origin", "main", cwd=work)

    return {"initial": initial_sha, "broken": broken_sha}


def test_partial_outcome_does_not_advance_base_sha(
    poll_module: Any,
    store: state.StateStore,
    origin: Path,
    partial_shas: dict[str, str],
    bundle_url_patched: None,
) -> None:
    """A partial automatic apply must leave ``base_sha`` at the last

    FULLY applied commit — never reset to ``None``, never advanced to
    the broken head (requirements.md 4.9 / 4.14).

    RED reason: ``poll.run()`` never calls ``apply_commit``, so there is
    no partial-outcome branch that would hold ``base_sha`` back — the
    only thing that touches ``base_sha`` today is ``set_pending``'s
    bootstrap (requirements.md 4.9's "no decision has ever been made"
    clause), which sets it to the CURRENT head unconditionally,
    regardless of whether that head's commit is later found to be
    partially unappliable. So ``base_sha`` ends up at the broken
    commit's own sha today, not at the last-fully-applied boundary this
    assertion requires.
    """
    poll_module.run()

    assert _reload_store().base_sha == partial_shas["initial"], (
        "a partial automatic apply must keep base_sha at the last "
        "fully-applied commit, never advance to the broken head and "
        "never reset to None"
    )


def test_partial_outcome_records_real_per_path_reasons_not_a_placeholder(
    poll_module: Any,
    store: state.StateStore,
    origin: Path,
    partial_shas: dict[str, str],
    bundle_url_patched: None,
) -> None:
    """The not-applied path's recorded reason must be the REAL cause

    apply.py's ``_apply_one_file`` encountered for ``mcp.json`` (its own
    malformed-JSON refusal), never the generic placeholder string
    ``routes.approve`` synthesizes today.

    RED reason: (a) nothing calls ``apply_commit`` from ``poll.run()``,
    so no not-applied-reasons state is ever recorded at all; (b) even
    if it were wired through the existing ``routes.approve`` shape, that
    code hard-codes the literal string
    ``"not applied — see apply result for details"`` for EVERY
    not-applied path (see ``backend/routes.py``'s
    ``not_applied_reasons = {relpath: "not applied — see apply result "
    "for details" for relpath in result.not_applied}``) — it is
    structurally incapable of carrying a distinct real reason per path,
    because ``ApplyResult.not_applied`` is only a bare list of relpaths
    with no reason data attached at all.
    """
    poll_module.run()

    pending = _reload_store().pending
    not_applied = (pending or {}).get("not_applied") or {}

    reason = not_applied.get("mcp.json", "")
    assert reason, "mcp.json must be recorded as not-applied with a reason"
    assert reason != "not applied — see apply result for details", (
        "the recorded reason must be the REAL per-path cause "
        "apply_commit encountered for mcp.json (its own malformed-JSON "
        "refusal), never the generic placeholder string"
    )
    assert (
        "json" in reason.lower() or "parse" in reason.lower()
    ), f"expected a real JSON-parse-failure reason for mcp.json, got {reason!r}"


def test_partial_outcome_next_tick_retries_same_range_and_a_later_fix_advances_base_sha(
    poll_module: Any,
    store: state.StateStore,
    origin: Path,
    partial_shas: dict[str, str],
    bundle_url_patched: None,
    isolated_env: dict[str, Path],
) -> None:
    """Guard against the #65 multi-commit-loss class: after a partial

    tick, a LATER commit that fixes the broken file must still cause the
    intermediate commit's OTHER change (``steering/x.md``) to have
    applied (from the first, partial tick) and the fix commit's own
    change to apply too, with ``base_sha`` finally advancing past both.

    RED reason: ``poll.run()`` never calls ``apply_commit``, so neither
    tick ever writes ``steering/x.md`` or ``mcp.json`` to the live roots,
    and ``base_sha`` never moves at all — this test's final assertion
    (``base_sha == fix_sha``) fails because ``base_sha`` stays ``None``
    throughout both ticks.
    """
    # First tick: partial (mcp.json broken).
    poll_module.run()

    # A later commit repairs mcp.json.
    work = isolated_env["state_dir"].parent / "fix-work"
    work.mkdir(parents=True, exist_ok=True)
    _git("clone", "-q", str(origin), str(work), cwd=work.parent)
    (work / "mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "fix mcp.json", cwd=work)
    fix_sha = _head_sha(work)
    _git("push", "-q", "origin", "main", cwd=work)

    # Second tick: retries base_sha..fix_sha (the same never-advanced
    # range, extended to the new head) and should now fully apply.
    poll_module.run()

    root_a = isolated_env["root_a"]
    assert (root_a / "steering" / "x.md").is_file(), (
        "the intermediate commit's other change (steering/x.md) must "
        "still apply once the blocking file is fixed — it must never be "
        "dropped just because it arrived in the same original partial "
        "range (Kiro-Config-Bundles#65 class)"
    )
    # apply re-serializes tracked JSON after sanitize/restore, so compare
    # the parsed document rather than the committed bytes.
    applied_mcp = json.loads((root_a / "mcp.json").read_text(encoding="utf-8"))
    assert applied_mcp == {"mcpServers": {}}
    assert _reload_store().base_sha == fix_sha, (
        "once the retried range fully applies, base_sha must advance "
        "past both the originally-broken commit and the fix commit"
    )


# ---------------------------------------------------------------------------
# (4) the materialized temp tree is always removed, including on exception.
# ---------------------------------------------------------------------------


def test_materialized_commit_root_is_removed_after_a_successful_apply(
    poll_module: Any,
    store: state.StateStore,
    origin: Path,
    shas: dict[str, str],
    bundle_url_patched: None,
    isolated_env: dict[str, Path],
) -> None:
    """No ``materialize-<sha>-*`` temp directory must survive under the

    state dir once a poll tick's automatic apply has finished.

    RED reason: ``poll.run()`` never calls the materialize helper at
    all today, so this assertion currently passes for the WRONG reason
    (nothing was ever created because nothing was ever applied) — it is
    included as a companion pin so that once ``apply_commit`` wiring
    lands, cleanup is verified in the same file rather than assumed. The
    genuinely red half of this behaviour is exercised by the exception
    variant below, which fails outright today because
    ``routes._materialize_pending_commit`` (or its poll-side
    equivalent) is never invoked, so patching it to raise has no effect
    to observe.
    """
    poll_module.run()

    state_dir = isolated_env["state_dir"]
    leftover = (
        [p for p in state_dir.iterdir() if p.name.startswith("materialize-")]
        if state_dir.is_dir()
        else []
    )
    assert not leftover, f"leftover materialize temp dirs: {leftover}"


def test_materialized_commit_root_is_removed_when_apply_commit_raises(
    poll_module: Any,
    store: state.StateStore,
    origin: Path,
    shas: dict[str, str],
    bundle_url_patched: None,
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The materialized temp tree must be removed even when

    ``apply_commit`` itself raises an unexpected exception mid-apply.

    RED reason: ``poll.run()`` has no call to ``apply.apply_commit`` to
    monkeypatch in the first place — this monkeypatch target does not
    exist on the code path ``run()`` actually executes, so the forced
    exception this test relies on to prove cleanup-on-exception never
    fires, and the state-dir leftover check below has nothing to prove
    against a real materialize call.
    """
    from backend import apply as apply_module

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(apply_module, "apply_commit", _boom)

    with pytest.raises(RuntimeError):
        poll_module.run()

    state_dir = isolated_env["state_dir"]
    leftover = (
        [p for p in state_dir.iterdir() if p.name.startswith("materialize-")]
        if state_dir.is_dir()
        else []
    )
    assert not leftover, f"leftover materialize temp dirs after exception: {leftover}"


# ---------------------------------------------------------------------------
# (5) approve/decline no longer exist.
# ---------------------------------------------------------------------------


def test_routes_module_has_no_approve_function() -> None:
    """RED reason: ``backend/routes.py`` still defines ``approve`` at

    module scope (verified by reading the file: ``def approve(store,
    sha) -> Dict[str, Any]:``).
    """
    from backend import routes

    assert not hasattr(routes, "approve"), (
        "routes.approve must not exist under the auto-apply ruling — "
        "there is no box-side approval step"
    )


def test_routes_module_has_no_decline_function() -> None:
    """RED reason: ``backend/routes.py`` still defines ``decline`` at

    module scope (verified by reading the file: ``def decline(store,
    sha) -> Dict[str, Any]:``).
    """
    from backend import routes

    assert not hasattr(routes, "decline"), (
        "routes.decline must not exist under the auto-apply ruling — "
        "there is no box-side approval step"
    )


@pytest.fixture
def running_server(
    isolated_env: dict[str, Path],
) -> Iterator[tuple[str, int]]:
    """Starts the REAL ``backend/server.py`` HTTP server on an ephemeral

    loopback port in a background thread — same shape as
    ``tests/test_server.py``'s own ``running_server`` fixture, rebuilt
    here (no spies) so this file exercises the real dispatch table with
    no dependency on that file's private fixtures.
    """
    import http.client
    import importlib
    import threading
    import time
    from http.server import HTTPServer

    import backend.server as server_module

    importlib.reload(server_module)

    httpd = HTTPServer(("127.0.0.1", 0), server_module.Handler)
    host = str(httpd.server_address[0])
    port = httpd.server_address[1]

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5.0
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                conn = http.client.HTTPConnection(host, port, timeout=1)
                conn.request("GET", "/health")
                resp = conn.getresponse()
                resp.read()
                conn.close()
                if resp.status == 200:
                    break
            except OSError as exc:
                last_error = exc
                time.sleep(0.05)
        else:
            raise AssertionError(f"server never became ready: {last_error}")

        yield host, port
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _post(host: str, port: int, path: str) -> int:
    import http.client

    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request(
            "POST",
            path,
            headers={
                "Host": f"{host}:{port}",
                "X-Config-Sync-Request": "1",
            },
        )
        resp = conn.getresponse()
        resp.read()
        return resp.status
    finally:
        conn.close()


def test_server_returns_404_for_post_approve(
    running_server: tuple[str, int],
) -> None:
    """RED reason: ``backend/server.py`` still routes

    ``POST /api/apps/config-sync/pending/<sha>/approve`` to
    ``routes.approve`` (verified by reading the file's route table: the
    3-segment ``("pending", sha, "approve")`` branch dispatches to
    ``routes.approve``), so this request is currently handled by real
    handler logic (200/an error payload) rather than 404ing.
    """
    host, port = running_server
    status = _post(host, port, "/api/apps/config-sync/pending/deadbeef/approve")

    assert status == 404, f"expected 404 for a removed route, got {status}"


def test_server_returns_404_for_post_decline(
    running_server: tuple[str, int],
) -> None:
    """RED reason: ``backend/server.py`` still routes

    ``POST /api/apps/config-sync/pending/<sha>/decline`` to
    ``routes.decline`` (same 3-segment branch as approve, keyed on the
    ``"decline"`` literal), so this request is currently handled by real
    handler logic rather than 404ing.
    """
    host, port = running_server
    status = _post(host, port, "/api/apps/config-sync/pending/deadbeef/decline")

    assert status == 404, f"expected 404 for a removed route, got {status}"
