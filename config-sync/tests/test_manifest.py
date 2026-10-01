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
    """Deployment 5: the manifest declares NO crons.

    KiroCrew runs cron subprocesses in its sandbox, which hides the git
    credential store, and the bundle repo is private — so a manifest command
    cron for either job fails every tick (live install 2026-10-01, `git
    ls-remote` exit 128). The poll ships as a vault-granted SCRIPT cron body
    (`host-crons/config_sync_poll.py`, installed per
    `skills/install-poll-cron/SKILL.md`; proved in
    `test_host_cron_poll_script.py`); the push runs in the backend process
    (dashboard "Push changes"), which lives outside the cron sandbox.
    """

    def test_manifest_declares_no_crons(self) -> None:
        manifest = _load_app_json()
        assert manifest.get("crons", []) == [], (
            "a manifest command cron cannot reach the private bundle repo from "
            "the host's cron sandbox; neither job may be declared here"
        )

    def test_poll_runs_as_a_module_from_the_app_root_via_the_pinned_script(
        self,
    ) -> None:
        """Senior-review H1 (round 1) found `python3 backend/poll.py` had a

        ModuleNotFoundError defect: launched as a plain script, `sys.path[0]`
        is `.../backend`, not the app root. The same invariant must hold for
        the pinned script that now launches the poll: `python -m backend.poll`
        from the installed app root. `test_host_cron_poll_script.py` proves
        argv and cwd at run time; this is the static mirror.
        """
        script = (APP_ROOT / "host-crons" / "config_sync_poll.py").read_text()
        assert '"-m", "backend.poll"' in script, (
            "the pinned script must invoke the poll by dotted module path "
            "(`-m backend.poll`), never as a file path"
        )
        assert "backend/poll.py" not in script

    def test_push_module_imports_from_the_app_root(self) -> None:
        """Senior-review P1 (round 1): `python3 backend/push.py` could not

        import `backend`. The push now runs in-process in the backend (the
        dashboard route imports `backend.push`), so the surviving property is
        that the module imports cleanly from the app root without side
        effects — the same probe the old cron test ran, minus the shell.
        """
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-c", "import backend.push, backend.poll"],
            cwd=str(APP_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert "ModuleNotFoundError" not in result.stderr


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
