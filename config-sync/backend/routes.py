"""Backend routes for config-sync (tasks.md 6.1; design.md's routes section).

Covers requirements.md 4.9, 7.1, 7.2, 7.4, 7.5.

Five plain, framework-agnostic functions — no web framework dependency;
`backend/server.py` (or a future dispatcher) is the thing that maps HTTP
verbs/paths onto these:

    status(store: StateStore) -> dict
    drift(store: StateStore) -> dict
    push_now(store: StateStore) -> dict
    approve(store: StateStore, sha: str) -> dict
    decline(store: StateStore, sha: str) -> dict

Every route returns a plain JSON-serializable dict carrying at least a
``"status"`` key (``"ok"`` | ``"error"``). Every route is gated on
``is_app_enabled`` (design.md's "every backend route SHALL refuse the
request" while disabled — requirements.md 7.4): the check is imported
LAZILY, inside each call, from ``kiro_crew.apps.manager`` — never at
module import time — so a platform import failure REFUSES that single
call (fail-closed) rather than either crashing this module's own import
(which would take every OTHER route down with it) or silently treating
the app as enabled. ``is_app_enabled`` is exposed as a module attribute
(reassigned on each call from the lazy import) precisely so tests can
monkeypatch ``backend.routes.is_app_enabled`` directly, per the platform
convention this module follows (see ``code_review_sage``'s
``backend/routes.py::_require_enabled``).

``approve`` is the ONLY route from which ``apply.apply_commit`` is ever
called (design.md: "approve being the only route from which an apply can
begin"). ``approve`` and ``decline`` both resolve the pending record via
the SAME ``state.StateStore.resolve_pending`` call (tasks.md 6.1) — there
is no separate "decline" mutation on ``StateStore``; the two routes are
distinguished only by whether ``approve`` also ran ``apply_commit`` first.
Both refuse with ``status: "error"`` when the submitted ``sha`` no longer
matches what is actually pending (Kiro-Config-Bundles#65's staleness
case), leaving ``pending``/``base_sha`` completely untouched, so the
operator's decision can never be silently applied against — or clear away
— files they never actually reviewed.

No response from any route ever carries an unredacted credential
(requirements.md 3.8, 7.5): ``status``/``drift`` build their view from
``collect.collect()`` + ``redact.redact()`` (never live, unredacted file
bytes), and ``push_now``/``approve``/``decline`` return only counts,
names, and outcome strings — never raw file content.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

from backend import allowlist, collect, push, redact
from backend import state as state_module
from backend.apply import _is_safe_relpath, _resolve_target, _root_path, apply_commit
from backend.poll import _BUNDLE_CLONE_DIRNAME, _clone_lock, _ensure_bundle_clone
from backend.push import run as push_run
from backend.safety import git_safety

_APP_NAME = "config-sync"

#: Root ids a pending record's merged ``classified_paths`` are split
#: across, matching ``poll.py``'s own ``_ROOT_IDS`` — the bundle repo's
#: tree interleaves both roots' relpaths with no per-root prefix, so a
#: pending path is assigned to whichever root's allowlist actually
#: matches it, via the SAME ``allowlist.is_tracked`` probe ``poll.py``'s
#: ``_classify_changed_paths`` already performs.
_ROOT_IDS: Tuple[str, ...] = ("A", "B")


class _MaterializeError(RuntimeError):
    """Raised when a pending sha cannot be materialized from the bundle repo.

    Caught by ``approve`` and turned into a ``status: "error"`` response
    with no state mutation — never propagated as an unhandled exception.
    """


def _split_paths_by_root(relpaths: List[str]) -> Dict[str, List[str]]:
    """Split a flat list of relpaths into ``{"A": [...], "B": [...]}`` by

    which root's allowlist actually matches each one — the same two-root
    probe ``poll.py``'s ``_classify_changed_paths`` performs, reusing
    ``allowlist.is_tracked`` rather than a second matcher.

    A relpath matching NEITHER root's allowlist is still placed under
    root ``"A"`` (the first ``_ROOT_IDS`` entry) rather than dropped
    entirely: ``apply_commit`` runs its OWN allowlist gate over whatever
    ``changed_paths`` it is handed and is the thing that actually reports
    an untracked relpath in ``ApplyResult.ignored_paths`` — a relpath this
    function silently drops before ``apply_commit`` ever sees it never
    reaches that gate at all, so it never surfaces in the response
    (senior-review Low). Root "A" is an arbitrary but consistent default
    for this case only; ``apply_commit`` ignores the path regardless of
    which root it is nominally filed under.
    """
    by_root: Dict[str, List[str]] = {root: [] for root in _ROOT_IDS}
    for relpath in relpaths:
        matched = False
        for root in _ROOT_IDS:
            if allowlist.is_tracked(root, relpath):
                by_root[root].append(relpath)
                matched = True
        if not matched:
            by_root[_ROOT_IDS[0]].append(relpath)
    return by_root


def _materialize_pending_commit(
    store: "state_module.StateStore", sha: str
) -> Tuple[Path, Dict[str, List[str]], Dict[str, List[str]]]:
    """Materialize ``sha``'s tree and split its pending changed paths by root.

    Returns ``(commit_root, changed_paths, deleted_paths)`` matching
    ``apply.apply_commit``'s own three real inputs (see
    ``tests/test_routes_approve_seam.py``'s module docstring for the full
    interface contract). Reuses ``poll.py``'s own bundle-repo clone
    plumbing (``_ensure_bundle_clone``, ``_clone_lock``,
    ``_BUNDLE_CLONE_DIRNAME``) rather than inventing a second git code
    path; the one additional git call this function needs (``git
    archive``) is built through ``git_safety.git_argv`` inline, per the
    single-call-site rule ``tests/safety/test_git_safety.py`` enforces
    statically over ``backend/``.

    ``commit_root`` is a freshly created temp directory under the app's
    own state directory (never ``/tmp``) holding ``sha``'s tree exactly as
    ``git archive`` extracts it — both roots' tracked relpaths interleaved
    with no per-root subdirectory, matching ``apply.apply_commit``'s own
    ``commit_root`` contract. The clone lock is held only while touching
    the shared clone (ensure + archive); it is released before the
    extracted tar is unpacked into ``commit_root``.

    Raises:
        _MaterializeError: if the clone cannot be updated, ``sha`` cannot
            be archived (e.g. it is absent from the bundle-repo clone), or
            the archive cannot be extracted. The caller is responsible for
            ensuring no partial ``commit_root`` is left behind and that no
            state mutation happens on this path.
    """
    state_dir = state_module.get_state_dir()
    clone_dir = state_dir / _BUNDLE_CLONE_DIRNAME
    commit_root = Path(
        tempfile.mkdtemp(prefix=f"materialize-{sha[:12]}-", dir=str(state_dir))
    )
    tar_path = commit_root.parent / f".{commit_root.name}.tar"

    try:
        try:
            # _ensure_bundle_clone takes the (non-reentrant) clone lock itself;
            # calling it while already holding that lock deadlocks until the
            # lock timeout. Take the lock separately for the archive read.
            _ensure_bundle_clone(clone_dir)
            with _clone_lock(clone_dir):
                with open(tar_path, "wb") as tar_handle:
                    subprocess.run(
                        git_safety.git_argv(clone_dir, "archive", sha),
                        stdout=tar_handle,
                        stderr=subprocess.PIPE,
                        check=True,
                    )
        except (subprocess.CalledProcessError, OSError, TimeoutError) as exc:
            raise _MaterializeError(
                f"could not materialize commit {sha}: {exc}"
            ) from exc

        try:
            # filter="data" (Python 3.12+) sanitizes an absolute-path or
            # ``..``-escaping member by REWRITING it under commit_root
            # (stripping the leading "/", collapsing ".."), rather than
            # raising — verified empirically: a member named
            # "/etc/passthrough.md" lands at
            # "<commit_root>/etc/passthrough.md", never outside
            # commit_root, but with no error either. That silent rewrite
            # is not good enough here: this app must REFUSE a commit
            # whose tree contains such a member outright (senior-review
            # M1), not accept a rewritten shadow of it as legitimate
            # content. So every member's name is checked explicitly,
            # BEFORE any extraction, and the whole materialize is refused
            # if one is absolute or contains a ``..`` segment; only once
            # that check passes does ``filter="data"`` run — as a second,
            # defence-in-depth layer against anything the explicit check
            # does not anticipate (e.g. a symlink member whose target
            # escapes commit_root).
            with tarfile.open(tar_path) as tar_handle_check:
                for member in tar_handle_check.getmembers():
                    member_path = PurePosixPath(member.name)
                    if member_path.is_absolute() or ".." in member_path.parts:
                        raise _MaterializeError(
                            f"commit {sha}'s tree contains an unsafe tar "
                            f"member: {member.name!r}"
                        )

            shutil.unpack_archive(
                str(tar_path), extract_dir=str(commit_root), filter="data"
            )
        except (shutil.ReadError, OSError, tarfile.TarError) as exc:
            raise _MaterializeError(
                f"could not extract commit {sha}'s tree: {exc}"
            ) from exc
    except _MaterializeError:
        # Low: a materialization failure must not leave the temp
        # commit_root directory (created above via tempfile.mkdtemp)
        # behind under the state directory.
        shutil.rmtree(commit_root, ignore_errors=True)
        raise
    finally:
        tar_path.unlink(missing_ok=True)

    pending = store.pending
    all_relpaths = list((pending or {}).get("classified_paths", {}).keys())

    changed_paths = _split_paths_by_root(all_relpaths)
    deleted_paths: Dict[str, List[str]] = {root: [] for root in _ROOT_IDS}
    for root, relpaths in changed_paths.items():
        for relpath in relpaths:
            # os.path.lexists — never Path.exists() — so a dangling
            # symlink at this relpath in the extracted tree (a tar
            # SYMTYPE member whose target is absent) is correctly seen
            # as PRESENT (senior-review M2). Path.exists()/os.path.exists
            # both follow the symlink and read a dangling one as absent,
            # which misclassifies it as an upstream deletion and would
            # make apply_commit unlink the corresponding LIVE file even
            # though the approved commit never deleted it. lexists()
            # reports on the link itself, matching git's own change-type
            # detection (a symlink member is a real tree entry, deleted
            # or not, independent of where it points).
            if not os.path.lexists(commit_root / relpath):
                deleted_paths[root].append(relpath)

    return commit_root, changed_paths, deleted_paths


_HOOKS_RELPATH = "hooks.json"
_MCP_RELPATH = "mcp.json"


def _load_json_relpath(root: Path, relpath: str) -> Dict[str, Any]:
    """Read and parse ``root/relpath`` as a JSON object, defaulting to

    ``{}`` when the file is absent, unreadable, or fails to parse — a
    materialization/live-tree read for the M5 changed-commands summary
    must never raise, since a malformed or missing file simply means
    "nothing to diff on that side" (an added file looks identical to an
    empty existing one; a removed file's commands are reported as
    changed by the surviving side only, which is the pre-existing
    ``apply_commit``/M5 shape — this summary never claims to report
    removals on its own).
    """
    path = root / relpath
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _hook_commands(document: Dict[str, Any]) -> Dict[str, str]:
    """Extract ``{hook_name: command}`` from a parsed ``hooks.json`` document.

    Mirrors the real shape (``kiro_crew/hooks.py::ScriptHook`` — no
    ``args`` field, a single shell string) that
    ``tests/test_apply_command_vet.py`` establishes. An entry with no
    usable name or no ``command`` string is skipped rather than raising.
    """
    commands: Dict[str, str] = {}
    for hook in document.get("hooks", []):
        if not isinstance(hook, dict):
            continue
        name = hook.get("name") or hook.get("id")
        command = hook.get("command")
        if isinstance(name, str) and name and isinstance(command, str) and command:
            commands[name] = command
    return commands


def _mcp_server_commands(document: Dict[str, Any]) -> Dict[str, str]:
    """Extract ``{server_name: joined_command}`` from a parsed ``mcp.json``

    document, joining ``command`` + ``args`` with ``shlex.join`` — the
    same joined-string shape ``test_apply_command_vet.py`` asserts
    (``"new-arg" in server_entry["command"]``), and the same shape the
    real subprocess invocation actually uses, so a change smuggled only
    through ``args`` still registers as a changed command.
    """
    import shlex

    commands: Dict[str, str] = {}
    servers = document.get("mcpServers", {})
    if not isinstance(servers, dict):
        return commands
    for name, server in servers.items():
        if not isinstance(name, str) or not name or not isinstance(server, dict):
            continue
        command = server.get("command")
        if not isinstance(command, str) or not command:
            continue
        args = server.get("args", [])
        args_list = [str(a) for a in args] if isinstance(args, list) else []
        commands[name] = shlex.join([command, *args_list])
    return commands


def _changed_commands_for_file(
    relpath: str,
    live_document: Dict[str, Any],
    incoming_document: Dict[str, Any],
    extractor: Any,
) -> List[Dict[str, str]]:
    """Diff one command-bearing file's live vs incoming commands by name,

    returning ``[{file, name, command}]`` for every name that is either
    new or whose command text changed — never for an unchanged or a
    removed name (pinned by
    ``test_apply_command_vet.py::test_apply_changed_commands_lists_only_added_or_changed_not_unchanged``,
    which this route-level helper matches for the pre-approval summary).
    """
    live_commands = extractor(live_document)
    incoming_commands = extractor(incoming_document)
    changed: List[Dict[str, str]] = []
    for name, command in incoming_commands.items():
        if live_commands.get(name) != command:
            changed.append({"file": relpath, "name": name, "command": command})
    return changed


def _changed_commands_summary(
    live_root: Path, commit_root: Path
) -> List[Dict[str, str]]:
    """Compute the M5 pre-approval "what commands changed" summary by

    comparing ``hooks.json``/``mcp.json`` in ``commit_root`` (the
    materialized pending commit's tree) against the same files under
    ``live_root``. A small, pure, routes-local re-implementation of
    ``apply_commit``'s own changed-commands comparison (tasks.md: "if you
    need apply's changed-command comparison, re-implement a small pure
    helper in routes.py rather than editing apply.py") — this function
    never calls ``apply_commit`` itself, which must only ever run from
    ``approve`` (``apply.py``'s own module docstring).

    Returns a flat list combining both files' entries, in a fixed
    (hooks then mcp) order — the shape
    ``test_apply_command_vet.py::test_status_pending_summary_lists_changed_commands_for_pending_commit``
    pins: ``[{"file": ..., "name": ..., "command": ...}]``.
    """
    live_hooks = _load_json_relpath(live_root, _HOOKS_RELPATH)
    commit_hooks = _load_json_relpath(commit_root, _HOOKS_RELPATH)
    live_mcp = _load_json_relpath(live_root, _MCP_RELPATH)
    commit_mcp = _load_json_relpath(commit_root, _MCP_RELPATH)

    changed: List[Dict[str, str]] = []
    changed.extend(
        _changed_commands_for_file(
            _HOOKS_RELPATH, live_hooks, commit_hooks, _hook_commands
        )
    )
    changed.extend(
        _changed_commands_for_file(
            _MCP_RELPATH, live_mcp, commit_mcp, _mcp_server_commands
        )
    )
    return changed


def _pending_changed_commands(
    store: "state_module.StateStore",
) -> Tuple[List[Dict[str, str]], Optional[str]]:
    """Compute the M5 changed-commands summary for the current pending

    commit, if any.

    Returns ``(changed_commands, materialize_error)``: on success
    ``materialize_error`` is ``None`` and ``changed_commands`` holds the
    diff; when there is no pending commit at all, both are empty/``None``
    (nothing to summarize); when materialization fails, per the task
    instruction ("if materialization fails report that in the summary,
    don't raise") ``changed_commands`` is ``[]`` and
    ``materialize_error`` carries the reason — ``status()`` must never
    raise on this path.

    Reuses ``_materialize_pending_commit`` (the SAME helper ``approve``
    uses) so the pre-approval view is computed against exactly the tree
    an actual approve would apply — and always cleans up the temporary
    commit_root it creates, matching that helper's own cleanup contract.
    """
    pending = store.pending
    if pending is None:
        return [], None

    sha = pending.get("sha")
    if not isinstance(sha, str) or not sha:
        return [], None

    try:
        commit_root, _changed_paths, _deleted_paths = _materialize_pending_commit(
            store, sha
        )
    except _MaterializeError as exc:
        return [], str(exc)

    try:
        root_a = _root_path("A")
        return _changed_commands_summary(root_a, commit_root), None
    finally:
        shutil.rmtree(commit_root, ignore_errors=True)


def is_app_enabled(name: str) -> bool:
    """Fail-closed enablement check, imported lazily on every call.

    Never imported at module scope: a missing/broken platform import
    (``kiro_crew.apps.manager`` unavailable) must REFUSE this call rather
    than either taking down every route in this module at import time or
    silently defaulting to "enabled". Reassigned as a plain function here
    (rather than only wrapping the import inline in ``_require_enabled``)
    so tests can monkeypatch ``backend.routes.is_app_enabled`` directly,
    matching the platform convention this module follows.
    """
    try:
        from kiro_crew.apps.manager import is_app_enabled as _real_is_app_enabled
    except ImportError:
        return False
    return bool(_real_is_app_enabled(name))


def _require_enabled() -> Optional[Dict[str, Any]]:
    """Return an error dict when the app is disabled, else ``None``.

    Every route calls this FIRST, before touching ``store`` or any other
    dependency — including ``approve``/``decline``, which must refuse
    before ever reaching ``state.resolve_pending`` or ``apply_commit``
    (requirements.md 7.4: unconditional, not merely "most routes").
    """
    if is_app_enabled(_APP_NAME):
        return None
    return {"status": "error", "reason": "config-sync is disabled"}


def _redacted_tree() -> Dict[str, bytes]:
    """Collect and redact the live tracked tree — the ONLY source

    ``status``/``drift`` ever read file content from. Never
    ``collect.collect()`` alone: an unredacted live value must never reach
    a response body (requirements.md 3.8, 7.5).
    """
    return redact.redact(collect.collect())


def _pending_with_changed_commands(
    store: "state_module.StateStore",
) -> Optional[Dict[str, Any]]:
    """Build the ``pending`` value ``status()`` returns: ``store.pending``

    verbatim, plus the M5 ``changed_commands`` summary (and, if
    materialization failed, a ``changed_commands_error`` reason) —
    without mutating the stored pending record itself. Returns ``None``
    when nothing is pending, matching ``store.pending``'s own shape.
    """
    pending = store.pending
    if pending is None:
        return None

    changed_commands, materialize_error = _pending_changed_commands(store)
    enriched = dict(pending)
    enriched["changed_commands"] = changed_commands
    if materialize_error is not None:
        enriched["changed_commands_error"] = materialize_error
    return enriched


def status(store: "state_module.StateStore") -> Dict[str, Any]:
    """GET status: push state, drift flag, last-seen SHA, pending summary.

    The pending summary (M5) additionally carries ``changed_commands`` —
    every added/changed ``hooks.json``/``mcp.json`` command entry for the
    pending commit, computed by comparing its materialized tree against
    the live files (see ``_pending_changed_commands``) — so the operator
    sees what commands a pending commit would change before clicking
    approve. A materialization failure is reported via
    ``changed_commands_error`` rather than raising.
    """
    disabled = _require_enabled()
    if disabled is not None:
        return disabled

    redacted = _redacted_tree()
    current_hash = push.tree_hash(redacted)
    drift_present = current_hash != store.last_pushed_hash

    return {
        "status": "ok",
        "last_push": store.last_push,
        "last_push_failure": store.last_push_failure,
        "last_seen_sha": store.last_seen_sha,
        "last_poll_failure": store.last_poll_failure,
        "drift": drift_present,
        "pending": _pending_with_changed_commands(store),
    }


def drift(store: "state_module.StateStore") -> Dict[str, Any]:
    """GET drift: collected-tree hash vs last pushed, with the changed

    (redacted) relpath list.
    """
    disabled = _require_enabled()
    if disabled is not None:
        return disabled

    redacted = _redacted_tree()
    current_hash = push.tree_hash(redacted)
    changed_files = (
        sorted(redacted.keys()) if current_hash != store.last_pushed_hash else []
    )

    return {
        "status": "ok",
        "tree_hash": current_hash,
        "last_pushed_hash": store.last_pushed_hash,
        "drift": current_hash != store.last_pushed_hash,
        "changed_files": changed_files,
    }


def push_now(store: "state_module.StateStore") -> Dict[str, Any]:
    """POST push: run the push job now, on the same code path as the cron."""
    disabled = _require_enabled()
    if disabled is not None:
        return disabled

    result = push_run()
    return {
        "status": "ok",
        "outcome": result.outcome,
        "tree_hash": result.tree_hash,
        "reason": result.reason,
        "non_portable": list(getattr(result, "non_portable", [])),
    }


def _report_to_dict(report: Any) -> Dict[str, Dict[str, Any]]:
    """Convert an ``ApplyResult.propagation`` (``propagate.Report``) into a

    plain JSON-serializable mapping — its ``ReportEntry`` values carry an
    ``Enum`` field (``PropagationClass``), which ``json.dumps`` cannot
    serialize on its own.
    """
    return {
        relpath: {
            "propagation_class": entry.propagation_class.value,
            "message": entry.message,
            "requires_restart": entry.requires_restart,
        }
        for relpath, entry in report.entries.items()
    }


def _relpath_to_root(
    relpath: str, changed_paths: Optional[Dict[str, List[str]]]
) -> Optional[str]:
    """Trace ``relpath`` back to the root id ("A"/"B") it belongs to,

    using the ``changed_paths`` split ``_materialize_pending_commit``
    already computed for this apply. Returns ``None`` when
    ``changed_paths`` is not supplied, or ``relpath`` is not found in
    either root's list (e.g. a relpath ``apply_commit`` reports that was
    never part of this commit's own split — should not happen, but must
    not raise).
    """
    if changed_paths is None:
        return None
    for root, relpaths in changed_paths.items():
        if relpath in relpaths:
            return root
    return None


def _root_map_for(
    relpaths: List[str], changed_paths: Optional[Dict[str, List[str]]]
) -> Dict[str, str]:
    """Build a ``{relpath: root_id}`` map for every relpath this function

    can trace to a root via ``changed_paths`` (senior-review Low).
    ``ApplyResult.ignored_paths``/``not_applied`` are flat relpath lists
    with no root id attached, so a caller cannot otherwise tell root A's
    entries apart from root B's when the same relpath text could occur
    under both. Kept as a SEPARATE mapping rather than prefixing the
    relpath strings themselves, so ``ignored_paths``/``not_applied``
    still carry the exact relpath text ``apply_commit`` reported (tests
    and any existing caller match on that text verbatim). A relpath this
    function cannot trace to a root is simply absent from the map.
    """
    root_map: Dict[str, str] = {}
    for relpath in relpaths:
        root = _relpath_to_root(relpath, changed_paths)
        if root is not None:
            root_map[relpath] = root
    return root_map


def _apply_result_to_dict(
    result: Any, changed_paths: Optional[Dict[str, List[str]]] = None
) -> Dict[str, Any]:
    """Convert an ``ApplyResult`` into a plain, JSON-serializable dict.

    Never includes raw file content — only outcome, counts, and names —
    so an applied file's (already-restored) live credential value can
    never reach a response body via this path either.

    Every field is read with ``getattr`` and a documented default rather
    than a direct attribute access: this module must not be edited to add
    a field to ``ApplyResult`` itself (that dataclass belongs to
    ``apply.py``, owned separately), so a field this dict wants that
    ``ApplyResult`` does not yet carry degrades to its empty default
    instead of raising ``AttributeError``.

    ``non_portable_paths`` (requirements.md 4.12), ``unresolved_references``
    (4.13), and ``untracked_prompt_agents`` (5.11(c)) are already present
    on ``ApplyResult`` (populated by ``apply_commit``) — this function
    previously dropped all three from the response; they are surfaced
    here now (senior-review H5).

    ``registration.Result.propagation_report`` (requirements.md 5.7) IS
    surfaced: ``apply_commit`` copies it verbatim from
    ``registration.check_registrations``'s own ``reg_result`` onto
    ``ApplyResult.propagation_report`` (present only for agents in
    ``complete_agents`` there). Read via the same documented-default
    ``getattr`` every other field here uses, for the same reason: a
    refusal path's result object (e.g. the sha-mismatch refusal) need not
    carry every ``ApplyResult`` field.

    ``changed_paths`` (the ``{"A": [...], "B": [...]}`` split
    ``_materialize_pending_commit`` already computed for this apply) is
    optional so callers with no such split on hand (there are none today,
    but a future caller might not run through ``approve``) still get a
    valid dict — the ``"root"`` map is then simply empty rather than
    raising.
    """
    propagation = getattr(result, "propagation", None)
    not_applied = list(getattr(result, "not_applied", []))
    ignored_paths = list(getattr(result, "ignored_paths", []))
    root_map = _root_map_for(not_applied + ignored_paths, changed_paths)
    return {
        "outcome": result.outcome,
        "applied": list(getattr(result, "applied", [])),
        "not_applied": not_applied,
        "ignored_paths": ignored_paths,
        "dropped_cron_names": list(getattr(result, "dropped_cron_names", [])),
        "paused_cron_names": list(getattr(result, "paused_cron_names", [])),
        "changed_instance_names": list(getattr(result, "changed_instance_names", [])),
        "incomplete_registrations": dict(
            getattr(result, "incomplete_registrations", {})
        ),
        "needs_credential": list(getattr(result, "needs_credential", [])),
        "non_portable_paths": list(getattr(result, "non_portable_paths", [])),
        "unresolved_references": list(getattr(result, "unresolved_references", [])),
        "untracked_prompt_agents": list(getattr(result, "untracked_prompt_agents", [])),
        "propagation_report": dict(getattr(result, "propagation_report", {})),
        "propagation": _report_to_dict(propagation) if propagation is not None else {},
        "apply_id": getattr(result, "apply_id", None),
        "reason": getattr(result, "reason", ""),
        "root": root_map,
    }


def _resolve_or_error(
    store: "state_module.StateStore", sha: str
) -> Optional[Dict[str, Any]]:
    """Call ``state.resolve_pending(sha)``, translating its ``ValueError``

    refusal (no pending record, or a stale ``sha`` no longer matching what
    is actually pending — Kiro-Config-Bundles#65) into the route-level
    error-dict shape. Returns ``None`` on success (pending resolved,
    ``base_sha`` advanced) so the caller proceeds; returns the error dict
    on refusal so the caller returns immediately without having mutated
    anything else.
    """
    try:
        store.resolve_pending(sha)
    except ValueError as exc:
        return {"status": "error", "reason": str(exc)}
    return None


def approve(store: "state_module.StateStore", sha: str) -> Dict[str, Any]:
    """POST pending/{sha}/approve — the ONLY route from which an apply can

    begin (design.md). Refuses on the enabled-gate first, then on a stale
    ``sha`` (Kiro-Config-Bundles#65) BEFORE ever calling ``apply_commit``
    — an apply must never even start against a commit the operator's own
    approve/decline UI was not actually rendered against; ``apply_commit``
    carries the identical check internally (it refuses with
    ``outcome="refused-sha-mismatch"``), and this route's own check ahead
    of the call is what stops the apply pipeline from being entered at
    all, not merely from succeeding.

    ``state.resolve_pending`` (which advances ``base_sha`` and clears
    ``pending`` together) is called ONLY once ``apply_commit`` reports
    outcome ``"applied"`` (H4, ratified) — never on ``"partial"`` and
    never on a refusal. A partial apply means at least one allowlisted
    file failed to apply; resolving pending on that outcome would
    silently drop the not-applied commit from what the operator is shown
    next, even though it was never actually, fully applied. Instead, on
    ``"partial"``, ``pending``/``base_sha`` are left exactly as they were
    (``state.record_partial_apply`` only annotates the existing pending
    record with the not-applied paths and reasons — it does not resolve
    it), so the operator still sees this commit as pending and can
    re-approve once the upstream issue is fixed. A later approve of the
    SAME sha that fully applies (e.g. after an upstream fix lands and a
    fresh pending record names the fixed sha) resolves pending normally;
    ``decline`` still always clears pending regardless of any apply
    outcome, since decline never calls ``apply_commit`` at all.
    """
    disabled = _require_enabled()
    if disabled is not None:
        return disabled

    pending = store.pending
    if pending is None or pending.get("sha") != sha:
        return {
            "status": "error",
            "reason": (
                "no pending commit matches the approved sha "
                "(nothing pending, or a newer commit has since accumulated)"
            ),
        }

    try:
        commit_root, changed_paths, deleted_paths = _materialize_pending_commit(
            store, sha
        )
    except _MaterializeError as exc:
        return {"status": "error", "reason": str(exc)}

    try:
        result = apply_commit(
            approved_sha=sha,
            commit_root=commit_root,
            changed_paths=changed_paths,
            store=store,
            deleted_paths=deleted_paths,
        )
    finally:
        shutil.rmtree(commit_root, ignore_errors=True)

    if result.outcome not in ("applied", "partial"):
        payload = _apply_result_to_dict(result, changed_paths)
        payload["status"] = "error"
        return payload

    if result.outcome == "partial":
        # H4 (ratified): a partial apply does NOT resolve pending. Record
        # the not-applied paths/reasons onto the still-pending record so
        # they surface via status(); base_sha stays put, pending stays
        # put, and re-approving the same sha is safe (this call simply
        # re-runs, re-annotates, and never crashes).
        not_applied_reasons = {
            relpath: "not applied — see apply result for details"
            for relpath in result.not_applied
        }
        store.record_partial_apply(sha=sha, not_applied=not_applied_reasons)
        payload = _apply_result_to_dict(result, changed_paths)
        payload["status"] = "ok"
        return payload

    resolve_error = _resolve_or_error(store, sha)
    if resolve_error is not None:
        # The #65 staleness race: pending moved between the check above
        # and this call (a poll tick accumulated a newer commit into it
        # mid-apply). apply_commit already ran against the sha it was
        # given and reported applied; report both the apply outcome and
        # the resolve refusal rather than silently dropping either.
        payload = _apply_result_to_dict(result, changed_paths)
        payload["status"] = "error"
        payload["resolve_error"] = resolve_error.get("reason", "")
        return payload

    payload = _apply_result_to_dict(result, changed_paths)
    payload["status"] = "ok"
    return payload


def decline(store: "state_module.StateStore", sha: str) -> Dict[str, Any]:
    """POST pending/{sha}/decline — clears pending, changes nothing else.

    Calls the SAME ``state.resolve_pending`` approve uses (design.md:
    "Approve and decline call a single resolve_pending()") — never
    ``apply_commit``. Refuses identically on the #65 staleness case: a
    stale decline must not silently clear a newer, still-unreviewed
    pending record, which would let that commit's files resurface on the
    next poll tick as if never decided.
    """
    disabled = _require_enabled()
    if disabled is not None:
        return disabled

    resolve_error = _resolve_or_error(store, sha)
    if resolve_error is not None:
        return resolve_error

    return {"status": "ok", "sha": sha}


_CREATED_MANIFEST_NAME = ".created-manifest.json"


def _is_safe_apply_id(apply_id: str) -> bool:
    """Reject an ``apply_id`` that could escape the app's own restores

    directory — the same shape of check ``apply._is_safe_relpath`` runs
    for a commit relpath, applied here to the path SEGMENT a caller
    supplies as ``apply_id`` (never itself a ``/``-separated relpath, so
    a single-segment check is the correct scope: an ``apply_id`` is a
    dict key ``store.restore_dirs`` looks up, not a filesystem path this
    function builds by joining segments).
    """
    normalized = apply_id.replace("\\", "/")
    if not normalized or normalized in (".", ".."):
        return False
    return "/" not in normalized and ".." not in normalized.split("/")


def _restore_manifest_relpaths(restore_dir: Path, root: str) -> List[str]:
    """Read ``restore_dir/<root>/.created-manifest.json``, returning its

    relpath list or an empty list when the manifest is absent or fails to
    parse (an apply that predates this manifest, or one where
    ``apply_commit`` never reached a given root at all, must not make
    restore fail outright — it simply has nothing to remove for that
    root).
    """
    manifest_path = restore_dir / root / _CREATED_MANIFEST_NAME
    if not manifest_path.is_file():
        return []
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    if not isinstance(raw, list):
        return []
    return [entry for entry in raw if isinstance(entry, str)]


def _restore_backed_up_relpaths(restore_dir: Path, root: str) -> List[str]:
    """Enumerate every real backed-up relpath under

    ``restore_dir/<root>/`` — every regular file EXCEPT the created-file
    manifest itself, which sits in the same directory but is metadata,
    never a tracked relpath to restore.
    """
    root_dir = restore_dir / root
    if not root_dir.is_dir():
        return []
    relpaths: List[str] = []
    for path in root_dir.rglob("*"):
        if not path.is_file():
            continue
        relpath = path.relative_to(root_dir).as_posix()
        if relpath == _CREATED_MANIFEST_NAME:
            continue
        relpaths.append(relpath)
    return sorted(relpaths)


def restore(store: "state_module.StateStore", apply_id: str) -> Dict[str, Any]:
    """POST restore/{id} — return the instance to the exact bytes recorded

    before ``apply_id``'s apply, using only the local restore directory
    (design.md: "no second network round trip") — never a git or network
    call.

    Two kinds of relpath are handled, matching what ``apply_commit``
    actually records for a given ``apply_id`` (tasks.md 6.2):

    - Every relpath backed up under ``restore_dir/<root>/<relpath>``
      (``apply.py::_backup_file``) is copied straight back over the live
      file, reported under ``"restored"``.
    - Every relpath named in ``restore_dir/<root>/.created-manifest.json``
      (a file this apply created, with no prior live bytes to back up —
      ``apply.py::_write_created_manifest``) is REMOVED from the live
      root, reported separately under ``"removed"`` — requirements.md 4.7
      means restore returns the EXACT pre-apply state, and the pre-apply
      state for a created file is "did not exist".

    Every relpath from either source is re-resolved through
    ``apply._resolve_target`` before any filesystem write/removal — the
    manifest and the backup tree are both DATA read off disk, not trusted
    instructions, so a tampered manifest entry (a ``..`` segment, an
    absolute path) is refused exactly like a hostile ``changed_paths``
    entry would be refused in ``apply_commit`` itself, rather than being
    joined onto the live root unchecked.

    Idempotent: restoring the same ``apply_id`` twice is safe — the
    second call re-copies the same backup bytes (a no-op rewrite) and
    finds the created files already absent (a no-op removal), rather than
    raising.

    ``status`` is ``"partial"`` — never a silent ``"ok"`` — when any
    individual restore write or created-file removal fails with
    ``OSError`` (senior-review H6): requirements.md 4.7's exact-restore
    guarantee is violated for that one relpath, and the operator must be
    told which one rather than the failure being swallowed by a bare
    ``continue``. Every OTHER relpath still gets its own restore/removal
    attempt regardless of an earlier failure — one bad file must not
    abort the rest of the restore.
    """
    disabled = _require_enabled()
    if disabled is not None:
        return disabled

    if not _is_safe_apply_id(apply_id):
        return {"status": "error", "reason": f"invalid apply id: {apply_id!r}"}

    restore_dir_raw = store.restore_dirs.get(apply_id)
    if restore_dir_raw is None:
        return {
            "status": "error",
            "reason": f"no restore directory recorded for apply id {apply_id!r}",
        }
    restore_dir = Path(restore_dir_raw)

    restored: Dict[str, List[str]] = {root: [] for root in _ROOT_IDS}
    removed: Dict[str, List[str]] = {root: [] for root in _ROOT_IDS}
    failed: List[str] = []

    for root in _ROOT_IDS:
        root_path = _root_path(root)

        for relpath in _restore_backed_up_relpaths(restore_dir, root):
            live_target = _resolve_target(root_path, relpath)
            if live_target is None:
                continue
            backup_path = restore_dir / root / relpath
            if not backup_path.is_file():
                continue
            live_target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{live_target.name}.",
                suffix=".tmp",
                dir=str(live_target.parent),
            )
            os.close(fd)
            tmp_path = Path(tmp_name)
            try:
                tmp_path.write_bytes(backup_path.read_bytes())
                tmp_path.replace(live_target)
            except OSError as exc:
                tmp_path.unlink(missing_ok=True)
                failed.append(f"{apply_id}:{root}:{relpath}: could not restore: {exc}")
                continue
            restored[root].append(relpath)

        for relpath in _restore_manifest_relpaths(restore_dir, root):
            if not _is_safe_relpath(relpath):
                continue
            live_target = _resolve_target(root_path, relpath)
            if live_target is None:
                continue
            try:
                live_target.unlink(missing_ok=True)
            except OSError as exc:
                failed.append(f"{apply_id}:{root}:{relpath}: could not remove: {exc}")
                continue
            removed[root].append(relpath)

    status = "partial" if failed else "ok"
    return {
        "status": status,
        "restored": restored,
        "removed": removed,
        "failed": failed,
    }
