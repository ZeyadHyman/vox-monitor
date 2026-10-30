"""Movie registry for the monitored titles."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Movie:
    slug: str
    name: str
    movie_id: str


MOVIES: tuple[Movie, ...] = (
    Movie("avengers-doomsday", "Avengers: Doomsday", "HO00013527"),
    Movie(
        "avengers-doomsday-infinity-vision",
        "Avengers: Doomsday (Infinity Vision)",
        "HO00013676",
    ),
)

_BY_SLUG = {m.slug: m for m in MOVIES}


def by_slug(slug: str) -> Movie | None:
    return _BY_SLUG.get(slug)


def movie_slugs() -> tuple[str, ...]:
    return tuple(m.slug for m in MOVIES)
