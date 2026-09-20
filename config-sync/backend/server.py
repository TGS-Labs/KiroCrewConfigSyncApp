"""Minimal backend for config-sync — a KiroCrew app.

Run with: python backend/server.py
Or let KiroCrew manage it via the app manifest backend section.
"""

import json
import os

from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.environ.get("PORT", 9100))
APP_NAME = os.environ.get("KIROCREW_APP_NAME", "config-sync")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"status": "ok", "app": APP_NAME})
        elif self.path == "/api/apps/config-sync/status":
            self._json(200, {"app": APP_NAME, "version": "0.1.0"})
        else:
            self._json(404, {"error": "not found"})

    def _json(self, code: int, data: dict[str, object]) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, *args: object) -> None:
        pass


if __name__ == "__main__":
    print(f"{APP_NAME} backend on port {PORT}")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
