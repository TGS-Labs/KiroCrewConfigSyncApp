"""Phase 8 live failure: the gateway could not start the backend.

The dashboard page showed `API 502: app 'config-sync' has no reachable
backend`. gateway.log:

    App config-sync backend exited immediately (rc=1) on port 9100 —
      File ".../apps/config-sync/backend/server.py", line 104, in <module>
        from backend import routes, state
    ModuleNotFoundError: No module named 'backend'

For a file-style `backend.entryPoint` (`backend/server.py`) the host runs
`[python, "<app>/backend/server.py"]` with cwd = the app root and no
PYTHONPATH (kiro_crew/apps/backend.py, the plain-.py branch). Python then
puts the SCRIPT's directory (`backend/`) on sys.path, not the cwd, so the
absolute `from backend import ...` imports fail. The crons run
`python3 -m backend.poll` from the app root and never hit this, which is
why every test passed and the live backend crashed.

This test launches the file exactly the way the host does and requires a
healthy `/health`.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_server_py_starts_when_launched_the_way_the_gateway_launches_it(
    tmp_path: Path,
) -> None:
    port = _free_port()
    manifest = json.loads((APP_ROOT / "app.json").read_text(encoding="utf-8"))
    entry_point = manifest["backend"]["entryPoint"]
    assert entry_point.endswith(".py"), entry_point
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("PYTHONPATH", "KIROCREW_APP_NAME")
    }
    env.update({"PORT": str(port), "CONFIG_SYNC_STATE_DIR": str(tmp_path / "state")})
    proc = subprocess.Popen(
        [sys.executable, str(APP_ROOT / entry_point)],
        cwd=str(APP_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.time() + 10
        status = None
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                conn.request("GET", "/health")
                status = conn.getresponse().status
                conn.close()
                break
            except OSError:
                time.sleep(0.1)
        if proc.poll() is not None:
            _, err = proc.communicate(timeout=5)
            raise AssertionError(
                f"backend exited rc={proc.returncode} when launched as the "
                f"gateway launches it:\n{err[-1500:]}"
            )
        assert status == 200
    finally:
        proc.kill()
        proc.wait(timeout=5)
