"""Message redaction for config-sync — sanitizes every commit message, PR
title, PR body, log line, and UI/API field the app emits so no unredacted
credential or exfiltration URL can appear in any of them.

Ported from ``backend/commit.py``'s credential-redaction path over
``kiro_crew.security.redact`` (design.md's ``backend/safety/`` component;
tasks.md 2.3), tracing to requirements.md 3.8, 7.5, 8.4.

WHY THIS MODULE REWRITES RATHER THAN REFUSES: unlike
``push_policy.py``'s ``scan_content_for_secrets`` (which only DETECTS and
refuses, because rewriting a diff would corrupt it), a commit message, PR
title, PR body, log line, or UI/API field is prose — sanitizing it in place
is exactly the point, so this module always returns a usable string rather
than a verdict.

Follows the exact import pattern used in
``backend/safety/push_policy.py``: a bare ``try/except Exception`` around
the ``kiro_crew.security`` import, since the scanners are an OPTIONAL system
dependency and an unimportable/unrunnable scanner must fail closed. Fail
closed here means returning a fixed, safe placeholder rather than the
original (possibly credential-carrying) text — the opposite failure mode
from push_policy's "refuse the push", since a redaction path has no way to
refuse an emission that is already happening (a log line, a UI field), only
a choice between the raw text and a safe one.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Returned in place of the original text when the credential/exfiltration
#: scanners cannot be imported or raise. A fixed literal — never derived
#: from the input — so an unscannable message can never leak into a log
#: line, commit message, or UI field unredacted.
REDACTION_UNAVAILABLE_PLACEHOLDER = (
    "[message redaction unavailable — original text withheld]"
)


def redact_message(text: str) -> str:
    """Return ``text`` with every credential and exfiltration URL removed.

    Runs both ``kiro_crew.security.redact_credentials`` and
    ``kiro_crew.security.redact_exfiltration_urls`` over ``text`` and
    returns the fully sanitized result. Safe to call on any commit message,
    PR title, PR body, log line, or UI/API field before it is emitted
    (requirements.md 3.8, 7.5).

    FAIL-CLOSED: if the scanners cannot be imported or raise, the original
    text is NOT returned — a fixed placeholder is, so an unscannable message
    can never carry an unredacted credential into a log, commit message, or
    UI field.
    """
    try:
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls
    except Exception:  # noqa: BLE001 - no scanner, fail closed
        logger.warning(
            "credential scanners unavailable — withholding original text",
            exc_info=True,
        )
        return REDACTION_UNAVAILABLE_PLACEHOLDER
    if not text:
        return text
    try:
        sanitized, _cred_hits = redact_credentials(text)
        sanitized, _exfil_hits = redact_exfiltration_urls(sanitized)
    except Exception:  # noqa: BLE001 - unrunnable scanner, fail closed
        logger.warning(
            "credential scanners raised — withholding original text",
            exc_info=True,
        )
        return REDACTION_UNAVAILABLE_PLACEHOLDER
    return str(sanitized)
