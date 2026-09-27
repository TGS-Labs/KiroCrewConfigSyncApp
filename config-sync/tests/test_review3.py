"""FAILING tests pinning senior-review round-3 findings (auto-apply path).

Reuses the real-git fixture helpers from ``tests/test_routes_approve_seam.py``
(``_bundle_repo_url_env``, ``_git``, ``_head_sha``, ``_init_origin_repo``,
``_seed_history``) exactly as ``tests/test_poll_autoapply.py`` already does —
no mocked git/subprocess anywhere below except where a test explicitly spies
on a call site to prove it was (or was not) invoked.

Findings covered:

- C-A: after a poll tick fully applies a commit, the next push tick must be
  a no-op — no clone, no branch, no PR. Today nothing on the auto-apply path
  ever touches ``state.last_pushed_hash``, so ``push.run()`` still sees the
  applied tree as "new" and opens a PR copying main back onto itself.
- H-1: after a PARTIAL apply on head H, the next tick (remote head still H)
  must retry the apply. Today ``poll.run()`` calls
  ``store.record_seen_sha(head_sha)`` unconditionally after
  ``_apply_new_head`` regardless of outcome, so the next tick's
  ``head_sha == store.last_seen_sha`` short-circuit reports "unchanged" and
  the partial apply is never retried (requirements.md 4.14).
- Medium: a multi-commit partial (base A, remote gains B then C in one poll
  range, apply is partial) must leave ``base_sha`` at A — never at B, which
  was never actually fully applied.
- Medium: ``drift``/``status`` (routes.py) hash the redacted-but-NOT-
  tokenized tree, while ``push.py`` hashes the redacted-AND-tokenized tree.
  The two disagree whenever a tracked file contains a portable absolute
  path under a tracked root, so drift can report "drift" immediately after
  a push that just recorded that exact tree as pushed.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Iterator, List, Tuple

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
# own helper functions (never importing its pytest fixtures by name, which
# flake8 flags as a redefinition at every parametrized use site;
# tests/test_poll_autoapply.py follows the same shape for the same reason).
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
def push_module(isolated_env: dict[str, Path]) -> Any:
    from backend import push

    return push


@pytest.fixture
def routes_module(isolated_env: dict[str, Path]) -> Any:
    from backend import routes

    return routes


@pytest.fixture
def store(isolated_env: dict[str, Path]) -> state.StateStore:
    return state.load_state()


def _reload_store() -> state.StateStore:
    """Re-read state from disk — call after any module-level run()."""
    return state.load_state()


# ---------------------------------------------------------------------------
# (1) C-A: a fully-applied poll tick must leave the next push tick a no-op.
# ---------------------------------------------------------------------------


def test_push_is_a_noop_immediately_after_a_full_poll_apply(
    poll_module: Any,
    push_module: Any,
    store: state.StateStore,
    origin: Path,
    shas: dict[str, str],
    bundle_url_patched: None,
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real poll tick that fully applies the seeded merge commit must

    make the immediately-following push tick report ``"no-op"`` with NO
    git subprocess call and NO PR handoff call — the applied tree is
    identical to what a push would collect right now, so there is
    nothing to sync back.

    RED reason: nothing on the auto-apply path
    (``poll._apply_new_head``/``state.resolve_pending``) ever writes
    ``state.last_pushed_hash``. ``push.run()`` gates purely on
    ``current_hash == store.last_pushed_hash`` (see ``push.py``'s own
    docstring, step 3) — with ``last_pushed_hash`` still ``None`` after
    the poll tick, the hashes can never match, so ``push.run()`` falls
    through to the real clone/branch/commit/push/PR-handoff pipeline and
    tries to copy the just-applied tree back onto a new branch, spying
    both a real subprocess git call and a PR handoff call that must
    never happen for content that only just arrived FROM main.
    """
    poll_module.run()

    root_a = isolated_env["root_a"]
    applied_file = root_a / "steering" / "x.md"
    assert applied_file.is_file(), (
        "setup precondition failed: the poll tick did not apply the "
        "seeded commit, so this test cannot exercise the push-after-"
        "apply seam at all"
    )

    # A real push clone/commit needs a resolvable git identity — set it
    # via env exactly like test_routes_approve_seam.py's own _GIT_ENV, so
    # a sandbox with no global git user.name/user.email configured still
    # reaches push.py's own no-op gate rather than failing earlier on an
    # unrelated "Author identity unknown" error from the FIX path this
    # test is trying to prove is unreachable.
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")

    real_run = subprocess.run
    git_calls: List[Tuple[Any, ...]] = []

    def _spy_run(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if argv and isinstance(argv, (list, tuple)) and "git" in str(argv[0]):
            git_calls.append(tuple(argv))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(push_module.subprocess, "run", _spy_run)

    pr_handoff_calls: List[Any] = []
    from backend import pr_handoff as pr_handoff_module

    def _spy_handoff(*args: Any, **kwargs: Any) -> None:
        pr_handoff_calls.append((args, kwargs))

    monkeypatch.setattr(pr_handoff_module, "handle_pushed_branch", _spy_handoff)

    # push.run() is expected to short-circuit at its no-op gate and never
    # reach a git call at all. Today it falls through to the real
    # clone/commit/push pipeline and dies on `git commit` finding nothing
    # to commit (the content is already identical to origin/main) — that
    # CalledProcessError IS the bug (a fabricated push failure for
    # content that needed no push), so it is caught here and turned into
    # an explicit failure naming the outcome this test actually wants,
    # rather than surfacing as an opaque subprocess traceback.
    try:
        result = push_module.run()
    except subprocess.CalledProcessError as exc:
        pytest.fail(
            "push.run() must short-circuit to a no-op before any git "
            "call when the applied tree already matches what push would "
            "push — instead it ran a real git subprocess that failed "
            f"({exc}), proving last_pushed_hash was never set by the "
            "poll apply"
        )

    assert result.outcome == "no-op", (
        f"expected push.run() to be a no-op immediately after a full "
        f"poll apply of identical content, got outcome={result.outcome!r}"
    )
    assert not git_calls, (
        "push.run() must not invoke any git subprocess when the applied "
        f"tree already matches last_pushed_hash — got calls: {git_calls}"
    )
    assert not pr_handoff_calls, (
        "push.run() must not hand off to pr_handoff (branch/PR) for "
        "content that only just arrived FROM main via the poll tick's "
        "own auto-apply"
    )


def test_last_pushed_hash_is_set_by_the_poll_apply_itself(
    poll_module: Any,
    push_module: Any,
    store: state.StateStore,
    origin: Path,
    shas: dict[str, str],
    bundle_url_patched: None,
) -> None:
    """After a full poll apply, ``state.last_pushed_hash`` must already

    equal the tree hash ``push.run()`` would compute for the live tree
    right now — the same tokenized hash push's own no-op gate reads.

    RED reason: no code path reachable from ``poll.run()`` ever calls
    ``state.record_branch_pushed``/``record_push_success`` or otherwise
    sets ``last_pushed_hash`` — it stays ``None`` after a poll tick that
    only ever touches ``base_sha``/``pending``/``last_seen_sha``/
    ``last_apply`` (verified by reading ``state.py``: those are the only
    setters ``poll.py`` calls).
    """
    poll_module.run()

    collected = push_module.collect.collect()
    redacted = push_module.redact.redact(collected)
    tokenized, _ = push_module._tokenize_tree_with_report(redacted)
    expected_hash = push_module.tree_hash(tokenized)

    reloaded = _reload_store()
    assert reloaded.last_pushed_hash == expected_hash, (
        "a fully-applied poll tick must record last_pushed_hash as the "
        "hash of the tree it just applied, so the next push tick's "
        "no-op gate actually fires instead of re-pushing main's own "
        "content back onto itself"
    )


# ---------------------------------------------------------------------------
# (2) H-1: a partial apply on head H must be retried by the next tick
#     while the remote head is still H.
# ---------------------------------------------------------------------------


@pytest.fixture
def partial_shas(origin: Path, tmp_path: Path) -> dict[str, str]:
    """A merge-free commit where one file is malformed JSON (refused by

    apply.py's own JSON-parse gate) and a second, unrelated file applies
    cleanly — the minimal construction for a "partial" ``ApplyResult``.
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


def test_partial_apply_is_retried_by_the_next_tick_at_the_same_head(
    poll_module: Any,
    store: state.StateStore,
    origin: Path,
    partial_shas: dict[str, str],
    bundle_url_patched: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tick that partially applies head H, followed by a second tick

    where the remote head is STILL H (nothing new pushed), must call
    ``apply.apply_commit`` a SECOND time against H — the retry
    requirements.md 4.14 requires for a still-failing partial apply.

    RED reason: ``poll.run()`` calls ``store.record_seen_sha(head_sha)``
    unconditionally after ``_apply_new_head`` returns (see ``poll.py``:
    the call sits after the ``if not materialized`` early-return, with no
    branch on ``result.outcome`` at all) — so after the FIRST tick,
    ``store.last_seen_sha == broken_sha``. The second tick's own
    ``head_sha == store.last_seen_sha`` short-circuit (the very first
    check in ``run()``) then fires and returns ``outcome="unchanged"``
    before ever reaching ``_apply_new_head`` again — ``apply_commit`` is
    called exactly ONCE across both ticks, not twice.
    """
    from backend import apply as apply_module

    apply_calls: List[str] = []
    real_apply_commit = apply_module.apply_commit

    def _spy_apply_commit(*args: Any, **kwargs: Any) -> Any:
        apply_calls.append(str(kwargs.get("approved_sha", "")))
        return real_apply_commit(*args, **kwargs)

    monkeypatch.setattr(apply_module, "apply_commit", _spy_apply_commit)
    # poll.py imports apply as `apply_module` at module scope — patch the
    # exact reference `_apply_new_head` actually calls through.
    from backend import poll as poll_module_ref

    monkeypatch.setattr(poll_module_ref.apply_module, "apply_commit", _spy_apply_commit)

    first = poll_module.run()
    assert first.outcome == "changed", (
        f"setup precondition failed: expected the first tick to report "
        f"'changed', got {first.outcome!r}"
    )
    assert apply_calls == [partial_shas["broken"]], (
        "setup precondition failed: the first tick did not call "
        "apply_commit against the broken head exactly once"
    )

    second = poll_module.run()

    assert apply_calls.count(partial_shas["broken"]) == 2, (
        "a poll tick whose remote head is unchanged from a still-"
        f"partially-applied commit must retry apply_commit — got "
        f"apply_commit called for shas {apply_calls}, outcome of the "
        f"second tick was {second.outcome!r}"
    )


def test_last_seen_sha_does_not_advance_on_a_partial_apply(
    poll_module: Any,
    store: state.StateStore,
    origin: Path,
    partial_shas: dict[str, str],
    bundle_url_patched: None,
) -> None:
    """Companion pin for H-1: after a single partial-apply tick,

    ``state.last_seen_sha`` must NOT equal the broken head — advancing it
    is exactly what makes the next tick's ``unchanged`` short-circuit
    swallow the retry.

    RED reason: ``poll.run()`` calls ``store.record_seen_sha(head_sha)``
    unconditionally regardless of ``result.outcome`` (verified by reading
    ``poll.py``: no ``if result.outcome == "applied"`` guard exists around
    that call) — so ``last_seen_sha`` is set to the broken head's sha even
    though the apply was only partial.
    """
    poll_module.run()

    reloaded = _reload_store()
    assert reloaded.last_seen_sha != partial_shas["broken"], (
        "last_seen_sha must not advance to a head whose apply was only "
        "partial — doing so is what defeats the next tick's retry "
        "(requirements.md 4.14)"
    )


# ---------------------------------------------------------------------------
# (3) Medium: multi-commit partial keeps base_sha at the pre-tick base,
#     never at an intermediate commit that was never itself fully applied.
# ---------------------------------------------------------------------------


@pytest.fixture
def multi_commit_partial_shas(origin: Path, tmp_path: Path) -> dict[str, str]:
    """Base commit A (fully seeded as the pre-existing boundary), then

    TWO more commits — B (adds an unrelated clean file) and C (breaks
    mcp.json) — pushed to origin BEFORE the first poll tick ever runs, so
    a single tick's range covers A..C in one classify/apply call and the
    resulting ``ApplyResult`` is partial (C's break is in-range).
    """
    work = tmp_path / "multi-seed-work"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    _git("remote", "add", "origin", str(origin), cwd=work)

    (work / "steering").mkdir()
    (work / "steering" / "keep.md").write_text("# keep\n", encoding="utf-8")
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "A: initial", cwd=work)
    a_sha = _head_sha(work)
    _git("push", "-q", "-u", "origin", "main", cwd=work)

    (work / "steering" / "b.md").write_text("# b\n", encoding="utf-8")
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "B: adds steering/b.md", cwd=work)
    b_sha = _head_sha(work)
    _git("push", "-q", "origin", "main", cwd=work)

    (work / "mcp.json").write_text("{not valid json", encoding="utf-8")
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "C: breaks mcp.json", cwd=work)
    c_sha = _head_sha(work)
    _git("push", "-q", "origin", "main", cwd=work)

    return {"a": a_sha, "b": b_sha, "c": c_sha}


def test_multi_commit_partial_base_sha_never_lands_on_intermediate_commit(
    poll_module: Any,
    store: state.StateStore,
    origin: Path,
    multi_commit_partial_shas: dict[str, str],
    bundle_url_patched: None,
) -> None:
    """A single tick whose range covers TWO new commits (B then C) where

    the apply is partial (C's break is in-range) must leave ``base_sha``
    at exactly the commit BEFORE this tick's own range even started — B
    was never itself independently applied and must never be recorded as
    the boundary, since a later retry ranging from B would silently skip
    re-attempting B's own file (Kiro-Config-Bundles#65's class: an
    intermediate commit inside a not-fully-applied range is not a valid
    boundary).

    RED reason: ``_apply_new_head``'s partial-outcome correction only
    fires when ``store.base_sha == head_sha`` (the very-first-tick
    bootstrap case) — here this is NOT the first-ever tick (``base_sha``
    already names commit A from an earlier successful run before B/C
    were even pushed), so that correction branch is skipped entirely and
    ``record_poll_pending``'s own bootstrap-free path leaves ``base_sha``
    exactly where it already was... EXCEPT the fixture's own pre-tick
    state has no prior boundary recorded yet either (no earlier poll tick
    ever ran), so ``record_poll_pending`` (called before the apply) sets
    ``base_sha = head_sha`` (= C) unconditionally on this very first
    call, and the partial-outcome correction path then walks back only
    ONE parent (to B) via ``_first_parent_parent_sha`` — landing on B,
    the intermediate commit that was itself never independently and
    fully applied, not on A.
    """
    poll_module.run()

    reloaded = _reload_store()
    assert reloaded.base_sha == multi_commit_partial_shas["a"], (
        "a multi-commit partial apply must leave base_sha at the "
        "pre-tick boundary (A), never at an intermediate commit (B) "
        f"that was folded into the same not-fully-applied range — got "
        f"base_sha={reloaded.base_sha!r}, expected "
        f"{multi_commit_partial_shas['a']!r} "
        f"(B={multi_commit_partial_shas['b']!r}, "
        f"C={multi_commit_partial_shas['c']!r})"
    )


# ---------------------------------------------------------------------------
# (4) Medium: drift/push must agree on the tree hash for the same tree.
# ---------------------------------------------------------------------------


def test_drift_and_push_compute_the_same_hash_for_a_tree_with_a_portable_path(
    routes_module: Any,
    push_module: Any,
    store: state.StateStore,
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live tracked file containing an absolute path under the

    ``KIROCREW_HOME`` root must hash IDENTICALLY whether computed by
    ``routes.drift``/``routes.status`` or by ``push.run()``'s own no-op
    gate — otherwise a push that just recorded ``last_pushed_hash`` for
    the tokenized tree leaves ``drift()`` reporting drift for a tree that
    was, in fact, just pushed.

    RED reason: ``routes._redacted_tree()`` + ``routes.drift``/
    ``routes.status`` call ``push.tree_hash(redacted)`` directly on the
    REDACTED-but-NOT-tokenized tree (verified by reading ``routes.py``:
    ``redacted = _redacted_tree(); current_hash =
    push.tree_hash(redacted)`` — no ``tokenize_tree``/
    ``_tokenize_tree_with_report`` call anywhere in that module), while
    ``push.run()`` hashes ``tree_hash(tokenize_tree(redacted))`` (see
    ``push.py``'s own docstring step 2, and its ``run()`` body). For any
    tracked JSON file containing an absolute path under a tracked root,
    tokenizing rewrites that value before hashing, so the two hashes
    differ for the exact same live file content.
    """
    root_a = isolated_env["root_a"]
    (root_a / "mcp.json").write_text(
        json.dumps({"root": str(root_a / "some" / "nested" / "path")}),
        encoding="utf-8",
    )

    monkeypatch.setattr(routes_module, "is_app_enabled", lambda _name: True)

    from backend import buildo_pr as buildo_pr_module
    from backend import pr_handoff as pr_handoff_module

    monkeypatch.setattr(
        buildo_pr_module,
        "open_pull_request",
        lambda *a, **k: {"pr_url": "https://example.invalid/pr/1"},
        raising=False,
    )
    monkeypatch.setattr(pr_handoff_module, "handle_pushed_branch", lambda *a, **k: None)

    push_module.BUNDLE_REPO_URL = f"file://{isolated_env['state_dir']}/does-not-exist"

    # Directly record last_pushed_hash the way a successful push+PR
    # confirmation would, using push.py's OWN hashing pipeline — this is
    # the value push.run()'s no-op gate would itself have written.
    collected = push_module.collect.collect()
    redacted = push_module.redact.redact(collected)
    tokenized, _ = push_module._tokenize_tree_with_report(redacted)
    pushed_hash = push_module.tree_hash(tokenized)

    fresh_store = state.load_state()
    fresh_store.record_branch_pushed(tree_hash=pushed_hash, branch="config-sync/x")
    fresh_store.record_push_success(
        tree_hash=pushed_hash,
        branch="config-sync/x",
        pr_url="https://example.invalid/pr/1",
    )

    reloaded = state.load_state()
    drift_result = routes_module.drift(reloaded)

    assert drift_result["drift"] is False, (
        "drift() must report no drift immediately after last_pushed_hash "
        "was recorded for this exact live tree, but drift() hashes the "
        "redacted-not-tokenized tree while push hashes the redacted-AND-"
        f"tokenized tree — got drift_result={drift_result!r}, "
        f"push's own tokenized hash was {pushed_hash!r}"
    )


# ---------------------------------------------------------------------------
# (5) Tighten test_routes_approve_seam.py:487's outcome assertion.
# ---------------------------------------------------------------------------
#
# See the one-line edit applied directly to
# tests/test_routes_approve_seam.py: the materialize-failure test now
# asserts outcome == "apply-error" exactly (the materialize/archive step
# fails inside _apply_new_head, never the fetch-commit-details step), not
# the looser `in ("fetch-failed", "apply-error")`.
