import json

import pytest

from vox_monitor.models import Availability, Session, SessionState
from vox_monitor.state import (
    MemoryStore,
    RedisRestStore,
    StateError,
    decide_updates,
    stamp_notified,
)

NOW = "2026-10-30T12:00:00+00:00"
LATER = "2026-10-30T12:05:00+00:00"


def make_session(
    availability=Availability.BOOKABLE,
    session_id="0047-276988",
    date="20261223",
    time_label="10:45am",
    fmt="MAX",
):
    return Session(
        movie_slug="avengers-doomsday-infinity-vision",
        movie_name="Avengers: Doomsday (Infinity Vision)",
        movie_id="HO00013676",
        cinema_slug="city-centre-almaza",
        cinema_name="City Centre Almaza",
        date=date,
        time_label=time_label,
        fmt=fmt,
        session_id=session_id,
        availability=availability,
        booking_url=(
            f"https://egy.voxcinemas.com/booking/{session_id}"
            if availability == Availability.BOOKABLE
            else None
        ),
    )


def test_session_key_contains_all_identity_parts():
    s = make_session()
    assert s.key == "HO00013676|city-centre-almaza|20261223|10:45am|MAX|0047-276988"


def test_same_time_different_sessions_have_different_keys():
    a = make_session(session_id="0047-111")
    b = make_session(session_id="0047-222")
    assert a.key != b.key


def test_new_bookable_session_triggers_alert():
    decision = decide_updates([make_session()], {}, NOW)
    assert [s.session_id for s in decision.to_alert] == ["0047-276988"]
    assert decision.notified_keys == [decision.to_alert[0].key]


def test_known_notified_bookable_is_deduplicated():
    s = make_session()
    known = {
        s.key: SessionState(
            status="bookable", first_seen=NOW, last_seen=NOW, notified_at=NOW
        )
    }
    decision = decide_updates([s], known, LATER)
    assert decision.to_alert == []
    # last_seen still refreshed
    assert decision.updates[s.key].last_seen == LATER


def test_bookable_never_notified_is_realerted():
    s = make_session()
    known = {
        s.key: SessionState(
            status="bookable", first_seen=NOW, last_seen=NOW, notified_at=None
        )
    }
    decision = decide_updates([s], known, LATER)
    assert len(decision.to_alert) == 1


def test_unavailable_to_bookable_triggers_alert():
    s = make_session()
    known = {
        s.key: SessionState(
            status="unavailable", first_seen=NOW, last_seen=NOW, notified_at=None
        )
    }
    decision = decide_updates([make_session()], known, LATER)
    assert len(decision.to_alert) == 1


def test_bookable_to_unavailable_updates_without_alert():
    s = make_session()
    known = {
        s.key: SessionState(
            status="bookable", first_seen=NOW, last_seen=NOW, notified_at=NOW
        )
    }
    decision = decide_updates([make_session(availability=Availability.UNAVAILABLE)], known, LATER)
    assert decision.to_alert == []
    assert decision.updates[s.key].status == "unavailable"


def test_unavailable_to_bookable_after_notification_cycle_alerts_again():
    s = make_session()
    known = {
        s.key: SessionState(
            status="unavailable", first_seen=NOW, last_seen=NOW, notified_at=None
        )
    }
    decision = decide_updates([make_session()], known, LATER)
    assert len(decision.to_alert) == 1


def test_unknown_sessions_recorded_but_never_alert():
    s = make_session(availability=Availability.UNKNOWN)
    decision = decide_updates([s], {}, NOW)
    assert decision.to_alert == []
    assert decision.updates[s.key].status == "unknown"


def test_stamp_notified_sets_timestamp():
    s = make_session()
    decision = decide_updates([s], {}, NOW)
    stamp_notified(decision.updates, decision.notified_keys, LATER)
    assert decision.updates[s.key].notified_at == LATER


def test_first_seen_preserved_across_updates():
    s = make_session()
    known = {
        s.key: SessionState(
            status="bookable", first_seen=NOW, last_seen=NOW, notified_at=NOW
        )
    }
    decision = decide_updates([s], known, LATER)
    assert decision.updates[s.key].first_seen == NOW


def test_memory_store_lock_exclusive():
    store = MemoryStore()
    assert store.acquire_lock(60) is True
    assert store.acquire_lock(60) is False
    store.release_lock()
    assert store.acquire_lock(60) is True


def test_memory_store_roundtrip():
    store = MemoryStore()
    s = make_session()
    decision = decide_updates([s], {}, NOW)
    store.save_states(decision.updates)
    loaded = store.load_states()
    assert loaded[s.key].status == "bookable"


def test_full_sequence_alerts_only_on_real_changes():
    """Repeated identical scans must never resend; only transitions alert."""
    known: dict[str, SessionState] = {}
    total_alerts = 0

    def cycle(availability, ts):
        nonlocal total_alerts
        decision = decide_updates([make_session(availability=availability)], known, ts)
        total_alerts += len(decision.to_alert)
        stamp_notified(decision.updates, decision.notified_keys, ts)
        known.update(decision.updates)
        return decision

    # 1) first appearance (bookable) -> alert
    assert len(cycle(Availability.BOOKABLE, NOW).to_alert) == 1
    # 2-3) repeated identical scans -> deduplicated, no resend
    assert cycle(Availability.BOOKABLE, LATER).to_alert == []
    assert cycle(Availability.BOOKABLE, LATER).to_alert == []
    # 4) goes unavailable -> update only
    assert cycle(Availability.UNAVAILABLE, LATER).to_alert == []
    # 5) returns to bookable -> re-alert exactly once
    assert len(cycle(Availability.BOOKABLE, LATER).to_alert) == 1
    # 6) repeated scan again -> deduplicated
    assert cycle(Availability.BOOKABLE, LATER).to_alert == []
    assert total_alerts == 2
    final = known[make_session().key]
    assert final.status == "bookable"
    assert final.notified_at == LATER
    assert final.first_seen == NOW


def test_redis_store_default_keys_match_production():
    store = RedisRestStore("https://example.upstash.io", "token-example")
    assert store.sessions_key == "vox:monitor:sessions"
    assert store.lock_key == "vox:monitor:lock"


def test_redis_store_custom_prefix_isolates_test_namespace():
    store = RedisRestStore(
        "https://example.upstash.io", "token-example", key_prefix="test:vox-monitor:"
    )
    assert store.sessions_key == "test:vox-monitor:sessions"
    assert store.lock_key == "test:vox-monitor:lock"


def test_malformed_url_raises_state_error_not_bare_exception():
    # Regression: Request() used to be built outside the try block, so a
    # bad URL escaped as a bare ValueError and crashed the monitoring cycle.
    store = RedisRestStore("not-a-url", "token-example")
    with pytest.raises(StateError):
        store.load_states()


def _state_json(notified=True):
    return json.dumps(
        SessionState(
            status="bookable",
            first_seen=NOW,
            last_seen=NOW,
            notified_at=NOW if notified else None,
        ).to_json()
    )


def test_load_states_accepts_dict_shaped_hgetall():
    store = RedisRestStore("https://example.upstash.io", "token-example")
    store._command = lambda command: {"field-a": _state_json()}
    states = store.load_states()
    assert states["field-a"].status == "bookable"
    assert states["field-a"].notified_at == NOW


def test_load_states_accepts_flat_list_shaped_hgetall():
    # Regression: the REAL Upstash REST instance returns HGETALL as a flat
    # RESP list [field, value, ...]; the old dict-only parser silently
    # returned {} and destroyed dedup in production.
    store = RedisRestStore("https://example.upstash.io", "token-example")
    store._command = lambda command: ["field-a", _state_json(), "field-b", _state_json(False)]
    states = store.load_states()
    assert states["field-a"].notified_at == NOW
    assert states["field-b"].notified_at is None


def test_load_states_skips_corrupt_entries_but_keeps_valid_ones():
    store = RedisRestStore("https://example.upstash.io", "token-example")
    store._command = lambda command: [
        "bad-json",
        "not-json{{",
        "good",
        _state_json(),
        "non-dict-json",
        '"just a string"',
        "mismatched",
        "x",
    ]
    states = store.load_states()
    assert list(states) == ["good"]
    assert states["good"].status == "bookable"


def test_load_states_returns_empty_on_unexpected_shapes():
    store = RedisRestStore("https://example.upstash.io", "token-example")
    store._command = lambda command: ["only-one-element"]
    assert store.load_states() == {}
    store._command = lambda command: None
    assert store.load_states() == {}


def test_memory_store_remove_states():
    store = MemoryStore()
    store.save_states({"a": SessionState("bookable", NOW, NOW, NOW)})
    store.save_states({"b": SessionState("bookable", NOW, NOW, NOW)})
    store.remove_states(["a", "missing"])
    assert list(store.load_states()) == ["b"]


def test_redis_store_remove_states_sends_hdel_for_those_keys_only():
    store = RedisRestStore("https://example.upstash.io", "token-example")
    seen: list[list] = []
    store._command = lambda command: seen.append(command) or 1
    store.remove_states(["k1", "k2"])
    assert seen == [["HDEL", "vox:monitor:sessions", "k1", "k2"]]


def test_redis_store_remove_states_empty_is_noop():
    store = RedisRestStore("https://example.upstash.io", "token-example")
    store._command = lambda command: (_ for _ in ()).throw(AssertionError("must not call"))
    store.remove_states([])  # must not raise, must not issue a command
