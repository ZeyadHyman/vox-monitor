"""Persistent monitoring state: Upstash Redis REST store + dedup logic.

State model
-----------
All sessions seen on showtime pages (bookable or unavailable) are stored in
one Redis hash ``vox:monitor:sessions``: field = session key, value = JSON
``SessionState``. A session key is::

    movie_id|cinema_slug|date|time_label|format|session_id

The movie segment keeps sessions of different films strictly distinct while
the store stays a single hash, so monitoring all movies at the cinema needs
no structural change.

Notification rules (see ``decide_updates``):
  * BOOKABLE and no prior entry            -> alert (new session)
  * BOOKABLE and prior status != bookable  -> alert (opened / reopened)
  * BOOKABLE, prior bookable, never sent   -> alert (retry after failure)
  * BOOKABLE, prior bookable, notified     -> no alert (deduplicated)
  * UNAVAILABLE / UNKNOWN                  -> update only, never alert

A cycle lock (``vox:monitor:lock``, SET NX EX) prevents overlapping runs.

Initial Baseline
---------------
An empty state + a live site would otherwise alert on every existing
bookable session at once. A separate marker key ``vox:monitor:baseline``
(plain string, completion timestamp) records that a first, complete,
successful snapshot of all sessions has been persisted:

  * marker absent  -> the cycle runs in BASELINE mode: sessions are saved
    (bookable entries stamped ``notified_at``) but NO Telegram notification
    is sent and nothing is counted as "new". The marker is written only if
    every page was fetched without failure, the whole cycle did not hit the
    time budget, and the state save succeeded.
  * marker present  -> normal operation: alert on new bookable sessions and
    on unavailable -> bookable transitions, deduplicated by ``notified_at``.

The marker is its own key: existing keys (sessions hash, lock) are never
renamed or cleared by the baseline mechanism.
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
    def remove_states(self, keys: list[str]) -> None: ...
    def get_baseline(self) -> str | None: ...
    def set_baseline(self, value: str) -> None: ...


class RedisRestStore:
    """Upstash Redis REST API client (JSON command protocol).

    ``key_prefix`` isolates namespaces: production uses the default
    ``vox:monitor:`` (keys ``vox:monitor:sessions`` / ``vox:monitor:lock`` /
    ``vox:monitor:baseline``), integration tests use ``test:vox-monitor:``
    so they can never touch or delete production state.
    """

    DEFAULT_PREFIX = "vox:monitor:"

    def __init__(
        self,
        url: str,
        token: str,
        timeout: float = 15.0,
        key_prefix: str = DEFAULT_PREFIX,
    ) -> None:
        try:
            import urllib.request
        except ImportError:  # pragma: no cover
            raise StateError("urllib unavailable")
        self._urllib = urllib.request
        self._url = url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._sessions_key = key_prefix + "sessions"
        self._lock_key = key_prefix + "lock"
        self._baseline_key = key_prefix + "baseline"

    @property
    def sessions_key(self) -> str:
        return self._sessions_key

    @property
    def lock_key(self) -> str:
        return self._lock_key

    @property
    def baseline_key(self) -> str:
        return self._baseline_key

    def _command(self, command: list[Any]) -> Any:
        try:
            # Request construction must be inside the try: a malformed URL
            # raises ValueError and must surface as StateError, never as an
            # unhandled exception that crashes the monitoring cycle.
            req = self._urllib.Request(
                self._url,
                data=json.dumps(command).encode("utf-8"),
                headers={
                    "Authorization": "Bearer " + self._token,
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with self._urllib.urlopen(req, timeout=self._timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise StateError(f"Redis command failed: {type(exc).__name__}") from exc
        if isinstance(payload, dict) and payload.get("error"):
            raise StateError("Redis returned an error")
        return payload.get("result") if isinstance(payload, dict) else None

    def acquire_lock(self, ttl_seconds: int) -> bool:
        result = self._command(
            ["SET", self._lock_key, "1", "NX", "EX", ttl_seconds]
        )
        return result == "OK"

    def release_lock(self) -> None:
        self._command(["DEL", self._lock_key])

    def load_states(self) -> dict[str, SessionState]:
        raw = self._command(["HGETALL", self._sessions_key])
        states: dict[str, SessionState] = {}
        # Real Upstash REST instances differ: some return an object
        # {"field": "json"} and others a flat RESP-style list
        # ["field", "json", "field", "json", ...]. Both shapes must parse,
        # otherwise the hash silently reads back as empty and dedup breaks.
        if isinstance(raw, dict):
            items: list[tuple[Any, Any]] = list(raw.items())
        elif isinstance(raw, list) and len(raw) % 2 == 0:
            items = list(zip(raw[0::2], raw[1::2]))
        else:
            items = []
        for key, value in items:
            try:
                data = json.loads(value) if isinstance(value, str) else value
                if not isinstance(data, dict):
                    continue
                states[str(key)] = SessionState.from_json(data)
            except Exception:
                continue  # skip corrupt entries, keep the rest
        return states

    def save_states(self, entries: dict[str, SessionState]) -> None:
        if not entries:
            return
        command: list[Any] = ["HSET", self._sessions_key]
        for key, state in entries.items():
            command.append(key)
            command.append(json.dumps(state.to_json(), ensure_ascii=False))
        self._command(command)

    def clear_states(self) -> int:
        """Delete this store's sessions hash.

        Scoped to the configured key prefix, so tests using
        ``test:vox-monitor:`` can reset their own state without ever
        touching the production hash.
        """
        result = self._command(["DEL", self._sessions_key])
        return int(result) if isinstance(result, (int, float)) else 0

    def remove_states(self, keys: list[str]) -> None:
        """Delete specific session entries (e.g. dates that have passed).

        Only ever called with keys already present in memory; never deletes
        wholesale, and never as a reaction to a fetch failure.
        """
        if not keys:
            return
        self._command(["HDEL", self._sessions_key, *keys])

    def get_baseline(self) -> str | None:
        """Completion timestamp of the initial baseline, or None."""
        raw = self._command(["GET", self._baseline_key])
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        if isinstance(raw, str) and raw:
            return raw
        return None

    def set_baseline(self, value: str) -> None:
        """Record baseline completion (plain SET; only ever called after a
        fully clean fetch + verified state save)."""
        self._command(["SET", self._baseline_key, value])

    def clear_baseline(self) -> None:
        """Remove the baseline marker, scoped to this store's key prefix."""
        self._command(["DEL", self._baseline_key])


class MemoryStore:
    """In-memory store for tests and local dry runs."""

    def __init__(self) -> None:
        self._states: dict[str, SessionState] = {}
        self._locked = False
        self._baseline: str | None = None

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

    def remove_states(self, keys: list[str]) -> None:
        for key in keys:
            self._states.pop(key, None)

    def get_baseline(self) -> str | None:
        return self._baseline

    def set_baseline(self, value: str) -> None:
        self._baseline = value


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
