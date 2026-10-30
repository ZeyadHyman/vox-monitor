"""Monitoring endpoint + public liveness, deployed as the Vercel entrypoint.

Routes (served regardless of Vercel routing mode):
  * GET /api/health, /health  -> 200 {"ok": true}   PUBLIC, no auth
  * GET /api/check            -> protected monitoring cycle

Authentication for /api/check (constant-time compared, never echoed):
  * Header ``x-monitor-secret: <MONITOR_SECRET>`` (external schedulers), or
  * Header ``Authorization: Bearer <CRON_SECRET>`` (Vercel Cron; falls back
    to MONITOR_SECRET if CRON_SECRET is unset).

Note: ``pyproject.toml`` sets ``tool.vercel.entrypoint = "api.check:handler"``,
so Vercel routes every path through this handler. Health requests must be
answered here (before any env/config access) or they would fall into the
monitoring auth path.
"""

from __future__ import annotations

import hmac
import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

from vox_monitor.config import ConfigError, Settings
from vox_monitor.http import VoxHttpClient
from vox_monitor.orchestrator import run_cycle
from vox_monitor.state import RedisRestStore
from vox_monitor.telegram import TelegramNotifier

_HEALTH_PATHS = frozenset({"/api/health", "/health"})

_SECRET_ENV_NAMES = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "UPSTASH_REDIS_REST_URL",
    "UPSTASH_REDIS_REST_TOKEN",
    "MONITOR_SECRET",
    "CRON_SECRET",
)


def _redact(text: str) -> str:
    """Strip environment secret values from text destined for logs."""
    for name in _SECRET_ENV_NAMES:
        value = os.environ.get(name) or ""
        if len(value) >= 4 and value in text:
            text = text.replace(value, f"[REDACTED:{name}]")
    return text


def _log_exception() -> None:
    """Surface the full root exception to runtime logs, secrets redacted."""
    print(_redact(traceback.format_exc()), file=sys.stderr, flush=True)


def _is_health_path(raw_path: str) -> bool:
    path = urlparse(raw_path or "/").path.rstrip("/") or "/"
    return path in _HEALTH_PATHS


def _secret_ok(header_value: str | None, expected: str | None) -> bool:
    if not header_value or not expected:
        return False
    return hmac.compare_digest(header_value.encode(), expected.encode())


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if _is_health_path(self.path):
            # Public liveness only: no auth, no env vars, no internals.
            self._json(200, {"ok": True})
            return

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
            # Surface the exact exception (type + traceback) to runtime logs
            # with environment secrets redacted; never leak it in the response.
            _log_exception()
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
