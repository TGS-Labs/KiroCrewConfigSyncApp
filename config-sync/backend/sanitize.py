"""Bound the Requirement 6 exception for `crons.json` / `instances.json`.

Covers design.md's ``backend/sanitize.py — the Requirement 6 exception,
bounded`` component and requirements.md Requirement 6, acceptance criteria
6.4-6.7:

- 6.4: every job in a pulled ``crons.json`` that carries a ``command`` is
  vetted by the same shell-command vet used at ``cron_add`` time; a job that
  fails the vet — or whose vet raises — is dropped and reported as dropped.
- 6.5: every surviving job carrying a ``command``, and every job naming a
  ``script``, is imported user-paused (not live); message-only jobs may be
  imported live.
- 6.6: ``instances.json`` records import disconnected regardless of
  ``was_connected`` in the pulled file — no instance is auto-connected as a
  result of an apply.
- 6.7: the apply result lists, by name, every job dropped, every job paused,
  and every instance record added or changed.

This module mirrors the posture of KiroCrew's own
``kiro_crew/portability.py::_sanitize_imported_crons`` (a settings
import/export sanitizer for the same underlying risk — a ``command`` that may
only be safe to run on a different machine) without depending on that
package: this app's own virtual environment does not install ``kiro_crew``,
so nothing here imports it at module scope. The real vet,
``kiro_crew.mcp_cron._vet_shell_command``, is reached only through the
injectable ``vet`` seam; when the caller does not inject one, the default is
resolved lazily (inside the function call, not at import time) and any
failure to resolve or run it is treated as a rejection, never a silent pass.

The vet decides what is unsafe; this module only enforces the consequences
(drop / pause / report) and must not invent a stricter metacharacter
denylist of its own.

Operator ruling M5 extends this same vet contract to two more pulled files
that carry a shell-executable ``command``: ``hooks.json`` (``{"hooks":
[{name, event, matcher, command}]}``, one shell string per hook — no
``args``) and ``mcp.json`` (``{"mcpServers": {name: {command, args, ...}}}``,
vetted against ``command`` + ``args`` joined with ``shlex.join`` so an
injection smuggled only through ``args`` is not missed). An entry whose vet
returns a rejection string, or whose vet raises, is dropped and reported by
name — fail-closed, exactly like a ``crons.json`` job. Other entries in the
same file still apply. :func:`sanitize_hooks` and :func:`sanitize_mcp_servers`
take the identical ``vet: VetCallable | None`` seam as :func:`sanitize_crons`
(the SAME callable ``apply.apply_commit``'s ``cron_vet`` parameter already
threads through, not a second differently-named parameter).
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List

VetCallable = Callable[[str], "str | None"]


def _call_real_vet(command: str) -> str | None:
    """Call the real, already-imported vet and normalize its return type.

    Split out from :func:`_default_vet` only so the (untestable in this
    app's own `kiro_crew`-less `.venv`) import-resolution branch and the
    call/exception-handling branch are each as small as possible; behaviour
    is identical to inlining this at the call site.
    """
    from kiro_crew.mcp_cron import _vet_shell_command

    result = _vet_shell_command(command)
    return str(result) if result is not None else None


def _default_vet(command: str) -> str | None:
    """Resolve and call the real ``kiro_crew`` shell-command vet lazily.

    Imported only inside this function (never at module scope), so a host
    without ``kiro_crew`` installed — this app's own `.venv` included — can
    still import ``backend.sanitize`` freely. Any failure to import or run
    the real vet is treated as a rejection: an unverifiable command must
    fail closed, matching requirements.md 6.4's "whose vet raises SHALL be
    DROPPED".
    """
    try:
        return _call_real_vet(command)
    except Exception:
        return "Error: cron command could not be verified (vet unavailable)"


@dataclass
class CronSanitizeResult:
    """Outcome of sanitizing one pulled ``crons.json`` store.

    Attributes:
        sanitized_store: The ``crons.json``-shaped document to write, with
            every dropped job removed and every surviving executable job
            forced to ``user_paused: True`` / ``enabled: False``.
        dropped_job_names: Names (never internal ids) of every job removed
            because its ``command`` failed the vet or the vet raised.
        paused_job_names: Names of every surviving job forced into paused
            state because it carries a ``command`` or names a ``script``.
    """

    sanitized_store: Dict[str, Any] = field(default_factory=dict)
    dropped_job_names: List[str] = field(default_factory=list)
    paused_job_names: List[str] = field(default_factory=list)


@dataclass
class InstanceSanitizeResult:
    """Outcome of sanitizing one pulled ``instances.json`` store.

    Attributes:
        sanitized_store: The ``instances.json``-shaped document to write,
            with every record's ``was_connected`` forced to ``False``.
        changed_instance_names: Names of every instance record present in
            the store (every record is "changed" by the unconditional
            disconnect rule).
    """

    sanitized_store: Dict[str, Any] = field(default_factory=dict)
    changed_instance_names: List[str] = field(default_factory=list)


def _name_of(job: Dict[str, Any]) -> str:
    """Return a job's human-readable name, falling back to its id.

    The apply result must report by name (6.7), never by internal id; a
    missing ``name`` falls back to ``id`` only so a malformed job never
    raises during reporting.
    """
    name = job.get("name")
    return name if isinstance(name, str) and name else str(job.get("id", ""))


def sanitize_crons(
    store: Dict[str, Any], vet: VetCallable | None = None
) -> CronSanitizeResult:
    """Sanitize a pulled ``crons.json`` store per Requirement 6 (6.4-6.5, 6.7).

    Args:
        store: The parsed ``crons.json`` document, shaped
            ``{"jobs": [...]}``.
        vet: Callable matching the real ``cron_add``-time shell-command vet
            contract — takes a command string, returns ``None`` when clean
            or an ``"Error: ..."`` string naming the refused pattern.
            Defaults to the real ``kiro_crew.mcp_cron._vet_shell_command``,
            resolved lazily, when not given.

    Returns:
        A ``CronSanitizeResult`` whose ``sanitized_store`` never contains a
        dropped job, whose surviving ``command``/``script`` jobs are forced
        paused, and whose two name lists report every drop and pause. An
        empty ``jobs`` list yields an empty, error-free result.
    """
    active_vet: VetCallable = vet if vet is not None else _default_vet

    kept: List[Dict[str, Any]] = []
    dropped_names: List[str] = []
    paused_names: List[str] = []

    for job in store.get("jobs", []):
        command = job.get("command", "")
        script = job.get("script", "")

        if command:
            try:
                reason = active_vet(command)
            except Exception:
                reason = "Error: cron command could not be verified"
            if reason is not None:
                dropped_names.append(_name_of(job))
                continue

        if command or script:
            job["user_paused"] = True
            job["enabled"] = False
            paused_names.append(_name_of(job))

        kept.append(job)

    sanitized_store = dict(store)
    sanitized_store["jobs"] = kept

    return CronSanitizeResult(
        sanitized_store=sanitized_store,
        dropped_job_names=dropped_names,
        paused_job_names=paused_names,
    )


def sanitize_instances(store: Dict[str, Any]) -> InstanceSanitizeResult:
    """Sanitize a pulled ``instances.json`` store per Requirement 6 (6.6-6.7).

    Every record's ``was_connected`` is forced to ``False`` unconditionally
    — regardless of its value or absence in the pulled file — so no apply
    can auto-connect an instance. No other field is touched.

    Args:
        store: The parsed ``instances.json`` document, shaped
            ``{"instances": [...]}``.

    Returns:
        An ``InstanceSanitizeResult`` whose ``sanitized_store`` has every
        record's ``was_connected`` forced ``False``, and whose
        ``changed_instance_names`` names every record present. An empty
        ``instances`` list yields an empty, error-free result.
    """
    changed_names: List[str] = []
    sanitized_records: List[Dict[str, Any]] = []

    for record in store.get("instances", []):
        record["was_connected"] = False
        sanitized_records.append(record)
        changed_names.append(_name_of(record))

    sanitized_store = dict(store)
    sanitized_store["instances"] = sanitized_records

    return InstanceSanitizeResult(
        sanitized_store=sanitized_store,
        changed_instance_names=changed_names,
    )


@dataclass
class CommandSanitizeResult:
    """Outcome of sanitizing one pulled ``hooks.json``/``mcp.json`` store

    (operator ruling M5).

    Attributes:
        sanitized_store: The document to write, with every dropped entry
            removed and every surviving entry otherwise unchanged.
        dropped_names: Names of every hook/server removed because its
            shell-joined command failed the vet or the vet raised.
    """

    sanitized_store: Dict[str, Any] = field(default_factory=dict)
    dropped_names: List[str] = field(default_factory=list)


class _MalformedCommand:
    """Sentinel type: the entry carries a ``command``/``args`` shape the

    vet cannot be run against (a non-string ``command``, a non-list
    ``args``, or an ``args`` element that is not itself a string).
    Distinct from ``""`` ("no command at all, never vetted, always
    kept") — a malformed shape must fail closed exactly like a vet
    rejection (Requirement 6.9), never be silently treated as absent or
    coerced with ``str()``. A dedicated class (rather than a bare
    ``object()``) so call sites can narrow the return type with
    ``isinstance(result, _MalformedCommand)`` instead of an ``is``
    comparison against a module-level constant that mypy cannot use to
    narrow a ``str | object`` union down to ``str``.
    """


_MALFORMED_COMMAND = _MalformedCommand()


def _hook_shell_command(hook: Dict[str, Any]) -> "str | _MalformedCommand":
    """Return a ``hooks.json`` entry's own shell command, ``""`` when it

    carries none, or ``_MALFORMED_COMMAND`` when ``command`` is present
    but not a string. A ``ScriptHook`` has no ``args`` field — its
    ``command`` is already the complete shell string to vet, unlike an
    ``mcp.json`` server entry.
    """
    if "command" not in hook:
        return ""
    command = hook.get("command")
    if command is None:
        return ""
    if not isinstance(command, str):
        return _MALFORMED_COMMAND
    return command


def _mcp_server_shell_command(server: Dict[str, Any]) -> "str | _MalformedCommand":
    """Return an ``mcp.json`` server entry's shell-joined ``command`` +

    ``args``, matching how the real subprocess is actually invoked —
    vetting ``command`` alone would miss an injection smuggled through
    ``args`` (e.g. ``args: ["$(whoami)"]``). Returns ``""`` when the
    server carries no command at all, or ``_MALFORMED_COMMAND`` when
    ``command`` is present but not a string, ``args`` is present but not
    a list, or any ``args`` element is not itself a string — each of
    those shapes must fail closed rather than be silently treated as an
    empty/absent command or args list.
    """
    if "command" not in server:
        return ""
    command = server.get("command")
    if command is None:
        return ""
    if not isinstance(command, str):
        return _MALFORMED_COMMAND
    if not command:
        return ""
    args = server.get("args", [])
    if not isinstance(args, list):
        return _MALFORMED_COMMAND
    if not all(isinstance(item, str) for item in args):
        return _MALFORMED_COMMAND
    return shlex.join([command, *args])


def sanitize_hooks(
    store: Dict[str, Any], vet: VetCallable | None = None
) -> CommandSanitizeResult:
    """Sanitize a pulled ``hooks.json`` store per operator ruling M5.

    Args:
        store: The parsed ``hooks.json`` document, shaped
            ``{"hooks": [...]}``.
        vet: Same contract as :func:`sanitize_crons`'s ``vet`` — takes a
            shell command string, returns ``None`` when clean or an
            ``"Error: ..."`` string when refused. Defaults to the real
            vet, resolved lazily, when not given.

    Returns:
        A ``CommandSanitizeResult`` whose ``sanitized_store`` never
        contains a hook whose command failed the vet (or whose vet
        raised), and whose ``dropped_names`` names each one. A hook with
        no ``command`` is never vetted and always kept.
    """
    active_vet: VetCallable = vet if vet is not None else _default_vet

    kept: List[Dict[str, Any]] = []
    dropped_names: List[str] = []

    for hook in store.get("hooks", []):
        if not isinstance(hook, dict):
            # Not command-shaped (a malformed entry) — nothing here to
            # vet, so it is kept through unchanged rather than crashing;
            # a shape defect in one entry must not block every other
            # entry in the same file, and `apply.py`'s per-file
            # try/except is the boundary for genuine bugs, not a place
            # for this vet to invent its own strictness.
            kept.append(hook)
            continue
        command = _hook_shell_command(hook)
        if isinstance(command, _MalformedCommand):
            dropped_names.append(_name_of(hook))
            continue
        if command:
            try:
                reason = active_vet(command)
            except Exception:
                reason = "Error: hook command could not be verified"
            if reason is not None:
                dropped_names.append(_name_of(hook))
                continue
        kept.append(hook)

    sanitized_store = dict(store)
    sanitized_store["hooks"] = kept

    return CommandSanitizeResult(
        sanitized_store=sanitized_store,
        dropped_names=dropped_names,
    )


def sanitize_mcp_servers(
    store: Dict[str, Any], vet: VetCallable | None = None
) -> CommandSanitizeResult:
    """Sanitize a pulled ``mcp.json`` store per operator ruling M5.

    Args:
        store: The parsed ``mcp.json`` document, shaped
            ``{"mcpServers": {name: {command, args, ...}}}``.
        vet: Same contract as :func:`sanitize_crons`'s ``vet``, called
            against the shell-joined ``command`` + ``args`` string (never
            ``command`` alone). Defaults to the real vet, resolved lazily,
            when not given.

    Returns:
        A ``CommandSanitizeResult`` whose ``sanitized_store`` never
        contains a server whose joined command failed the vet (or whose
        vet raised), and whose ``dropped_names`` names each one. A server
        with no ``command`` is never vetted and always kept. A non-dict
        ``mcpServers`` value is treated as empty.
    """
    active_vet: VetCallable = vet if vet is not None else _default_vet

    servers = store.get("mcpServers", {})
    if not isinstance(servers, dict):
        servers = {}

    kept: Dict[str, Any] = {}
    dropped_names: List[str] = []

    for name, server in servers.items():
        if not isinstance(server, dict):
            kept[name] = server
            continue
        command = _mcp_server_shell_command(server)
        if isinstance(command, _MalformedCommand):
            dropped_names.append(name if isinstance(name, str) else str(name))
            continue
        if command:
            try:
                reason = active_vet(command)
            except Exception:
                reason = "Error: mcp server command could not be verified"
            if reason is not None:
                dropped_names.append(name if isinstance(name, str) else str(name))
                continue
        kept[name] = server

    sanitized_store = dict(store)
    sanitized_store["mcpServers"] = kept

    return CommandSanitizeResult(
        sanitized_store=sanitized_store,
        dropped_names=dropped_names,
    )
