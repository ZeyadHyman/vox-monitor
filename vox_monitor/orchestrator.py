"""Monitoring orchestration for one cycle.

Flow:
  1. Acquire the cycle lock (skip if another run is active).
  2. Fetch the Almaza showtimes page WITHOUT movie parameters (returns the
     default on-sale date, the full list of on-sale dates, and every movie
     playing on that date).
  3. Fetch every remaining on-sale date with bounded concurrency and a
     global time budget; dates the budget does not reach roll into the
     next cycle, so coverage grows cycle over cycle until complete.
  4. Union all movies discovered across fetched pages — the movie registry
     is dynamic, never a fixed list.
  5. Normalize sessions, compare with stored state, alert on newly
     bookable sessions, persist updates.
  6. Partial fetch failures never erase state and never mean "no tickets":
     failed pages contribute nothing, everything else proceeds. Entries
     whose date has passed are pruned separately, best-effort, never as a
     reaction to a failure.
  7. Initial Baseline (state.py): while the ``vox:monitor:baseline`` marker
     is absent the cycle persists every session WITHOUT sending any
     Telegram notification and without counting anything as new. The marker
     is written only after a fully clean fetch (no failed pages, no time
     budget cancellation) and a successful state save; any fetch failure or
     Redis failure keeps the baseline pending for the next cycle.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Callable

from .cinemas import MONITORED_CINEMA
from .config import Settings, showtimes_url
from .http import HttpClient, HttpFetchError
from .models import CycleReport, Session, ShowtimePage
from .movies import merge_movies
from .showtimes import parse_showtimes_page
from .state import Decision, SessionStore, StateError, decide_updates, stamp_notified
from .telegram import Notifier, TelegramError, format_alert_chunks

LOCK_TTL_SECONDS = 240


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_yyyymmdd() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _compact_today(iso_ts: str) -> str:
    """'2026-10-30T12:00:00+00:00' -> '20261030'; falls back to UTC now."""
    try:
        return iso_ts[:10].replace("-", "")
    except AttributeError:
        return _utc_yyyymmdd()


def run_cycle(
    settings: Settings,
    client: HttpClient,
    store: SessionStore,
    notifier: Notifier,
    now: Callable[[], str] = _utcnow,
) -> CycleReport:
    report = CycleReport(ok=True, dry_run=settings.dry_run)

    try:
        locked = store.acquire_lock(LOCK_TTL_SECONDS)
    except StateError as exc:
        report.ok = False
        report.state_error = type(exc).__name__
        return report
    if not locked:
        report.busy = True
        return report

    try:
        return _run_locked(settings, client, store, notifier, report, now)
    finally:
        try:
            store.release_lock()
        except StateError:
            pass


def _run_locked(
    settings: Settings,
    client: HttpClient,
    store: SessionStore,
    notifier: Notifier,
    report: CycleReport,
    now: Callable[[], str],
) -> CycleReport:
    deadline = time.monotonic() + settings.cycle_budget_seconds
    cinema = MONITORED_CINEMA
    report.cinemas_checked = 1  # hard scope: City Centre Almaza only

    pages: list[ShowtimePage] = []
    failed = 0

    # Phase 1: the no-date page gives the default date, the full list of
    # on-sale dates, and the movies of the default date.
    first_page: ShowtimePage | None = None
    try:
        first_page = _fetch_page(client, cinema.slug, None)
    except HttpFetchError:
        failed += 1
        report.errors.append(f"{cinema.slug}: fetch failed")
    if first_page is not None:
        pages.append(first_page)

    # Phase 2: remaining dates discovered from the nav (capped per cycle).
    tasks: list[str] = []
    if first_page is not None:
        first = first_page.date or (
            first_page.nav_dates[0] if first_page.nav_dates else ""
        )
        rest = [d for d in first_page.nav_dates if d != first]
        tasks = rest[: max(0, settings.max_dates_per_cinema - 1)]

    if tasks and time.monotonic() >= deadline:
        report.partial = True
        report.errors.append(
            f"time budget reached; {len(tasks)} date pages left for next cycle"
        )
        tasks = []

    if tasks:
        with ThreadPoolExecutor(max_workers=max(1, settings.max_workers)) as pool:
            futures = {
                pool.submit(_fetch_page, client, cinema.slug, d): d for d in tasks
            }
            for future in as_completed(futures):
                if time.monotonic() > deadline:
                    cancelled = sum(
                        1 for f in futures if not f.done() and f.cancel()
                    )
                    report.partial = True
                    report.errors.append(
                        f"time budget reached; {cancelled} date pages left "
                        "for next cycle"
                    )
                    break
                date = futures[future]
                try:
                    pages.append(future.result())
                except HttpFetchError:
                    failed += 1
                    report.errors.append(f"{cinema.slug} d={date}: fetch failed")

    report.pages_fetched = len(pages)
    report.pages_failed = failed

    if not pages:
        report.ok = False
        if not report.errors:
            report.errors.append("no pages fetched")
        return report

    if failed:
        report.partial = True

    # Dynamic movie registry: union of every movie seen on any fetched page.
    movies = merge_movies(page.movies for page in pages)
    report.movies_discovered = len(movies)

    all_sessions: list[Session] = []
    for page in pages:
        all_sessions.extend(page.sessions)
    report.sessions_seen = len(all_sessions)
    report.bookable_seen = sum(1 for s in all_sessions if s.availability.value == "bookable")

    # State comparison. If state cannot be loaded, abort without alerting:
    # a state failure must never cause duplicate notifications. The baseline
    # marker rides in the same try: without knowing baseline status the cycle
    # must not notify either (a lost marker would cause a mass alert).
    try:
        known = store.load_states()
        baseline_at = store.get_baseline()
    except StateError:
        report.ok = False
        report.state_error = "load_failed"
        return report

    baseline_pending = baseline_at is None

    # Prune state for dates that have passed (relative to this cycle's own
    # clock). Best-effort housekeeping with strict key parsing — never
    # triggered by fetch failures, never fatal, and never applied while
    # dry-running (no state mutation at all).
    ts = now()
    if not settings.dry_run:
        expired = _expired_keys(known, today=_compact_today(ts))
        if expired:
            try:
                store.remove_states(expired)
            except StateError:
                report.errors.append("expired-state cleanup failed (non-fatal)")

    decision: Decision = decide_updates(all_sessions, known, ts)

    if baseline_pending:
        # INITIAL BASELINE MODE: persist everything, notify about nothing,
        # treat no session as new. The marker is recorded only after a fully
        # clean cycle + verified save (any fetch failure or Redis failure
        # keeps baseline pending and the next cycle retries the same way).
        report.awaiting_baseline = True
        report.notified = False
        if settings.dry_run:
            # Dry runs never write state, so the baseline cannot complete
            # here; report what the baseline cycle would persist instead.
            return report
        # Stamp every bookable session as already handled BEFORE saving, so
        # a partially written baseline can never cause a mass alert later.
        stamp_notified(decision.updates, decision.notified_keys, ts)
        try:
            store.save_states(decision.updates)
        except StateError:
            report.ok = False
            report.state_error = "save_failed"
            report.errors.append("state save failed; baseline not recorded")
            return report
        if report.pages_failed == 0 and not report.partial:
            try:
                store.set_baseline(ts)
            except StateError:
                report.ok = False
                report.state_error = "baseline_save_failed"
                report.errors.append(
                    "baseline marker save failed; baseline not recorded"
                )
                return report
            report.baseline_completed = True
            report.awaiting_baseline = False
        else:
            report.errors.append(
                "baseline pending: fetch incomplete; will retry next cycle"
            )
        return report

    report.new_bookable = [
        {
            "movie": s.movie_name,
            "cinema": s.cinema_name,
            "date": s.date,
            "time": s.time_label,
            "format": s.fmt,
            "session_id": s.session_id,
            "booking_url": s.booking_url,
        }
        for s in decision.to_alert
    ]

    if settings.dry_run:
        # Dry run: no Telegram traffic, no state mutation.
        report.notified = False
        return report

    if decision.to_alert:
        try:
            for chunk in format_alert_chunks(decision.to_alert):
                notifier.send(chunk)
            report.notified = True
            stamp_notified(decision.updates, decision.notified_keys, ts)
        except TelegramError as exc:
            report.notify_error = type(exc).__name__
            # Persist status changes WITHOUT notified_at so the alert is
            # retried on the next cycle.

    try:
        store.save_states(decision.updates)
    except StateError:
        report.ok = False
        report.state_error = "save_failed"
        report.errors.append("state save failed; alerts may be re-sent next cycle")
    return report


def _expired_keys(known: dict, today: str) -> list[str]:
    """Session keys whose date is strictly before today (UTC).

    Keys are ``movie_id|cinema|date|time|fmt|session_id``; anything that
    does not parse exactly is left untouched (never delete on doubt).
    """
    expired: list[str] = []
    for key in known:
        parts = key.split("|")
        if len(parts) != 6:
            continue
        date = parts[2]
        if len(date) == 8 and date.isdigit() and date < today:
            expired.append(key)
    return expired


def _fetch_page(
    client: HttpClient,
    cinema_slug: str,
    date: str | None,
) -> ShowtimePage:
    url = showtimes_url(cinema_slug, date)
    resp = client.get(url)
    if resp.status != 200:
        raise HttpFetchError(f"HTTP {resp.status} for {url}")
    return parse_showtimes_page(resp.text, cinema_slug, requested_date=date)
