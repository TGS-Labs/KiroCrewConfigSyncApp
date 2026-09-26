"""Apply an approved bundle-repo commit to this instance (tasks.md 5.1).

Covers design.md's ``backend/apply.py — the applier (backend route,
human-triggered only)`` component and requirements.md 4.4, 4.5, 4.7, 4.8,
4.10, Requirement 5, Requirement 6.

This is called ONLY from an approve route, never from a poll tick
(requirements.md 4.4 — "no automatic application"): the caller supplies
``approved_sha`` explicitly and this module refuses unless it matches
``store.pending``. This module never calls ``store.clear_pending()`` or
``store.advance_base_sha()`` — those two mutations belong exclusively to
``backend/routes.py``'s approve/decline handlers via a single
``state.resolve_pending()`` (tasks.md 6.1). ``apply_commit`` only READS
``store.pending``/``store.base_sha`` for the approval-SHA gate and only
WRITES ``store.restore_dirs`` for the backup directory it creates.

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
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend import allowlist, propagate, redact, registration, sanitize
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
        not_applied: Relpaths that were allowlisted and eligible but
            failed to write/delete.
        ignored_paths: Relpaths present in the commit but not allowlisted
            (requirements.md 4.5) — refused before any write attempt,
            including a path-traversal or symlink-shaped relpath.
        dropped_cron_names: Cron job names dropped by
            ``sanitize.sanitize_crons``'s vet.
        paused_cron_names: Cron job names imported paused.
        changed_instance_names: Instance record names forced disconnected.
        incomplete_registrations: Agent name -> missing-part descriptions,
            per ``registration.check_registrations``.
        needs_credential: Key paths (by server/job name and key) where a
            placeholder was written because no live value existed to
            restore (requirements.md 4.10).
        propagation: The per-applied-file propagation report.
        apply_id: The restore-directory id for this apply, or ``None``
            when the gate refused before any backup was made.
        reason: A short human-readable explanation, set on refusal.
    """

    outcome: str
    applied: List[str] = field(default_factory=list)
    not_applied: List[str] = field(default_factory=list)
    ignored_paths: List[str] = field(default_factory=list)
    dropped_cron_names: List[str] = field(default_factory=list)
    paused_cron_names: List[str] = field(default_factory=list)
    changed_instance_names: List[str] = field(default_factory=list)
    incomplete_registrations: Dict[str, List[str]] = field(default_factory=dict)
    needs_credential: List[str] = field(default_factory=list)
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
            return [
                _walk(item, live_list[i] if i < len(live_list) else None, owner_label)
                for i, item in enumerate(commit_node)
            ]
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


def _split_by_root(paths: Dict[str, List[str]]) -> List[tuple]:
    """Flatten a ``{"A": [...], "B": [...]}`` mapping to ``(root, relpath)``

    pairs, in root-then-order iteration order.
    """
    pairs: List[tuple] = []
    for root in ("A", "B"):
        for relpath in paths.get(root, []):
            pairs.append((root, relpath))
    return pairs


def apply_commit(
    *,
    approved_sha: str,
    commit_root: Path,
    changed_paths: Dict[str, List[str]],
    store: "state_module.StateStore",
    deleted_paths: Optional[Dict[str, List[str]]] = None,
    cron_vet: Optional[VetCallable] = None,
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

    apply_id = f"apply-{time.strftime('%Y%m%d%H%M%S', time.gmtime())}"
    restore_dir = Path(state_module.get_state_dir()) / "restores" / apply_id

    applied: List[str] = []
    not_applied: List[str] = []
    ignored_paths: List[str] = []
    needs_credential: List[str] = []
    applied_files: List[AppliedFile] = []
    backup_made = False
    created_by_root: Dict[str, List[str]] = {"A": [], "B": []}

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
        not_applied.extend(sorted(blocked))

    # --- Sanitize crons.json / instances.json (Requirement 6) -----------
    # Restoration (step 4) must run before sanitize (step 4a), so this is
    # folded into the per-file loop below rather than a separate pass —
    # sanitize only ever sees the already-restored crons/instances doc.
    dropped_cron_names: List[str] = []
    paused_cron_names: List[str] = []
    changed_instance_names: List[str] = []

    for root, relpath in eligible:
        root_path = _root_path(root)

        # Resolve and check containment ONCE, before any filesystem touch
        # (exists/read/backup/unlink/write). `live_target` below is the
        # single resolved path every later operation on this file uses —
        # there is no second, unchecked `root_path / relpath` derivation
        # later in the loop. A live-side symlinked directory component
        # (e.g. `steering/` itself pointing outside the root) is caught
        # HERE, before the backup step's `exists()`/read or the delete
        # branch's `unlink()` can ever reach through it.
        live_target = _resolve_target(root_path, relpath)
        if live_target is None:
            not_applied.append(relpath)
            continue

        is_deleted = relpath in deleted_by_root.get(root, set())

        # Step 2: back up before any overwrite/delete. Verified, not just
        # attempted — a mutation that disables `_backup_file` (dropping
        # the backup call while keeping the destructive write/delete) must
        # not be able to slip an unbacked-up file through: confirm the
        # backup copy actually landed on disk before proceeding.
        live_existed_before = live_target.exists()
        if live_existed_before:
            _backup_file(live_target, restore_dir, root, relpath)
            backup_copy = restore_dir / root / relpath
            if not backup_copy.is_file():
                raise RuntimeError(
                    f"refusing to modify {relpath}: backup was not "
                    f"confirmed on disk before the destructive step"
                )
            backup_made = True

        if is_deleted:
            try:
                if live_target.exists():
                    live_target.unlink()
            except OSError:
                not_applied.append(relpath)
                continue
            applied.append(relpath)
            applied_files.append(
                AppliedFile(root=root, relpath=relpath, kind=ChangeKind.removed)
            )
            continue

        source = commit_root / relpath
        if _is_unsafe_source(commit_root, relpath) or not source.exists():
            not_applied.append(relpath)
            continue

        try:
            raw_content = source.read_bytes()
        except OSError:
            not_applied.append(relpath)
            continue

        existed_live = live_existed_before
        content_to_write = raw_content
        frontmatter_changed = False

        if relpath == _CRONS_RELPATH or relpath == _INSTANCES_RELPATH:
            commit_doc = _load_json_or_none(raw_content)
            if commit_doc is None:
                # Fail CLOSED: an unparsable crons.json/instances.json is
                # refused outright, never written through unvetted. The
                # vet/sanitizer exists precisely because these two files
                # are a deliberate, bounded exception (Requirement 6) —
                # a parse failure must not be treated as "safe to apply
                # verbatim", which would bypass that boundary entirely.
                not_applied.append(relpath)
                continue
            live_doc = (
                _load_json_or_none(live_target.read_bytes()) if existed_live else None
            )
            restored_doc = _restore_redacted_values(
                commit_doc, live_doc, relpath, needs_credential
            )
            if relpath == _CRONS_RELPATH:
                cron_result = sanitize.sanitize_crons(restored_doc, vet=cron_vet)
                dropped_cron_names.extend(cron_result.dropped_job_names)
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
                live_doc = (
                    _load_json_or_none(live_target.read_bytes())
                    if existed_live
                    else None
                )
                restored_doc = _restore_redacted_values(
                    commit_doc, live_doc, relpath, needs_credential
                )
                content_to_write = (
                    json.dumps(restored_doc, indent=2, ensure_ascii=False) + "\n"
                ).encode("utf-8")
            elif relpath.endswith("SKILL.md"):
                frontmatter_changed = _frontmatter_changed(
                    relpath, raw_content, live_target
                )
            # A non-cron/instances JSON file that fails to parse is NOT a
            # placeholder-restore candidate: `commit_doc is None` here
            # means `_restore_redacted_values` never ran, so no key path
            # for this file is ever treated as having been restored —
            # its raw bytes are written through as committed (this file
            # class has no vet/sanitizer boundary to bypass, unlike
            # crons.json/instances.json above).

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
        except OSError:
            not_applied.append(relpath)

    reason = ""

    # Write the per-root created-file manifest for every root that had at
    # least one eligible write/delete attempt in this apply — even when
    # nothing was actually created — so `routes.restore` can always tell
    # "no manifest: an older apply, or a bug" apart from "manifest
    # present, nothing was created" (requirements.md 4.7). Written AFTER
    # every file's own write/delete attempt (so it reflects the final
    # `created_by_root`), but BEFORE `record_restore_dir`, mirroring that
    # call's own "must not be silently swallowed" treatment below: a
    # manifest write failure here is folded into the same guard.
    manifest_error: Optional[str] = None
    touched_roots = {root for root, _relpath in eligible}
    for root in sorted(touched_roots):
        try:
            _write_created_manifest(restore_dir, root, created_by_root[root])
        except OSError as exc:
            manifest_error = f"created-file manifest was not recorded: {exc}"
            break

    if manifest_error is not None:
        # Same rationale as the `record_restore_dir` failure below: a
        # restore that cannot tell created files apart from modified ones
        # cannot safely return the instance to its exact pre-apply state,
        # so this apply must not be reported as an unqualified success.
        reason = manifest_error
        not_applied.extend(p for p in applied if p not in not_applied)
        applied = []
    elif backup_made:
        try:
            store.record_restore_dir(apply_id=apply_id, restore_dir=str(restore_dir))
        except OSError as exc:
            # requirements.md 4.7 exists so a restore is always possible
            # after an apply. If the restore-dir mapping cannot be
            # persisted, the restore route has no way to find this
            # apply's backups, so this apply cannot be reported as an
            # unqualified success even though the files themselves were
            # already safely written to disk.
            reason = f"restore directory was not recorded: {exc}"
            not_applied.extend(p for p in applied if p not in not_applied)
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
        incomplete_registrations=dict(reg_result.incomplete_agents),
        needs_credential=needs_credential,
        propagation=report,
        apply_id=apply_id if (backup_made or applied or not_applied) else apply_id,
        reason=reason,
    )
