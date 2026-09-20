"""Tests for backend/pr_handoff.py (task 3.3).

Covers tasks.md 3.3: "`backend/pr_handoff.py` records the pushed branch as
*PR pending* in state, notifies the operator and surfaces the handoff, and
advances `last_pushed_hash` only once PR creation is confirmed — so a failed
push or an uncompleted PR retries on the next tick instead of being
swallowed, and every failure is recorded with its cause for the UI without a
tight retry loop inside the tick", tracing to requirements.md:

- 2.3: "WHEN the hash differs THEN the job SHALL ... push to a named feature
  branch, and open a pull request."
- 2.6: "WHEN the push completes THEN the recorded last-pushed hash SHALL be
  updated only after the push and PR creation both succeed, so that a failed
  push is retried on the next tick rather than silently skipped."
- 2.7: "WHEN a push fails for any reason THEN the failure SHALL be recorded
  with its cause and surfaced in the app's UI, and the job SHALL NOT retry in
  a tight loop within the same tick."

Upstream signatures this module was built against (read, not guessed):

- `backend/push.py::PushResult` is `@dataclass(frozen=True)` with fields
  `outcome: str`, `tree_hash: str`, `reason: str = ""`. For a
  ``outcome="pushed"`` result, `push.run()` sets ``reason`` to the pushed
  branch name (see `push.py::run()`'s final `return PushResult(outcome=
  "pushed", tree_hash=current_hash, reason=branch)`).
- `backend/buildo_pr.py::build_pull_request_payload(head_branch, title,
  body)` returns a plain dict with keys `repo`, `base`, `head`, `title`,
  `body` (no `merge_method`).
- `backend/state.py::StateStore` (as it exists after task 1.4/3.1/3.2) has
  `record_push_success(*, tree_hash, branch, pr_url)`,
  `record_push_failure(*, reason)`, and a `last_pushed_hash` property with a
  plain (non-persisting) setter for test setup — but as of this task it has
  NO pending-PR concept yet (no `pending_pr`, no `record_pr_pending`, no
  `confirm_pr_created`). Per the task instructions, `pr_handoff.py` is
  expected to introduce that seam on `StateStore` itself (extending its
  schema is explicitly sanctioned by tasks.md 3.3's "records ... in state"
  wording and by design.md's provisional `pending_pr` field name in
  `skills/complete-pr-handoff/SKILL.md`), rather than persisting pending-PR
  bookkeeping somewhere state.py knows nothing about.

This module therefore assumes `backend/pr_handoff.py` extends
`StateStore` with three new methods it calls directly:

- ``record_pr_pending(*, branch, tree_hash, payload)`` — records the
  pending-PR state after a successful branch push, before PR creation is
  confirmed. Mirrors `record_push_failure`'s keyword-only shape.
- ``record_pr_pending_failure(*, reason)`` — records a PR-handoff failure
  (payload-build failure, notify failure, or a reported failed PR creation)
  with its cause, WITHOUT touching `last_pushed_hash`.
- ``confirm_pr_created(*, tree_hash, branch, pr_url)`` — the explicit,
  out-of-band confirmation entry point (arriving via the
  `complete-pr-handoff` skill from a KiroCrew agent context) that advances
  `last_pushed_hash` to `tree_hash`. This is expected to delegate to (or
  behave identically to) `record_push_success`, since that is the only
  existing method that advances `last_pushed_hash` — the tests below assert
  the OBSERVABLE property (last_pushed_hash advances, a `last_push` record
  appears) rather than requiring a specific delegation shape internally.

None of the three names above is guessed blindly: they are the natural
extension point implied by task 3.3's own text ("records ... as *PR
pending*", "advances `last_pushed_hash` only once PR creation is
confirmed") and by the provisional `pending_pr` field the
`complete-pr-handoff` skill already names for whatever `pr_handoff.py`
lands with. If software-engineer's real implementation calls these methods
under different names, that is a legitimate implementation choice to
reconcile — per this task's own instruction, tests should mock/inspect
"whatever seam" the module actually introduces, and updating the mocked
method names here is a same-class fix as any other API-shape correction,
not a sign these tests were wrong to specify behaviour up front.

Import target `backend.pr_handoff` does not exist yet — every test below is
expected to fail with an ImportError / ModuleNotFoundError until
software-engineer writes it. That is the correct TDD red-phase starting
state, not a test defect.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import pytest

from backend.push import PushResult


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeStateStore:
    """A minimal stand-in for `backend.state.StateStore` that records every
    call made to it, so tests assert on OBSERVED CALLS AND their resulting
    state rather than on a real on-disk JSON document — pr_handoff.py's
    contract with state.py is the thing under test here, not state.py's own
    persistence (that is test_state.py's job).
    """

    last_pushed_hash: str | None = None
    pending_calls: list[dict[str, Any]] = field(default_factory=list)
    pending_failure_calls: list[dict[str, Any]] = field(default_factory=list)
    confirm_calls: list[dict[str, Any]] = field(default_factory=list)
    push_success_calls: list[dict[str, Any]] = field(default_factory=list)
    push_failure_calls: list[dict[str, Any]] = field(default_factory=list)

    def record_pr_pending(
        self, *, branch: str, tree_hash: str, payload: dict[str, Any]
    ) -> None:
        self.pending_calls.append(
            {"branch": branch, "tree_hash": tree_hash, "payload": payload}
        )

    def record_pr_pending_failure(self, *, reason: str) -> None:
        self.pending_failure_calls.append({"reason": reason})

    def confirm_pr_created(self, *, tree_hash: str, branch: str, pr_url: str) -> None:
        self.confirm_calls.append(
            {"tree_hash": tree_hash, "branch": branch, "pr_url": pr_url}
        )
        # Mirrors record_push_success's observable effect: this is the
        # ONLY call in this fake that advances last_pushed_hash, matching
        # requirements.md 2.6.
        self.last_pushed_hash = tree_hash
        self.push_success_calls.append(
            {"tree_hash": tree_hash, "branch": branch, "pr_url": pr_url}
        )

    def record_push_failure(self, *, reason: str) -> None:
        self.push_failure_calls.append({"reason": reason})


@pytest.fixture
def fake_state() -> _FakeStateStore:
    return _FakeStateStore()


@pytest.fixture
def pushed_result() -> PushResult:
    """A `push.run()` result matching outcome="pushed" — branch name lives
    in `reason`, per push.py::run()'s real final return statement."""
    return PushResult(
        outcome="pushed",
        tree_hash="a" * 64,
        reason="config-sync/deadbeef123456-abcdef012345",
    )


def _import_module() -> Any:
    """Import helper so every test raises the same clear failure while the
    module doesn't exist yet, and so a future rename only needs one edit."""
    from backend import pr_handoff

    return pr_handoff


# ---------------------------------------------------------------------------
# Requirement 2.3 / tasks.md 3.3: build the PR payload from the pushed branch
# ---------------------------------------------------------------------------


class TestBuildsPayloadFromPushedBranch:
    """`pr_handoff.py` must build the PR payload via
    `buildo_pr.build_pull_request_payload` using the pushed branch name from
    the `PushResult.reason` field — never a re-derived or hardcoded branch
    name."""

    def test_handoff_calls_build_pull_request_payload_with_pushed_branch(
        self,
        fake_state: _FakeStateStore,
        pushed_result: PushResult,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pr_handoff = _import_module()
        build_payload_mock = MagicMock(
            return_value={
                "repo": "TGS-Labs/Kiro-Config-Bundles",
                "base": "main",
                "head": pushed_result.reason,
                "title": "chore: sync",
                "body": "Automated config sync.",
            }
        )
        monkeypatch.setattr(
            pr_handoff, "build_pull_request_payload", build_payload_mock
        )
        # Notification is mocked at whatever seam the module introduces —
        # see TestNotifiesOperator below for the dedicated coverage; here we
        # only need the handoff to run to completion without erroring.
        _patch_notify(monkeypatch, pr_handoff)

        pr_handoff.handle_pushed_branch(pushed_result, state=fake_state)

        assert build_payload_mock.call_count == 1
        _, kwargs = build_payload_mock.call_args
        called_args = build_payload_mock.call_args.args
        head_branch = kwargs.get("head_branch") or (
            called_args[0] if called_args else None
        )
        assert head_branch == pushed_result.reason

    def test_handoff_does_not_build_payload_for_a_no_op_push(
        self, fake_state: _FakeStateStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A no-op `PushResult` (hash-gate hit) carries no branch to open a
        PR from — the handoff must not attempt to build a payload for it."""
        pr_handoff = _import_module()
        build_payload_mock = MagicMock()
        monkeypatch.setattr(
            pr_handoff, "build_pull_request_payload", build_payload_mock
        )
        _patch_notify(monkeypatch, pr_handoff)

        no_op_result = PushResult(outcome="no-op", tree_hash="b" * 64)
        pr_handoff.handle_pushed_branch(no_op_result, state=fake_state)

        assert build_payload_mock.call_count == 0

    def test_handoff_does_not_build_payload_for_a_refused_push(
        self, fake_state: _FakeStateStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A refused push (secret scan / branch authorization) already
        recorded its own failure inside `push.run()` — the handoff must not
        treat it as a pushed branch."""
        pr_handoff = _import_module()
        build_payload_mock = MagicMock()
        monkeypatch.setattr(
            pr_handoff, "build_pull_request_payload", build_payload_mock
        )
        _patch_notify(monkeypatch, pr_handoff)

        refused_result = PushResult(
            outcome="refused-secret-scan",
            tree_hash="c" * 64,
            reason="secret-scan-finding:1",
        )
        pr_handoff.handle_pushed_branch(refused_result, state=fake_state)

        assert build_payload_mock.call_count == 0


# ---------------------------------------------------------------------------
# Requirement 2.3 / 2.6 / tasks.md 3.3: pending-PR state recording
# ---------------------------------------------------------------------------


class TestRecordsPendingPrState:
    """After building the payload, the handoff records a pending-PR state
    entry in `state` — BEFORE PR creation is confirmed, and without
    advancing `last_pushed_hash`."""

    def test_handoff_records_pr_pending_with_branch_and_tree_hash(
        self,
        fake_state: _FakeStateStore,
        pushed_result: PushResult,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pr_handoff = _import_module()
        monkeypatch.setattr(
            pr_handoff,
            "build_pull_request_payload",
            MagicMock(
                return_value={
                    "repo": "TGS-Labs/Kiro-Config-Bundles",
                    "base": "main",
                    "head": pushed_result.reason,
                    "title": "chore: sync",
                    "body": "Automated config sync.",
                }
            ),
        )
        _patch_notify(monkeypatch, pr_handoff)

        pr_handoff.handle_pushed_branch(pushed_result, state=fake_state)

        assert len(fake_state.pending_calls) == 1
        recorded = fake_state.pending_calls[0]
        assert recorded["branch"] == pushed_result.reason
        assert recorded["tree_hash"] == pushed_result.tree_hash

    def test_recording_pending_state_does_not_advance_last_pushed_hash(
        self,
        fake_state: _FakeStateStore,
        pushed_result: PushResult,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Requirement 2.6: only a CONFIRMED PR creation advances
        last_pushed_hash — recording the pending state must not."""
        pr_handoff = _import_module()
        fake_state.last_pushed_hash = "previous-hash"
        monkeypatch.setattr(
            pr_handoff,
            "build_pull_request_payload",
            MagicMock(
                return_value={
                    "repo": "TGS-Labs/Kiro-Config-Bundles",
                    "base": "main",
                    "head": pushed_result.reason,
                    "title": "chore: sync",
                    "body": "Automated config sync.",
                }
            ),
        )
        _patch_notify(monkeypatch, pr_handoff)

        pr_handoff.handle_pushed_branch(pushed_result, state=fake_state)

        assert fake_state.last_pushed_hash == "previous-hash"


# ---------------------------------------------------------------------------
# Requirement 2.3 / tasks.md 3.3: operator notification
# ---------------------------------------------------------------------------


class TestNotifiesOperator:
    """The handoff notifies the operator that a PR handoff is pending. The
    exact notification mechanism is unspecified as of this task — the test
    mocks whatever seam `pr_handoff.py` actually exposes for it (a module-
    level `notify_operator`/`notify` callable is the natural extension
    point, matching how `build_pull_request_payload` is itself imported
    into this module's namespace and therefore independently patchable)."""

    def test_handoff_invokes_the_notification_seam_on_success_path(
        self,
        fake_state: _FakeStateStore,
        pushed_result: PushResult,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pr_handoff = _import_module()
        monkeypatch.setattr(
            pr_handoff,
            "build_pull_request_payload",
            MagicMock(
                return_value={
                    "repo": "TGS-Labs/Kiro-Config-Bundles",
                    "base": "main",
                    "head": pushed_result.reason,
                    "title": "chore: sync",
                    "body": "Automated config sync.",
                }
            ),
        )
        notify_mock = _patch_notify(monkeypatch, pr_handoff)

        pr_handoff.handle_pushed_branch(pushed_result, state=fake_state)

        assert notify_mock.call_count == 1

    def test_handoff_does_not_notify_for_a_no_op_push(
        self, fake_state: _FakeStateStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pr_handoff = _import_module()
        monkeypatch.setattr(pr_handoff, "build_pull_request_payload", MagicMock())
        notify_mock = _patch_notify(monkeypatch, pr_handoff)

        no_op_result = PushResult(outcome="no-op", tree_hash="d" * 64)
        pr_handoff.handle_pushed_branch(no_op_result, state=fake_state)

        assert notify_mock.call_count == 0


# ---------------------------------------------------------------------------
# Requirement 2.6: the explicit confirmation entry point
# ---------------------------------------------------------------------------


class TestConfirmationEntryPoint:
    """`pr_handoff.py` exposes an explicit confirmation entry point (called
    out-of-band, from a KiroCrew agent context via the complete-pr-handoff
    skill, per design.md) that ONLY THEN advances `last_pushed_hash`."""

    def test_confirm_pr_created_advances_last_pushed_hash(
        self, fake_state: _FakeStateStore
    ) -> None:
        pr_handoff = _import_module()
        fake_state.last_pushed_hash = None

        pr_handoff.confirm_pr_created(
            tree_hash="e" * 64,
            branch="config-sync/deadbeef-eeeeee",
            pr_url="https://github.com/TGS-Labs/Kiro-Config-Bundles/pull/999",
            state=fake_state,
        )

        assert fake_state.last_pushed_hash == "e" * 64

    def test_confirm_pr_created_records_the_pr_url(
        self, fake_state: _FakeStateStore
    ) -> None:
        pr_handoff = _import_module()

        pr_handoff.confirm_pr_created(
            tree_hash="f" * 64,
            branch="config-sync/deadbeef-ffffff",
            pr_url="https://github.com/TGS-Labs/Kiro-Config-Bundles/pull/1000",
            state=fake_state,
        )

        assert len(fake_state.confirm_calls) == 1
        assert (
            fake_state.confirm_calls[0]["pr_url"]
            == "https://github.com/TGS-Labs/Kiro-Config-Bundles/pull/1000"
        )

    def test_no_confirmation_call_leaves_last_pushed_hash_unchanged(
        self,
        fake_state: _FakeStateStore,
        pushed_result: PushResult,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The decisive property behind requirements.md 2.6: running the
        push-and-handoff path WITHOUT ever calling the confirmation entry
        point must leave `last_pushed_hash` exactly as it was, so the next
        tick's hash-gate does not treat the change as already delivered and
        silently drops the retry."""
        pr_handoff = _import_module()
        fake_state.last_pushed_hash = "unrelated-previous-hash"
        monkeypatch.setattr(
            pr_handoff,
            "build_pull_request_payload",
            MagicMock(
                return_value={
                    "repo": "TGS-Labs/Kiro-Config-Bundles",
                    "base": "main",
                    "head": pushed_result.reason,
                    "title": "chore: sync",
                    "body": "Automated config sync.",
                }
            ),
        )
        _patch_notify(monkeypatch, pr_handoff)

        pr_handoff.handle_pushed_branch(pushed_result, state=fake_state)

        assert fake_state.last_pushed_hash == "unrelated-previous-hash"
        assert fake_state.confirm_calls == []


# ---------------------------------------------------------------------------
# Requirement 2.7: every failure path is recorded with its cause
# ---------------------------------------------------------------------------


class TestFailurePathsAreRecordedWithCause:
    def test_payload_build_failure_is_recorded_with_its_cause(
        self,
        fake_state: _FakeStateStore,
        pushed_result: PushResult,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pr_handoff = _import_module()
        monkeypatch.setattr(
            pr_handoff,
            "build_pull_request_payload",
            MagicMock(side_effect=ValueError("malformed branch name")),
        )
        _patch_notify(monkeypatch, pr_handoff)

        pr_handoff.handle_pushed_branch(pushed_result, state=fake_state)

        assert len(fake_state.pending_failure_calls) == 1
        reason = fake_state.pending_failure_calls[0]["reason"]
        assert "malformed branch name" in reason

    def test_payload_build_failure_does_not_advance_last_pushed_hash(
        self,
        fake_state: _FakeStateStore,
        pushed_result: PushResult,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pr_handoff = _import_module()
        fake_state.last_pushed_hash = "prior-hash"
        monkeypatch.setattr(
            pr_handoff,
            "build_pull_request_payload",
            MagicMock(side_effect=ValueError("boom")),
        )
        _patch_notify(monkeypatch, pr_handoff)

        pr_handoff.handle_pushed_branch(pushed_result, state=fake_state)

        assert fake_state.last_pushed_hash == "prior-hash"

    def test_notification_failure_is_recorded_with_its_cause(
        self,
        fake_state: _FakeStateStore,
        pushed_result: PushResult,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failure to notify the operator is a failure of the handoff
        itself (the operator now has no way to learn a PR is pending) and
        must be recorded like any other failure path, per requirements.md
        2.7's "for any reason" wording."""
        pr_handoff = _import_module()
        monkeypatch.setattr(
            pr_handoff,
            "build_pull_request_payload",
            MagicMock(
                return_value={
                    "repo": "TGS-Labs/Kiro-Config-Bundles",
                    "base": "main",
                    "head": pushed_result.reason,
                    "title": "chore: sync",
                    "body": "Automated config sync.",
                }
            ),
        )
        notify_seam = _find_notify_seam(pr_handoff)
        monkeypatch.setattr(
            pr_handoff,
            notify_seam,
            MagicMock(side_effect=RuntimeError("notification channel down")),
        )

        pr_handoff.handle_pushed_branch(pushed_result, state=fake_state)

        all_failure_reasons = [
            call["reason"] for call in fake_state.pending_failure_calls
        ] + [call["reason"] for call in fake_state.push_failure_calls]
        assert any(
            "notification channel down" in reason for reason in all_failure_reasons
        )

    def test_a_reported_failed_pr_creation_is_recorded_and_does_not_advance_hash(
        self, fake_state: _FakeStateStore
    ) -> None:
        """Per skills/complete-pr-handoff/SKILL.md: when the out-of-band PR
        creation itself fails (Buildo returns an error), the agent context
        reports that failure back into app state rather than calling
        `confirm_pr_created`. `pr_handoff.py` must expose a matching entry
        point that records the cause and leaves `last_pushed_hash` alone."""
        pr_handoff = _import_module()
        fake_state.last_pushed_hash = "untouched-hash"

        pr_handoff.report_pr_creation_failed(
            reason="Buildo create_pull_request returned 422",
            state=fake_state,
        )

        assert fake_state.last_pushed_hash == "untouched-hash"
        assert len(fake_state.pending_failure_calls) == 1
        assert "422" in fake_state.pending_failure_calls[0]["reason"]


# ---------------------------------------------------------------------------
# Requirement 2.7: no tight retry loop inside one call
# ---------------------------------------------------------------------------


class TestNoInternalRetryLoop:
    """`handle_pushed_branch` must make a SINGLE attempt at building the
    payload and notifying — never retry internally within one call. Retrying
    is the next cron tick's job, not this function's."""

    def test_payload_build_is_attempted_exactly_once_even_on_failure(
        self,
        fake_state: _FakeStateStore,
        pushed_result: PushResult,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pr_handoff = _import_module()
        build_payload_mock = MagicMock(side_effect=ValueError("transient-looking"))
        monkeypatch.setattr(
            pr_handoff, "build_pull_request_payload", build_payload_mock
        )
        _patch_notify(monkeypatch, pr_handoff)

        pr_handoff.handle_pushed_branch(pushed_result, state=fake_state)

        assert build_payload_mock.call_count == 1

    def test_notification_is_attempted_exactly_once_even_on_failure(
        self,
        fake_state: _FakeStateStore,
        pushed_result: PushResult,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pr_handoff = _import_module()
        monkeypatch.setattr(
            pr_handoff,
            "build_pull_request_payload",
            MagicMock(
                return_value={
                    "repo": "TGS-Labs/Kiro-Config-Bundles",
                    "base": "main",
                    "head": pushed_result.reason,
                    "title": "chore: sync",
                    "body": "Automated config sync.",
                }
            ),
        )
        notify_seam = _find_notify_seam(pr_handoff)
        notify_mock = MagicMock(side_effect=RuntimeError("down"))
        monkeypatch.setattr(pr_handoff, notify_seam, notify_mock)

        pr_handoff.handle_pushed_branch(pushed_result, state=fake_state)

        assert notify_mock.call_count == 1


# ---------------------------------------------------------------------------
# Module import shape
# ---------------------------------------------------------------------------


class TestModuleExposesExpectedEntryPoints:
    """A structural check that the module's public surface matches what
    tasks.md 3.3 and this task's own instructions describe, independent of
    any one test scenario above."""

    def test_module_exposes_handle_pushed_branch(self) -> None:
        pr_handoff = _import_module()
        assert callable(pr_handoff.handle_pushed_branch)

    def test_module_exposes_confirm_pr_created(self) -> None:
        pr_handoff = _import_module()
        assert callable(pr_handoff.confirm_pr_created)

    def test_module_exposes_report_pr_creation_failed(self) -> None:
        pr_handoff = _import_module()
        assert callable(pr_handoff.report_pr_creation_failed)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOTIFY_SEAM_CANDIDATES = ("notify_operator", "notify", "send_notification")


def _find_notify_seam(pr_handoff: Any) -> str:
    """Find whichever notification seam name `pr_handoff.py` actually
    exposes at module level, per this task's own instruction to mock
    "whatever seam you introduce" rather than assume one fixed name."""
    for candidate in _NOTIFY_SEAM_CANDIDATES:
        if hasattr(pr_handoff, candidate):
            return candidate
    raise AssertionError(
        "backend.pr_handoff must expose a module-level notification seam "
        f"named one of {_NOTIFY_SEAM_CANDIDATES!r} so it can be mocked in "
        "tests; none was found on the module."
    )


def _patch_notify(monkeypatch: pytest.MonkeyPatch, pr_handoff: Any) -> MagicMock:
    """Patch whichever notification seam the module exposes and return the
    mock, so call-count assertions can be made against it."""
    seam_name = _find_notify_seam(pr_handoff)
    mock = MagicMock()
    monkeypatch.setattr(pr_handoff, seam_name, mock)
    return mock
