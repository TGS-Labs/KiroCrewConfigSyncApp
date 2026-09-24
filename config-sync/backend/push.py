"""The push job (design.md `backend/push.py`), a cron `command` target.

Runs as a plain module invocation (``python3 -m backend.push``, per
`app.json`'s ``"command"`` cron entry, which ``cd``'s into the app's own
installed directory first so the ``backend`` package import resolves) —
never a `message` cron target — so a quiet tick costs zero LLM tokens
(requirements.md 2.1). This module therefore defines no LLM/agent-invocation
surface anywhere in its symbol table.

Pipeline (design.md step list):

1. Collect (allowlist) -> redact -> canonical serialize.
2. ``tree_hash`` = hash over sorted ``(relpath, sha256(content))`` pairs.
3. If ``tree_hash == state.last_pushed_hash``: return ``no-op``. No clone,
   no network, no tokens. This is the common case and it is the whole
   reason the push is a `command` cron rather than an agent prompt.
3a. Else if ``tree_hash`` matches the tree hash already recorded on
    ``state.pending_pr`` (i.e. this exact change was already pushed on a
    prior tick and is only waiting on the out-of-band
    `pr_handoff.confirm_pr_created`/`report_pr_creation_failed` call):
    return ``awaiting-pr-confirmation``. Also no clone, no network, no git
    invocation of any kind — a re-entrant tick before confirmation arrives
    must never repeat the push. This is a THIRD case, distinct from both the
    no-op above (hash already delivered) and the change path below (hash
    genuinely new): the bundle-repo clone already has this exact commit on
    its branch, so re-running the commit/push sequence would find nothing
    to commit and fail, and that failure must not be recorded as a
    fabricated push failure for a push that already succeeded.
3b. Else if ``state.last_push`` names this exact ``tree_hash`` with
    ``pr_url is None`` (i.e. a prior tick pushed this exact content and
    handed off to `pr_handoff`, but the out-of-band PR-open attempt for it
    was later reported failed via `report_pr_creation_failed` — which
    clears ``pending_pr`` per H-NEW-1 so the hash-gate does not loop
    forever on 3a — leaving no `pending_pr` entry even though the branch
    is still sitting on the remote with that exact commit): return
    ``retry-pr-only``. This is a FOURTH case, distinct from 3a: no clone,
    no fetch, no checkout, no commit, no push — none of the git pipeline
    runs at all, because the content is already on the remote branch and
    `git commit` would find nothing to commit (the exact regression this
    case exists to prevent — a second occurrence of the N1 class of bug,
    this time reachable through the PR-failure retry path rather than the
    pre-confirmation path 3a already covers). Only the PR-open step is
    retried, via `pr_handoff.handle_pushed_branch`, reusing the branch
    `last_push` already recorded.
4. ``scan_content_for_secrets`` over every file's content. A finding
   refuses the whole push (code + count only, never the matched text, and
   never a git call — not even a clone probe).
5. Clone/update the bundle repo into the app's own state directory, write
   the redacted tree into a working copy, ``git add``.
6. Branch: ``config-sync/<instance-id>-<short-hash>``.
   ``authorize_direct_push`` guards the target; a protected, empty, or
   ambiguous branch refuses before any git call.
7. Commit (redacted message), push the named branch explicitly. Record the
   push via `state.record_branch_pushed` (which does NOT advance
   ``last_pushed_hash`` — see step 8) then hand off to
   `backend.pr_handoff.handle_pushed_branch`, which builds the PR payload,
   records a pending-PR state entry, and notifies the operator. Opening the
   PR itself happens out-of-band (task 3.3, ``backend/buildo_pr.py`` +
   `backend/pr_handoff.py`'s ``confirm_pr_created``/
   ``report_pr_creation_failed``) — this module's own `run()` stops at
   "branch pushed" and handed off.
8. Record ``last_pushed_hash`` only once the push AND PR creation both
   succeed — via `pr_handoff.confirm_pr_created`. A refusal, a mid-write
   failure, or a pushed branch with no confirmed PR yet never advances it,
   so the next tick retries.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from backend import collect, redact, state
from backend.safety import git_safety, push_policy
from backend.safety import redact_msg

#: Name/URL of the bundle repo every sync push targets, matching
#: `backend/buildo_pr.py`'s `TARGET_REPO` (`TGS-Labs/Kiro-Config-Bundles`).
BUNDLE_REPO_URL = "https://github.com/TGS-Labs/Kiro-Config-Bundles.git"

#: Directory name, under the app's own state directory
#: (`state.get_state_dir()`), that holds the bundle repo's working clone.
#: Never inside either tracked configuration root, matching state.py's own
#: isolation guarantee.
_BUNDLE_CLONE_DIRNAME = "bundle-repo"


@dataclass(frozen=True)
class PushResult:
    """Outcome of a single push-job tick."""

    outcome: str
    tree_hash: str
    reason: str = ""


def tree_hash(tree: Mapping[str, bytes]) -> str:
    """Hash a collected (and, for the real gate, redacted) tree.

    Computed over the sorted ``(relpath, sha256(content))`` pairs (design.md
    step 2), so the result is insensitive to the input mapping's iteration
    order and changes whenever any tracked file's content changes.

    Args:
        tree: mapping of relative path -> file content bytes.

    Returns:
        A hex-encoded SHA-256 digest of the canonical pair sequence.
    """
    digest = hashlib.sha256()
    for relpath, content in sorted(tree.items(), key=lambda item: item[0]):
        content_hash = hashlib.sha256(content).hexdigest()
        digest.update(relpath.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content_hash.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _instance_id() -> str:
    """A short, stable identifier for this host/instance.

    Used only as one component of the push branch name
    (``config-sync/<instance-id>-<short-hash>``) so two instances pushing
    concurrently do not collide on the same branch. Derived from the app's
    own state directory path — stable across ticks on the same host,
    requires no new external dependency, and carries no credential or
    hostname PII into a branch name that ends up on a public repo.
    """
    digest = hashlib.sha256(str(state.get_state_dir()).encode("utf-8")).hexdigest()
    return digest[:12]


def _branch_name(current_hash: str) -> str:
    """The push target branch name, per design.md step 6.

    ``config-sync/<instance-id>-<short-hash>``, where ``short-hash`` is the
    first 12 hex characters of the tree hash — enough to make the branch
    name unique per change without being unwieldy.
    """
    return f"config-sync/{_instance_id()}-{current_hash[:12]}"


def _scan_tree_for_secrets(redacted: Mapping[str, bytes]) -> tuple[bool, str]:
    """Run the content secret scan over every file in the redacted tree.

    Concatenates each file's decoded text (best-effort; undecodable content
    is skipped — `push_policy.scan_content_for_secrets` scans text, and a
    binary blob that cannot decode carries no scannable credential text
    either way) and scans it as one call, per push_policy's "one gate for
    every exit" design. Returns the first non-clean verdict found, short
    circuiting the moment one file scans dirty.
    """
    for _relpath, content in sorted(redacted.items(), key=lambda item: item[0]):
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            continue
        clean, note = push_policy.scan_content_for_secrets(text)
        if not clean:
            return clean, note
    return True, push_policy.SCAN_OK


def _write_working_copy(clone_dir: Path, redacted: Mapping[str, bytes]) -> None:
    """Write every file in the redacted tree into the working copy.

    Bytes written are exactly `redacted`'s own values — never re-derived or
    re-serialized — so the working copy is byte-for-byte what
    `redact.redact()` actually produced. Fails closed: the first
    unreadable/unwritable tracked file raises immediately (design.md step
    5's "no partial commit"), before any git call.
    """
    for relpath, content in sorted(redacted.items(), key=lambda item: item[0]):
        target = clone_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def run() -> PushResult:
    """Run one push-job tick: the hash-gate, and (on a miss) the push itself.

    Zero-argument, matching the `command` cron entrypoint shape
    (`python3 -m backend.push`) — no agent/LLM context is required to call
    it (requirements.md 2.1).

    Returns:
        A :class:`PushResult` describing the outcome. On a hash match this
        is always ``outcome="no-op"``, with no clone, network, commit, or
        git invocation of any kind, and `state.last_pushed_hash` left
        unchanged.
    """
    collected = collect.collect()
    redacted = redact.redact(collected)
    current_hash = tree_hash(redacted)

    store = state.load_state()
    if current_hash == store.last_pushed_hash:
        return PushResult(outcome="no-op", tree_hash=current_hash)

    # Step 3a: this exact change was already pushed on a prior tick and is
    # still waiting on the out-of-band PR-confirmation call — re-entering
    # the change path below would re-run commit/push against a bundle-repo
    # clone that already holds this exact commit on that branch (the branch
    # name is derived from the hash), so `git commit` would find nothing to
    # commit, fail, and get recorded as a FABRICATED push failure for a push
    # that actually already succeeded. Detect it and return a distinct
    # outcome instead, with zero git/network calls — same zero-call
    # guarantee as the no-op path above, just gated on a different field.
    pending_pr = store.pending_pr
    if pending_pr is not None and pending_pr.get("tree_hash") == current_hash:
        return PushResult(
            outcome="awaiting-pr-confirmation",
            tree_hash=current_hash,
            reason=str(pending_pr.get("branch", "")),
        )

    # Step 3b: a prior tick pushed this exact content and handed off to
    # pr_handoff, but the out-of-band PR-open attempt was later reported
    # failed via `report_pr_creation_failed` — which clears `pending_pr`
    # (H-NEW-1), so this tick's `pending_pr` is None even though the branch
    # is still sitting on the remote with this exact commit.
    # `state.last_push` is the one record that survives that clear:
    # `record_branch_pushed` set it when the branch was pushed and nothing
    # since has overwritten it (only `record_push_success` — a CONFIRMED
    # PR — would, and that also advances `last_pushed_hash`, which the
    # no-op check above would have already caught). So
    # `last_push.tree_hash == current_hash and last_push.pr_url is None`
    # unambiguously means: this content is already on the remote branch,
    # nothing to commit or push, only the PR-open step needs retrying.
    # Skip clone/fetch/checkout/commit/push ENTIRELY — re-running any of
    # them would hit `git commit`'s "nothing to commit" and fabricate a
    # push failure for a push that already succeeded (the N1 class of bug,
    # reachable here through the PR-failure retry path rather than 3a's
    # pre-confirmation path).
    last_push = store.last_push
    if (
        pending_pr is None
        and last_push is not None
        and last_push.get("tree_hash") == current_hash
        and last_push.get("pr_url") is None
    ):
        branch = str(last_push.get("branch", ""))
        result = PushResult(
            outcome="retry-pr-only", tree_hash=current_hash, reason=branch
        )

        from backend import pr_handoff

        pr_handoff.handle_pushed_branch(result, state=store)

        return result

    # Step 4: scan every file's redacted content for secrets BEFORE any git
    # call — not even a clone probe. A finding (or an unavailable scanner,
    # which fails closed the same way) refuses the whole push and reports
    # only the code/count push_policy handed back, never matched text.
    clean, scan_note = _scan_tree_for_secrets(redacted)
    if not clean:
        store.record_push_failure(reason=scan_note)
        return PushResult(
            outcome="refused-secret-scan", tree_hash=current_hash, reason=scan_note
        )

    # Step 6 (authorization check ahead of any git call): decide the target
    # branch and get authorize_direct_push's ruling. A refusal (main,
    # protected, empty, ambiguous) stops here with the policy's own reason
    # string — still no git call of any kind.
    branch = _branch_name(current_hash)
    authorized, auth_reason = push_policy.authorize_direct_push(branch=branch)
    if not authorized:
        store.record_push_failure(reason=auth_reason)
        return PushResult(
            outcome="refused-branch-authorization",
            tree_hash=current_hash,
            reason=auth_reason,
        )

    # Materialize the redacted tree into a scratch working copy BEFORE any
    # git call of any kind (clone/fetch/checkout included) — an unreadable/
    # unwritable tracked file must fail closed with no partial operation
    # and no git invocation at all, not merely no push (design.md step 5's
    # "no partial commit"; requirement 3.1). This first write is the
    # fail-closed check; the bundle clone's own working tree is written
    # again below once it exists on disk.
    #
    # Everything from here through the final push is wrapped in one
    # try/except: a `CalledProcessError` (a git subprocess) or `OSError`
    # (an unreadable/unwritable tracked file) must be recorded via
    # `record_push_failure` before propagating — requirement 2.7 requires
    # every failure be recorded with its cause; it does not require the
    # failure be swallowed instead of raised, so the original exception is
    # re-raised after recording (matching the existing raise-based contract
    # the no-op/refusal paths' callers already rely on).
    clone_dir = state.get_state_dir() / _BUNDLE_CLONE_DIRNAME
    scratch_dir = state.get_state_dir() / "push-scratch"

    try:
        _write_working_copy(scratch_dir, redacted)

        # Steps 5/7: clone/update the bundle repo, write the checked tree
        # into the working copy, commit (redacted message), push the named
        # branch.
        clone_dir.mkdir(parents=True, exist_ok=True)

        if not (clone_dir / ".git").exists():
            subprocess.run(
                git_safety.git_argv(
                    clone_dir.parent, "clone", BUNDLE_REPO_URL, str(clone_dir)
                ),
                check=True,
            )
        else:
            subprocess.run(
                git_safety.git_argv(clone_dir, "fetch", "origin"), check=True
            )

        subprocess.run(
            git_safety.git_argv(clone_dir, "checkout", "-B", branch), check=True
        )

        _write_working_copy(clone_dir, redacted)

        subprocess.run(git_safety.git_argv(clone_dir, "add", "-A"), check=True)

        commit_message = redact_msg.redact_message(
            f"chore(config-sync): sync configuration ({current_hash[:12]})"
        )
        subprocess.run(
            git_safety.git_argv(clone_dir, "commit", "-m", commit_message),
            check=True,
        )
        subprocess.run(
            git_safety.git_argv(clone_dir, "push", "-u", "origin", branch),
            check=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        store.record_push_failure(reason=str(exc))
        raise

    # Step 7/8: a branch reached the remote, but no PR exists yet — record
    # the push WITHOUT advancing `last_pushed_hash` (only a confirmed PR
    # does that; requirement 2.6), then hand off to `pr_handoff` so the
    # pending-PR record and operator notification actually happen.
    store.record_branch_pushed(tree_hash=current_hash, branch=branch)
    result = PushResult(outcome="pushed", tree_hash=current_hash, reason=branch)

    from backend import pr_handoff

    pr_handoff.handle_pushed_branch(result, state=store)

    return result


if __name__ == "__main__":
    run()
