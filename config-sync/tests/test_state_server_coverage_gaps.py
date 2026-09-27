"""Additive unit tests closing branch-coverage gaps left by the C1/H7/M4

fix in `backend/state.py` / `backend/server.py`, without touching any
existing test file. Each test here exercises one branch no other test
file reaches:

- `server._is_loopback_host`'s bracketed-IPv6 branch (``[::1]``/
  ``[::1]:port``) — every H7 test uses a bare host/IPv4 value.
- `state._load_payload`'s per-key merge loop, both when a key IS present
  in the raw on-disk dict (partial document, e.g. written by an older
  schema) and when it is ABSENT (so the default is kept).
"""

from __future__ import annotations

import json
from typing import Any

from backend import server, state


class TestIsLoopbackHostIPv6:
    def test_bracketed_ipv6_with_no_port_is_loopback(self) -> None:
        assert server._is_loopback_host("[::1]") is True

    def test_bracketed_ipv6_with_port_is_loopback(self) -> None:
        assert server._is_loopback_host("[::1]:9100") is True

    def test_bare_ipv6_without_brackets_is_loopback(self) -> None:
        assert server._is_loopback_host("::1") is True

    def test_bracketed_non_loopback_ipv6_is_refused(self) -> None:
        assert server._is_loopback_host("[::2]") is False


class TestLoadPayloadPartialDocument:
    def test_a_document_missing_some_keys_keeps_defaults_for_those_keys(
        self, tmp_path: Any
    ) -> None:
        """A raw on-disk dict that only has SOME of `_DEFAULT_FIELDS`

        (e.g. an older schema version, or a hand-edited file) must load
        with the present keys taken from disk and the absent keys left
        at their fresh default — exercising both the `key in raw` True
        and False arms of `_load_payload`'s merge loop in one document.
        """
        path = tmp_path / "state.json"
        path.write_text(
            json.dumps({"last_seen_sha": "onlysha", "history": [{"x": 1}]}),
            encoding="utf-8",
        )

        payload = state._load_payload(path)

        assert payload["last_seen_sha"] == "onlysha"
        assert payload["history"] == [{"x": 1}]
        # Absent keys fall back to the fresh default.
        assert payload["last_pushed_hash"] is None
        assert payload["pending"] is None
        assert payload["restore_dirs"] == {}

    def test_a_document_with_every_key_present_takes_every_value_from_disk(
        self, tmp_path: Any
    ) -> None:
        path = tmp_path / "state.json"
        full: dict[str, Any] = {key: None for key in state._DEFAULT_FIELDS}
        full["last_pushed_hash"] = "full-hash"
        full["history"] = []
        full["restore_dirs"] = {}
        path.write_text(json.dumps(full), encoding="utf-8")

        payload = state._load_payload(path)

        assert payload["last_pushed_hash"] == "full-hash"


class TestFileLockTimeout:
    def test_timeout_raises_timeouterror_without_ever_acquiring(
        self, tmp_path: Any
    ) -> None:
        """A lock already held (by a separate real OS-level lock, taken

        via a second file descriptor on the same path) must cause a
        second acquisition attempt to time out and raise — never block
        forever, never silently proceed unlocked.
        """
        import fcntl
        import os

        lock_path = tmp_path / "state.json.lock"
        holder_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(holder_fd, fcntl.LOCK_EX)
        try:
            try:
                with state._file_lock(lock_path, timeout=0.2):
                    raise AssertionError("should never acquire while held")
            except TimeoutError:
                pass
            else:
                raise AssertionError("expected TimeoutError")
        finally:
            fcntl.flock(holder_fd, fcntl.LOCK_UN)
            os.close(holder_fd)
