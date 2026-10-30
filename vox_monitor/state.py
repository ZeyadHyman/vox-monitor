"""Persistent monitoring state: Upstash Redis REST store + dedup logic.

State model
-----------
All sessions seen on showtime pages (bookable or unavailable) are stored in
one Redis hash ``vox:doomsday:sessions``: field = session key, value = JSON
``SessionState``. A session key is::

    movie_id|cinema_slug|date|time_label|format|session_id

Notification rules (see ``decide_updates``):
  * BOOKABLE and no prior entry            -> alert (new session)
  * BOOKABLE and prior status != bookable  -> alert (opened / reopened)
  * BOOKABLE, prior bookable, never sent   -> alert (retry after failure)
  * BOOKABLE, prior bookable, notified     -> no alert (deduplicated)
  * UNAVAILABLE / UNKNOWN                  -> update only, never alert

A cycle lock (``vox:doomsday:lock``, SET NX EX) prevents overlapping runs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from .models import Availability, Session, SessionState


class StateError(Exception):
    """Redis (or backing store) failure."""


class SessionStore(Protocol):
    def acquire_lock(self, ttl_seconds: int) -> bool: ...
    def release_lock(self) -> None: ...
    def load_states(self) -> dict[str, SessionState]: ...
    def save_states(self, entries: dict[str, SessionState]) -> None: ...


class RedisRestStore:
    """Upstash Redis REST API client (JSON command protocol)."""

    def __init__(self, url: str, token: str, timeout: float = 15.0) -> None:
        try:
            import urllib.request
        except ImportError:  # pragma: no cover
            raise StateError("urllib unavailable")
        self._urllib = urllib.request
        self._url = url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def _command(self, command: list[Any]) -> Any:
        req = self._urllib.Request(
            self._url,
            data=json.dumps(command).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + self._token,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with self._urllib.urlopen(req, timeout=self._timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise StateError(f"Redis command failed: {type(exc).__name__}") from exc
        if isinstance(payload, dict) and payload.get("error"):
            raise StateError("Redis returned an error")
        return payload.get("result") if isinstance(payload, dict) else None

    def acquire_lock(self, ttl_seconds: int) -> bool:
        result = self._command(["SET", "vox:doomsday:lock", "1", "NX", "EX", ttl_seconds])
        return result == "OK"

    def release_lock(self) -> None:
        self._command(["DEL", "vox:doomsday:lock"])

    def load_states(self) -> dict[str, SessionState]:
        raw = self._command(["HGETALL", "vox:doomsday:sessions"])
        states: dict[str, SessionState] = {}
        if isinstance(raw, dict):
            for key, value in raw.items():
                try:
                    states[str(key)] = SessionState.from_json(json.loads(value))
                except Exception:
                    continue  # skip corrupt entries, keep the rest
        return states

    def save_states(self, entries: dict[str, SessionState]) -> None:
        if not entries:
            return
        command: list[Any] = ["HSET", "vox:doomsday:sessions"]
        for key, state in entries.items():
            command.append(key)
            command.append(json.dumps(state.to_json(), ensure_ascii=False))
        self._command(command)


class MemoryStore:
    """In-memory store for tests and local dry runs."""

    def __init__(self) -> None:
        self._states: dict[str, SessionState] = {}
        self._locked = False

    def acquire_lock(self, ttl_seconds: int) -> bool:
        if self._locked:
            return False
        self._locked = True
        return True

    def release_lock(self) -> None:
        self._locked = False

    def load_states(self) -> dict[str, SessionState]:
        return dict(self._states)

    def save_states(self, entries: dict[str, SessionState]) -> None:
        self._states.update(entries)


@dataclass
class Decision:
    to_alert: list[Session]
    updates: dict[str, SessionState]  # merged state to persist
    notified_keys: list[str]  # keys to stamp notified_at after a successful send


def decide_updates(
    current: list[Session],
    known: dict[str, SessionState],
    now_iso: str,
) -> Decision:
    to_alert: list[Session] = []
    updates: dict[str, SessionState] = {}
    notified_keys: list[str] = []

    for session in current:
        status = session.availability.value
        prior = known.get(session.key)
        merged = SessionState(
            status=status,
            first_seen=prior.first_seen if prior else now_iso,
            last_seen=now_iso,
            notified_at=prior.notified_at if prior else None,
        )
        updates[session.key] = merged

        if session.availability == Availability.BOOKABLE:
            needs_alert = prior is None or prior.status != status or prior.notified_at is None
            if needs_alert:
                to_alert.append(session)
                notified_keys.append(session.key)

    return Decision(to_alert=to_alert, updates=updates, notified_keys=notified_keys)


def stamp_notified(
    updates: dict[str, SessionState], keys: list[str], now_iso: str
) -> dict[str, SessionState]:
    for key in keys:
        state = updates.get(key)
        if state is not None:
            state.notified_at = now_iso
    return updates
