"""The push job (design.md `backend/push.py`), a cron `command` target.

Runs as a plain script (``python3 backend/push.py``, per `app.json`'s
``"command"`` cron entry) — never a `message` cron target — so a quiet tick
costs zero LLM tokens (requirements.md 2.1). This module therefore defines
no LLM/agent-invocation surface anywhere in its symbol table.

Pipeline (design.md step list):

1. Collect (allowlist) -> redact -> canonical serialize.
2. ``tree_hash`` = hash over sorted ``(relpath, sha256(content))`` pairs.
3. If ``tree_hash == state.last_pushed_hash``: return ``no-op``. No clone,
   no network, no tokens. This is the common case and it is the whole
   reason the push is a `command` cron rather than an agent prompt.
4. (change path) scan for secrets, clone/branch/commit/push, open the PR.

Only step 3 — the hash-gate no-op path — is implemented here. The change
path (steps 4 onward: secret scan, clone/checkout, commit, push, PR) is a
separate task; see the ``TODO(3.2)`` seam in :func:`run`.
"""

from __future__ import annotations

import hashlib
import subprocess  # noqa: F401  # spied on by tests; used by the change path (3.2)
from dataclasses import dataclass
from typing import Mapping

from backend import collect, redact, state
from backend.safety import git_safety, push_policy  # noqa: F401  # used by 3.2


@dataclass(frozen=True)
class PushResult:
    """Outcome of a single push-job tick."""

    outcome: str
    tree_hash: str


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


def run() -> PushResult:
    """Run one push-job tick: the hash-gate, and (on a miss) the push itself.

    Zero-argument, matching the `command` cron entrypoint shape
    (`python3 backend/push.py`) — no agent/LLM context is required to call
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

    # TODO(3.2): implement the change path here — scan_content_for_secrets,
    # clone/checkout the bundle repo via git_safety.git_argv, write the
    # tree, branch as config-sync/<instance-id>-<short-hash>, commit
    # (redacted message), push the named branch, open the PR, and only
    # then call store.record_push_success(). Never construct a git_argv or
    # touch subprocess before this seam.
    raise NotImplementedError(
        "backend.push change path is not yet implemented (task 3.2)"
    )


if __name__ == "__main__":
    run()
