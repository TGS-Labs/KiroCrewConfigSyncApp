from hypothesis import settings, Verbosity
import glob
import os
import sys

import pytest

# Test-environment wiring, repo-wide: backend/safety/push_policy.py and
# backend/safety/redact_msg.py both call kiro_crew.security at runtime (see
# design.md). The deployed app always runs as a subprocess of the KiroCrew
# gateway, which already has `kiro_crew` on sys.path from its own
# installation -- but this venv is isolated from that installation, so
# tests need the system site-packages directory added here. This never
# adds to, edits, or copies the installed `kiro_crew` package
# (requirements.md 1.1 / 8.4) -- it only makes an already-installed,
# untouched package importable from this venv. Mirrors
# tests/safety/conftest.py's shim (kept in place for that subtree); this
# root-level copy covers top-level test modules such as
# tests/test_buildo_pr.py that also exercise redact_message.
for _candidate in glob.glob("/usr/local/lib/python3.*/site-packages"):
    if (
        os.path.isdir(os.path.join(_candidate, "kiro_crew"))
        and _candidate not in sys.path
    ):
        sys.path.append(_candidate)

settings.register_profile(
    "dev",
    max_examples=15,
    verbosity=Verbosity.normal,
    deadline=None,
)
settings.register_profile(
    "ci",
    max_examples=100,
    verbosity=Verbosity.verbose,
    deadline=None,
)
settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "dev"))


@pytest.fixture(autouse=True)
def _isolate_config_roots(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default every test to disposable config roots and state dir.

    The poll tick now applies and then hashes the live roots, so a test
    that forgot to isolate them would walk (and could write to) the real
    host's ~/.kiro. Tests that set their own values override these.
    """
    base = tmp_path_factory.mktemp("isolated-roots")
    monkeypatch.setenv("KIROCREW_HOME", str(base / "root-a"))
    monkeypatch.setenv("KIRO_HOME", str(base / "root-b"))
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(base / "state"))
