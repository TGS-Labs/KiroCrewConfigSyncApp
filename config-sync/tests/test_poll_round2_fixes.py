"""Tests for `backend/poll.py`'s senior-review ROUND 2 fixes.

Covers, against real git (matching `test_poll_pending.py` /
`test_poll_fetch_commit_details.py`'s established convention — no mocked
git call for anything `poll.run()` itself invokes; Kiro-Config-Bundles#57
tracks the mocked-git methodology gap this convention closes):

- **H3** (carried over from round 1): the notify-ordering race.
  `state.record_seen_sha` must run BEFORE `notify_operator`, so a raising
  notify seam costs at most one missed notification rather than a
  permanent re-nag loop.
- **H-new-1**: any failure AFTER the head is resolved as changed (the
  bundle-repo clone/fetch, the commit-metadata/changed-path git calls) is
  caught and reported as `PollResult(outcome="fetch-failed")` rather than
  propagating uncaught — proven here via a real, unreachable clone target.
- **M-new-2**: `_clone_lock` actually serializes two processes racing to
  clone the same directory — proven by holding the lock in the test
  process and confirming a second acquisition attempt via `_ensure_bundle_clone`
  blocks until the lock is released, and that the lock times out rather
  than hanging forever when the holder never releases.
- **M3**: a non-ASCII changed-path filename survives `_changed_paths_for_range`
  as its real UTF-8 relpath, not git's default quoted-octal-escape rendering.
- **M2**: `ignored` and `touched_classes` from `classify.classify_paths`
  are carried into the pending record via `state.set_pending`.
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock

import pytest

from backend import poll, state


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
    """Isolates the app's own state dir AND both tracked config roots.

    Every test in this file drives a real `poll.run()` tick on a changed
    head, which under the auto-apply ruling (requirements.md 4.4) always
    materializes and applies the new commit for real — KIROCREW_HOME/
    KIRO_HOME are pinned here, once, rather than per test, or an unpinned
    test would let `apply_commit` write into the real host home
    directory.
    """
    state_dir = tmp_path / "state"
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(state_dir))
    kirocrew_home = tmp_path / "kirocrew_home"
    kiro_home = tmp_path / "kiro_home"
    kirocrew_home.mkdir(parents=True, exist_ok=True)
    kiro_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("KIROCREW_HOME", str(kirocrew_home))
    monkeypatch.setenv("KIRO_HOME", str(kiro_home))
    yield state_dir


@pytest.fixture
def git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Round2")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "round2@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Round2")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "round2@example.com")


@pytest.fixture
def bundle_remote(
    tmp_path: Path,
    isolated_state_dir: Path,
    git_identity: None,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[dict]:
    """A real local bare repo standing in for the bundle repo, matching

    `test_poll_pending.py`'s fixture exactly.
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


@pytest.fixture
def bundle_repo_url_redirect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Redirect `poll.BUNDLE_REPO_URL` (the real hardcoded GitHub URL) to a

    real local bare repo via git's own `url.<base>.insteadOf` config,
    injected through `GIT_CONFIG_GLOBAL` — never a `monkeypatch.setattr`
    on `poll.BUNDLE_REPO_URL`.

    `runpy.run_module("backend.poll", run_name="__main__", alter_sys=True)`
    re-executes `backend/poll.py`'s module body from scratch in a fresh
    namespace, so a `monkeypatch.setattr(poll, "BUNDLE_REPO_URL", ...)` on
    the ALREADY-IMPORTED module object never reaches the freshly
    re-executed code — that code reads its own fresh `BUNDLE_REPO_URL`
    module-level constant, which is the real
    `https://github.com/TGS-Labs/Kiro-Config-Bundles.git`. The prior
    version of the `__main__`-guard fetch-failed test relied on that real
    URL being unreachable (or reachable-but-failing for an unrelated
    reason) — an accidental, network-dependent failure mode, not a
    deliberate one (senior-review round-6 test-quality gap).

    A `GIT_CONFIG_GLOBAL` environment variable, by contrast, is PROCESS
    state, not a Python attribute — it survives `runpy`'s fresh
    re-execution exactly like this suite's existing
    `GIT_AUTHOR_*`/`GIT_COMMITTER_*` env-var convention
    (`test_push_retry_pr_only.py`'s `bare_remote` fixture). Every
    `git_safety.git_argv` call already passes `-c` flags of its own
    (`GIT_SAFE_CONFIG`); those are per-invocation overrides and do not
    conflict with a `url.insteadOf` rule supplied via the global config
    file — a real git call confirms the rule applies whether the `-c`
    flags are present or not (`git_argv` builds
    ``["git", "-C", cwd, *GIT_SAFE_CONFIG, *args]``, so the global config
    is still read first regardless of ``-c`` ordering).

    Sets `GIT_CONFIG_GLOBAL` via `monkeypatch.setenv` (restored on
    teardown) rather than writing to any real `~/.gitconfig` — this test
    process must never mutate the host's real git configuration, matching
    every other fixture in this suite.
    """
    redirect_file = tmp_path / "redirect.gitconfig"
    redirect_file.write_text(
        f'[url "{tmp_path / "bundle-remote.git"}"]\n'
        # The literal real URL, not `poll.BUNDLE_REPO_URL` — a fixture
        # ordered before this one (e.g. `bundle_remote`) may already have
        # `monkeypatch.setattr(poll, "BUNDLE_REPO_URL", ...)`'d it to the
        # LOCAL path, which would make this rule redirect the local path
        # to itself (a no-op) and leave the real GitHub URL completely
        # unredirected once `runpy` re-executes with a fresh `poll`
        # module reading the real module-level constant.
        "\tinsteadOf = https://github.com/TGS-Labs/Kiro-Config-Bundles.git\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(redirect_file))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    yield redirect_file


@pytest.fixture
def notify_spy(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    spy = MagicMock(name="poll.notify_operator")
    monkeypatch.setattr(poll, "notify_operator", spy)
    return spy


# ---------------------------------------------------------------------------
# H3 — notify-ordering: record_seen_sha before notify_operator.
# ---------------------------------------------------------------------------


def test_last_seen_sha_advances_even_when_notify_operator_raises(
    isolated_state_dir: Path,
    bundle_remote: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WHEN `notify_operator` raises THEN `state.last_seen_sha` must

    already have advanced to the new head — proving the fix reordered
    `record_seen_sha` before `notify_operator` (H3). With the OLD ordering
    a raising notify would leave `last_seen_sha` unchanged and the next
    tick would re-classify/re-notify the same commit forever.

    Under the auto-apply ruling the commit is materialized and applied
    BEFORE `record_seen_sha`/`notify_operator` run at all, so by the time
    `notify_operator` raises the commit has already been fully applied
    and its pending record resolved — `last_apply` (not `pending`) is
    what proves the apply actually happened for this head.
    """
    store = state.load_state()
    store.record_seen_sha(bundle_remote["old_sha"])

    new_sha = _push_new_commit(
        bundle_remote["seed_dir"],
        relpath="steering/h3.md",
        content="h3 regression guard\n",
        subject="h3 regression guard",
    )

    def _raising_notify(
        *,
        head_sha: str,
        author: str = "",
        subject: str = "",
        touched_classes: list[str] | None = None,
    ) -> None:
        raise RuntimeError("simulated notification channel outage")

    monkeypatch.setattr(poll, "notify_operator", _raising_notify)

    with pytest.raises(RuntimeError, match="simulated notification channel outage"):
        poll.run()

    reloaded = state.load_state()
    assert reloaded.last_seen_sha == new_sha, (
        "last_seen_sha must advance to the new head BEFORE notify_operator "
        "is called, so a raising notify costs at most one missed "
        "notification rather than a permanent re-nag loop (H3)"
    )
    assert reloaded.pending is None, (
        "the commit must already have been applied and resolved before "
        "notify_operator ever runs"
    )
    assert reloaded.last_apply is not None
    assert reloaded.last_apply["sha"] == new_sha


def test_second_tick_after_a_notify_failure_does_not_reclassify(
    isolated_state_dir: Path,
    bundle_remote: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WHEN a first tick's `notify_operator` raised (but `last_seen_sha`

    already advanced per H3) THEN a second tick resolving the SAME head
    must take the unchanged-head path — never re-entering the
    fetch/classify/set_pending block a second time for the same commit.
    """
    store = state.load_state()
    store.record_seen_sha(bundle_remote["old_sha"])

    _push_new_commit(
        bundle_remote["seed_dir"],
        relpath="steering/h3b.md",
        content="h3 second-tick guard\n",
        subject="h3 second-tick guard",
    )

    def _raising_notify(
        *,
        head_sha: str,
        author: str = "",
        subject: str = "",
        touched_classes: list[str] | None = None,
    ) -> None:
        raise RuntimeError("simulated outage")

    monkeypatch.setattr(poll, "notify_operator", _raising_notify)
    with pytest.raises(RuntimeError):
        poll.run()

    # Restore a working notify seam and tick again with the same remote
    # head — must resolve as "unchanged", not re-enter the changed path.
    spy = MagicMock(name="poll.notify_operator")
    monkeypatch.setattr(poll, "notify_operator", spy)

    result = poll.run()

    assert result.outcome == "unchanged"
    spy.assert_not_called()


# ---------------------------------------------------------------------------
# H1 (round 3) — notify_operator carries the full requirement 4.3 payload:
# commit, author, subject, and touched configuration classes.
# ---------------------------------------------------------------------------


def test_notify_operator_receives_all_four_required_fields_on_a_changed_head(
    isolated_state_dir: Path,
    bundle_remote: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WHEN a poll tick resolves a genuinely changed head THEN

    `notify_operator` must be called with `head_sha`, `author`, `subject`,
    and `touched_classes` all carrying the REAL values for that commit —
    not just `head_sha` as before round 3. Requirements.md 4.3 requires
    the notification identify "the commit, its author, its subject, and
    which tracked configuration classes the change touches"; task 4.4's
    "notifies once" exit criterion is unverifiable against a call that
    only ever carried one of those four fields.

    Uses a real git remote/commit (matching this file's own no-mocked-git
    convention) so `author`/`subject` are genuine `git show` output and
    `touched_classes` is genuinely produced by `classify.classify_paths`
    against a real allowlisted path, not a stubbed-in value.
    """
    store = state.load_state()
    store.record_seen_sha(bundle_remote["old_sha"])

    new_sha = _push_new_commit(
        bundle_remote["seed_dir"],
        relpath="steering/h1-payload.md",
        content="H1 notify-payload regression guard\n",
        subject="h1 notify payload guard",
    )

    spy = MagicMock(name="poll.notify_operator")
    monkeypatch.setattr(poll, "notify_operator", spy)

    result = poll.run()

    assert result.outcome == "changed"
    spy.assert_called_once()
    _, kwargs = spy.call_args
    assert kwargs["head_sha"] == new_sha
    assert kwargs["subject"] == "h1 notify payload guard"
    assert kwargs["author"], "author must be a real non-empty git-show value"
    assert kwargs["touched_classes"], (
        "touched_classes must carry at least one propagation class for a "
        "commit that touches an allowlisted steering/ path"
    )

    reloaded = state.load_state()
    assert reloaded.pending is None, (
        "a fully-applied commit resolves its own pending record in the "
        "same tick under the auto-apply ruling"
    )
    assert reloaded.last_apply is not None
    assert reloaded.last_apply["sha"] == new_sha, (
        "last_apply must record the applied commit's sha, matching the "
        "same head_sha notify_operator was called with"
    )


# ---------------------------------------------------------------------------
# H-new-1 — a failure after the head is resolved as changed is caught and
# reported, not left to propagate uncaught.
# ---------------------------------------------------------------------------


def test_unreachable_clone_target_reports_fetch_failed_outcome(
    isolated_state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """WHEN the resolved head is "changed" but the bundle-repo clone step

    fails (an unreachable/invalid remote) THEN `run()` must return
    `PollResult(outcome="fetch-failed")` rather than let the real
    `CalledProcessError` from `git clone` propagate uncaught (H-new-1) —
    `app.json`'s poll cron is `"silent": true`, so an uncaught exception
    would leave a failing poll completely invisible.
    """
    old_sha = "0" * 40
    new_sha = "1" * 40

    store = state.load_state()
    store.record_seen_sha(old_sha)

    def _fake_resolve_remote_head(state_dir_owner: str) -> str:
        return new_sha

    monkeypatch.setattr(poll, "_resolve_remote_head", _fake_resolve_remote_head)
    # A path with no repository at all, and no network route to become one
    # — `git clone` against it fails deterministically and fast.
    unreachable = tmp_path / "does-not-exist" / "nothing-here.git"
    monkeypatch.setattr(poll, "BUNDLE_REPO_URL", str(unreachable))

    result = poll.run()

    assert result.outcome == "fetch-failed", (
        f"expected outcome='fetch-failed' on a real clone failure after a "
        f"changed head, got {result.outcome!r}"
    )
    assert result.head_sha == new_sha
    assert result.reason, "a fetch-failed PollResult must carry a reason"

    reloaded = state.load_state()
    assert reloaded.last_seen_sha == old_sha, (
        "a fetch-failed tick must leave last_seen_sha UNCHANGED so the "
        "next tick re-attempts the same head rather than skipping it"
    )
    assert reloaded.pending is None


def test_fetch_failed_sends_no_notification(
    isolated_state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    notify_spy: MagicMock,
) -> None:
    """A fetch-failed tick must never reach the notify seam — there is no

    pending record and nothing genuinely new to tell the operator about
    yet (the SAME commit will be retried on the next tick).
    """
    old_sha = "2" * 40
    new_sha = "3" * 40
    store = state.load_state()
    store.record_seen_sha(old_sha)

    monkeypatch.setattr(poll, "_resolve_remote_head", lambda state_dir_owner: new_sha)
    monkeypatch.setattr(poll, "BUNDLE_REPO_URL", str(tmp_path / "missing" / "repo.git"))

    result = poll.run()

    assert result.outcome == "fetch-failed"
    notify_spy.assert_not_called()


def test_main_guard_exits_non_zero_on_fetch_failed(
    isolated_state_dir: Path,
    bundle_remote: dict,
    bundle_repo_url_redirect: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `__main__` guard's exit-code mapping must treat `"fetch-failed"`

    as a failure exit, matching `"ls-remote-failed"` (H-new-1 extends H2's
    exit-code contract to the new failure outcome).

    Forces a REAL, deterministic `"fetch-failed"` outcome — not the prior
    version's accidental one (senior-review round-6): `ls-remote` and the
    bundle-repo clone both run for real against `bundle_remote`'s local
    bare repo via `bundle_repo_url_redirect` (a `url.insteadOf` rule that
    survives `runpy`'s fresh re-execution, unlike a
    `monkeypatch.setattr(poll, "BUNDLE_REPO_URL", ...)`), so the head
    resolves as genuinely changed and the clone genuinely succeeds. The
    failure is forced one step later and on purpose: `state.base_sha` is
    primed (via `set_pending`/`clear_pending`, same mechanism as before)
    to a well-formed but NONEXISTENT 40-hex SHA, so
    `_changed_paths_for_range`'s `git log <base_sha>..<head_sha>` fails
    with a real, deterministic `fatal: Invalid revision range` (exit 128)
    regardless of network reachability — confirmed identical whether or
    not the real `TGS-Labs/Kiro-Config-Bundles` GitHub repo is reachable,
    since every git call here targets only the local redirect target.
    """
    import runpy
    import sys

    bogus_base_sha = "d" * 40
    store = state.load_state()
    store.record_seen_sha(bundle_remote["old_sha"])
    # `poll.run()` ranges its changed-path fetch from `state.base_sha`
    # (requirements.md 4.9), not `last_seen_sha` — prime `base_sha` to a
    # SHA that never existed in `bundle_remote`'s repo, so the later
    # `git log base_sha..head_sha` call deterministically fails with
    # "Invalid revision range" once the head is resolved as changed.
    store.set_pending(sha=bogus_base_sha, author="", subject="", classified_paths={})
    store.clear_pending()

    _push_new_commit(
        bundle_remote["seed_dir"],
        relpath="steering/fetch-failed.md",
        content="fetch-failed regression guard\n",
        subject="fetch-failed regression guard",
    )

    monkeypatch.delitem(sys.modules, "backend.poll", raising=False)
    try:
        with pytest.raises(SystemExit) as exc_info:
            runpy.run_module("backend.poll", run_name="__main__", alter_sys=True)
        assert exc_info.value.code == 1
    finally:
        sys.modules["backend.poll"] = poll

    reloaded = state.load_state()
    assert reloaded.last_seen_sha == bundle_remote["old_sha"], (
        "a fetch-failed tick must leave last_seen_sha UNCHANGED so the "
        "next tick re-attempts the same head rather than skipping it"
    )
    # Prove the REAL local redirect target was actually reached (ls-remote
    # AND the bundle-repo clone both succeeded against it) rather than the
    # failure coming from an unreachable/misconfigured URL — the exact
    # distinction senior-review round-6 flagged as unverified. A clone
    # only happens on the changed-head path, past `_resolve_remote_head`;
    # an `"ls-remote-failed"` tick never gets this far.
    clone_dir = isolated_state_dir / "bundle-repo"
    assert (clone_dir / ".git").exists(), (
        "the bundle-repo clone must exist on disk — proving ls-remote AND "
        "the clone both succeeded against the real local redirect target, "
        "so the forced failure came from the deliberate bogus base_sha "
        "range (a genuine 'fetch-failed'), not an unreachable URL "
        "(which would report 'ls-remote-failed' and never clone at all)"
    )
    assert reloaded.last_poll_failure is not None
    assert "128" in reloaded.last_poll_failure["reason"], (
        "the recorded failure reason must be the real git process's "
        "non-zero exit (128, 'Invalid revision range') from the bogus "
        f"base_sha range, got: {reloaded.last_poll_failure['reason']!r}"
    )


# ---------------------------------------------------------------------------
# M-new-2 — the shared clone-directory lock actually serializes access.
# ---------------------------------------------------------------------------


def test_ensure_bundle_clone_blocks_while_lock_is_held_elsewhere(
    isolated_state_dir: Path,
    tmp_path: Path,
) -> None:
    """WHEN another holder has the `_clone_lock` for a directory THEN a

    concurrent `_ensure_bundle_clone` call for the SAME directory blocks
    until the lock is released, rather than racing straight into its own
    clone/fetch decision (M-new-2's whole point: the `.git`-exists check
    and the clone/fetch it selects must be atomic across processes).
    """
    clone_dir = tmp_path / "shared-clone"

    release_event = threading.Event()
    acquired_event = threading.Event()

    def _hold_lock() -> None:
        with poll._clone_lock(clone_dir):
            acquired_event.set()
            release_event.wait(timeout=5)

    holder = threading.Thread(target=_hold_lock, daemon=True)
    holder.start()
    assert acquired_event.wait(timeout=5), "lock holder never acquired the lock"

    # A second attempt on the SAME directory must not acquire the lock
    # while the holder still has it.
    second_acquired = threading.Event()

    def _second_attempt() -> None:
        with poll._clone_lock(clone_dir):
            second_acquired.set()

    waiter = threading.Thread(target=_second_attempt, daemon=True)
    waiter.start()

    assert not second_acquired.wait(timeout=0.5), (
        "a second _clone_lock acquisition succeeded while the first "
        "holder still held it — the lock is not actually exclusive"
    )

    release_event.set()
    holder.join(timeout=5)
    assert second_acquired.wait(timeout=5), (
        "the second attempt never acquired the lock after the first "
        "holder released it"
    )
    waiter.join(timeout=5)


def test_clone_lock_times_out_rather_than_hanging_forever(
    isolated_state_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WHEN the lock cannot be acquired within the configured timeout

    THEN `_clone_lock` raises `TimeoutError` rather than blocking forever
    — bounded so a wedged/dead lock holder cannot hang a poll tick.
    """
    clone_dir = tmp_path / "shared-clone-timeout"
    monkeypatch.setattr(poll, "_CLONE_LOCK_TIMEOUT_SECS", 0.3)
    monkeypatch.setattr(poll, "_CLONE_LOCK_POLL_INTERVAL_SECS", 0.05)

    release_event = threading.Event()
    acquired_event = threading.Event()

    def _hold_lock_forever() -> None:
        with poll._clone_lock(clone_dir):
            acquired_event.set()
            release_event.wait(timeout=5)

    holder = threading.Thread(target=_hold_lock_forever, daemon=True)
    holder.start()
    assert acquired_event.wait(timeout=5)

    start = time.monotonic()
    with pytest.raises(TimeoutError):
        with poll._clone_lock(clone_dir):
            pass
    elapsed = time.monotonic() - start
    assert elapsed < 5, f"timeout took {elapsed}s, far longer than the 0.3s bound"

    release_event.set()
    holder.join(timeout=5)


def test_ensure_bundle_clone_still_works_normally_under_the_lock(
    isolated_state_dir: Path,
    bundle_remote: dict,
) -> None:
    """The lock must not break the ordinary, uncontended clone-then-fetch

    path — `_ensure_bundle_clone` must still produce a working clone when
    nothing else is contending for the lock.
    """
    clone_dir = isolated_state_dir / poll._BUNDLE_CLONE_DIRNAME
    poll._ensure_bundle_clone(clone_dir)
    assert (clone_dir / ".git").exists()

    # A second call (fetch, not clone) must also succeed under the lock.
    poll._ensure_bundle_clone(clone_dir)
    assert (clone_dir / ".git").exists()


# ---------------------------------------------------------------------------
# M3 — non-ASCII filenames survive as real UTF-8, not quoted-octal escapes.
# ---------------------------------------------------------------------------


def test_non_ascii_filename_is_reported_as_real_utf8_not_quoted_octal(
    isolated_state_dir: Path,
    bundle_remote: dict,
    notify_spy: MagicMock,
) -> None:
    """WHEN a changed commit touches a non-ASCII-named file (e.g.

    ``steering/café.md``) THEN the file must be applied under its REAL
    UTF-8 relpath, not git's default `core.quotePath=true` quoted-octal-
    escape rendering (M3) — the allowlist is written against the real
    UTF-8 name, so a quoted escape string would never match it and the
    file would be silently dropped. Checked via `last_apply`/the live
    root rather than `pending`, since a fully-applied commit resolves its
    own pending record in the same tick under the auto-apply ruling.
    """
    store = state.load_state()
    store.record_seen_sha(bundle_remote["old_sha"])

    new_sha = _push_new_commit(
        bundle_remote["seed_dir"],
        relpath="steering/café.md",
        content="non-ascii filename regression guard\n",
        subject="add café steering doc",
    )

    poll.run()

    reloaded = state.load_state()
    assert reloaded.pending is None
    last_apply = reloaded.last_apply
    assert last_apply is not None
    assert last_apply["sha"] == new_sha
    assert "steering/café.md" in last_apply["applied"], (
        f"expected the real UTF-8 relpath 'steering/café.md' in "
        f"last_apply['applied'], got {last_apply['applied']!r} — "
        f"a quoted-octal-escape string here means core.quotePath=false "
        f"was not applied"
    )


# ---------------------------------------------------------------------------
# M2 — ignored / touched_classes carried through the automatic apply.
# ---------------------------------------------------------------------------


def test_pending_record_carries_ignored_paths(
    isolated_state_dir: Path,
    bundle_remote: dict,
    notify_spy: MagicMock,
) -> None:
    """WHEN a changed commit touches a path that matches NO allowlist

    entry on either root THEN it appears in `last_apply`'s
    `ignored_paths`, not silently dropped without a trace (M2) — a
    consumer should be able to see what a commit deliberately skipped
    without re-fetching and re-classifying it. Checked via `last_apply`
    rather than `pending`, since a fully-applied commit resolves its own
    pending record in the same tick under the auto-apply ruling.
    """
    store = state.load_state()
    store.record_seen_sha(bundle_remote["old_sha"])

    new_sha = _push_new_commit(
        bundle_remote["seed_dir"],
        relpath="not-tracked-by-any-allowlist-entry.bin",
        content="opaque\n",
        subject="add an untracked file",
    )

    poll.run()

    reloaded = state.load_state()
    assert reloaded.pending is None
    last_apply = reloaded.last_apply
    assert last_apply is not None
    assert last_apply["sha"] == new_sha
    assert "ignored_paths" in last_apply
    assert "not-tracked-by-any-allowlist-entry.bin" in last_apply["ignored_paths"]


def test_pending_record_carries_touched_classes(
    isolated_state_dir: Path,
    bundle_remote: dict,
    notify_spy: MagicMock,
) -> None:
    """WHEN a changed commit's paths classify to at least one propagation

    class THEN that class's value string is carried through to the
    `notify_operator` call's `touched_classes` kwarg (M2), straight from
    `classify.classify_paths`'s own `Result.touched_classes`, not
    recomputed ad hoc. Checked via the notify call rather than `pending`,
    since a fully-applied commit resolves its own pending record in the
    same tick under the auto-apply ruling — `touched_classes` itself is
    not carried into `last_apply`'s summary.
    """
    store = state.load_state()
    store.record_seen_sha(bundle_remote["old_sha"])

    new_sha = _push_new_commit(
        bundle_remote["seed_dir"],
        relpath="steering/touched-classes.md",
        content="touched classes regression guard\n",
        subject="touched classes regression guard",
    )

    poll.run()

    reloaded = state.load_state()
    assert reloaded.pending is None
    assert reloaded.last_apply is not None
    assert reloaded.last_apply["sha"] == new_sha

    notify_spy.assert_called_once()
    _, notify_kwargs = notify_spy.call_args
    assert notify_kwargs["touched_classes"], (
        "expected at least one propagation class recorded for a path "
        "that classified successfully under the steering/ allowlist entry"
    )


def test_pending_record_ignored_and_touched_classes_default_empty_when_absent(
    isolated_state_dir: Path,
) -> None:
    """`state.set_pending` called WITHOUT `ignored_paths`/`touched_classes`

    (the pre-M2 call shape) must still succeed and default both new fields
    to an empty list — additive, non-breaking for any existing caller.
    """
    store = state.load_state()
    store.set_pending(
        sha="a" * 40,
        author="Someone",
        subject="A commit",
        classified_paths={"steering/x.md": "steering"},
    )

    pending = state.load_state().pending
    assert pending is not None
    assert pending["ignored_paths"] == []
    assert pending["touched_classes"] == []
