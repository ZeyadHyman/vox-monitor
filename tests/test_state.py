from vox_monitor.models import Availability, Session, SessionState
from vox_monitor.state import MemoryStore, decide_updates, stamp_notified

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
