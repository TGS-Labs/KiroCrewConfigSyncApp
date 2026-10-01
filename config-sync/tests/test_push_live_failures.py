"""Phase 8 live failure #4: "Push changes" broke the page.

The gateway-spawned backend ran the push with the gateway's clean
environment: no global git config, so `git commit` exited 128
("Author identity unknown") — every push attempt on a fresh box fails
before anything is pushed. Worse, `routes.push_now` let the
`CalledProcessError` propagate out of the HTTP handler, which dropped the
connection; the gateway proxy logged "Server disconnected" and the page
showed a 502 instead of the failure it had just recorded.

Both are seams the mocked push tests could never see: tests/test_push.py
replaces `subprocess.run`, and no server test exercised a route raising.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest

from backend.safety import git_safety
from test_server_security import (
    _request,
    _valid_headers,
    isolated_state_dir,
    route_calls,
    running_server,
)

REUSED_FIXTURES = (isolated_state_dir, route_calls, running_server)


@pytest.fixture
def identityless_git_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Dict[str, str]:
    """What the gateway-spawned backend actually sees: no user.name /
    user.email from any config level."""
    home = tmp_path / "empty-home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "EMAIL"))}
    env.update(
        {
            "HOME": str(home),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
    )
    return env


def test_git_argv_commits_without_any_configured_identity(
    identityless_git_env: Dict[str, str], tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    env = identityless_git_env
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
    (repo / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(git_safety.git_argv(repo, "add", "-A"), check=True, env=env)
    result = subprocess.run(
        git_safety.git_argv(repo, "commit", "-m", "probe"),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "the push job's git commit fails on a box with no git identity "
        f"(the gateway's clean env): rc={result.returncode}\n{result.stderr}"
    )
    author = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%an <%ae>"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert author == f"{git_safety.COMMIT_NAME} <{git_safety.COMMIT_EMAIL}>"


def test_a_route_that_raises_answers_500_json_and_the_server_survives(
    running_server: Tuple[str, int],
    route_calls: Dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend import routes as routes_module

    def exploding_push(store: Any) -> Dict[str, Any]:
        raise subprocess.CalledProcessError(128, ["git", "commit"])

    monkeypatch.setattr(routes_module, "push_now", exploding_push)
    host, port = running_server
    status, headers, body = _request(
        host, port, "POST", "/api/push", _valid_headers(host, port), b""
    )
    assert status == 500, (status, body[:200])
    assert headers.get("content-type", "").startswith("application/json")
    payload = json.loads(body)
    assert payload["status"] == "error"
    assert "128" in payload["reason"]

    # The handler must not have taken the server down with it.
    status2, _, body2 = _request(
        host, port, "GET", "/api/status", _valid_headers(host, port)
    )
    assert status2 == 200, (status2, body2[:200])
    assert route_calls["status"] == 1


def test_route_error_reason_is_redacted(
    running_server: Tuple[str, int],
    route_calls: Dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exception message can carry anything (a git stderr, a token); the
    page must get the same redaction every other surfaced message gets."""
    from backend import routes as routes_module

    def leaking_push(store: Any) -> Dict[str, Any]:
        raise RuntimeError(
            "remote: https://x:ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123@github.com"
        )

    monkeypatch.setattr(routes_module, "push_now", leaking_push)
    host, port = running_server
    status, _, body = _request(
        host, port, "POST", "/api/push", _valid_headers(host, port), b""
    )
    assert status == 500
    assert "ghp_ABCDEFGHIJ" not in body.decode("utf-8", "replace")
