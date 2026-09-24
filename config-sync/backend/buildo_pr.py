"""Builds the `create_pull_request` payload for TGS-Labs/Kiro-Config-Bundles.

See design.md's "Push credential and PR-creation route — RESOLVED" and
tasks.md 3.4, tracing to requirements.md 2.3, 2.6, 2.7, 3.8.

This app's own MCP/agent context is read-only for GitHub and has no `gh`
CLI, and Buildo is the sanctioned PR tool for TGS-Labs repos. This module
does NOT call any MCP tool itself — it only builds the plain dict a
KiroCrew agent context passes to Buildo MCP's `create_pull_request` (see
`skills/complete-pr-handoff/SKILL.md`).

Per the org's disallow-squash rule, the built payload OMITS `merge_method`
entirely so the tool's own default (`merge`) applies. Never add a
`merge_method` key here, under any input, including a caller-supplied
value — the key's absence from the payload is the acceptance criterion.
"""

from __future__ import annotations

from typing import Any, Dict

from backend.safety.redact_msg import redact_message

#: The bundle repo tasks.md 3.4 names, and the base branch every sync PR
#: targets. Fixed literals — never derived from caller input.
TARGET_REPO = "TGS-Labs/Kiro-Config-Bundles"
TARGET_BASE = "main"


def build_pull_request_payload(
    head_branch: str, title: str, body: str
) -> Dict[str, Any]:
    """Build the `create_pull_request` payload for a config-sync PR.

    Args:
        head_branch: the pushed branch to open the PR from.
        title: the PR title. Passed through `redact_message` before being
            placed in the payload.
        body: the PR body. Passed through `redact_message` before being
            placed in the payload.

    Returns:
        A plain dict with exactly the keys `repo`, `base`, `head`, `title`,
        `body` — all string-valued. `merge_method` is never present, so the
        Buildo MCP tool's own default (`merge`) applies.
    """
    return {
        "repo": TARGET_REPO,
        "base": TARGET_BASE,
        "head": head_branch,
        "title": redact_message(title),
        "body": redact_message(body),
    }
