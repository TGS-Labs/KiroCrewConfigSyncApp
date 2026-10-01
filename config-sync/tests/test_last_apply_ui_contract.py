"""`last_apply` must carry every key the page's `LastApply` type requires.

Live-install defect 9 (2026-10-01): the first real apply that paused a cron
crashed the Config Sync page with "Cannot read properties of undefined
(reading 'find')" — `LastApplyCard` does `lastApply.changed_commands.find`
whenever `paused_cron_names` is non-empty, but
`materialize._apply_result_to_dict` never emitted `changed_commands`, so the
UI type (`ui/src/types.ts`) promised a key the backend did not send. The
real-fixture shape test did not catch it: its `last_apply` came from a
partial tick with no crons in range, and `_shape` compares only the keys both
sides have.

Seam test (testing-standards § Composition): parse `ui/src/types.ts`'s
`LastApply` interface for its REQUIRED keys and assert each is present in a
`last_apply` rendered from a real tick that pauses a cron.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Iterator

import pytest

from backend import poll, state

APP_ROOT = Path(__file__).resolve().parents[1]
TYPES_TS = APP_ROOT / "ui" / "src" / "types.ts"

_GIT_ENV_ARGS = ["-c", "user.name=t", "-c", "user.email=t@example.com"]


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *_GIT_ENV_ARGS, *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def required_last_apply_keys() -> set[str]:
    """Keys of `interface LastApply` in types.ts declared WITHOUT `?`."""
    text = TYPES_TS.read_text(encoding="utf-8")
    match = re.search(r"export interface LastApply \{(.*?)\n\}", text, re.S)
    assert match, "LastApply interface not found in types.ts"
    keys: set[str] = set()
    for line in match.group(1).splitlines():
        line = line.strip()
        m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)(\??):", line)
        if m and not m.group(2):
            keys.add(m.group(1))
    assert keys, "no required keys parsed from LastApply"
    return keys


@pytest.fixture
def tick_that_pauses_a_cron(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Any]]:
    root_a = tmp_path / "kirocrew-home"
    root_b = tmp_path / "kiro-home"
    state_dir = tmp_path / "config-sync-state"
    for d in (root_a, root_b, state_dir):
        d.mkdir(parents=True)
    monkeypatch.setenv("KIROCREW_HOME", str(root_a))
    monkeypatch.setenv("KIRO_HOME", str(root_b))
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setenv(poll.PREFETCHED_ENV, "1")
    monkeypatch.setattr(poll, "BUNDLE_REPO_URL", "https://127.0.0.1:9/x.git")

    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    (work / "crons.json").write_text(
        json.dumps(
            {
                "version": 3,
                "jobs": [
                    {
                        "id": "abcd1234",
                        "name": "fleet-job",
                        "command": "echo hi",
                        "schedule": {"every": 900},
                        "enabled": True,
                    }
                ],
            }
        )
        + "\n"
    )
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "crons", cwd=work)
    origin = tmp_path / "origin.git"
    _git("clone", "-q", "--bare", str(work), str(origin), cwd=tmp_path)
    clone_dir = state_dir / poll._BUNDLE_CLONE_DIRNAME
    _git("clone", "-q", str(origin), str(clone_dir), cwd=tmp_path)
    _git("remote", "set-url", "origin", "https://127.0.0.1:9/x.git", cwd=clone_dir)
    (root_a / "crons.json").write_text(json.dumps({"version": 3, "jobs": []}) + "\n")

    result = poll.run()
    assert result.outcome == "changed", (result.outcome, result.reason)
    last_apply = state.load_state().last_apply
    assert last_apply is not None
    yield {"last_apply": last_apply, "root_a": root_a}


def test_last_apply_has_every_key_the_ui_type_requires(
    tick_that_pauses_a_cron: dict[str, Any],
) -> None:
    last_apply = tick_that_pauses_a_cron["last_apply"]
    assert last_apply["paused_cron_names"] == ["fleet-job"], last_apply
    missing = sorted(required_last_apply_keys() - set(last_apply))
    assert missing == [], f"last_apply lacks keys the page dereferences: {missing}"


def test_changed_commands_is_a_list_even_when_no_hook_or_mcp_changed(
    tick_that_pauses_a_cron: dict[str, Any],
) -> None:
    """Requirement 6.10 scopes `changed_commands` to hooks.json/mcp.json; a
    crons-only apply still sends the key as an empty list so the card's
    `.find` has something to call."""
    assert tick_that_pauses_a_cron["last_apply"]["changed_commands"] == []
