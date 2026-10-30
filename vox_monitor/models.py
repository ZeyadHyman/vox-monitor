"""Core data models: availability, sessions, parsed pages, cycle reports."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from .movies import Movie


class Availability(str, Enum):
    BOOKABLE = "bookable"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Session:
    movie_slug: str
    movie_name: str
    movie_id: str
    cinema_slug: str
    cinema_name: str
    date: str  # YYYYMMDD
    time_label: str  # e.g. "10:45am" exactly as VOX displays it
    fmt: str  # e.g. "MAX", "GOLD", "Standard"
    session_id: str  # e.g. "0047-276988"
    availability: Availability
    booking_url: str | None = None

    @property
    def key(self) -> str:
        return "|".join(
            (
                self.movie_id,
                self.cinema_slug,
                self.date,
                self.time_label,
                self.fmt,
                self.session_id,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["availability"] = self.availability.value
        return d


@dataclass(frozen=True)
class Cinema:
    slug: str
    name: str


@dataclass
class ShowtimePage:
    cinema_slug: str
    date: str  # YYYYMMDD; may be "" if unknown
    sessions: list[Session] = field(default_factory=list)
    nav_dates: list[str] = field(default_factory=list)  # YYYYMMDD dates offered by VOX
    # Movies listed on this page (article slug/name/id), including movies
    # that have no sessions at all for this date.
    movies: list[Movie] = field(default_factory=list)


@dataclass
class SessionState:
    status: str  # Availability value
    first_seen: str  # ISO timestamp
    last_seen: str  # ISO timestamp
    notified_at: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "notified_at": self.notified_at,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "SessionState":
        return cls(
            status=str(data.get("status", "")),
            first_seen=str(data.get("first_seen", "")),
            last_seen=str(data.get("last_seen", "")),
            notified_at=data.get("notified_at"),
        )


@dataclass
class CycleReport:
    ok: bool
    busy: bool = False
    partial: bool = False
    dry_run: bool = False
    cinemas_checked: int = 0
    movies_discovered: int = 0
    pages_fetched: int = 0
    pages_failed: int = 0
    sessions_seen: int = 0
    bookable_seen: int = 0
    new_bookable: list[dict[str, Any]] = field(default_factory=list)
    notified: bool = False
    notify_error: str | None = None
    state_error: str | None = None
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
