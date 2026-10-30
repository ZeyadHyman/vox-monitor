"""Server-rendered VOX showtimes page parser.

Verified page structure (egy.voxcinemas.com/showtimes?c=...&m=...&d=...):

    <section class="showtimes">
      <h1>...
        <form class="auto-submit" action="/showtimes" method="get">
          <input type="hidden" name="c" value="city-centre-almaza"/>
          <input type="hidden" name="m" value="..."/>   (repeatable)
          <input type="hidden" name="d" value="20261223"/>
      </h1>
      <nav class="date-filter">
        <a href="/showtimes?...&d=20261216">Wed 16 Dec</a> ...
        <span>Wed 23 Dec</span>            <- current date is not a link
      </nav>
      <article class="movie-compare" data-slug="avengers-doomsday-infinity-vision">
        <aside class="movie-hero"><div><h2>Name</h2>...</div></aside>
        <div class="dates">
          <h3 class="highlight">City Centre Almaza</h3>
          <ol class="showtimes">
            <li>
              <strong>MAX</strong>
              <ol>
                <li data-id="0047-276988">
                  <a class="action showtime" href="https://.../booking/0047-276988"
                     data-id="0047-276988">10:45am</a>
                </li>
                <li data-id="0047-276797">
                  <span class="action showtime unavailable">10:30pm</span>
                </li>
              </ol>
            </li>
          </ol>
        </div>
      </article>
    </section>

Availability contract (server-side, rendered by VOX itself):
  * ``<a class="action showtime" href=".../booking/ID">``  -> BOOKABLE
  * ``<span class="action showtime unavailable">``         -> UNAVAILABLE
  * session ``<li data-id=...>`` with neither marker       -> UNKNOWN
"""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin

from .config import BASE_URL, booking_url
from .models import Availability, Cinema, Session, ShowtimePage
from .movies import by_slug

_D_PARAM_RE = re.compile(r"[?&]d=(\d{8})")


class _RawSession:
    __slots__ = ("movie_slug", "cinema_name", "fmt", "time_label", "session_id", "availability", "href")

    def __init__(self) -> None:
        self.movie_slug: str = ""
        self.cinema_name: str = ""
        self.fmt: str = ""
        self.time_label: str = ""
        self.session_id: str = ""
        self.availability: Availability = Availability.UNKNOWN
        self.href: str | None = None


class ShowtimesParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.default_date: str | None = None
        self.nav_dates: list[str] = []
        self.raw_sessions: list[_RawSession] = []
        self.empty_state = False

        self._stack: list[tuple[str, frozenset[str]]] = []
        self._in_date_nav = False
        self._nav_href: str | None = None
        self._in_movie = False
        self._movie_slug: str = ""
        self._capture_movie_name = False
        self._movie_name_parts: list[str] = []
        self._current_cinema_name = ""
        self._capture_cinema_name = False
        self._cinema_name_parts: list[str] = []
        self._in_base_ol = False
        self._base_ol_depth = -1
        self._capture_format = False
        self._format_parts: list[str] = []
        self._format = ""
        self._session: _RawSession | None = None
        self._session_li_depth = -1
        self._capture_time = False
        self._time_parts: list[str] = []
        self._in_form = False

    # -- helpers -----------------------------------------------------------

    def _classes(self, attrs: list[tuple[str, str | None]]) -> frozenset[str]:
        for name, value in attrs:
            if name == "class" and value:
                return frozenset(value.split())
        return frozenset()

    def _attr(self, attrs: list[tuple[str, str | None]], key: str) -> str | None:
        for name, value in attrs:
            if name == key:
                return value
        return None

    # -- parser ------------------------------------------------------------

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        tag = tag.lower()
        classes = self._classes(attrs)
        self._stack.append((tag, classes))

        if tag == "nav" and "date-filter" in classes:
            self._in_date_nav = True
        elif self._in_date_nav and tag == "a":
            self._nav_href = self._attr(attrs, "href")

        if tag == "form" and "auto-submit" in classes:
            self._in_form = True
        elif self._in_form and tag == "input":
            name = self._attr(attrs, "name")
            value = self._attr(attrs, "value") or ""
            if name == "d" and re.fullmatch(r"\d{8}", value):
                self.default_date = value

        if tag == "article" and "movie-compare" in classes:
            self._in_movie = True
            self._movie_slug = self._attr(attrs, "data-slug") or ""

        if tag == "h2" and self._in_movie:
            self._capture_movie_name = True
            self._movie_name_parts = []

        if tag == "h3" and "highlight" in classes:
            self._capture_cinema_name = True
            self._cinema_name_parts = []

        if tag == "ol" and "showtimes" in classes:
            self._in_base_ol = True
            self._base_ol_depth = len(self._stack)

        if (
            tag == "strong"
            and self._in_base_ol
            and self._session is None
            and len(self._stack) >= 3
            and self._stack[-2][0] == "li"
            and self._stack[-3][0] == "ol"
            and "showtimes" in self._stack[-3][1]
        ):
            self._capture_format = True
            self._format_parts = []

        if tag == "li" and self._in_base_ol:
            data_id = self._attr(attrs, "data-id")
            if data_id and self._session is None:
                self._session = _RawSession()
                self._session.movie_slug = self._movie_slug
                self._session.session_id = data_id
                self._session_li_depth = len(self._stack)
                self._capture_time = True
                self._time_parts = []

        if self._session is not None and tag in ("a", "span"):
            if "action" in classes and "showtime" in classes:
                if "unavailable" in classes:
                    self._session.availability = Availability.UNAVAILABLE
                elif tag == "a":
                    href = self._attr(attrs, "href")
                    if href and "/booking/" in href:
                        self._session.availability = Availability.BOOKABLE
                        self._session.href = href
                    else:
                        self._session.availability = Availability.UNKNOWN
                else:
                    self._session.availability = Availability.UNKNOWN

        if tag == "p" and "empty" in classes:
            self.empty_state = True

    def handle_data(self, data: str) -> None:
        if self._capture_movie_name:
            self._movie_name_parts.append(data)
        if self._capture_cinema_name:
            self._cinema_name_parts.append(data)
        if self._capture_format:
            self._format_parts.append(data)
        if self._capture_time and self._session is not None:
            self._time_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()

        if tag == "a" and self._in_date_nav and self._nav_href is not None:
            m = _D_PARAM_RE.search(unescape(self._nav_href))
            if m and m.group(1) not in self.nav_dates:
                self.nav_dates.append(m.group(1))
            self._nav_href = None

        if tag == "nav":
            self._in_date_nav = False
        if tag == "form":
            self._in_form = False

        if tag == "h2" and self._capture_movie_name:
            self._capture_movie_name = False

        if tag == "h3" and self._capture_cinema_name:
            name = " ".join("".join(self._cinema_name_parts).split())
            if name:
                self._current_cinema_name = name
            self._capture_cinema_name = False

        if tag == "strong" and self._capture_format:
            self._format = " ".join("".join(self._format_parts).split())
            self._capture_format = False

        if tag == "li" and self._session is not None and len(self._stack) == self._session_li_depth:
            self._session.time_label = " ".join("".join(self._time_parts).split()).lower()
            self._session.fmt = self._format
            self._session.cinema_name = self._current_cinema_name
            self.raw_sessions.append(self._session)
            self._session = None
            self._session_li_depth = -1
            self._capture_time = False
            self._time_parts = []

        if tag == "article":
            self._in_movie = False
            self._movie_slug = ""

        if tag == "ol" and self._in_base_ol and len(self._stack) == self._base_ol_depth:
            self._in_base_ol = False
            self._base_ol_depth = -1
            self._format = ""

        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                del self._stack[i:]
                break

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)


def parse_showtimes_page(
    html: str, cinema_slug: str, requested_date: str | None = None
) -> ShowtimePage:
    parser = ShowtimesParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # A malformed page must never crash a monitoring cycle; treat as empty.
        return ShowtimePage(cinema_slug=cinema_slug, date=requested_date or "")

    date = requested_date or parser.default_date or ""
    sessions: list[Session] = []
    for raw in parser.raw_sessions:
        movie = by_slug(raw.movie_slug)
        movie_name = movie.name if movie else (raw.movie_slug or "UNKNOWN")
        movie_id = movie.movie_id if movie else "UNKNOWN"
        if raw.availability == Availability.BOOKABLE:
            url = raw.href or booking_url(raw.session_id)
            booking = urljoin(BASE_URL, url)
        else:
            booking = None
        sessions.append(
            Session(
                movie_slug=raw.movie_slug,
                movie_name=movie_name,
                movie_id=movie_id,
                cinema_slug=cinema_slug,
                cinema_name=raw.cinema_name or cinema_slug,
                date=date,
                time_label=raw.time_label,
                fmt=raw.fmt,
                session_id=raw.session_id,
                availability=raw.availability,
                booking_url=booking,
            )
        )

    nav_dates = sorted(set(parser.nav_dates))
    return ShowtimePage(
        cinema_slug=cinema_slug,
        date=date,
        sessions=sessions,
        nav_dates=nav_dates,
    )


def parse_cinemas_page(html: str) -> list[Cinema]:
    """Extract cinemas from https://egy.voxcinemas.com/cinemas."""
    found: dict[str, str] = {}
    parser = _CinemasParser(found)
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass
    return [Cinema(slug=s, name=n) for s, n in found.items()]


class _CinemasParser(HTMLParser):
    def __init__(self, found: dict[str, str]) -> None:
        super().__init__(convert_charrefs=True)
        self._found = found
        self._href: str | None = None
        self._parts: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag.lower() != "a" or self._href is not None:
            return
        for name, value in attrs:
            if name == "href" and value and value.startswith("/cinemas/"):
                slug = value[len("/cinemas/") :].strip("/")
                if slug and "/" not in slug and "?" not in slug:
                    self._href = slug
                    self._parts = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href is not None:
            name = " ".join("".join(self._parts).split())
            if name and self._href not in self._found:
                self._found[self._href] = name
            self._href = None
            self._parts = []
