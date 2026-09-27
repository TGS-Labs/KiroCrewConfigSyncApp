"""Edge-branch tests for the redesigned H7 mutation guard's pure helpers.

`tests/test_server_security.py` proves the guard over the wire (cross-site
`Sec-Fetch-Site`, mismatched `Origin`, non-loopback `Host` -> 403; the
same-site shapes reach the route). These pin the helper decisions those
wire tests cannot reach with a real HTTP client -- an absent or empty
`Host`, a bare IPv6 literal that a naive port-strip would misparse, and an
`Origin` value with no authority -- so a regression in any one of them
fails a named test rather than hiding in an untested branch.
"""

from __future__ import annotations

from typing import Dict, Optional

import pytest

from backend import server


@pytest.mark.parametrize(
    ("host_header", "expected"),
    [
        (None, False),
        ("", False),
        ("127.0.0.1", True),
        ("127.0.0.1:9100", True),
        ("localhost:8080", True),
        ("::1", True),
        ("[::1]", True),
        ("[::1]:9100", True),
        ("evil.example", False),
        ("evil.example:127.0.0.1", False),
        ("127.0.0.1.evil.example", False),
    ],
)
def test_is_loopback_host_accepts_only_loopback_authorities(
    host_header: Optional[str], expected: bool
) -> None:
    """A missing or empty `Host` is refused (nothing to validate), a bare
    or bracketed IPv6 loopback literal is accepted with or without a port,
    and a hostile host that merely CONTAINS a loopback string is refused."""
    assert server._is_loopback_host(host_header) is expected


@pytest.mark.parametrize(
    ("origin_header", "expected"),
    [
        (None, None),
        ("", None),
        ("null", None),
        ("not a url", None),
        ("https://evil.example", "evil.example"),
        ("http://127.0.0.1:9100", "127.0.0.1:9100"),
    ],
)
def test_origin_host_returns_authority_or_none(
    origin_header: Optional[str], expected: Optional[str]
) -> None:
    """`Origin: null` (an opaque origin, e.g. a sandboxed iframe or a
    redirected cross-site POST) and an unparseable value yield no
    authority, so the caller cannot accidentally match them to `Host`."""
    assert server._origin_host(origin_header) == expected


def test_same_site_refuses_an_opaque_or_unparseable_origin() -> None:
    """With `Sec-Fetch-Site` absent, an `Origin` that yields no authority
    must be refused, never treated like the neither-header CLI case."""
    headers: Dict[str, str] = {"Origin": "null", "Host": "127.0.0.1:9100"}
    assert server._is_same_site(headers) is False


def test_same_site_refuses_a_matching_origin_when_host_is_missing() -> None:
    """The Origin fallback compares against `Host`; with no `Host` there is
    nothing to match, so the request is refused rather than waved through."""
    headers: Dict[str, str] = {"Origin": "http://127.0.0.1:9100"}
    assert server._is_same_site(headers) is False
