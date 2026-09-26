"""FAILING tests for tasks.md 6.1's `StateStore.resolve_pending(sha)`.

Covers requirements.md 4.9, 7.1, 7.2, 7.4, 7.5 (the `resolve_pending`
sub-contract) and design.md's routes section: "Approve and decline call a
single ``resolve_pending()`` in ``state.py`` that advances ``base_sha`` to
the decided SHA and clears the pending record together (never one without
the other)".

`resolve_pending` does not exist yet on `StateStore` (only `clear_pending`
and `advance_base_sha` exist, as two SEPARATE calls) — every test below is
expected to fail at collection or at the first call with an AttributeError,
not an assertion error, until software-engineer adds it.

Contract pinned here (no implementation choice left open for
software-engineer to invent differently):

- ``resolve_pending(sha: str) -> None`` on ``StateStore``.
- On success: ``base_sha`` becomes ``sha`` AND ``pending`` becomes ``None``,
  persisted in ONE on-disk write (Kiro-Config-Bundles#65's fix must not
  reopen a window where one field is written and the other is not).
- Refuses (raises ``ValueError``) when ``store.pending`` is ``None``, or
  when ``store.pending["sha"] != sha`` — the #65 staleness case: a poll
  tick accumulated a newer commit into ``pending`` after the operator's
  approve/decline UI was rendered against an older ``sha``. On refusal,
  neither ``base_sha`` nor ``pending`` changes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest

from backend import state


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Point the app's own state directory at a disposable tmp_path dir,

    matching tests/test_state.py's isolation convention so this module
    never touches the real ``~/.config-sync``.
    """
    state_dir = tmp_path / "config-sync-state"
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(state_dir))
    yield state_dir


@pytest.fixture
def store(isolated_state_dir: Path) -> state.StateStore:
    """A real StateStore backed by a file under tmp_path (no mocking of

    StateStore itself — only `_save`/the write primitive is monkeypatched,
    and only in the atomicity test below).
    """
    return state.load_state()


def _seed_pending(
    store: state.StateStore,
    *,
    sha: str = "cccccccccccccccccccccccccccccccccccccc",
) -> None:
    """Put a real pending record + base_sha onto `store` via the existing,

    already-implemented `set_pending`, so these tests exercise
    `resolve_pending` against genuine prior state rather than a hand-built
    payload shortcut.
    """
    store.set_pending(
        sha=sha,
        author="Author Name <author@example.com>",
        subject="Rotate a token",
        classified_paths={"mcp.json": "LIVE_ON_NEXT_RESOLUTION"},
    )


def _reload(isolated_state_dir: Path) -> state.StateStore:
    """Load a FRESH StateStore from the same on-disk file, so assertions

    read what was actually persisted rather than the in-memory object
    that made the call (catches a bug where a mutation only updates
    `_payload` without calling `_save()`).
    """
    return state.load_state()


# ---------------------------------------------------------------------------
# Success path: one call, one persisted write, both fields move together
# ---------------------------------------------------------------------------


class TestResolvePendingSuccess:
    def test_advances_base_sha_to_the_given_sha(
        self, store: state.StateStore, isolated_state_dir: Path
    ) -> None:
        sha = "cccccccccccccccccccccccccccccccccccccc"
        _seed_pending(store, sha=sha)

        store.resolve_pending(sha)

        reloaded = _reload(isolated_state_dir)
        assert reloaded.base_sha == sha

    def test_clears_pending(
        self, store: state.StateStore, isolated_state_dir: Path
    ) -> None:
        sha = "cccccccccccccccccccccccccccccccccccccc"
        _seed_pending(store, sha=sha)

        store.resolve_pending(sha)

        reloaded = _reload(isolated_state_dir)
        assert reloaded.pending is None

    def test_both_fields_land_in_the_same_on_disk_document(
        self, store: state.StateStore, isolated_state_dir: Path
    ) -> None:
        """Not just "both eventually true" (the two-call sequence

        `advance_base_sha` + `clear_pending` would also satisfy the two
        tests above) — read the raw JSON bytes written by ONE
        `resolve_pending` call and confirm both fields already carry
        their resolved values in that single document.
        """
        sha = "cccccccccccccccccccccccccccccccccccccc"
        _seed_pending(store, sha=sha)

        store.resolve_pending(sha)

        raw = json.loads(state.get_state_path().read_text(encoding="utf-8"))
        assert raw["base_sha"] == sha
        assert raw["pending"] is None

    def test_declining_a_different_but_valid_sha_also_resolves(
        self, store: state.StateStore, isolated_state_dir: Path
    ) -> None:
        """The decline route calls the SAME resolve_pending — there is no

        separate "decline" method (design.md: "Approve and decline call a
        single resolve_pending()"). Calling it with the pending sha must
        resolve identically regardless of which route called it; the
        distinction between approve/decline is made by the CALLER
        (whether apply.apply_commit ran first), not by resolve_pending.
        """
        sha = "dddddddddddddddddddddddddddddddddddddd"
        _seed_pending(store, sha=sha)

        store.resolve_pending(sha)

        reloaded = _reload(isolated_state_dir)
        assert reloaded.base_sha == sha
        assert reloaded.pending is None


# ---------------------------------------------------------------------------
# Atomicity: prove the write is ONE persisted operation, not two
# ---------------------------------------------------------------------------


class TestResolvePendingAtomicity:
    def test_a_failed_save_leaves_neither_field_changed_on_reload(
        self,
        store: state.StateStore,
        isolated_state_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Security/data-integrity mutation-test (testing-standards.md

        Mutation Requirement): monkeypatch the underlying persistence
        primitive to raise, call resolve_pending, and confirm the ON-DISK
        document (reloaded fresh, not the in-memory object that made the
        failed call) still shows the PRE-resolve base_sha/pending — proving
        the write is atomic (one document write, not
        "advance_base_sha-then-save, clear_pending-then-save" which would
        leave base_sha advanced but pending still present if the save
        failed between the two).

        Implementation mutation that would make this test FAIL (i.e. that
        this test is designed to catch): `resolve_pending` implemented as
        two separate self._save()-ing steps
        (`self.advance_base_sha(sha); self.clear_pending()`) instead of one
        combined payload mutation followed by a single `self._save()` call.
        Under that two-step mutation, patching `_save` to raise on its
        FIRST invocation still leaves `base_sha` advanced in the on-disk
        file from a possible earlier successful save, or patching it to
        raise only on the SECOND invocation reproduces exactly the
        half-written state this test exists to forbid.
        """
        original_sha = "cccccccccccccccccccccccccccccccccccccc"
        _seed_pending(store, sha=original_sha)
        # Capture the fully-settled pre-resolve document (base_sha was set
        # to original_sha by set_pending itself per requirements.md 4.9).
        pre_base_sha = store.base_sha
        pre_pending = store.pending
        assert pre_pending is not None  # sanity: seeding actually worked

        def _boom(self: state.StateStore) -> None:
            raise OSError("simulated disk failure during resolve_pending")

        original_save = state.StateStore._save
        monkeypatch.setattr(state.StateStore, "_save", _boom)

        with pytest.raises(OSError):
            store.resolve_pending(original_sha)

        # Restore only the _save patch (never CONFIG_SYNC_STATE_DIR) so the
        # "fresh reload" below still reads the isolated tmp_path state file,
        # not the real ~/.config-sync/state/state.json.
        monkeypatch.setattr(state.StateStore, "_save", original_save)
        reloaded = _reload(isolated_state_dir)
        assert reloaded.base_sha == pre_base_sha
        assert reloaded.pending == pre_pending


# ---------------------------------------------------------------------------
# Refusal: no pending, or pending.sha != sha (the #65 staleness case)
# ---------------------------------------------------------------------------


class TestResolvePendingRefusal:
    def test_refuses_when_there_is_no_pending_record(
        self, store: state.StateStore, isolated_state_dir: Path
    ) -> None:
        assert store.pending is None  # sanity: nothing seeded

        with pytest.raises(ValueError):
            store.resolve_pending("cccccccccccccccccccccccccccccccccccccc")

        reloaded = _reload(isolated_state_dir)
        assert reloaded.pending is None
        assert reloaded.base_sha is None

    def test_refuses_when_submitted_sha_does_not_match_pending_sha(
        self, store: state.StateStore, isolated_state_dir: Path
    ) -> None:
        """The #65 staleness case named in tasks.md 6.1: a poll tick

        accumulated a NEWER commit into `pending` after the operator's
        approve/decline UI rendered against an OLDER sha. The route layer
        submits the sha the operator actually saw; resolve_pending must
        refuse rather than silently resolving against whatever is
        currently pending, which would apply/decline files the operator
        never reviewed.
        """
        stale_sha_operator_saw = "cccccccccccccccccccccccccccccccccccccc"
        newer_sha_now_pending = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        # Seed as if a poll tick already accumulated past what the
        # operator's UI rendered.
        _seed_pending(store, sha=newer_sha_now_pending)

        with pytest.raises(ValueError):
            store.resolve_pending(stale_sha_operator_saw)

        reloaded = _reload(isolated_state_dir)
        # Neither field moved: the newer pending commit is still pending,
        # untouched, and base_sha has not advanced past it.
        assert reloaded.pending is not None
        assert reloaded.pending["sha"] == newer_sha_now_pending
        assert reloaded.base_sha != stale_sha_operator_saw

    def test_refusal_leaves_pending_byte_identical_on_disk(
        self, store: state.StateStore, isolated_state_dir: Path
    ) -> None:
        """Security/data-integrity mutation-test companion to the

        staleness refusal above: read the raw on-disk pending payload
        before and after a refused call and assert byte-for-byte equality,
        not just that pending is "still not None". This is the assertion
        that an implementation mutation dropping the sha-equality check
        (e.g. comparing `sha is not None` instead of `pending["sha"] ==
        sha`, or comparing against `store.base_sha` instead of
        `store.pending["sha"]`) would fail: such a mutation would let the
        call proceed and mutate the pending payload despite the mismatch.

        Implementation mutation that makes this test fail: relaxing
        `resolve_pending`'s guard from an exact string-equality check on
        `pending["sha"]` to any weaker check (truthiness, prefix match, or
        comparing the wrong field) that a mismatched sha still satisfies.
        """
        stale_sha_operator_saw = "cccccccccccccccccccccccccccccccccccccc"
        newer_sha_now_pending = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        _seed_pending(store, sha=newer_sha_now_pending)

        before = state.get_state_path().read_text(encoding="utf-8")

        with pytest.raises(ValueError):
            store.resolve_pending(stale_sha_operator_saw)

        after = state.get_state_path().read_text(encoding="utf-8")
        assert before == after
