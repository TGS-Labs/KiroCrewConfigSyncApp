"""Tests for backend/buildo_pr.py.

Covers tasks.md 3.4: "`backend/buildo_pr.py` builds the `create_pull_request`
payload for `TGS-Labs/Kiro-Config-Bundles` (head branch, base `main`, redacted
title and body) with a test that fails if `merge_method` is ever present in
the payload", tracing to requirements.md:

- 2.3: "WHEN the hash differs THEN the job SHALL ... open a pull request."
- 2.6: "WHEN the push completes THEN the recorded last-pushed hash SHALL be
  updated only after the push and PR creation both succeed" — this module
  builds the payload PR creation is confirmed against, so its shape is what
  that confirmation depends on.
- 2.7: "WHEN a push fails for any reason THEN the failure SHALL be recorded
  with its cause and surfaced in the app's UI."
- 3.8: "No log line, commit message, PR title, PR body, or UI field produced
  by this app SHALL contain an unredacted credential; commit messages SHALL
  pass through the ported credential-redaction path."

Design context (design.md, "Push credential and PR-creation route —
RESOLVED"): this app's own MCP/agent context is read-only for GitHub and has
no `gh` CLI, and Buildo is the sanctioned PR tool for TGS-Labs repos. This
module does NOT call any MCP tool itself — it only BUILDS the
`create_pull_request` payload dict that `skills/complete-pr-handoff/SKILL.md`
instructs a KiroCrew agent context to pass to Buildo MCP. Per the org's
disallow-squash rule (learned correction: never pass `merge_method` unless
the target repo is verified to allow it — TGS-Labs repos do not), this
payload must OMIT `merge_method` entirely so the tool default (`merge`)
applies; the omission is the whole point of the acceptance criterion, so the
test asserts the KEY IS ABSENT, not merely `None`/falsy, since a caller that
reintroduces `"merge_method": None` would defeat the guarantee just as badly
as `"merge_method": "squash"` (some HTTP/JSON-RPC layers serialize a `None`
value key as an explicit `null` rather than dropping it, which is exactly the
byte on the wire this criterion exists to prevent).

Import target `backend.buildo_pr` does not exist yet — every test below is
expected to fail with an ImportError / ModuleNotFoundError until
software-engineer writes it. That is the correct TDD red-phase starting
state, not a test defect.
"""

from __future__ import annotations

from typing import Any, Callable

import pytest

#: The exact target repo tasks.md 3.4 names. Asserted literally rather than
#: via a substring/contains check, since a wrong owner or a sibling repo
#: name would otherwise silently pass a substring-style assertion.
_TARGET_REPO = "TGS-Labs/Kiro-Config-Bundles"
_TARGET_BASE = "main"

#: A credential shape long enough to trip the real
#: `sk-ant-[A-Za-z0-9_-]{16,}` pattern in `kiro_crew.security.redaction` (see
#: tests/safety/test_redact_msg.py, which this fixture matches so both test
#: files exercise the same real scanner behaviour rather than each rolling
#: its own too-short placeholder that would stay green even if redaction
#: were entirely broken).
_CREDENTIAL = "sk-ant-VERY-DISTINCTIVE-SECRET-VALUE-99887766"


def _import_build_payload() -> Callable[..., dict[str, Any]]:
    """Import helper so every test raises the same clear failure while the
    module doesn't exist yet, and so a future rename only needs one edit."""
    from backend.buildo_pr import build_pull_request_payload

    return build_pull_request_payload


class TestPayloadTargetsCorrectRepoAndBase:
    """Requirement 2.3: the PR SHALL be opened against the named bundle repo
    on branch `main`, from the pushed head branch."""

    def test_payload_targets_kiro_config_bundles_repo(self) -> None:
        build_payload = _import_build_payload()
        payload = build_payload(
            head_branch="config-sync/instance-42-abc123",
            title="chore: sync config-sync/instance-42",
            body="Automated config sync.",
        )
        assert payload["repo"] == _TARGET_REPO

    def test_payload_base_is_main(self) -> None:
        build_payload = _import_build_payload()
        payload = build_payload(
            head_branch="config-sync/instance-42-abc123",
            title="chore: sync config-sync/instance-42",
            body="Automated config sync.",
        )
        assert payload["base"] == _TARGET_BASE

    def test_payload_head_is_the_pushed_branch_verbatim(self) -> None:
        build_payload = _import_build_payload()
        head_branch = "config-sync/instance-99-deadbeef"
        payload = build_payload(
            head_branch=head_branch,
            title="chore: sync config-sync/instance-99",
            body="Automated config sync.",
        )
        assert payload["head"] == head_branch

    def test_different_head_branches_produce_different_payload_heads(
        self,
    ) -> None:
        """A behavioural (non-snapshot) check that `head` is actually wired
        to the caller's branch rather than a hardcoded literal that happens
        to match one example call."""
        build_payload = _import_build_payload()
        payload_a = build_payload(
            head_branch="config-sync/instance-1-aaa",
            title="t",
            body="b",
        )
        payload_b = build_payload(
            head_branch="config-sync/instance-2-bbb",
            title="t",
            body="b",
        )
        assert payload_a["head"] != payload_b["head"]
        assert payload_a["repo"] == payload_b["repo"] == _TARGET_REPO
        assert payload_a["base"] == payload_b["base"] == _TARGET_BASE


class TestMergeMethodNeverPresent:
    """tasks.md 3.4's named test: fails if `merge_method` is EVER present in
    the built payload — key absence, not falsy/None, per the learned
    correction that a reintroduced `"merge_method": None` key is the same
    defect class as `"merge_method": "squash"` for callers that serialize
    None as an explicit null on the wire."""

    def test_merge_method_key_is_absent_on_ordinary_call(self) -> None:
        build_payload = _import_build_payload()
        payload = build_payload(
            head_branch="config-sync/instance-1-aaa",
            title="chore: sync",
            body="Automated config sync.",
        )
        assert "merge_method" not in payload

    def test_merge_method_key_is_absent_with_empty_title_and_body(
        self,
    ) -> None:
        """Boundary/edge-case input should not change the invariant."""
        build_payload = _import_build_payload()
        payload = build_payload(head_branch="config-sync/x-y", title="", body="")
        assert "merge_method" not in payload

    def test_merge_method_key_is_absent_even_if_caller_passes_one(
        self,
    ) -> None:
        """If `build_pull_request_payload` accepts (and ignores/rejects) an
        incoming `merge_method`-shaped kwarg, the built payload must still
        never carry the key — the guarantee is about the OUTPUT shape, not
        about trusting every caller to never try. A `TypeError` on an
        unexpected keyword is an equally acceptable way to uphold the
        guarantee (it never reaches the payload), so either outcome passes
        this test; only a payload that ends up carrying the key fails it."""
        build_payload = _import_build_payload()
        try:
            payload = build_payload(
                head_branch="config-sync/x-y",
                title="t",
                body="b",
                merge_method="squash",  # type: ignore[call-arg]
            )
        except TypeError:
            return
        assert "merge_method" not in payload

    def test_payload_keys_do_not_include_merge_method_via_full_key_set(
        self,
    ) -> None:
        """Belt-and-suspenders on the exact assertion tasks.md 3.4 calls
        out: inspect the full key set rather than only probing one key, so a
        renamed-but-still-present variant (e.g. `mergeMethod`) is not missed
        by this second, independent check."""
        build_payload = _import_build_payload()
        payload = build_payload(head_branch="config-sync/x-y", title="t", body="b")
        merge_method_like_keys = {key for key in payload if "merge" in key.lower()}
        assert merge_method_like_keys == set()


class TestTitleAndBodyPassThroughRedaction:
    """Requirement 3.8: title/body pass through the ported credential-
    redaction path (`backend/safety/redact_msg.py`) before reaching the
    payload — verified with a real credential-shaped seed value, not a
    mocked-out redactor, so the test fails if the wiring to the real
    scanner is ever removed."""

    def test_credential_in_title_never_appears_verbatim_in_payload(
        self,
    ) -> None:
        build_payload = _import_build_payload()
        payload = build_payload(
            head_branch="config-sync/x-y",
            title=f"chore: rotate {_CREDENTIAL}",
            body="Automated config sync.",
        )
        assert _CREDENTIAL not in payload["title"]

    def test_credential_in_body_never_appears_verbatim_in_payload(
        self,
    ) -> None:
        build_payload = _import_build_payload()
        payload = build_payload(
            head_branch="config-sync/x-y",
            title="chore: sync",
            body=f"Diff includes a leaked key: {_CREDENTIAL}",
        )
        assert _CREDENTIAL not in payload["body"]

    def test_credential_never_appears_verbatim_anywhere_in_payload(
        self,
    ) -> None:
        """Defends against the credential leaking into a field other than
        the one it was seeded into (e.g. a caller that mirrors the raw
        title into a different key)."""
        build_payload = _import_build_payload()
        payload = build_payload(
            head_branch="config-sync/x-y",
            title=f"chore: rotate {_CREDENTIAL}",
            body=f"Also here: {_CREDENTIAL}",
        )
        serialized_values = " ".join(str(value) for value in payload.values())
        assert _CREDENTIAL not in serialized_values

    def test_title_without_a_credential_is_not_corrupted_by_redaction(
        self,
    ) -> None:
        """An ordinary title with no credential-shaped content should
        survive the redaction pass unchanged, so redaction is not merely
        replacing every title/body wholesale with a placeholder."""
        build_payload = _import_build_payload()
        ordinary_title = "chore: sync config-sync/instance-7"
        payload = build_payload(
            head_branch="config-sync/x-y",
            title=ordinary_title,
            body="No secrets here.",
        )
        assert payload["title"] == ordinary_title

    def test_body_without_a_credential_is_not_corrupted_by_redaction(
        self,
    ) -> None:
        build_payload = _import_build_payload()
        ordinary_body = "Automated config sync from instance-7."
        payload = build_payload(
            head_branch="config-sync/x-y",
            title="chore: sync",
            body=ordinary_body,
        )
        assert payload["body"] == ordinary_body


class TestPayloadShapeIsAPlainSerializableDict:
    """The payload is handed to an agent context to pass to Buildo MCP's
    `create_pull_request` — it must be a plain dict of JSON-serializable
    values, not a dataclass/object a caller would need to know how to
    unpack."""

    def test_build_payload_returns_a_dict(self) -> None:
        build_payload = _import_build_payload()
        payload = build_payload(head_branch="config-sync/x-y", title="t", body="b")
        assert isinstance(payload, dict)

    def test_all_payload_values_are_strings(self) -> None:
        build_payload = _import_build_payload()
        payload = build_payload(head_branch="config-sync/x-y", title="t", body="b")
        assert all(isinstance(value, str) for value in payload.values())

    @pytest.mark.parametrize("required_key", ["repo", "base", "head", "title", "body"])
    def test_required_keys_are_present(self, required_key: str) -> None:
        build_payload = _import_build_payload()
        payload = build_payload(head_branch="config-sync/x-y", title="t", body="b")
        assert required_key in payload
