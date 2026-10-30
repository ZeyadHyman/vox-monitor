"""Environment-driven configuration. Secrets are never logged or echoed."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

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
    max_dates_per_cinema: int = 21
    max_retries: int = 1
    cinema_slugs_override: tuple[str, ...] | None = field(default=None)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        e = os.environ if env is None else env
        missing = [name for name in _REQUIRED if not e.get(name)]
        if missing:
            raise ConfigError(
                "Missing required environment variables: " + ", ".join(missing)
            )
        override_raw = (e.get("CINEMA_SLUGS") or "").strip()
        override: tuple[str, ...] | None = None
        if override_raw:
            override = tuple(s.strip() for s in override_raw.split(",") if s.strip())
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
            max_dates_per_cinema=_int_env(e, "MAX_DATES_PER_CINEMA", 21),
            max_retries=_int_env(e, "MAX_RETRIES", 1),
            cinema_slugs_override=override,
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


def showtimes_url(
    cinema_slug: str, movie_slugs: tuple[str, ...], date: str | None = None
) -> str:
    from urllib.parse import urlencode

    params: list[tuple[str, str]] = [("c", cinema_slug)]
    params += [("m", s) for s in movie_slugs]
    if date:
        params.append(("d", date))
    return f"{BASE_URL}/showtimes?{urlencode(params)}"


def showtimes_page_url(cinema_slug: str, movie_slug: str, date: str) -> str:
    from urllib.parse import urlencode

    return (
        f"{BASE_URL}/showtimes?"
        + urlencode([("c", cinema_slug), ("m", movie_slug), ("d", date)])
    )


def booking_url(session_id: str) -> str:
    return f"{BASE_URL}/booking/{session_id}"


def is_safe_slug(slug: str) -> bool:
    return bool(slug) and all(c.isalnum() or c in "-_" for c in slug)
