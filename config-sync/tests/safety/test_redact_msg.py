"""Tests for backend/safety/redact_msg.py.

Covers design.md's `backend/safety/` component (the commit-message redaction
row — ported from `backend/commit.py`'s credential-redaction path over
`kiro_crew.security.redact`) and tasks.md 2.3, tracing to requirements.md
3.8, 7.5, 8.4:

- tasks.md 2.3: "`backend/safety/redact_msg.py` (ported commit-message
  redaction over `kiro_crew.security.redact`) sanitizes every commit message,
  PR title, PR body, log line and UI/API field the app emits, so no
  unredacted credential can appear in any of them."
- requirements.md 3.8: "No log line, commit message, PR title, PR body, or UI
  field produced by this app SHALL contain an unredacted credential; commit
  messages SHALL pass through the ported credential-redaction path."
- requirements.md 7.5: "No UI field or API response SHALL contain an
  unredacted credential."
- requirements.md 8.4: the app SHALL reuse the three named safety concerns by
  PORTING them (this module is one of the three), and SHALL NOT port or
  depend on the AI-authored-PR pipeline modules (`driver`, `agent_runner`,
  `ledger`, `proposer`, `bug_gate`).

The module under test wraps `kiro_crew.security.redact_credentials` and
`kiro_crew.security.redact_exfiltration_urls` — the exact same two functions
`backend/safety/push_policy.py`'s `scan_content_for_secrets` already calls
(read that file first for the import pattern: a bare `try/except Exception`
around the import, since the scanners are an OPTIONAL system dependency and
an unimportable scanner must fail closed rather than crash). Here the
contract is different from push_policy's: push_policy DETECTS-and-REFUSES
(never rewrites, because rewriting a diff would corrupt it), while this
module REWRITES-and-RETURNS a sanitized string (a commit message / PR title
/ PR body / log line / UI field is prose, not a diff to preserve byte-for-
byte — sanitizing it in place is exactly the point).

Public surface under test: a single function, `redact_message(text: str) ->
str`, that runs both `redact_credentials` and `redact_exfiltration_urls` over
`text` and returns the fully sanitized result. Every test is written to FAIL
if the underlying credential/URL redaction were removed or a caller path
were skipped — not merely to pass on a happy path.

This test file requires `tests/safety/conftest.py`'s sys.path shim (adds the
system site-packages dir carrying the installed, untouched `kiro_crew`
package) to resolve `kiro_crew.security` from this isolated venv. The
conftest is already in place from Wave 0 (`push_policy.py`'s tests rely on
the same shim).

Import target `backend.safety.redact_msg` does not exist yet — every test
below is expected to fail with an ImportError / ModuleNotFoundError until
software-engineer writes it. That is the correct TDD red-phase starting
state, not a test defect.
"""

from __future__ import annotations

import pytest

# A credential shape that is genuinely long enough to satisfy the real
# `sk-ant-[A-Za-z0-9_-]{16,}` / `sk-proj-[A-Za-z0-9_-]{16,}` patterns in
# kiro_crew.security.redaction — anything shorter than 16 trailing chars
# would not actually match, and a test built on a too-short fixture would
# stay green even if the module were entirely broken.
_SK_ANT_CREDENTIAL = "sk-ant-VERY-DISTINCTIVE-SECRET-VALUE-99887766"
_SK_PROJ_CREDENTIAL = "sk-proj-ANOTHER-DISTINCTIVE-SECRET-VALUE-11223344"

# A URL carrying an embedded credential-like query value, long enough to trip
# `redact_exfiltration_urls`' long-query-param heuristic
# (`_EXFIL_QUERY_MIN_LEN`) rather than relying on a borderline-length string.
_EXFIL_URL = (
    "https://evil.example.com/collect"
    "?token=AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHHIIIIJJJJKKKKLLLLMMMMNNNNOOOO"
)


def _import_redact_message():
    """Import helper so every test raises the same clear failure while the
    module doesn't exist yet, and so a future rename only needs one edit."""
    from backend.safety.redact_msg import redact_message

    return redact_message


# ---------------------------------------------------------------------------
# Credential redaction — sk-ant- / sk-proj- prefixed secrets
# ---------------------------------------------------------------------------


class TestRedactMessageRemovesCredentials:
    """Requirements 3.8 / 8.4: no emitted string may contain a credential."""

    @pytest.mark.parametrize(
        "field_name,template",
        [
            ("commit_message", "fix: rotate key\n\napi_key = {cred}\n"),
            ("pr_title", "Rotate leaked key {cred}"),
            ("pr_body", "This PR rotates a leaked credential: {cred}\n"),
            ("log_line", "2026-09-20 08:00:00 WARNING found secret {cred}"),
            ("ui_field", "Last error: could not auth with {cred}"),
            ("api_field", '{{"detail": "auth failed for {cred}"}}'),
        ],
    )
    def test_sk_ant_credential_is_removed_from_every_field_kind(
        self, field_name: str, template: str
    ) -> None:
        """An sk-ant- prefixed credential must not survive in ANY of the six
        field kinds requirement 3.8 names by name (commit message, PR title,
        PR body, log line, UI field, API field) — parametrized so a fix that
        only covers commit messages cannot pass."""
        redact_message = _import_redact_message()
        text = template.format(cred=_SK_ANT_CREDENTIAL)

        result = redact_message(text)

        assert _SK_ANT_CREDENTIAL not in result, (
            f"{field_name} still contains the raw sk-ant- credential after "
            "redact_message"
        )

    @pytest.mark.parametrize(
        "field_name,template",
        [
            ("commit_message", "fix: rotate key\n\napi_key = {cred}\n"),
            ("pr_title", "Rotate leaked key {cred}"),
            ("pr_body", "This PR rotates a leaked credential: {cred}\n"),
            ("log_line", "2026-09-20 08:00:00 WARNING found secret {cred}"),
            ("ui_field", "Last error: could not auth with {cred}"),
            ("api_field", '{{"detail": "auth failed for {cred}"}}'),
        ],
    )
    def test_sk_proj_credential_is_removed_from_every_field_kind(
        self, field_name: str, template: str
    ) -> None:
        """Same property as the sk-ant- test, for the sk-proj- (OpenAI
        project key) prefix — a distinct regex branch in
        kiro_crew.security.redaction, so it must be exercised separately
        rather than assumed to be covered by the sk-ant- case."""
        redact_message = _import_redact_message()
        text = template.format(cred=_SK_PROJ_CREDENTIAL)

        result = redact_message(text)

        assert _SK_PROJ_CREDENTIAL not in result, (
            f"{field_name} still contains the raw sk-proj- credential after "
            "redact_message"
        )

    def test_partial_echo_of_the_credential_is_also_absent(self) -> None:
        """Guard against a partial-redaction evasion: a fixed-length prefix
        slice of the secret leaking out is as much a violation as the whole
        value leaking (same discipline as push_policy's refusal-note test)."""
        redact_message = _import_redact_message()
        text = f"api_key = {_SK_ANT_CREDENTIAL}\n"

        result = redact_message(text)

        assert _SK_ANT_CREDENTIAL[:20] not in result

    def test_non_credential_prose_around_the_secret_is_preserved(self) -> None:
        """redact_message must SANITIZE, not obliterate — the surrounding
        commit-message prose (a diff's worth of context) must survive so the
        message stays informative once the secret is masked out."""
        redact_message = _import_redact_message()
        text = (
            f"fix: rotate the leaked Anthropic key\n\napi_key = {_SK_ANT_CREDENTIAL}\n"
        )

        result = redact_message(text)

        assert "fix: rotate the leaked Anthropic key" in result
        assert _SK_ANT_CREDENTIAL not in result

    def test_credential_free_text_is_returned_unchanged(self) -> None:
        """A message with no credential must pass through unmodified — a
        redactor that mangles clean prose would make every commit message
        illegible even when nothing needed redacting."""
        redact_message = _import_redact_message()
        text = "fix: correct the off-by-one error in the poll cron scheduler"

        result = redact_message(text)

        assert result == text


# ---------------------------------------------------------------------------
# Exfiltration URL redaction
# ---------------------------------------------------------------------------


class TestRedactMessageRemovesExfiltrationUrls:
    """Requirement 3.8 covers "a credential" broadly; a URL carrying an
    embedded credential in its query string is exactly the shape
    `redact_exfiltration_urls` exists to catch, and push_policy.py's
    `scan_content_for_secrets` already treats it as a first-class finding
    alongside plaintext credentials — this module must too."""

    @pytest.mark.parametrize(
        "field_name,template",
        [
            ("commit_message", "fix: remove debug beacon\n\nsee {url}\n"),
            ("pr_title", "Remove call to {url}"),
            ("pr_body", "This PR removes the beacon at {url}\n"),
            ("log_line", "2026-09-20 08:00:00 WARNING outbound call to {url}"),
            ("ui_field", "Blocked request: {url}"),
        ],
    )
    def test_exfiltration_url_is_removed_from_every_field_kind(
        self, field_name: str, template: str
    ) -> None:
        redact_message = _import_redact_message()
        text = template.format(url=_EXFIL_URL)

        result = redact_message(text)

        assert _EXFIL_URL not in result, (
            f"{field_name} still contains the raw exfiltration URL after "
            "redact_message"
        )

    def test_exfiltration_url_query_value_does_not_survive_either(self) -> None:
        """Even if the exact URL string were somehow reconstructed, the
        credential-shaped query VALUE itself must not appear in the output —
        this is the property that actually matters (the URL is a vehicle for
        the leaked value, not the secret itself)."""
        redact_message = _import_redact_message()
        query_value = _EXFIL_URL.split("token=", 1)[1]
        text = f"see {_EXFIL_URL}\n"

        result = redact_message(text)

        assert query_value not in result

    def test_url_free_text_is_returned_unchanged(self) -> None:
        redact_message = _import_redact_message()
        text = "fix: update the README to mention the poll interval"

        result = redact_message(text)

        assert result == text


# ---------------------------------------------------------------------------
# Combined / edge cases
# ---------------------------------------------------------------------------


class TestRedactMessageCombinedAndEdgeCases:
    def test_a_credential_and_an_exfiltration_url_in_one_message_are_both_removed(
        self,
    ) -> None:
        """A single PR body containing BOTH hazard shapes must have neither
        survive — a degenerate implementation that only wires one of the two
        underlying scanners must fail this test."""
        redact_message = _import_redact_message()
        text = f"api_key = {_SK_ANT_CREDENTIAL}\n" f"beacon: {_EXFIL_URL}\n"

        result = redact_message(text)

        assert _SK_ANT_CREDENTIAL not in result
        assert _EXFIL_URL not in result

    def test_empty_string_is_returned_unchanged(self) -> None:
        redact_message = _import_redact_message()

        assert redact_message("") == ""

    def test_return_type_is_str(self) -> None:
        """The function contracts a str->str transform (a commit message,
        title, body, log line, or field value is always a str by the time it
        reaches this module) — a bytes or tuple leak here would break every
        caller that immediately writes the result to a message/log/response."""
        redact_message = _import_redact_message()

        result = redact_message("plain text, nothing to redact")

        assert isinstance(result, str)

    def test_multiple_occurrences_of_the_same_credential_are_all_removed(self) -> None:
        """A credential repeated (e.g. quoted once in a log line and again in
        a follow-up sentence) must not survive in either occurrence — a
        redactor that only replaces the first match would leave the second
        instance live."""
        redact_message = _import_redact_message()
        text = (
            f"first mention: {_SK_ANT_CREDENTIAL}\n"
            f"repeated again: {_SK_ANT_CREDENTIAL}\n"
        )

        result = redact_message(text)

        assert _SK_ANT_CREDENTIAL not in result

    def test_does_not_depend_on_the_ai_authored_pr_pipeline_modules(self) -> None:
        """Requirement 8.4 / design.md: this app explicitly does NOT port or
        depend on driver/agent_runner/ledger/proposer/bug_gate. A static
        check that the module's own source text names none of them keeps
        this property enforced at the file level, not just by omission."""
        import inspect

        import backend.safety.redact_msg as module

        source = inspect.getsource(module)
        for forbidden in ("driver", "agent_runner", "ledger", "proposer", "bug_gate"):
            assert forbidden not in source, (
                f"backend/safety/redact_msg.py must not reference {forbidden!r} "
                "(requirements.md 8.4 forbids porting the AI-authored-PR "
                "pipeline modules)"
            )
