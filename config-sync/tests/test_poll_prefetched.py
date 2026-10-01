"""Prefetched poll mode (Deployment 5, live-install defect 7).

The first real poll tick under KiroCrew's cron runner failed at
``git ls-remote`` with exit 128 ("could not read Username"): the host runs
command crons inside its sandbox, which hides ``~/.git-credentials`` by
design, and the bundle repo is private. The sanctioned way for a cron to
hold a credential is an operator-approved vault grant, and that exists for
SCRIPT crons only — the grant pins the approved script BODY, not the
binaries it calls. So the credentialed step (fetching the bundle repo into
the shared clone) lives in a small pinned script
(``host-crons/config_sync_poll.py``) and the app's own ``backend.poll`` must
be able to run WITHOUT touching the network, reading the head it is asked
to compare from the already-fetched clone.

``CONFIG_SYNC_PREFETCHED=1`` selects that mode:

* head resolution reads ``refs/remotes/origin/<default-branch>`` from the
  local clone instead of ``git ls-remote``;
* the clone-or-fetch step verifies the clone exists and does nothing else;
* a missing clone is a recorded, non-raising poll failure (the next tick
  retries) — never a clone or fetch, because this process has no credential.

Every test below points ``BUNDLE_REPO_URL`` at an unroutable address, so
any code path that still reaches for the network fails loudly rather than
passing because the test host happens to have a credential.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterator

import pytest

from backend import poll, state

_UNROUTABLE_REMOTE = "https://127.0.0.1:9/unreachable/bundle.git"


def _git(*args: str, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.fixture()
def origin_and_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, object]]:
    """A real bare "origin" with one commit on ``main``, cloned into the
    app's shared ``bundle-repo`` directory under an isolated state dir."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    (work / "steering").mkdir()
    (work / "steering" / "a.md").write_text("# a\n")
    _git("add", ".", cwd=work)
    _git("commit", "-q", "-m", "one", cwd=work)
    _git("clone", "-q", "--bare", str(work), str(origin), cwd=tmp_path)

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(state_dir))
    clone_dir = state_dir / poll._BUNDLE_CLONE_DIRNAME
    _git("clone", "-q", str(origin), str(clone_dir), cwd=tmp_path)

    # Prove no network path is taken: the module-level remote is unroutable.
    monkeypatch.setattr(poll, "BUNDLE_REPO_URL", _UNROUTABLE_REMOTE)
    head = _git("rev-parse", "main", cwd=origin)
    yield {
        "origin": origin,
        "work": work,
        "state_dir": state_dir,
        "clone_dir": clone_dir,
        "head": head,
    }


def test_prefetched_head_resolves_from_the_local_clone_without_network(
    origin_and_clone: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(poll.PREFETCHED_ENV, "1")
    resolved = poll._resolve_remote_head(str(origin_and_clone["state_dir"]))
    assert resolved == origin_and_clone["head"]


def test_prefetched_head_tracks_a_fetch_made_by_someone_else(
    origin_and_clone: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pinned script fetches; poll must see the NEW remote-tracking
    ref, not a cached value from its own earlier tick."""
    monkeypatch.setenv(poll.PREFETCHED_ENV, "1")
    work = origin_and_clone["work"]
    assert isinstance(work, Path)
    (work / "steering" / "b.md").write_text("# b\n")
    _git("add", ".", cwd=work)
    _git("commit", "-q", "-m", "two", cwd=work)
    _git("push", "-q", str(origin_and_clone["origin"]), "main", cwd=work)
    new_head = _git("rev-parse", "main", cwd=Path(str(origin_and_clone["origin"])))
    clone_dir = origin_and_clone["clone_dir"]
    assert isinstance(clone_dir, Path)
    # Stand-in for the pinned script's credentialed fetch (local, no creds).
    _git("fetch", "-q", "origin", cwd=clone_dir)

    resolved = poll._resolve_remote_head(str(origin_and_clone["state_dir"]))
    assert resolved == new_head
    assert resolved != origin_and_clone["head"]


def test_prefetched_ensure_bundle_clone_never_fetches(
    origin_and_clone: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the remote unroutable, the network path raises; prefetched mode
    must return quietly because the clone is already there."""
    monkeypatch.setenv(poll.PREFETCHED_ENV, "1")
    clone_dir = origin_and_clone["clone_dir"]
    assert isinstance(clone_dir, Path)
    poll._ensure_bundle_clone(clone_dir)  # must not raise


def test_prefetched_mode_with_no_clone_is_a_recorded_failure_not_a_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setenv(poll.PREFETCHED_ENV, "1")
    monkeypatch.setattr(poll, "BUNDLE_REPO_URL", _UNROUTABLE_REMOTE)

    result = poll.run()

    assert result.outcome == "ls-remote-failed"
    assert "prefetched" in result.reason.lower()
    assert not (state_dir / poll._BUNDLE_CLONE_DIRNAME / ".git").exists()
    store = state.load_state()
    assert store.last_poll_failure is not None
    assert store.last_seen_sha in ("", None)


def test_unset_prefetched_env_keeps_the_network_path(
    origin_and_clone: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard: without the flag the poll still asks the remote,
    which (unroutable here) fails — the flag is opt-in, not the default."""
    monkeypatch.delenv(poll.PREFETCHED_ENV, raising=False)
    with pytest.raises(subprocess.CalledProcessError):
        poll._resolve_remote_head(str(origin_and_clone["state_dir"]))


def test_prefetched_run_applies_nothing_when_head_is_unchanged(
    origin_and_clone: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(poll.PREFETCHED_ENV, "1")
    store = state.load_state()
    head = origin_and_clone["head"]
    assert isinstance(head, str)
    store.record_seen_sha(head)

    result = poll.run()

    assert result.outcome == "unchanged"
    assert result.head_sha == head
