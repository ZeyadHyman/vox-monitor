"""Isolated-process probe for real Upstash persistence tests.

Invoked by ``tests/test_upstash_integration.py`` as::

    python tests/integration_probe.py <action> [args...]

Each invocation is a brand-new Python process, so any state observed by a
later invocation proves the data actually persisted in Upstash Redis (real
cross-process persistence, not in-memory sharing).

Actions:
  write <case_id>       - store a notified bookable SessionState
  verify <case_id>      - fresh read; compare every persisted field
  dedup <case_id>       - previously-notified bookable must NOT re-alert
  transition <case_id>  - bookable -> unavailable -> bookable re-alert cycle
  baseline-set <id>     - record a baseline marker (test prefix only)
  baseline-flow <id>    - baseline snapshot: save sessions + marker in one go
  baseline-get <id>     - read the marker back in this fresh process
  lock-hold <seconds>   - acquire test-prefix lock, print HELD, hold, release
  lock-try              - attempt lock: reports ACQUIRED or BUSY
  cleanup               - delete ONLY test:vox-monitor:* keys

Output: a single JSON line on stdout. Secrets (URL/token) are never printed.
Exit code 0 = action completed as expected, 1 = failure, 3 = no credentials.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _integration_support import TEST_PREFIX, load_upstash_creds  # noqa: E402
from vox_monitor.models import Availability, Session, SessionState  # noqa: E402
from vox_monitor.state import (  # noqa: E402
    RedisRestStore,
    StateError,
    decide_updates,
    stamp_notified,
)

TS1 = "2026-10-30T12:00:00+00:00"
TS2 = "2026-10-30T12:05:00+00:00"
TS3 = "2026-10-30T12:10:00+00:00"


def make_session(case_id: str, availability: Availability) -> Session:
    return Session(
        movie_slug="avengers-doomsday-infinity-vision",
        movie_name="Avengers: Doomsday (Infinity Vision)",
        movie_id="HO00013676",
        cinema_slug=f"test-cinema-{case_id}",
        cinema_name="Test Cinema",
        date="20261219",
        time_label="10:45am",
        fmt="MAX",
        session_id=f"0047-{case_id}",
        availability=availability,
        booking_url=(
            f"https://egy.voxcinemas.com/booking/0047-{case_id}"
            if availability == Availability.BOOKABLE
            else None
        ),
    )


def action_write(store: RedisRestStore, case_id: str) -> dict:
    session = make_session(case_id, Availability.BOOKABLE)
    state = SessionState(
        status="bookable", first_seen=TS1, last_seen=TS1, notified_at=TS1
    )
    store.save_states({session.key: state})
    return {"ok": True, "field": session.key}


def action_verify(store: RedisRestStore, case_id: str) -> dict:
    session = make_session(case_id, Availability.BOOKABLE)
    states = store.load_states()
    state = states.get(session.key)
    if state is None:
        return {"ok": False, "reason": "missing", "field": session.key}
    actual = {
        "status": state.status,
        "first_seen": state.first_seen,
        "last_seen": state.last_seen,
        "notified_at": state.notified_at,
    }
    expected = {
        "status": "bookable",
        "first_seen": TS1,
        "last_seen": TS1,
        "notified_at": TS1,
    }
    matched = sorted(k for k in expected if actual[k] == expected[k])
    return {
        "ok": actual == expected,
        "field": session.key,
        "matched_fields": matched,
        "notified_at_ok": actual["notified_at"] == TS1,
    }


def action_dedup(store: RedisRestStore, case_id: str) -> dict:
    session = make_session(case_id, Availability.BOOKABLE)
    known = store.load_states()
    if session.key not in known:
        return {"ok": False, "reason": "missing", "field": session.key}
    decision = decide_updates([session], known, TS2)
    return {
        "ok": decision.to_alert == [],
        "field": session.key,
        "alerts": len(decision.to_alert),
        "notified_at_preserved": known[session.key].notified_at == TS1,
    }


def action_transition(store: RedisRestStore, case_id: str) -> dict:
    known = store.load_states()
    unavailable = make_session(case_id, Availability.UNAVAILABLE)
    if unavailable.key not in known:
        return {"ok": False, "reason": "missing", "field": unavailable.key}

    # Cycle 1: bookable -> unavailable. Never alerts; notified_at preserved.
    d1 = decide_updates([unavailable], known, TS2)
    if d1.to_alert:
        return {"ok": False, "reason": "alert_on_unavailable"}
    store.save_states(d1.updates)

    # Cycle 2: unavailable -> bookable. Must re-alert exactly once.
    d2 = decide_updates([make_session(case_id, Availability.BOOKABLE)], store.load_states(), TS3)
    if len(d2.to_alert) != 1:
        return {"ok": False, "reason": f"expected_1_realert_got_{len(d2.to_alert)}"}
    stamp_notified(d2.updates, d2.notified_keys, TS3)
    store.save_states(d2.updates)

    # Cycle 3: unchanged bookable. Must not re-alert after the stamp.
    bookable = make_session(case_id, Availability.BOOKABLE)
    d3 = decide_updates([bookable], store.load_states(), TS3)
    if d3.to_alert:
        return {"ok": False, "reason": "realert_after_stamp"}
    final = store.load_states()[bookable.key]
    if final.status != "bookable" or final.notified_at != TS3 or final.first_seen != TS1:
        return {"ok": False, "reason": "final_state_mismatch"}
    return {"ok": True, "field": bookable.key}


def action_lock_hold(store: RedisRestStore, hold_seconds: int) -> dict:
    ttl = hold_seconds + 15
    if not store.acquire_lock(ttl):
        return {"ok": False, "state": "BUSY"}
    print(json.dumps({"action": "lock-hold", "ok": True, "state": "HELD"}), flush=True)
    time.sleep(hold_seconds)
    store.release_lock()
    return {"ok": True, "state": "RELEASED", "early_line": True}


def action_lock_try(store: RedisRestStore) -> dict:
    if store.acquire_lock(20):
        store.release_lock()
        return {"ok": True, "state": "ACQUIRED"}
    return {"ok": True, "state": "BUSY"}


def action_cleanup(store: RedisRestStore) -> dict:
    # clear_states/release_lock/clear_baseline are scoped to the store's own
    # (test-prefix) keys, so this can never delete production data.
    sessions_deleted = store.clear_states()
    store.release_lock()
    store.clear_baseline()
    return {"ok": True, "sessions_deleted": sessions_deleted}


def action_baseline_set(store: RedisRestStore, case_id: str) -> dict:
    store.set_baseline(f"baseline-{case_id}-{TS1}")
    return {"ok": True, "marker": store.get_baseline()}


def action_baseline_get(store: RedisRestStore, case_id: str) -> dict:
    expected = f"baseline-{case_id}-{TS1}"
    actual = store.get_baseline()
    return {"ok": actual == expected, "marker": actual, "expected": expected}


def action_baseline_flow(store: RedisRestStore, case_id: str) -> dict:
    """Emulates the orchestrator's baseline cycle in one isolated process:
    persist every session (bookable stamped as handled) and only then record
    the baseline marker. A later process must observe BOTH."""
    session = make_session(case_id, Availability.BOOKABLE)
    state = SessionState(
        status="bookable", first_seen=TS1, last_seen=TS1, notified_at=TS1
    )
    store.save_states({session.key: state})
    store.set_baseline(f"baseline-{case_id}-{TS1}")
    return {"ok": True, "field": session.key}


def main() -> int:
    if len(sys.argv) < 2:
        print(json.dumps({"ok": False, "reason": "usage"}))
        return 1
    action = sys.argv[1]
    creds = load_upstash_creds()
    if creds is None:
        print(json.dumps({"action": action, "ok": False, "reason": "no_creds"}))
        return 3
    store = RedisRestStore(creds[0], creds[1], key_prefix=TEST_PREFIX)
    try:
        if action in ("write", "verify", "dedup", "transition"):
            if len(sys.argv) < 3:
                raise ValueError("case_id required")
            case_id = sys.argv[2]
            handler = {
                "write": action_write,
                "verify": action_verify,
                "dedup": action_dedup,
                "transition": action_transition,
            }[action]
            result = handler(store, case_id)
        elif action in ("baseline-set", "baseline-get", "baseline-flow"):
            if len(sys.argv) < 3:
                raise ValueError("case_id required")
            case_id = sys.argv[2]
            handler = {
                "baseline-set": action_baseline_set,
                "baseline-get": action_baseline_get,
                "baseline-flow": action_baseline_flow,
            }[action]
            result = handler(store, case_id)
        elif action == "lock-hold":
            if len(sys.argv) < 3:
                raise ValueError("hold_seconds required")
            # HELD line is printed directly by the action (before this summary
            # line) so the parent can synchronize on it.
            result = action_lock_hold(store, int(sys.argv[2]))
        elif action == "lock-try":
            result = action_lock_try(store)
        elif action == "cleanup":
            result = action_cleanup(store)
        else:
            result = {"ok": False, "reason": "unknown_action"}
    except StateError:
        print(json.dumps({"action": action, "ok": False, "reason": "state_error"}))
        return 1
    except Exception as exc:  # noqa: BLE001 - class name only, never str(exc)
        print(json.dumps({"action": action, "ok": False, "reason": type(exc).__name__}))
        return 1
    print(json.dumps({"action": action, **result}))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
