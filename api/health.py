"""Unauthenticated liveness endpoint: GET /api/health

Returns a static 200 {"ok": true} only. No auth, no environment access,
no secrets, no internal state. (Served by api/check.py when the Vercel
entrypoint routes all paths through that handler; this file covers
file-based /api routing.)
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        pass
