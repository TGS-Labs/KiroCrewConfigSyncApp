"""Failing tests for tasks.md 7.4: apply.py runs portable.expand BEFORE the

requirements.md 4.10 placeholder restore (design.md apply step order: 4
expand -> 4a restore -> 4b sanitize crons/instances -> atomic write).

Covers requirements.md 4.11, 4.12, 4.13:

- 4.11: every in-scope string value equal to (or prefixed by) a
  ``${KIROCREW_HOME}``/``${KIRO_HOME}`` token is expanded to THIS host's
  own root path, keeping the scheme prefix, BEFORE the 4.10 placeholder
  restore. Never refuses a file.
- 4.12: a legacy absolute path under NEITHER of this host's roots (another
  host's home, or a product-shipped path) is written unchanged and listed
  in the apply result as non-portable (file path + JSON key path). Never
  refuses the file.
- 4.13: after expansion, a ``file://``/``skill://`` reference under one of
  this host's roots whose target does not exist locally (glob-checked) is
  listed as an unresolved reference. Never refuses the file.

INTERFACE PINNED for this task (``ApplyResult`` gains three fields; design.md
does not name them, so the narrowest shape consistent with
``PushResult``'s already-named ``non_portable`` sibling on the push side
(tasks.md 7.3) is chosen here):

    non_portable_paths: list[str]        # "<relpath>:<json.key.path>"
    unresolved_references: list[str]     # "<relpath>:<json.key.path>"
    untracked_prompt_agents: list[str]   # verbatim from
                                          # registration.Result (7.5)

``non_portable_paths``/``unresolved_references`` entries are formatted
identically to ``needs_credential``'s own existing convention in
``backend/apply.py`` (``f"{relpath}:{owner_label}.{key}.{sub_key}"`` for
placeholders) — here ``"{relpath}:{dotted.json.key.path}"`` — so a test
that wants "this file, this key" checks a single string rather than a
tuple/dict shape this task does not pin.

Real git throughout (per project lesson: mocked subprocess for git has
produced defects five times in this project). ``commit_root`` is a real
``git archive`` checkout of a real commit in a bundle-repo-shaped fixture,
matching ``test_apply.py``'s own convention exactly.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

import pytest

from backend import apply, portable, state

# ---------------------------------------------------------------------------
# Real-git fixture helpers -- copied verbatim from test_apply.py's own
# conventions so this file's fixtures behave identically.
# ---------------------------------------------------------------------------

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        timeout=120,
        stdin=subprocess.DEVNULL,
    )


def _head_sha(repo: Path) -> str:
    return _git("rev-parse", "HEAD", cwd=repo).stdout.strip()


def _commit_all(repo: Path, message: str) -> None:
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", message, cwd=repo)


def _checkout_head_into(repo: Path, dest: Path) -> None:
    """Materialize the repo's current HEAD tree into ``dest`` via a real

    ``git archive`` -- a genuine checkout of real commit content.
    """
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(
        ["git", "archive", "HEAD"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        env=_GIT_ENV,
        timeout=120,
        stdin=subprocess.DEVNULL,
    )
    tar_path = dest.parent / f"{dest.name}.tar"
    tar_path.write_bytes(archive.stdout)
    subprocess.run(
        ["tar", "-xf", str(tar_path), "-C", str(dest)],
        check=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
    )
    tar_path.unlink()


@pytest.fixture
def target_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real target filesystem root, wired as KIROCREW_HOME/KIRO_HOME --

    matches test_apply.py's own fixture exactly so apply.py resolves the
    same roots backend/collect.py does.
    """
    root_a = tmp_path / "kirocrew-home"
    root_b = tmp_path / "kiro-home"
    root_a.mkdir()
    root_b.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(root_a))
    monkeypatch.setenv("KIRO_HOME", str(root_b))
    return tmp_path


@pytest.fixture
def state_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> state.StateStore:
    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(tmp_path / "config-sync-state"))
    return state.load_state()


@pytest.fixture
def bundle_repo(tmp_path: Path) -> Path:
    """A minimal real bundle-repo-shaped commit this file's tests check out

    from -- one commit is enough; the interesting content lives in each
    test's own ``commit_root`` (built by checking that one commit out),
    matching test_apply.py's "commit_root is a real checkout" convention
    without needing test_apply.py's fuller multi-commit history, which
    this file's assertions do not exercise.
    """
    repo = tmp_path / "bundle-repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    (repo / "README.md").write_text("# bundle\n", encoding="utf-8")
    _commit_all(repo, "initial")
    return repo


def _seed_pending(store: state.StateStore, sha: str) -> None:
    store.set_pending(
        sha=sha,
        author="a",
        subject="s",
        classified_paths={},
        ignored_paths=[],
        touched_classes=[],
    )


def _commit_root_for(bundle_repo: Path, tmp_path: Path, name: str) -> Path:
    """Commit the CURRENT working tree of ``bundle_repo`` and check that

    exact commit out into a fresh ``tmp_path/<name>`` directory, returning
    the checkout. Every test builds its own file content into
    ``bundle_repo`` first, then calls this to get both a real ``sha`` and a
    real checked-out ``commit_root`` from ONE real commit.
    """
    _commit_all(bundle_repo, f"content for {name}")
    dest = tmp_path / name
    _checkout_head_into(bundle_repo, dest)
    return dest


# ---------------------------------------------------------------------------
# (1) Token expansion lands on THIS host's roots, across every in-scope
# JSON file in the commit -- iterate the discovered collection, assert per
# member no token survives (requirements.md 4.11).
# ---------------------------------------------------------------------------

_IN_SCOPE_JSON_RELPATHS: tuple = (
    "config.json",
    "hooks.json",
    "mcp.json",
    "crons.json",
    "instances.json",
    "agent_model_state.json",
)


def _write_tokenized_fixture_files(bundle_repo: Path) -> None:
    """Write one token-bearing value into every in-scope root-A JSON file

    plus one root-B ``agents/*.json`` file, so the "iterate every in-scope
    JSON file in the commit" assertion below has a real member to check
    per file, not a single hand-picked one (testing-standards.md
    § Parametrised Guard Coverage).
    """
    (bundle_repo / "config.json").write_text(
        json.dumps(
            {
                "agents": {},
                "note": "file://${KIROCREW_HOME}/steering/a.md",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (bundle_repo / "hooks.json").write_text(
        json.dumps(
            {"hooks": [{"path": "file://${KIROCREW_HOME}/hooks/on-save.py"}]},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (bundle_repo / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "github": {
                        "headers": {"Authorization": "<redacted>"},
                        "resources": ["file://${KIROCREW_HOME}/mcp/github.json"],
                    }
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (bundle_repo / "crons.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "name": "j1",
                        "command": "echo hi",
                        "script": "file://${KIROCREW_HOME}/crons/j1.py",
                    }
                ]
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (bundle_repo / "instances.json").write_text(
        json.dumps(
            {
                "instances": [
                    {
                        "name": "box",
                        "remote_bin": "file://${KIROCREW_HOME}/bin/remote",
                    }
                ]
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (bundle_repo / "agents").mkdir(exist_ok=True)
    (bundle_repo / "agents" / "x.json").write_text(
        json.dumps(
            {
                "name": "x",
                "prompt": "file://${KIROCREW_HOME}/config-bundles/agent-prompts/x.md",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    # Registration (requirements.md 5.6/5.11/5.14) is a separate concern
    # from portability -- without the referenced prompt FILE existing in
    # the commit's own tree, plus a config.json agents{} entry and an
    # agent_model_state.json pin, this commit's agents/x.json is refused
    # as an incomplete registration and never applies, which would make
    # this fixture prove nothing about token expansion at all. config.json
    # already carries its own top-level "agents" object above (used by
    # config.json's own token-bearing "note" value), so the agent entry
    # is merged into it rather than overwritten.
    (bundle_repo / "config-bundles" / "agent-prompts").mkdir(parents=True)
    (bundle_repo / "config-bundles" / "agent-prompts" / "x.md").write_text(
        "# x\n\nYou are agent x.\n", encoding="utf-8"
    )
    config_doc = json.loads((bundle_repo / "config.json").read_text(encoding="utf-8"))
    config_doc["agents"]["x"] = {"source": "local", "model": "claude-sonnet-5"}
    (bundle_repo / "config.json").write_text(
        json.dumps(config_doc, indent=2) + "\n", encoding="utf-8"
    )
    (bundle_repo / "agent_model_state.json").write_text(
        json.dumps(
            {
                "note": "file://${KIRO_HOME}/agents/x.json",
                "x": {"model_managed": False, "model": "claude-sonnet-5"},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _walk_string_values(
    node: object, path: Tuple[str, ...] = ()
) -> Iterator[Tuple[str, str]]:
    """Yield ``(dotted_key_path, value)`` for every string leaf in a parsed

    JSON document -- used to assert, PER MEMBER, that no token survived
    anywhere in the written file, rather than a single `any(...)`/`"token"
    in text` check (testing-standards.md's assertion-strength ban on
    "every" properties).
    """
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk_string_values(value, path + (str(key),))
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from _walk_string_values(item, path + (str(i),))
    elif isinstance(node, str):
        yield (".".join(path), node)


def test_expand_replaces_token_on_every_in_scope_json_file_no_survivor(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """Every in-scope JSON file committed carries a token-form value;

    after apply, NONE of them may still contain the literal token string
    anywhere in a string value -- checked per file, per string leaf
    (testing-standards.md § Parametrised Guard Coverage: "every" means
    assert per member, never `any(...)`/`len > 0`).
    """
    _write_tokenized_fixture_files(bundle_repo)
    commit_root = _commit_root_for(bundle_repo, tmp_path, "commit-root")

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={
            "A": list(_IN_SCOPE_JSON_RELPATHS),
            "B": ["agents/x.json"],
        },
        store=state_store,
    )

    assert result.outcome == "applied", result

    root_a = Path(os.environ["KIROCREW_HOME"])
    root_b = Path(os.environ["KIRO_HOME"])

    written_by_relpath = {
        relpath: json.loads((root_a / relpath).read_text(encoding="utf-8"))
        for relpath in _IN_SCOPE_JSON_RELPATHS
    }
    written_by_relpath["agents/x.json"] = json.loads(
        (root_b / "agents" / "x.json").read_text(encoding="utf-8")
    )

    # Discovered collection is non-empty and covers every member named
    # above -- fail loudly if a future edit silently drops one.
    assert set(written_by_relpath) == set(_IN_SCOPE_JSON_RELPATHS) | {"agents/x.json"}

    for relpath, document in written_by_relpath.items():
        for key_path, value in _walk_string_values(document):
            assert "${KIROCREW_HOME}" not in value, (relpath, key_path, value)
            assert "${KIRO_HOME}" not in value, (relpath, key_path, value)

    # And the expansion actually substituted THIS host's real root, not
    # merely stripped the token -- spot-check one value per distinct file
    # shape (config.json's plain value; agents/x.json's scheme-prefixed
    # prompt) to prove substitution happened, not deletion.
    assert written_by_relpath["config.json"]["note"] == (
        f"file://{root_a}/steering/a.md"
    )
    assert written_by_relpath["agents/x.json"]["prompt"] == (
        f"file://{root_a}/config-bundles/agent-prompts/x.md"
    )


def test_expand_mutation_skip_expand_step_turns_the_no_survivor_guard_red(
    bundle_repo: Path,
    target_root: Path,
    state_store: state.StateStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MUTATION NOTE (testing-standards.md § Mutation Requirement): the

    guard above ("no token survives on disk") is proven CAPABLE of
    catching a real violation by simulating the exact mutation this task
    could ship with -- an ``apply_commit`` that never calls
    ``portable.expand`` at all (its current, pre-7.4 state). Patch
    ``portable.expand`` to the identity function (this IS "skip expand":
    every caller in the module gets an expand that changes nothing) and
    confirm the SAME assertion this test's sibling relies on now FAILS --
    i.e. the written file still contains the token. If this red/green
    pair ever both pass, the guard in the sibling test is not actually
    exercising the expand step.
    """
    _write_tokenized_fixture_files(bundle_repo)
    commit_root = _commit_root_for(bundle_repo, tmp_path, "commit-root")

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    def _identity_expand(doc: object, roots: object) -> object:
        return doc

    monkeypatch.setattr(portable, "expand", _identity_expand)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["config.json"], "B": []},
        store=state_store,
    )

    assert result.outcome == "applied", result
    root_a = Path(os.environ["KIROCREW_HOME"])
    written = json.loads((root_a / "config.json").read_text(encoding="utf-8"))
    # RED: with expand mutated to a no-op, the token DID survive -- this
    # is the deliberately-violating-input proof the guard can fail.
    assert written["note"] == "file://${KIROCREW_HOME}/steering/a.md"
    assert "${KIROCREW_HOME}" in written["note"]


# ---------------------------------------------------------------------------
# (2) A legacy other-host absolute path is written UNCHANGED and listed in
# ApplyResult.non_portable_paths with file + key path, without refusing
# the file (requirements.md 4.12).
# ---------------------------------------------------------------------------


def test_legacy_other_host_path_written_unchanged_and_listed_non_portable(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    other_host_value = "file:///home/otherhost/.kiro/crew/steering/legacy.md"
    (bundle_repo / "config.json").write_text(
        json.dumps({"agents": {}, "legacy_ref": other_host_value}, indent=2) + "\n",
        encoding="utf-8",
    )
    commit_root = _commit_root_for(bundle_repo, tmp_path, "commit-root")

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["config.json"], "B": []},
        store=state_store,
    )

    assert result.outcome == "applied", result
    root_a = Path(os.environ["KIROCREW_HOME"])
    written = json.loads((root_a / "config.json").read_text(encoding="utf-8"))
    # Unchanged -- never refused, never rewritten to this host's root.
    assert written["legacy_ref"] == other_host_value

    assert result.non_portable_paths == ["config.json:legacy_ref"]
    # Never refused: the file is still in `applied`, not `not_applied`.
    assert "config.json" in result.applied
    assert "config.json" not in result.not_applied


def test_legacy_other_host_path_in_agent_definition_also_listed_non_portable(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """The same non-portable listing applies to a root-B agent definition,

    not only root-A files -- the scope is every in-scope JSON file, not
    just the root-A set.
    """
    (bundle_repo / "agents").mkdir()
    (bundle_repo / "agents" / "legacy.json").write_text(
        json.dumps(
            {
                "name": "legacy",
                "prompt": "file:///home/otherhost/.kiro/crew/"
                "config-bundles/agent-prompts/legacy.md",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    # Complete the registration (requirements.md 5.6/5.14) -- an
    # out-of-root prompt reference is NOT required (5.11(c): reported as
    # untracked, not blocking), so the config.json entry + model-state pin
    # are the only two other parts this registration needs to be COMPLETE
    # and therefore actually apply (a refused/incomplete registration
    # would never reach the non-portable-listing code path this test is
    # about).
    (bundle_repo / "config.json").write_text(
        json.dumps(
            {"agents": {"legacy": {"source": "local", "model": "claude-sonnet-5"}}},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (bundle_repo / "agent_model_state.json").write_text(
        json.dumps(
            {"legacy": {"model_managed": False, "model": "claude-sonnet-5"}},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    commit_root = _commit_root_for(bundle_repo, tmp_path, "commit-root")

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={
            "A": ["config.json", "agent_model_state.json"],
            "B": ["agents/legacy.json"],
        },
        store=state_store,
    )

    assert "legacy" not in result.incomplete_registrations, result
    root_b = Path(os.environ["KIRO_HOME"])
    written = json.loads(
        (root_b / "agents" / "legacy.json").read_text(encoding="utf-8")
    )
    assert written["prompt"] == (
        "file:///home/otherhost/.kiro/crew/config-bundles/agent-prompts/legacy.md"
    )
    assert "agents/legacy.json:prompt" in result.non_portable_paths
    assert "agents/legacy.json" in result.applied


# ---------------------------------------------------------------------------
# (3) A file:// resource under an untracked location whose target is
# absent on this host is listed in ApplyResult.unresolved_references, not
# refused (requirements.md 4.13).
# ---------------------------------------------------------------------------


def test_unresolved_reference_to_absent_skill_listed_not_refused(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """A ``file://`` reference resolving (after expansion) to one of this

    host's own roots, at a relpath that does NOT exist on disk here (a
    skill delivered by ``sync-bundles.sh``, per design.md's
    ``config-bundles/skills/**`` note -- untracked by the allowlist, but
    still a same-root reference subject to 4.13's existence check) is
    listed unresolved -- the file carrying the reference still applies.
    """
    missing_skill_ref = "skill://${KIROCREW_HOME}/config-bundles/skills/foo/SKILL.md"
    (bundle_repo / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "github": {
                        "headers": {},
                        "resources": [missing_skill_ref],
                    }
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    commit_root = _commit_root_for(bundle_repo, tmp_path, "commit-root")

    root_a = Path(os.environ["KIROCREW_HOME"])
    # Deliberately do NOT create config-bundles/skills/foo/SKILL.md --
    # this host genuinely lacks the target.
    assert not (root_a / "config-bundles" / "skills" / "foo" / "SKILL.md").exists()

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["mcp.json"], "B": []},
        store=state_store,
    )

    assert result.outcome == "applied", result
    written = json.loads((root_a / "mcp.json").read_text(encoding="utf-8"))
    # Expanded to this host's root -- the reference is rewritten from the
    # token form, per 4.11 -- but not refused.
    expected_expanded = f"skill://{root_a}/config-bundles/skills/foo/SKILL.md"
    assert written["mcpServers"]["github"]["resources"][0] == expected_expanded

    assert any(
        entry.startswith("mcp.json:") and "resources" in entry
        for entry in result.unresolved_references
    )
    assert "mcp.json" in result.applied
    assert "mcp.json" not in result.not_applied


def test_reference_whose_target_exists_locally_is_not_listed_unresolved(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """The negative case for guard (3): when the expanded target DOES

    exist on this host, it must NOT appear in `unresolved_references` --
    proves the check is a real existence probe, not an unconditional
    "every reference is unresolved" stub that would trivially satisfy the
    positive test above.
    """
    present_ref = "skill://${KIROCREW_HOME}/config-bundles/skills/present/SKILL.md"
    (bundle_repo / "mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"github": {"headers": {}, "resources": [present_ref]}}},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    commit_root = _commit_root_for(bundle_repo, tmp_path, "commit-root")

    root_a = Path(os.environ["KIROCREW_HOME"])
    present_target = root_a / "config-bundles" / "skills" / "present" / "SKILL.md"
    present_target.parent.mkdir(parents=True)
    present_target.write_text("# present\n", encoding="utf-8")

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["mcp.json"], "B": []},
        store=state_store,
    )

    assert result.outcome == "applied", result
    assert not any(
        entry.startswith("mcp.json:") for entry in result.unresolved_references
    )


# ---------------------------------------------------------------------------
# (4) Ordering: a headers value of '<redacted>' is restored from the live
# file exactly as before (expand runs before restore and never touches
# it); a crons.json command containing a token is expanded BEFORE the vet
# sees it (spy vet records the exact command string received).
# ---------------------------------------------------------------------------


def test_redacted_headers_value_untouched_by_expand_still_restored_from_live(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """The literal placeholder never starts with a token or a root path

    (``portable.resolve_reference`` never resolves it, per its own
    docstring), so expand-before-restore is observationally identical to
    restore-before-expand for a `headers`/`env` value specifically --
    this test proves that identity holds in practice: the live value is
    restored exactly as it was on the pre-7.4 code path, unaffected by
    the new expand step running first.
    """
    root_a = Path(os.environ["KIROCREW_HOME"])
    root_a.joinpath("mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"github": {"headers": {"Authorization": "Bearer LIVE"}}}},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    (bundle_repo / "mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"github": {"headers": {"Authorization": "<redacted>"}}}},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    commit_root = _commit_root_for(bundle_repo, tmp_path, "commit-root")

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["mcp.json"], "B": []},
        store=state_store,
    )

    written = json.loads(root_a.joinpath("mcp.json").read_text(encoding="utf-8"))
    assert written["mcpServers"]["github"]["headers"]["Authorization"] == (
        "Bearer LIVE"
    )
    assert result.needs_credential == []


def test_crons_command_token_is_expanded_before_the_vet_sees_it(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """A ``crons.json`` job's ``command`` string carrying a token must be

    expanded to this host's real root BEFORE ``sanitize.sanitize_crons``'s
    vet callable is invoked -- proven with a spy vet that records the
    exact command string it received, per design.md's explicit ordering
    rule ("expand is the FIRST symmetric apply step"; sanitize is step
    4b, strictly after both expand (4) and restore (4a)).

    The command's value STARTS WITH the token (requirements.md 4.11: a
    string value is only expanded when its path part -- after an optional
    scheme prefix -- equals the token or starts with ``token + "/"``, an
    anchored-at-the-start match). A command that merely contains the
    token mid-string is a distinct, deliberately-NOT-expanded case,
    covered by the sibling test below.
    """
    (bundle_repo / "crons.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "name": "j1",
                        "command": "${KIROCREW_HOME}/scripts/run.sh --flag",
                    }
                ]
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    commit_root = _commit_root_for(bundle_repo, tmp_path, "commit-root")

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    root_a = Path(os.environ["KIROCREW_HOME"])
    seen_commands: List[str] = []

    def _spy_vet(command: str) -> Optional[str]:
        seen_commands.append(command)
        return None

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["crons.json"], "B": []},
        store=state_store,
        cron_vet=_spy_vet,
    )

    expected_command = f"{root_a}/scripts/run.sh --flag"
    assert seen_commands == [expected_command], seen_commands
    assert "${KIROCREW_HOME}" not in seen_commands[0]

    written = json.loads(root_a.joinpath("crons.json").read_text(encoding="utf-8"))
    assert written["jobs"][0]["command"] == expected_command
    assert result.outcome == "applied", result


def test_crons_command_token_mid_string_is_left_unexpanded_and_unreported(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """The negative case for the guard above: a token appearing anywhere

    OTHER than the start of the value's path part is never expanded
    (requirements.md 4.11's own text: "A token appearing anywhere else
    in a value SHALL NOT be expanded"). ``portable.resolve_reference``
    never resolves such a value either (it uses the identical anchored
    match), so it is not a ``file://``/``skill://`` reference at all --
    it is neither non-portable (requirements.md 4.12 is scoped to an
    ABSOLUTE PATH outside both roots, which this is not) nor an
    unresolved reference (4.13 is scoped to a resolved reference whose
    target is absent) -- it is simply an ordinary string value the vet
    sees and applies exactly as committed, unreported either way.
    """
    mid_string_command = "echo starting; python3 ${KIROCREW_HOME}/scripts/run.py"
    (bundle_repo / "crons.json").write_text(
        json.dumps({"jobs": [{"name": "j1", "command": mid_string_command}]}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    commit_root = _commit_root_for(bundle_repo, tmp_path, "commit-root")

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    seen_commands: List[str] = []

    def _spy_vet(command: str) -> Optional[str]:
        seen_commands.append(command)
        return None

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["crons.json"], "B": []},
        store=state_store,
        cron_vet=_spy_vet,
    )

    # Unexpanded: the vet saw, and the live file now holds, the literal
    # mid-string token -- proving the anchored-match rule, not merely
    # that SOME expansion happened somewhere.
    assert seen_commands == [mid_string_command], seen_commands
    root_a = Path(os.environ["KIROCREW_HOME"])
    written = json.loads(root_a.joinpath("crons.json").read_text(encoding="utf-8"))
    assert written["jobs"][0]["command"] == mid_string_command
    assert "${KIROCREW_HOME}" in written["jobs"][0]["command"]

    # Not reported either way: a mid-string token is not a resolvable
    # reference at all under requirements.md 4.11's anchored-match rule,
    # so it is neither a non-portable path (4.12) nor an unresolved
    # reference (4.13).
    assert not any(
        entry.startswith("crons.json:") for entry in result.non_portable_paths
    )
    assert not any(
        entry.startswith("crons.json:") for entry in result.unresolved_references
    )
    assert result.outcome == "applied", result


# ---------------------------------------------------------------------------
# (5) Non-JSON files (a steering .md containing a token) are written
# byte-for-byte unchanged -- expand/tokenize is scoped to JSON documents
# only (requirements.md 2.10 / design.md's own scope statement).
# ---------------------------------------------------------------------------


def test_non_json_steering_file_with_token_text_written_byte_for_byte_unchanged(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """A steering markdown file whose BODY happens to contain the literal

    token substring (e.g. documenting the token itself) must never be
    rewritten -- `portable.expand`'s scope is parsed JSON documents only,
    never arbitrary file bytes (design.md: "A non-JSON file within that
    scope SHALL pass through unchanged").
    """
    markdown_body = (
        "# Steering doc\n\n"
        "Root A resolves via `${KIROCREW_HOME}` and root B via "
        "`${KIRO_HOME}`. Do not hardcode either.\n"
    )
    (bundle_repo / "steering").mkdir()
    (bundle_repo / "steering" / "explains-tokens.md").write_text(
        markdown_body, encoding="utf-8"
    )
    commit_root = _commit_root_for(bundle_repo, tmp_path, "commit-root")

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["steering/explains-tokens.md"], "B": []},
        store=state_store,
    )

    assert result.outcome == "applied", result
    root_a = Path(os.environ["KIROCREW_HOME"])
    written_bytes = (root_a / "steering" / "explains-tokens.md").read_bytes()
    assert written_bytes == markdown_body.encode("utf-8")
    assert "steering/explains-tokens.md" not in result.non_portable_paths
    assert not any(
        entry.startswith("steering/explains-tokens.md:")
        for entry in result.non_portable_paths
    )
    assert not any(
        entry.startswith("steering/explains-tokens.md:")
        for entry in result.unresolved_references
    )


def test_non_json_skill_md_body_with_token_text_written_byte_for_byte_unchanged(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """Same guarantee for a ``SKILL.md`` body -- exercises the OTHER

    non-JSON allowlist shape apply.py already special-cases for
    frontmatter-change detection, proving that special-casing does not
    also (accidentally) run it through JSON parsing/expansion.
    """
    skill_body = (
        '---\ntriggers: ["explains-tokens"]\n---\n'
        "# Explains tokens\n\nUses `${KIRO_HOME}` in prose, not as a path "
        "to expand.\n"
    )
    (bundle_repo / "skills" / "explains-tokens").mkdir(parents=True)
    (bundle_repo / "skills" / "explains-tokens" / "SKILL.md").write_text(
        skill_body, encoding="utf-8"
    )
    commit_root = _commit_root_for(bundle_repo, tmp_path, "commit-root")

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={"A": ["skills/explains-tokens/SKILL.md"], "B": []},
        store=state_store,
    )

    assert result.outcome == "applied", result
    root_a = Path(os.environ["KIROCREW_HOME"])
    written_bytes = (root_a / "skills" / "explains-tokens" / "SKILL.md").read_bytes()
    assert written_bytes == skill_body.encode("utf-8")


# ---------------------------------------------------------------------------
# (6) ApplyResult carries 7.5's untracked-prompt list -- pinned field name
# `untracked_prompt_agents`, read verbatim from
# registration.Result.untracked_prompt_agents.
# ---------------------------------------------------------------------------


def test_apply_result_carries_untracked_prompt_agents_from_registration(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """An agent whose ``prompt`` is a ``file://`` reference resolving to a

    root but an UNTRACKED relpath (``config-bundles/skills/**`` --
    requirements.md 5.11(c) / 1.8) must surface by name in
    ``ApplyResult.untracked_prompt_agents``, verbatim from
    ``registration.Result.untracked_prompt_agents`` -- this pins that
    ``apply_commit`` actually plumbs the field through rather than
    dropping it after calling ``registration.check_registrations``.
    """
    (bundle_repo / "agents").mkdir()
    (bundle_repo / "agents" / "untracked-prompt-agent.json").write_text(
        json.dumps(
            {
                "name": "untracked-prompt-agent",
                "prompt": (
                    "file://${KIROCREW_HOME}/config-bundles/skills/" "foo/SKILL.md"
                ),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (bundle_repo / "config.json").write_text(
        json.dumps(
            {"agents": {"untracked-prompt-agent": {"model": "claude-sonnet-5"}}},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (bundle_repo / "agent_model_state.json").write_text(
        json.dumps({"untracked-prompt-agent": {"model_managed": False}}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    commit_root = _commit_root_for(bundle_repo, tmp_path, "commit-root")

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={
            "A": ["config.json", "agent_model_state.json"],
            "B": ["agents/untracked-prompt-agent.json"],
        },
        store=state_store,
    )

    # Prompt not required (untracked location) -> registration otherwise
    # complete on its other three parts.
    assert "untracked-prompt-agent" not in result.incomplete_registrations
    assert "untracked-prompt-agent" in result.untracked_prompt_agents


def test_apply_result_untracked_prompt_agents_empty_when_none_untracked(
    bundle_repo: Path, target_root: Path, state_store: state.StateStore, tmp_path: Path
) -> None:
    """Negative case: an agent with an inline prompt (no file reference at

    all) must NOT appear in ``untracked_prompt_agents`` -- proves the
    field reflects the real per-commit registration result rather than
    being unconditionally populated.
    """
    (bundle_repo / "agents").mkdir()
    (bundle_repo / "agents" / "inline-prompt-agent.json").write_text(
        json.dumps(
            {"name": "inline-prompt-agent", "prompt": "You are a helpful agent."}
        ),
        encoding="utf-8",
    )
    (bundle_repo / "config.json").write_text(
        json.dumps(
            {"agents": {"inline-prompt-agent": {"model": "claude-sonnet-5"}}},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (bundle_repo / "agent_model_state.json").write_text(
        json.dumps({"inline-prompt-agent": {"model_managed": False}}, indent=2) + "\n",
        encoding="utf-8",
    )
    commit_root = _commit_root_for(bundle_repo, tmp_path, "commit-root")

    sha = _head_sha(bundle_repo)
    _seed_pending(state_store, sha)

    result = apply.apply_commit(
        approved_sha=sha,
        commit_root=commit_root,
        changed_paths={
            "A": ["config.json", "agent_model_state.json"],
            "B": ["agents/inline-prompt-agent.json"],
        },
        store=state_store,
    )

    assert "inline-prompt-agent" not in result.incomplete_registrations
    assert result.untracked_prompt_agents == []


# ---------------------------------------------------------------------------
# Import-failure sanity: fields must exist on ApplyResult even before any
# behaviour is exercised. Confirmed red for the right reason -- AttributeError
# on missing fields, not a pre-existing failure elsewhere.
# ---------------------------------------------------------------------------


def test_apply_result_declares_the_three_new_fields_with_list_defaults() -> None:
    result = apply.ApplyResult(outcome="applied")
    assert result.non_portable_paths == []
    assert result.unresolved_references == []
    assert result.untracked_prompt_agents == []
