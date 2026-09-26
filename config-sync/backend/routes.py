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

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend import allowlist, collect, push, redact
from backend import state as state_module
from backend.apply import apply_commit
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
    """
    by_root: Dict[str, List[str]] = {root: [] for root in _ROOT_IDS}
    for relpath in relpaths:
        for root in _ROOT_IDS:
            if allowlist.is_tracked(root, relpath):
                by_root[root].append(relpath)
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
            shutil.unpack_archive(str(tar_path), extract_dir=str(commit_root))
        except (shutil.ReadError, OSError) as exc:
            raise _MaterializeError(
                f"could not extract commit {sha}'s tree: {exc}"
            ) from exc
    finally:
        tar_path.unlink(missing_ok=True)

    pending = store.pending
    all_relpaths = list((pending or {}).get("classified_paths", {}).keys())

    changed_paths = _split_paths_by_root(all_relpaths)
    deleted_paths: Dict[str, List[str]] = {root: [] for root in _ROOT_IDS}
    for root, relpaths in changed_paths.items():
        for relpath in relpaths:
            if not (commit_root / relpath).exists():
                deleted_paths[root].append(relpath)

    return commit_root, changed_paths, deleted_paths


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


def status(store: "state_module.StateStore") -> Dict[str, Any]:
    """GET status: push state, drift flag, last-seen SHA, pending summary."""
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
        "pending": store.pending,
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


def _apply_result_to_dict(result: Any) -> Dict[str, Any]:
    """Convert an ``ApplyResult`` into a plain, JSON-serializable dict.

    Never includes raw file content — only outcome, counts, and names —
    so an applied file's (already-restored) live credential value can
    never reach a response body via this path either.
    """
    propagation = getattr(result, "propagation", None)
    return {
        "outcome": result.outcome,
        "applied": list(getattr(result, "applied", [])),
        "not_applied": list(getattr(result, "not_applied", [])),
        "ignored_paths": list(getattr(result, "ignored_paths", [])),
        "dropped_cron_names": list(getattr(result, "dropped_cron_names", [])),
        "paused_cron_names": list(getattr(result, "paused_cron_names", [])),
        "changed_instance_names": list(getattr(result, "changed_instance_names", [])),
        "incomplete_registrations": dict(
            getattr(result, "incomplete_registrations", {})
        ),
        "needs_credential": list(getattr(result, "needs_credential", [])),
        "propagation": _report_to_dict(propagation) if propagation is not None else {},
        "apply_id": getattr(result, "apply_id", None),
        "reason": getattr(result, "reason", ""),
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
    outcome ``"applied"`` or ``"partial"`` — i.e. it actually ran against
    the matching pending commit and attempted real work — never on a
    refusal. Resolving on a refusal would advance ``base_sha``/clear
    ``pending`` for a commit that was never actually applied, silently
    dropping it from what the operator is shown next.
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
        payload = _apply_result_to_dict(result)
        payload["status"] = "error"
        return payload

    resolve_error = _resolve_or_error(store, sha)
    if resolve_error is not None:
        # The #65 staleness race: pending moved between the check above
        # and this call (a poll tick accumulated a newer commit into it
        # mid-apply). apply_commit already ran against the sha it was
        # given and reported applied/partial; report both the apply
        # outcome and the resolve refusal rather than silently dropping
        # either.
        payload = _apply_result_to_dict(result)
        payload["status"] = "error"
        payload["resolve_error"] = resolve_error.get("reason", "")
        return payload

    payload = _apply_result_to_dict(result)
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
