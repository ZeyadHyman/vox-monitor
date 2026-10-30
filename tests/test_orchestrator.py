"""Orchestrator tests using real VOX fixtures and fakes for HTTP/state/Telegram.

Fixture facts (parsed from real Almaza pages captured on 2026-10-30):
  * default (no d=): date=20261030, 13 nav dates, 20 movies, 44 sessions (41 bookable)
  * d=20261031: 22 movies, 77 sessions (73 bookable)
  * d=20261217:  3 movies, 24 sessions ( 8 bookable)
  * d=20261223:  3 movies, 24 sessions (21 bookable)
  * other dates: empty page (0 movies, 0 sessions)
  => one full cycle: 14 pages, 26 unique movies, 169 sessions, 143 bookable.
"""

from pathlib import Path

import pytest

from vox_monitor.config import Settings
from vox_monitor.http import HttpFetchError, HttpResponse
from vox_monitor.orchestrator import _expired_keys, run_cycle
from vox_monitor.state import MemoryStore, StateError
from vox_monitor.telegram import NullNotifier, TelegramError

FIXTURES = Path(__file__).parent / "fixtures"

FIXED_TS = "2026-10-30T12:00:00+00:00"


def _fixed_now() -> str:
    return FIXED_TS


def _fx(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_settings(**overrides) -> Settings:
    base = dict(
        telegram_bot_token="test-token",
        telegram_chat_id="123",
        redis_url="https://example.invalid",
        redis_token="test",
        dry_run=False,
        max_workers=3,
        request_timeout=1.0,
        cycle_budget_seconds=30.0,
        politeness_delay=0.0,
        max_dates_per_cinema=60,
        max_retries=0,
    )
    base.update(overrides)
    return Settings(**base)


class FakeVoxClient:
    """Serves real fixture pages based on URL parameters.

    ``routes`` maps a date param (or None for the no-date page) to a fixture
    file name, letting a test simulate pages changing between cycles.
    """

    DEFAULT_ROUTES = {
        None: "showtimes_almaza_live_default.html",
        "20261031": "showtimes_almaza_live_20261031.html",
        "20261217": "showtimes_almaza_live_20261217.html",
        "20261223": "showtimes_almaza_live_20261223.html",
    }
    EMPTY = "showtimes_empty_20270401.html"

    def __init__(self, fail_urls=(), fail_all=False, routes=None):
        self.fail_urls = tuple(fail_urls)
        self.fail_all = fail_all
        self.routes = dict(self.DEFAULT_ROUTES)
        if routes:
            self.routes.update(routes)
        self.calls: list[str] = []

    @staticmethod
    def _date_of(url: str):
        if "d=" not in url:
            return None
        for part in url.split("?")[-1].split("&"):
            if part.startswith("d="):
                return part[2:]
        return None

    def get(self, url: str) -> HttpResponse:
        self.calls.append(url)
        if self.fail_all:
            raise HttpFetchError("all fail")
        for bad in self.fail_urls:
            if bad in url:
                raise HttpFetchError("targeted fail")
        date = self._date_of(url)
        fixture = self.routes.get(date, self.EMPTY) if date is not None else self.routes[None]
        return HttpResponse(200, _fx(fixture))


class FailingStore(MemoryStore):
    def load_states(self):
        raise StateError("load boom")

    def save_states(self, entries):
        raise StateError("save boom")


class BoomNotifier(NullNotifier):
    def send(self, text):
        raise TelegramError("telegram boom")


def _cycle(client, store=None, notifier=None, settings=None):
    return run_cycle(
        settings or make_settings(),
        client,
        store if store is not None else MemoryStore(),
        notifier if notifier is not None else NullNotifier(),
        now=_fixed_now,
    )


# ---- full cycle over all dates -------------------------------------------


def test_first_cycle_discovers_all_movies_and_alerts():
    client = FakeVoxClient()
    store = MemoryStore()
    notifier = NullNotifier()

    report = _cycle(client, store, notifier)

    assert report.ok is True
    assert report.busy is False
    assert report.partial is False
    assert report.cinemas_checked == 1
    assert report.movies_discovered == 26
    assert report.pages_fetched == 14  # no-d page + 13 nav dates
    assert report.pages_failed == 0
    assert report.sessions_seen == 169
    assert report.bookable_seen == 143
    assert len(report.new_bookable) == 143
    assert report.notified is True
    assert len(notifier.messages) >= 1
    combined = "\n".join(notifier.messages)
    assert "TICKETS AVAILABLE" in combined
    assert "City Centre Almaza" in combined
    assert "https://egy.voxcinemas.com/booking/0047-276988" in combined
    states = store.load_states()
    assert len(states) == 169  # every session persisted, per-movie keys
    assert states


def test_only_almaza_is_requested_never_other_cinemas_or_movies_param():
    client = FakeVoxClient()
    _cycle(client)
    assert client.calls, "cycle made no requests"
    assert len(client.calls) == 14
    for url in client.calls:
        assert "c=city-centre-almaza" in url, url
        assert "mall-of-egypt" not in url
        assert "city-centre-alexandria" not in url
        assert "/cinemas" not in url  # no cinema-discovery request at all
        assert "&m=" not in url and "?m=" not in url  # all-movies via no m=


def test_second_cycle_deduplicates():
    store = MemoryStore()
    _cycle(FakeVoxClient(), store)

    notifier = NullNotifier()
    report = _cycle(FakeVoxClient(), store, notifier)
    assert report.ok is True
    assert report.new_bookable == []
    assert report.notified is False
    assert notifier.messages == []


def test_dry_run_sends_nothing_and_keeps_state_clean():
    store = MemoryStore()
    notifier = NullNotifier()

    report = _cycle(FakeVoxClient(), store, notifier, make_settings(dry_run=True))

    assert report.dry_run is True
    assert len(report.new_bookable) == 143
    assert notifier.messages == []
    assert store.load_states() == {}


def test_telegram_failure_persists_status_without_notified_stamp():
    store = MemoryStore()
    report = _cycle(FakeVoxClient(), store, BoomNotifier())
    assert report.notified is False
    assert report.notify_error == "TelegramError"
    states = store.load_states()
    assert states
    assert all(s.notified_at is None for s in states.values())
    assert any(s.status == "bookable" for s in states.values())

    # Next cycle with a working notifier re-alerts (never-lost alert).
    notifier = NullNotifier()
    report2 = _cycle(FakeVoxClient(), store, notifier)
    assert report2.notified is True
    assert len(report2.new_bookable) == 143


def test_all_fetches_failing_is_hard_failure():
    store = MemoryStore()
    notifier = NullNotifier()
    report = _cycle(FakeVoxClient(fail_all=True), store, notifier)
    assert report.ok is False
    assert report.pages_fetched == 0
    assert report.movies_discovered == 0
    assert notifier.messages == []
    assert store.load_states() == {}


# ---- partial failures never lose state -----------------------------------


def test_partial_failure_keeps_state_and_processes_other_pages():
    store = MemoryStore()
    # Cycle 1: everything healthy -> full state (incl. 17 Dec sessions).
    report1 = _cycle(FakeVoxClient(), store)
    assert report1.ok is True
    assert len(store.load_states()) == 169

    # Cycle 2: the 17 Dec page fails; everything else still runs.
    client = FakeVoxClient(fail_urls=("d=20261217",))
    notifier = NullNotifier()
    report2 = _cycle(client, store, notifier)

    assert report2.ok is True
    assert report2.partial is True
    assert report2.pages_failed == 1
    assert report2.pages_fetched == 13
    assert report2.sessions_seen == 145  # 169 - 24 (failed page)
    assert report2.movies_discovered == 26  # the 3 movies remain via 23 Dec
    # Nothing was deleted by the failure: state intact, no re-alerts.
    assert len(store.load_states()) == 169
    assert report2.new_bookable == []
    assert notifier.messages == []
    assert any("d=20261217" in e for e in report2.errors)


def test_failure_of_every_page_having_a_movie_does_not_erase_its_state():
    store = MemoryStore()
    _cycle(FakeVoxClient(), store)
    before = dict(store.load_states())

    # Both December pages (only pages with Avengers/Dune) fail now.
    client = FakeVoxClient(fail_urls=("d=20261217", "d=20261223"))
    report = _cycle(client, store)
    assert report.ok is True
    assert report.partial is True
    assert report.movies_discovered == 23  # Avenger/Dune pages gone this cycle
    after = store.load_states()
    assert len(after) == 169  # state from cycle 1 untouched
    for key, state in before.items():
        assert after[key].status == state.status
        assert after[key].notified_at == state.notified_at


# ---- dynamic movie discovery ---------------------------------------------


def test_new_movie_appearing_later_is_discovered_and_alerted():
    store = MemoryStore()
    # Cycle 1: the December pages exist but show no movies yet.
    client1 = FakeVoxClient(routes={"20261217": FakeVoxClient.EMPTY,
                                    "20261223": FakeVoxClient.EMPTY})
    report1 = _cycle(client1, store)
    assert report1.ok is True
    assert report1.movies_discovered == 23  # 20 default + 3 only-on-31-Oct
    assert len(report1.new_bookable) == 114

    # Cycle 2: tickets for Avengers: Doomsday & Dune: Part Three went on sale.
    notifier = NullNotifier()
    report2 = _cycle(FakeVoxClient(), store, notifier)
    assert report2.ok is True
    assert report2.movies_discovered == 26  # 3 new titles picked up
    assert len(report2.new_bookable) == 29  # only the genuinely new bookables
    combined = "\n".join(notifier.messages)
    assert "Avengers: Doomsday" in combined
    assert "City Centre Almaza" in combined
    assert report2.notified is True


def test_movie_leaving_sale_is_silently_dropped_from_future_discovery():
    store = MemoryStore()
    _cycle(FakeVoxClient(), store)
    # Now the default page has no movies (hypothetical takeover of the site).
    client = FakeVoxClient(routes={None: FakeVoxClient.EMPTY})
    report = _cycle(client, store)
    assert report.ok is True
    # Default-page movies dropped from discovery; only the Dec pages' 3
    # movies remain (empty fixture's nav still covers 17/23 December).
    assert report.movies_discovered == 3
    assert report.new_bookable == []  # no re-alerts for previously known
    assert len(store.load_states()) == 169  # state untouched


# ---- state expiry pruning -------------------------------------------------


def test_expired_state_pruned_only_outside_dry_run():
    store = MemoryStore()
    old_key = "HO0001|city-centre-almaza|20200101|10:00am|MAX|0047-old"
    live_key = "HO0002|city-centre-almaza|29990101|10:00am|MAX|0047-live"
    from vox_monitor.state import SessionState

    seed = {
        old_key: SessionState("bookable", FIXED_TS, FIXED_TS, FIXED_TS),
        live_key: SessionState("bookable", FIXED_TS, FIXED_TS, FIXED_TS),
    }
    store.save_states(dict(seed))

    # Dry run must not mutate state at all.
    _cycle(FakeVoxClient(), store, None, make_settings(dry_run=True))
    assert set(store.load_states()) == {old_key, live_key}

    # Real cycle prunes only the expired (past-date) entry.
    _cycle(FakeVoxClient(), store)
    remaining = set(store.load_states())
    assert old_key not in remaining
    assert live_key in remaining  # unknown-but-future entry never deleted


def test_expired_keys_only_matches_strict_session_key_format():
    known = {
        "m|c|20200101|t|f|s": object(),       # expired -> picked
        "m|c|20990101|t|f|s": object(),       # future -> kept
        "m|c|2020|t|f|s": object(),           # short date -> kept
        "malformed-key": object(),            # wrong arity -> kept
        "m|c|20200101abcd|t|f|s": object(),   # non-digit date -> kept
        "m|c|2020010|t|f|s": object(),        # 7-digit date -> kept
    }
    assert _expired_keys(known, today="20261030") == ["m|c|20200101|t|f|s"]


# ---- state failure / lock paths ------------------------------------------


def test_state_load_failure_blocks_alerting():
    notifier = NullNotifier()
    report = _cycle(FakeVoxClient(), notifier=notifier, store=FailingStore())
    assert report.ok is False
    assert report.state_error == "load_failed"
    assert notifier.messages == []


def test_busy_lock_skips_cycle():
    store = MemoryStore()
    assert store.acquire_lock(60) is True
    report = _cycle(FakeVoxClient(), store)
    assert report.busy is True
    assert report.ok is True


def test_new_date_appearing_in_nav_is_fetched():
    store = MemoryStore()
    _cycle(FakeVoxClient(), store)
    client = FakeVoxClient()
    _cycle(client, store)
    d_params = {c for c in client.calls if "d=" in c}
    assert any("d=20261217" in c for c in d_params)
    assert any("d=20261223" in c for c in d_params)
    assert any("d=20261031" in c for c in d_params)
