"""Unit coverage for `backend.poll._fetch_commit_details`'s two defensive

edge branches that `tests/test_poll_pending.py`'s fixtures never exercise
(that suite always sends a well-formed, tab-delimited metadata line):

- `git show` producing no readable stdout at all (empty/whitespace-only) —
  `_fetch_commit_details` must degrade to `("", "", [])` rather than raise
  an `IndexError`.
- A metadata line with no tab separator (a malformed/unexpected `git show`
  format line) — must fall back to treating the whole line as `author`
  with an empty `subject`, rather than raising a `ValueError` on unpack.

These are implementation-detail unit tests for a private helper (not a
behavioural contract test like `test_poll_pending.py`/`test_poll.py`), so
they may reach into `backend.poll._fetch_commit_details` directly.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock

import pytest

from backend import poll


@pytest.fixture
def isolated_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(state_dir))
    yield state_dir


def _completed(*, stdout: str) -> MagicMock:
    completed = MagicMock(name="CompletedProcess")
    completed.stdout = stdout
    completed.returncode = 0
    return completed


def test_no_readable_output_degrades_to_empty_tuple(
    isolated_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WHEN `git show` produces empty stdout THEN `_fetch_commit_details`

    returns `("", "", [])` rather than raising on an empty `lines` list.
    """
    run_mock = MagicMock(name="subprocess.run", return_value=_completed(stdout=""))
    monkeypatch.setattr(subprocess, "run", run_mock)

    result = poll._fetch_commit_details(str(isolated_state_dir), "a" * 40)

    assert result == ("", "", [])


def test_metadata_line_with_no_tab_falls_back_to_author_only(
    isolated_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WHEN the first line of `git show`'s output has no tab separator THEN

    `_fetch_commit_details` treats the whole line as `author` with an
    empty `subject`, rather than raising on a 2-tuple unpack.
    """
    stdout = "no-tab-here\nsteering/x.md\n"
    run_mock = MagicMock(name="subprocess.run", return_value=_completed(stdout=stdout))
    monkeypatch.setattr(subprocess, "run", run_mock)

    author, subject, changed_paths = poll._fetch_commit_details(
        str(isolated_state_dir), "b" * 40
    )

    assert author == "no-tab-here"
    assert subject == ""
    assert changed_paths == ["steering/x.md"]
