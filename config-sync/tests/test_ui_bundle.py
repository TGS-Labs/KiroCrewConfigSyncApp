"""Seam test: the shipped UI bundle matches ``app.json`` and the sources.

Senior review round 3 (H-2): ``kirocrew app install`` copies the app
directory as-is and never runs ``vite build``, and the gateway serves
``<app>/ui/<ui.entry>`` verbatim. So the built bundle MUST be committed, at
the path the manifest names, and MUST correspond to the committed sources —
otherwise a clean checkout installs an app whose sidebar page is a 404, or
one that silently serves stale UI.

The rebuild check needs Node. On a host without it the check is skipped
with an explicit reason; the presence/manifest checks always run.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[1]
UI_ROOT = APP_ROOT / "ui"


def _manifest_ui_entry() -> str:
    manifest = json.loads((APP_ROOT / "app.json").read_text(encoding="utf-8"))
    entry = manifest["ui"]["entry"]
    assert isinstance(entry, str) and entry
    return entry


def test_manifest_ui_entry_exists_under_ui_root() -> None:
    """The gateway resolves ``ui.entry`` under ``<app>/ui/`` — the bundle
    must be present there in a clean checkout, not produced at install."""
    bundle = UI_ROOT / _manifest_ui_entry()
    assert bundle.is_file(), f"shipped UI bundle missing: {bundle}"
    assert bundle.stat().st_size > 0


def test_shipped_ui_bundle_is_tracked_by_git() -> None:
    """A bundle that exists locally but is gitignored still ships nothing."""
    bundle = UI_ROOT / _manifest_ui_entry()
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(bundle)],
        cwd=APP_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"{bundle.relative_to(APP_ROOT)} is not tracked by git: {result.stderr}"
    )


def test_shipped_ui_bundle_matches_a_fresh_build(tmp_path: Path) -> None:
    """Rebuild into a scratch outDir and compare bytes: a source edit without
    a rebuilt, committed bundle fails here instead of shipping stale UI."""
    node_env = Path(
        os.environ.get(
            "CONFIG_SYNC_NODE_ENV",
            str(Path.home() / ".kiro/crew/workspace/tools/node-env.sh"),
        )
    )
    if shutil.which("npx") is None and not node_env.is_file():
        pytest.skip("Node.js not available on this host; bundle drift not checked")
    if not (UI_ROOT / "node_modules").is_dir():
        pytest.skip("ui/node_modules not installed; bundle drift not checked")

    bundle = UI_ROOT / _manifest_ui_entry()
    prefix = f". '{node_env}' && " if node_env.is_file() else ""
    out_dir = tmp_path / "dist"
    build = subprocess.run(
        [
            "bash",
            "-c",
            f"{prefix}CI=1 npx vite build --outDir '{out_dir}' --emptyOutDir",
        ],
        cwd=UI_ROOT,
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
        timeout=240,
    )
    assert build.returncode == 0, f"vite build failed:\n{build.stderr[-2000:]}"
    fresh = out_dir / bundle.name
    assert fresh.is_file(), f"build produced no {bundle.name}"
    assert fresh.read_bytes() == bundle.read_bytes(), (
        "committed ui/dist bundle differs from a fresh build of ui/src — "
        "run `vite build` in ui/ and commit the result"
    )
