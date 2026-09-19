"""Tests for backend/state.py — durable app state (task 1.4).

Covers Requirements 2.2, 2.6, 2.7, 4.6, 4.7:
- One JSON document under the app's OWN state directory, never inside
  either tracked configuration root (KIROCREW_HOME / KIRO_HOME).
- Persists last_pushed_hash, last_push, last_seen_sha, pending, a bounded
  history, and restore_dirs.
- Writes are atomic: no partial/corrupt state file survives a simulated
  interrupted write.
- A failed push leaves last_pushed_hash unchanged.

This module intentionally imports `state` (backend/state.py), which does not
exist yet. All tests below are expected to fail with a collection-time
ImportError / ModuleNotFoundError until software-engineer implements it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator

import pytest

import state


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Path]]:
    """Point both tracked configuration roots at disposable directories.

    KIROCREW_HOME (root A) and KIRO_HOME (root B) are the two trees this app
    tracks and commits from. The app's own state directory must resolve
    OUTSIDE both, so tests give each an unambiguous, distinct path under
    tmp_path and assert the state directory is not a descendant of either.
    """
    root_a = tmp_path / "kirocrew_home"
    root_b = tmp_path / "kiro_home"
    root_a.mkdir()
    root_b.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(root_a))
    monkeypatch.setenv("KIRO_HOME", str(root_b))
    yield {"root_a": root_a, "root_b": root_b, "tmp_path": tmp_path}


@pytest.fixture
def state_store(isolated_roots: dict[str, Path]):
    """A fresh state store instance/module state, isolated per test."""
    return state.load_state()


def _is_descendant(candidate: Path, ancestor: Path) -> bool:
    """True if `candidate` is `ancestor` itself or lives under it."""
    candidate = candidate.resolve()
    ancestor = ancestor.resolve()
    return candidate == ancestor or ancestor in candidate.parents


# ---------------------------------------------------------------------------
# State directory location — Requirement 2.2 / design.md state.py component
# ---------------------------------------------------------------------------


class TestStateDirectoryLocation:
    def test_state_dir_is_outside_kirocrew_home_root(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        state_dir = Path(state.get_state_dir())
        assert not _is_descendant(state_dir, isolated_roots["root_a"]), (
            "app state directory must never live inside KIROCREW_HOME "
            "(root A) or it could be swept into a tracked-tree commit"
        )

    def test_state_dir_is_outside_kiro_home_root(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        state_dir = Path(state.get_state_dir())
        assert not _is_descendant(state_dir, isolated_roots["root_b"]), (
            "app state directory must never live inside KIRO_HOME "
            "(root B) or it could be swept into a tracked-tree commit"
        )

    def test_state_file_lives_under_the_state_dir(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        state_dir = Path(state.get_state_dir())
        state_path = Path(state.get_state_path())
        assert _is_descendant(state_path, state_dir)

    def test_state_file_is_a_single_json_document(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.record_push_success(
            tree_hash="abc123",
            branch="config-sync/instance-abc123",
            pr_url="https://github.com/TGS-Labs/Kiro-Config-Bundles/pull/1",
        )
        state_path = Path(state.get_state_path())
        assert state_path.is_file()
        assert state_path.suffix == ".json"
        # A single parse must succeed and it must be the ONE document
        # carrying every field this task lists.
        with open(state_path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        for key in (
            "last_pushed_hash",
            "last_push",
            "last_seen_sha",
            "pending",
            "history",
            "restore_dirs",
        ):
            assert key in payload, f"missing '{key}' in the single state document"


# ---------------------------------------------------------------------------
# Persisted fields — last_pushed_hash, last_push
# ---------------------------------------------------------------------------


class TestLastPushedHashAndLastPush:
    def test_record_push_success_persists_hash_and_push_metadata(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.record_push_success(
            tree_hash="deadbeef",
            branch="config-sync/instance-deadbeef",
            pr_url="https://github.com/TGS-Labs/Kiro-Config-Bundles/pull/42",
        )

        reloaded = state.load_state()
        assert reloaded.last_pushed_hash == "deadbeef"
        last_push = reloaded.last_push
        assert last_push is not None
        assert last_push["branch"] == "config-sync/instance-deadbeef"
        assert (
            last_push["pr_url"]
            == "https://github.com/TGS-Labs/Kiro-Config-Bundles/pull/42"
        )
        assert "time" in last_push

    def test_last_pushed_hash_survives_reload_from_disk(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.record_push_success(
            tree_hash="cafef00d", branch="config-sync/instance-cafef00d", pr_url=None
        )
        del store

        reloaded = state.load_state()
        assert reloaded.last_pushed_hash == "cafef00d"


# ---------------------------------------------------------------------------
# Failed push leaves last_pushed_hash unchanged — Requirements 2.6, 2.7
# ---------------------------------------------------------------------------


class TestFailedPushLeavesHashUnchanged:
    def test_record_push_failure_does_not_change_last_pushed_hash(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.record_push_success(
            tree_hash="original-hash",
            branch="config-sync/instance-original-hash",
            pr_url="https://github.com/TGS-Labs/Kiro-Config-Bundles/pull/1",
        )

        store.record_push_failure(reason="protected branch refused")

        assert store.last_pushed_hash == "original-hash"

    def test_record_push_failure_does_not_change_hash_when_none_set_yet(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        assert store.last_pushed_hash is None

        store.record_push_failure(reason="secret scan finding")

        assert store.last_pushed_hash is None

    def test_failed_push_is_reflected_on_disk_not_only_in_memory(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.record_push_success(
            tree_hash="stable-hash",
            branch="config-sync/instance-stable-hash",
            pr_url="https://github.com/TGS-Labs/Kiro-Config-Bundles/pull/2",
        )
        store.record_push_failure(reason="clone failed")

        reloaded = state.load_state()
        assert reloaded.last_pushed_hash == "stable-hash"

    def test_record_push_failure_surfaces_the_cause(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.record_push_failure(reason="ambiguous target branch")

        last_failure = store.last_push_failure
        assert last_failure is not None
        assert last_failure["reason"] == "ambiguous target branch"
        assert "time" in last_failure


# ---------------------------------------------------------------------------
# last_seen_sha (poll direction)
# ---------------------------------------------------------------------------


class TestLastSeenSha:
    def test_last_seen_sha_defaults_to_none_before_any_poll(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        assert store.last_seen_sha is None

    def test_record_seen_sha_persists_across_reload(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.record_seen_sha("0123456789abcdef0123456789abcdef01234567")

        reloaded = state.load_state()
        assert (
            reloaded.last_seen_sha == "0123456789abcdef0123456789abcdef01234567"
        )

    def test_record_seen_sha_overwrites_the_previous_value(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.record_seen_sha("1111111111111111111111111111111111111111")
        store.record_seen_sha("2222222222222222222222222222222222222222")

        reloaded = state.load_state()
        assert (
            reloaded.last_seen_sha == "2222222222222222222222222222222222222222"
        )


# ---------------------------------------------------------------------------
# pending record
# ---------------------------------------------------------------------------


class TestPending:
    def test_no_pending_record_by_default(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        assert store.pending is None

    def test_set_pending_persists_sha_author_subject_and_classified_paths(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.set_pending(
            sha="abcdef0123456789abcdef0123456789abcdef01",
            author="staneslevski",
            subject="Add new steering file",
            classified_paths={"steering/new-rule.md": "steering"},
        )

        reloaded = state.load_state()
        pending = reloaded.pending
        assert pending is not None
        assert pending["sha"] == "abcdef0123456789abcdef0123456789abcdef01"
        assert pending["author"] == "staneslevski"
        assert pending["subject"] == "Add new steering file"
        assert pending["classified_paths"] == {"steering/new-rule.md": "steering"}

    def test_clear_pending_removes_the_pending_record(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.set_pending(
            sha="abcdef0123456789abcdef0123456789abcdef01",
            author="staneslevski",
            subject="Add new steering file",
            classified_paths={"steering/new-rule.md": "steering"},
        )
        store.clear_pending()

        reloaded = state.load_state()
        assert reloaded.pending is None

    def test_declining_a_pending_commit_does_not_re_add_it_on_reload(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        # Requirement 4.6: decline leaves configuration byte-unchanged and
        # does not resurrect the same pending commit on its own.
        store = state.load_state()
        store.set_pending(
            sha="fedcba9876543210fedcba9876543210fedcba98",
            author="staneslevski",
            subject="Rotate a token",
            classified_paths={},
        )
        store.clear_pending()

        reloaded_again = state.load_state()
        assert reloaded_again.pending is None


# ---------------------------------------------------------------------------
# bounded history
# ---------------------------------------------------------------------------


class TestBoundedHistory:
    def test_history_starts_empty(self, isolated_roots: dict[str, Path]) -> None:
        store = state.load_state()
        assert store.history == []

    def test_history_records_events_in_order(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.record_push_success(
            tree_hash="hash-1", branch="config-sync/instance-hash-1", pr_url=None
        )
        store.record_push_success(
            tree_hash="hash-2", branch="config-sync/instance-hash-2", pr_url=None
        )

        reloaded = state.load_state()
        hashes = [entry.get("tree_hash") for entry in reloaded.history]
        assert hashes.index("hash-1") < hashes.index("hash-2")

    def test_history_is_bounded_and_does_not_grow_without_limit(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        limit = state.HISTORY_LIMIT
        # Push one more event than the documented bound.
        for i in range(limit + 5):
            store.record_push_success(
                tree_hash=f"hash-{i}",
                branch=f"config-sync/instance-hash-{i}",
                pr_url=None,
            )

        reloaded = state.load_state()
        assert len(reloaded.history) <= limit

    def test_bounded_history_drops_oldest_entries_first(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        limit = state.HISTORY_LIMIT
        for i in range(limit + 3):
            store.record_push_success(
                tree_hash=f"hash-{i}",
                branch=f"config-sync/instance-hash-{i}",
                pr_url=None,
            )

        reloaded = state.load_state()
        surviving_hashes = {entry.get("tree_hash") for entry in reloaded.history}
        # The earliest pushes (hash-0, hash-1, hash-2) must have been evicted
        # first; the most recent push must still be present.
        assert "hash-0" not in surviving_hashes
        assert f"hash-{limit + 2}" in surviving_hashes


# ---------------------------------------------------------------------------
# restore_dirs
# ---------------------------------------------------------------------------


class TestRestoreDirs:
    def test_restore_dirs_starts_empty(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        assert store.restore_dirs == {}

    def test_record_restore_dir_persists_the_mapping(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.record_restore_dir(
            apply_id="apply-20260919-1",
            restore_dir="/some/state/dir/restores/apply-20260919-1",
        )

        reloaded = state.load_state()
        assert (
            reloaded.restore_dirs["apply-20260919-1"]
            == "/some/state/dir/restores/apply-20260919-1"
        )

    def test_multiple_restore_dirs_are_all_retained(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.record_restore_dir(apply_id="apply-1", restore_dir="/state/restores/1")
        store.record_restore_dir(apply_id="apply-2", restore_dir="/state/restores/2")

        reloaded = state.load_state()
        assert reloaded.restore_dirs["apply-1"] == "/state/restores/1"
        assert reloaded.restore_dirs["apply-2"] == "/state/restores/2"


# ---------------------------------------------------------------------------
# Atomic writes — no partial/corrupt state file survives an interrupted write
# ---------------------------------------------------------------------------


class TestAtomicWrites:
    def test_write_uses_a_temp_file_and_rename_not_in_place_truncation(
        self, isolated_roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An atomic writer creates a temp file and os.replace()s it into
        place; it must never open the real state path with a truncating
        mode ('w') directly, because that leaves a truncated-to-empty file
        visible to any concurrent reader for the duration of the write."""
        store = state.load_state()
        state_path = Path(state.get_state_path())

        opened_modes_on_real_path: list[str] = []
        real_open = open

        def spy_open(path, mode="r", *args, **kwargs):
            if Path(path) == state_path and "w" in mode:
                opened_modes_on_real_path.append(mode)
            return real_open(path, mode, *args, **kwargs)

        monkeypatch.setattr("builtins.open", spy_open)

        store.record_push_success(
            tree_hash="atomic-check", branch="config-sync/instance-x", pr_url=None
        )

        assert opened_modes_on_real_path == [], (
            "the real state file must never be opened in a writing/truncating "
            "mode directly; write to a temp file then rename/replace"
        )

    def test_interrupted_write_leaves_previous_valid_state_file_intact(
        self, isolated_roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulate a crash mid-write: os.replace (the atomic rename step)
        raises. The state file on disk must remain the LAST successfully
        committed, fully valid JSON document -- never truncated or partial."""
        store = state.load_state()
        store.record_push_success(
            tree_hash="good-hash", branch="config-sync/instance-good", pr_url=None
        )
        state_path = Path(state.get_state_path())
        good_bytes = state_path.read_bytes()

        def failing_replace(*args, **kwargs):
            raise OSError("simulated crash during atomic rename")

        monkeypatch.setattr(os, "replace", failing_replace)

        with pytest.raises(OSError):
            store.record_push_success(
                tree_hash="never-committed",
                branch="config-sync/instance-never",
                pr_url=None,
            )

        # The on-disk file must be untouched: still the last good bytes,
        # and still parseable as the complete document.
        assert state_path.read_bytes() == good_bytes
        with open(state_path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        assert payload["last_pushed_hash"] == "good-hash"

    def test_no_leftover_temp_file_after_a_successful_write(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        store = state.load_state()
        store.record_push_success(
            tree_hash="clean-hash", branch="config-sync/instance-clean", pr_url=None
        )

        state_dir = Path(state.get_state_dir())
        leftover_temp_files = [
            p
            for p in state_dir.iterdir()
            if p.name != Path(state.get_state_path()).name and p.is_file()
        ]
        assert leftover_temp_files == [], (
            "a successful write must not leave a stray temp/partial file "
            f"behind: found {leftover_temp_files}"
        )

    def test_load_state_never_returns_a_half_written_document(
        self, isolated_roots: dict[str, Path]
    ) -> None:
        """Directly simulate a corrupt/truncated state file on disk (as an
        interrupted non-atomic write would leave) and confirm the loader
        does not silently accept truncated JSON as valid state -- it must
        either raise or fall back to a fresh, well-formed default document,
        never return a half-parsed/partial in-memory state."""
        store = state.load_state()
        store.record_push_success(
            tree_hash="pre-corruption", branch="config-sync/instance-x", pr_url=None
        )
        state_path = Path(state.get_state_path())

        # Truncate the file mid-document, as a crash during a non-atomic
        # write would leave it.
        original = state_path.read_text(encoding="utf-8")
        truncated = original[: len(original) // 2]
        state_path.write_text(truncated, encoding="utf-8")

        try:
            reloaded = state.load_state()
        except (json.JSONDecodeError, ValueError):
            # Failing loudly on unreadable state is an acceptable outcome.
            return

        # If it did not raise, it must not have silently kept the corrupt
        # value: last_pushed_hash must be well-defined (either the last
        # good value recovered from a backup, or a clean default), and the
        # full field set must still be present and JSON-serializable.
        assert reloaded.last_pushed_hash in (None, "pre-corruption")
        json.dumps(
            {
                "last_pushed_hash": reloaded.last_pushed_hash,
                "last_push": reloaded.last_push,
                "last_seen_sha": reloaded.last_seen_sha,
                "pending": reloaded.pending,
                "history": reloaded.history,
                "restore_dirs": reloaded.restore_dirs,
            }
        )
