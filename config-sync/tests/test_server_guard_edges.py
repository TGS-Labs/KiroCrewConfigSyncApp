"""Edge-branch tests for the H7 mutation guard's and the gateway-HMAC

check's pure helpers.

`tests/test_server_security.py` and `tests/test_review4_proxy_auth.py`
prove the guard over the wire (cross-site `Sec-Fetch-Site`, non-loopback
`Host` -> 403; unsigned -> 401). These pin the helper decisions those wire
tests cannot reach with a real HTTP client -- an absent or empty `Host`, a
bare IPv6 literal that a naive port-strip would misparse, and the
`verify_proxy_request` boundaries at a fixed clock -- so a regression in
any one of them fails a named test rather than hiding in an untested
branch. Round 4 removed the `Origin` fallback (and `_origin_host`); the
same-site tests below pin that removal.
"""

from __future__ import annotations

import hashlib
import hmac
import time
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
    ("headers", "expected"),
    [
        ({}, True),
        ({"Origin": "null", "Host": "127.0.0.1:9100"}, True),
        ({"Origin": "https://dashboard.example", "Host": "127.0.0.1:9100"}, True),
        ({"Sec-Fetch-Site": "same-origin"}, True),
        ({"Sec-Fetch-Site": "none"}, True),
        ({"Sec-Fetch-Site": "same-site"}, False),
        ({"Sec-Fetch-Site": "cross-site"}, False),
        ({"Sec-Fetch-Site": ""}, False),
    ],
)
def test_same_site_checks_only_a_present_sec_fetch_site(
    headers: Dict[str, str], expected: bool
) -> None:
    """A present `Sec-Fetch-Site` must be `same-origin`/`none`; an absent
    one passes whatever `Origin` says -- the `Origin`-vs-`Host` fallback is
    gone (the gateway forwards the dashboard Origin with a rewritten
    loopback Host, so the two never match)."""
    assert server._is_same_site(headers) is expected


_SECRET = "edge-secret"
_NOW = 1_700_000_000.0


def _sig(ts: str, method: str, target: str, body: bytes, secret: str) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    msg = f"{ts}:{method}:{target}:{body_hash}"
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def _header(ts: int, secret: str = _SECRET, body: bytes = b"") -> str:
    return f"{ts}:{_sig(str(ts), 'POST', '/api/push?x=1', body, secret)}"


def _verify(header: str, *, secret: str = _SECRET, body: bytes = b"") -> bool:
    return server.verify_proxy_request(
        header,
        method="POST",
        target="/api/push?x=1",
        body=body,
        secret=secret,
        now=_NOW,
    )


def test_verify_accepts_a_fresh_valid_signature_at_the_skew_boundary() -> None:
    assert _verify(_header(int(_NOW))) is True
    assert _verify(_header(int(_NOW) - 60)) is True
    assert _verify(_header(int(_NOW) + 60)) is True


@pytest.mark.parametrize("offset", [-61, 61])
def test_verify_refuses_a_timestamp_outside_the_skew_window(offset: int) -> None:
    assert _verify(_header(int(_NOW) + offset)) is False


@pytest.mark.parametrize(
    "header", ["", "no-colon", "abc:deadbeef", "-5:deadbeef", "1700000000:"]
)
def test_verify_refuses_a_malformed_header(header: str) -> None:
    assert _verify(header) is False


def test_verify_fails_closed_on_an_empty_secret() -> None:
    """Even a header correctly computed with the empty key is refused."""
    assert _verify(_header(int(_NOW), secret=""), secret="") is False


def test_verify_refuses_a_wrong_secret_or_tampered_body() -> None:
    assert _verify(_header(int(_NOW), secret="other")) is False
    assert _verify(_header(int(_NOW), body=b"a"), body=b"b") is False


def test_verify_uses_the_real_clock_when_now_is_omitted() -> None:
    ts = int(time.time())
    header = f"{ts}:{_sig(str(ts), 'GET', '/api/status', b'', _SECRET)}"
    assert server.verify_proxy_request(
        header, method="GET", target="/api/status", body=b"", secret=_SECRET
    )
