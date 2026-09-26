"""Host-independent configuration: root-path <-> token rewriting.

Design.md's `backend/portable.py` component (requirements.md 2.8-2.11,
4.11-4.13, 5.12). Three pure functions, no I/O except `resolve_reference`'s
existence/glob check:

    tokenize(doc, roots) -> new doc, absolute root paths replaced by tokens.
    expand(doc, roots) -> new doc, tokens replaced by this host's root paths.
    resolve_reference(value, roots) -> (root_id, relpath) or None.

``roots`` is ``{"A": Path(...), "B": Path(...)}`` — the same root-id
convention `backend.collect._roots()` uses: string ids mapping to resolved,
``~``-expanded, no-trailing-slash `Path` objects. Root A is tried before root
B because the default root A (``KIROCREW_HOME``) lies inside root B
(``KIRO_HOME``); trying B first would let it shadow every value actually
under A.

Both `tokenize` and `expand` operate on *one already-parsed JSON document*
of any shape (dict/list/str/int/float/bool/None nesting) — never a
``{relpath: bytes}`` mapping, which is collect.py/redact.py's layer. Neither
mutates its input; both return a new document. Only string *values* are ever
rewritten — dict *keys* are always returned unchanged. All three functions
are pure string/data transforms with no filesystem I/O; the existence/glob
check requirements.md 4.13 asks for on an unresolved reference is the
caller's (apply.py's) responsibility, not this module's.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

_SCHEMES: Tuple[str, ...] = ("file://", "skill://")

# Root A before root B: the default root A (KIROCREW_HOME) is nested inside
# root B (KIRO_HOME), so trying B first would shadow every A-rooted value.
_ROOT_ORDER: Tuple[str, ...] = ("A", "B")

_TOKEN_BY_ROOT: Mapping[str, str] = {
    "A": "${KIROCREW_HOME}",
    "B": "${KIRO_HOME}",
}
_ROOT_BY_TOKEN: Mapping[str, str] = {
    token: root_id for root_id, token in _TOKEN_BY_ROOT.items()
}


def _split_scheme(value: str) -> Tuple[str, str]:
    """Strip an optional leading ``file://``/``skill://`` scheme.

    Returns ``(scheme, path_part)`` where ``scheme`` is ``""`` when no
    recognised scheme prefix is present.
    """
    for scheme in _SCHEMES:
        if value.startswith(scheme):
            return scheme, value[len(scheme) :]
    return "", value


def _ordered_roots(roots: Mapping[str, Path]) -> Tuple[Tuple[str, Path], ...]:
    """Roots in match-precedence order (A before B), skipping absent ids."""
    return tuple(
        (root_id, roots[root_id]) for root_id in _ROOT_ORDER if root_id in roots
    )


def _match_root_prefix(path_part: str, root_str: str) -> Optional[str]:
    """WHEN ``path_part == root_str`` or starts with ``root_str + "/"``,

    return the remainder relpath (``""`` for an exact match). Anchored at
    the START of ``path_part`` only — a root string appearing elsewhere in
    the value, or followed by a character other than ``/``, is not a match.

    Implemented as a single prefix test against ``root_str + "/"`` applied
    to ``path_part + "/"`` — this folds the "exact match" and "match with a
    trailing segment" cases into one comparison with no separate branch for
    the exact-match case: appending then stripping one trailing ``/`` makes
    both cases the same code path, with an empty remainder for the exact
    match.
    """
    prefix = root_str + "/"
    candidate = path_part + "/"
    if not candidate.startswith(prefix):
        return None
    return candidate[len(prefix) : -1]


def _tokenize_value(value: str, roots: Mapping[str, Path]) -> str:
    scheme, path_part = _split_scheme(value)
    for root_id, root_path in _ordered_roots(roots):
        root_str = str(root_path)
        remainder = _match_root_prefix(path_part, root_str)
        if remainder is None:
            continue
        token = _TOKEN_BY_ROOT[root_id]
        new_path = token if remainder == "" else f"{token}/{remainder}"
        return f"{scheme}{new_path}"
    return value


def _expand_value(value: str, roots: Mapping[str, Path]) -> str:
    scheme, path_part = _split_scheme(value)
    for token, root_id in _ROOT_BY_TOKEN.items():
        if root_id not in roots:
            continue
        remainder = _match_root_prefix(path_part, token)
        if remainder is None:
            continue
        root_str = str(roots[root_id])
        new_path = root_str if remainder == "" else f"{root_str}/{remainder}"
        return f"{scheme}{new_path}"
    return value


def _walk(node: Any, rewrite: Any) -> Any:
    """Recurse through a JSON-compatible structure, rewriting string

    values only. Dict keys are always returned unchanged. Returns a new
    structure; never mutates ``node``.
    """
    if isinstance(node, dict):
        return {key: _walk(val, rewrite) for key, val in node.items()}
    if isinstance(node, list):
        return [_walk(item, rewrite) for item in node]
    if isinstance(node, str):
        return rewrite(node)
    return node


def tokenize(doc: Any, roots: Mapping[str, Path]) -> Any:
    """Return a new document with every in-scope string value's leading

    root path replaced by its portable token form (requirements.md
    2.8-2.10). A value under neither root, a relative reference, or a
    non-path value (e.g. a URL) is returned unchanged. Dict keys are never
    rewritten. Idempotent: tokenizing already-tokenized content is a no-op
    because a token string never matches a root's absolute path prefix.
    """
    return _walk(doc, lambda value: _tokenize_value(value, roots))


def expand(doc: Any, roots: Mapping[str, Path]) -> Any:
    """Return a new document with every in-scope string value's leading

    token replaced by this host's own root path (requirements.md
    4.11-4.12). A token appearing anywhere other than the start of the
    value is left unexpanded. A legacy absolute path from another host (not
    a token, not under either of this host's roots) is left unchanged.
    Dict keys are never rewritten. Idempotent: expanding already-expanded
    content is a no-op because an absolute path never matches a token
    prefix.
    """
    return _walk(doc, lambda value: _expand_value(value, roots))


def resolve_reference(
    value: str, roots: Mapping[str, Path]
) -> Optional[Tuple[str, str]]:
    """Resolve ``value`` to ``(root_id, relpath)`` under one of ``roots``.

    Accepts both the token form (``file://${KIROCREW_HOME}/...``) and this
    host's own absolute form (``file:///abs/path/...``); root A is tried
    before root B. Returns ``None`` for a value under neither root, a
    relative reference, or a non-path value (e.g. a URL) — including the
    literal ``"<redacted>"`` placeholder, which never starts with a root or
    a token.

    The mapping is purely string-based: a resolvable value always maps to
    ``(root_id, relpath)`` regardless of whether ``relpath`` (which may be
    a glob pattern) has any match on disk. Requirements.md 4.13 delegates
    the zero-match "unresolved" judgement to the caller (apply.py) — this
    function never fails the mapping because nothing exists.
    """
    scheme, path_part = _split_scheme(value)
    del scheme  # scheme does not affect resolution, only display form

    for root_id, root_path in _ordered_roots(roots):
        remainder = _match_root_prefix(path_part, str(root_path))
        if remainder is not None:
            return (root_id, remainder)

    for token, root_id in _ROOT_BY_TOKEN.items():
        if root_id not in roots:
            continue
        remainder = _match_root_prefix(path_part, token)
        if remainder is not None:
            return (root_id, remainder)

    return None
