"""The poll job (design.md `backend/poll.py`), a cron `command` target.

Runs as a plain module invocation (``python3 -m backend.poll``), never a
``message`` cron target, so a quiet tick costs zero LLM tokens
(requirements.md 4.1). Resolves the bundle repo's default-branch head via a
single ``git ls-remote`` — never a full clone (design.md: "`git ls-remote
<bundle-repo> <default-branch>` -> head SHA. Unchanged -> exit.").

This task (4.1) implements only the ls-remote/signal contract itself:

1. Unchanged head (``ls-remote`` result == ``state.last_seen_sha``): exit 0,
   no notification, no git call beyond the single ``ls-remote``.
2. Changed head: the difference is signalled in the returned result (outcome
   + the new head SHA) so a later wave's classify/pending-record logic
   (tasks 4.2/4.3) has something concrete to consume. This module does not
   itself classify changed paths, write a `pending` record, or notify on the
   changed-head path — the `notify_operator` seam exists for a later wave to
   use, and today's paths never call it (per test_poll.py's scope note).
3. ``ls-remote`` failure (non-zero exit or raised exception): exit non-zero,
   ``state.last_seen_sha`` left unchanged, no notification.

Modelled on `backend/push.py`'s module shape: a frozen result dataclass, a
zero-argument ``run()`` entrypoint, and a patchable ``notify_operator`` seam
matching `backend/pr_handoff.py`'s convention.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from backend import state
from backend.safety import git_safety

#: Name/URL of the bundle repo every poll tick checks, matching
#: `backend/push.py`'s `BUNDLE_REPO_URL` and `backend/buildo_pr.py`'s
#: `TARGET_REPO`/`TARGET_BASE` (`TGS-Labs/Kiro-Config-Bundles`, `main`).
BUNDLE_REPO_URL = "https://github.com/TGS-Labs/Kiro-Config-Bundles.git"
BUNDLE_DEFAULT_BRANCH = "main"


@dataclass(frozen=True)
class PollResult:
    """Outcome of a single poll-job tick.

    ``outcome`` distinguishes the three cases this module reports:
    ``"unchanged"`` (head matches `state.last_seen_sha`), ``"changed"``
    (head differs — the signal task 4.2/4.3 consumes), and
    ``"ls-remote-failed"`` (the resolution itself failed). ``head_sha`` is
    the newly resolved head SHA on ``"changed"``; ``None`` otherwise.
    """

    outcome: str
    head_sha: str | None = None
    reason: str = ""


def notify_operator(*, head_sha: str, reason: str = "") -> None:
    """Notify the operator that the bundle repo's head has changed.

    The real notification channel is out of scope for this task — this is
    the seam tests patch. A production implementation wires this to the
    app's actual notification path; until then this is a deliberate no-op,
    matching `backend/pr_handoff.py`'s `notify_operator` convention so the
    poll job's own unchanged/failure paths can assert it was never called.
    """


def _resolve_remote_head(state_dir_owner: str) -> str:
    """Resolve the bundle repo's default-branch head via a single

    ``git ls-remote`` call, built through `git_safety.git_argv` (never a
    hand-built argv) — the single hardened call-site every host-side git
    invocation in this app must route through.

    Args:
        state_dir_owner: the directory `git_argv`'s ``-C`` targets. A
            pre-clone probe (design.md: "no full clone") has no gitdir of
            its own to protect, so any writable directory works; the app's
            own state directory is used so no scratch directory is
            created just for this call.

    Returns:
        The resolved head SHA (the first whitespace-delimited token of
        ``git ls-remote``'s stdout).

    Raises:
        subprocess.CalledProcessError: if the ``git ls-remote`` process
            exits non-zero.
    """
    completed = subprocess.run(
        git_safety.git_argv(
            state_dir_owner, "ls-remote", BUNDLE_REPO_URL, BUNDLE_DEFAULT_BRANCH
        ),
        capture_output=True,
        text=True,
        check=True,
    )
    stdout = completed.stdout or ""
    first_line = stdout.strip().splitlines()[0] if stdout.strip() else ""
    head_sha = first_line.split()[0] if first_line else ""
    return head_sha


def run() -> PollResult:
    """Run one poll-job tick: resolve the bundle repo's head, compare it

    against `state.last_seen_sha`, and report the outcome.

    Zero-argument, matching the `command` cron entrypoint shape
    (`python3 -m backend.poll`) — no agent/LLM context is required to call
    it (requirements.md 4.1).

    Returns:
        A :class:`PollResult` describing the outcome. On a resolution
        failure this function does not raise: it records no state change
        and returns ``outcome="ls-remote-failed"`` so the cron wrapper can
        turn that into a non-zero process exit without this module owning
        the exit-code mechanics itself.
    """
    store = state.load_state()

    try:
        head_sha = _resolve_remote_head(str(state.get_state_dir()))
    except (subprocess.CalledProcessError, OSError) as exc:
        return PollResult(outcome="ls-remote-failed", reason=str(exc))

    if head_sha == store.last_seen_sha:
        return PollResult(outcome="unchanged", head_sha=head_sha)

    return PollResult(outcome="changed", head_sha=head_sha)


if __name__ == "__main__":
    run()
