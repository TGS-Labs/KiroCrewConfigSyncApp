"""Failing tests for backend/portable.py (tasks.md 7.2, not yet implemented).

Contract under test — design.md's `backend/portable.py` component, tightened
by requirements.md 2.8-2.11, 4.11-4.13, 5.12. Three pure functions operate on
*one already-parsed JSON document* (any of the tracked JSON files — not just
`agents/*.json`), never on a `{relpath: bytes}` mapping (that shape belongs to
collect.py/redact.py one layer up):

    tokenize(doc: Any, roots: Mapping[str, Path]) -> Any
    expand(doc: Any, roots: Mapping[str, Path]) -> Any
    resolve_reference(value: str, roots: Mapping[str, Path]) -> tuple[str, str] | None

Return shapes design.md leaves open are PINNED here (narrowest choice that
satisfies every cited criterion):

- ``roots`` is ``{"A": Path(...), "B": Path(...)}`` — the same root-id
  convention `backend.collect._roots()` already uses (string ids "A"/"B"
  mapping to resolved, ``~``-expanded, no-trailing-slash `Path` objects).
  This is the natural shape for "resolved exactly as the collector resolves
  it" (requirements.md 2.8) without inventing a second convention.
- ``tokenize(doc, roots)`` and ``expand(doc, roots)`` each return a **new**
  document of the same JSON-compatible shape (dict/list/str/int/float/bool/
  None nesting) — the input ``doc`` is never mutated in place. Only string
  *values* are ever rewritten; dict *keys* are always returned unchanged
  (requirements.md 2.8, 4.11).
- ``resolve_reference(value, roots)`` returns ``(root_id, relpath)`` — a
  2-tuple of ``str`` — on a match (mirroring `allowlist.is_tracked`'s own
  ``(root, relpath)`` pairing), or ``None`` when ``value`` does not resolve
  under either root (covers: an absolute path outside both roots, a
  relative reference, a URL, or a non-path value). This is the exact return
  type design.md's own signature comment declares
  (``resolve_reference(value, roots) -> (root, relpath) | None``), so no
  choice was actually open here — pinned for the test file's own clarity.
- Root ids passed to ``resolve_reference``'s roots mapping and returned in
  its output are the same ``"A"``/``"B"`` strings ``allowlist.py`` and
  ``collect.py`` already use — not ``"KIROCREW_HOME"``/``"KIRO_HOME"``,
  which are only the *env var names*, never a root id.
- Non-portable / unresolved-reference *reporting* (file path + JSON key
  path, as requirements.md 2.9/4.12/4.13 describe for `PushResult` /
  `ApplyResult`) is explicitly NOT this module's job — design.md scopes
  reporting to push.py/apply.py, one layer up. `resolve_reference`
  returning `None` (out-of-root) and `tokenize`/`expand` leaving an
  out-of-root value byte-identical is what this file verifies; the
  key-path-tagged report itself is tasks.md 7.3/7.4's contract, not 7.2's.

Confirmed red for the right reason: every test below fails at collection
time with ``ModuleNotFoundError`` / ``ImportError`` on ``backend.portable``,
because that module does not exist yet — see the bottom of this file for the
explicit import-failure check.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from backend import portable

# ---------------------------------------------------------------------------
# Shared fixtures: two distinct root layouts, root A nested inside root B
# (the default relationship: KIROCREW_HOME=~/.kiro/crew sits inside
# KIRO_HOME=~/.kiro), matching requirements.md 2.8's "root A tried first
# because the default root A lies inside root B".
# ---------------------------------------------------------------------------


@pytest.fixture
def nested_roots() -> Dict[str, Path]:
    """Root A nested inside root B -- the default, realistic relationship."""
    kiro_home = Path("/h/.kiro")
    kirocrew_home = kiro_home / "crew"
    return {"A": kirocrew_home, "B": kiro_home}


@pytest.fixture
def host2_roots() -> Dict[str, Path]:
    """A different host's root layout -- used for cross-host round-trips."""
    kiro_home = Path("/opt/kirouser/.kiro")
    kirocrew_home = kiro_home / "crew"
    return {"A": kirocrew_home, "B": kiro_home}


# ---------------------------------------------------------------------------
# tokenize(): root precedence, boundary matching, scheme preservation
# ---------------------------------------------------------------------------


def test_tokenize_root_a_wins_when_nested_inside_root_b(
    nested_roots: Dict[str, Path],
) -> None:
    """Root A ('/h/.kiro/crew') is nested inside root B ('/h/.kiro').

    A value under root A must tokenize to ${KIROCREW_HOME}, never falling
    through to ${KIRO_HOME} just because root A's path is also a prefix of
    root B's own path space (requirements.md 2.8: "tried in that order (root
    A first, because the default root A lies inside root B)").
    """
    doc = {"prompt": "file:///h/.kiro/crew/steering/foo.md"}
    result = portable.tokenize(doc, nested_roots)
    assert result["prompt"] == "file://${KIROCREW_HOME}/steering/foo.md"


def test_tokenize_sibling_prefix_matches_root_b_not_root_a(
    nested_roots: Dict[str, Path],
) -> None:
    """A directory that merely shares root A's basename as a string *prefix*

    (e.g. '/h/.kiro/crew2/x') is NOT inside root A ('/h/.kiro/crew') -- the
    match boundary must be "R + '/'", not a bare string prefix
    (requirements.md 2.8). But this path genuinely IS inside root B
    ('/h/.kiro'), so it correctly tokenizes to ${KIRO_HOME}, never to any
    ${KIROCREW_HOME} form. The property under test is root selection, not
    "unchanged" -- a value only stays unchanged when it is outside BOTH
    roots (see the sibling-of-root-B case below).
    """
    doc = {"resources": ["file:///h/.kiro/crew2/x"]}
    result = portable.tokenize(doc, nested_roots)
    assert result["resources"] == ["file://${KIRO_HOME}/crew2/x"]
    assert result["resources"][0] != "file://${KIROCREW_HOME}/crew2/x"
    assert "${KIROCREW_HOME}" not in result["resources"][0]


def test_tokenize_sibling_of_root_b_itself_unmatched(
    nested_roots: Dict[str, Path],
) -> None:
    """A path that shares root B's PARENT prefix but is not under root B at

    all (e.g. '/h/.kiro2/x', a sibling of '/h/.kiro' itself) lies outside
    both roots and must be left completely unchanged -- this is the actual
    "boundary, not string-prefix" case requirements.md 2.8 guards: 2.9
    requires the push to leave such a value unchanged and list it as
    non-portable by its JSON key path (verified here via
    resolve_reference's None return, since reporting itself is push.py's
    job, not portable.py's).
    """
    doc = {"resources": ["file:///h/.kiro2/x"]}
    result = portable.tokenize(doc, nested_roots)
    assert result["resources"] == ["file:///h/.kiro2/x"]
    assert portable.resolve_reference("file:///h/.kiro2/x", nested_roots) is None


def test_tokenize_root_string_embedded_mid_text_not_matched(
    nested_roots: Dict[str, Path],
) -> None:
    """The root path appearing anywhere other than the START of the value

    (e.g. inside an inline prompt's free text) must not be rewritten.
    Requirements.md 2.8: "A value in which R appears anywhere other than at
    the start of p ... SHALL NOT be rewritten."
    """
    doc = {
        "prompt": (
            "You are an agent. Your config lives under /h/.kiro/crew/steering "
            "but you should never assume that path is real."
        )
    }
    result = portable.tokenize(doc, nested_roots)
    assert result["prompt"] == doc["prompt"]


def test_tokenize_scheme_preserved_file(nested_roots: Dict[str, Path]) -> None:
    """The file:// scheme prefix is kept across tokenization."""
    doc = {"prompt": "file:///h/.kiro/crew/config-bundles/agent-prompts/x.md"}
    result = portable.tokenize(doc, nested_roots)
    assert (
        result["prompt"] == "file://${KIROCREW_HOME}/config-bundles/agent-prompts/x.md"
    )


def test_tokenize_scheme_preserved_skill(nested_roots: Dict[str, Path]) -> None:
    """The skill:// scheme prefix is kept across tokenization."""
    doc = {"resources": ["skill:///h/.kiro/crew/config-bundles/skills/x/SKILL.md"]}
    result = portable.tokenize(doc, nested_roots)
    assert result["resources"] == [
        "skill://${KIROCREW_HOME}/config-bundles/skills/x/SKILL.md"
    ]


def test_tokenize_bare_path_no_scheme(nested_roots: Dict[str, Path]) -> None:
    """A bare path with no scheme prefix at all still tokenizes; no scheme

    is invented where none existed (requirements.md 2.8 only says the
    scheme prefix "SHALL be kept" when present -- it never requires one).
    """
    doc = {"remote_bin": "/h/.kiro/crew/scripts/run.sh"}
    result = portable.tokenize(doc, nested_roots)
    assert result["remote_bin"] == "${KIROCREW_HOME}/scripts/run.sh"


def test_tokenize_root_b_matched_when_not_under_root_a(
    nested_roots: Dict[str, Path],
) -> None:
    """A value under root B but NOT under root A tokenizes to ${KIRO_HOME}."""
    doc = {"prompt": "file:///h/.kiro/agents/other-agent.json"}
    result = portable.tokenize(doc, nested_roots)
    assert result["prompt"] == "file://${KIRO_HOME}/agents/other-agent.json"


def test_tokenize_out_of_root_absolute_value_unchanged(
    nested_roots: Dict[str, Path],
) -> None:
    """An absolute path under neither root (e.g. a site-packages install

    path) is left completely unchanged by tokenize -- requirements.md 2.9:
    "the push SHALL leave the value unchanged". (The *reporting* of this
    value with its JSON key path is push.py's job, tasks.md 7.3 -- verified
    separately via resolve_reference's None return, below.)
    """
    site_packages_prompt = (
        "file:///usr/local/lib/python3.12/site-packages/kiro_crew/config/prompt.md"
    )
    doc = {"prompt": site_packages_prompt}
    result = portable.tokenize(doc, nested_roots)
    assert result["prompt"] == site_packages_prompt


def test_tokenize_https_url_untouched(nested_roots: Dict[str, Path]) -> None:
    """A non-path https:// value is left unchanged and is not a candidate

    for rewriting (requirements.md 2.9: "non-path values (e.g. https://...
    URLs) SHALL be left unchanged and not listed").
    """
    doc = {"webhook": "https://example.com/hooks/callback"}
    result = portable.tokenize(doc, nested_roots)
    assert result["webhook"] == "https://example.com/hooks/callback"


def test_tokenize_relative_reference_untouched(nested_roots: Dict[str, Path]) -> None:
    """A relative file:// reference is left unchanged (requirements.md 2.9:

    "Relative references (e.g. file://.kiro/steering/**) ... SHALL be left
    unchanged and not listed").
    """
    doc = {"resources": ["file://.kiro/steering/**"]}
    result = portable.tokenize(doc, nested_roots)
    assert result["resources"] == ["file://.kiro/steering/**"]


def test_tokenize_dict_keys_never_rewritten(nested_roots: Dict[str, Path]) -> None:
    """Only string VALUES are rewritten; a key that happens to look like a

    root path is never touched (requirements.md 2.8: "never an object
    key").
    """
    weird_key = "/h/.kiro/crew/steering/foo.md"
    doc = {weird_key: "unrelated value"}
    result = portable.tokenize(doc, nested_roots)
    assert weird_key in result
    assert result[weird_key] == "unrelated value"


def test_tokenize_only_matches_start_of_value(nested_roots: Dict[str, Path]) -> None:
    """A root path that is a SUFFIX or interior substring of the value, but

    not its start, is not rewritten -- match is anchored at the start of the
    path part only (requirements.md 2.8).
    """
    doc = {"note": "see /elsewhere/h/.kiro/crew/steering/foo.md for details"}
    result = portable.tokenize(doc, nested_roots)
    assert result["note"] == doc["note"]


def test_tokenize_redacted_placeholder_never_a_candidate(
    nested_roots: Dict[str, Path],
) -> None:
    """A "<redacted>" value (as produced by backend/redact.py, upstream of

    tokenize per requirements.md 2.8's ordering) is never treated as a
    tokenize candidate -- it does not start with either root and must pass
    through byte-identical.
    """
    doc = {"headers": {"Authorization": "<redacted>"}, "env": {"API_KEY": "<redacted>"}}
    result = portable.tokenize(doc, nested_roots)
    assert result == doc


def test_tokenize_does_not_mutate_input_document(
    nested_roots: Dict[str, Path],
) -> None:
    """tokenize returns a new document; the caller's input is untouched."""
    doc = {"prompt": "file:///h/.kiro/crew/steering/foo.md"}
    original = copy.deepcopy(doc)
    portable.tokenize(doc, nested_roots)
    assert doc == original


# ---------------------------------------------------------------------------
# tokenize(): document shapes beyond agents/*.json -- crons.json, mcp.json
# ---------------------------------------------------------------------------


def test_tokenize_crons_json_shaped_document(nested_roots: Dict[str, Path]) -> None:
    """A crons.json-shaped document (a `script` path deep in a list) is

    tokenized the same way an agent definition's `prompt` would be --
    design.md: "crons.json's script path ... [is] the same class of
    host-absolute value as an agent's prompt."
    """
    doc = {
        "jobs": [
            {
                "name": "config-sync-push",
                "script": "file:///h/.kiro/crew/crons/push.py:run",
                "enabled": False,
            }
        ]
    }
    result = portable.tokenize(doc, nested_roots)
    assert result["jobs"][0]["script"] == "file://${KIROCREW_HOME}/crons/push.py:run"
    assert result["jobs"][0]["name"] == "config-sync-push"
    assert result["jobs"][0]["enabled"] is False


def test_tokenize_mcp_json_shaped_document(nested_roots: Dict[str, Path]) -> None:
    """An mcp.json-shaped document tokenizes a `resources` path the same

    way (design.md: "mcp.json's resources field ... [is] the same class of
    host-absolute value as an agent's prompt"), while an unrelated
    ``headers`` block (already redacted upstream) stays untouched.
    """
    doc = {
        "mcpServers": {
            "github": {
                "command": "file:///h/.kiro/crew/skills/github/scripts/run.sh",
                "headers": {"Authorization": "<redacted>"},
            }
        }
    }
    result = portable.tokenize(doc, nested_roots)
    server = result["mcpServers"]["github"]
    assert server["command"] == ("file://${KIROCREW_HOME}/skills/github/scripts/run.sh")
    assert server["headers"] == {"Authorization": "<redacted>"}


def test_tokenize_agent_definition_document(nested_roots: Dict[str, Path]) -> None:
    """An agents/<name>.json-shaped document tokenizes its `prompt` field."""
    doc = {
        "name": "git-manager",
        "prompt": "file:///h/.kiro/crew/config-bundles/agent-prompts/git-manager.md",
        "tools": ["read", "write"],
    }
    result = portable.tokenize(doc, nested_roots)
    assert result["prompt"] == (
        "file://${KIROCREW_HOME}/config-bundles/agent-prompts/git-manager.md"
    )
    assert result["name"] == "git-manager"
    assert result["tools"] == ["read", "write"]


# ---------------------------------------------------------------------------
# expand(): the inverse direction
# ---------------------------------------------------------------------------


def test_expand_token_form_to_this_host_root_a(nested_roots: Dict[str, Path]) -> None:
    doc = {"prompt": "file://${KIROCREW_HOME}/steering/foo.md"}
    result = portable.expand(doc, nested_roots)
    assert result["prompt"] == "file:///h/.kiro/crew/steering/foo.md"


def test_expand_token_form_to_this_host_root_b(nested_roots: Dict[str, Path]) -> None:
    doc = {"prompt": "file://${KIRO_HOME}/agents/other-agent.json"}
    result = portable.expand(doc, nested_roots)
    assert result["prompt"] == "file:///h/.kiro/agents/other-agent.json"


def test_expand_token_elsewhere_in_value_not_expanded(
    nested_roots: Dict[str, Path],
) -> None:
    """A token appearing anywhere other than the start of the value is not

    expanded (requirements.md 4.11 mirrors 2.8's start-anchored rule).
    """
    doc = {"note": "the token ${KIROCREW_HOME} is just an example, not a path"}
    result = portable.expand(doc, nested_roots)
    assert result["note"] == doc["note"]


def test_expand_out_of_root_legacy_absolute_value_unchanged(
    nested_roots: Dict[str, Path],
) -> None:
    """A legacy commit pushed before tokenization existed, carrying another

    host's absolute path, is written unchanged by expand (requirements.md
    4.12): expand only rewrites the token form, it never guesses at an
    arbitrary absolute path from another host.
    """
    other_host_path = "file:///home/otheruser/.kiro/crew/steering/foo.md"
    doc = {"prompt": other_host_path}
    result = portable.expand(doc, nested_roots)
    assert result["prompt"] == other_host_path


def test_expand_does_not_mutate_input_document(nested_roots: Dict[str, Path]) -> None:
    doc = {"prompt": "file://${KIROCREW_HOME}/steering/foo.md"}
    original = copy.deepcopy(doc)
    portable.expand(doc, nested_roots)
    assert doc == original


def test_expand_crons_json_shaped_document(nested_roots: Dict[str, Path]) -> None:
    doc = {
        "jobs": [{"name": "x", "script": "file://${KIROCREW_HOME}/crons/push.py:run"}]
    }
    result = portable.expand(doc, nested_roots)
    assert result["jobs"][0]["script"] == "file:///h/.kiro/crew/crons/push.py:run"


def test_expand_mcp_json_shaped_document(nested_roots: Dict[str, Path]) -> None:
    doc = {
        "mcpServers": {
            "github": {
                "command": "file://${KIROCREW_HOME}/skills/github/scripts/run.sh"
            }
        }
    }
    result = portable.expand(doc, nested_roots)
    assert result["mcpServers"]["github"]["command"] == (
        "file:///h/.kiro/crew/skills/github/scripts/run.sh"
    )


# ---------------------------------------------------------------------------
# resolve_reference(): existence/glob check, and shape
# ---------------------------------------------------------------------------


def test_resolve_reference_matches_root_a(nested_roots: Dict[str, Path]) -> None:
    result = portable.resolve_reference(
        "file:///h/.kiro/crew/steering/foo.md", nested_roots
    )
    assert result == ("A", "steering/foo.md")


def test_resolve_reference_matches_root_b(nested_roots: Dict[str, Path]) -> None:
    result = portable.resolve_reference(
        "file:///h/.kiro/agents/other-agent.json", nested_roots
    )
    assert result == ("B", "agents/other-agent.json")


def test_resolve_reference_token_form_resolves_too(
    nested_roots: Dict[str, Path],
) -> None:
    """resolve_reference accepts BOTH the token form and this host's own

    absolute form (requirements.md 5.12): a legacy same-host commit still
    resolves.
    """
    result = portable.resolve_reference(
        "file://${KIROCREW_HOME}/steering/foo.md", nested_roots
    )
    assert result == ("A", "steering/foo.md")


def test_resolve_reference_out_of_root_returns_none(
    nested_roots: Dict[str, Path],
) -> None:
    result = portable.resolve_reference(
        "file:///usr/local/lib/python3.12/site-packages/kiro_crew/config/prompt.md",
        nested_roots,
    )
    assert result is None


def test_resolve_reference_relative_value_returns_none(
    nested_roots: Dict[str, Path],
) -> None:
    result = portable.resolve_reference("file://.kiro/steering/**", nested_roots)
    assert result is None


def test_resolve_reference_url_returns_none(nested_roots: Dict[str, Path]) -> None:
    result = portable.resolve_reference(
        "https://example.com/hooks/callback", nested_roots
    )
    assert result is None


def test_resolve_reference_existence_check_glob_match(
    tmp_path: Path,
) -> None:
    """resolve_reference's existence/glob check: a glob-shaped relpath with

    AT LEAST ONE match on disk resolves successfully (requirements.md 4.13:
    "glob patterns are checked for at least one match").
    """
    root_a = tmp_path / "kiro" / "crew"
    skill_dir = root_a / "config-bundles" / "skills" / "github-pr"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# github-pr\n")
    root_b = tmp_path / "kiro"
    roots = {"A": root_a, "B": root_b}

    result = portable.resolve_reference(
        f"skill://{root_a}/config-bundles/skills/**/SKILL.md", roots
    )
    assert result is not None
    assert result[0] == "A"


def test_resolve_reference_existence_check_no_glob_match(
    tmp_path: Path,
) -> None:
    """A glob-shaped reference under a tracked root with NO match on disk

    still resolves to (root, relpath) at the string level -- the caller
    (apply.py, requirements.md 4.13) is responsible for treating a
    zero-match existence check as "unresolved", not resolve_reference
    itself, whose contract is purely the (root, relpath) mapping. This test
    pins that resolve_reference does not silently fail the mapping just
    because nothing exists on disk yet.
    """
    root_a = tmp_path / "kiro" / "crew"
    root_b = tmp_path / "kiro"
    roots = {"A": root_a, "B": root_b}

    result = portable.resolve_reference(
        f"skill://{root_a}/config-bundles/skills/nonexistent/**/SKILL.md", roots
    )
    assert result == ("A", "config-bundles/skills/nonexistent/**/SKILL.md")


def test_resolve_reference_redacted_placeholder_never_a_candidate(
    nested_roots: Dict[str, Path],
) -> None:
    """A "<redacted>" value is never a candidate for resolve_reference

    either -- it does not start with either root and resolves to None.
    """
    result = portable.resolve_reference("<redacted>", nested_roots)
    assert result is None


# ---------------------------------------------------------------------------
# Property tests (hypothesis) -- idempotence, and same-host round trip.
# Uses the project's dev/ci two-tier profile registered in conftest.py.
# ---------------------------------------------------------------------------


_PATH_SEGMENT = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="-_."
    ),
    min_size=1,
    max_size=12,
)

_RELATIVE_PATH = st.lists(_PATH_SEGMENT, min_size=1, max_size=4).map(
    lambda segs: "/".join(segs)
)

_SCHEME = st.sampled_from(["file://", "skill://", ""])
_ROOT_CHOICE = st.sampled_from(["A", "B"])


@st.composite
def _rooted_value(draw: st.DrawFn) -> str:
    """Build a string value that is anchored under a known root."""
    scheme = draw(_SCHEME)
    root_choice = draw(_ROOT_CHOICE)
    rel = draw(_RELATIVE_PATH)
    root_token = "${KIROCREW_HOME}" if root_choice == "A" else "${KIRO_HOME}"
    return f"{scheme}{root_token}/{rel}"


@st.composite
def _json_doc(draw: st.DrawFn) -> Dict[str, Any]:
    """A small JSON-object document mixing rooted path values, unrelated

    strings, nested lists/dicts, and non-string scalars -- exercising
    tokenize/expand across the general "any parsed JSON document" contract,
    not just an agents/*.json shape.
    """
    keys = draw(
        st.lists(
            st.text(
                alphabet=st.characters(whitelist_categories=("Ll", "Lu")),
                min_size=1,
                max_size=8,
            ),
            min_size=1,
            max_size=4,
            unique=True,
        )
    )
    doc: Dict[str, Any] = {}
    for key in keys:
        kind = draw(st.integers(min_value=0, max_value=3))
        if kind == 0:
            doc[key] = draw(_rooted_value())
        elif kind == 1:
            doc[key] = draw(st.text(max_size=20))
        elif kind == 2:
            doc[key] = draw(st.lists(_rooted_value(), max_size=3))
        else:
            doc[key] = draw(st.one_of(st.integers(), st.booleans(), st.none()))
    return doc


@given(doc=_json_doc())
@settings(deadline=None)
def test_tokenize_is_idempotent(doc: Dict[str, Any]) -> None:
    """Applying tokenize twice produces the same result as applying it once

    (requirements.md 2.10: "Tokenization SHALL be idempotent").
    """
    roots = {"A": Path("/h/.kiro/crew"), "B": Path("/h/.kiro")}
    once = portable.tokenize(doc, roots)
    twice = portable.tokenize(once, roots)
    assert once == twice


@given(doc=_json_doc())
@settings(deadline=None)
def test_expand_is_idempotent(doc: Dict[str, Any]) -> None:
    """Applying expand twice produces the same result as applying it once

    (requirements.md 4.11: "Expansion SHALL be idempotent").
    """
    roots = {"A": Path("/h/.kiro/crew"), "B": Path("/h/.kiro")}
    tokenized = portable.tokenize(doc, roots)
    once = portable.expand(tokenized, roots)
    twice = portable.expand(once, roots)
    assert once == twice


@given(doc=_json_doc())
@settings(deadline=None)
def test_expand_tokenize_round_trip_on_one_host(doc: Dict[str, Any]) -> None:
    """expand(tokenize(x)) == x on one host (design.md's stated law), for

    every value this module actually rewrites -- i.e. after first
    tokenizing the freshly-generated absolute-form document, then
    expanding it back on the SAME roots must reproduce the pre-tokenize
    absolute-form document byte-for-byte.
    """
    roots = {"A": Path("/h/.kiro/crew"), "B": Path("/h/.kiro")}
    # Build the "already absolute, this-host" form first (rooted values
    # start out as this host's real absolute paths, mirroring what the
    # collector actually hands portable.py on push).
    absolute_doc = json.loads(
        json.dumps(doc)
        .replace("${KIROCREW_HOME}", str(roots["A"]))
        .replace("${KIRO_HOME}", str(roots["B"]))
    )
    tokenized = portable.tokenize(absolute_doc, roots)
    round_tripped = portable.expand(tokenized, roots)
    assert round_tripped == absolute_doc


@given(doc=_json_doc())
@settings(deadline=None)
def test_tokenize_output_contains_no_root_path(doc: Dict[str, Any]) -> None:
    """After tokenize, no root's absolute path string appears anywhere in

    the output for any value that was actually anchored under a root --
    requirements.md 2.8/2.11's host-independence guarantee, restated as a
    universal property over the tokenizer's own output.
    """
    roots = {"A": Path("/h/.kiro/crew"), "B": Path("/h/.kiro")}
    absolute_doc = json.loads(
        json.dumps(doc)
        .replace("${KIROCREW_HOME}", str(roots["A"]))
        .replace("${KIRO_HOME}", str(roots["B"]))
    )
    result = portable.tokenize(absolute_doc, roots)
    serialized = json.dumps(result)
    assert str(roots["A"]) not in serialized
    assert str(roots["B"]) not in serialized
