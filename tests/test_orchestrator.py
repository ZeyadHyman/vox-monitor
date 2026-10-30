"""Orchestrator tests using real VOX fixtures and fakes for HTTP/state/Telegram."""

from pathlib import Path

import pytest

from vox_monitor.config import Settings
from vox_monitor.http import HttpFetchError, HttpResponse
from vox_monitor.orchestrator import run_cycle
from vox_monitor.state import MemoryStore, StateError
from vox_monitor.telegram import NullNotifier, TelegramError

FIXTURES = Path(__file__).parent / "fixtures"


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
        max_dates_per_cinema=21,
        max_retries=0,
        cinema_slugs_override=("city-centre-almaza",),
    )
    base.update(overrides)
    return Settings(**base)


class FakeVoxClient:
    """Serves real fixture pages based on URL parameters."""

    def __init__(self, fail_urls=(), fail_all=False):
        self.fail_urls = tuple(fail_urls)
        self.fail_all = fail_all
        self.calls: list[str] = []

    def get(self, url: str) -> HttpResponse:
        self.calls.append(url)
        if self.fail_all:
            raise HttpFetchError("all fail")
        for bad in self.fail_urls:
            if bad in url:
                raise HttpFetchError("targeted fail")
        if "d=20261223" in url:
            return HttpResponse(200, _fx("showtimes_almaza_20261223.html"))
        if "d=" in url:
            return HttpResponse(200, _fx("showtimes_empty_20270401.html"))
        return HttpResponse(200, _fx("showtimes_almaza_default.html"))


class FailingStore(MemoryStore):
    def load_states(self):
        raise StateError("load boom")

    def save_states(self, entries):
        raise StateError("save boom")


class BoomNotifier(NullNotifier):
    def send(self, text):
        raise TelegramError("telegram boom")


def test_first_cycle_alerts_all_new_bookable_sessions():
    settings = make_settings()
    client = FakeVoxClient()
    store = MemoryStore()
    notifier = NullNotifier()

    report = run_cycle(settings, client, store, notifier)

    assert report.ok is True
    assert report.busy is False
    assert report.partial is False
    # 16 Dec (20 sessions, 0 bookable) + 23 Dec (20 sessions, 17 bookable)
    assert report.sessions_seen == 40
    assert report.bookable_seen == 17
    assert len(report.new_bookable) == 17
    assert report.notified is True
    assert len(notifier.messages) >= 1
    combined = "\n".join(notifier.messages)
    assert "TICKETS AVAILABLE" in combined
    assert "https://egy.voxcinemas.com/booking/0047-276988" in combined
    assert store.load_states()  # persisted


def test_second_cycle_deduplicates():
    settings = make_settings()
    store = MemoryStore()
    run_cycle(settings, FakeVoxClient(), store, NullNotifier())

    notifier = NullNotifier()
    report = run_cycle(settings, FakeVoxClient(), store, notifier)
    assert report.ok is True
    assert report.new_bookable == []
    assert report.notified is False
    assert notifier.messages == []


def test_dry_run_sends_nothing_and_keeps_state_clean():
    settings = make_settings(dry_run=True)
    store = MemoryStore()
    notifier = NullNotifier()

    report = run_cycle(settings, FakeVoxClient(), store, notifier)

    assert report.dry_run is True
    assert len(report.new_bookable) == 17
    assert notifier.messages == []
    assert store.load_states() == {}


def test_telegram_failure_persists_status_without_notified_stamp():
    settings = make_settings()
    store = MemoryStore()

    report = run_cycle(settings, FakeVoxClient(), store, BoomNotifier())
    assert report.notified is False
    assert report.notify_error == "TelegramError"
    states = store.load_states()
    assert states
    assert all(s.notified_at is None for s in states.values())
    assert any(s.status == "bookable" for s in states.values())

    # Next cycle with a working notifier re-alerts (never-lost alert).
    notifier = NullNotifier()
    report2 = run_cycle(settings, FakeVoxClient(), store, notifier)
    assert report2.notified is True
    assert len(report2.new_bookable) == 17


def test_all_fetches_failing_is_hard_failure():
    settings = make_settings()
    store = MemoryStore()
    notifier = NullNotifier()

    report = run_cycle(settings, FakeVoxClient(fail_all=True), store, notifier)
    assert report.ok is False
    assert report.pages_fetched == 0
    assert notifier.messages == []
    assert store.load_states() == {}


def test_partial_failure_processes_available_pages_and_keeps_state():
    settings = make_settings(
        cinema_slugs_override=("city-centre-almaza", "mall-of-egypt")
    )
    client = FakeVoxClient(fail_urls=("c=mall-of-egypt",))
    store = MemoryStore()
    notifier = NullNotifier()

    report = run_cycle(settings, client, store, notifier)
    assert report.ok is True
    assert report.partial is True
    assert report.pages_failed > 0
    assert report.pages_fetched > 0
    assert len(report.new_bookable) == 17
    assert report.notified is True


def test_state_load_failure_blocks_alerting():
    settings = make_settings()
    notifier = NullNotifier()
    report = run_cycle(settings, FakeVoxClient(), FailingStore(), notifier)
    assert report.ok is False
    assert report.state_error == "load_failed"
    assert notifier.messages == []


def test_busy_lock_skips_cycle():
    settings = make_settings()
    store = MemoryStore()
    assert store.acquire_lock(60) is True
    report = run_cycle(settings, FakeVoxClient(), store, NullNotifier())
    assert report.busy is True
    assert report.ok is True


def test_new_date_appearing_in_nav_is_fetched():
    settings = make_settings()
    store = MemoryStore()
    run_cycle(settings, FakeVoxClient(), store, NullNotifier())
    client = FakeVoxClient()
    run_cycle(settings, client, store, NullNotifier())
    d_params = {c for c in client.calls if "d=" in c}
    assert any("d=20261217" in c for c in d_params)
    assert any("d=20261223" in c for c in d_params)
