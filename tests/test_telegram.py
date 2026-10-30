from vox_monitor.models import Availability, Session
from vox_monitor.telegram import NullNotifier, format_alert_message, format_date_yyyymmdd


def make_session(availability=Availability.BOOKABLE):
    return Session(
        movie_slug="avengers-doomsday-infinity-vision",
        movie_name="Avengers: Doomsday (Infinity Vision)",
        movie_id="HO00013676",
        cinema_slug="city-centre-almaza",
        cinema_name="City Centre Almaza",
        date="20261219",
        time_label="10:45am",
        fmt="MAX",
        session_id="0047-276988",
        availability=availability,
        booking_url="https://egy.voxcinemas.com/booking/0047-276988",
    )


def test_format_date():
    assert format_date_yyyymmdd("20261219") == "19 December 2026"
    assert format_date_yyyymmdd("20270105") == "5 January 2027"
    assert format_date_yyyymmdd("garbage") == "garbage"


def test_alert_message_contains_confirmed_fields():
    text = format_alert_message([make_session()])
    assert "AVENGERS: DOOMSDAY — TICKETS AVAILABLE" in text
    assert "Avengers: Doomsday (Infinity Vision)" in text
    assert "City Centre Almaza" in text
    assert "19 December 2026" in text
    assert "10:45am" in text
    assert "MAX" in text
    assert "Confirmed bookable" in text
    assert "https://egy.voxcinemas.com/booking/0047-276988" in text


def test_alert_message_without_booking_url_uses_showtimes_fallback():
    s = make_session()
    s = Session(**{**s.to_dict(), "availability": Availability.BOOKABLE, "booking_url": None})
    text = format_alert_message([s])
    assert "https://egy.voxcinemas.com/showtimes?c=city-centre-almaza" in text
    assert "d=20261219" in text


def test_alert_message_batches_multiple_sessions():
    s1 = make_session()
    s2 = Session(
        **{
            **s1.to_dict(),
            "session_id": "0047-276984",
            "time_label": "2:30pm",
            "fmt": "MAX",
            "booking_url": "https://egy.voxcinemas.com/booking/0047-276984",
        }
    )
    text = format_alert_message([s1, s2])
    assert "10:45am" in text
    assert "2:30pm" in text


def test_empty_session_list_raises():
    import pytest

    with pytest.raises(ValueError):
        format_alert_message([])


def test_null_notifier_records_messages():
    n = NullNotifier()
    n.send("hello")
    assert n.messages == ["hello"]


def test_alert_chunks_split_large_batches():
    from vox_monitor.telegram import TELEGRAM_MESSAGE_LIMIT, format_alert_chunks

    sessions = [
        Session(
            movie_slug="avengers-doomsday-infinity-vision",
            movie_name="Avengers: Doomsday (Infinity Vision)",
            movie_id="HO00013676",
            cinema_slug="city-centre-almaza",
            cinema_name="City Centre Almaza",
            date="20261223",
            time_label=f"{(i % 12) + 1}:00{'am' if i % 2 else 'pm'}",
            fmt="MAX",
            session_id=f"0047-{276900 + i}",
            availability=Availability.BOOKABLE,
            booking_url=f"https://egy.voxcinemas.com/booking/0047-{276900 + i}",
        )
        for i in range(80)
    ]
    chunks = format_alert_chunks(sessions)
    assert len(chunks) > 1
    assert all(len(c) <= TELEGRAM_MESSAGE_LIMIT for c in chunks)
    joined = "\n".join(chunks)
    assert "booking/0047-276900" in joined
    assert "booking/0047-276979" in joined


def test_single_session_chunk_is_single_message():
    from vox_monitor.telegram import format_alert_chunks

    chunks = format_alert_chunks([make_session()])
    assert len(chunks) == 1
    assert "TICKETS AVAILABLE" in chunks[0]
