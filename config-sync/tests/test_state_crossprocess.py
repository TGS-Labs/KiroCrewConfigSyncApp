"""FAILING tests for senior-review C1 (state.py / server.py cross-process

staleness) and M4 (state.py:266 `last_push_failure` never cleared on a
successful push).

## C1 — the gap

`backend/server.py` caches ONE `StateStore` for the server process's
entire lifetime (`_store`, built once by `_get_store()`). `poll.py` and
`push.py` are separate CRON PROCESSES: each calls `state.load_state()`
fresh, mutates, and lets `StateStore._save()` write straight to disk with
no cross-process coordination at all. Three consequences, each with its
own test below:

1. A pending commit `poll.py` records on-disk (via its own, separate
   `StateStore`) after the server started is invisible to `GET status`,
   because the server never re-reads the file — it serves its own
   in-memory copy forever.
2. The Kiro-Config-Bundles#65 stale-sha check
   (`StateStore.resolve_pending`, called from `routes.approve`/
   `routes.decline`) runs against the SERVER's stale in-memory `pending`,
   not the newest on-disk record — so a genuinely-stale approve can be
   wrongly ACCEPTED (the server's cache still shows the old, matching
   sha) and a genuinely-current approve can be wrongly REFUSED (the
   server's cache is behind what the operator was actually shown).
3. `resolve_pending`'s write is a read-modify-write over the FULL
   payload dict (`self._payload`) — approve/decline calling it against a
   stale in-memory `_payload` clobbers fields a concurrent poll/push
   process wrote in the meantime (`last_seen_sha`, `last_pushed_hash`,
   `pending_pr`, ...), because the server's write silently reverts them
   to whatever stale value it already held.

There is also no lock inside `state.py` itself: `_atomic_write_json`'s
`os.replace` only protects against a READER observing a half-written
file — it does nothing to stop two `StateStore` instances (two
processes, or two objects in one process standing in for two processes)
from each doing load -> mutate -> save with the second save blowing
away the first's update. The last test in this file drives exactly that
interleaving.

## Interface this file assumes (not yet true)

- `backend.server` exposes `build_server(host, port)` (already true) and
  a way for the test to make the running server's `_get_store()` see a
  freshly-reloaded-from-disk `StateStore` on every request — e.g.
  `_get_store()` reloads on every call, or reloads when the on-disk
  file's mtime has advanced since the cached copy was built. This file
  does not prescribe which; it only asserts the OBSERVABLE effect: a
  second process's disk write becomes visible to the next request, and
  a read-modify-write the server performs never silently reverts a
  concurrent writer's field.
- `backend.state` exposes a cross-process file lock a read-modify-write
  helper (`resolve_pending`, or an equivalent used by the server) holds
  for the full load-mutate-save span, so two interleaved `StateStore`
  instances against the SAME on-disk file never lose an update.

## M4 — the gap

`StateStore.record_push_success` (state.py, the `record_push_success`
def starting at the line senior-review cites as `state.py:266`) never
clears `_payload["last_push_failure"]`, unlike `clear_poll_failure`'s
already-correct pairing for the poll side. A push that fails once and
then SUCCEEDS still reports the earlier failure in `GET status` forever
(`status()` in routes.py surfaces `store.last_push_failure` verbatim).
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Tuple

import pytest

from backend import state as state_module

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_state_dir(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Isolates the app's own state dir AND both tracked configuration

    roots (`KIROCREW_HOME`/`KIRO_HOME`) `routes.status`/`routes.drift`
    walk via `collect.collect()` under the hood — unset, these default
    to the real `~/.kiro/crew`/`~/.kiro`, which on a real dev machine is
    large enough that a real `GET status` call hangs well past this
    file's HTTP client timeouts (`tests/test_routes.py`'s own
    `isolated_state_dir` fixture isolates the same three surfaces for the
    same reason).
    """
    state_dir = tmp_path / "config-sync-state"
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(state_dir))

    root_a = tmp_path / "kiro-crew-home"
    root_b = tmp_path / "kiro-home"
    root_a.mkdir(parents=True, exist_ok=True)
    root_b.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("KIROCREW_HOME", str(root_a))
    monkeypatch.setenv("KIRO_HOME", str(root_b))

    yield state_dir


@pytest.fixture
def running_server(
    isolated_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Tuple[str, int]]:
    """Starts the REAL `backend/server.py` HTTP server on 127.0.0.1 with

    an ephemeral port, in a background thread — same shape as
    `tests/test_server.py`'s own `running_server` fixture — but does NOT
    patch `backend.routes.*`: these tests need the server's real
    `_get_store()` wiring and real `routes.status`/`routes.approve`/
    `routes.decline` behaviour against the on-disk state file, since the
    bug under test is specifically about which `StateStore` object the
    server consults.
    """
    import importlib

    import backend.server as server_module
    from backend import routes as routes_module

    importlib.reload(server_module)
    # These tests exercise real `routes.*` bodies (never spied), so the
    # enabled-gate every route calls first (`_require_enabled`) must be
    # forced open the same way `test_routes.py`/`test_routes_restore.py`
    # do — otherwise every request 200s with `{"status": "error",
    # "reason": "config-sync is disabled"}`, which is a real refusal
    # this file is not testing, not the cross-process bug under test.
    monkeypatch.setattr(routes_module, "is_app_enabled", lambda _name: True)

    httpd = server_module.build_server(host="127.0.0.1", port=0)
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


def _request(
    host: str,
    port: int,
    method: str,
    path: str,
    body: bytes | None = None,
) -> Tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        # H7: every mutating request must carry this header; these tests
        # exercise cross-process state, not the CSRF guard itself
        # (tests/test_server_security.py owns that).
        headers_out = {"X-Config-Sync-Request": "1"} if method == "POST" else {}
        conn.request(method, path, body=body, headers=headers_out)
        resp = conn.getresponse()
        payload = resp.read()
        headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, headers, payload
    finally:
        conn.close()


def _second_process_store() -> "state_module.StateStore":
    """A second `StateStore`, standing in for the separate poll/push cron

    process — built via the SAME `state.load_state()` entry point a real
    cron invocation uses, against the SAME on-disk file the server
    fixture is pointed at (`CONFIG_SYNC_STATE_DIR` is already set by
    `isolated_state_dir`).
    """
    return state_module.load_state()


# ---------------------------------------------------------------------------
# C1.i — a second process's pending write is invisible to GET status
# ---------------------------------------------------------------------------


def test_status_sees_pending_written_by_second_process_after_server_start(
    running_server: Tuple[str, int],
) -> None:
    """The server starts (and lazily builds its cached `_store`) BEFORE

    the second "poll process" StateStore writes a pending record to the
    same on-disk file. `GET status` must reflect that write — proving the
    server does not serve a permanently-stale in-memory copy.

    Mutation this catches: a `_get_store()` that builds `_store` once and
    never reloads passes the fixture's own `/health` warm-up (which is
    exactly what triggers the first, empty-state load) and then returns
    that same empty-`pending` snapshot forever.
    """
    host, port = running_server

    # Force the server to have already built (and cached) its store by
    # making one real request before the second process writes.
    status_code, _headers, body = _request(
        host, port, "GET", "/api/apps/config-sync/status"
    )
    assert status_code == 200
    assert json.loads(body)["pending"] is None

    poll_store = _second_process_store()
    poll_store.set_pending(
        sha="deadbeef" * 5,
        author="poller",
        subject="a change poll noticed",
        classified_paths={"some/file.md": "instant"},
    )

    status_code, _headers, body = _request(
        host, port, "GET", "/api/apps/config-sync/status"
    )
    assert status_code == 200
    parsed = json.loads(body)
    assert parsed["pending"] is not None
    assert parsed["pending"]["sha"] == "deadbeef" * 5


# ---------------------------------------------------------------------------
# C1.ii — the #65 stale-sha check must run against the newest on-disk sha
# ---------------------------------------------------------------------------


def test_approve_with_sha_written_by_second_process_is_accepted(
    running_server: Tuple[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pending record written by the second process AFTER the server's

    store was already cached must still be approvable by ITS sha — the
    #65 staleness check must consult the newest on-disk `pending`, not
    the server's stale in-memory one. Approve is routed through
    `routes.approve`, which calls `apply_commit`; stub that out so this
    test isolates the STALENESS CHECK (state visibility) from the apply
    pipeline itself, which is covered elsewhere.
    """
    from backend import routes as routes_module

    def _fake_apply_commit(**kwargs: Any) -> Any:
        class _Result:
            outcome = "applied"
            applied: list[str] = []
            not_applied: list[str] = []
            ignored_paths: list[str] = []
            dropped_cron_names: list[str] = []
            paused_cron_names: list[str] = []
            changed_instance_names: list[str] = []
            incomplete_registrations: dict[str, Any] = {}
            needs_credential: list[str] = []
            propagation = None
            apply_id = "apply-test"
            reason = ""

        return _Result()

    monkeypatch.setattr(routes_module, "apply_commit", _fake_apply_commit)
    monkeypatch.setattr(
        routes_module,
        "_materialize_pending_commit",
        lambda store, sha: (Path("/nonexistent"), {}, {}),
    )

    host, port = running_server

    # Warm the server's cache with an EMPTY store first.
    _request(host, port, "GET", "/api/apps/config-sync/status")

    fresh_sha = "cafef00d" * 5
    poll_store = _second_process_store()
    poll_store.set_pending(
        sha=fresh_sha,
        author="poller",
        subject="fresh commit",
        classified_paths={"a.md": "instant"},
    )

    status_code, _headers, body = _request(
        host, port, "POST", f"/api/apps/config-sync/pending/{fresh_sha}/approve"
    )
    assert status_code == 200
    parsed = json.loads(body)
    assert parsed["status"] == "ok", parsed


def test_approve_with_stale_sha_after_second_process_advanced_pending_is_refused(
    running_server: Tuple[str, int],
) -> None:
    """The mirror case: the server's cache holds an OLDER pending sha; a

    second process (poll) has since accumulated a NEWER commit into
    pending. Approving the OLD sha must be refused — never silently
    accepted against memory that no longer matches what the operator
    would actually be shown if they reloaded the UI.
    """
    host, port = running_server

    old_sha = "111111111111111111111111111111111111ab"
    poll_store = _second_process_store()
    poll_store.set_pending(
        sha=old_sha,
        author="poller",
        subject="first commit",
        classified_paths={"a.md": "instant"},
    )

    # Server observes the old pending sha (populating whatever cache it
    # keeps) via a status read.
    status_code, _headers, body = _request(
        host, port, "GET", "/api/apps/config-sync/status"
    )
    assert json.loads(body)["pending"]["sha"] == old_sha

    # A second process tick advances pending to a NEWER commit.
    new_sha = "222222222222222222222222222222222222cd"
    poll_store2 = _second_process_store()
    poll_store2.accumulate_pending(
        sha=new_sha,
        author="poller",
        subject="second commit",
        classified_paths={"b.md": "instant"},
    )

    # Approving the now-stale OLD sha must be refused, not accepted
    # against the server's outdated cache.
    status_code, _headers, body = _request(
        host, port, "POST", f"/api/apps/config-sync/pending/{old_sha}/approve"
    )
    parsed = json.loads(body)
    assert parsed.get("status") == "error", parsed


# ---------------------------------------------------------------------------
# C1.iii — approve/decline must not clobber a concurrent writer's fields
# ---------------------------------------------------------------------------


def test_decline_does_not_clobber_fields_a_concurrent_process_wrote_meanwhile(
    running_server: Tuple[str, int],
) -> None:
    """Sequence: server reads state (pending present, `last_pushed_hash`

    is None) -> a second process (push) records a successful push,
    setting `last_pushed_hash` -> the operator declines the pending
    commit. The decline's read-modify-write must NOT revert
    `last_pushed_hash` back to `None` — it must only ever touch
    `pending`/`base_sha`, the fields `resolve_pending` actually owns.

    This is the concrete shape of "approve/decline save the whole stale
    document back" from the finding: a naive implementation that reads
    once into a long-lived in-memory `_payload` and later writes that
    WHOLE dict back overwrites every field a concurrent process changed
    in between, not just the two fields this call means to change.
    """
    host, port = running_server

    sha = "3333333333333333333333333333333333333e"
    poll_store = _second_process_store()
    poll_store.set_pending(
        sha=sha,
        author="poller",
        subject="a change",
        classified_paths={"a.md": "instant"},
    )

    # Server observes pending, populating its own cache/copy.
    _request(host, port, "GET", "/api/apps/config-sync/status")

    # Concurrent push process (a THIRD store instance — push.py's own
    # separate cron invocation) records a successful push in between.
    push_store = _second_process_store()
    push_store.record_push_success(
        tree_hash="pushedhash123",
        branch="config-sync/push",
        pr_url="https://example.invalid/pr/1",
    )

    status_code, _headers, body = _request(
        host, port, "POST", f"/api/apps/config-sync/pending/{sha}/decline"
    )
    assert json.loads(body).get("status") == "ok"

    # Re-read from disk directly (ground truth, bypassing the server's
    # own possibly-stale cache) to confirm the push process's write
    # survived the server's decline write.
    verify_store = _second_process_store()
    assert verify_store.last_pushed_hash == "pushedhash123"


# ---------------------------------------------------------------------------
# Cross-process file lock: no update lost under a load/mutate/save race
# ---------------------------------------------------------------------------


def test_interleaved_state_store_saves_never_lose_an_update(
    isolated_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two `StateStore` instances (standing in for two real OS processes)

    against the SAME on-disk file, with their load/mutate/save spans
    deliberately interleaved via a monkeypatched pause between load and
    save. Neither instance's update may be lost: a cross-process file
    lock around the read-modify-write span is required to serialize the
    two saves so the second one is based on the first one's already-
    written result, not on the stale payload it loaded before the first
    save landed.

    Interleaving forced: instance A loads, then a barrier hands control
    to instance B, which loads (sees the pre-A payload), mutates, and
    saves — WITHOUT a lock, this already loses nothing yet (B saved
    first). The loss happens when A, which loaded BEFORE B saved, now
    saves its own mutation: with no lock, A's save is a full-payload
    write of A's stale in-memory copy, silently reverting B's write. A
    correct cross-process lock makes A's mutate-then-save happen only
    after re-acquiring the lock and re-reading the current on-disk value
    (or otherwise ensures B's write is never reverted) — this test does
    not prescribe the mechanism, only the observable outcome: both
    updates are present afterward.
    """
    import queue

    result_queue: "queue.Queue[str]" = queue.Queue()
    b_may_proceed = threading.Event()
    a_loaded = threading.Event()

    def instance_a() -> None:
        store = state_module.load_state()
        a_loaded.set()
        # Give B a chance to load-mutate-save before A saves, so A's
        # in-memory payload is provably older than what's on disk by
        # the time A calls set_pending/_save.
        b_may_proceed.wait(timeout=5)
        store.set_pending(
            sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            author="process-a",
            subject="A's commit",
            classified_paths={"a.md": "instant"},
        )
        result_queue.put("a-done")

    def instance_b() -> None:
        a_loaded.wait(timeout=5)
        store = state_module.load_state()
        store.record_seen_sha("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
        b_may_proceed.set()
        result_queue.put("b-done")

    thread_a = threading.Thread(target=instance_a)
    thread_b = threading.Thread(target=instance_b)
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=10)
    thread_b.join(timeout=10)

    assert result_queue.qsize() == 2

    final_store = state_module.load_state()
    assert final_store.pending is not None
    assert (
        final_store.pending["sha"] == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    ), "A's own update to its own field must never be lost"
    assert (
        final_store.last_seen_sha == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    ), "B's update must survive A's later save — this is the field a missing lock loses"


# ---------------------------------------------------------------------------
# M4 — last_push_failure must clear on a subsequent successful push
# ---------------------------------------------------------------------------


def test_record_push_success_clears_a_prior_last_push_failure() -> None:
    """`state.py:266`'s `record_push_success` must clear

    `last_push_failure`, mirroring `clear_poll_failure`'s already-correct
    behaviour for the poll side. Without this, `GET status` keeps
    reporting a push failure that has since been superseded by a success,
    with no way for an operator to tell "still failing" from "recovered".
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.json"
        store = state_module.StateStore(_path=path)

        store.record_push_failure(reason="network unreachable")
        assert store.last_push_failure is not None

        store.record_push_success(
            tree_hash="deadbeef",
            branch="config-sync/push",
            pr_url="https://example.invalid/pr/2",
        )

        assert store.last_push_failure is None
