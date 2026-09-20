"""Test-environment wiring for tests/safety/.

backend/safety/push_policy.py calls kiro_crew.security at runtime (see
design.md's push_policy.py component). The deployed app always runs as a
subprocess of the KiroCrew gateway, which already has `kiro_crew` on
sys.path from its own installation -- but this venv is isolated from that
installation, so tests need the system site-packages directory added here.
This never adds to, edits, or copies the installed `kiro_crew` package
(requirements.md 1.1 / 8.4) -- it only makes an already-installed, untouched
package importable from this venv.
"""

from __future__ import annotations

import glob
import os
import sys

for _candidate in glob.glob("/usr/local/lib/python3.*/site-packages"):
    if (
        os.path.isdir(os.path.join(_candidate, "kiro_crew"))
        and _candidate not in sys.path
    ):
        sys.path.append(_candidate)
