"""Tests for backend/safety/git_safety.py.

Covers design.md's "backend/safety/" component (git_safety.py row) and
tasks.md 2.2, tracing to requirements.md 2.5, 8.4, 8.5:

- Every host-side git invocation this app makes is routed through a single
  `git_argv` wrapper that applies `GIT_SAFE_CONFIG`, so a target repository's
  own `core.hooksPath`, `core.fsmonitor`, attributes file, or excludes file
  cannot execute code at us during clone/commit/push (requirements.md 2.5,
  8.4).
- Symlink / UNC / TOCTOU handling on every path this module touches: a
  symlinked `.git` file/dir, a symlinked `info/` or `attributes`, a
  linked-worktree `.git` file whose `gitdir:` backpointer does not round-trip,
  and a hardlink-swap attempt on `attributes` must all be rejected or
  neutralized rather than followed (requirements.md 8.4, 8.5).
- A static, grep-based test asserting no OTHER module anywhere in the
  config-sync backend spawns `git` or a raw `subprocess` call outside this
  module's `git_argv` helper — the single-call-site guarantee the module's
  own docstring names as its reason for existing.

This module is a direct port of
`kiro_crew/apps/builtins/auto_improvement/spine/git_safety.py` (read as
reference only; never imported from here, and never modified by this task).
The port target `backend/safety/git_safety.py` does not exist yet — every
test below is expected to fail with an ImportError / ModuleNotFoundError
until software-engineer writes it. That is the correct TDD starting state,
not a test defect.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterator

import pytest

from backend.safety import git_safety


# ---------------------------------------------------------------------------
# GIT_SAFE_CONFIG: the config-named vectors must all be disabled, and the
# flags must be usable directly on a git argv (i.e. `-c key=value` pairs
# that precede any subcommand).
# ---------------------------------------------------------------------------


def test_git_safe_config_is_a_tuple_of_strings() -> None:
    cfg = git_safety.GIT_SAFE_CONFIG
    assert isinstance(cfg, tuple)
    assert all(isinstance(part, str) for part in cfg)


def test_git_safe_config_disables_hooks_path() -> None:
    cfg = git_safety.GIT_SAFE_CONFIG
    pairs = _as_dash_c_pairs(cfg)
    assert "core.hooksPath" in pairs, (
        "GIT_SAFE_CONFIG must set core.hooksPath so a repo-written hook "
        "file cannot run on commit/push/checkout"
    )
    # Must point somewhere that cannot resolve to a real hook directory.
    assert pairs["core.hooksPath"] in (os.devnull, "/dev/null")


def test_git_safe_config_disables_fsmonitor() -> None:
    pairs = _as_dash_c_pairs(git_safety.GIT_SAFE_CONFIG)
    assert pairs.get("core.fsmonitor") == "false", (
        "GIT_SAFE_CONFIG must disable core.fsmonitor so a repo-configured "
        "fsmonitor program cannot be spawned by status/diff"
    )


def test_git_safe_config_pins_attributes_file_to_devnull() -> None:
    pairs = _as_dash_c_pairs(git_safety.GIT_SAFE_CONFIG)
    assert pairs.get("core.attributesFile") in (os.devnull, "/dev/null"), (
        "GIT_SAFE_CONFIG must pin core.attributesFile away from any "
        "agent-writable global attributes file"
    )


def test_git_safe_config_pins_excludes_file_to_devnull() -> None:
    pairs = _as_dash_c_pairs(git_safety.GIT_SAFE_CONFIG)
    assert pairs.get("core.excludesFile") in (os.devnull, "/dev/null"), (
        "GIT_SAFE_CONFIG must pin core.excludesFile so git cannot open an "
        "agent-selected UNC/network/FIFO path via a global excludes file"
    )


def test_git_safe_config_disables_submodule_recursion_on_push() -> None:
    pairs = _as_dash_c_pairs(git_safety.GIT_SAFE_CONFIG)
    assert pairs.get("push.recurseSubmodules") == "no", (
        "GIT_SAFE_CONFIG must stop push from recursing into submodule "
        "remotes, since the egress guard validates only the single "
        "superproject origin"
    )


def test_git_safe_config_options_all_use_dash_c_form() -> None:
    """Every setting must be expressed as a `-c key=value` pair (which

    always wins over repo-local config), not as an env var or a file path
    that a repo could shadow.
    """
    cfg = git_safety.GIT_SAFE_CONFIG
    assert len(cfg) % 2 == 0, "every -c must be paired with its key=value"
    for i in range(0, len(cfg), 2):
        assert cfg[i] == "-c", f"expected '-c' at position {i}, got {cfg[i]!r}"
        assert "=" in cfg[i + 1], f"expected key=value at position {i + 1}"


def _as_dash_c_pairs(cfg: tuple[str, ...]) -> dict[str, str]:
    pairs: dict[str, str] = {}
    it = iter(cfg)
    for flag in it:
        assert flag == "-c"
        key, _, value = next(it).partition("=")
        pairs[key] = value
    return pairs


# ---------------------------------------------------------------------------
# git_argv: the call-site helper. Must pin first (fail closed), then return
# `git -C <cwd> <safe-config...> <args...>` verbatim.
# ---------------------------------------------------------------------------


def test_git_argv_returns_git_dash_c_cwd_then_safe_config_then_args(
    tmp_path: Path,
) -> None:
    argv = git_safety.git_argv(tmp_path, "status", "--short")
    assert argv[0] == "git"
    assert argv[1] == "-C"
    assert argv[2] == str(tmp_path)
    assert (
        tuple(argv[3 : 3 + len(git_safety.GIT_SAFE_CONFIG)])
        == git_safety.GIT_SAFE_CONFIG
    )
    assert argv[3 + len(git_safety.GIT_SAFE_CONFIG) :] == ["status", "--short"]


def test_git_argv_accepts_no_extra_args() -> None:
    argv = git_safety.git_argv(".")
    assert argv[0] == "git"
    # No trailing subcommand args beyond -C/cwd/safe-config.
    assert len(argv) == 3 + len(git_safety.GIT_SAFE_CONFIG)


def test_git_argv_pins_attributes_before_returning_on_a_real_repo(
    tmp_path: Path,
) -> None:
    """git_argv must establish the attributes pin as a side effect BEFORE

    handing back the argv — a caller must never be able to run git with a
    hardened argv against a repo whose in-tree `.gitattributes`
    filter/diff driver is still bound.
    """
    root = _init_bare_worktree(tmp_path)
    git_safety.git_argv(root, "status")
    pin_path = root / ".git" / "info" / "attributes"
    assert pin_path.is_file()
    assert pin_path.read_text(encoding="utf-8") == git_safety._ATTRIBUTES_PIN


def test_git_argv_raises_git_safety_error_when_pin_cannot_be_written(
    tmp_path: Path,
) -> None:
    """Fail closed: if the attributes pin cannot be established, git_argv

    must raise rather than return a usable argv, because running git
    unpinned against a real repo would leave any bound filter/diff driver
    live.
    """
    root = _init_bare_worktree(tmp_path)
    info_dir = root / ".git" / "info"
    info_dir.mkdir(parents=True, exist_ok=True)
    # Replace `attributes` with a symlink to an unrelated file, simulating an
    # agent-planted swap. git_argv must refuse to write through it.
    target = tmp_path / "elsewhere.txt"
    target.write_text("not the pin", encoding="utf-8")
    (info_dir / "attributes").symlink_to(target)

    with pytest.raises(git_safety.GitSafetyError):
        git_safety.git_argv(root, "status")

    # The symlink target must be untouched — the module must never write
    # THROUGH a symlink it is refusing to follow.
    assert target.read_text(encoding="utf-8") == "not the pin"


def test_git_argv_on_a_non_repo_path_does_not_raise(tmp_path: Path) -> None:
    """A path with no gitdir (a pre-clone probe, a plain tmp dir) has no

    attribute-execution surface to defend, so git_argv must let it through
    rather than refusing a harmless call.
    """
    argv = git_safety.git_argv(
        tmp_path, "clone", "https://example.invalid/repo.git", "."
    )
    assert argv[0] == "git"
    assert str(tmp_path) in argv


# ---------------------------------------------------------------------------
# require_pinned: the fail-closed primitive git_argv calls first.
# ---------------------------------------------------------------------------


def test_require_pinned_raises_git_safety_error_on_unwritable_info_dir(
    tmp_path: Path,
) -> None:
    root = _init_bare_worktree(tmp_path)
    info_dir = root / ".git" / "info"
    info_dir.mkdir(parents=True, exist_ok=True)
    # Make `info` read-only so writing `attributes` under it fails at the OS
    # level, exercising the "gitdir exists but pin cannot be written" path.
    info_dir.chmod(0o500)
    try:
        with pytest.raises(git_safety.GitSafetyError):
            git_safety.require_pinned(root)
    finally:
        info_dir.chmod(0o700)


def test_require_pinned_is_a_noop_return_on_a_pinned_repo(tmp_path: Path) -> None:
    root = _init_bare_worktree(tmp_path)
    git_safety.require_pinned(root)  # first call establishes the pin
    # Second call must not raise and must leave the pin exactly as written.
    git_safety.require_pinned(root)
    pin_path = root / ".git" / "info" / "attributes"
    assert pin_path.read_text(encoding="utf-8") == git_safety._ATTRIBUTES_PIN


def test_require_pinned_allows_a_path_with_no_gitdir(tmp_path: Path) -> None:
    empty_dir = tmp_path / "not-a-repo"
    empty_dir.mkdir()
    git_safety.require_pinned(empty_dir)  # must not raise


# ---------------------------------------------------------------------------
# pin_attributes: best-effort boolean wrapper.
# ---------------------------------------------------------------------------


def test_pin_attributes_returns_true_on_a_real_repo(tmp_path: Path) -> None:
    root = _init_bare_worktree(tmp_path)
    assert git_safety.pin_attributes(root) is True


def test_pin_attributes_returns_false_on_a_non_repo_path(tmp_path: Path) -> None:
    empty_dir = tmp_path / "not-a-repo"
    empty_dir.mkdir()
    assert git_safety.pin_attributes(empty_dir) is False


def test_pin_attributes_still_raises_on_a_symlink_swap(tmp_path: Path) -> None:
    """A symlink swap is a security event, not a soft miss — the boolean

    wrapper must propagate GitSafetyError rather than swallowing it into
    False.
    """
    root = _init_bare_worktree(tmp_path)
    info_dir = root / ".git" / "info"
    info_dir.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "elsewhere.txt"
    target.write_text("x", encoding="utf-8")
    (info_dir / "attributes").symlink_to(target)

    with pytest.raises(git_safety.GitSafetyError):
        git_safety.pin_attributes(root)


# ---------------------------------------------------------------------------
# Attributes pin content: unbinds filter AND diff drivers while keeping
# diff output readable (the `-diff` vs `diff` distinction the module's
# docstring calls out as previously wrong).
# ---------------------------------------------------------------------------


def test_attributes_pin_unsets_the_filter_attribute() -> None:
    assert "-filter" in git_safety._ATTRIBUTES_PIN


def test_attributes_pin_sets_diff_not_unsets_it() -> None:
    """Regression guard: an earlier version used `-diff`, which marks every

    path binary and blinds a diff-reading credential scanner. The pin must
    SET `diff` (forcing git's built-in textual differ), never unset it.
    """
    tokens = git_safety._ATTRIBUTES_PIN.split()
    assert "diff" in tokens
    assert "-diff" not in tokens


def test_attributes_pin_targets_all_paths() -> None:
    assert git_safety._ATTRIBUTES_PIN.split()[0] == "*"


# ---------------------------------------------------------------------------
# Symlink / reparse-point rejection on every component this module touches.
# ---------------------------------------------------------------------------


def test_reject_link_raises_on_a_symlinked_dot_git(tmp_path: Path) -> None:
    real_gitdir = tmp_path / "real.git"
    real_gitdir.mkdir()
    linked_root = tmp_path / "linked-root"
    linked_root.mkdir()
    (linked_root / ".git").symlink_to(real_gitdir)

    with pytest.raises(git_safety.GitSafetyError):
        git_safety.git_argv(linked_root, "status")


def test_reject_link_raises_on_a_symlinked_info_dir(tmp_path: Path) -> None:
    root = _init_bare_worktree(tmp_path)
    real_info = tmp_path / "real-info"
    real_info.mkdir()
    # `git init` (2.47.3) already creates `.git/info/` as a real directory
    # (containing `exclude`) — remove it first so the symlink can actually
    # be created where the test intends.
    shutil.rmtree(root / ".git" / "info")
    (root / ".git" / "info").symlink_to(real_info)

    with pytest.raises(git_safety.GitSafetyError):
        git_safety.git_argv(root, "status")


def test_reject_link_permits_a_missing_component() -> None:
    """A component that does not exist yet is fine — the module creates it.

    This is not a link, so it must not be treated as one.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        missing = Path(td) / "does" / "not" / "exist"
        # Must not raise: FileNotFoundError on lstat is the expected,
        # tolerated case.
        git_safety._reject_link(missing)


def test_reject_link_raises_on_a_symlinked_attributes_file(tmp_path: Path) -> None:
    root = _init_bare_worktree(tmp_path)
    info_dir = root / ".git" / "info"
    info_dir.mkdir(parents=True, exist_ok=True)
    elsewhere = tmp_path / "not-under-info.txt"
    elsewhere.write_text("x", encoding="utf-8")
    (info_dir / "attributes").symlink_to(elsewhere)

    with pytest.raises(git_safety.GitSafetyError):
        git_safety.require_pinned(root)


# ---------------------------------------------------------------------------
# TOCTOU: the pin must be written atomically (replace the directory entry),
# never truncated in place through whatever inode `attributes` currently
# names — otherwise a hardlink swap lets an agent redirect the write to an
# external file.
# ---------------------------------------------------------------------------


def test_pin_write_does_not_follow_a_hardlink_to_an_external_file(
    tmp_path: Path,
) -> None:
    """If `attributes` is a hardlink to a file OUTSIDE the gitdir, pinning

    must not corrupt that external file by writing through the shared
    inode — it must replace the directory entry so the external inode is
    untouched and the repo's own `attributes` ends up holding the pin.
    """
    root = _init_bare_worktree(tmp_path)
    info_dir = root / ".git" / "info"
    info_dir.mkdir(parents=True, exist_ok=True)

    external = tmp_path / "external-shared.txt"
    external.write_text("do not corrupt me", encoding="utf-8")
    target = info_dir / "attributes"
    try:
        os.link(external, target)
    except OSError:
        pytest.skip("hardlinks not supported on this filesystem")

    git_safety.git_argv(root, "status")

    assert external.read_text(encoding="utf-8") == "do not corrupt me", (
        "the external hardlinked inode must remain untouched — the pin "
        "write must replace the directory entry, not truncate in place"
    )
    assert target.read_text(encoding="utf-8") == git_safety._ATTRIBUTES_PIN
    # After the atomic replace, `attributes` must no longer share the
    # external file's inode.
    assert target.lstat().st_ino != external.lstat().st_ino


def test_pin_write_is_idempotent_and_does_not_rewrite_an_already_correct_pin(
    tmp_path: Path,
) -> None:
    """When the pin is already correctly in place with a single link, the

    module should recognise this and not needlessly replace the file (a
    behavioural detail of `_pin`'s early-return branch), while still
    leaving the correct content in place either way.
    """
    root = _init_bare_worktree(tmp_path)
    git_safety.git_argv(root, "status")
    pin_path = root / ".git" / "info" / "attributes"
    first_stat = pin_path.stat()

    git_safety.git_argv(root, "status")
    second_stat = pin_path.stat()

    assert pin_path.read_text(encoding="utf-8") == git_safety._ATTRIBUTES_PIN
    # Content is correct on both checks; this does not assert immutability
    # of inode number (an atomic replace is also an acceptable
    # implementation), only that content stays correct across repeats.
    assert first_stat.st_size == second_stat.st_size


# ---------------------------------------------------------------------------
# Linked-worktree handling: `.git` is a FILE holding `gitdir: <path>`, and
# the pin must land in the COMMON gitdir (not the per-worktree copy), with
# the bidirectional backpointer validated first.
# ---------------------------------------------------------------------------


def test_linked_worktree_pin_lands_in_the_common_gitdir(tmp_path: Path) -> None:
    common, per_worktree, worktree_root = _init_linked_worktree(tmp_path)

    git_safety.git_argv(worktree_root, "status")

    common_pin = common / "info" / "attributes"
    per_worktree_pin = per_worktree / "info" / "attributes"
    assert common_pin.is_file()
    assert common_pin.read_text(encoding="utf-8") == git_safety._ATTRIBUTES_PIN
    # The per-worktree copy must NOT be where the pin was written — git
    # does not read info/ from there for a linked worktree, and a pin
    # written only there would leave the real attack surface open.
    assert not per_worktree_pin.exists()


def test_linked_worktree_backpointer_mismatch_is_refused(tmp_path: Path) -> None:
    """If the `.git` file's `gitdir:` target's OWN `gitdir` backpointer does

    not resolve back to this worktree's `.git` file, the target has been
    repointed (e.g. at another repository's gitdir) and must be refused
    rather than followed — this is the arbitrary-write primitive the
    bidirectional check exists to close.
    """
    common, per_worktree, worktree_root = _init_linked_worktree(tmp_path)

    # Corrupt the backpointer so it no longer points at this worktree's
    # `.git` file.
    bogus_target = tmp_path / "totally-unrelated-path"
    (per_worktree / "gitdir").write_text(str(bogus_target) + "\n", encoding="utf-8")

    with pytest.raises(git_safety.GitSafetyError):
        git_safety.git_argv(worktree_root, "status")


def test_linked_worktree_missing_backpointer_is_refused(tmp_path: Path) -> None:
    common, per_worktree, worktree_root = _init_linked_worktree(tmp_path)
    (per_worktree / "gitdir").unlink()

    with pytest.raises(git_safety.GitSafetyError):
        git_safety.git_argv(worktree_root, "status")


def test_linked_worktree_dot_git_file_repointed_at_foreign_gitdir_is_refused(
    tmp_path: Path,
) -> None:
    """A `.git` FILE's contents are agent-writable. Repointing it at a

    DIFFERENT repository's real gitdir (which has no backpointer to THIS
    worktree) must be refused rather than silently pinning — and,
    critically, must never truncate/write that foreign gitdir's own
    `info/attributes`.
    """
    _common_a, _per_a, worktree_a = _init_linked_worktree(tmp_path, name="repo-a")
    common_b, _per_b, _worktree_b = _init_linked_worktree(tmp_path, name="repo-b")

    # Repoint repo-a's worktree `.git` file at repo-b's common gitdir, which
    # has no bidirectional pointer back to repo-a's worktree.
    (worktree_a / ".git").write_text(f"gitdir: {common_b}\n", encoding="utf-8")

    foreign_pin_before = common_b / "info" / "attributes"
    foreign_existed_before = foreign_pin_before.exists()
    foreign_content_before = (
        foreign_pin_before.read_text(encoding="utf-8")
        if foreign_existed_before
        else None
    )

    with pytest.raises(git_safety.GitSafetyError):
        git_safety.git_argv(worktree_a, "status")

    # The foreign repo's info/attributes must be exactly as before — never
    # written through the repointed reference.
    if foreign_existed_before:
        assert foreign_pin_before.read_text(encoding="utf-8") == foreign_content_before
    else:
        assert not foreign_pin_before.exists()


def test_linked_worktree_commondir_file_mismatch_is_refused(tmp_path: Path) -> None:
    """If a `commondir` file exists under the per-worktree gitdir but names

    a location OTHER than the layout-derived common dir, that is a
    poisoned copy and must be refused rather than trusted.
    """
    common, per_worktree, worktree_root = _init_linked_worktree(tmp_path)
    bogus_common = tmp_path / "not-the-real-common"
    bogus_common.mkdir()
    (bogus_common / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (per_worktree / "commondir").write_text(str(bogus_common) + "\n", encoding="utf-8")

    with pytest.raises(git_safety.GitSafetyError):
        git_safety.git_argv(worktree_root, "status")


def test_linked_worktree_symlinked_gitdir_target_is_refused(tmp_path: Path) -> None:
    """The `.git` file's `gitdir:` target itself must be link-checked — a

    symlink there is the same class of redirection as a symlinked `.git`.
    """
    common, per_worktree, worktree_root = _init_linked_worktree(tmp_path)
    real_target = per_worktree
    linked_elsewhere = tmp_path / "linked-per-worktree"
    linked_elsewhere.symlink_to(real_target)
    (worktree_root / ".git").write_text(
        f"gitdir: {linked_elsewhere}\n", encoding="utf-8"
    )

    with pytest.raises(git_safety.GitSafetyError):
        git_safety.git_argv(worktree_root, "status")


# ---------------------------------------------------------------------------
# GitSafetyError: fail-closed contract.
# ---------------------------------------------------------------------------


def test_git_safety_error_is_a_runtime_error() -> None:
    assert issubclass(git_safety.GitSafetyError, RuntimeError)


def test_git_safety_error_never_silently_downgrades_to_a_bool_in_require_pinned(
    tmp_path: Path,
) -> None:
    """require_pinned must propagate the raise, not translate it into a

    falsy return the caller might ignore.
    """
    root = _init_bare_worktree(tmp_path)
    info_dir = root / ".git" / "info"
    info_dir.mkdir(parents=True, exist_ok=True)
    (info_dir / "attributes").symlink_to(tmp_path / "nope.txt")

    result: object = None
    try:
        require_pinned: Callable[[Path], object] = git_safety.require_pinned
        result = require_pinned(root)
    except git_safety.GitSafetyError:
        pass
    else:
        pytest.fail(
            f"require_pinned must raise GitSafetyError on a symlink swap, "
            f"not return {result!r}"
        )


# ---------------------------------------------------------------------------
# UNC-shaped path rejection: GIT_SAFE_CONFIG must pin attributes/excludes to
# a spelling git cannot reinterpret as a UNC/network path, and the module
# must not accept a caller-supplied UNC-shaped devnull override.
# ---------------------------------------------------------------------------


def test_attributes_and_excludes_are_pinned_to_a_devnull_spelling_not_unc() -> None:
    pairs = _as_dash_c_pairs(git_safety.GIT_SAFE_CONFIG)
    for key in ("core.attributesFile", "core.excludesFile"):
        value = pairs[key]
        assert not value.startswith("\\\\"), (
            f"{key} must not be a UNC-shaped path ({value!r}) — git-for-"
            "Windows must be able to map this to a real null device"
        )
        assert not re.match(r"^[A-Za-z]:[\\/]", value), (
            f"{key} must not be a drive-letter path controllable by a "
            f"repo-relative trick ({value!r})"
        )


# ---------------------------------------------------------------------------
# Static, grep-based single-call-site guarantee: no OTHER backend module may
# spawn git (or a raw subprocess) outside this module's git_argv helper.
# ---------------------------------------------------------------------------

_BACKEND_ROOT = Path(__file__).resolve().parents[1]  # .../config-sync/backend
_THIS_FILE = Path(__file__).resolve()
_GIT_SAFETY_MODULE = _THIS_FILE.parent / "git_safety.py"

# Matches a subprocess call that spawns a process (Popen/run/call/check_call/
# check_output), or a literal ["git", ...] / "git ..." argv construction
# outside git_safety.py itself.
_SUBPROCESS_SPAWN_RE = re.compile(
    r"\bsubprocess\.(Popen|run|call|check_call|check_output)\s*\("
)
_LITERAL_GIT_ARGV_RE = re.compile(
    r"""(?:\[\s*["']git["']|^\s*["']git\b)""", re.MULTILINE
)


def _iter_backend_python_files() -> Iterator[Path]:
    if not _BACKEND_ROOT.is_dir():
        return
    for path in sorted(_BACKEND_ROOT.rglob("*.py")):
        # Skip this test file, the module under test, and any __pycache__.
        if path == _THIS_FILE or path == _GIT_SAFETY_MODULE:
            continue
        if "__pycache__" in path.parts:
            continue
        yield path


def test_no_backend_module_other_than_git_safety_spawns_subprocess_directly() -> None:
    """Every host-side git invocation must be built via git_safety.git_argv.

    A module that calls subprocess.Popen/run/call/check_call/check_output
    directly bypasses GIT_SAFE_CONFIG and the attributes pin entirely,
    reopening every vector git_safety.py exists to close. This scans every
    .py file under backend/ (excluding git_safety.py and this test file)
    for such a call.
    """
    offenders: list[str] = []
    for path in _iter_backend_python_files():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _SUBPROCESS_SPAWN_RE.search(line):
                offenders.append(
                    f"{path.relative_to(_BACKEND_ROOT.parent)}:{lineno}: {line.strip()}"
                )
    assert not offenders, (
        "found a direct subprocess spawn outside git_safety.py — every git "
        "invocation must go through git_safety.git_argv():\n" + "\n".join(offenders)
    )


def test_no_backend_module_other_than_git_safety_builds_a_literal_git_argv() -> None:
    """A module could dodge the subprocess-call regex above by building a

    ["git", ...] list and handing it to something else (os.execvp, a
    helper, a queued job runner). Catch the literal argv construction too,
    anywhere outside git_safety.py.
    """
    offenders: list[str] = []
    for path in _iter_backend_python_files():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if _LITERAL_GIT_ARGV_RE.search(line):
                offenders.append(
                    f"{path.relative_to(_BACKEND_ROOT.parent)}:{lineno}: {stripped}"
                )
    assert not offenders, (
        "found a literal ['git', ...] argv built outside git_safety.py — "
        "route every git invocation through git_safety.git_argv():\n"
        + "\n".join(offenders)
    )


def test_git_safety_module_itself_is_the_only_declared_git_argv_builder() -> None:
    """Sanity check on the scan's own premise: git_safety.py must actually

    exist (once implemented) and actually contain the subprocess-spawn-
    shaped pattern is irrelevant here — but it MUST define git_argv, so a
    future refactor cannot rename the single call site without this test
    noticing every other module lost its own escape hatch.
    """
    assert hasattr(git_safety, "git_argv")
    assert callable(git_safety.git_argv)


# ---------------------------------------------------------------------------
# Helpers: real git fixtures. These use the actual `git` binary (never
# git_safety's own argv, to avoid testing the module with itself) so the
# scenarios are genuine repos/worktrees, not hand-rolled approximations.
# ---------------------------------------------------------------------------


def _run_git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    )


def _init_bare_worktree(base: Path) -> Path:
    root = base / "repo"
    root.mkdir(exist_ok=True)
    _run_git("init", "-q", cwd=root)
    return root


def _init_linked_worktree(base: Path, name: str = "repo") -> tuple[Path, Path, Path]:
    """Create a normal repo plus one linked worktree off it.

    Returns (common_gitdir, per_worktree_gitdir, worktree_root).
    """
    main_root = base / f"{name}-main"
    main_root.mkdir(exist_ok=True)
    _run_git("init", "-q", cwd=main_root)
    (main_root / "README.md").write_text("x", encoding="utf-8")
    _run_git("add", ".", cwd=main_root)
    _run_git("commit", "-q", "-m", "initial", cwd=main_root)

    worktree_root = base / f"{name}-worktree"
    _run_git(
        "worktree",
        "add",
        "-q",
        "-b",
        f"{name}-wt-branch",
        str(worktree_root),
        cwd=main_root,
    )

    common_gitdir = main_root / ".git"
    per_worktree_gitdir = common_gitdir / "worktrees" / worktree_root.name
    if not per_worktree_gitdir.is_dir():
        # git may pick a de-duplicated id if the name collides; discover it.
        candidates = [p for p in (common_gitdir / "worktrees").iterdir() if p.is_dir()]
        assert len(candidates) == 1, candidates
        per_worktree_gitdir = candidates[0]

    return common_gitdir, per_worktree_gitdir, worktree_root


if sys.platform.startswith("win"):
    pytest.skip(
        "these fixtures assume POSIX symlink/hardlink semantics",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------
# Coverage gaps: OSError branches in _atomic_write / _reject_link /
# _resolve_gitdir / _common_gitdir that are hard to hit with real fixtures
# alone, plus the pure-branch cases (malformed `.git` file, root named
# `.git`, non-`worktrees`-parented gitdir).
# ---------------------------------------------------------------------------


def test_atomic_write_cleans_up_temp_file_when_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If writing/replacing the temp file raises, the temp file is unlinked
    (best-effort) and the original exception propagates -- confirming the
    cleanup-on-failure branch of _atomic_write, not just its happy path."""
    root = _init_bare_worktree(tmp_path)
    info_dir = root / ".git" / "info"
    info_dir.mkdir(parents=True, exist_ok=True)

    real_replace = os.replace

    def _failing_replace(*args: object, **kwargs: object) -> object:
        raise OSError("simulated: replace failed")

    monkeypatch.setattr(os, "replace", _failing_replace)

    with pytest.raises(git_safety.GitSafetyError, match="could not write"):
        git_safety.git_argv(root, "status")

    monkeypatch.setattr(os, "replace", real_replace)
    # No leftover .tmp file from the failed write.
    leftovers = list(info_dir.glob("*.tmp"))
    assert leftovers == [], f"temp file(s) not cleaned up: {leftovers}"


def test_atomic_write_swallows_a_failed_cleanup_unlink_and_still_raises_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When BOTH the write/replace step AND the best-effort cleanup unlink
    fail, the cleanup failure is swallowed (not masking the original error)
    and the original write/replace exception still propagates."""
    root = _init_bare_worktree(tmp_path)
    info_dir = root / ".git" / "info"
    info_dir.mkdir(parents=True, exist_ok=True)

    real_replace = os.replace
    real_unlink = os.unlink

    def _failing_replace(*args: object, **kwargs: object) -> object:
        raise OSError("simulated: replace failed")

    def _failing_unlink(*args: object, **kwargs: object) -> object:
        raise OSError("simulated: cleanup unlink also failed")

    monkeypatch.setattr(os, "replace", _failing_replace)
    monkeypatch.setattr(os, "unlink", _failing_unlink)

    with pytest.raises(git_safety.GitSafetyError, match="could not write"):
        git_safety.git_argv(root, "status")

    monkeypatch.setattr(os, "replace", real_replace)
    monkeypatch.setattr(os, "unlink", real_unlink)


def test_reject_link_raises_git_safety_error_on_permission_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-FileNotFoundError OSError from lstat() (e.g. permission denied
    walking a parent directory) must raise GitSafetyError, not be treated
    the same as a merely-missing path."""
    target = tmp_path / "some-path"

    def _failing_lstat(self: Path) -> object:
        raise PermissionError("simulated: permission denied")

    monkeypatch.setattr(Path, "lstat", _failing_lstat)

    with pytest.raises(git_safety.GitSafetyError, match="cannot stat"):
        git_safety._reject_link(target)


def test_resolve_gitdir_returns_none_when_dot_git_file_has_no_gitdir_text(
    tmp_path: Path,
) -> None:
    """A `.git` FILE whose content does not contain `gitdir:` at all (a
    corrupt or unexpected file, not the standard linked-worktree shape) is
    treated as "not a git tree" -- None -- rather than raised on."""
    root = tmp_path / "odd-root"
    root.mkdir()
    (root / ".git").write_text("not a gitdir pointer at all", encoding="utf-8")

    assert git_safety._resolve_gitdir(root) is None


def test_resolve_gitdir_returns_none_when_gitdir_target_is_not_a_directory(
    tmp_path: Path,
) -> None:
    """A `.git` FILE's `gitdir:` target that does not resolve to an actual
    directory (e.g. it names a file, or nothing) is treated as "not a git
    tree" rather than raised on -- there is nothing there to validate."""
    root = tmp_path / "odd-root"
    root.mkdir()
    not_a_dir = tmp_path / "just-a-file.txt"
    not_a_dir.write_text("x", encoding="utf-8")
    (root / ".git").write_text(f"gitdir: {not_a_dir}\n", encoding="utf-8")

    assert git_safety._resolve_gitdir(root) is None


def test_resolve_gitdir_raises_when_backpointer_read_raises_os_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If reading the `gitdir` backpointer file raises an OSError other than
    it being simply absent (e.g. a transient I/O error), _resolve_gitdir
    must still refuse (GitSafetyError) rather than let the exception
    surface as an unrelated crash or silently proceed unvalidated."""
    common, per_worktree, worktree_root = _init_linked_worktree(tmp_path)
    backptr = per_worktree / "gitdir"
    assert backptr.is_file()

    real_read_text = Path.read_text

    def _flaky_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self == backptr:
            raise OSError("simulated: I/O error reading backpointer")
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", _flaky_read_text)

    with pytest.raises(git_safety.GitSafetyError, match="no `gitdir` backpointer"):
        git_safety.git_argv(worktree_root, "status")


def test_resolve_gitdir_raises_when_backpointer_resolve_raises_os_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If resolving the backpointer's path raises OSError (e.g. an ELOOP-
    shaped symlink cycle), the mismatch must be treated as a refusal, not
    let the exception propagate uncaught or be treated as a match."""
    common, per_worktree, worktree_root = _init_linked_worktree(tmp_path)

    real_resolve = Path.resolve
    dot_git = worktree_root / ".git"

    def _flaky_resolve(self: Path, *args: object, **kwargs: object) -> Path:
        if self == dot_git:
            raise OSError("simulated: cannot resolve (e.g. symlink loop)")
        return real_resolve(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "resolve", _flaky_resolve)

    with pytest.raises(git_safety.GitSafetyError, match="backpoints to"):
        git_safety.git_argv(worktree_root, "status")


def test_resolve_gitdir_accepts_the_gitdir_itself_named_dot_git(
    tmp_path: Path,
) -> None:
    """A caller may pass the gitdir itself rather than its owning worktree
    root -- the `root.name == ".git"` branch. Confirmed by pointing
    git_argv directly at a real repo's `.git` directory."""
    worktree_root = _init_bare_worktree(tmp_path)
    dot_git = worktree_root / ".git"

    argv = git_safety.git_argv(dot_git, "status")

    assert argv[0] == "git"
    pin_path = dot_git / "info" / "attributes"
    assert pin_path.read_text(encoding="utf-8") == git_safety._ATTRIBUTES_PIN


def test_resolve_gitdir_returns_none_when_root_named_dot_git_is_not_a_dir(
    tmp_path: Path,
) -> None:
    """A path literally named `.git` that does not exist as a directory
    (git_argv's `root.name == ".git"` branch, taken when there is nothing
    at that path at all) is "not a git tree" -- None, not raised on."""
    odd = tmp_path / "owner" / ".git"

    assert git_safety._resolve_gitdir(odd) is None


def test_common_gitdir_raises_when_per_worktree_parent_is_not_worktrees(
    tmp_path: Path,
) -> None:
    """_common_gitdir refuses a per-worktree path whose parent directory is
    not literally named `worktrees` -- the layout assumption it relies on
    to derive the common gitdir without trusting the `commondir` file."""
    fake_per_worktree = tmp_path / "not-worktrees-dir" / "some-id"
    fake_per_worktree.mkdir(parents=True)

    with pytest.raises(git_safety.GitSafetyError, match="not under a"):
        git_safety._common_gitdir(fake_per_worktree)


def test_common_gitdir_raises_when_derived_common_dir_is_not_a_real_gitdir(
    tmp_path: Path,
) -> None:
    """If the layout-derived common dir does not exist, isn't a directory,
    or lacks a HEAD file, _common_gitdir refuses rather than trusting a
    directory that merely happens to sit at the expected path."""
    fake_worktrees_parent = tmp_path / "fake-common" / "worktrees"
    fake_worktrees_parent.mkdir(parents=True)
    fake_per_worktree = fake_worktrees_parent / "some-id"
    fake_per_worktree.mkdir()
    # fake-common has no HEAD file, so it fails the "is a real gitdir" check.

    with pytest.raises(git_safety.GitSafetyError, match="is not a git directory"):
        git_safety._common_gitdir(fake_per_worktree)


def test_common_gitdir_accepts_a_valid_worktree_with_no_commondir_file(
    tmp_path: Path,
) -> None:
    """When no `commondir` file is present at all under the per-worktree
    gitdir, _common_gitdir must still succeed using the layout-derived
    common dir alone -- the cross-check block is opportunistic, not
    mandatory, so its absence must not be treated as a mismatch."""
    common, per_worktree, worktree_root = _init_linked_worktree(tmp_path)
    commondir_file = per_worktree / "commondir"
    assert commondir_file.is_file(), "a real `git worktree add` always writes one"
    commondir_file.unlink()

    resolved = git_safety._common_gitdir(per_worktree)

    assert resolved == common


def test_common_gitdir_raises_when_commondir_file_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `commondir` file that exists but raises OSError on read (rather
    than being cleanly absent) must be refused, not silently skipped."""
    common, per_worktree, worktree_root = _init_linked_worktree(tmp_path)
    commondir_file = per_worktree / "commondir"
    assert commondir_file.is_file()

    real_read_text = Path.read_text

    def _flaky_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self == commondir_file:
            raise OSError("simulated: I/O error reading commondir")
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", _flaky_read_text)

    with pytest.raises(git_safety.GitSafetyError, match="cannot read commondir"):
        git_safety.git_argv(worktree_root, "status")


def test_common_gitdir_treats_unresolvable_common_path_as_disagreement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If resolving the layout-derived common path raises OSError while
    cross-checking it against a present `commondir` file, that must be
    treated as a disagreement (refused), never as an implicit match."""
    common, per_worktree, worktree_root = _init_linked_worktree(tmp_path)

    real_resolve = Path.resolve

    def _flaky_resolve(self: Path, *args: object, **kwargs: object) -> Path:
        if self == common:
            raise OSError("simulated: cannot resolve common gitdir")
        return real_resolve(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "resolve", _flaky_resolve)

    with pytest.raises(git_safety.GitSafetyError, match="not the"):
        git_safety.git_argv(worktree_root, "status")
