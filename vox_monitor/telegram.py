"""Telegram notifications via the existing bot (HTTP API, no libraries)."""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime
from typing import Protocol

from .models import Session


class TelegramError(Exception):
    pass


class Notifier(Protocol):
    def send(self, text: str) -> None: ...


def format_date_yyyymmdd(date: str) -> str:
    """'20261219' -> '19 December 2026'."""
    try:
        dt = datetime.strptime(date, "%Y%m%d")
    except ValueError:
        return date
    return f"{dt.day} {dt.strftime('%B %Y')}"


def format_alert_message(sessions: list[Session]) -> str:
    """Build one message for a batch of newly bookable sessions."""
    if not sessions:
        raise ValueError("no sessions")
    lines = ["🎬 NEW TICKETS AVAILABLE", ""]
    for s in sessions:
        lines.append(f"🎥 Movie: {s.movie_name}")
        lines.append(f"📍 Cinema: {s.cinema_name}")
        lines.append(f"📅 Date: {format_date_yyyymmdd(s.date)}")
        lines.append(f"🕒 Time: {s.time_label}")
        lines.append(f"🎟 Format: {s.fmt}")
        lines.append("✅ Booking status: Confirmed bookable")
        if s.booking_url:
            lines.append(f"🔗 Book now: {s.booking_url}")
        else:
            lines.append(
                "🔗 VOX showtimes: "
                f"https://egy.voxcinemas.com/showtimes?c={s.cinema_slug}"
                f"&m={s.movie_slug}&d={s.date}"
            )
        lines.append("")
    lines.append("_Deduplicated by session ID; you will not be re-alerted for these._")
    return "\n".join(lines)


TELEGRAM_MESSAGE_LIMIT = 4096
_CHUNK_TARGET = 3400  # headroom for escaping / encoding overhead


def format_alert_chunks(sessions: list[Session]) -> list[str]:
    """Split newly bookable sessions into Telegram-sized messages."""
    chunks: list[str] = []
    current: list[Session] = []
    for session in sessions:
        candidate = current + [session]
        text = format_alert_message(candidate)
        if len(text) > _CHUNK_TARGET and current:
            chunks.append(format_alert_message(current))
            current = [session]
        else:
            current = candidate
    if current:
        chunks.append(format_alert_message(current))
    for chunk in chunks:
        if len(chunk) > TELEGRAM_MESSAGE_LIMIT:  # defensive; single-session guard
            chunks = [c[: TELEGRAM_MESSAGE_LIMIT - 1] + "…" for c in chunks]
            break
    return chunks


class TelegramNotifier:
    def __init__(
        self, bot_token: str, chat_id: str, timeout: float = 15.0
    ) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._timeout = timeout

    def send(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        payload = json.dumps(
            {
                "chat_id": self._chat_id,
                "text": text,
                "disable_web_page_preview": True,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise TelegramError(f"Telegram request failed: {type(exc).__name__}") from exc
        if not isinstance(body, dict) or not body.get("ok"):
            raise TelegramError("Telegram sendMessage returned ok=false")


class NullNotifier:
    """Used in dry-run mode: records messages instead of sending them."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, text: str) -> None:
        self.messages.append(text)
