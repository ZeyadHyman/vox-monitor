"""Parser tests against real VOX pages captured from egy.voxcinemas.com."""

from vox_monitor.models import Availability
from vox_monitor.showtimes import parse_showtimes_page


def test_parses_real_almaza_23dec_page(fixture_html):
    page = parse_showtimes_page(
        fixture_html("showtimes_almaza_20261223.html"),
        "city-centre-almaza",
        requested_date="20261223",
    )
    assert page.date == "20261223"
    assert page.nav_dates == [
        "20261216", "20261217", "20261218", "20261219",
        "20261220", "20261221", "20261222",
    ]
    assert len(page.sessions) == 20
    bookable = [s for s in page.sessions if s.availability == Availability.BOOKABLE]
    unavailable = [s for s in page.sessions if s.availability == Availability.UNAVAILABLE]
    assert len(bookable) == 17
    assert len(unavailable) == 3


def test_bookable_session_fields(fixture_html):
    page = parse_showtimes_page(
        fixture_html("showtimes_almaza_20261223.html"),
        "city-centre-almaza",
        requested_date="20261223",
    )
    session = next(s for s in page.sessions if s.session_id == "0047-276988")
    assert session.movie_slug == "avengers-doomsday-infinity-vision"
    assert session.movie_name == "Avengers: Doomsday (Infinity Vision)"
    assert session.movie_id == "HO00013676"
    assert session.cinema_slug == "city-centre-almaza"
    assert session.cinema_name == "City Centre Almaza"
    assert session.time_label == "10:45am"
    assert session.fmt == "MAX"
    assert session.availability == Availability.BOOKABLE
    assert session.booking_url == "https://egy.voxcinemas.com/booking/0047-276988"


def test_unavailable_sessions_have_no_booking_url(fixture_html):
    page = parse_showtimes_page(
        fixture_html("showtimes_almaza_20261223.html"),
        "city-centre-almaza",
        requested_date="20261223",
    )
    unavailable = [s for s in page.sessions if s.availability == Availability.UNAVAILABLE]
    ids = {s.session_id for s in unavailable}
    assert ids == {"0047-276797", "0047-276976", "0047-276980"}
    for s in unavailable:
        assert s.booking_url is None


def test_formats_and_movies_captured(fixture_html):
    page = parse_showtimes_page(
        fixture_html("showtimes_almaza_20261223.html"),
        "city-centre-almaza",
        requested_date="20261223",
    )
    formats = {s.fmt for s in page.sessions}
    assert formats == {"MAX", "GOLD", "Standard"}
    movies = {s.movie_slug for s in page.sessions}
    assert movies == {"avengers-doomsday", "avengers-doomsday-infinity-vision"}


def test_default_page_date_from_hidden_input(fixture_html):
    page = parse_showtimes_page(
        fixture_html("showtimes_almaza_default.html"),
        "city-centre-almaza",
        requested_date=None,
    )
    assert page.date == "20261216"
    assert len(page.sessions) == 20
    # Opening day is fully sold out at capture time: no BOOKABLE sessions.
    assert all(s.availability != Availability.BOOKABLE for s in page.sessions)


def test_empty_showtimes_page_yields_no_sessions(fixture_html):
    page = parse_showtimes_page(
        fixture_html("showtimes_empty_20270401.html"),
        "city-centre-almaza",
        requested_date="20270401",
    )
    assert page.sessions == []
    assert page.date == "20270401"


def test_malformed_html_does_not_crash():
    page = parse_showtimes_page("<<< not html >>><div", "city-centre-almaza", None)
    assert page.sessions == []


def test_session_li_without_marker_is_unknown():
    html = """
    <section class="showtimes">
      <article class="movie-compare" data-slug="avengers-doomsday">
        <div class="dates">
          <h3 class="highlight">City Centre Almaza</h3>
          <ol class="showtimes">
            <li>
              <strong>Standard</strong>
              <ol>
                <li data-id="0047-999999"><span>mystery</span></li>
              </ol>
            </li>
          </ol>
        </div>
      </article>
    </section>
    """
    page = parse_showtimes_page(html, "city-centre-almaza", "20261223")
    assert len(page.sessions) == 1
    s = page.sessions[0]
    assert s.availability == Availability.UNKNOWN
    assert s.booking_url is None
    assert s.session_id == "0047-999999"


def test_anchor_without_booking_href_is_unknown():
    html = """
    <section class="showtimes">
      <article class="movie-compare" data-slug="avengers-doomsday">
        <div class="dates">
          <h3 class="highlight">City Centre Almaza</h3>
          <ol class="showtimes">
            <li>
              <strong>Standard</strong>
              <ol>
                <li data-id="0047-999998">
                  <a class="action showtime" href="#">10:00pm</a>
                </li>
              </ol>
            </li>
          </ol>
        </div>
      </article>
    </section>
    """
    page = parse_showtimes_page(html, "city-centre-almaza", "20261223")
    s = page.sessions[0]
    assert s.availability == Availability.UNKNOWN
    assert s.booking_url is None
    assert s.time_label == "10:00pm"


# ---- all-movie discovery from the page (no hardcoded registry) -----------


def test_page_lists_all_movies_with_names_and_ids(fixture_html):
    page = parse_showtimes_page(
        fixture_html("showtimes_almaza_live_default.html"),
        "city-centre-almaza",
        requested_date=None,
    )
    # 20 distinct movies on this real Almaza page — none hardcoded.
    assert len(page.movies) == 20
    for movie in page.movies:
        assert movie.slug
        assert movie.name, f"missing name for {movie.slug}"
        assert movie.movie_id.startswith("HO"), f"missing id for {movie.slug}"
    slugs = {m.slug for m in page.movies}
    assert "clayface" in slugs
    assert "street-fighter" in slugs


def test_sessions_of_different_movies_have_distinct_keys(fixture_html):
    page = parse_showtimes_page(
        fixture_html("showtimes_almaza_live_20261031.html"),
        "city-centre-almaza",
        requested_date="20261031",
    )
    movies_in_sessions = {s.movie_slug for s in page.sessions}
    assert len(movies_in_sessions) >= 10  # many different films in one page
    keys = [s.key for s in page.sessions]
    assert len(keys) == len(set(keys)), "session keys must be unique across movies"
    # movie_id travels with the session so state can never mix films
    by_movie = {}
    for s in page.sessions:
        by_movie.setdefault(s.movie_slug, set()).add(s.movie_id)
    for slug, ids in by_movie.items():
        assert len(ids) == 1, f"{slug} mapped to multiple ids {ids}"


def test_movie_with_no_sessions_is_still_discovered():
    html = """
    <section class="showtimes">
      <article class="movie-compare" data-slug="future-film">
        <aside class="movie-hero">
          <img class="hero" src="https://assets.voxcinemas.com/heroes/B_HO00099999_1.jpg"/>
          <div><h2>Future Film</h2></div>
        </aside>
        <div class="dates">
          <h3 class="highlight">City Centre Almaza</h3>
          <ol class="showtimes"></ol>
        </div>
      </article>
    </section>
    """
    page = parse_showtimes_page(html, "city-centre-almaza", "20261223")
    assert page.sessions == []
    assert [m.slug for m in page.movies] == ["future-film"]
    assert page.movies[0].name == "Future Film"
    assert page.movies[0].movie_id == "HO00099999"


def test_movie_id_falls_back_to_slug_when_hero_missing():
    html = """
    <section class="showtimes">
      <article class="movie-compare" data-slug="no-hero-yet">
        <aside class="movie-hero"><div><h2>No Hero Yet</h2></div></aside>
        <div class="dates">
          <h3 class="highlight">City Centre Almaza</h3>
          <ol class="showtimes">
            <li>
              <strong>Standard</strong>
              <ol>
                <li data-id="0047-111222">
                  <a class="action showtime" href="https://egy.voxcinemas.com/booking/0047-111222">7:00pm</a>
                </li>
              </ol>
            </li>
          </ol>
        </div>
      </article>
    </section>
    """
    page = parse_showtimes_page(html, "city-centre-almaza", "20261223")
    s = page.sessions[0]
    assert s.movie_name == "No Hero Yet"
    assert s.movie_id == "no-hero-yet"  # slug fallback keeps identity unique
    assert s.key.startswith("no-hero-yet|")
