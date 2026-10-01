"""Apply an approved bundle-repo commit to this instance (tasks.md 5.1).

Covers design.md's ``backend/apply.py`` component and requirements.md
4.4, 4.5, 4.7, 4.8, 4.10, Requirement 5, Requirement 6.

Under the operator ruling (the PR merge into ``Kiro-Config-Bundles``
main IS the approval gate — there is no box-side approve/decline step),
``apply_commit`` is called by ``backend/poll.py`` itself, once per poll
tick that finds a new head, immediately after materializing that head's
tree — never from an HTTP route. The caller supplies ``approved_sha``
explicitly and this module refuses unless it matches ``store.pending``.
This module never calls ``store.clear_pending()`` or
``store.advance_base_sha()`` directly — those mutations are owned by
``poll.py`` (via ``state.resolve_pending`` on a full "applied" outcome)
or ``state.record_partial_apply`` (on a "partial" outcome), following
the exact same outcome-driven split the former approve route used.
``apply_commit`` only READS ``store.pending``/``store.base_sha`` for the
approval-SHA gate and only WRITES ``store.restore_dirs`` for the backup
directory it creates.

Step order (design.md apply step-by-step, commit a06f117):

1. Refuse unless a ``pending`` record exists and ``approved_sha`` matches.
2. For every allowlisted file about to be written or deleted: back up its
   current live bytes (if any) into a timestamped restore directory,
   BEFORE any overwrite/delete.
3. Filter the commit's changed paths to the allowlist; a non-allowlisted
   path is reported ignored, never applied.
4. Restore redacted values: for every ``"<redacted>"`` placeholder at a
   ``headers``/``env`` key path, substitute the live file's existing
   value at that same key path. Where no live value exists there, keep
   the placeholder and list the key path in ``needs_credential``.
4a. Sanitize ``crons.json``/``instances.json`` (Requirement 6), AFTER
    restore, so the vet sees exactly the bytes about to be written.
5. Write every file atomically (temp + rename, same directory).
6. Build the per-file propagation report.
7. On partial failure, report applied vs. not-applied; never report the
   apply as an overall success (requirements.md 4.8).

Agent registration (design.md's registration section, requirements.md
5.6) is handled as one refusal unit via ``backend.registration.
check_registrations``: an incomplete registration's own present parts are
blocked from applying while unrelated files in the same commit still
apply.
"""

from __future__ import annotations

import json
import os
import secrets
import shlex
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from backend import allowlist, portable, propagate, redact, registration, sanitize
from backend import state as state_module
from backend.propagate import AppliedFile, ChangeKind
from backend.sanitize import VetCallable

_REDACT_BLOCK_NAMES = ("headers", "env")

_OUTCOME_APPLIED = "applied"
_OUTCOME_REFUSED_SHA_MISMATCH = "refused-sha-mismatch"
_OUTCOME_PARTIAL = "partial"

_ROOT_A_ENV = "KIROCREW_HOME"
_ROOT_B_ENV = "KIRO_HOME"
_ROOT_A_DEFAULT = "~/.kiro/crew"
_ROOT_B_DEFAULT = "~/.kiro"

_CRONS_RELPATH = "crons.json"
_INSTANCES_RELPATH = "instances.json"
_HOOKS_RELPATH = "hooks.json"
_MCP_RELPATH = "mcp.json"


@dataclass(frozen=True)
class ApplyResult:
    """The outcome of one ``apply_commit`` call.

    Attributes:
        outcome: ``"applied"`` (every allowlisted file that needed
            writing/deleting succeeded), ``"refused-sha-mismatch"`` (the
            approval gate refused before touching anything), or
            ``"partial"`` (at least one file failed — never reported as
            ``"applied"``, per requirements.md 4.8).
        applied: Relpaths successfully written or deleted.
        not_applied: ``{relpath: reason}`` for every path that was
            allowlisted and eligible but failed to write/delete — the
            REAL per-path cause (requirements.md 4.14): a malformed-JSON
            parse failure, the specific vet-rejection, the specific
            registration-block reason, a missing-parent-directory or
            permission ``OSError`` string, or a symlink/path-containment
            refusal. Never a generic placeholder — a caller (e.g.
            ``poll.py``'s status summary) surfaces this string verbatim.
        ignored_paths: Relpaths present in the commit but not allowlisted
            (requirements.md 4.5) — refused before any write attempt,
            including a path-traversal or symlink-shaped relpath.
        dropped_cron_names: Cron job names dropped by
            ``sanitize.sanitize_crons``'s vet. Per operator ruling M5,
            also carries every ``hooks.json`` hook name and ``mcp.json``
            server name dropped by ``sanitize.sanitize_hooks``/
            ``sanitize.sanitize_mcp_servers`` — the same fail-closed
            vet, same name-only reporting contract, one combined list.
        paused_cron_names: Cron job names imported paused.
        changed_instance_names: Instance record names forced disconnected.
        changed_commands: Every ADDED or CHANGED ``hooks.json``/
            ``mcp.json`` command surviving the vet, as ``{"file": <hooks.
            json|mcp.json>, "name": <hook/server name>, "command": <the
            shell command/joined command+args string>}`` — an unchanged
            command (identical to the live file's) is never listed
            (operator ruling M5).
        incomplete_registrations: Agent name -> missing-part descriptions,
            per ``registration.check_registrations``.
        needs_credential: Key paths (by server/job name and key) where a
            placeholder was written because no live value existed to
            restore (requirements.md 4.10).
        non_portable_paths: ``"<relpath>:<dotted.json.key.path>"`` entries
            for every applied-file string value, within the Requirement
            2.8 scope, that is an absolute path under NEITHER of this
            host's roots (requirements.md 4.12) — a legacy other-host
            path, or a product-shipped path. Written unchanged; never a
            refusal.
        unresolved_references: ``"<relpath>:<dotted.json.key.path>"``
            entries for every ``file://``/``skill://`` reference, after
            expansion, that resolves to one of this host's own roots but
            whose target does not exist on this host — glob patterns
            checked for at least one match (requirements.md 4.13). Never
            a refusal.
        untracked_prompt_agents: Agent names carried verbatim from
            ``registration.Result.untracked_prompt_agents`` (tasks.md
            7.5) — an agent whose ``prompt`` resolves to an untracked
            relpath, or to neither root.
        propagation_report: Agent name -> requirements.md 5.7 reporting
            string, carried verbatim from
            ``registration.Result.propagation_report`` — present only
            for agents in ``complete_agents`` there, so this is already
            scoped to COMPLETE registrations by the time it reaches this
            field; nothing here re-filters it.
        propagation: The per-applied-file propagation report.
        apply_id: The restore-directory id for this apply, or ``None``
            when the gate refused before any backup was made.
        reason: A short human-readable explanation, set on refusal.
    """

    outcome: str
    applied: List[str] = field(default_factory=list)
    not_applied: Dict[str, str] = field(default_factory=dict)
    ignored_paths: List[str] = field(default_factory=list)
    dropped_cron_names: List[str] = field(default_factory=list)
    paused_cron_names: List[str] = field(default_factory=list)
    changed_instance_names: List[str] = field(default_factory=list)
    changed_commands: List[Dict[str, str]] = field(default_factory=list)
    incomplete_registrations: Dict[str, List[str]] = field(default_factory=dict)
    needs_credential: List[str] = field(default_factory=list)
    non_portable_paths: List[str] = field(default_factory=list)
    unresolved_references: List[str] = field(default_factory=list)
    untracked_prompt_agents: List[str] = field(default_factory=list)
    propagation_report: Dict[str, str] = field(default_factory=dict)
    propagation: propagate.Report = field(
        default_factory=lambda: propagate.Report(entries={})
    )
    apply_id: Optional[str] = None
    reason: str = ""


_ROOT_ENV_DEFAULTS: Dict[str, tuple] = {
    "A": (_ROOT_A_ENV, _ROOT_A_DEFAULT),
    "B": (_ROOT_B_ENV, _ROOT_B_DEFAULT),
}


def _root_path(root: str) -> Path:
    """Resolve the live filesystem root for allowlist root ``"A"``/``"B"``.

    Mirrors ``backend/collect.py``'s own env-var resolution
    (``KIROCREW_HOME`` / ``KIRO_HOME``, each with the same default) rather
    than inventing a second target-root parameter the spec does not name.
    """
    env_name, default = _ROOT_ENV_DEFAULTS[root]
    return Path(os.environ.get(env_name, default)).expanduser()


def _roots_mapping() -> Dict[str, Path]:
    """Root-id -> resolved `Path` mapping, in `portable.py`'s convention.

    Built from this module's own `_root_path` resolution (the same
    KIROCREW_HOME/KIRO_HOME env-var lookup `push._roots_mapping` uses on
    the other side of the seam), so `portable.expand`/`resolve_reference`
    match against exactly the roots files are written under here.
    """
    return {root: _root_path(root) for root in _ROOT_ENV_DEFAULTS}


_GLOB_CHARS: Tuple[str, ...] = ("*", "?", "[")


def _reference_exists_locally(absolute_path_part: str) -> bool:
    """Requirements.md 4.13's existence check for one expanded reference.

    A glob pattern (containing ``*``, ``?`` or ``[``) is checked for AT
    LEAST ONE match via `Path.glob` on its parent directory; a literal
    path is checked with a plain `exists()`. Any path whose parent cannot
    be resolved (e.g. a relative or malformed value slipping through)
    counts as not existing, never raises.
    """
    path = Path(absolute_path_part)
    if not any(char in path.name for char in _GLOB_CHARS):
        try:
            return path.exists()
        except OSError:
            return False
    try:
        return any(path.parent.glob(path.name))
    except OSError:
        return False


def _walk_json_for_portability(
    node: Any,
    roots: Mapping[str, Path],
    relpath: str,
    key_path: List[str],
    non_portable: List[str],
    unresolved: List[str],
) -> None:
    """Walk an EXPANDED JSON document, recording (requirements.md 4.12,

    4.13) every applied string value that is either an absolute path
    under neither of this host's roots (non-portable — written unchanged
    already, by the time this runs) or a `file://`/`skill://` reference
    under one of this host's roots whose target is absent locally
    (unresolved). Dict keys are never inspected as values. Read-only:
    this never rewrites `node`, it only appends report entries.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            _walk_json_for_portability(
                value, roots, relpath, key_path + [str(key)], non_portable, unresolved
            )
        return
    if isinstance(node, list):
        for index, item in enumerate(node):
            _walk_json_for_portability(
                item,
                roots,
                relpath,
                key_path + [str(index)],
                non_portable,
                unresolved,
            )
        return
    if not isinstance(node, str):
        return

    scheme, path_part = portable._split_scheme(node)
    if not path_part.startswith("/"):
        return

    dotted = ".".join(key_path)
    resolved = portable.resolve_reference(node, roots)
    if resolved is None:
        non_portable.append(f"{relpath}:{dotted}")
        return
    if scheme and not _reference_exists_locally(path_part):
        unresolved.append(f"{relpath}:{dotted}")


def _is_safe_relpath(relpath: str) -> bool:
    """Reject a relpath that could escape its root.

    Refuses an absolute path, any ``..`` component, and an empty string.
    Comparison is on the raw ``/``-separated segments — never resolved
    against the filesystem first, so a traversal is caught before any
    path object touches disk.
    """
    normalized = relpath.replace("\\", "/")
    segments = normalized.split("/")
    return (
        bool(normalized)
        and not normalized.startswith("/")
        and (".." not in segments and "" not in segments)
    )


def _resolve_target(root_path: Path, relpath: str) -> Optional[Path]:
    """Resolve ``relpath`` under ``root_path``, refusing any escape.

    Callers only ever reach this with a ``relpath`` that already passed
    ``_is_safe_relpath`` at the allowlist gate, but the escape check here
    is the real guarantee: it resolves both paths and confirms containment
    after resolution, which is what actually matters for a symlinked
    component introduced elsewhere in the tree — including a LIVE-side
    symlinked parent directory (e.g. ``KIROCREW_HOME/steering`` itself
    being a symlink to somewhere outside the root), not only a symlink
    inside the pulled commit tree. Returns ``None`` when the resolved
    candidate is not contained within ``root_path``, or when ``root_path``
    itself cannot be resolved.

    This is the ONE containment check for a live-side path: every
    operation on a given ``(root, relpath)`` — exists/read, backup,
    delete, write — MUST use the single ``Path`` this returns rather than
    re-deriving ``root_path / relpath`` itself, so there is no second,
    unchecked path through which a symlinked directory component could
    still be reached.
    """
    candidate = root_path / relpath
    try:
        resolved_root = root_path.resolve()
        resolved_candidate = candidate.resolve()
    except OSError:
        return None
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError:
        return None
    return candidate


def _is_unsafe_source(commit_root: Path, relpath: str) -> bool:
    """Return True if the commit-tree entry for ``relpath`` must be refused.

    A checked-out commit entry that is itself a symlink — OR that sits
    beneath a symlinked parent directory anywhere between ``commit_root``
    and the leaf — is never followed, since either shape lets the commit
    point content storage outside the tree apply.py is allowed to read.
    ``lstat`` (never ``stat``) inspects each component itself rather than
    resolving through it.
    """
    current = commit_root
    for segment in relpath.split("/"):
        current = current / segment
        if current.is_symlink():
            return True
    return False


def _backup_file(live_path: Path, restore_dir: Path, root: str, relpath: str) -> None:
    """Copy ``live_path``'s current bytes into ``restore_dir/<root>/relpath``.

    Namespaced by ``root`` ("A"/"B") because root A and root B can each
    carry a file at the identical ``relpath`` — without the root
    component two such backups in the same apply would collide at the
    same destination path, and the second write would silently clobber
    the first (Kiro-Config-Bundles error-path review). A no-op when
    ``live_path`` does not exist on disk — there is nothing to back up
    (requirements.md 4.7 only requires a restorable copy of a file
    actually about to be overwritten or deleted). Patched out by
    ``test_apply_never_deletes_a_file_that_was_never_backed_up_first`` to
    prove the delete step never runs ahead of a successful backup.
    """
    if not live_path.exists():
        return
    dest = restore_dir / root / relpath
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(live_path.read_bytes())


_CREATED_MANIFEST_NAME = ".created-manifest.json"


def _write_created_manifest(
    restore_dir: Path, root: str, created_relpaths: List[str]
) -> None:
    """Write the per-root created-file manifest at

    ``restore_dir/<root>/.created-manifest.json`` — a JSON list of every
    relpath this apply created (no prior live bytes, so ``_backup_file``
    never wrote a real backup for it). Written even when the list is
    empty, so a later reader (``routes.restore``) can distinguish "no
    manifest — an older apply, or a bug" from "manifest present, nothing
    was created". Uses the same atomic temp-file + ``os.replace`` pattern
    as ``_atomic_write_bytes``/``state._atomic_write_json`` rather than a
    bare ``write_text``, so a crash mid-write never leaves a truncated
    manifest for restore to (mis)parse later.
    """
    manifest_path = restore_dir / root / _CREATED_MANIFEST_NAME
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(sorted(created_relpaths)).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{_CREATED_MANIFEST_NAME}.",
        suffix=".tmp",
        dir=str(manifest_path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, manifest_path)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise


def _atomic_write_bytes(target: Path, content: bytes) -> None:
    """Write ``content`` to ``target`` via temp file + ``os.replace``,

    in the SAME directory as ``target`` so the rename is atomic on the
    same filesystem. Raises whatever ``os.replace`` raises on failure,
    leaving the temp file cleaned up and the original ``target`` (if any)
    untouched.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise


def _load_json_or_none(content: bytes) -> Any:
    try:
        return json.loads(content.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def _entry_identity(entry: Any) -> Optional[Tuple[str, str]]:
    """Return a ``(kind, value)`` identity key for a list entry, or

    ``None`` when the entry carries neither a usable ``name`` nor ``id``
    (C2, requirements.md 4.10: list-shaped restore must match live-vs-
    commit entries by stable identity, never by list index). ``name`` is
    tried first, ``id`` only as a fallback when ``name`` is absent —
    mirroring ``sanitize._name_of``'s own precedence — and the two kinds
    are namespaced separately so a job named ``"1"`` can never collide
    with a different job whose ``id`` happens to be ``"1"``.
    """
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    if isinstance(name, str) and name:
        return ("name", name)
    entry_id = entry.get("id")
    if isinstance(entry_id, str) and entry_id:
        return ("id", entry_id)
    return None


def _index_live_list_by_identity(live_list: List[Any]) -> Dict[Tuple[str, str], Any]:
    """Index a live list's entries by :func:`_entry_identity`.

    An entry with no usable identity, or an identity that collides with an
    earlier entry's, is simply not indexed — it can never be matched by
    identity, so it is treated the same as "no live counterpart" rather
    than risking a wrong match via index or a later duplicate silently
    winning.
    """
    by_identity: Dict[Tuple[str, str], Any] = {}
    seen: set = set()
    for item in live_list:
        identity = _entry_identity(item)
        if identity is None or identity in seen:
            continue
        seen.add(identity)
        by_identity[identity] = item
    return by_identity


def _restore_redacted_values(
    commit_doc: Any, live_doc: Any, relpath: str, needs_credential: List[str]
) -> Any:
    """Walk ``commit_doc``, restoring the live value at every ``headers``/

    ``env`` key path whose commit value is EXACTLY the placeholder
    (requirements.md 4.10). Restoration is scoped strictly to ``headers``/
    ``env`` objects — every other key path (a cron ``command``, for
    instance) is returned unchanged even when its value happens to equal
    the placeholder string. An exact match only: a value that merely
    contains the substring is never treated as the sentinel.

    ``live_doc`` may be ``None`` (the live file does not exist / does not
    parse) — every placeholder is then left in place and every key path
    is listed in ``needs_credential``, keyed by the nearest enclosing
    object's own dict key (e.g. the server name under ``mcpServers``, or
    the job name under a cron entry) when available, else the bare key.

    C2 (requirements.md 4.10, Kiro-Config-Bundles senior review): when
    ``commit_node``/``live_node`` are both lists, entries are paired by
    stable identity (``name``, then ``id`` — :func:`_entry_identity`),
    never by list index. A commit entry with no live counterpart at that
    identity — because it is new, or because its identity could not be
    determined — restores against ``None`` (i.e. every placeholder in it
    stays a placeholder and is reported in ``needs_credential``), exactly
    as if the whole live document were absent. This is what stops a
    reordered or inserted entry from ever receiving a DIFFERENT entry's
    live credential.
    """

    def _walk(commit_node: Any, live_node: Any, owner_label: str) -> Any:
        if isinstance(commit_node, dict):
            live_map = live_node if isinstance(live_node, dict) else {}
            result: Dict[str, Any] = {}
            for key, value in commit_node.items():
                if key in _REDACT_BLOCK_NAMES and isinstance(value, dict):
                    live_block = live_map.get(key)
                    live_block = live_block if isinstance(live_block, dict) else {}
                    restored: Dict[str, Any] = {}
                    for sub_key, sub_value in value.items():
                        if sub_value == redact.REDACTED:
                            if sub_key in live_block:
                                restored[sub_key] = live_block[sub_key]
                            else:
                                restored[sub_key] = redact.REDACTED
                                needs_credential.append(
                                    f"{relpath}:{owner_label}.{key}.{sub_key}"
                                )
                        else:
                            restored[sub_key] = sub_value
                    result[key] = restored
                else:
                    # Descend with the dict KEY as the new owner label
                    # when the value is itself a mapping (e.g. the server
                    # name under `mcpServers`, or a job's own `name` field
                    # sitting alongside `env` in the same object) — this
                    # is what lets `needs_credential` name the right
                    # server/job rather than only the file path.
                    if isinstance(value, dict) and "name" in value:
                        child_label = str(value["name"])
                    elif isinstance(value, dict):
                        child_label = key
                    else:
                        child_label = owner_label
                    result[key] = _walk(value, live_map.get(key), child_label)
            return result
        if isinstance(commit_node, list):
            live_list = live_node if isinstance(live_node, list) else []
            live_by_identity = _index_live_list_by_identity(live_list)
            walked: List[Any] = []
            for item in commit_node:
                identity = _entry_identity(item)
                live_counterpart = (
                    live_by_identity.get(identity) if identity is not None else None
                )
                # An item's own name/id (when it has one) is a better
                # `needs_credential` label than the parent's — it is
                # what lets an inserted job like "C" be reported as
                # "crons.json:C.env.TOKEN" rather than under the file's
                # bare relpath.
                item_label = str(identity[1]) if identity is not None else owner_label
                walked.append(_walk(item, live_counterpart, item_label))
            return walked
        return commit_node

    return _walk(commit_doc, live_doc, relpath)


def _classify_kind(existed_live: bool) -> ChangeKind:
    """Derive a ``ChangeKind`` for a written (non-deleted) file.

    Only ever called on the write path — the delete path already
    reports ``ChangeKind.removed`` directly and returns before reaching
    here, so this only ever distinguishes an add from a modify.
    """
    return ChangeKind.modified if existed_live else ChangeKind.added


def _frontmatter_triggers(content: bytes) -> Optional[str]:
    """Extract the raw YAML frontmatter block from a SKILL.md's bytes, or

    ``None`` when there is no well-formed ``---``-delimited frontmatter.
    Returned as raw text (not parsed) since only equality between two
    extractions is needed to detect a change.
    """
    text = content.decode("utf-8", errors="replace")
    parts = text.split("---", 2)
    return parts[1] if text.startswith("---") and len(parts) >= 3 else None


def _frontmatter_changed(relpath: str, new_content: bytes, live_path: Path) -> bool:
    if not relpath.endswith("SKILL.md"):
        return False
    new_fm = _frontmatter_triggers(new_content)
    if not live_path.exists():
        return new_fm is not None
    old_fm = _frontmatter_triggers(live_path.read_bytes())
    return new_fm != old_fm


def _changed_hook_commands(
    final_doc: Dict[str, Any], live_doc: Any, relpath: str
) -> List[Dict[str, str]]:
    """Operator ruling M5: every surviving ``hooks.json`` hook whose

    ``command`` is ADDED or CHANGED relative to the live document,
    shaped ``{"file", "name", "command"}``. An unchanged command is
    never listed. Compares by the same name/id identity
    :func:`_entry_identity` uses, so a renamed hook is treated as one
    entry removed and one added rather than silently matched.
    """
    live_hooks = live_doc.get("hooks", []) if isinstance(live_doc, dict) else []
    live_by_identity = _index_live_list_by_identity(
        [h for h in live_hooks if isinstance(h, dict)]
    )
    changed: List[Dict[str, str]] = []
    for hook in final_doc.get("hooks", []):
        if not isinstance(hook, dict):
            continue
        command = hook.get("command")
        if not isinstance(command, str) or not command:
            continue
        identity = _entry_identity(hook)
        live_counterpart = (
            live_by_identity.get(identity) if identity is not None else None
        )
        live_command = live_counterpart.get("command") if live_counterpart else None
        if live_command == command:
            continue
        changed.append(
            {"file": relpath, "name": _hook_display_name(hook), "command": command}
        )
    return changed


def _hook_display_name(hook: Dict[str, Any]) -> str:
    """Return a hook-shaped dict's display name, falling back to id.

    Mirrors ``sanitize._name_of``'s own precedence exactly (name first,
    then id) — duplicated here rather than imported because it operates
    on the applied, already-sanitized document, a different lifecycle
    stage than `sanitize.py`'s own private helper.
    """
    name = hook.get("name")
    return name if isinstance(name, str) and name else str(hook.get("id", ""))


def _mcp_command_string(server: Dict[str, Any]) -> str:
    """Return an ``mcp.json`` server entry's shell-joined ``command`` +

    ``args``, matching ``sanitize._mcp_server_shell_command``'s exact
    join convention so a changed ``args`` entry is detected the same way
    the vet sees it.
    """
    command = server.get("command")
    if not isinstance(command, str) or not command:
        return ""
    args = server.get("args", [])
    args_list = [str(item) for item in args] if isinstance(args, list) else []
    return shlex.join([command, *args_list])


def _changed_mcp_commands(
    final_doc: Dict[str, Any], live_doc: Any, relpath: str
) -> List[Dict[str, str]]:
    """Operator ruling M5: every surviving ``mcp.json`` server whose

    shell-joined ``command`` + ``args`` is ADDED or CHANGED relative to
    the live document, shaped ``{"file", "name", "command"}``. An
    unchanged command is never listed.
    """
    live_servers = live_doc.get("mcpServers", {}) if isinstance(live_doc, dict) else {}
    if not isinstance(live_servers, dict):
        live_servers = {}
    servers = final_doc.get("mcpServers", {})
    if not isinstance(servers, dict):
        return []
    changed: List[Dict[str, str]] = []
    for name, server in servers.items():
        if not isinstance(name, str) or not isinstance(server, dict):
            continue
        command = _mcp_command_string(server)
        if not command:
            continue
        live_server = live_servers.get(name)
        live_command = (
            _mcp_command_string(live_server) if isinstance(live_server, dict) else ""
        )
        if live_command == command:
            continue
        changed.append({"file": relpath, "name": name, "command": command})
    return changed


def _dropped_hook_commands(
    pre_sanitize_doc: Dict[str, Any], dropped_names: List[str], relpath: str
) -> List[Dict[str, str]]:
    """Requirements.md 6.10: every ``hooks.json`` hook the vet DROPPED,

    shaped ``{"file", "name", "command"}`` exactly like
    :func:`_changed_hook_commands`'s survivors — 6.10 lists a dropped
    command "whether it survives the vet or is dropped under 6.9", so
    ``changed_commands`` must carry it too, not only
    ``dropped_cron_names``. Read from ``pre_sanitize_doc`` (the document
    as it stood immediately before ``sanitize_hooks`` removed the
    dropped entries), never from the already-sanitized store, since a
    dropped hook is by definition absent from the sanitized document.
    """
    if not dropped_names:
        return []
    dropped_set = set(dropped_names)
    entries: List[Dict[str, str]] = []
    for hook in pre_sanitize_doc.get("hooks", []):
        if not isinstance(hook, dict):
            continue
        name = _hook_display_name(hook)
        if name not in dropped_set:
            continue
        command = hook.get("command")
        entries.append(
            {
                "file": relpath,
                "name": name,
                "command": command if isinstance(command, str) else str(command),
            }
        )
    return entries


def _dropped_mcp_commands(
    pre_sanitize_doc: Dict[str, Any], dropped_names: List[str], relpath: str
) -> List[Dict[str, str]]:
    """Requirements.md 6.10: every ``mcp.json`` server the vet DROPPED,

    shaped ``{"file", "name", "command"}`` exactly like
    :func:`_changed_mcp_commands`'s survivors. Read from
    ``pre_sanitize_doc`` (the document before ``sanitize_mcp_servers``
    removed the dropped entries) so the command string is still there
    to report.
    """
    if not dropped_names:
        return []
    dropped_set = set(dropped_names)
    servers = pre_sanitize_doc.get("mcpServers", {})
    if not isinstance(servers, dict):
        return []
    entries: List[Dict[str, str]] = []
    for name, server in servers.items():
        if not isinstance(name, str) or name not in dropped_set:
            continue
        if not isinstance(server, dict):
            entries.append({"file": relpath, "name": name, "command": ""})
            continue
        command = server.get("command")
        args = server.get("args", [])
        args_list = [str(item) for item in args] if isinstance(args, list) else []
        command_str = (
            shlex.join([command, *args_list])
            if isinstance(command, str) and command
            else str(command)
        )
        entries.append({"file": relpath, "name": name, "command": command_str})
    return entries


def _split_by_root(paths: Dict[str, List[str]]) -> List[tuple]:
    """Flatten a ``{"A": [...], "B": [...]}`` mapping to ``(root, relpath)``

    pairs, in root-then-order iteration order.
    """
    pairs: List[tuple] = []
    for root in ("A", "B"):
        for relpath in paths.get(root, []):
            pairs.append((root, relpath))
    return pairs


def _new_apply_id() -> str:
    """Generate a per-apply id unique even for two applies within the

    same wall-clock second (Low, senior review). ``time.strftime`` alone
    has one-second resolution, so two applies started in the same second
    would otherwise collide on both ``apply_id`` and restore directory —
    the second apply's backups landing in the first apply's directory.
    The random suffix (4 bytes / 8 hex chars from ``secrets.token_hex``,
    a CSPRNG) disambiguates them; it is a distinctness tool here, not a
    security boundary, but ``secrets`` is used anyway since it is no
    harder to call than ``random`` and never weaker.
    """
    return (
        f"apply-{time.strftime('%Y%m%d%H%M%S', time.gmtime())}-{secrets.token_hex(4)}"
    )


def _vetted_base_crons(
    base_crons_doc: Optional[Dict[str, Any]], cron_vet: Optional[VetCallable]
) -> Optional[Dict[str, Any]]:
    """The base ``crons.json`` as it was ACTUALLY written at its own apply.

    ``sanitize_crons`` drops vet-failing jobs and the file still counts as
    applied, so a raw base over-states what was live: a job dropped then was
    never on this box, and the fleet's fixed version must be ADDED, not
    classed "removed locally" (review round 4, N1(i)). Running the base
    through the same vet reproduces the written document. Works on a copy;
    the caller's document is not mutated. A base of the wrong shape is
    returned as-is — ``merge_crons`` treats it as no base.
    """
    if base_crons_doc is None:
        return None
    jobs = base_crons_doc.get("jobs")
    if not isinstance(jobs, list):
        return base_crons_doc
    copy = dict(base_crons_doc)
    copy["jobs"] = [dict(j) for j in jobs if isinstance(j, dict)]
    return sanitize.sanitize_crons(copy, vet=cron_vet).sanitized_store


def _apply_one_file(
    *,
    root: str,
    relpath: str,
    commit_root: Path,
    restore_dir: Path,
    deleted_by_root: Dict[str, set],
    apply_roots: Dict[str, Path],
    cron_vet: Optional[VetCallable],
    base_crons_doc: Optional[Dict[str, Any]],
    applied: List[str],
    not_applied: Dict[str, str],
    needs_credential: List[str],
    non_portable_paths: List[str],
    unresolved_references: List[str],
    applied_files: List[AppliedFile],
    created_by_root: Dict[str, List[str]],
    dropped_cron_names: List[str],
    paused_cron_names: List[str],
    changed_instance_names: List[str],
    changed_commands: List[Dict[str, str]],
) -> None:
    """Apply (write, restore-placeholder, or delete) exactly one eligible

    (root, relpath) pair, mutating the caller's per-apply accumulator
    lists in place. Every outcome for this single file is recorded into
    ``applied``/``not_applied``/``applied_files`` from directly within
    this function; nothing about a SINGLE file's own decision escapes as
    a return value, so the caller's per-file ``try/except`` (H3) is the
    only boundary a genuine bug (a confirmed-backup ``RuntimeError``, a
    backup ``OSError``, or a sanitize crash on a malformed document) ever
    crosses.

    ``not_applied`` is ``{relpath: reason}`` — every refusal below records
    the REAL cause (requirements.md 4.14), never a generic placeholder.
    """
    root_path = _root_path(root)

    # Resolve and check containment ONCE, before any filesystem touch
    # (exists/read/backup/unlink/write). `live_target` below is the
    # single resolved path every later operation on this file uses —
    # there is no second, unchecked `root_path / relpath` derivation
    # later on. A live-side symlinked directory component (e.g.
    # `steering/` itself pointing outside the root) is caught HERE,
    # before the backup step's `exists()`/read or the delete branch's
    # `unlink()` can ever reach through it.
    live_target = _resolve_target(root_path, relpath)
    if live_target is None:
        not_applied[relpath] = (
            f"{relpath}: refused — the live path escapes root {root}'s "
            "directory (symlink or path-traversal containment check)"
        )
        return

    is_deleted = relpath in deleted_by_root.get(root, set())

    # Step 2: back up before any overwrite/delete. Verified, not just
    # attempted — a mutation that disables `_backup_file` (dropping the
    # backup call while keeping the destructive write/delete) must not
    # be able to slip an unbacked-up file through: confirm the backup
    # copy actually landed on disk before proceeding. A confirmation
    # failure here, or an `OSError` from `_backup_file` itself, is caught
    # by the CALLER's per-file try/except (H3) — never handled here —
    # so it degrades to a `not_applied` entry for this file alone while
    # every earlier file's write and the restore dir stay intact.
    live_existed_before = live_target.exists()
    if live_existed_before:
        _backup_file(live_target, restore_dir, root, relpath)
        backup_copy = restore_dir / root / relpath
        if not backup_copy.is_file():
            raise RuntimeError(
                f"refusing to modify {relpath}: backup was not "
                f"confirmed on disk before the destructive step"
            )

    if is_deleted:
        try:
            if live_target.exists():
                live_target.unlink()
        except OSError as exc:
            not_applied[relpath] = f"{relpath}: delete failed — {exc}"
            return
        applied.append(relpath)
        applied_files.append(
            AppliedFile(root=root, relpath=relpath, kind=ChangeKind.removed)
        )
        return

    source = commit_root / relpath
    if _is_unsafe_source(commit_root, relpath):
        not_applied[relpath] = (
            f"{relpath}: refused — the commit's own tree entry escapes "
            "the materialized commit root (path-traversal check)"
        )
        return
    if not source.exists():
        not_applied[relpath] = f"{relpath}: not found in the materialized commit tree"
        return

    try:
        raw_content = source.read_bytes()
    except OSError as exc:
        not_applied[relpath] = f"{relpath}: could not read from the commit tree — {exc}"
        return

    existed_live = live_existed_before
    content_to_write = raw_content
    frontmatter_changed = False

    if relpath == _CRONS_RELPATH or relpath == _INSTANCES_RELPATH:
        commit_doc = _load_json_or_none(raw_content)
        if commit_doc is None:
            # Fail CLOSED: an unparsable crons.json/instances.json is
            # refused outright, never written through unvetted. The
            # vet/sanitizer exists precisely because these two files are
            # a deliberate, bounded exception (Requirement 6) — a parse
            # failure must not be treated as "safe to apply verbatim",
            # which would bypass that boundary entirely.
            not_applied[relpath] = (
                f"{relpath}: refused — could not parse as JSON (a "
                "crons.json/instances.json file must be valid JSON to "
                "pass the Requirement 6 vet)"
            )
            return
        # Step 4 (design.md): expand tokens to this host's roots BEFORE
        # step 4a's placeholder restore and step 4b's sanitize, so the
        # vet sees the real, host-resolved command (requirements.md
        # 4.11; tasks.md 7.4's ordering test).
        expanded_doc = portable.expand(commit_doc, apply_roots)
        _walk_json_for_portability(
            expanded_doc,
            apply_roots,
            relpath,
            [],
            non_portable_paths,
            unresolved_references,
        )
        live_doc = (
            _load_json_or_none(live_target.read_bytes()) if existed_live else None
        )
        restored_doc = _restore_redacted_values(
            expanded_doc, live_doc, relpath, needs_credential
        )
        # A malformed commit doc (e.g. a top-level LIST instead of the
        # expected `{"jobs": [...]}` mapping) reaches here as a valid
        # JSON value that is simply the wrong shape — `sanitize_crons`/
        # `sanitize_instances` call `.get(...)` on it and raise
        # `AttributeError`. That is caught by the CALLER's per-file
        # try/except (H3), never here, so it degrades to a
        # `not_applied` entry for this file alone.
        if relpath == _CRONS_RELPATH:
            cron_result = sanitize.sanitize_crons(restored_doc, vet=cron_vet)
            dropped_cron_names.extend(cron_result.dropped_job_names)
            if existed_live:
                # Requirement 6.11: MERGE into the live store, never replace
                # it. A live job is kept verbatim (enabled state, grant,
                # bookkeeping); only commit jobs absent live are added. A
                # live file that cannot be merged into is refused rather
                # than overwritten (live-install defect 8: a wholesale write
                # deleted the poll's own operator-granted job).
                if live_doc is None:
                    not_applied[relpath] = (
                        f"{relpath}: refused — the live crons.json could not be "
                        "parsed, so the pulled jobs cannot be merged into it"
                    )
                    return
                try:
                    # N1(i): the base must be what was ACTUALLY written at the
                    # base apply. A job the vet dropped then was never live,
                    # so it must not count as "removed locally" when the fleet
                    # ships a fixed version. Vet the base with the same vet.
                    merge_result = sanitize.merge_crons(
                        live_doc,
                        cron_result.sanitized_store,
                        base=_vetted_base_crons(base_crons_doc, cron_vet),
                    )
                except ValueError as exc:
                    not_applied[relpath] = f"{relpath}: refused — cannot merge: {exc}"
                    return
                added = set(merge_result.added_job_names)
                paused_cron_names.extend(
                    name for name in cron_result.paused_job_names if name in added
                )
                final_doc = merge_result.merged_store
            else:
                paused_cron_names.extend(cron_result.paused_job_names)
                final_doc = cron_result.sanitized_store
        else:
            instance_result = sanitize.sanitize_instances(restored_doc)
            changed_instance_names.extend(instance_result.changed_instance_names)
            final_doc = instance_result.sanitized_store
        content_to_write = (
            json.dumps(final_doc, indent=2, ensure_ascii=False) + "\n"
        ).encode("utf-8")
    else:
        commit_doc = _load_json_or_none(raw_content)
        if commit_doc is not None:
            # Step 4, mirrored for every other in-scope JSON file
            # (requirements.md 4.11-4.13): expand before restore, same
            # as the crons/instances branch above.
            expanded_doc = portable.expand(commit_doc, apply_roots)
            _walk_json_for_portability(
                expanded_doc,
                apply_roots,
                relpath,
                [],
                non_portable_paths,
                unresolved_references,
            )
            live_doc = (
                _load_json_or_none(live_target.read_bytes()) if existed_live else None
            )
            restored_doc = _restore_redacted_values(
                expanded_doc, live_doc, relpath, needs_credential
            )
            # Operator ruling M5: hooks.json/mcp.json pass the SAME
            # shell vet crons.json jobs already pass (`cron_vet`, the
            # identical `VetCallable` seam — not a second, differently-
            # named parameter), run AFTER step 4's expand + 4.10's
            # placeholder restore so the vet sees the real, credential-
            # restored command about to be written. An entry whose vet
            # rejects — or whose vet raises — is dropped and reported by
            # name (fail-closed, `sanitize.sanitize_crons`'s own
            # posture); every other entry in the same file still
            # applies. Every ADDED or CHANGED surviving command
            # (compared to the live document) is recorded into
            # `changed_commands` for operator visibility at approve
            # time.
            if relpath == _HOOKS_RELPATH:
                hook_result = sanitize.sanitize_hooks(restored_doc, vet=cron_vet)
                dropped_cron_names.extend(hook_result.dropped_names)
                command_final_doc: Any = hook_result.sanitized_store
                changed_commands.extend(
                    _changed_hook_commands(command_final_doc, live_doc, relpath)
                )
                changed_commands.extend(
                    _dropped_hook_commands(
                        restored_doc, hook_result.dropped_names, relpath
                    )
                )
                restored_doc = command_final_doc
            elif relpath == _MCP_RELPATH:
                mcp_result = sanitize.sanitize_mcp_servers(restored_doc, vet=cron_vet)
                dropped_cron_names.extend(mcp_result.dropped_names)
                command_final_doc = mcp_result.sanitized_store
                changed_commands.extend(
                    _changed_mcp_commands(command_final_doc, live_doc, relpath)
                )
                changed_commands.extend(
                    _dropped_mcp_commands(
                        restored_doc, mcp_result.dropped_names, relpath
                    )
                )
                restored_doc = command_final_doc
            content_to_write = (
                json.dumps(restored_doc, indent=2, ensure_ascii=False) + "\n"
            ).encode("utf-8")
        elif relpath.endswith("SKILL.md"):
            frontmatter_changed = _frontmatter_changed(
                relpath, raw_content, live_target
            )
        elif relpath.endswith(".json"):
            # H1 (senior review): an allowlisted JSON file — outside the
            # crons/instances Requirement 6 exception — that fails to
            # parse must be REFUSED, never written through raw. Writing
            # it unparsed would skip step 4's placeholder restore
            # entirely, silently destroying whatever live credential
            # restore would otherwise have preserved. Refusing this one
            # file never blocks the rest of the commit's eligible files.
            not_applied[relpath] = f"{relpath}: refused — could not parse as JSON"
            return
        # A non-JSON file (e.g. a SKILL.md body, or any other allowlisted
        # non-JSON asset) is written through as committed — this file
        # class has no placeholder-restore or vet/sanitizer boundary to
        # bypass.

    try:
        _atomic_write_bytes(live_target, content_to_write)
        applied.append(relpath)
        applied_files.append(
            AppliedFile(
                root=root,
                relpath=relpath,
                kind=_classify_kind(existed_live),
                frontmatter_changed=frontmatter_changed,
            )
        )
        if not existed_live:
            created_by_root[root].append(relpath)
    except OSError as exc:
        not_applied[relpath] = f"{relpath}: write failed — {exc}"


def apply_commit(
    *,
    approved_sha: str,
    commit_root: Path,
    changed_paths: Dict[str, List[str]],
    store: "state_module.StateStore",
    deleted_paths: Optional[Dict[str, List[str]]] = None,
    cron_vet: Optional[VetCallable] = None,
    base_crons_doc: Optional[Dict[str, Any]] = None,
) -> ApplyResult:
    """Apply an approved commit's allowlisted files to this instance.

    Args:
        approved_sha: The SHA the caller (an approve route) is asserting
            was approved. Refused unless it matches ``store.pending``'s
            own ``sha`` (requirements.md 4.4 — no automatic application).
        commit_root: Directory holding the approved commit's checked-out
            tree, laid out per root exactly like
            ``registration.check_registrations`` expects.
        changed_paths: ``{"A": [...], "B": [...]}`` — every path changed
            in the commit, relative to its own root, ``/``-separated.
        store: The durable app state store. Read for the approval gate;
            written only via ``record_restore_dir``.
        deleted_paths: ``{"A": [...], "B": [...]}`` — the subset of
            ``changed_paths`` that were deleted upstream (status ``D``),
            for which ``commit_root`` holds no new content. Defaults to
            an empty mapping when omitted.
        cron_vet: Optional override for ``sanitize.sanitize_crons``'s
            shell-command vet; defaults to that module's own default.
        base_crons_doc: The parsed ``crons.json`` at ``state.base_sha`` (the
            last fully-applied commit), when the caller has one — the third
            side of the ``crons.json`` merge (Requirement 6.11): a commit job
            that is in the base but absent live was removed locally and is
            not re-added. ``None`` means no base (first-ever tick, or the
            file was not in the base commit) and every unmatched commit job
            is added.

    Returns:
        An ``ApplyResult`` reflecting exactly what happened. Never reports
        ``outcome="applied"`` when any eligible file failed to write or
        delete (requirements.md 4.8).
    """
    deleted_paths = deleted_paths or {}
    deleted_by_root: Dict[str, set] = {
        "A": set(deleted_paths.get("A", [])),
        "B": set(deleted_paths.get("B", [])),
    }

    pending = store.pending
    if pending is None or pending.get("sha") != approved_sha:
        return ApplyResult(
            outcome=_OUTCOME_REFUSED_SHA_MISMATCH,
            reason="no matching pending commit for the approved sha",
        )

    apply_id = _new_apply_id()
    restore_dir = Path(state_module.get_state_dir()) / "restores" / apply_id

    applied: List[str] = []
    not_applied: Dict[str, str] = {}
    ignored_paths: List[str] = []
    needs_credential: List[str] = []
    non_portable_paths: List[str] = []
    unresolved_references: List[str] = []
    applied_files: List[AppliedFile] = []
    created_by_root: Dict[str, List[str]] = {"A": [], "B": []}
    apply_roots = _roots_mapping()

    all_pairs = _split_by_root(changed_paths)

    # --- Gate 1: allowlist + path-safety filter --------------------------
    eligible: List[tuple] = []
    for root, relpath in all_pairs:
        if not _is_safe_relpath(relpath) or not allowlist.is_tracked(root, relpath):
            ignored_paths.append(relpath)
            continue
        eligible.append((root, relpath))

    # --- Registration handling: refuse incomplete, block their own parts -
    root_a_eligible = {relpath for root, relpath in eligible if root == "A"}
    root_b_eligible = {relpath for root, relpath in eligible if root == "B"}
    registration_roots = {"A": _root_path("A"), "B": _root_path("B")}
    reg_result = registration.check_registrations(
        commit_root, sorted(root_a_eligible | root_b_eligible), registration_roots
    )
    blocked = set(reg_result.blocked_paths)
    if blocked:
        eligible = [
            (root, relpath) for root, relpath in eligible if relpath not in blocked
        ]
        for blocked_relpath in sorted(blocked):
            reasons = [
                f"{agent}: {'; '.join(agent_reasons)}"
                for agent, agent_reasons in sorted(reg_result.incomplete_agents.items())
            ]
            detail = "; ".join(reasons) if reasons else "incomplete agent registration"
            not_applied[blocked_relpath] = (
                f"{blocked_relpath}: refused — part of an incomplete "
                f"agent registration ({detail})"
            )

    # --- Sanitize crons.json / instances.json (Requirement 6) -----------
    # Restoration (step 4) must run before sanitize (step 4a), so this is
    # folded into the per-file loop below rather than a separate pass —
    # sanitize only ever sees the already-restored crons/instances doc.
    dropped_cron_names: List[str] = []
    paused_cron_names: List[str] = []
    changed_instance_names: List[str] = []
    changed_commands: List[Dict[str, str]] = []

    # H3 (senior review): the restore directory is recorded BEFORE the
    # first write, not after the whole loop. Recording it only after
    # every file's write/delete attempt meant that any exception escaping
    # the per-file loop — including one that legitimately propagated all
    # the way out of `apply_commit` — left every earlier file's backup in
    # `restore_dir` with no entry in `store.restore_dirs` pointing at it,
    # so `routes.restore` could never find it. Recording it up front (it
    # is a pure bookkeeping write to the app's OWN state, unconditional on
    # whether any file in this apply actually needs a backup) means a
    # crash on file 2 still leaves file 1's backup reachable, and a
    # creation-only apply — where no live file ever existed to back up —
    # still has a restore dir recorded for its created-manifest.
    try:
        store.record_restore_dir(apply_id=apply_id, restore_dir=str(restore_dir))
    except OSError as exc:
        # Same rationale as before: if this bookkeeping write itself
        # cannot be persisted, no file in this apply can be safely
        # reported as an unqualified success, since restore would have
        # no way to find any of their backups either.
        report = propagate.build_report(applied_files)
        restore_dir_reason = f"restore directory was not recorded: {exc}"
        return ApplyResult(
            outcome=_OUTCOME_PARTIAL,
            not_applied={
                relpath: restore_dir_reason
                for relpath in sorted(
                    {relpath for _root, relpath in eligible} | blocked
                )
            },
            ignored_paths=ignored_paths,
            incomplete_registrations=dict(reg_result.incomplete_agents),
            propagation_report=dict(reg_result.propagation_report),
            propagation=report,
            apply_id=apply_id,
            reason=restore_dir_reason,
        )

    for root, relpath in eligible:
        # H3 (senior review): once an earlier file in THIS apply has
        # already been written or deleted, a failure on a later file must
        # degrade to a `not_applied` entry rather than raise — the
        # earlier write's backup is reachable via the restore dir
        # recorded above, so there is something for the caller to act on
        # even on partial failure. When NOTHING in this apply has
        # succeeded yet, there is no such earlier state to preserve, so
        # the existing "refuse loudly" contract for a first-file failure
        # (`test_apply.py::test_apply_never_deletes_a_file_that_was_never
        # _backed_up_first` — a confirmed-backup `RuntimeError` on the
        # apply's only file must still propagate, proving the delete
        # never ran ahead of a successful backup) is preserved unchanged.
        prior_progress = bool(applied or not_applied)
        try:
            _apply_one_file(
                root=root,
                relpath=relpath,
                commit_root=commit_root,
                restore_dir=restore_dir,
                deleted_by_root=deleted_by_root,
                apply_roots=apply_roots,
                cron_vet=cron_vet,
                base_crons_doc=base_crons_doc,
                applied=applied,
                not_applied=not_applied,
                needs_credential=needs_credential,
                non_portable_paths=non_portable_paths,
                unresolved_references=unresolved_references,
                applied_files=applied_files,
                created_by_root=created_by_root,
                dropped_cron_names=dropped_cron_names,
                paused_cron_names=paused_cron_names,
                changed_instance_names=changed_instance_names,
                changed_commands=changed_commands,
            )
        except Exception as exc:
            # H3: once an earlier file in this apply has already
            # succeeded or been refused, NOTHING raised from a later
            # file's handling may escape this loop — a confirmed-backup
            # `RuntimeError`, an `OSError` from the backup step, or a
            # sanitize crash on a malformed/top-level-list `crons.json`
            # (`sanitize_crons` calls `.get("jobs", [])`, which raises
            # `AttributeError` on a list) must all be caught per file and
            # turned into a `not_applied` entry with the earlier files'
            # writes and the restore dir left intact — never a bare
            # exception reaching the caller, and never silently
            # swallowed with no record of which file or why. `Exception`
            # (not a narrower tuple) is deliberate: this boundary's whole
            # purpose is that ANY failure in one file's processing
            # degrades to "this file was not applied", not a curated
            # subset of exception types.
            #
            # When this is the FIRST file to fail in the whole apply
            # (`prior_progress` is False), the failure is re-raised
            # instead: this preserves `test_apply.py`'s existing
            # mutation-proof contract for the backup-before-destructive-
            # write guard, which asserts that a confirmed-backup
            # `RuntimeError` on an apply's ONLY file still propagates —
            # proving the delete never ran ahead of a successful backup.
            # There is no earlier write to keep reachable in that case,
            # so nothing is lost by keeping the original "refuse loudly"
            # behaviour there.
            if not prior_progress:
                raise
            if relpath not in not_applied:
                not_applied[relpath] = f"{relpath}: unexpected error — {exc}"

    # Write the per-root created-file manifest for every root that had at
    # least one eligible write/delete attempt in this apply — even when
    # nothing was actually created — so `routes.restore` can always tell
    # "no manifest: an older apply, or a bug" apart from "manifest
    # present, nothing was created" (requirements.md 4.7). Written AFTER
    # every file's own write/delete attempt (so it reflects the final
    # `created_by_root`).
    reason = ""
    manifest_error: Optional[str] = None
    touched_roots = {root for root, _relpath in eligible}
    for root in sorted(touched_roots):
        try:
            _write_created_manifest(restore_dir, root, created_by_root[root])
        except OSError as exc:
            manifest_error = f"created-file manifest was not recorded: {exc}"
            break

    if manifest_error is not None:
        # A restore that cannot tell created files apart from modified
        # ones cannot safely return the instance to its exact pre-apply
        # state, so this apply must not be reported as an unqualified
        # success.
        reason = manifest_error
        for p in applied:
            if p not in not_applied:
                not_applied[p] = f"{p}: {manifest_error}"
        applied = []

    report = propagate.build_report(applied_files)

    outcome = _OUTCOME_PARTIAL if not_applied else _OUTCOME_APPLIED

    return ApplyResult(
        outcome=outcome,
        applied=applied,
        not_applied=not_applied,
        ignored_paths=ignored_paths,
        dropped_cron_names=dropped_cron_names,
        paused_cron_names=paused_cron_names,
        changed_instance_names=changed_instance_names,
        changed_commands=changed_commands,
        incomplete_registrations=dict(reg_result.incomplete_agents),
        needs_credential=needs_credential,
        non_portable_paths=non_portable_paths,
        unresolved_references=unresolved_references,
        untracked_prompt_agents=list(reg_result.untracked_prompt_agents),
        propagation_report=dict(reg_result.propagation_report),
        propagation=report,
        apply_id=apply_id,
        reason=reason,
    )
