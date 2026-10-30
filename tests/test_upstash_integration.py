"""Real Upstash Redis persistence tests — no mocks, no Telegram.

Requires real credentials: ``UPSTASH_REDIS_REST_URL`` and
``UPSTASH_REDIS_REST_TOKEN`` either in the environment or in a gitignored
``.env.integration`` file. Without them the whole module is skipped (the
unit suite must never pretend mock results are real persistence proof).

Design guarantees:
  * Every Redis operation runs in a SEPARATE python subprocess
    (``tests/integration_probe.py``): write in process A, read/verify in
    process B — genuine cross-process persistence in real Upstash.
  * All writes use the ``test:vox-monitor:`` key prefix; production keys
    (``vox:doomsday:sessions`` / ``vox:doomsday:lock``) are only ever READ
    and a final test asserts no test data leaked into them.
  * Telegram code is never imported or invoked; no messages are sent.
  * Secrets are passed via child environment only — never printed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from _integration_support import TEST_PREFIX, load_upstash_creds
from integration_probe import TS1, TS3
from vox_monitor.state import RedisRestStore

CREDS = load_upstash_creds()
PROBE = Path(__file__).resolve().parent / "integration_probe.py"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        CREDS is None,
        reason=(
            "Real Upstash credentials unavailable: set UPSTASH_REDIS_REST_URL/"
            "UPSTASH_REDIS_REST_TOKEN or create .env.integration at the repo "
            "root (values stay local, never committed or printed)"
        ),
    ),
]

# Every case id used anywhere in this module; the final safety test asserts
# none of them (nor any test-cinema field) appear in production state.
USED_CASE_IDS: list[str] = []


def _new_case_id() -> str:
    case_id = uuid.uuid4().hex[:12]
    USED_CASE_IDS.append(case_id)
    return case_id


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    if CREDS:
        env["UPSTASH_REDIS_REST_URL"] = CREDS[0]
        env["UPSTASH_REDIS_REST_TOKEN"] = CREDS[1]
    return env


def _run_probe(action: str, *args: str, timeout: int = 60) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, str(PROBE), action, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_child_env(),
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    payload: dict = {}
    if lines:
        try:
            payload = json.loads(lines[-1])
        except json.JSONDecodeError:
            payload = {"ok": False, "reason": "bad_output"}
    return proc.returncode, payload


def _test_store() -> RedisRestStore:
    assert CREDS is not None
    return RedisRestStore(CREDS[0], CREDS[1], key_prefix=TEST_PREFIX)


def _production_store() -> RedisRestStore:
    """Default-prefix store — used strictly for READ-ONLY assertions."""
    assert CREDS is not None
    return RedisRestStore(CREDS[0], CREDS[1])


@pytest.fixture(autouse=True)
def _reset_test_keys():
    """Fresh test namespace before and guaranteed cleanup after each test."""
    _run_probe("cleanup")
    yield
    _run_probe("cleanup")


def test_fresh_process_reads_state_written_by_another_process():
    case_id = _new_case_id()
    code, payload = _run_probe("write", case_id)
    assert code == 0, payload
    # A brand-new interpreter must see the exact persisted record.
    code, payload = _run_probe("verify", case_id)
    assert code == 0 and payload.get("ok") is True, payload
    assert set(payload.get("matched_fields", [])) == {
        "status",
        "first_seen",
        "last_seen",
        "notified_at",
    }


def test_notified_at_persists_across_process_restart():
    case_id = _new_case_id()
    code, _ = _run_probe("write", case_id)
    assert code == 0
    code, payload = _run_probe("verify", case_id)
    assert code == 0 and payload.get("notified_at_ok") is True, payload


def test_previously_notified_session_does_not_realert_in_new_process():
    case_id = _new_case_id()
    code, _ = _run_probe("write", case_id)
    assert code == 0
    code, payload = _run_probe("dedup", case_id)
    assert code == 0, payload
    assert payload.get("alerts") == 0, payload
    assert payload.get("notified_at_preserved") is True, payload


def test_status_transitions_roundtrip_across_processes():
    case_id = _new_case_id()
    code, _ = _run_probe("write", case_id)
    assert code == 0
    code, payload = _run_probe("transition", case_id)
    assert code == 0, payload
    # Independent read from this (parent) process confirms final persisted state.
    final = _test_store().load_states().get(payload["field"])
    assert final is not None, payload
    assert final.status == "bookable"
    assert final.first_seen == TS1
    assert final.notified_at == TS3


def test_lock_blocks_second_process_until_released():
    hold_seconds = 6
    hold = subprocess.Popen(
        [sys.executable, str(PROBE), "lock-hold", str(hold_seconds)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_child_env(),
    )
    try:
        first_line = hold.stdout.readline()
        assert first_line.strip(), "lock-hold child produced no output"
        held = json.loads(first_line.strip())
        assert held.get("state") == "HELD", held
        # Second process must be refused while the first holds the lock.
        code, payload = _run_probe("lock-try")
        assert code == 0 and payload.get("state") == "BUSY", payload
        hold.wait(timeout=hold_seconds + 30)
        assert hold.returncode == 0, "lock-hold child failed"
        # After release, a third process acquires it.
        code, payload = _run_probe("lock-try")
        assert code == 0 and payload.get("state") == "ACQUIRED", payload
    finally:
        if hold.poll() is None:
            hold.kill()
            hold.wait(timeout=10)


def test_cleanup_removes_only_test_prefixed_keys():
    case_id = _new_case_id()
    code, _ = _run_probe("write", case_id)
    assert code == 0
    assert _test_store().load_states() != {}
    code, payload = _run_probe("cleanup")
    assert code == 0, payload
    assert _test_store().load_states() == {}
    # Production hash still readable (read-only) — no exception, no deletion.
    _production_store().load_states()


def test_production_state_never_contains_test_keys():
    """Final safety assertion: the suite left production state untouched."""
    prod = _production_store().load_states()
    leaked = [key for key in prod if "test-cinema-" in key]
    assert leaked == [], f"test fields leaked into production: {leaked}"
    leaked_cases = [cid for cid in USED_CASE_IDS if any(cid in key for key in prod)]
    assert leaked_cases == [], f"case ids leaked into production: {leaked_cases}"
    # Test namespace is fully cleaned up.
    store = _test_store()
    assert store.load_states() == {}
    assert store._command(["GET", store.lock_key]) in (None, "")
