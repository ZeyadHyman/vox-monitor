"""Cinema discovery with a configurable fallback list.

The VOX Egypt site currently operates three cinemas. Discovery reads them
from the server-rendered /cinemas page so new locations are picked up
automatically; if that page is unreachable the fallback list is used.
"""

from __future__ import annotations

from .config import BASE_URL, is_safe_slug
from .http import HttpClient, HttpFetchError
from .models import Cinema
from .showtimes import parse_cinemas_page

FALLBACK_CINEMAS: tuple[Cinema, ...] = (
    Cinema("city-centre-almaza", "City Centre Almaza"),
    Cinema("city-centre-alexandria", "City Centre Alexandria"),
    Cinema("mall-of-egypt", "Mall of Egypt"),
)


def discover_cinemas(
    client: HttpClient, override_slugs: tuple[str, ...] | None = None
) -> tuple[list[Cinema], bool]:
    """Return (cinemas, discovered). Never raises."""
    if override_slugs:
        slugs = [s for s in override_slugs if is_safe_slug(s)]
        known = {c.slug: c.name for c in FALLBACK_CINEMAS}
        return [Cinema(s, known.get(s, s)) for s in slugs], False

    try:
        resp = client.get(f"{BASE_URL}/cinemas")
    except HttpFetchError:
        return list(FALLBACK_CINEMAS), False
    if resp.status != 200:
        return list(FALLBACK_CINEMAS), False
    cinemas = parse_cinemas_page(resp.text)
    if not cinemas:
        return list(FALLBACK_CINEMAS), False
    return cinemas, True
