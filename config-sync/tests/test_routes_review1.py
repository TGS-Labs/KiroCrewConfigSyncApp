"""FAILING tests for senior-review findings H5, H6, M1, M2, H9, and a Low

against ``backend/routes.py`` (branch ``feature/config-sync-apply``, HEAD
``f2c6aca``). Every test in this file is RED against the current
``backend/routes.py`` for the reason stated in its own docstring; none of
them edit ``backend/`` per this task's own instruction.

## Findings covered

- **H5**: ``approve``'s response (``_apply_result_to_dict``) drops
  ``ApplyResult.non_portable_paths``, ``unresolved_references``, and
  ``untracked_prompt_agents`` (requirements.md 4.12, 4.13, 5.11(c)); and
  registration's Requirement 5.7 propagation report
  (``registration.Result.propagation_report``) never reaches
  ``ApplyResult``/the response at all (requirements.md 5.7). ``push_now``
  drops ``PushResult.non_portable`` (requirements.md 2.9).
- **H6**: ``restore`` silently ``continue``s on ``OSError`` at both the
  restored-file copy step (~568) and the created-file removal step (~581)
  and still returns ``status: "ok"`` — requirements.md 4.7's exact-restore
  guarantee is violated with no signal to the operator.
- **M1**: ``shutil.unpack_archive`` in ``_materialize_pending_commit`` runs
  with no extraction filter, which on Python 3.12+ raises a
  ``DeprecationWarning`` (the future default becomes ``data``, which
  refuses absolute-path/``..``-escaping members) — this app must already
  reject a malicious tar member, not rely on a future Python default.
- **M2**: ``_materialize_pending_commit``'s deletion detection (``not
  (commit_root / relpath).exists()`` at ~171) uses ``Path.exists()``, which
  follows symlinks — a dangling symlink at a tracked relpath in the
  extracted tree reads as "does not exist" and is misclassified as a
  deletion, causing ``apply_commit`` to remove the corresponding LIVE file
  even though the approved commit did not actually delete it.
- **H9**: the orchestrator's own call sequence in
  ``_materialize_pending_commit`` — ``_ensure_bundle_clone(clone_dir)``
  (which internally takes ``_clone_lock`` itself, non-reentrant) followed
  by a SECOND, separate ``with _clone_lock(clone_dir):`` block around the
  archive step — must not deadlock a real (file-based, cross-process)
  clone lock, and running ``approve`` twice with identical inputs must be
  safe (the second call finds nothing pending and refuses cleanly).
- **Low**: when materialization fails, the temporary ``commit_root`` it
  created must be removed (never left behind under the state directory),
  and every root's ``ignored_paths``/``not_applied`` entries a test can
  observe carry the affected root id, per ``ApplyResult``'s own
  ``ignored_paths``/``not_applied`` contract.

## Method note

Every test drives ``routes.approve``/``routes.push_now``/``routes.restore``
through a REAL git history built the same way
``tests/test_routes_approve_seam.py`` builds its own fixtures (a bare
"origin", commits pushed from a scratch working clone, a genuine
``--no-ff`` merge where a merge is needed) — reusing that file's fixture
shapes (``_init_origin_repo``/``_seed_history``-equivalent helpers,
``isolated_env``, ``routes_module``, ``enabled_by_default``) rather than
mocking ``apply_commit`` or git itself, except at the ONE deliberately
narrow point each finding's own mutation targets (documented per test).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Iterator

import pytest

from backend import state


# ---------------------------------------------------------------------------
# Real-git fixture helpers (mirrors test_routes_approve_seam.py).
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
    )


def _head_sha(repo: Path) -> str:
    return _git("rev-parse", "HEAD", cwd=repo).stdout.strip()


def _init_origin_repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git("init", "-q", "--bare", "-b", "main", cwd=origin)
    return origin


def _seed_simple_commit(
    origin: Path, tmp_path: Path, *, relpath: str, content: str, message: str
) -> str:
    """Push one commit adding/overwriting ``relpath`` at ``origin/main``.

    Used for tests that only need a single tracked-path change, not the
    full merge-history shape ``test_routes_approve_seam.py`` builds.
    """
    work = tmp_path / f"seed-work-{abs(hash((relpath, message)))}"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    _git("remote", "add", "origin", str(origin), cwd=work)

    target = work / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", message, cwd=work)
    sha = _head_sha(work)
    _git("push", "-q", "-u", "origin", "main", cwd=work)
    return sha


# ---------------------------------------------------------------------------
# App-state isolation (matches test_routes_approve_seam.py / test_routes.py).
# ---------------------------------------------------------------------------


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    return _init_origin_repo(tmp_path)


@pytest.fixture
def isolated_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Path]]:
    state_dir = tmp_path / "config-sync-state"
    root_a = tmp_path / "kiro-crew-home"
    root_b = tmp_path / "kiro-home"
    root_a.mkdir(parents=True, exist_ok=True)
    root_b.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("CONFIG_SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setenv("KIROCREW_HOME", str(root_a))
    monkeypatch.setenv("KIRO_HOME", str(root_b))

    yield {"state_dir": state_dir, "root_a": root_a, "root_b": root_b}


@pytest.fixture
def routes_module(isolated_env: dict[str, Path]) -> Any:
    from backend import routes

    return routes


@pytest.fixture(autouse=True)
def enabled_by_default(routes_module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes_module, "is_app_enabled", lambda _name: True)


@pytest.fixture
def store(isolated_env: dict[str, Path]) -> state.StateStore:
    return state.load_state()


def _bundle_repo_url_env(monkeypatch: pytest.MonkeyPatch, origin: Path) -> None:
    """Point the bundle-repo URL constant at the local ``origin`` bare repo

    on every module that might reuse it, mirroring
    ``test_routes_approve_seam.py``'s own helper.
    """
    from backend import poll as poll_module

    monkeypatch.setattr(poll_module, "BUNDLE_REPO_URL", f"file://{origin}")
    try:
        from backend import routes as routes_module

        if hasattr(routes_module, "BUNDLE_REPO_URL"):
            monkeypatch.setattr(routes_module, "BUNDLE_REPO_URL", f"file://{origin}")
    except ImportError:
        pass


@pytest.fixture
def bundle_url_patched(monkeypatch: pytest.MonkeyPatch, origin: Path) -> None:
    _bundle_repo_url_env(monkeypatch, origin)


def _seed_pending(
    store: state.StateStore,
    *,
    sha: str,
    classified_paths: dict[str, str],
    author: str = "Author <a@example.com>",
    subject: str = "test commit",
) -> None:
    store.set_pending(
        sha=sha,
        author=author,
        subject=subject,
        classified_paths=classified_paths,
    )


# ===========================================================================
# H5: approve()'s response drops ApplyResult.non_portable_paths,
# unresolved_references, untracked_prompt_agents, and the Req 5.7
# propagation_report; push_now() drops PushResult.non_portable.
# ===========================================================================


class TestH5ApproveResponseCarriesEveryApplyResultField:
    def test_approve_response_includes_non_portable_paths(
        self,
        routes_module: Any,
        store: state.StateStore,
        origin: Path,
        tmp_path: Path,
        isolated_env: dict[str, Path],
        bundle_url_patched: None,
    ) -> None:
        """A config.json value that is an absolute path under NEITHER

        configured root (requirements.md 2.9/4.12 — e.g. a site-packages
        path) must surface in the approve response's
        ``non_portable_paths``. Through a REAL approve on a real-git
        commit carrying the condition: `ApplyResult.non_portable_paths`
        is populated by `apply.py` (verified independently by
        `tests/test_apply.py`), but `routes._apply_result_to_dict`
        (routes.py's `_apply_result_to_dict`) has no
        ``"non_portable_paths"`` key in its returned dict at all -- this
        assertion is RED against the current routes.py regardless of
        whether apply.py populated the field correctly, because the key
        is simply absent from the response mapping.
        """
        non_portable_value = "/usr/local/lib/python3.12/site-packages/x.md"
        sha = _seed_simple_commit(
            origin,
            tmp_path,
            relpath="config.json",
            content=json.dumps(
                {"agents": {}, "some_path": f"file://{non_portable_value}"}
            ),
            message="add non-portable path value",
        )
        _seed_pending(
            store,
            sha=sha,
            classified_paths={"config.json": "live_after_cache_invalidation"},
        )

        result = routes_module.approve(store, sha)

        assert "non_portable_paths" in result, (
            "approve()'s response dropped ApplyResult.non_portable_paths "
            "entirely (H5) -- an operator has no way to see a value that "
            "the apply reported as non-portable"
        )
        assert any(
            "config.json" in entry for entry in result["non_portable_paths"]
        ), "the specific non-portable config.json entry was not carried through"

    def test_approve_response_includes_unresolved_references(
        self,
        routes_module: Any,
        store: state.StateStore,
        origin: Path,
        tmp_path: Path,
        isolated_env: dict[str, Path],
        bundle_url_patched: None,
    ) -> None:
        """A `file://${KIROCREW_HOME}/...` reference that expands to this

        host's own root but whose target does not exist on this host
        (requirements.md 4.13) must surface in ``unresolved_references``.
        Red against current routes.py: the key is absent from
        `_apply_result_to_dict`'s returned mapping.
        """
        sha = _seed_simple_commit(
            origin,
            tmp_path,
            relpath="config.json",
            content=json.dumps(
                {
                    "agents": {},
                    "some_ref": ("file://${KIROCREW_HOME}/steering/does-not-exist.md"),
                }
            ),
            message="add unresolved reference",
        )
        _seed_pending(
            store,
            sha=sha,
            classified_paths={"config.json": "live_after_cache_invalidation"},
        )

        result = routes_module.approve(store, sha)

        assert "unresolved_references" in result, (
            "approve()'s response dropped ApplyResult.unresolved_references "
            "entirely (H5) -- an operator has no way to see a reference "
            "that resolves to their own root but does not exist locally"
        )
        assert any("config.json" in entry for entry in result["unresolved_references"])

    def test_approve_response_includes_untracked_prompt_agents(
        self,
        routes_module: Any,
        store: state.StateStore,
        origin: Path,
        tmp_path: Path,
        isolated_env: dict[str, Path],
        bundle_url_patched: None,
    ) -> None:
        """An agent whose ``prompt`` resolves to an untracked location

        (requirements.md 5.11(c)) is named in
        ``registration.Result.untracked_prompt_agents`` ->
        ``ApplyResult.untracked_prompt_agents``. Red against current
        routes.py: the key never appears in the response dict.
        """
        work = tmp_path / "untracked-prompt-work"
        work.mkdir()
        _git("init", "-q", "-b", "main", cwd=work)
        _git("remote", "add", "origin", str(origin), cwd=work)
        (work / "agents").mkdir()
        (work / "agents" / "w.json").write_text(
            json.dumps(
                {
                    "name": "w",
                    "description": "Agent w.",
                    "prompt": (
                        "file:///usr/local/lib/python3.12/site-packages/"
                        "kiro_crew/config/prompt.md"
                    ),
                    "tools": ["read"],
                    "allowedTools": ["read"],
                    "resources": [],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        (work / "config.json").write_text(
            json.dumps({"agents": {"w": {"source": "local"}}}), encoding="utf-8"
        )
        (work / "agent_model_state.json").write_text(
            json.dumps({"w": {"model_managed": False}}), encoding="utf-8"
        )
        _git("add", "-A", cwd=work)
        _git("commit", "-q", "-m", "register agent w with untracked prompt", cwd=work)
        sha = _head_sha(work)
        _git("push", "-q", "-u", "origin", "main", cwd=work)

        _seed_pending(
            store,
            sha=sha,
            classified_paths={
                "agents/w.json": "live_on_next_resolution",
                "config.json": "live_after_cache_invalidation",
                "agent_model_state.json": "live_after_cache_invalidation",
            },
        )

        result = routes_module.approve(store, sha)

        assert "untracked_prompt_agents" in result, (
            "approve()'s response dropped ApplyResult.untracked_prompt_agents "
            "entirely (H5) -- an operator is never told agent w's prompt "
            "references an untracked location"
        )
        assert "w" in result["untracked_prompt_agents"]

    def test_approve_response_includes_registration_propagation_report(
        self,
        routes_module: Any,
        store: state.StateStore,
        origin: Path,
        tmp_path: Path,
        isolated_env: dict[str, Path],
        bundle_url_patched: None,
    ) -> None:
        """requirements.md 5.7: a COMPLETE agent registration's own

        propagation report (``registration.Result.propagation_report``,
        keyed by agent name) must reach the approve response. This report
        is produced entirely inside ``registration.check_registrations``
        and is DISCARDED by ``apply.apply_commit`` -- it is read off
        ``reg_result`` but never copied onto ``ApplyResult`` at all, so no
        response field can carry it forward regardless of what
        `_apply_result_to_dict` does. This test therefore pins the
        end-to-end requirement (an operator sees agent v's own Req-5.7
        reporting string in the approve response) rather than asserting
        against a single named `ApplyResult` field -- it is RED today
        because nothing anywhere in the pipeline exposes it to the route.
        """
        work = tmp_path / "propagation-report-work"
        work.mkdir()
        _git("init", "-q", "-b", "main", cwd=work)
        _git("remote", "add", "origin", str(origin), cwd=work)
        (work / "agents").mkdir()
        (work / "agents" / "v.json").write_text(
            json.dumps(
                {
                    "name": "v",
                    "description": "Agent v.",
                    "prompt": "You are agent v, an inline-prompt test agent.",
                    "tools": ["read"],
                    "allowedTools": ["read"],
                    "resources": [],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        (work / "config.json").write_text(
            json.dumps({"agents": {"v": {"source": "local"}}}), encoding="utf-8"
        )
        (work / "agent_model_state.json").write_text(
            json.dumps({"v": {"model_managed": False}}), encoding="utf-8"
        )
        _git("add", "-A", cwd=work)
        _git("commit", "-q", "-m", "register agent v", cwd=work)
        sha = _head_sha(work)
        _git("push", "-q", "-u", "origin", "main", cwd=work)

        _seed_pending(
            store,
            sha=sha,
            classified_paths={
                "agents/v.json": "live_on_next_resolution",
                "config.json": "live_after_cache_invalidation",
                "agent_model_state.json": "live_after_cache_invalidation",
            },
        )

        result = routes_module.approve(store, sha)

        assert result.get("status") == "ok"

        found_report = None
        for value in result.values():
            if (
                isinstance(value, dict)
                and "v" in value
                and isinstance(value.get("v"), str)
            ):
                found_report = value["v"]
                break

        assert found_report is not None, (
            "no field in approve()'s response carries agent v's "
            "requirements.md 5.7 propagation-report string (H5) -- "
            "registration.check_registrations computes it but nothing "
            "threads it through apply_commit's ApplyResult into the "
            "response the operator actually sees"
        )


class TestH5PushNowResponseCarriesNonPortable:
    def test_push_now_response_includes_non_portable(
        self,
        routes_module: Any,
        store: state.StateStore,
        isolated_env: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """requirements.md 2.9: push_now's response must carry

        `PushResult.non_portable` so the operator can see which values
        were left un-tokenized because they lie under neither root. Stub
        `push_run` (the narrow, non-git point `routes.push_now` calls) to
        return a canned `PushResult` carrying a non-empty
        `non_portable` list, and confirm the route's response actually
        includes it. Red against current routes.py, whose `push_now`
        builds its dict from only `outcome`/`tree_hash`/`reason`.
        """
        from backend.push import PushResult

        stub_result = PushResult(
            outcome="pushed",
            tree_hash="deadbeef",
            reason="",
            non_portable=[
                {
                    "path": "config.json",
                    "key_path": ["some_path"],
                    "value": "/usr/local/lib/site-packages/x.md",
                }
            ],
        )

        monkeypatch.setattr(routes_module, "push_run", lambda: stub_result)

        result = routes_module.push_now(store)

        assert "non_portable" in result, (
            "push_now()'s response dropped PushResult.non_portable "
            "entirely (H5) -- an operator has no way to see which pushed "
            "values were left un-tokenized"
        )
        assert result["non_portable"] == stub_result.non_portable


# ===========================================================================
# H6: restore() silently `continue`s on OSError and still reports "ok".
# ===========================================================================


class TestH6RestoreReportsFailureInsteadOfSilentlyContinuing:
    def _seed_apply_with_restore_dir(
        self, store: state.StateStore, isolated_env: dict[str, Path], tmp_path: Path
    ) -> tuple[str, Path, Path, Path]:
        """Build a restore directory + live root state as if a real apply

        already ran: one backed-up file (steering/a.md) and one
        created-file manifest entry (steering/created.md), matching what
        `apply.py::restore`'s two branches each read.
        """
        apply_id = "apply-test-h6"
        restore_dir = isolated_env["state_dir"] / "restores" / apply_id
        (restore_dir / "A").mkdir(parents=True, exist_ok=True)

        backup_relpath = "steering/a.md"
        backup_path = restore_dir / "A" / backup_relpath
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        backup_path.write_text("# a (pre-apply)\n", encoding="utf-8")

        created_relpath = "steering/created.md"
        (restore_dir / "A" / ".created-manifest.json").write_text(
            json.dumps([created_relpath]), encoding="utf-8"
        )

        live_a = isolated_env["root_a"] / "steering" / "a.md"
        live_a.parent.mkdir(parents=True, exist_ok=True)
        live_a.write_text("# a (post-apply, wrong)\n", encoding="utf-8")

        live_created = isolated_env["root_a"] / "steering" / "created.md"
        live_created.write_text("# created by the apply\n", encoding="utf-8")

        store.record_restore_dir(apply_id=apply_id, restore_dir=str(restore_dir))

        return apply_id, restore_dir, live_a, live_created

    def test_restore_reports_partial_when_a_restore_write_fails(
        self,
        routes_module: Any,
        store: state.StateStore,
        isolated_env: dict[str, Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """MUTATION: monkeypatch `Path.replace` so the atomic rename step

        for exactly `steering/a.md`'s restore raises OSError (the narrow
        write-failure point restore.py's docstring says is silently
        `continue`d past at ~568) while every other file's own
        `.replace()` call behaves normally. This isolates the failure to
        the one file under test without mocking the whole restore
        pipeline.
        """
        apply_id, restore_dir, live_a, live_created = self._seed_apply_with_restore_dir(
            store, isolated_env, tmp_path
        )

        real_replace = Path.replace

        def _failing_replace(self: Path, target: Any) -> Any:
            target_path = Path(target)
            if target_path.name == "a.md" and target_path.parent.name == "steering":
                raise OSError("simulated: restore write failed for steering/a.md")
            return real_replace(self, target)

        monkeypatch.setattr(Path, "replace", _failing_replace)

        result = routes_module.restore(store, apply_id)

        assert result.get("status") == "partial", (
            "restore() silently swallowed the OSError on steering/a.md's "
            "restore write and still reported status 'ok' (H6) -- the "
            "operator has no signal that requirements.md 4.7's exact-"
            "restore guarantee was violated for this file"
        )
        failed = result.get("failed", [])
        assert any(
            "steering/a.md" in entry and apply_id in entry for entry in failed
        ), (
            "the failed list must name the specific file that could not "
            "be restored, together with the apply id it belongs to"
        )

        # Other files must still be restored despite the one failure:
        # steering/created.md's removal branch is unaffected by this
        # mutation (only steering/a.md's replace() call raises), so it
        # must be reported removed.
        removed = result.get("removed", {})
        removed_relpaths = {rp for root_paths in removed.values() for rp in root_paths}
        assert "steering/created.md" in removed_relpaths

    def test_restore_reports_partial_when_a_created_file_removal_fails(
        self,
        routes_module: Any,
        store: state.StateStore,
        isolated_env: dict[str, Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """MUTATION: monkeypatch `Path.unlink` so removing exactly

        `steering/created.md` (the created-file manifest branch, ~581)
        raises OSError, while `steering/a.md`'s restore succeeds
        normally. Isolates the failure to the created-file removal
        branch specifically.
        """
        apply_id, restore_dir, live_a, live_created = self._seed_apply_with_restore_dir(
            store, isolated_env, tmp_path
        )

        real_unlink = Path.unlink

        def _failing_unlink(self: Path, missing_ok: bool = False) -> Any:
            if self.name == "created.md" and self.parent.name == "steering":
                raise OSError("simulated: could not remove steering/created.md")
            return real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", _failing_unlink)

        result = routes_module.restore(store, apply_id)

        assert result.get("status") == "partial", (
            "restore() silently swallowed the OSError removing "
            "steering/created.md and still reported status 'ok' (H6) -- "
            "the operator is never told the pre-apply state (the file "
            "did not exist) was not actually restored"
        )
        failed = result.get("failed", [])
        assert any(
            "steering/created.md" in entry and apply_id in entry for entry in failed
        ), "the failed list must name the file and the apply id"

        # The other file (steering/a.md) must have restored successfully
        # despite this unrelated failure.
        assert live_a.read_text(encoding="utf-8") == "# a (pre-apply)\n"


# ===========================================================================
# M1: tar extraction runs with no filter (DeprecationWarning on 3.12+;
# a malicious absolute-path or ".." member must be rejected outright).
# ===========================================================================


class TestM1TarExtractionIsFilteredAndSafe:
    def test_unpack_archive_call_does_not_emit_deprecation_warning(
        self,
        routes_module: Any,
        store: state.StateStore,
        origin: Path,
        shas_simple: str,
        tmp_path: Path,
        isolated_env: dict[str, Path],
        bundle_url_patched: None,
    ) -> None:
        """Runs a real approve (real git archive, real extraction) with

        DeprecationWarning promoted to an error for the extraction step.
        `_materialize_pending_commit` calls `shutil.unpack_archive` with
        no `filter=` argument, which is exactly what triggers Python
        3.12+'s "Python 3.14 will, by default, filter extracted tar
        archives" DeprecationWarning on every real archive extraction --
        RED today because that warning fires and this test turns warnings
        into errors around the call.
        """
        sha = shas_simple
        _seed_pending(
            store, sha=sha, classified_paths={"steering/one.md": "live_in_new_session"}
        )

        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            result = routes_module.approve(store, sha)

        assert result.get("status") == "ok", (
            "approve() raised or refused once DeprecationWarning was "
            "promoted to an error -- unpack_archive is being called "
            "without an explicit extraction filter (M1)"
        )

    def test_malicious_absolute_path_tar_member_is_rejected(
        self,
        routes_module: Any,
        store: state.StateStore,
        isolated_env: dict[str, Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """MUTATION: build a tar archive by hand containing one member

        named with an absolute path (``/etc/passthrough.md``) instead of
        going through a real git archive (git itself would never produce
        such a member -- this proves the extraction step's OWN defence,
        independent of git). Stub `_ensure_bundle_clone`/the git-archive
        subprocess call to instead hand back this hostile tar, and
        confirm the extraction step refuses rather than writing outside
        `commit_root`.
        """
        import tarfile

        bogus_sha = "a" * 40
        _seed_pending(
            store,
            sha=bogus_sha,
            classified_paths={"steering/x.md": "live_in_new_session"},
        )

        escape_target = tmp_path / "escaped.md"

        def _fake_run(argv: Any, stdout: Any = None, **kwargs: Any) -> Any:
            # git_safety.git_argv(clone_dir, "archive", sha) is the only
            # subprocess.run call _materialize_pending_commit makes before
            # unpack_archive -- write a hostile tar to `stdout` instead of
            # actually invoking git, isolating this test to the extraction
            # step's own safety, not git's.
            tar_path = Path(stdout.name)
            with tarfile.open(tar_path, "w") as handle:
                info = tarfile.TarInfo(name=str(escape_target))
                data = b"# escaped\n"
                info.size = len(data)
                import io

                handle.addfile(info, io.BytesIO(data))

            class _Result:
                returncode = 0

            return _Result()

        monkeypatch.setattr(routes_module.subprocess, "run", _fake_run)
        monkeypatch.setattr(
            routes_module, "_ensure_bundle_clone", lambda _clone_dir: None
        )

        result = routes_module.approve(store, bogus_sha)

        assert not escape_target.exists(), (
            "a tar member named with an absolute path escaped commit_root "
            "and was written to an arbitrary filesystem location -- "
            "unpack_archive is not rejecting an unsafe member (M1)"
        )
        assert result.get("status") == "error"


@pytest.fixture
def shas_simple(origin: Path, tmp_path: Path) -> str:
    return _seed_simple_commit(
        origin,
        tmp_path,
        relpath="steering/one.md",
        content="# one\n",
        message="add steering/one.md",
    )


# ===========================================================================
# M2: deletion detection via Path.exists() -- a dangling symlink in the
# extracted commit tree reads as "does not exist" and gets misclassified
# as a deletion, causing the LIVE file to be removed even though the
# approved commit did not delete it.
# ===========================================================================


class TestM2DanglingSymlinkIsNotTreatedAsADeletion:
    def test_dangling_symlink_in_commit_tree_does_not_delete_the_live_file(
        self,
        routes_module: Any,
        store: state.StateStore,
        origin: Path,
        tmp_path: Path,
        isolated_env: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """MUTATION: after the real materialize step extracts the

        approved commit's tree into `commit_root`, replace
        `steering/x.md` inside that extracted tree with a DANGLING
        symlink (pointing at a target that does not exist) before
        `_materialize_pending_commit` computes `deleted_paths`. This
        simulates the exact byte-shape a tar member of type `SYMTYPE`
        with a broken target produces, without depending on git/tar to
        manufacture one. Wrap `shutil.unpack_archive` (the narrow point)
        so the swap happens immediately after real extraction completes.

        Pre-condition: the live file exists (as if a PRIOR apply already
        wrote it) so removal is observable at all -- if the current
        defect fires, the live file is deleted; if fixed, it is left
        untouched and this file's presence in the commit is instead
        reported as refused/not-applied since commit_root's own copy of
        it is a dangling symlink (not real content to apply either).
        """
        sha = _seed_simple_commit(
            origin,
            tmp_path,
            relpath="steering/x.md",
            content="# x (approved content)\n",
            message="add steering/x.md",
        )
        _bundle_repo_url_env(monkeypatch, origin)

        live_x = isolated_env["root_a"] / "steering" / "x.md"
        live_x.parent.mkdir(parents=True, exist_ok=True)
        live_x.write_text("# x (already live from a prior apply)\n", encoding="utf-8")

        real_unpack_archive = shutil.unpack_archive

        def _unpack_then_replace_with_dangling_symlink(
            filename: Any, extract_dir: Any = None, **kwargs: Any
        ) -> None:
            real_unpack_archive(filename, extract_dir=extract_dir, **kwargs)
            extracted_target = Path(extract_dir) / "steering" / "x.md"
            if extracted_target.exists() or extracted_target.is_symlink():
                extracted_target.unlink()
            broken_target = Path(extract_dir) / "steering" / "__does_not_exist__.md"
            extracted_target.symlink_to(broken_target)

        monkeypatch.setattr(
            routes_module.shutil,
            "unpack_archive",
            _unpack_then_replace_with_dangling_symlink,
        )

        _seed_pending(
            store, sha=sha, classified_paths={"steering/x.md": "live_in_new_session"}
        )

        result = routes_module.approve(store, sha)

        assert live_x.is_file(), (
            "the live file was deleted because a dangling symlink at the "
            "same relpath in the extracted commit tree read as "
            "Path.exists() == False and was misclassified as an upstream "
            "deletion (M2) -- the approved commit did not actually delete "
            "this file"
        )
        assert live_x.read_text(encoding="utf-8") == (
            "# x (already live from a prior apply)\n"
        ), (
            "the live file's content was altered even though it should "
            "have been refused, not deleted or overwritten"
        )

        # The file must be reported as refused/not-applied rather than
        # silently either deleted or silently skipped with no signal.
        applied = result.get("applied", [])
        not_applied = result.get("not_applied", [])
        ignored = result.get("ignored_paths", [])
        assert "steering/x.md" not in applied, (
            "a dangling symlink has no real content -- it must never be "
            "reported as successfully applied"
        )
        assert "steering/x.md" in not_applied or "steering/x.md" in ignored, (
            "the dangling-symlink path must be reported to the operator "
            "as refused, not silently dropped with no trace in the result"
        )


# ===========================================================================
# H9: _ensure_bundle_clone was moved out of `with _clone_lock`, so the
# nested acquisition inside _materialize_pending_commit must not deadlock,
# and approve must be idempotent when nothing is pending on the retry.
# ===========================================================================


class TestH9CloneLockIsNotDeadlockedByNestedAcquisition:
    def test_approve_completes_within_a_short_timeout_under_a_held_clone_lock(
        self,
        routes_module: Any,
        store: state.StateStore,
        origin: Path,
        tmp_path: Path,
        isolated_env: dict[str, Path],
        bundle_url_patched: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Holds the REAL, file-based clone lock (via `git_safety.clone_lock`

        directly -- not through `_ensure_bundle_clone`) on a background
        thread for a short window, releasing it before the bounded
        `approve()` call's own lock-acquisition timeout would expire, then
        confirms `approve()` actually completes rather than hanging until
        ITS OWN internal timeout: `_ensure_bundle_clone` takes the lock
        once (and releases it), then `_materialize_pending_commit`'s own
        `with _clone_lock(clone_dir):` block takes it a SECOND, separate
        time for the archive step -- proving the two acquisitions are
        sequential, not nested/reentrant, is exactly what this test
        exercises. The lock timeout constants are patched down to a few
        seconds so a genuine deadlock manifests as a fast, bounded test
        failure (an unexpired `TimeoutError`/hang) rather than a 60s
        stall.
        """
        from backend import poll as poll_module

        monkeypatch.setattr(poll_module, "_CLONE_LOCK_TIMEOUT_SECS", 3.0)
        monkeypatch.setattr(poll_module, "_CLONE_LOCK_POLL_INTERVAL_SECS", 0.05)

        sha = _seed_simple_commit(
            origin,
            tmp_path,
            relpath="steering/x.md",
            content="# x\n",
            message="add steering/x.md",
        )
        _seed_pending(
            store, sha=sha, classified_paths={"steering/x.md": "live_in_new_session"}
        )

        from backend.poll import _BUNDLE_CLONE_DIRNAME
        from backend.safety import git_safety

        state_dir = state.get_state_dir()
        clone_dir = state_dir / _BUNDLE_CLONE_DIRNAME
        clone_dir.mkdir(parents=True, exist_ok=True)

        release_event = threading.Event()
        acquired_event = threading.Event()

        def _hold_lock_briefly() -> None:
            with git_safety.clone_lock(clone_dir, timeout_secs=5.0):
                acquired_event.set()
                release_event.wait(timeout=1.0)

        holder = threading.Thread(target=_hold_lock_briefly, daemon=True)
        holder.start()
        assert acquired_event.wait(timeout=2.0), "background lock holder never acquired"

        # Release well before approve's own timeout budget so a correctly
        # SEQUENTIAL (not deadlocked) implementation finishes quickly.
        release_event.set()
        holder.join(timeout=2.0)

        start = time.monotonic()
        result = routes_module.approve(store, sha)
        elapsed = time.monotonic() - start

        assert elapsed < 8.0, (
            f"approve() took {elapsed:.1f}s -- consistent with the nested "
            f"clone-lock acquisition deadlocking (H9) rather than "
            f"completing promptly once the external holder released"
        )
        assert result.get("status") == "ok"

    def test_approving_the_same_sha_twice_is_safe_and_the_second_refuses_cleanly(
        self,
        routes_module: Any,
        store: state.StateStore,
        origin: Path,
        tmp_path: Path,
        isolated_env: dict[str, Path],
        bundle_url_patched: None,
    ) -> None:
        """Running approve twice in a row with the identical sha: the

        first application resolves pending and advances base_sha; the
        second call must find NOTHING pending (pending is None after the
        first call's resolve_pending) and refuse cleanly with status
        'error' -- never re-apply, never hang, never raise.
        """
        sha = _seed_simple_commit(
            origin,
            tmp_path,
            relpath="steering/x.md",
            content="# x\n",
            message="add steering/x.md",
        )
        _seed_pending(
            store, sha=sha, classified_paths={"steering/x.md": "live_in_new_session"}
        )

        first = routes_module.approve(store, sha)
        assert first.get("status") == "ok"

        second = routes_module.approve(store, sha)

        assert second.get("status") == "error", (
            "approving the identical sha twice must refuse cleanly on the "
            "second call (nothing pending) rather than re-applying or "
            "hanging (H9)"
        )

        reloaded = state.load_state()
        assert reloaded.pending is None
        assert reloaded.base_sha == sha


# ===========================================================================
# Low: when materialization fails, the temp commit dir is removed;
# ignored/not_applied entries carry the root id.
# ===========================================================================


class TestLowMaterializeFailureCleansUpTempDirAndRootIdsAreCarried:
    def test_materialize_failure_removes_the_temp_commit_root(
        self,
        routes_module: Any,
        store: state.StateStore,
        isolated_env: dict[str, Path],
        tmp_path: Path,
    ) -> None:
        """A bogus sha (absent from the bundle clone) makes materialization

        fail at the git-archive step. The temp `commit_root` directory
        `_materialize_pending_commit` creates via `tempfile.mkdtemp`
        BEFORE that failure must not be left behind under the state
        directory afterwards.
        """
        bogus_sha = "b" * 40
        _seed_pending(
            store,
            sha=bogus_sha,
            classified_paths={"steering/x.md": "live_in_new_session"},
        )

        state_dir = state.get_state_dir()
        before = {p.name for p in state_dir.iterdir()} if state_dir.exists() else set()

        result = routes_module.approve(store, bogus_sha)

        assert result.get("status") == "error"

        after = {p.name for p in state_dir.iterdir()} if state_dir.exists() else set()
        leftover_materialize_dirs = {
            name
            for name in (after - before)
            if name.startswith(f"materialize-{bogus_sha[:12]}")
        }
        assert not leftover_materialize_dirs, (
            f"materialization failure left behind a temp commit-root "
            f"directory ({leftover_materialize_dirs}) instead of cleaning "
            f"it up (Low)"
        )

    def test_ignored_and_not_applied_entries_are_traceable_to_their_root(
        self,
        routes_module: Any,
        store: state.StateStore,
        origin: Path,
        tmp_path: Path,
        isolated_env: dict[str, Path],
        bundle_url_patched: None,
    ) -> None:
        """A pending record naming one root-A path and one path that is

        NOT allowlisted at all (so it lands in `ignored_paths`) must let
        an operator determine which root a given ignored/not-applied
        relpath belongs to. `ApplyResult.ignored_paths`/`not_applied` are
        flat relpath lists with no root id attached -- the response must
        carry enough structure (either the relpath's own root-A/root-B
        namespace being unambiguous, or an explicit root id alongside
        it) for a caller to trace the entry back to a root. This test
        pins the observable requirement: the SAME relpath text must not
        appear ambiguously across two different roots' reports without
        a way to disambiguate which root it was ignored/not-applied for.
        """
        sha = _seed_simple_commit(
            origin,
            tmp_path,
            relpath="not-an-allowlisted-file.txt",
            content="not tracked\n",
            message="add a file with no allowlist entry",
        )
        _seed_pending(
            store,
            sha=sha,
            classified_paths={
                "not-an-allowlisted-file.txt": "live_in_new_session",
            },
        )

        result = routes_module.approve(store, sha)

        ignored = result.get("ignored_paths", [])
        assert "not-an-allowlisted-file.txt" in ignored

        # The response must carry the root each ignored entry belongs to
        # somewhere -- either as a "root:relpath" composite entry, or a
        # parallel/keyed structure. A bare, root-less string list gives
        # the operator no way to tell root A's ignored files from root
        # B's when relpaths collide across roots.
        assert (
            any(
                ":" in entry or "/" in entry or isinstance(entry, dict)
                for entry in ignored
            )
            or "root" in result
        ), (
            "ignored_paths carries no per-entry root identifier at all -- "
            "an operator cannot tell which configured root "
            "'not-an-allowlisted-file.txt' was ignored for (Low)"
        )
