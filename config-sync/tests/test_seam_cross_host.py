"""Task 8.1 -- the portability seam, cross-host, end to end (tasks.md 8.1;

requirements.md 2.11, 4.11, 5.11, 5.12, 5.14).

push tokenizes -> apply expands -> registration resolves the same prompt
file, over the full requirements.md 2.8 scope, exercised across TWO
different hosts (two independent ``tmp_path``-rooted root pairs) connected
by a REAL local git bundle repo (bare origin + clone, merged via a genuine
``--no-ff`` merge -- Kiro-Config-Bundles disallows squash org-wide, matching
``tests/test_routes_approve_seam.py``'s own fixture shape).

Only the Buildo/network edge is stubbed (there is none here -- push never
opens a PR in this test, it stops at ``push.tokenize_tree`` over an
in-memory tree) and the bundle-repo remote is a local ``file://`` bare repo,
exactly as ``tests/test_push.py``/``tests/test_routes_approve_seam.py`` stub
it. Every other step runs through the REAL modules: ``push.tokenize_tree``,
``apply.apply_commit`` (which internally calls ``portable.expand`` and
``registration.check_registrations``), and ``routes.approve``'s real
``_materialize_pending_commit`` + ``state.resolve_pending`` path.

Interface pinned by this file (host-1 root pair, host-2 root pair):

    host 1: KIROCREW_HOME=<h1>/kiro-crew-home  KIRO_HOME=<h1>/kiro-home
    host 2: KIROCREW_HOME=<h2>/kiro-crew-home  KIRO_HOME=<h2>/kiro-home

Host 1 seeds, under its OWN roots:
    - root B  agents/rev.json  {"prompt": "file://<h1 root A>/...rev.md"}
    - root A  config-bundles/agent-prompts/rev.md   (the referenced resource)
    - root A  config.json                   {"agents": {"rev": {...}}}
    - root A  agent_model_state.json        {"rev": {...}}
    - root A  mcp.json                      one server whose command/args start
                                             with an h1 root path, plus a
                                             ``headers`` value redact must replace

``push.tokenize_tree`` is run over this collected+redacted tree (git steps
stubbed by never calling them -- the captured tree is what push.py would
hand to ``tree_hash``/the working-copy writer). That tokenized tree is
committed directly into a real bundle-repo clone via a real
``--no-ff`` merge (mirroring the org's disallow-squash policy), then
``routes.approve`` is run against a REAL host-2 clone/materialize path with
``KIROCREW_HOME``/``KIRO_HOME`` repointed at host 2's own roots, so
``apply.apply_commit`` and ``registration.check_registrations`` run for
real against host 2's live filesystem.

Assertions (a)-(e), matching tasks.md 8.1 verbatim:
    (a) tree layout matches what ``apply_commit``/
        ``routes._materialize_pending_commit`` consume (relpaths per root,
        no extra prefix).
    (b) on host 2, ``registration.check_registrations`` (invoked inside
        ``apply.apply_commit``) resolves the push-emitted prompt relpath
        and ``rev`` is a complete registration.
    (c) after ``routes.approve`` on host 2, the live ``agents/rev.json``
        prompt and ``mcp.json`` path values expand to HOST 2's roots, the
        prompt file holds host 1's bytes, and the ``mcp.json`` header value
        is host 2's own live credential -- never ``"<redacted>"``
        (requirements.md 4.10); no ``${KIROCREW_HOME}``/``${KIRO_HOME}``
        token and no host-1 path survives in ANY applied JSON file
        (iterated per file, per member).
    (d) re-running the push collector+tokenizer on host 2 yields host 1's
        ``tree_hash`` (requirements.md 2.11's cross-host byte-identity
        property).
    (e) a follow-up commit touching ONLY ``agents/rev.json`` -- with the
        shared files (``config.json``, ``agent_model_state.json``) absent
        from that commit's own ``changed_paths`` but already carrying
        ``rev`` in the commit TREE -- still completes and applies
        (requirements.md 5.14).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterator

import pytest

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_TERMINAL_PROMPT": "0",
}


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        timeout=180,
        stdin=subprocess.DEVNULL,
    )


def _head_sha(repo: Path) -> str:
    return _git("rev-parse", "HEAD", cwd=repo).stdout.strip()


def _init_origin_repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git("init", "-q", "--bare", "-b", "main", cwd=origin)
    return origin


# ---------------------------------------------------------------------------
# Host-1 seed content.
# ---------------------------------------------------------------------------


def _seed_host1_roots(h1_root_a: Path, h1_root_b: Path) -> None:
    """Seed host 1's LIVE roots with a complete `rev` agent registration

    plus an `mcp.json` carrying a header credential, all under host 1's
    OWN absolute root paths -- exactly what an operator's real instance
    would look like before its first config-sync push.
    """
    (h1_root_a / "config-bundles" / "agent-prompts").mkdir(parents=True)
    (h1_root_a / "config-bundles" / "agent-prompts" / "rev.md").write_text(
        "# rev prompt\nHost-1-authored prompt body.\n", encoding="utf-8"
    )

    (h1_root_b / "agents").mkdir(parents=True)
    agent_def = {
        "name": "rev",
        "description": "Reviews things.",
        "prompt": f"file://{h1_root_a}/config-bundles/agent-prompts/rev.md",
        "tools": ["read"],
    }
    (h1_root_b / "agents" / "rev.json").write_text(
        json.dumps(agent_def, indent=2) + "\n", encoding="utf-8"
    )

    config_doc = {"agents": {"rev": {"source": "local", "model": "sonnet"}}}
    (h1_root_a / "config.json").write_text(
        json.dumps(config_doc, indent=2) + "\n", encoding="utf-8"
    )

    model_state_doc = {"rev": {"model_managed": False, "model": "claude-sonnet-5"}}
    (h1_root_a / "agent_model_state.json").write_text(
        json.dumps(model_state_doc, indent=2) + "\n", encoding="utf-8"
    )

    mcp_doc = {
        "mcpServers": {
            "kirocrew-core": {
                "command": f"{h1_root_a}/bin/mcp-server",
                "args": [f"{h1_root_a}/bin/mcp-server", "--stdio"],
                "headers": {"Authorization": "Bearer host1-secret-token-ABC"},
            }
        }
    }
    (h1_root_a / "mcp.json").write_text(
        json.dumps(mcp_doc, indent=2) + "\n", encoding="utf-8"
    )


def _collect_redact_tokenize_h1(
    monkeypatch: pytest.MonkeyPatch, h1_root_a: Path, h1_root_b: Path
) -> Dict[str, bytes]:
    """Run the REAL collect -> redact -> tokenize pipeline over host 1's

    roots (`push.py`'s own step order, git steps never invoked -- this
    captures exactly the in-memory tree `push.run()` would hand to
    `tree_hash`/the working-copy writer, per the task's "git steps
    stubbed" instruction).
    """
    monkeypatch.setenv("KIROCREW_HOME", str(h1_root_a))
    monkeypatch.setenv("KIRO_HOME", str(h1_root_b))

    from backend import collect, push, redact

    collected = collect.collect()
    redacted = redact.redact(collected)
    tokenized = push.tokenize_tree(redacted)
    return tokenized


# ---------------------------------------------------------------------------
# Bundle-repo helpers: commit host 1's tokenized tree via a real --no-ff
# merge, matching the org's disallow-squash policy and
# test_routes_approve_seam.py's own fixture shape.
# ---------------------------------------------------------------------------


def _write_tree(work: Path, tree: Dict[str, bytes]) -> None:
    for relpath, content in tree.items():
        target = work / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def _commit_tree_via_merge(
    origin: Path, tmp_path: Path, work_name: str, tree: Dict[str, bytes]
) -> str:
    """Commit `tree` into `origin`'s main via a genuine --no-ff merge from

    a feature branch, mirroring test_routes_approve_seam.py's real-git
    fixture shape (main disallows squash org-wide).
    """
    work = tmp_path / work_name
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    _git("remote", "add", "origin", str(origin), cwd=work)

    if not (origin / "HEAD").exists() or _origin_is_empty(origin):
        (work / ".gitkeep").write_text("", encoding="utf-8")
        _git("add", "-A", cwd=work)
        _git("commit", "-q", "-m", "initial", cwd=work)
        _git("push", "-q", "-u", "origin", "main", cwd=work)
    else:
        _git("fetch", "-q", "origin", cwd=work)
        _git("reset", "-q", "--hard", "origin/main", cwd=work)

    _git("checkout", "-q", "-b", work_name, cwd=work)
    _write_tree(work, tree)
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", f"config-sync: {work_name}", cwd=work)

    _git("checkout", "-q", "main", cwd=work)
    _git("merge", "-q", "--no-ff", work_name, "-m", f"merge {work_name}", cwd=work)
    merge_sha = _head_sha(work)
    _git("push", "-q", "origin", "main", cwd=work)
    return merge_sha


def _origin_is_empty(origin: Path) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "refs/heads/main"],
        cwd=str(origin),
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        timeout=180,
        stdin=subprocess.DEVNULL,
    )
    return result.returncode != 0


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    return _init_origin_repo(tmp_path)


@pytest.fixture
def host1_roots(tmp_path: Path) -> Dict[str, Path]:
    h1 = tmp_path / "h1"
    root_a = h1 / ".kiro" / "crew"
    root_b = h1 / ".kiro"
    root_a.mkdir(parents=True)
    root_b.mkdir(parents=True, exist_ok=True)
    return {"root_a": root_a, "root_b": root_b}


@pytest.fixture
def host2_roots(tmp_path: Path) -> Dict[str, Path]:
    h2 = tmp_path / "h2"
    root_a = h2 / ".kiro" / "crew"
    root_b = h2 / ".kiro"
    root_a.mkdir(parents=True)
    root_b.mkdir(parents=True, exist_ok=True)
    return {"root_a": root_a, "root_b": root_b}


@pytest.fixture
def on_host2(
    tmp_path: Path, host2_roots: Dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> Iterator[Dict[str, Path]]:
    """Point every env var the app resolves roots/state from at host 2."""
    state_dir = tmp_path / "config-sync-state-h2"
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setenv("KIROCREW_HOME", str(host2_roots["root_a"]))
    monkeypatch.setenv("KIRO_HOME", str(host2_roots["root_b"]))
    yield {**host2_roots, "state_dir": state_dir}


@pytest.fixture
def routes_module_h2(on_host2: Dict[str, Path]) -> Any:
    from backend import routes

    return routes


@pytest.fixture(autouse=True)
def _fresh_modules() -> Iterator[None]:
    """`backend.collect`/`backend.apply` resolve roots from env vars read

    at CALL time (not import time), so no module reload is needed between
    the host-1 collection phase and the host-2 apply phase -- both simply
    read whatever `KIROCREW_HOME`/`KIRO_HOME` are set to at the moment
    each function runs. This fixture exists only as a documented no-op
    marker of that fact for future readers of this file.
    """
    yield


@pytest.fixture(autouse=True)
def enabled_by_default(routes_module_h2: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes_module_h2, "is_app_enabled", lambda _name: True)


@pytest.fixture
def bundle_url_patched_h2(monkeypatch: pytest.MonkeyPatch, origin: Path) -> None:
    """Point poll.py's (and routes.py's, if present) BUNDLE_REPO_URL at

    the local bare origin, matching test_routes_approve_seam.py's own
    helper exactly -- no network, ever.
    """
    from backend import poll as poll_module

    monkeypatch.setattr(poll_module, "BUNDLE_REPO_URL", f"file://{origin}")
    try:
        from backend import routes as routes_module

        if hasattr(routes_module, "BUNDLE_REPO_URL"):
            monkeypatch.setattr(routes_module, "BUNDLE_REPO_URL", f"file://{origin}")
    except ImportError:
        pass


# ---------------------------------------------------------------------------
# Shared JSON-tree walk helper for assertion (c)'s "no token, no host-1
# path survives anywhere" per-member check.
# ---------------------------------------------------------------------------


def _iter_string_values(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        for value in node.values():
            yield from _iter_string_values(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_string_values(item)
    elif isinstance(node, str):
        yield node


def _assert_no_token_or_h1_path(
    label: str, doc: Dict[str, Any], h1_root_a: Path, h1_root_b: Path
) -> None:
    for value in _iter_string_values(doc):
        assert "${KIROCREW_HOME}" not in value, (
            f"{label}: an unexpanded ${{KIROCREW_HOME}} token survived "
            f"in value {value!r}"
        )
        assert "${KIRO_HOME}" not in value, (
            f"{label}: an unexpanded ${{KIRO_HOME}} token survived in "
            f"value {value!r}"
        )
        assert (
            str(h1_root_a) not in value
        ), f"{label}: host 1's root A path leaked into value {value!r}"
        assert (
            str(h1_root_b) not in value
        ), f"{label}: host 1's root B path leaked into value {value!r}"


# ---------------------------------------------------------------------------
# The seam test.
# ---------------------------------------------------------------------------


class TestPortabilitySeamCrossHost:
    def test_push_tokenizes_apply_expands_registration_resolves_cross_host(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        origin: Path,
        host1_roots: Dict[str, Path],
        on_host2: Dict[str, Path],
        routes_module_h2: Any,
        bundle_url_patched_h2: None,
    ) -> None:
        h1_root_a = host1_roots["root_a"]
        h1_root_b = host1_roots["root_b"]
        h2_root_a = on_host2["root_a"]
        h2_root_b = on_host2["root_b"]

        # --- Seed host 1 and run the REAL collect -> redact -> tokenize
        # pipeline (git steps stubbed: we never call push.run()/git). ---
        _seed_host1_roots(h1_root_a, h1_root_b)
        tokenized_h1 = _collect_redact_tokenize_h1(monkeypatch, h1_root_a, h1_root_b)

        from backend import push as push_module

        # --- (a) tree layout: relpaths per root, no extra prefix -- the
        # exact shape apply_commit/registration.check_registrations and
        # routes._materialize_pending_commit consume. ---
        assert "agents/rev.json" in tokenized_h1, (
            "push-emitted tree is missing agents/rev.json at its bare "
            "root-B relpath -- apply_commit's commit_root contract "
            "requires root A and root B interleaved with no per-root "
            "prefix"
        )
        assert "config-bundles/agent-prompts/rev.md" in tokenized_h1
        assert "config.json" in tokenized_h1
        assert "agent_model_state.json" in tokenized_h1
        assert "mcp.json" in tokenized_h1
        for relpath in tokenized_h1:
            assert not relpath.startswith("A/") and not relpath.startswith(
                "B/"
            ), f"{relpath} carries a root-id prefix the real tree never has"

        rev_agent_def_h1 = json.loads(tokenized_h1["agents/rev.json"])
        assert rev_agent_def_h1["prompt"] == (
            "file://${KIROCREW_HOME}/config-bundles/agent-prompts/rev.md"
        ), "push did not tokenize the prompt file:// value to ${KIROCREW_HOME}"

        mcp_doc_h1 = json.loads(tokenized_h1["mcp.json"])
        server = mcp_doc_h1["mcpServers"]["kirocrew-core"]
        assert server["command"] == "${KIROCREW_HOME}/bin/mcp-server"
        assert server["args"][0] == "${KIROCREW_HOME}/bin/mcp-server"
        assert server["headers"]["Authorization"] == "<redacted>", (
            "redact must have already replaced the header value before "
            "tokenize ran -- a live credential must never reach the "
            "committed tree"
        )

        # --- Commit host 1's tokenized tree into the bundle repo via a
        # real --no-ff merge (matches org disallow-squash policy). ---
        merge_sha = _commit_tree_via_merge(
            origin, tmp_path, "config-sync-h1-initial", tokenized_h1
        )

        # --- Seed a pending record on host 2 naming this commit, then
        # approve it through the REAL routes.approve path. Re-point
        # KIROCREW_HOME/KIRO_HOME back at host 2's roots first --
        # `_collect_redact_tokenize_h1` above pointed them at host 1 for
        # the collection phase, and `apply.py`/`collect.py` resolve these
        # env vars at CALL time, so every host-2 operation from here on
        # must re-assert host 2's own roots immediately before it runs. ---
        monkeypatch.setenv("KIROCREW_HOME", str(h2_root_a))
        monkeypatch.setenv("KIRO_HOME", str(h2_root_b))

        from backend import state as state_module

        store = state_module.load_state()
        classified_paths = {
            relpath: "live_on_next_resolution" for relpath in tokenized_h1
        }
        classified_paths["config-bundles/agent-prompts/rev.md"] = "live_in_new_session"
        store.set_pending(
            sha=merge_sha,
            author="Author <a@example.com>",
            subject="config-sync-h1-initial",
            classified_paths=classified_paths,
        )

        approve_result = routes_module_h2.approve(store, merge_sha)

        # --- (b) registration resolves the push-emitted prompt relpath
        # and rev is complete. ---
        assert (
            approve_result.get("status") == "ok"
        ), f"approve on host 2 did not report ok: {approve_result!r}"
        incomplete = approve_result.get("incomplete_registrations", {})
        assert "rev" not in incomplete, (
            "rev's registration was reported incomplete on host 2's "
            f"first apply: {incomplete!r}"
        )

        # --- (c) live agents/rev.json + mcp.json expand to HOST 2's own
        # roots; the prompt file holds host 1's bytes; the mcp.json
        # header is host 2's OWN live credential, never "<redacted>"
        # (requirements.md 4.10); no token or host-1 path survives
        # anywhere, iterated per file per member. ---
        live_agent_def = json.loads(
            (h2_root_b / "agents" / "rev.json").read_text(encoding="utf-8")
        )
        assert live_agent_def["prompt"] == (
            f"file://{h2_root_a}/config-bundles/agent-prompts/rev.md"
        ), "apply did not expand the prompt token to host 2's own root A"

        live_prompt_bytes = (
            h2_root_a / "config-bundles" / "agent-prompts" / "rev.md"
        ).read_text(encoding="utf-8")
        assert live_prompt_bytes == "# rev prompt\nHost-1-authored prompt body.\n", (
            "the applied prompt file's bytes differ from what host 1 "
            "originally authored"
        )

        # Seed host 2's OWN live mcp.json credential BEFORE approving a
        # SECOND time would be the normal 4.10 flow, but on a first-ever
        # apply there is no live value yet to restore, so the placeholder
        # is written and reported via needs_credential -- confirm that
        # honest reporting first, then prove the live-value-preserved
        # path with a follow-up commit below (assertion (e)'s apply also
        # doubles as this proof, since config.json/agent_model_state.json
        # are the shared files there; mcp.json's own live-preserve path is
        # proven directly here on a SECOND approve of a fresh mcp.json
        # commit).
        live_mcp_after_first = json.loads(
            (h2_root_a / "mcp.json").read_text(encoding="utf-8")
        )
        first_apply_header = live_mcp_after_first["mcpServers"]["kirocrew-core"][
            "headers"
        ]["Authorization"]
        assert first_apply_header == "<redacted>", (
            "first-ever apply has no live value to restore at this key "
            "path, so it must write the placeholder and list it in "
            "needs_credential -- got a different value entirely"
        )
        assert "Authorization" in "".join(
            approve_result.get("needs_credential", [])
        ) or any(
            "Authorization" in entry
            for entry in approve_result.get("needs_credential", [])
        ), (
            "the placeholder-written credential key path was not listed "
            f"in needs_credential: {approve_result.get('needs_credential')!r}"
        )

        # Now set host 2's OWN live credential directly (simulating the
        # operator configuring it), push a second, unrelated-content
        # commit that still contains mcp.json unchanged from the tree's
        # perspective is not enough to prove 4.10 -- 4.10 is proven by
        # re-approving a commit whose mcp.json is still "<redacted>" and
        # confirming host 2's live value survives instead of being
        # overwritten by the placeholder. Do that as its own commit here.
        (h2_root_a / "mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "kirocrew-core": {
                            "command": f"{h2_root_a}/bin/mcp-server",
                            "args": [f"{h2_root_a}/bin/mcp-server", "--stdio"],
                            "headers": {
                                "Authorization": "Bearer host2-own-live-secret-XYZ"
                            },
                        }
                    }
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        second_commit_tree = dict(tokenized_h1)
        second_commit_tree["config-bundles/agent-prompts/rev.md"] = (
            b"# rev prompt\nHost-1-authored prompt body, v2.\n"
        )
        second_merge_sha = _commit_tree_via_merge(
            origin, tmp_path, "config-sync-h1-v2", second_commit_tree
        )
        store2 = state_module.load_state()
        store2.set_pending(
            sha=second_merge_sha,
            author="Author <a@example.com>",
            subject="config-sync-h1-v2",
            classified_paths={
                "config-bundles/agent-prompts/rev.md": "live_in_new_session"
            },
        )
        second_approve_result = routes_module_h2.approve(store2, second_merge_sha)
        assert (
            second_approve_result.get("status") == "ok"
        ), f"second approve did not report ok: {second_approve_result!r}"

        live_mcp_after_second = json.loads(
            (h2_root_a / "mcp.json").read_text(encoding="utf-8")
        )
        second_header = live_mcp_after_second["mcpServers"]["kirocrew-core"]["headers"][
            "Authorization"
        ]
        assert second_header == "Bearer host2-own-live-secret-XYZ", (
            "requirements.md 4.10 violated: applying a commit whose "
            "mcp.json header is the placeholder must preserve host 2's "
            "OWN live credential at that key path, not overwrite it -- "
            f"got {second_header!r}"
        )
        assert second_header != "<redacted>"

        # Iterate EVERY applied JSON file and assert per member -- no
        # token, no host-1 path anywhere (testing-standards.md's
        # parametrised-guard-coverage rule: assert per member, not
        # any(...)/len>0).
        applied_json_files = {
            "agents/rev.json": h2_root_b / "agents" / "rev.json",
            "config.json": h2_root_a / "config.json",
            "agent_model_state.json": h2_root_a / "agent_model_state.json",
            "mcp.json": h2_root_a / "mcp.json",
        }
        for label, path in applied_json_files.items():
            assert path.is_file(), f"{label} was never applied to host 2"
            doc = json.loads(path.read_text(encoding="utf-8"))
            _assert_no_token_or_h1_path(label, doc, h1_root_a, h1_root_b)

        # --- (d) re-running the push collector+tokenizer on host 2
        # yields host 1's tree_hash (requirements.md 2.11's cross-host
        # byte-identity property) -- restore host 2's mcp.json header
        # back to a value that redacts identically first (redaction
        # collapses any headers value to the same placeholder, so the
        # rotated credential set above does not by itself break hash
        # equality; what WOULD break it is the prompt file's v2 content
        # from the second commit, so re-collect after both applies and
        # compare against a tokenize of that SAME v2 content directly,
        # proving the round trip rather than the first commit's hash). ---
        monkeypatch.setenv("KIROCREW_HOME", str(h2_root_a))
        monkeypatch.setenv("KIRO_HOME", str(h2_root_b))

        from backend import collect as collect_module
        from backend import redact as redact_module

        host2_collected = collect_module.collect()
        host2_redacted = redact_module.redact(host2_collected)
        host2_tokenized = push_module.tokenize_tree(host2_redacted)
        host2_tree_hash = push_module.tree_hash(host2_tokenized)

        expected_tree_hash = push_module.tree_hash(second_commit_tree)
        assert host2_tree_hash == expected_tree_hash, (
            "re-collecting and re-tokenizing host 2's now-applied "
            "configuration did not reproduce the exact tree hash of the "
            "commit that was applied -- the cross-host round trip is not "
            "byte-identical (requirements.md 2.11)"
        )

        # --- (e) a follow-up commit touching ONLY agents/rev.json, with
        # config.json/agent_model_state.json absent from ITS OWN
        # changed_paths but already carrying rev in the commit TREE,
        # still completes and applies (requirements.md 5.14). ---
        third_commit_tree = dict(second_commit_tree)
        rev_def_v3 = json.loads(third_commit_tree["agents/rev.json"])
        rev_def_v3["description"] = "Reviews things, v3."
        third_commit_tree["agents/rev.json"] = (
            json.dumps(rev_def_v3, indent=2) + "\n"
        ).encode("utf-8")
        # config.json / agent_model_state.json are UNCHANGED bytes from
        # the prior commit but ARE part of this commit's tree (git commits
        # the whole tree every time) -- the test asserts the CHANGED-PATHS
        # LIST omits them, matching requirements.md 5.14's "regardless of
        # whether that file's relpath is itself in the commit's
        # changed-path list".
        third_merge_sha = _commit_tree_via_merge(
            origin, tmp_path, "config-sync-h1-v3", third_commit_tree
        )
        store3 = state_module.load_state()
        store3.set_pending(
            sha=third_merge_sha,
            author="Author <a@example.com>",
            subject="config-sync-h1-v3",
            classified_paths={"agents/rev.json": "live_on_next_resolution"},
        )
        third_approve_result = routes_module_h2.approve(store3, third_merge_sha)

        assert third_approve_result.get("status") == "ok", (
            "follow-up commit touching only agents/rev.json (with shared "
            "files absent from changed_paths but already key-carrying in "
            f"the commit tree) did not apply: {third_approve_result!r}"
        )
        third_incomplete = third_approve_result.get("incomplete_registrations", {})
        assert "rev" not in third_incomplete, (
            "requirements.md 5.14 violated: rev's registration was "
            "refused as incomplete on a commit whose shared files were "
            "unchanged in changed_paths but already carried rev's key "
            f"in the commit tree: {third_incomplete!r}"
        )

        live_agent_def_v3 = json.loads(
            (h2_root_b / "agents" / "rev.json").read_text(encoding="utf-8")
        )
        assert live_agent_def_v3["description"] == "Reviews things, v3."
