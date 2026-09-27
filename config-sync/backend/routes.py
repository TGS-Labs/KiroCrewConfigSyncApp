"""Backend routes for config-sync (tasks.md 6.1; design.md's routes section).

Covers requirements.md 4.9, 7.1, 7.2, 7.4, 7.5.

Plain, framework-agnostic functions — no web framework dependency;
`backend/server.py` (or a future dispatcher) is the thing that maps HTTP
verbs/paths onto these:

    status(store: StateStore) -> dict
    drift(store: StateStore) -> dict
    push_now(store: StateStore) -> dict
    restore(store: StateStore, apply_id: str) -> dict

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

Under the operator ruling, the PR merge into ``Kiro-Config-Bundles`` main
IS the approval gate — there is no box-side approve/decline route.
``backend/poll.py`` itself materializes and applies every new head
automatically (see that module and ``backend/apply.py``); this module no
longer defines ``approve``/``decline`` at all (removed, along with their
``_resolve_or_error`` helper), and ``backend/server.py`` 404s the former
routes. ``status()``'s pending summary still surfaces a still-retrying
(partial) commit's per-path not-applied reasons and the M5
"what commands would change" view, computed via
``backend.materialize._materialize_pending_commit`` — the same
materialize helper ``poll.py`` uses for the real apply, moved to its own
module so ``poll.py`` can call it without importing this HTTP-route
layer.

No response from any route ever carries an unredacted credential
(requirements.md 3.8, 7.5): ``status``/``drift`` build their view from
``collect.collect()`` + ``redact.redact()`` (never live, unredacted file
bytes), and ``push_now`` returns only counts, names, and outcome strings
— never raw file content.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend import collect, push, redact
from backend import state as state_module
from backend.apply import _HOOKS_RELPATH, _is_safe_relpath, _resolve_target, _root_path
from backend.materialize import _materialize_pending_commit, _MaterializeError
from backend.push import run as push_run

_APP_NAME = "config-sync"

#: Root ids `restore()` iterates over — matches `poll.py`'s own
#: `_ROOT_IDS` / `materialize.py`'s copy of the same constant.
_ROOT_IDS: Tuple[str, ...] = ("A", "B")

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
    """GET status: push state, drift flag, last-seen SHA, last-apply

    summary, pending (still-retrying) summary.

    ``last_apply`` (dashboard) reports the most recent automatic apply
    ``poll.py`` ran: applied sha, not-applied paths with their real
    per-path reasons, paused cron names, needs-credential entries, and
    the command checked for each — the auto-apply replacement for what
    the former approve response used to return inline.

    The pending summary (M5) additionally carries ``changed_commands`` —
    every added/changed ``hooks.json``/``mcp.json`` command entry for a
    still-retrying (partial) commit, computed by comparing its
    materialized tree against the live files (see
    ``_pending_changed_commands``). A materialization failure is reported
    via ``changed_commands_error`` rather than raising.
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
        "last_apply": store.last_apply,
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
