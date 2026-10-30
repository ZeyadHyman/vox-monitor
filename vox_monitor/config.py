"""Environment-driven configuration. Secrets are never logged or echoed."""

from __future__ import annotations

import os
from dataclasses import dataclass

BASE_URL = "https://egy.voxcinemas.com"

_REQUIRED = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "UPSTASH_REDIS_REST_URL",
    "UPSTASH_REDIS_REST_TOKEN",
)


class ConfigError(Exception):
    """Raised when required environment variables are missing or malformed."""


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_chat_id: str
    redis_url: str
    redis_token: str
    monitor_secret: str | None = None
    cron_secret: str | None = None
    dry_run: bool = False
    test_chat_id: str | None = None
    max_workers: int = 3
    request_timeout: float = 12.0
    cycle_budget_seconds: float = 45.0
    politeness_delay: float = 0.4
    # Legacy name kept for env compatibility (MAX_DATES_PER_CINEMA); the
    # project now monitors one cinema, so this caps dates per cycle. The
    # default covers VOX's entire advance-sale horizon (currently ~13
    # dates); the cycle time budget is the real safety limit.
    max_dates_per_cinema: int = 60
    max_retries: int = 1

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        e = os.environ if env is None else env
        missing = [name for name in _REQUIRED if not e.get(name)]
        if missing:
            raise ConfigError(
                "Missing required environment variables: " + ", ".join(missing)
            )
        return cls(
            telegram_bot_token=e["TELEGRAM_BOT_TOKEN"],
            telegram_chat_id=e["TELEGRAM_CHAT_ID"],
            redis_url=e["UPSTASH_REDIS_REST_URL"].rstrip("/"),
            redis_token=e["UPSTASH_REDIS_REST_TOKEN"],
            monitor_secret=e.get("MONITOR_SECRET") or None,
            cron_secret=e.get("CRON_SECRET") or None,
            dry_run=e.get("DRY_RUN", "").strip() in ("1", "true", "yes"),
            test_chat_id=e.get("TEST_NOTIFY_CHAT_ID") or None,
            max_workers=_int_env(e, "MAX_WORKERS", 3),
            request_timeout=_float_env(e, "REQUEST_TIMEOUT", 12.0),
            cycle_budget_seconds=_float_env(e, "CYCLE_BUDGET_SECONDS", 45.0),
            politeness_delay=_float_env(e, "POLITENESS_DELAY", 0.4),
            max_dates_per_cinema=_int_env(e, "MAX_DATES_PER_CINEMA", 60),
            max_retries=_int_env(e, "MAX_RETRIES", 1),
        )


def _int_env(e: dict[str, str], name: str, default: int) -> int:
    raw = e.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"Environment variable {name} must be an integer")


def _float_env(e: dict[str, str], name: str, default: float) -> float:
    raw = e.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"Environment variable {name} must be a number")


def showtimes_url(cinema_slug: str, date: str | None = None) -> str:
    """Showtimes URL for a cinema date.

    Deliberately NO ``m=`` (movie) parameters: omitting them makes VOX
    render every movie playing at that cinema for the date, which is how
    the monitor discovers all films automatically instead of a fixed list.
    """
    from urllib.parse import urlencode

    params: list[tuple[str, str]] = [("c", cinema_slug)]
    if date:
        params.append(("d", date))
    return f"{BASE_URL}/showtimes?{urlencode(params)}"


def booking_url(session_id: str) -> str:
    return f"{BASE_URL}/booking/{session_id}"
