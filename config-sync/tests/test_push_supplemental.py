"""Supplemental coverage for backend/push.py's change path (task 3.2).

tests/test_push.py's own change-path test classes (which this file must
never modify — see that module's docstring) already exercise the scan,
authorization, unreadable-file, and byte-match branches for a fresh clone.
Two branches in the change path are not reachable by any of those tests
without changing their fixtures:

- `_scan_tree_for_secrets`'s per-file `UnicodeDecodeError` skip, which only
  fires on genuinely undecodable (non-UTF-8) tracked content.
- `run()`'s "bundle repo already cloned" branch (`git fetch` rather than
  `git clone`), which only fires when `<state dir>/bundle-repo/.git` already
  exists on disk before the tick runs.

Both are real behaviour, not implementation-detail restatements: the first
is push_policy's "text-only scan" contract; the second is design.md step
5's "clone/update" (update = fetch on an existing clone, not just create).

Fixtures are defined locally rather than imported from tests/test_push.py:
that module is scoped for its own 25 tests and must not be modified for
this file's sake, and a cross-module pytest-fixture import trips flake8's
F811 (a fixture parameter of the same name in a using test "redefines" the
imported one) without a clean per-file resolution.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock

import pytest

from backend import push


@pytest.fixture
def isolated_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict]:
    """Point KIROCREW_HOME / KIRO_HOME / the app's state dir at fresh temp

    directories, matching tests/test_push.py's own fixture of the same
    name and purpose.
    """
    root_a = tmp_path / "kirocrew_home"
    root_b = tmp_path / "kiro_home"
    root_a.mkdir()
    root_b.mkdir()

    monkeypatch.setenv("KIROCREW_HOME", str(root_a))
    monkeypatch.setenv("KIRO_HOME", str(root_b))
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(tmp_path / "state"))

    yield {"root_a": root_a, "root_b": root_b, "tmp_path": tmp_path}


def _write(root: Path, relpath: str, content: bytes) -> Path:
    """Write a file at relpath under root, creating parent dirs as needed."""
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _seed_changed_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seed state so the computed tree_hash differs from last_pushed_hash,

    driving push.run() down the change-path seam, matching
    tests/test_push.py's helper of the same name.
    """
    from backend import state

    store = state.load_state()
    monkeypatch.setattr(store, "last_pushed_hash", "a-completely-different-hash")
    monkeypatch.setattr(state, "load_state", lambda: store, raising=False)


@pytest.fixture
def change_path_collaborators(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict]:
    """Patch the change path's collaborators with permissive defaults,

    matching tests/test_push.py's fixture of the same name and shape.
    """
    from backend.safety import git_safety, push_policy, redact_msg

    scan_mock = MagicMock(
        name="push_policy.scan_content_for_secrets", return_value=(True, "ok")
    )
    authorize_mock = MagicMock(
        name="push_policy.authorize_direct_push",
        return_value=(True, "push authorized"),
    )
    git_argv_mock = MagicMock(name="git_safety.git_argv", wraps=git_safety.git_argv)
    redact_message_mock = MagicMock(
        name="redact_msg.redact_message", side_effect=lambda text: text
    )
    subprocess_run_mock = MagicMock(name="subprocess.run")

    monkeypatch.setattr(push_policy, "scan_content_for_secrets", scan_mock)
    monkeypatch.setattr(push_policy, "authorize_direct_push", authorize_mock)
    monkeypatch.setattr(git_safety, "git_argv", git_argv_mock)
    monkeypatch.setattr(redact_msg, "redact_message", redact_message_mock)
    monkeypatch.setattr(subprocess, "run", subprocess_run_mock)
    monkeypatch.setattr(subprocess, "Popen", MagicMock(name="subprocess.Popen"))

    if hasattr(push, "push_policy"):
        monkeypatch.setattr(push.push_policy, "scan_content_for_secrets", scan_mock)
        monkeypatch.setattr(push.push_policy, "authorize_direct_push", authorize_mock)
    if hasattr(push, "git_safety"):
        monkeypatch.setattr(push.git_safety, "git_argv", git_argv_mock)
    if hasattr(push, "redact_msg"):
        monkeypatch.setattr(push.redact_msg, "redact_message", redact_message_mock)
    if hasattr(push, "subprocess"):
        monkeypatch.setattr(push.subprocess, "run", subprocess_run_mock)

    yield {
        "scan": scan_mock,
        "authorize": authorize_mock,
        "git_argv": git_argv_mock,
        "redact_message": redact_message_mock,
        "subprocess_run": subprocess_run_mock,
    }


def test_scan_skips_a_file_whose_content_is_not_valid_utf8(
    isolated_roots: dict,
    change_path_collaborators: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tracked file whose (post-redaction) bytes cannot decode as UTF-8

    must not crash the scan — it is skipped, since `scan_content_for_secrets`
    scans text and an undecodable blob carries no scannable credential text
    either way. The push must still proceed (clean scan) rather than
    refusing on a decode error.
    """
    root_a = isolated_roots["root_a"]
    # `config.json` is an allowlisted path (unlike an arbitrary filename,
    # which `collect.collect()` would never pick up at all). Its content is
    # not valid UTF-8, so `redact.redact()`'s own JSON-decode also fails and
    # passes it through unchanged (see redact.py's docstring), and it
    # reaches the scanner exactly as written here.
    _write(root_a, "config.json", b"\xff\xfe\x00\x01\x02")

    _seed_changed_hash(monkeypatch)

    result = push.run()

    outcome = getattr(result, "outcome", result)
    assert str(outcome) == "pushed"
    # The one tracked file is undecodable, so the loop skips it without
    # ever calling the scanner — proving the skip, not a scan of empty text.
    change_path_collaborators["scan"].assert_not_called()


def test_scan_refusal_names_every_dirty_file_by_path(
    isolated_roots: dict,
    change_path_collaborators: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``hit: 2 finding(s)`` on its own left the operator with no way to find
    the offending file among hundreds (first live push, 2026-10-01). The
    tree-level refusal must name each dirty file's relpath -- a path is not
    scanned content, so this does not weaken push_policy's no-echo contract
    -- and must not stop at the first dirty file, so one failure lists all
    of them.
    """
    root_a = isolated_roots["root_a"]
    _write(root_a, "config.json", b'{"a": 1}')
    _write(root_a, "crons.json", b'{"jobs": []}')
    _write(root_a, "mcp.json", b"{}")

    _seed_changed_hash(monkeypatch)

    def _scan(text: str) -> tuple[bool, str]:
        # mcp.json is the clean one; redaction may re-serialise its JSON, so
        # key on the other two files' distinctive content instead of "{}".
        dirty = '"a"' in text or "jobs" in text
        return (False, "hit: 1 finding(s)") if dirty else (True, "ok")

    change_path_collaborators["scan"].side_effect = _scan

    result = push.run()

    assert result.outcome == "refused-secret-scan"
    assert result.reason.startswith("hit: 2 finding(s)")
    assert "config.json" in result.reason
    assert "crons.json" in result.reason
    assert "mcp.json" not in result.reason
    assert change_path_collaborators["scan"].call_count == 3


@pytest.fixture
def real_scanner(
    change_path_collaborators: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Undo the fixture's scanner mock so push.run() hits the REAL host
    scanner (kiro_crew.security via push_policy). Git/network stay mocked.
    The original function is recovered by executing push_policy's source
    into a fresh module, since the mock replaced the module attribute."""
    import importlib.util

    from backend.safety import push_policy

    spec = importlib.util.spec_from_file_location("_pp_fresh", push_policy.__file__)
    assert spec is not None and spec.loader is not None
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    original = fresh.scan_content_for_secrets
    monkeypatch.setattr(push_policy, "scan_content_for_secrets", original)
    if hasattr(push, "push_policy"):
        monkeypatch.setattr(push.push_policy, "scan_content_for_secrets", original)


_WEB_VERIFY_DOC = (
    b"# web-verify\n"
    b'3. **Open it:** `playwright-cli open "http://127.0.0.1:PORT/?token='
    b'\xe2\x80\xa6"`.\n'
    b'  "http://127.0.0.1:PORT/?token=\xe2\x80\xa6"` then screenshot /tmp/<name>.png\n'
)


def test_real_scanner_pushes_a_tracked_skill_doc_with_token_placeholders(
    isolated_roots: dict,
    change_path_collaborators: dict,
    real_scanner: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live bug, end to end with the host's real scanner: KiroCrew's own
    shipped `skills/web-verify/SKILL.md` carries `?token=…` placeholders, is
    allowlisted, and must not refuse the push (2026-10-01 first live push:
    "hit: 2 finding(s)")."""
    root_a = isolated_roots["root_a"]
    _write(root_a, "skills/web-verify/SKILL.md", _WEB_VERIFY_DOC)
    _write(root_a, "steering/notes.md", b"# notes\nplain prose, no secrets.\n")

    _seed_changed_hash(monkeypatch)

    result = push.run()

    assert result.outcome == "pushed", result.reason


def test_real_scanner_refuses_a_real_bearer_and_names_the_file(
    isolated_roots: dict,
    change_path_collaborators: dict,
    real_scanner: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_a = isolated_roots["root_a"]
    _write(root_a, "skills/web-verify/SKILL.md", _WEB_VERIFY_DOC)
    bearer = b"q7Zp2mXv9Lk4Rt8Yw3Nb6Hs1Dg5Fj0Ca"
    _write(root_a, "steering/leak.md", b"see http://127.0.0.1/?token=" + bearer)

    _seed_changed_hash(monkeypatch)

    result = push.run()

    assert result.outcome == "refused-secret-scan"
    assert result.reason.startswith("hit: 1 finding(s) in steering/leak.md")
    assert "web-verify" not in result.reason
    assert bearer.decode() not in result.reason
    change_path_collaborators["subprocess_run"].assert_not_called()


def test_unavailable_scanner_refuses_once_without_listing_every_file(
    isolated_roots: dict,
    change_path_collaborators: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-closed must not become fail-noisy: when the scanner itself is
    unavailable, stop at the first file -- one refusal, no per-file list,
    no 400-traceback log storm per tick."""
    root_a = isolated_roots["root_a"]
    for name in ("config.json", "crons.json", "mcp.json"):
        _write(root_a, name, b"{}")

    _seed_changed_hash(monkeypatch)
    change_path_collaborators["scan"].return_value = (False, "no_scanner")

    result = push.run()

    assert result.outcome == "refused-secret-scan"
    assert result.reason == "no_scanner"
    assert change_path_collaborators["scan"].call_count == 1


def test_scan_refusal_caps_the_named_paths(
    isolated_roots: dict,
    change_path_collaborators: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reason rendered on a dashboard card and stored in state.json must
    stay bounded: name at most five files, then "... and N more"."""
    root_a = isolated_roots["root_a"]
    for i in range(8):
        _write(root_a, f"steering/s{i}.md", b"x")

    _seed_changed_hash(monkeypatch)
    change_path_collaborators["scan"].return_value = (False, "hit: 1 finding(s)")

    result = push.run()

    assert result.outcome == "refused-secret-scan"
    assert result.reason.startswith("hit: 8 finding(s) in ")
    assert result.reason.count("steering/s") == 5
    assert result.reason.endswith(" and 3 more")


def test_run_fetches_rather_than_clones_when_bundle_repo_already_exists(
    isolated_roots: dict,
    change_path_collaborators: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """design.md step 5 is "clone/UPDATE the bundle repo" — when the app's

    own state directory already holds a `.git` for the bundle clone, the
    change path must fetch the existing clone rather than cloning a second
    time, so a repeated tick reuses the same working copy instead of
    re-cloning on every push.
    """
    from backend import state

    root_a = isolated_roots["root_a"]
    _write(root_a, "config.json", b'{"key": "value"}')

    _seed_changed_hash(monkeypatch)

    clone_dir = state.get_state_dir() / "bundle-repo"
    (clone_dir / ".git").mkdir(parents=True, exist_ok=True)

    push.run()

    git_argv_calls = [
        call.args for call in change_path_collaborators["git_argv"].call_args_list
    ]
    fetch_calls = [c for c in git_argv_calls if "fetch" in c]
    clone_calls = [c for c in git_argv_calls if "clone" in c]
    assert fetch_calls, "an existing clone must be updated via `git fetch`"
    assert not clone_calls, "an existing clone must not be re-cloned"
