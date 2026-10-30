"""Dynamically discovered movies — no hardcoded registry.

Movies are discovered from the server-rendered Almaza showtimes pages:
every ``<article class="movie-compare" data-slug="...">`` contributes a
slug, a display name (its ``<h2>``), and a movie id (the ``B_<HO id>_``
prefix of its hero image) when those are available. The set updates itself
automatically as films come on or drop off sale — nothing is pinned to a
single title.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class Movie:
    slug: str
    name: str
    movie_id: str


def merge_movies(groups: Iterable[Iterable[Movie]]) -> list[Movie]:
    """Union movies by slug, filling missing name/id from any group.

    The same film appears on several date pages; identity is the slug.
    Names/ids missing on one page (e.g. a partially rendered article) are
    filled in from another page where available.
    """
    merged: dict[str, Movie] = {}
    for group in groups:
        for movie in group:
            if not movie.slug:
                continue
            prev = merged.get(movie.slug)
            if prev is None:
                merged[movie.slug] = movie
            else:
                merged[movie.slug] = Movie(
                    slug=movie.slug,
                    name=prev.name or movie.name,
                    movie_id=prev.movie_id or movie.movie_id,
                )
    return list(merged.values())
