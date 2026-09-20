"""PR handoff for the push job (design.md `backend/pr_handoff.py`, task 3.3).

Bridges `backend/push.py`'s ``PushResult`` to `backend/buildo_pr.py`'s
payload builder and `backend/state.py`'s pending-PR bookkeeping, tracing to
requirements.md:

- 2.3: on a pushed branch, build the PR payload and hand it off.
- 2.6: ``last_pushed_hash`` advances only once PR creation is CONFIRMED —
  never at push time and never at payload-build/notify time. A push with no
  matching :func:`confirm_pr_created` call must leave it unchanged, so the
  next tick retries rather than silently treating the change as delivered.
- 2.7: every failure is recorded with its cause, in a single attempt with
  no internal retry loop — retrying is the next cron tick's job.

This app's own MCP/agent context is read-only for GitHub and has no `gh`
CLI (see `backend/buildo_pr.py`), so this module never calls Buildo itself.
:func:`handle_pushed_branch` only builds the payload, records it as
*pending*, and notifies the operator; a KiroCrew agent context completes
the handoff via the `complete-pr-handoff` skill, then reports the outcome
back through :func:`confirm_pr_created` (success) or
:func:`report_pr_creation_failed` (failure).
"""

from __future__ import annotations

from backend.buildo_pr import build_pull_request_payload
from backend.push import PushResult
from backend.state import StateStore

#: Outcome value push.run() returns for a successfully pushed branch — the
#: only outcome this module acts on. Every other outcome (a no-op hash-gate
#: hit, or any `refused-*` policy refusal) already carries no branch to
#: open a PR from, or already recorded its own failure inside push.run(),
#: so the handoff does nothing for it.
_PUSHED_OUTCOME = "pushed"

_PR_TITLE = "chore: sync configuration"
_PR_BODY = "Automated config sync."


def notify_operator(*, branch: str, payload: dict[str, object]) -> None:
    """Notify the operator that a PR handoff is pending.

    The real notification channel is out of scope for this task — this is
    the seam tests patch. A production implementation wires this to the
    app's actual notification path; until then this is a deliberate no-op
    so the handoff completes without requiring a live channel.
    """


def handle_pushed_branch(result: PushResult, *, state: StateStore) -> None:
    """Handle one `push.run()` result: hand off a pushed branch to PR.

    A single attempt, no internal retry: a payload-build failure or a
    notify failure is recorded via `state.record_pr_pending_failure` and
    the function returns — the next cron tick is what retries, never this
    call itself (requirements.md 2.7).

    Args:
        result: the `PushResult` `push.run()` returned this tick.
        state: the state store to record the handoff's outcome into.
    """
    if result.outcome != _PUSHED_OUTCOME:
        return

    branch = result.reason
    try:
        payload = build_pull_request_payload(branch, _PR_TITLE, _PR_BODY)
    except Exception as exc:  # broad: any failure recorded, not swallowed
        state.record_pr_pending_failure(
            reason=str(exc), tree_hash=result.tree_hash, branch=branch
        )
        return

    state.record_pr_pending(branch=branch, tree_hash=result.tree_hash, payload=payload)

    try:
        notify_operator(branch=branch, payload=payload)
    except Exception as exc:  # broad: any failure recorded, not swallowed
        state.record_pr_pending_failure(
            reason=str(exc), tree_hash=result.tree_hash, branch=branch
        )
        return


def confirm_pr_created(
    *, tree_hash: str, branch: str, pr_url: str, state: StateStore
) -> None:
    """Confirm a pending PR was actually created.

    The explicit, out-of-band confirmation entry point (called from a
    KiroCrew agent context via the `complete-pr-handoff` skill once Buildo
    has actually opened the PR). ONLY this call advances
    `state.last_pushed_hash` for the PR-handoff flow — a push whose branch
    never reaches this call leaves the hash unchanged, so the next tick
    retries (requirements.md 2.6).

    Args:
        tree_hash: the pushed tree hash to advance `last_pushed_hash` to.
        branch: the branch the PR was opened from.
        pr_url: the created PR's URL.
        state: the state store to record the confirmation into.
    """
    state.confirm_pr_created(tree_hash=tree_hash, branch=branch, pr_url=pr_url)


def report_pr_creation_failed(
    *, reason: str, tree_hash: str, branch: str, state: StateStore
) -> None:
    """Report that the out-of-band PR creation itself failed.

    Called from a KiroCrew agent context via the `complete-pr-handoff`
    skill when Buildo reports an error instead of a created PR. Records
    the cause and leaves `state.last_pushed_hash` untouched, so the sync
    job remains retryable on its next run (requirements.md 2.6, 2.7).

    Args:
        reason: the failure's cause (e.g. Buildo's error message/code).
        tree_hash: the tree hash the failed PR attempt was for — the same
            value the caller was originally handed when the push happened.
            Used to check this report is still about the CURRENT
            `pending_pr` (H-NEW-2): a report that arrives after a newer
            push has already superseded it is recorded as stale rather
            than clearing the newer pending-PR record.
        branch: the branch the failed PR attempt was for.
        state: the state store to record the failure into.
    """
    state.record_pr_pending_failure(reason=reason, tree_hash=tree_hash, branch=branch)
