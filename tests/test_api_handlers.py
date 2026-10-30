"""API handler tests: /api/health is public; /api/check still requires its secret."""

import json

import pytest

from api.check import _is_health_path, handler as check_handler
from api.health import handler as health_handler


class _FakeWfile:
    def __init__(self) -> None:
        self.body = b""

    def write(self, data: bytes) -> None:
        self.body += data


def run_handler(handler_cls, path: str, headers: dict | None = None):
    """Exercise do_GET on a BaseHTTPRequestHandler subclass without a socket."""
    h = object.__new__(handler_cls)
    h.path = path
    h.headers = headers or {}
    h.command = "GET"
    h.request_version = "HTTP/1.1"
    h.client_address = ("127.0.0.1", 12345)
    captured: dict = {"status": None}

    def _send_response(code, message=None):
        captured["status"] = code

    h.send_response = _send_response
    h.send_header = lambda *a, **k: None
    h.end_headers = lambda: None
    h.wfile = _FakeWfile()
    h.log_message = lambda *a, **k: None
    h.do_GET()
    body = json.loads(h.wfile.body.decode("utf-8") or "{}")
    return captured["status"], body


def _clear_env(monkeypatch):
    for name in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "UPSTASH_REDIS_REST_URL",
        "UPSTASH_REDIS_REST_TOKEN",
        "MONITOR_SECRET",
        "CRON_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)


# ---- /api/health: public -------------------------------------------------


def test_health_public_without_auth_or_env(monkeypatch):
    _clear_env(monkeypatch)
    status, body = run_handler(check_handler, "/api/health")
    assert status == 200
    assert body == {"ok": True}


def test_health_with_query_string(monkeypatch):
    _clear_env(monkeypatch)
    status, body = run_handler(check_handler, "/api/health?ts=1730000000")
    assert status == 200
    assert body == {"ok": True}


def test_health_with_trailing_slash(monkeypatch):
    _clear_env(monkeypatch)
    status, body = run_handler(check_handler, "/api/health/")
    assert status == 200
    assert body == {"ok": True}


def test_health_with_env_present_leaks_nothing(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "super-secret-token")
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "super-redis-token")
    monkeypatch.setenv("MONITOR_SECRET", "super-monitor-secret")
    status, body = run_handler(check_handler, "/api/health")
    assert status == 200
    assert body == {"ok": True}
    raw = json.dumps(body)
    for secret in ("super-secret-token", "super-redis-token", "super-monitor-secret"):
        assert secret not in raw


def test_health_prefix_recognition():
    assert _is_health_path("/api/health")
    assert _is_health_path("/api/health/")
    assert _is_health_path("/api/health?x=1")
    assert _is_health_path("/health")
    assert not _is_health_path("/api/check")
    assert not _is_health_path("/api/healthcheck")
    assert not _is_health_path("/")


def test_standalone_health_handler_is_public(monkeypatch):
    _clear_env(monkeypatch)
    status, body = run_handler(health_handler, "/api/health")
    assert status == 200
    assert body == {"ok": True}


# ---- /api/check: authentication intact -----------------------------------


def _set_check_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://redis.example")
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "r")
    monkeypatch.setenv("MONITOR_SECRET", "right-secret")


def test_check_without_secret_is_401(monkeypatch):
    _set_check_env(monkeypatch)
    status, body = run_handler(check_handler, "/api/check")
    assert status == 401
    assert body == {"ok": False, "error": "Unauthorized"}


def test_check_with_wrong_secret_is_401(monkeypatch):
    _set_check_env(monkeypatch)
    status, body = run_handler(
        check_handler, "/api/check", {"x-monitor-secret": "wrong"}
    )
    assert status == 401
    assert body == {"ok": False, "error": "Unauthorized"}


def test_check_with_wrong_bearer_is_401(monkeypatch):
    _set_check_env(monkeypatch)
    status, body = run_handler(
        check_handler, "/api/check", {"Authorization": "Bearer wrong"}
    )
    assert status == 401


def test_check_missing_env_names_only(monkeypatch):
    _clear_env(monkeypatch)
    status, body = run_handler(
        check_handler, "/api/check", {"x-monitor-secret": "s"}
    )
    assert status == 500
    assert "TELEGRAM_BOT_TOKEN" in body["error"]
    assert "TELEGRAM_BOT_TOKEN" == body["error"].split(": ")[-1].split(", ")[0]


def test_check_with_valid_secret_reaches_cycle(monkeypatch):
    _set_check_env(monkeypatch)
    import api.check as check_module

    calls = {"cycle": 0}

    class _FakeClient:
        def __init__(self, **kwargs):
            pass

    class _FakeStore:
        def __init__(self, *a, **k):
            pass

    class _FakeNotifier:
        def __init__(self, *a, **k):
            pass

    def _fake_cycle(*a, **k):
        calls["cycle"] += 1
        from vox_monitor.models import CycleReport

        return CycleReport(ok=True)

    monkeypatch.setattr(check_module, "VoxHttpClient", _FakeClient)
    monkeypatch.setattr(check_module, "RedisRestStore", _FakeStore)
    monkeypatch.setattr(check_module, "TelegramNotifier", _FakeNotifier)
    monkeypatch.setattr(check_module, "run_cycle", _fake_cycle)

    status, body = run_handler(
        check_handler, "/api/check", {"x-monitor-secret": "right-secret"}
    )
    assert status == 200
    assert body["ok"] is True
    assert calls["cycle"] == 1
