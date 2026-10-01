"""Push-safety policy for config-sync — the non-overridable protected-branch
denylist and the content secret scan every push path funnels through.

Ported from
``kiro_crew/apps/builtins/auto_improvement/spine/push_policy.py`` (read-only
reference — never modified). config-sync's push path always intends to push
(there is no operator "direct-commit" opt-in checkbox as in the source app),
so this port drops the F10 direct-commit framing: ``authorize_direct_push``
here takes only ``branch`` and is exercised on branch safety alone
(requirements.md 2.4, 3.6, 3.7).

WHY THIS MODULE OWNS THE DENYLIST: the protected-branch list must be
enforced where a hand-edited config cannot widen it. This is the
authoritative check the push path consults before any push — a crafted
config that names ``branch: "origin/main"`` is refused here.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Branches that a push must NEVER target. These are shared / release /
# integration branches. Matched after stripping a leading ``origin/`` (or
# other known remote/ref prefix) and lower-cased. Keep this list TIGHT and
# conservative — when unsure whether a branch is shared, it is treated as
# protected.
PROTECTED_BRANCH_NAMES = frozenset(
    {
        "mainline",  # a common primary-branch name in some orgs
        "main",
        "master",  # wokeignore:rule=master  # legacy primary: a protected name
        "trunk",
        "develop",
        "development",
        "prod",
        "production",
        "release",
        "stable",
    }
)

# Release/integration line prefixes — ``release/*``, ``releases/*``, ``hotfix/*``, etc.
# These are conventionally protected integration lines, never a personal feature branch.
PROTECTED_BRANCH_PREFIXES = (
    "release/",
    "releases/",
    "hotfix/",
    "prod/",
    "production/",
    "mainline/",
)


# The remote prefixes we strip to recover the bare branch name. config-sync
# only ever uses ``origin``; ``upstream`` is included so a non-origin remote
# can't smuggle a protected name past the check (``upstream/mainline``). We
# do NOT strip arbitrary first segments — that would mangle a real branch
# path like ``release/2026.1`` -> ``2026.1`` and defeat the release-line
# prefix denylist.
_REMOTE_PREFIXES = ("origin/", "upstream/")

#: Fully-qualified ref prefixes. ``refs/heads/main`` names the same branch as
#: ``main``, so a denylist that only knows the short form is trivially
#: bypassed by spelling it out.
_REF_PREFIXES = ("refs/heads/", "refs/remotes/")


def normalize_branch(branch: str | None) -> str:
    """Strip a leading well-known remote prefix (``origin/`` / ``upstream/``)
    and ref prefix (``refs/heads/`` / ``refs/remotes/``), plus whitespace.

    Strip ONLY a known remote/ref prefix — never an arbitrary first segment
    — so a real branch path like ``release/2026.1`` keeps its protected
    ``release/`` prefix (stripping it would let it slip past
    :func:`is_protected_branch`).
    """
    b = (branch or "").strip()
    # Strip known ref/remote prefixes REPEATEDLY until stable. A single
    # ordered pass is not enough: ``refs/heads/main`` and
    # ``origin/refs/heads/main`` are the same ref as ``main`` and git accepts
    # all three, but each needs a different strip order.
    #
    # Bounded loop, not ``while True``: a crafted value like
    # ``origin/origin/origin/...`` must not spin. Six passes is far more
    # nesting than any real ref.
    for _ in range(6):
        low = b.lower()
        for pfx in _REF_PREFIXES + _REMOTE_PREFIXES:
            if low.startswith(pfx):
                b = b[len(pfx) :]
                break
        else:
            break
    return b


def is_protected_branch(branch: str | None) -> bool:
    """True iff ``branch`` is a protected/shared branch that a push must refuse.

    Conservative: an empty/blank branch is treated as protected (refuse
    rather than push to an ambiguous target). Matching is case-insensitive on
    the ``origin/``-stripped name, against the exact denylist and the
    release-line prefixes.
    """
    name = normalize_branch(branch).lower()
    if not name:
        return True  # ambiguous/empty target -> refuse (fail closed)
    if name in PROTECTED_BRANCH_NAMES:
        return True
    return any(name.startswith(p) for p in PROTECTED_BRANCH_PREFIXES)


def authorize_direct_push(*, branch: str | None) -> tuple[bool, str]:
    """The single authorization decision for a config-sync push.

    Returns ``(allowed, reason)``. A push is authorized ONLY when the target
    is a real, non-protected feature branch. ``reason`` is a
    human-readable refusal cause when not allowed (surfaced to the UI/log),
    or a confirmation string when allowed — never silent.

    This is the ONE place that says "yes" to a push; the push path must call
    it and honor the result. It does not itself push (no side effects) — it
    is a pure policy decision so it is trivially testable and auditable.
    """
    name = normalize_branch(branch)
    if not name:
        return (
            False,
            "no branch configured — refusing to push to an empty/ambiguous target",
        )
    if is_protected_branch(branch):
        return False, (
            f"branch {name!r} is protected/shared — push is refused "
            "(the protected-branch denylist is non-overridable)"
        )
    return True, f"push authorized for non-protected branch {name!r}"


#: Refusal codes from :func:`scan_content_for_secrets`. A caller renders its own
#: message from these, so no string built inside the scanner is ever logged --
#: which is what makes "the log line carries no scanned content" true by
#: construction rather than by review.
SCAN_OK = "ok"
SCAN_HIT = "hit"
SCAN_NO_SCANNER = "no_scanner"

#: Human-readable text per refusal code, for a caller that needs to log or record one.
#: A pure literal table: no scanned text, no exception message, no finding can enter it.
SCAN_REASON_TEXT = {
    SCAN_OK: "",
    SCAN_HIT: "content scan found credential/exfiltration finding(s)",
    SCAN_NO_SCANNER: "credential scanners unavailable",
}


def describe_scan(code: str) -> str:
    """The log-safe message for a scan refusal code. Unknown code -> a fixed literal."""
    return SCAN_REASON_TEXT.get(code, "content scan refused the push")


def scan_content_for_secrets(text: str) -> tuple[bool, str]:
    """Return ``(clean, note)`` for content that is about to leave the host.

    Every push path in config-sync funnels through here. One implementation,
    because a credential scan that only guards *some* of the exits is not a
    gate — and the exit that was missed is the one that publishes.

    DETECT, never rewrite. Redacting a code/config diff would corrupt the
    very content it is meant to protect, so a hit refuses the push and
    leaves the change for a human. That is why this returns a verdict rather
    than cleaned text.

    FAIL-CLOSED: if the scanners cannot be imported, the content is treated
    as unsafe. An unscannable push is indistinguishable from an unscanned
    one.

    The note is built from LITERALS and an integer COUNT only — never from
    the scanned text, the scanner's own findings, or an exception's message.
    Two reasons, and the second is why the count is formatted separately
    below:

    1. A credential-scanner warning can quote the text it matched, so
       interpolating a finding would write the secret into the very log
       this exists to keep it out of.
    2. Callers LOG this note. A static-analysis clear-text-logging query
       follows dataflow, and any path from ``text`` to the returned string
       makes a log call look like it publishes a secret. Keeping the note
       demonstrably independent of ``text`` is what makes the property
       checkable by a machine instead of arguable in a comment.
    """
    try:
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls
    except Exception:  # noqa: BLE001 - no scanner, no push
        # The exception message is deliberately dropped rather than
        # interpolated: an ImportError text can carry a filesystem path, and
        # this string gets logged.
        logger.warning(
            "credential scanners unavailable — refusing the push", exc_info=True
        )
        return False, SCAN_NO_SCANNER
    if not (text or "").strip():
        return True, SCAN_OK
    try:
        _, cred_hits = redact_credentials(text)
        _, exfil_hits = redact_exfiltration_urls(text)
    except Exception:  # noqa: BLE001 - unrunnable scanner, no push
        logger.warning("credential scanners raised — refusing the push", exc_info=True)
        return False, SCAN_NO_SCANNER
    # Only the COUNT crosses out of this function. The findings themselves
    # are discarded here, in the one place that has them, so no caller can
    # log them by accident.
    total = int(
        sum(1 for warning in cred_hits if _is_blocking_credential_warning(warning))
        + len(list(exfil_hits))
    )
    del cred_hits, exfil_hits
    if total:
        # Only a CODE and a COUNT leave this function — never a message and
        # never the matched text.
        logger.warning(
            "content scan found %d credential/exfiltration finding(s)", total
        )
        return False, f"{SCAN_HIT}: {total} finding(s)"
    return True, SCAN_OK


#: The host's ``?token=``/``&token=`` URL-parameter pass (``redaction.py``
#: pass 4) reports this warning literal with the VALUE's length. The host
#: documents that pass as output redaction only — "the blocking surface is
#: unchanged" — and accepts that it matches documentation placeholders
#: (``?token=…``, ``?token=$TOKEN``), which KiroCrew's own shipped skill
#: docs contain and this app tracks but can never rewrite.
_TOKEN_PARAM_WARNING_RE = re.compile(r"Redacted token parameter value \((\d+) chars\)")

#: A ``?token=`` VALUE shorter than this is treated as a placeholder, not a
#: bearer. Same floor as the host's own credential pre-filter
#: (``_PREFILTER_MIN_LEN``): no real token the host issues is this short.
_TOKEN_PARAM_MIN_BEARER_LEN = 16


def _is_blocking_credential_warning(warning: str) -> bool:
    """Whether one ``redact_credentials`` warning counts as a push finding.

    Every warning blocks EXCEPT a pass-4 token-parameter hit whose value is
    too short to be a real bearer. The decision reads only the warning
    literal (a fixed template plus an integer), never the scanned text, so
    the note this module returns stays independent of the content. An
    unrecognised warning — a renamed or newly added host pass — blocks, so
    this carve-out can only ever narrow in the fail-closed direction.
    """
    match = _TOKEN_PARAM_WARNING_RE.fullmatch(str(warning))
    if match is None:
        return True
    return int(match.group(1)) >= _TOKEN_PARAM_MIN_BEARER_LEN
