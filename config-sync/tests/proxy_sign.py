"""Shared gateway-signature helper for wire tests.

Builds the `X-KiroCrew-Proxy` header the real KiroCrew gateway attaches
to every request it forwards to an app backend
(`kiro_crew/apps/proxy_auth.py`): ``<ts>:<hex hmac_sha256(secret,
f"{ts}:{method}:{target}:{sha256(body).hexdigest()}")>``. Test fixtures
set ``KIROCREW_PROXY_SECRET`` to `TEST_PROXY_SECRET` and sign each request
with `signed_headers`, so a wire test exercises the same authenticated
path a gateway-forwarded request takes.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Dict, Optional

PROXY_HEADER = "X-KiroCrew-Proxy"
PROXY_SECRET_ENV = "KIROCREW_PROXY_SECRET"
TEST_PROXY_SECRET = "test-proxy-secret-shared"


def sign(
    method: str,
    target: str,
    body: bytes = b"",
    *,
    secret: str = TEST_PROXY_SECRET,
    ts: Optional[int] = None,
) -> str:
    """Return a valid `X-KiroCrew-Proxy` header value for the request."""
    ts_val = int(time.time()) if ts is None else ts
    body_hash = hashlib.sha256(body or b"").hexdigest()
    msg = f"{ts_val}:{method}:{target}:{body_hash}"
    sig = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return f"{ts_val}:{sig}"


def signed_headers(
    method: str,
    target: str,
    body: bytes = b"",
    headers: Optional[Dict[str, str]] = None,
    *,
    secret: str = TEST_PROXY_SECRET,
) -> Dict[str, str]:
    """Return a copy of ``headers`` with a valid proxy signature added."""
    out = dict(headers or {})
    out[PROXY_HEADER] = sign(method, target, body, secret=secret)
    return out
