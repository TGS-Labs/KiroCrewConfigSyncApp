"""Tests for backend/safety/push_policy.py (config-sync).

This module is PORTED from
``kiro_crew/apps/builtins/auto_improvement/spine/push_policy.py`` (read-only
reference — never modified). config-sync's port drops the F10
"direct-commit opt-in" framing: per tasks.md 2.1 and requirements 2.4, 3.6,
3.7, 8.4, 8.5, the config-sync push path *always* intends to push (there is
no operator direct-commit checkbox), so ``authorize_direct_push`` here is
exercised on branch safety alone. Tests target the behavioural contract in
tasks.md task 2.1:

    "refuses a push to main, to any protected branch, and to an empty or
    ambiguous target with the policy's reason string; scan_content_for_secrets
    refuses -- never rewrites -- content with a finding, reporting refusal
    code and count only; an unimportable or unrunnable scanner fails closed."

Every test in this file is written to FAIL if the corresponding safety
property were removed from the implementation -- not merely to pass on a
happy path. Where useful, a test asserts on the delta between two calls
(e.g. "protected" and "empty" must produce DIFFERENT reason strings) so a
degenerate implementation that returns one constant refusal string for
every case cannot pass.
"""

from __future__ import annotations

import importlib
import sys
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# authorize_direct_push
# ---------------------------------------------------------------------------


class TestAuthorizeDirectPushProtectedBranches:
    """Requirement 2.4 / 3.6: refuse main, protected, empty, ambiguous targets."""

    @pytest.mark.parametrize(
        "branch",
        [
            "main",
            "origin/main",
            "refs/heads/main",
            "master",  # wokeignore:rule=master
            "origin/master",  # wokeignore:rule=master
        ],
    )
    def test_refuses_push_to_main_or_master(self, branch: str) -> None:
        from backend.safety.push_policy import authorize_direct_push

        allowed, reason = authorize_direct_push(branch=branch)

        assert allowed is False, f"push to {branch!r} must be refused"
        assert (
            isinstance(reason, str) and reason.strip()
        ), "a refusal must carry a non-empty human-readable reason string"

    @pytest.mark.parametrize(
        "branch",
        [
            "develop",
            "trunk",
            "production",
            "prod",
            "release/2026.1",
            "hotfix/urgent-fix",
        ],
    )
    def test_refuses_push_to_other_protected_branches(self, branch: str) -> None:
        """Protection is not limited to main/master -- shared/release lines too."""
        from backend.safety.push_policy import authorize_direct_push

        allowed, reason = authorize_direct_push(branch=branch)

        assert allowed is False, f"push to {branch!r} must be refused"
        assert reason.strip()

    @pytest.mark.parametrize("branch", ["", "   ", None])
    def test_refuses_push_to_empty_or_ambiguous_target(
        self, branch: str | None
    ) -> None:
        """An empty/blank/absent branch must fail closed (refused), not pass."""
        from backend.safety.push_policy import authorize_direct_push

        allowed, reason = authorize_direct_push(branch=branch)

        assert allowed is False, "an empty/ambiguous target must be refused"
        assert reason.strip()

    def test_allows_push_to_a_real_feature_branch(self) -> None:
        """The policy must not be a blanket refusal -- a normal feature branch
        (config-sync's own naming convention) must be authorized."""
        from backend.safety.push_policy import authorize_direct_push

        allowed, reason = authorize_direct_push(
            branch="config-sync/instance-abc123-deadbeef"
        )

        assert allowed is True, "a non-protected feature branch must be authorized"
        assert isinstance(reason, str)

    def test_distinct_reason_strings_per_refusal_case(self) -> None:
        """A degenerate implementation could return True/False correctly while
        collapsing every refusal into one constant string. Require the reason
        for a PROTECTED branch to differ from the reason for an EMPTY branch,
        since tasks.md 2.1 explicitly asks for 'a distinct reason string per
        case'."""
        from backend.safety.push_policy import authorize_direct_push

        _, protected_reason = authorize_direct_push(branch="main")
        _, empty_reason = authorize_direct_push(branch="")
        _, ambiguous_reason = authorize_direct_push(branch="   ")

        assert protected_reason != empty_reason, (
            "refusing 'main' and refusing an empty branch must not share the "
            "exact same reason string"
        )
        # An ambiguous (whitespace-only) target and a genuinely empty target
        # are allowed to share wording (both collapse to "no branch"), but at
        # least one of the three pairs above must differ from 'main's reason
        # -- already asserted. This second check guards the other direction:
        # ambiguous/empty must not accidentally be classified as the SAME
        # kind of refusal as a real protected-branch name collision.
        assert "main" not in empty_reason.lower()
        assert "main" not in ambiguous_reason.lower()

    def test_case_insensitive_and_remote_prefix_stripped(self) -> None:
        """MAIN / Main / origin/MAIN must all be treated as the protected
        branch 'main', not bypassed via casing or an unstripped remote
        prefix."""
        from backend.safety.push_policy import authorize_direct_push

        for branch in ("MAIN", "Main", "origin/MAIN", "ORIGIN/MAIN"):
            allowed, _ = authorize_direct_push(branch=branch)
            assert allowed is False, f"{branch!r} must resolve to protected main"

    def test_refuses_nested_remote_and_ref_prefix_evasion(self) -> None:
        """A crafted branch string that nests remote/ref prefixes
        (e.g. 'origin/refs/heads/main') must still resolve to the protected
        'main' branch, not slip past a single-pass prefix strip."""
        from backend.safety.push_policy import authorize_direct_push

        allowed, _ = authorize_direct_push(branch="origin/refs/heads/main")
        assert (
            allowed is False
        ), "nested remote/ref prefixes must not bypass the denylist"

    def test_release_prefix_is_not_stripped_as_a_remote(self) -> None:
        """'release/2026.1' must remain protected -- normalize_branch must
        strip ONLY known remote/ref prefixes, never an arbitrary first path
        segment (stripping 'release/' itself would defeat the release-line
        prefix denylist)."""
        from backend.safety.push_policy import authorize_direct_push

        allowed, _ = authorize_direct_push(branch="release/2026.1")
        assert allowed is False


# ---------------------------------------------------------------------------
# scan_content_for_secrets
# ---------------------------------------------------------------------------


class TestScanContentForSecretsRefusesRatherThanRewrites:
    """Requirement 3.6 / 3.7: a finding refuses the push; it is never
    silently rewritten, and the refusal carries a code and a count only."""

    def test_clean_content_is_not_refused(self) -> None:
        from backend.safety.push_policy import scan_content_for_secrets

        clean, note = scan_content_for_secrets("just some ordinary config text")

        assert clean is True
        assert note is not None

    def test_empty_content_is_not_refused(self) -> None:
        from backend.safety.push_policy import scan_content_for_secrets

        clean, _note = scan_content_for_secrets("")

        assert clean is True

    def test_content_with_a_credential_is_refused(self) -> None:
        """A recognizable credential-shaped secret must refuse the push --
        this is the core safety property. If the scanner is a no-op, or
        someone deletes the credential check, this test must go red."""
        from backend.safety.push_policy import scan_content_for_secrets

        secret_bearing_text = (
            "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY\n"
            "github_token = ghp_1234567890abcdefghijklmnopqrstuvwxyz12\n"
        )

        clean, note = scan_content_for_secrets(secret_bearing_text)

        assert clean is False, "content containing credentials must be refused"
        assert note is not None

    def test_refusal_never_echoes_the_matched_secret_value(self) -> None:
        """The refusal note must report a code and a count only -- never the
        secret text itself. This is the property a naive 'helpful' error
        message (f"found secret: {match}") would violate."""
        from backend.safety.push_policy import scan_content_for_secrets

        distinctive_secret = (
            "sk-ant-VERY-DISTINCTIVE-SECRET-VALUE-should-never-leak-99887766"
        )
        secret_bearing_text = f"api_key = {distinctive_secret}\n"

        clean, note = scan_content_for_secrets(secret_bearing_text)

        assert clean is False
        assert (
            distinctive_secret not in note
        ), "the refusal note must never contain the scanned secret value"
        # Guard against a partial-echo evasion too (e.g. logging half the key).
        assert distinctive_secret[:20] not in note

    def test_refusal_note_is_independent_of_input_text_content(self) -> None:
        """Two DIFFERENT secret-bearing inputs that trip the same refusal
        *code* must not produce two different notes that leak per-input
        content -- the note must be constructable from the code and a count
        alone. We assert this by checking the note contains none of either
        input's distinctive substrings, for two inputs engineered to differ
        only in the secret value."""
        from backend.safety.push_policy import scan_content_for_secrets

        text_a = "token = AAAA-FIRST-DISTINCT-VALUE-1111111111111111"
        text_b = "token = BBBB-SECOND-DISTINCT-VALUE-2222222222222222"

        _, note_a = scan_content_for_secrets(text_a)
        _, note_b = scan_content_for_secrets(text_b)

        assert "FIRST-DISTINCT-VALUE" not in note_a
        assert "SECOND-DISTINCT-VALUE" not in note_b

    def test_refusal_reports_a_finding_count(self) -> None:
        """The refusal must report *how many* findings were detected, not
        merely that one existed -- tasks.md 2.1 requires 'refusal code and a
        count only'. We probe this via the public verdict tuple: if the
        implementation exposes the count only through a richer return shape,
        this test documents that scan_content_for_secrets's public contract
        is (clean: bool, note) and the count must be discoverable from note
        or from a sibling accessor -- so we require the note to contain at
        least one digit character when there is a finding, since a
        code-only string with zero count information would fail this."""
        from backend.safety.push_policy import scan_content_for_secrets

        many_secrets_text = "\n".join(
            f"secret_{i} = ghp_abcdefghijklmnopqrstuvwxyz{i:010d}" for i in range(3)
        )

        clean, note = scan_content_for_secrets(many_secrets_text)

        assert clean is False
        assert any(
            ch.isdigit() for ch in note
        ), "a multi-finding refusal must surface a count, not just a bare code"

    def test_content_is_never_mutated_or_rewritten(self) -> None:
        """scan_content_for_secrets must return a VERDICT, never a modified
        copy of the content. Redacting/rewriting a diff would corrupt the
        change it is meant to protect -- the function's return shape must
        not be (rewritten_text,) and the original string object must be
        left untouched (no in-place mutation possible on an immutable str,
        so this also guards against a future signature change to
        mutable bytes/bytearray)."""
        from backend.safety.push_policy import scan_content_for_secrets

        original = "api_key = ghp_thisIsASecretTokenValue1234567890"
        original_copy = str(original)

        result = scan_content_for_secrets(original)

        assert original == original_copy, "input content must not be mutated"
        assert isinstance(result, tuple) and len(result) == 2, (
            "scan_content_for_secrets must return a (clean, note) verdict "
            "tuple, not rewritten content"
        )
        clean, note = result
        assert isinstance(clean, bool)
        # The note must not simply echo the input back as "cleaned" content.
        assert note != original


class TestScanContentForSecretsFailsClosed:
    """Requirement 3.7 / 8.5: an unimportable or unrunnable scanner fails
    CLOSED (refuses), because an unscannable push is indistinguishable from
    an unscanned one."""

    def test_fails_closed_when_scanner_module_is_unimportable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Force the underlying scanner import to raise, and confirm the
        push is refused rather than silently waved through. This must hold
        even though (in this dev environment) the scanner dependency may
        genuinely already be absent -- we force the failure explicitly so
        the test does not depend on the environment's install state."""
        import builtins

        real_import = builtins.__import__

        def _hostile_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "kiro_crew.security" or name.startswith("kiro_crew.security"):
                raise ImportError("simulated: scanner unavailable")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _hostile_import)

        # Force a fresh import of the module under test so any module-level
        # caching of the scanner import can't paper over the forced failure.
        sys.modules.pop("backend.safety.push_policy", None)
        push_policy = importlib.import_module("backend.safety.push_policy")

        clean, note = push_policy.scan_content_for_secrets("harmless looking text")

        assert clean is False, (
            "an unimportable scanner must refuse the push (fail closed), "
            "never treat unscannable content as clean"
        )
        assert note is not None

    def test_fails_closed_when_scanner_raises_at_call_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even if the scanner module imports fine, a scanner function that
        raises when actually invoked must also refuse -- 'unrunnable', not
        just 'unimportable'."""
        sys.modules.pop("backend.safety.push_policy", None)
        push_policy = importlib.import_module("backend.safety.push_policy")

        def _boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("simulated: scanner crashed at call time")

        # Patch whichever underlying scanner call the module makes. We patch
        # broadly at the kiro_crew.security boundary the design.md component
        # table names, so this test fails loudly (ImportError/AttributeError)
        # if the implementation stops calling through that surface, rather
        # than silently passing.
        import kiro_crew.security as security_mod

        monkeypatch.setattr(security_mod, "redact_credentials", _boom)
        monkeypatch.setattr(security_mod, "redact_exfiltration_urls", _boom)

        clean, note = push_policy.scan_content_for_secrets("harmless looking text")

        assert clean is False, (
            "a scanner that raises at call time must refuse the push "
            "(fail closed), not propagate the exception as a false-clean"
        )
        assert note is not None

    def test_does_not_raise_out_to_the_caller_on_scanner_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Failing closed means returning a refusal verdict, not letting the
        exception propagate uncaught -- a caller that doesn't wrap every
        call site in try/except must still get a clean refusal tuple back."""
        import builtins

        real_import = builtins.__import__

        def _hostile_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "kiro_crew.security" or name.startswith("kiro_crew.security"):
                raise ImportError("simulated: scanner unavailable")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _hostile_import)

        sys.modules.pop("backend.safety.push_policy", None)
        push_policy = importlib.import_module("backend.safety.push_policy")

        try:
            result = push_policy.scan_content_for_secrets("some content")
        except Exception as exc:  # noqa: BLE001 - the whole point of this test
            pytest.fail(
                "scan_content_for_secrets must not raise when the scanner "
                f"is unavailable; it must return a fail-closed verdict "
                f"instead, got {exc!r}"
            )

        clean, _note = result
        assert clean is False


# ---------------------------------------------------------------------------
# module-level import hygiene (design.md: no dependency on the AI-authored
# PR pipeline modules that this app deliberately does not port)
# ---------------------------------------------------------------------------


class TestPortedModuleDoesNotImportUnportedPipeline:
    """Requirement 8.4: the app SHALL NOT port or depend on driver,
    agent_runner, ledger, proposer, bug_gate."""

    @pytest.mark.parametrize(
        "forbidden",
        ["driver", "agent_runner", "ledger", "proposer", "bug_gate"],
    )
    def test_module_source_does_not_reference_unported_pipeline(
        self, forbidden: str
    ) -> None:
        import backend.safety.push_policy as push_policy_module
        import inspect

        source = inspect.getsource(push_policy_module)

        assert forbidden not in source, (
            f"push_policy.py must not import or reference {forbidden!r} "
            "(the AI-authored PR pipeline is explicitly not ported)"
        )
