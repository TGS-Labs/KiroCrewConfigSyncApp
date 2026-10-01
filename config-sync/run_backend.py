"""Gateway entry point for the config-sync backend (``backend.entryPoint``).

The host starts a file-style entry point as ``python <app root>/<entryPoint>``
with cwd = the app root and no PYTHONPATH. Python puts the SCRIPT's own
directory on ``sys.path``, not the cwd — so an entry point inside
``backend/`` cannot import the ``backend`` package (Phase 8 live failure:
``ModuleNotFoundError: No module named 'backend'``, page showed
``API 502: no reachable backend``). This file lives at the app root, so
launching it puts the app root first on ``sys.path`` and the package
imports below resolve. The two cron jobs already run ``python3 -m
backend.push`` / ``backend.poll`` from the app root and are unaffected.
"""

from __future__ import annotations

from backend.server import APP_NAME, PORT, build_server


def main() -> None:
    print(f"{APP_NAME} backend on port {PORT}")
    build_server(host="127.0.0.1", port=PORT).serve_forever()


if __name__ == "__main__":
    main()
