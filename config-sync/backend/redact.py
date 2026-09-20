"""Structure-preserving redaction of secret-bearing configuration values.

See design.md's `backend/redact.py` component and requirements 3.1-3.5.

`redact()` takes a mapping of relative path -> raw file bytes (as collected by
`backend/collect.py`) and returns a new mapping where every JSON document has
had each value inside any object named ``headers`` or ``env`` replaced with
the literal placeholder ``"<redacted>"``. Keys, key order, server names, and
all other document structure are preserved exactly. Re-serialization is
deterministic: redacting the same input twice produces byte-identical output,
so an unchanged file never produces a spurious diff.

A file whose content is not valid JSON passes through unchanged — redaction
only ever touches parsed JSON documents; it never guesses at other formats.
"""

from __future__ import annotations

import json
from typing import Any, Dict

REDACTED = "<redacted>"

_REDACT_BLOCK_NAMES = ("headers", "env")


def _redact_value_block(block: Any) -> Any:
    """Replace every value in a headers/env-shaped mapping with the
    placeholder, preserving key order. Non-mapping content is returned
    unchanged rather than guessed at."""
    if not isinstance(block, dict):
        return block
    return {key: REDACTED for key in block.keys()}


def _walk(node: Any) -> Any:
    """Recursively walk a parsed JSON document, redacting the value of every
    key named ``headers`` or ``env`` at any nesting depth, while leaving
    every other key, value, and ordering untouched."""
    if isinstance(node, dict):
        result: Dict[str, Any] = {}
        for key, value in node.items():
            if key in _REDACT_BLOCK_NAMES:
                result[key] = _redact_value_block(value)
            else:
                result[key] = _walk(value)
        return result
    if isinstance(node, list):
        return [_walk(item) for item in node]
    return node


def redact(collected: Dict[str, bytes]) -> Dict[str, bytes]:
    """Redact every headers/env value in every JSON file of `collected`.

    Args:
        collected: mapping of relative path -> raw file bytes.

    Returns:
        A new mapping with the same keys. For each value that parses as
        JSON, every object named ``headers`` or ``env`` has its values
        replaced with ``"<redacted>"``; the result is re-serialized
        deterministically (fixed indent, original key order preserved) so
        redacting identical input twice is byte-identical. A value that does
        not parse as JSON is passed through unchanged.
    """
    redacted: Dict[str, bytes] = {}
    for relpath, content in collected.items():
        try:
            parsed = json.loads(content.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            redacted[relpath] = content
            continue

        walked = _walk(parsed)
        serialized = json.dumps(walked, indent=2, ensure_ascii=False) + "\n"
        redacted[relpath] = serialized.encode("utf-8")

    return redacted
