"""Store-install contract: the app directory must not trigger a build step.

KiroCrew's App Store installer (`kiro_crew.apps.registry._run_app_build`)
inspects the APP directory (the registry entry's ``subdirectory``) and runs
``pip install .`` when it finds ``pyproject.toml`` / ``setup.py`` /
``requirements.txt``, or ``npm install`` when it finds ``package.json``. This
app is plain stdlib Python run in place by the gateway; it is not a Python
package and must not be pip-installed into the gateway interpreter. The first
store install (2026-10-01) failed exactly this way: the dev-tooling
``pyproject.toml`` that lived in the app directory made the store run
``pip install .``, and setuptools refused the flat layout.

Dev tooling configuration therefore lives in the REPOSITORY root
``pyproject.toml``, outside the app directory, and this test keeps it there.
"""

from __future__ import annotations

from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = APP_DIR.parent

#: Exactly the detection list in the host's `_run_app_build`.
BUILD_TRIGGER_FILES = ("pyproject.toml", "setup.py", "requirements.txt", "package.json")


@pytest.mark.parametrize("name", BUILD_TRIGGER_FILES)
def test_app_dir_has_no_build_trigger_file(name: str) -> None:
    assert not (APP_DIR / name).exists(), (
        f"{name} in the app directory makes the App Store run a build step "
        "(pip install . / npm install) that this stdlib app does not want; "
        "keep tooling config in the repository-root pyproject.toml"
    )


def test_tooling_config_lives_at_the_repo_root() -> None:
    root_pyproject = REPO_ROOT / "pyproject.toml"
    assert root_pyproject.is_file()
    text = root_pyproject.read_text(encoding="utf-8")
    assert "[tool.pytest.ini_options]" in text
    assert "[tool.black]" in text
    assert "[tool.flake8]" in text
    assert "[tool.mypy]" in text
    # Paths are relative to the repo root now, so they must point into the app.
    assert 'testpaths = ["config-sync/tests"]' in text
    assert 'pythonpath = ["config-sync"]' in text


def test_app_manifest_still_at_the_app_root() -> None:
    assert (APP_DIR / "app.json").is_file()
    assert (REPO_ROOT / "app-registry.json").is_file()
