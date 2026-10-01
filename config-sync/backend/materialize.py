"""Materialize a bundle-repo commit's tree, and render an ApplyResult.

Under the operator ruling (the PR merge into ``Kiro-Config-Bundles`` main
IS the approval gate), ``backend/poll.py`` is the only caller of
``_materialize_pending_commit`` for the purpose of an automatic apply —
this module exists so ``poll.py`` can call it without importing
``backend/routes.py``'s HTTP-route layer (which would be a reverse import,
since ``routes.py`` already imports plumbing FROM ``poll.py``).
``routes.py`` still imports from here for its own, unrelated M5
pre-apply "what would this commit change" summary (``status()``'s
pending view), which never calls ``apply_commit``.

Moved out of ``routes.py`` unchanged (Kiro-Config-Bundles auto-apply
migration): ``_MaterializeError``, ``_split_paths_by_root``,
``_materialize_pending_commit``, ``_apply_result_to_dict`` and their
small private helpers (``_relpath_to_root``, ``_root_map_for``,
``_report_to_dict``).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

from backend import allowlist
from backend import state as state_module
from backend.safety import git_safety

#: Root ids a pending record's merged ``classified_paths`` are split
#: across, matching ``poll.py``'s own ``_ROOT_IDS`` — the bundle repo's
#: tree interleaves both roots' relpaths with no per-root prefix, so a
#: pending path is assigned to whichever root's allowlist actually
#: matches it, via the SAME ``allowlist.is_tracked`` probe ``poll.py``'s
#: ``_classify_changed_paths`` already performs.
_ROOT_IDS: Tuple[str, ...] = ("A", "B")


class _MaterializeError(RuntimeError):
    """Raised when a sha cannot be materialized from the bundle repo.

    Caught by the caller (``poll.run()``, or ``routes._pending_changed_
    commands``) and turned into a non-raising outcome — never propagated
    as an unhandled exception.
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
    ``apply.apply_commit``'s own three real inputs. Reuses ``poll.py``'s
    own bundle-repo clone plumbing (``_ensure_bundle_clone``,
    ``_clone_lock``, ``_BUNDLE_CLONE_DIRNAME``) rather than inventing a
    second git code path; the one additional git call this function needs
    (``git archive``) is built through ``git_safety.git_argv`` inline, per
    the single-call-site rule ``tests/safety/test_git_safety.py`` enforces
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
    # Imported lazily (rather than at module scope) to avoid a circular
    # import: `poll.py` imports THIS module, so this module must not
    # import `poll.py` at load time.
    from backend.poll import _BUNDLE_CLONE_DIRNAME, _clone_lock, _ensure_bundle_clone

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
    ``ApplyResult.ignored_paths``/``not_applied`` carry no root id
    attached, so a caller cannot otherwise tell root A's entries apart
    from root B's when the same relpath text could occur under both.
    Kept as a SEPARATE mapping rather than prefixing the relpath strings
    themselves, so ``ignored_paths``/``not_applied`` still carry the
    exact relpath text ``apply_commit`` reported (tests and any existing
    caller match on that text verbatim). A relpath this function cannot
    trace to a root is simply absent from the map.
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

    ``not_applied`` is ``{relpath: reason}`` (requirements.md 4.14) —
    carried through verbatim, never reduced to a bare relpath list.

    ``changed_paths`` (the ``{"A": [...], "B": [...]}`` split
    ``_materialize_pending_commit`` already computed for this apply) is
    optional so a caller with no such split on hand still gets a valid
    dict — the ``"root"`` map is then simply empty rather than raising.
    """
    propagation = getattr(result, "propagation", None)
    not_applied = dict(getattr(result, "not_applied", {}))
    ignored_paths = list(getattr(result, "ignored_paths", []))
    root_map = _root_map_for(list(not_applied) + ignored_paths, changed_paths)
    return {
        "outcome": result.outcome,
        "applied": list(getattr(result, "applied", [])),
        "not_applied": not_applied,
        "ignored_paths": ignored_paths,
        "dropped_cron_names": list(getattr(result, "dropped_cron_names", [])),
        "paused_cron_names": list(getattr(result, "paused_cron_names", [])),
        # Requirement 6.10; the page's LastApply type requires this key and
        # LastApplyCard calls `.find` on it whenever a cron was paused
        # (live-install defect 9: it was never emitted -> page crash).
        "changed_commands": [
            dict(c) for c in getattr(result, "changed_commands", []) or []
        ],
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
