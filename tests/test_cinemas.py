from vox_monitor.cinemas import FALLBACK_CINEMAS, discover_cinemas
from vox_monitor.http import HttpFetchError, HttpResponse
from vox_monitor.showtimes import parse_cinemas_page


def test_parses_cinemas_index(fixture_html):
    cinemas = parse_cinemas_page(fixture_html("cinemas_index.html"))
    by_slug = {c.slug: c.name for c in cinemas}
    assert by_slug == {
        "city-centre-almaza": "City Centre Almaza",
        "city-centre-alexandria": "City Centre Alexandria",
        "mall-of-egypt": "Mall of Egypt",
    }


class _FakeClient:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error

    def get(self, url):
        if self._error:
            raise self._error
        return self._response


def test_discovery_falls_back_on_fetch_error():
    client = _FakeClient(error=HttpFetchError("boom"))
    cinemas, discovered = discover_cinemas(client)
    assert discovered is False
    assert [c.slug for c in cinemas] == [c.slug for c in FALLBACK_CINEMAS]


def test_discovery_falls_back_on_empty_page():
    client = _FakeClient(response=HttpResponse(200, "<html></html>"))
    cinemas, discovered = discover_cinemas(client)
    assert discovered is False
    assert cinemas == list(FALLBACK_CINEMAS)


def test_override_slugs_used_without_fetch():
    client = _FakeClient(error=HttpFetchError("should not be called"))
    cinemas, discovered = discover_cinemas(client, ("mall-of-egypt",))
    assert discovered is False
    assert [(c.slug, c.name) for c in cinemas] == [("mall-of-egypt", "Mall of Egypt")]


def test_override_filters_unsafe_slugs():
    client = _FakeClient()
    cinemas, _ = discover_cinemas(client, ("mall-of-egypt", "../evil", "ok-slug"))
    assert [c.slug for c in cinemas] == ["mall-of-egypt", "ok-slug"]
