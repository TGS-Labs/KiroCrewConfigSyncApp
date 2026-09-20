"""Tests for the app manifest (`app.json`) and `README.md`.

Covers tasks.md 2.4 and requirements.md 6.2, 6.3, 8.1-8.3:

- `app.json` declares `defaultEnabled: false` (requirements.md 8.2).
- `app.json` declares exactly two crons, one push and one poll, both using
  `command`/`script` (not a bare LLM `message` cron) and both `enabled: false`
  so neither fires before the operator configures the bundle-repo target and
  credentials (requirements.md 8.3, design.md's push/poll decisions).
- The `kirocrew app init` scaffold entries (`agents/sample-agent.json`,
  `skills/sample-skill/`) are removed from the manifest (tasks.md 2.4).
- The placeholder icon is replaced with a real, non-trivial icon file; since
  actual icon *content* can't be asserted from here, this is checked at the
  boundary of what a file-level test can prove: the file exists and is not
  the trivial placeholder size (tasks.md 2.4).
- `README.md` documents the `crons.json`/`instances.json` tracking exception
  and names BOTH concrete failure modes from requirements.md Requirement 6.3:
  a pulled cron whose `command`/`script`/paths/`env` reference a different
  machine, and a pulled instance record whose ssh aliases/SSM targets/port
  pairs/`remote_bin` belong to a different machine.

This is a JSON + Markdown content check, not a running-app test, so it is
expressed as a plain pytest file over the manifest and README text rather
than an app-lifecycle test. These tests are expected to FAIL against the
current `kirocrew app init` scaffold (defaultEnabled missing/true, one
message-only sample cron, sample agent/skill scaffold present, placeholder
icon, generic scaffold README) — this is the correct TDD starting state, not
a test defect.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
APP_JSON_PATH = APP_ROOT / "app.json"
README_PATH = APP_ROOT / "README.md"
ICON_PATH = APP_ROOT / "assets" / "icon.png"

# A freshly-scaffolded `kirocrew app init` icon.png placeholder is a tiny
# stub (well under 1KB). Anything genuinely replaced should clear this by a
# wide margin; this is a floor, not a claim about what a "real" icon weighs.
PLACEHOLDER_ICON_MAX_BYTES = 512


def _load_app_json() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(APP_JSON_PATH.read_text(encoding="utf-8")))


def _load_readme() -> str:
    return README_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# app.json: defaultEnabled
# ---------------------------------------------------------------------------


class TestDefaultEnabled:
    def test_default_enabled_key_present(self) -> None:
        manifest = _load_app_json()
        assert "defaultEnabled" in manifest, (
            "app.json must declare 'defaultEnabled' explicitly " "(requirements.md 8.2)"
        )

    def test_default_enabled_is_false(self) -> None:
        manifest = _load_app_json()
        assert manifest.get("defaultEnabled") is False, (
            "app.json's 'defaultEnabled' must be the boolean false so a "
            "first registration leaves the app off (requirements.md 8.2)"
        )


# ---------------------------------------------------------------------------
# app.json: exactly two command/script crons, both disabled
# ---------------------------------------------------------------------------


class TestCrons:
    def test_exactly_two_crons_declared(self) -> None:
        manifest = _load_app_json()
        crons = manifest.get("crons", [])
        assert len(crons) == 2, (
            "app.json must declare exactly two crons — one push, one poll "
            f"(requirements.md 8.3); found {len(crons)}"
        )

    def test_every_cron_is_command_or_script_based(self) -> None:
        """A message-only (LLM) cron would consume tokens on every tick and
        does not satisfy the zero-token command-cron design (design.md,
        tasks.md 2.4). Each declared cron must carry a `command` or `script`
        key, not just a bare `message`."""
        manifest = _load_app_json()
        crons = manifest.get("crons", [])
        for cron in crons:
            has_executable_field = "command" in cron or "script" in cron
            assert has_executable_field, (
                "every cron in app.json must use 'command' or 'script', "
                f"not a bare message-only cron (requirements.md 8.3): {cron!r}"
            )

    def test_every_cron_is_disabled(self) -> None:
        manifest = _load_app_json()
        crons = manifest.get("crons", [])
        for cron in crons:
            assert cron.get("enabled") is False, (
                "every cron in app.json must be declared with "
                f"'enabled: false' (requirements.md 8.3): {cron!r}"
            )

    def test_crons_cover_push_and_poll(self) -> None:
        """The two crons must be distinguishable as one push job and one
        poll job (design.md decisions: push = scheduled cron on tree-hash
        change; poll = every 15 minutes, notify-and-approve). This is
        checked loosely, by name/command/script substring, since the exact
        naming convention is an implementation choice — but each concept
        must be represented by exactly one cron, not zero or both by one."""
        manifest = _load_app_json()
        crons = manifest.get("crons", [])

        def _text_blob(cron: dict[str, Any]) -> str:
            return " ".join(
                str(cron.get(key, ""))
                for key in ("name", "command", "script", "message")
            ).lower()

        push_matches = [c for c in crons if "push" in _text_blob(c)]
        poll_matches = [c for c in crons if "poll" in _text_blob(c)]

        assert len(push_matches) == 1, (
            "exactly one cron must identify itself as the push job "
            f"(requirements.md 8.3); matched {len(push_matches)}"
        )
        assert len(poll_matches) == 1, (
            "exactly one cron must identify itself as the poll job "
            f"(requirements.md 8.3); matched {len(poll_matches)}"
        )
        assert push_matches[0] is not poll_matches[0], (
            "the push cron and the poll cron must be two distinct entries, "
            "not the same cron matching both labels"
        )


# ---------------------------------------------------------------------------
# app.json: scaffold sample-agent / sample-skill entries removed
# ---------------------------------------------------------------------------


class TestScaffoldEntriesRemoved:
    def test_no_sample_agent_reference_in_manifest(self) -> None:
        manifest = _load_app_json()
        agents = manifest.get("agents", [])
        assert "agents/sample-agent.json" not in agents, (
            "the kirocrew app init scaffold's sample agent entry must be "
            "removed from app.json (tasks.md 2.4)"
        )

    def test_no_sample_skill_reference_in_manifest(self) -> None:
        manifest = _load_app_json()
        skills = manifest.get("skills", [])
        assert "skills/sample-skill" not in skills, (
            "the kirocrew app init scaffold's sample skill entry must be "
            "removed from app.json (tasks.md 2.4)"
        )

    def test_sample_agent_file_absent(self) -> None:
        sample_agent_path = APP_ROOT / "agents" / "sample-agent.json"
        assert not sample_agent_path.exists(), (
            "the scaffold file agents/sample-agent.json must be deleted, "
            "not merely unreferenced (tasks.md 2.4)"
        )

    def test_sample_skill_dir_absent(self) -> None:
        sample_skill_dir = APP_ROOT / "skills" / "sample-skill"
        assert not sample_skill_dir.exists(), (
            "the scaffold directory skills/sample-skill/ must be deleted, "
            "not merely unreferenced (tasks.md 2.4)"
        )


# ---------------------------------------------------------------------------
# assets/icon.png: placeholder replaced
# ---------------------------------------------------------------------------


class TestIconReplaced:
    def test_icon_file_exists(self) -> None:
        assert ICON_PATH.exists(), (
            "app.json's iconPath (assets/icon.png) must exist on disk " "(tasks.md 2.4)"
        )

    def test_icon_is_not_the_trivial_placeholder_size(self) -> None:
        """Actual icon *content* cannot be meaningfully asserted from a
        file-level test. This asserts the one property that is checkable:
        the placeholder stub has been replaced with something substantive,
        by size floor, rather than left as the scaffold's tiny stand-in."""
        size = ICON_PATH.stat().st_size
        assert size > PLACEHOLDER_ICON_MAX_BYTES, (
            "assets/icon.png must be replaced with a real icon, not left as "
            f"the placeholder stub (tasks.md 2.4); file is only {size} bytes"
        )


# ---------------------------------------------------------------------------
# README.md: crons.json / instances.json exception documentation
# ---------------------------------------------------------------------------


class TestReadmeExceptionDocumentation:
    def test_readme_names_crons_json_and_instances_json(self) -> None:
        readme = _load_readme().lower()
        assert "crons.json" in readme, (
            "README.md must name crons.json explicitly when documenting "
            "the tracking exception (requirements.md 6.2)"
        )
        assert "instances.json" in readme, (
            "README.md must name instances.json explicitly when documenting "
            "the tracking exception (requirements.md 6.2)"
        )

    def test_readme_states_deliberate_documented_exception(self) -> None:
        readme = _load_readme().lower()
        assert "exception" in readme, (
            "README.md must state that tracking crons.json/instances.json "
            "is a deliberate, documented EXCEPTION to instance isolation, "
            "not routine practice (requirements.md 6.2)"
        )
        assert "isolation" in readme, (
            "README.md must reference instance isolation as the general "
            "principle this exception departs from (requirements.md 6.2)"
        )

    def test_readme_names_foreign_cron_failure_mode(self) -> None:
        """requirements.md 6.3, failure mode 1: a pulled crons.json can
        import jobs whose command, script, paths, or env reference resources
        that exist only on a different machine."""
        readme = _load_readme().lower()
        mentions_cron_fields = any(token in readme for token in ("command", "script"))
        mentions_foreign_machine = any(
            token in readme
            for token in ("different machine", "another machine", "other machine")
        )
        assert mentions_cron_fields, (
            "README.md must name the cron-side failure mode by its concrete "
            "fields (command/script/paths/env) (requirements.md 6.3)"
        )
        assert mentions_foreign_machine, (
            "README.md must state that a pulled cron can reference a "
            "DIFFERENT machine's resources, not just abstractly mention "
            "risk (requirements.md 6.3)"
        )

    def test_readme_names_foreign_instance_failure_mode(self) -> None:
        """requirements.md 6.3, failure mode 2: a pulled instances.json can
        import ssh host aliases, SSM targets, local/remote port pairs, and
        remote_bin paths belonging to a different machine, including a
        was_connected hint that drives lazy reconnect."""
        readme = _load_readme().lower()
        mentions_ssh_or_ssm = any(token in readme for token in ("ssh", "ssm"))
        mentions_ports_or_remote_bin = any(
            token in readme for token in ("port", "remote_bin")
        )
        assert mentions_ssh_or_ssm, (
            "README.md must name ssh aliases and/or SSM targets as part of "
            "the instance-side failure mode (requirements.md 6.3)"
        )
        assert mentions_ports_or_remote_bin, (
            "README.md must name port pairs and/or remote_bin paths as part "
            "of the instance-side failure mode (requirements.md 6.3)"
        )

    def test_readme_documents_both_failure_modes_distinctly(self) -> None:
        """Both failure modes named in requirements.md 6.3 must actually be
        present — this guards against a README that only documents one of
        the two and passes the narrower per-mode tests above by accident
        (e.g. a single sentence that happens to contain both a cron word and
        an instance word without describing either failure mode)."""
        readme = _load_readme().lower()
        cron_failure_present = (
            "crons.json" in readme
            and any(t in readme for t in ("command", "script"))
            and any(
                t in readme
                for t in ("different machine", "another machine", "other machine")
            )
        )
        instance_failure_present = (
            "instances.json" in readme
            and any(t in readme for t in ("ssh", "ssm"))
            and any(t in readme for t in ("port", "remote_bin"))
        )
        assert cron_failure_present, (
            "README.md's documentation of the cron failure mode must "
            "combine crons.json, a concrete field name, and the "
            "different-machine risk in a way a reader can trace "
            "(requirements.md 6.3)"
        )
        assert instance_failure_present, (
            "README.md's documentation of the instance failure mode must "
            "combine instances.json, a concrete field name (ssh/SSM), and "
            "a concrete field name (port/remote_bin) in a way a reader can "
            "trace (requirements.md 6.3)"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
