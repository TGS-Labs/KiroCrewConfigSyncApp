"""Failing tests for tasks.md 7.3: push.py runs portable.tokenize after

redact, before tree_hash (requirements.md 2.8-2.11).

Contract under test — design.md's push.py step list, amended by the
Deployment-4 tokenize step: "Collect (allowlist) -> redact ->
portable.tokenize(every collected JSON file, Requirement 2.8 scope) ->
canonical serialize -> tree_hash". Order matters: redact.redact() must run
BEFORE portable.tokenize() so a `headers`/`env` value is already the literal
placeholder `"<redacted>"` by the time tokenize walks the tree — it is never
inspected for a root-path prefix (requirements.md 2.8's own worked example).

Scope is every tracked file that parses as JSON — not `agents/*.json`
alone (requirements.md 2.8: "root A's `config.json`, `hooks.json`,
`mcp.json`, `crons.json`, `instances.json`, `agent_model_state.json`, and
root B's `agents/*.json`").

`PushResult` currently (as of tasks.md 7.2, HEAD b6179aa) carries only
`outcome`, `tree_hash`, `reason` — no non-portable list. This file PINS the
new field's name as `non_portable`: a list of per-value reports, each a
mapping with keys `path` (the tracked relpath) and `key_path` (the JSON key
path within that file, as a list of str/int segments), matching
requirements.md 2.9 ("list it in the push result as non-portable (file path
and JSON key path)"). Every test below is expected to fail RED against the
current `backend/push.py`/`backend/state.py` — either at collection
(`AttributeError`/`KeyError` on a `non_portable` field that does not exist
yet) or on an assertion — because `push.run()`'s no-op path does not yet
call `portable.tokenize` at all. This is the correct TDD starting state.

Every test uses REAL `collect.collect()` against real tmp roots (per the
task's explicit instruction) and stubs only the network/Buildo/git-push edge
`tests/test_push.py` already stubs (`change_path_collaborators`,
`no_git_or_network_calls`) — never `backend/portable.py`, `backend/redact.py`,
or `backend/collect.py` themselves.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable, Dict, Iterator
from unittest.mock import MagicMock

import pytest

from backend import push

# ---------------------------------------------------------------------------
# Fixtures — mirrors tests/test_push.py's isolated_roots / no_git_or_network
# / change_path_collaborators conventions exactly, duplicated here (rather
# than imported) because pytest fixtures are file-scoped by default and this
# is a fresh test module per the task's instruction ("New file
# tests/test_push_portable.py").
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict]:
    """Point KIROCREW_HOME (root A), KIRO_HOME (root B), and the app's own

    state directory at fresh, disposable temp directories — no test here may
    touch the real host configuration or the real ~/.config-sync state.
    """
    root_a = tmp_path / "kirocrew_home"
    root_b = tmp_path / "kiro_home"
    root_a.mkdir()
    root_b.mkdir()

    monkeypatch.setenv("KIROCREW_HOME", str(root_a))
    monkeypatch.setenv("KIRO_HOME", str(root_b))
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(tmp_path / "state"))

    yield {"root_a": root_a, "root_b": root_b, "tmp_path": tmp_path}


def _write_json(root: Path, relpath: str, doc: dict) -> Path:
    """Write a JSON document at relpath under root, creating parent dirs."""
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return path


def _write_text(root: Path, relpath: str, text: str) -> Path:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def no_git_or_network_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[], None]:
    """Spy every host-side git/subprocess surface and return an assertion

    helper that fails loudly if any of them was called — identical
    convention to tests/test_push.py's own fixture of the same name.
    """
    from backend.safety import git_safety

    git_argv_spy = MagicMock(name="git_safety.git_argv", wraps=git_safety.git_argv)
    monkeypatch.setattr(git_safety, "git_argv", git_argv_spy)
    if hasattr(push, "git_argv"):
        monkeypatch.setattr(push, "git_argv", git_argv_spy)
    if hasattr(push, "git_safety"):
        monkeypatch.setattr(push.git_safety, "git_argv", git_argv_spy)

    subprocess_run_spy = MagicMock(name="subprocess.run")
    subprocess_popen_spy = MagicMock(name="subprocess.Popen")
    monkeypatch.setattr(subprocess, "run", subprocess_run_spy)
    monkeypatch.setattr(subprocess, "Popen", subprocess_popen_spy)
    if hasattr(push, "subprocess"):
        monkeypatch.setattr(push.subprocess, "run", subprocess_run_spy)
        monkeypatch.setattr(push.subprocess, "Popen", subprocess_popen_spy)

    def _assert_never_called() -> None:
        git_argv_spy.assert_not_called()
        subprocess_run_spy.assert_not_called()
        subprocess_popen_spy.assert_not_called()

    return _assert_never_called


@pytest.fixture
def change_path_collaborators(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[dict]:
    """Patch every change-path collaborator (scan, git_argv, authorize,

    message redaction) with permissive defaults — identical convention to
    tests/test_push.py's fixture of the same name, duplicated here since
    fixtures do not cross test files.
    """
    from backend.safety import git_safety, push_policy, redact_msg

    scan_mock = MagicMock(
        name="push_policy.scan_content_for_secrets", return_value=(True, "ok")
    )
    authorize_mock = MagicMock(
        name="push_policy.authorize_direct_push",
        return_value=(True, "push authorized"),
    )
    git_argv_mock = MagicMock(
        name="git_safety.git_argv",
        side_effect=lambda cwd, *args: ["git", "-C", str(cwd), *args],
    )
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

    from backend import pr_handoff

    monkeypatch.setattr(
        pr_handoff,
        "build_pull_request_payload",
        MagicMock(
            return_value={
                "repo": "TGS-Labs/Kiro-Config-Bundles",
                "base": "main",
                "head": "irrelevant",
                "title": "chore: sync",
                "body": "Automated config sync.",
            }
        ),
    )
    monkeypatch.setattr(pr_handoff, "notify_operator", MagicMock())

    yield {
        "scan": scan_mock,
        "authorize": authorize_mock,
        "git_argv": git_argv_mock,
        "redact_message": redact_message_mock,
        "subprocess_run": subprocess_run_mock,
    }


def _collected_json_relpaths(collected: Dict[str, bytes]) -> list:
    """Every tracked relpath in `collected` whose content parses as JSON —

    the Requirement 2.8 scope, discovered from the ACTUAL collected tree
    rather than a hand-named fixture list (testing-standards.md's
    Parametrised Guard Coverage: iterate the discovered collection).
    """
    json_paths = []
    for relpath, content in collected.items():
        try:
            json.loads(content.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        json_paths.append(relpath)
    return json_paths


# ---------------------------------------------------------------------------
# Requirement 2.11 — identical config under two different roots produces a
# byte-identical collected+redacted+tokenized tree and equal tree_hash.
# ---------------------------------------------------------------------------


def _seed_identical_config(root_a: Path, root_b: Path, home_prefix: str) -> None:
    """Seed both roots with tracked files whose content embeds `home_prefix`

    as the absolute root, mirroring a real agent definition/config that
    carries this host's own path.
    """
    _write_json(
        root_a,
        "config.json",
        {
            "agents": {
                "foo": {"prompt": f"file://{home_prefix}/crew/agent-prompts/foo.md"}
            }
        },
    )
    _write_json(
        root_a,
        "mcp.json",
        {
            "mcpServers": {
                "github": {
                    "command": "node",
                    "args": [f"{home_prefix}/crew/mcp/github.js"],
                }
            }
        },
    )
    _write_text(root_a, "config-bundles/agent-prompts/foo.md", "You are foo.")
    _write_json(
        root_b,
        "agents/foo.json",
        {"name": "foo", "prompt": f"file://{home_prefix}/crew/agent-prompts/foo.md"},
    )


class TestIdenticalConfigAcrossTwoRootsProducesByteIdenticalTree:
    def test_byte_identical_collected_trees_across_two_roots(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Requirement 2.11: two hosts with different root paths holding

        identical configuration (same content once each host's own root is
        substituted by its token) must produce byte-identical pushed trees.
        Uses REAL collect.collect() against two genuinely different tmp
        root pairs (host 1 under tmp_path/'host1', host 2 under
        tmp_path/'host2'), each with its OWN distinct absolute root prefix.
        """
        from backend import collect, redact

        host1_a = tmp_path / "host1" / "kirocrew_home"
        host1_b = tmp_path / "host1" / "kiro_home"
        host1_a.mkdir(parents=True)
        host1_b.mkdir(parents=True)
        _seed_identical_config(host1_a, host1_b, str(host1_b))

        host2_a = tmp_path / "host2" / "somewhere-else" / "kirocrew_home"
        host2_b = tmp_path / "host2" / "somewhere-else" / "kiro_home"
        host2_a.mkdir(parents=True)
        host2_b.mkdir(parents=True)
        _seed_identical_config(host2_a, host2_b, str(host2_b))

        monkeypatch.setenv("KIROCREW_HOME", str(host1_a))
        monkeypatch.setenv("KIRO_HOME", str(host1_b))
        host1_collected = collect.collect()
        host1_redacted = redact.redact(host1_collected)
        host1_tokenized = push.tokenize_tree(host1_redacted)

        monkeypatch.setenv("KIROCREW_HOME", str(host2_a))
        monkeypatch.setenv("KIRO_HOME", str(host2_b))
        host2_collected = collect.collect()
        host2_redacted = redact.redact(host2_collected)
        host2_tokenized = push.tokenize_tree(host2_redacted)

        assert host1_tokenized == host2_tokenized, (
            "identical config under two different root layouts must "
            "tokenize to a byte-identical tree"
        )

    def test_equal_tree_hash_across_two_roots(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same as above, but asserts on push.tree_hash() directly — the

        actual gate value push.run() compares against state.last_pushed_hash.
        """
        from backend import collect, redact

        host1_a = tmp_path / "host1" / "kirocrew_home"
        host1_b = tmp_path / "host1" / "kiro_home"
        host1_a.mkdir(parents=True)
        host1_b.mkdir(parents=True)
        _seed_identical_config(host1_a, host1_b, str(host1_b))

        host2_a = tmp_path / "host2" / "elsewhere" / "kirocrew_home"
        host2_b = tmp_path / "host2" / "elsewhere" / "kiro_home"
        host2_a.mkdir(parents=True)
        host2_b.mkdir(parents=True)
        _seed_identical_config(host2_a, host2_b, str(host2_b))

        monkeypatch.setenv("KIROCREW_HOME", str(host1_a))
        monkeypatch.setenv("KIRO_HOME", str(host1_b))
        host1_hash = push.tree_hash(
            push.tokenize_tree(redact.redact(collect.collect()))
        )

        monkeypatch.setenv("KIROCREW_HOME", str(host2_a))
        monkeypatch.setenv("KIRO_HOME", str(host2_b))
        host2_hash = push.tree_hash(
            push.tokenize_tree(redact.redact(collect.collect()))
        )

        assert host1_hash == host2_hash


# ---------------------------------------------------------------------------
# Requirement 2.8/2.11 — no root path string appears in ANY tracked JSON
# file of the pushed tree. Iterates the DISCOVERED collection of tracked
# JSON files, per-member assertion (testing-standards.md Parametrised Guard
# Coverage) — never a single hand-named fixture or an `any(...)` check.
# ---------------------------------------------------------------------------


class TestNoRootPathInAnyTrackedJsonFile:
    """Security/privacy guard: this host's absolute root paths must not

    survive into the pushed tree for any tracked JSON file. Mutation-tested
    below (see TestMutationSkippingTokenizeTurnsGuardRed) per
    testing-standards.md's Mutation Requirement.
    """

    def test_root_path_absent_from_every_discovered_tracked_json_file(
        self, isolated_roots: dict
    ) -> None:
        root_a = isolated_roots["root_a"]
        root_b = isolated_roots["root_b"]
        _seed_identical_config(root_a, root_b, str(root_b))
        # Add a couple more in-scope files so the discovered collection has
        # more than one member per file class named in the task text.
        _write_json(root_a, "hooks.json", {"onStart": [f"{root_a}/hooks/x.sh"]})
        _write_json(
            root_a,
            "crons.json",
            {"jobs": [{"name": "x", "script": f"{root_a}/crons/x.py"}]},
        )
        _write_json(root_a, "instances.json", {"remote_bin": f"{root_a}/bin/kc"})
        _write_json(root_a, "agent_model_state.json", {"foo": {"model": "sonnet"}})

        from backend import collect, redact

        collected = collect.collect()
        redacted = redact.redact(collected)
        tokenized = push.tokenize_tree(redacted)

        json_relpaths = _collected_json_relpaths(tokenized)
        assert json_relpaths, (
            "the discovered collection of tracked JSON files must be "
            "non-empty, or this guard is testing nothing"
        )
        expected_names = {
            "config.json",
            "mcp.json",
            "hooks.json",
            "crons.json",
            "instances.json",
            "agent_model_state.json",
            "agents/foo.json",
        }
        discovered_names = set(json_relpaths)
        assert expected_names <= discovered_names, (
            f"expected tracked files missing from the discovered "
            f"collection: {expected_names - discovered_names}"
        )

        root_a_str = str(root_a)
        root_b_str = str(root_b)
        for relpath in json_relpaths:
            content_str = tokenized[relpath].decode("utf-8")
            assert root_a_str not in content_str, (
                f"{relpath} still contains the root A absolute path "
                f"after tokenize — {root_a_str!r} found in content"
            )
            assert root_b_str not in content_str, (
                f"{relpath} still contains the root B absolute path "
                f"after tokenize — {root_b_str!r} found in content"
            )


class TestMutationSkippingTokenizeTurnsGuardRed:
    """Mutation proof (testing-standards.md's Mutation Requirement): the

    "no root path in any file" guard above must be demonstrated FAILING
    against a deliberately violating input — here, a push pipeline that
    skips the tokenize step entirely (the exact defect the guard exists to
    catch). If the guard could not fail against this mutation, it would not
    actually be testing what it claims to.
    """

    def test_guard_fails_red_when_tokenize_step_is_skipped(
        self, isolated_roots: dict
    ) -> None:
        root_a = isolated_roots["root_a"]
        root_b = isolated_roots["root_b"]
        _seed_identical_config(root_a, root_b, str(root_b))

        from backend import collect, redact

        collected = collect.collect()
        redacted = redact.redact(collected)

        # Deliberate mutation: skip portable.tokenize_tree entirely and
        # treat the merely-redacted tree as if it were the pushed tree —
        # this is exactly what a regression that dropped the tokenize step
        # would produce.
        mutated_tree = redacted

        json_relpaths = _collected_json_relpaths(mutated_tree)
        root_b_str = str(root_b)
        violating_paths = [
            relpath
            for relpath in json_relpaths
            if root_b_str in mutated_tree[relpath].decode("utf-8")
        ]
        assert violating_paths, (
            "the mutation (skipping tokenize) must actually reintroduce a "
            "root path into at least one tracked JSON file, or this "
            "mutation proof is not exercising the guard it claims to"
        )
        # This is the RED assertion: with tokenize skipped, the guard's own
        # "root path absent" property fails for at least one file.
        with pytest.raises(AssertionError):
            for relpath in json_relpaths:
                assert root_b_str not in mutated_tree[relpath].decode("utf-8"), (
                    f"{relpath} contains the root path after the mutation "
                    "(expected — this proves the guard can fail)"
                )


# ---------------------------------------------------------------------------
# Requirement 2.9 — a site-packages path in a NON-agent file stays unchanged
# and is listed in PushResult.non_portable, without refusing the push.
# ---------------------------------------------------------------------------


class TestSitePackagesPathInNonAgentFileReportedNonPortable:
    def test_site_packages_path_in_mcp_json_unchanged_and_listed(
        self,
        isolated_roots: dict,
        change_path_collaborators: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root_a = isolated_roots["root_a"]
        site_packages_path = (
            "/usr/local/lib/python3.12/site-packages/kiro_crew/config/prompt.md"
        )
        _write_json(
            root_a,
            "mcp.json",
            {
                "mcpServers": {
                    "docs": {
                        "command": "python3",
                        "args": [site_packages_path],
                    }
                }
            },
        )

        from backend import state

        store = state.load_state()
        monkeypatch.setattr(store, "last_pushed_hash", "a-completely-different-hash")
        monkeypatch.setattr(state, "load_state", lambda: store, raising=False)

        result = push.run()

        assert getattr(result, "outcome", None) not in (
            "refused-secret-scan",
            "refused-branch-authorization",
        ), "an out-of-root path must never refuse the push"

        non_portable = getattr(result, "non_portable", None)
        assert non_portable is not None, (
            "PushResult must carry a non_portable field listing "
            "out-of-root values (requirements.md 2.9)"
        )
        matches = [entry for entry in non_portable if entry.get("path") == "mcp.json"]
        assert (
            matches
        ), f"expected an mcp.json entry in non_portable, got: {non_portable}"
        assert any(
            site_packages_path in json.dumps(entry) for entry in matches
        ), f"the site-packages value itself must appear in the report: {matches}"

        # The value itself must remain in the actually-pushed content,
        # unchanged, for the mcp.json file.
        from backend import collect, redact

        collected = collect.collect()
        redacted = redact.redact(collected)
        tokenized = push.tokenize_tree(redacted)
        assert site_packages_path in tokenized["mcp.json"].decode("utf-8")


# ---------------------------------------------------------------------------
# A headers/env value is already "<redacted>" when tokenize runs and is
# never rewritten — ordering: redact BEFORE tokenize. Proved with a header
# value that is ITSELF a root path.
# ---------------------------------------------------------------------------


class TestRedactRunsBeforeTokenizeForHeadersAndEnvValues:
    def test_header_value_that_is_itself_a_root_path_is_redacted_not_tokenized(
        self, isolated_roots: dict
    ) -> None:
        """A `headers` value equal to a root path is a pathological but

        legal input (e.g. a misconfigured credential value that happens to
        look like a filesystem path). If tokenize ran BEFORE redact, this
        value would be rewritten to a token form and leak the fact that a
        credential once held a root-shaped string. Because redact runs
        FIRST, the value is already the literal placeholder
        `"<redacted>"` by the time tokenize walks the tree, so it must
        remain exactly `"<redacted>"` — never a token, never the original
        root-path string.
        """
        root_a = isolated_roots["root_a"]
        root_b = isolated_roots["root_b"]
        header_value_that_is_a_root_path = str(root_b)
        _write_json(
            root_a,
            "mcp.json",
            {
                "mcpServers": {
                    "github": {
                        "command": "node",
                        "headers": {"Authorization": header_value_that_is_a_root_path},
                    }
                }
            },
        )

        from backend import collect, redact

        collected = collect.collect()
        redacted = redact.redact(collected)
        tokenized = push.tokenize_tree(redacted)

        tokenized_doc = json.loads(tokenized["mcp.json"].decode("utf-8"))
        header_value = tokenized_doc["mcpServers"]["github"]["headers"]["Authorization"]
        assert header_value == "<redacted>", (
            "a headers value must remain the literal placeholder after "
            "tokenize runs, never a token form and never the original "
            "root-path string — proving redact ran BEFORE tokenize"
        )
        assert header_value != "${KIRO_HOME}"
        assert header_value != header_value_that_is_a_root_path

    def test_env_value_that_is_itself_a_root_path_is_redacted_not_tokenized(
        self, isolated_roots: dict
    ) -> None:
        root_a = isolated_roots["root_a"]
        root_b = isolated_roots["root_b"]
        env_value_that_is_a_root_path = str(root_b)
        _write_json(
            root_a,
            "crons.json",
            {
                "jobs": [
                    {
                        "name": "x",
                        "command": "echo hi",
                        "env": {"HOME_OVERRIDE": env_value_that_is_a_root_path},
                    }
                ]
            },
        )

        from backend import collect, redact

        collected = collect.collect()
        redacted = redact.redact(collected)
        tokenized = push.tokenize_tree(redacted)

        tokenized_doc = json.loads(tokenized["crons.json"].decode("utf-8"))
        env_value = tokenized_doc["jobs"][0]["env"]["HOME_OVERRIDE"]
        assert env_value == "<redacted>"
        assert env_value != "${KIRO_HOME}"


# ---------------------------------------------------------------------------
# A non-JSON tracked file (a steering .md containing a root path) is NOT
# rewritten.
# ---------------------------------------------------------------------------


class TestNonJsonTrackedFileNotRewritten:
    def test_steering_markdown_containing_a_root_path_is_untouched(
        self, isolated_roots: dict
    ) -> None:
        root_a = isolated_roots["root_a"]
        root_path_text = f"See {root_a}/steering/other.md for details."
        _write_text(root_a, "steering/plan.md", f"# Plan\n\n{root_path_text}\n")

        from backend import collect, redact

        collected = collect.collect()
        redacted = redact.redact(collected)
        tokenized = push.tokenize_tree(redacted)

        assert (
            tokenized["steering/plan.md"] == redacted["steering/plan.md"]
        ), "a non-JSON tracked file must pass through tokenize unchanged"
        # And the root path text must still be there verbatim -- proving
        # "unchanged" means literally untouched, not merely "no crash".
        assert root_path_text.encode("utf-8") in tokenized["steering/plan.md"]


# ---------------------------------------------------------------------------
# last_pushed_hash from before the upgrade (hash of the UNTOKENIZED tree)
# takes the normal changed-push path, not an error (requirements.md 2.11).
# ---------------------------------------------------------------------------


class TestPreUpgradeLastPushedHashTakesNormalChangePath:
    def test_pre_upgrade_hash_is_treated_as_a_normal_change_not_an_error(
        self,
        isolated_roots: dict,
        change_path_collaborators: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Seed `last_pushed_hash` with the hash of the tree as it would

        have been computed BEFORE tokenization shipped (collect -> redact,
        no tokenize step) — the exact value a pre-upgrade instance would
        have recorded. The first tick after upgrade must NOT treat this as
        an error: it recomputes over the now-tokenized tree, finds a
        (generally) different hash, and runs the ordinary change path —
        never a special-cased "migration" branch, never a crash.
        """
        root_a = isolated_roots["root_a"]
        root_b = isolated_roots["root_b"]
        _seed_identical_config(root_a, root_b, str(root_b))

        from backend import collect, redact, state

        collected = collect.collect()
        redacted = redact.redact(collected)
        pre_upgrade_hash = push.tree_hash(redacted)  # no tokenize -- old gate

        store = state.load_state()
        monkeypatch.setattr(store, "last_pushed_hash", pre_upgrade_hash)
        monkeypatch.setattr(state, "load_state", lambda: store, raising=False)

        post_upgrade_hash = push.tree_hash(push.tokenize_tree(redacted))
        assert post_upgrade_hash != pre_upgrade_hash, (
            "fixture must actually exercise a hash change across the "
            "tokenize upgrade, or this test proves nothing"
        )

        result = push.run()

        assert getattr(result, "outcome", None) not in (
            None,
        ), "push.run() must return a normal PushResult, not raise or return None"
        assert getattr(result, "outcome", None) != "no-op", (
            "the pre-upgrade hash must NOT match the post-upgrade tokenized "
            "tree hash, so this must not be reported as a no-op"
        )
        assert getattr(result, "outcome", None) == "pushed", (
            f"expected the normal change path to run and succeed, got "
            f"outcome={getattr(result, 'outcome', None)!r} "
            f"reason={getattr(result, 'reason', None)!r}"
        )
        assert result.tree_hash == post_upgrade_hash
