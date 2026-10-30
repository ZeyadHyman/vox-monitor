"""Protected monitoring endpoint: GET /api/check

Authentication (constant-time compared, never echoed):
  * Header ``x-monitor-secret: <MONITOR_SECRET>`` (external schedulers), or
  * Header ``Authorization: Bearer <CRON_SECRET>`` (Vercel Cron; falls back
    to MONITOR_SECRET if CRON_SECRET is unset).
"""

from __future__ import annotations

import hmac
import json
from http.server import BaseHTTPRequestHandler

from vox_monitor.config import ConfigError, Settings
from vox_monitor.http import VoxHttpClient
from vox_monitor.orchestrator import run_cycle
from vox_monitor.state import RedisRestStore
from vox_monitor.telegram import TelegramNotifier


def _secret_ok(header_value: str | None, expected: str | None) -> bool:
    if not header_value or not expected:
        return False
    return hmac.compare_digest(header_value.encode(), expected.encode())


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        try:
            settings = Settings.from_env()
        except ConfigError as exc:
            # Message contains variable NAMES only, never values.
            self._json(500, {"ok": False, "error": str(exc)})
            return

        supplied = self.headers.get("x-monitor-secret")
        authorized = _secret_ok(supplied, settings.monitor_secret)

        if not authorized:
            auth = self.headers.get("Authorization") or ""
            bearer = auth[7:] if auth.startswith("Bearer ") else None
            expected_bearer = settings.cron_secret or settings.monitor_secret
            authorized = _secret_ok(bearer, expected_bearer)

        if not authorized:
            self._json(401, {"ok": False, "error": "Unauthorized"})
            return

        try:
            client = VoxHttpClient(
                timeout=settings.request_timeout, max_retries=settings.max_retries
            )
            store = RedisRestStore(settings.redis_url, settings.redis_token)
            chat_id = settings.test_chat_id or settings.telegram_chat_id
            notifier = TelegramNotifier(settings.telegram_bot_token, chat_id)
            report = run_cycle(settings, client, store, notifier)
        except Exception:
            # Never leak internals or secrets.
            self._json(500, {"ok": False, "error": "internal error"})
            return

        if report.busy:
            self._json(200, {"ok": True, "skipped": "cycle already running"})
            return

        status = 200
        if not report.ok and report.state_error:
            status = 503
        elif not report.ok:
            status = 502

        self._json(status, report.to_dict())

    def _json(self, status: int, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # quiet default logging
        pass
