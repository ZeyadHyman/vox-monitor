"""Monitoring orchestration for one cycle.

Flow:
  1. Acquire the cycle lock (skip if another run is active).
  2. Discover cinemas (or use override/fallback).
  3. For each cinema: fetch the showtimes page without ``d`` (returns the
     first on-sale date plus the full list of on-sale dates), then fetch
     the remaining dates, with bounded concurrency and a global time budget.
  4. Normalize all sessions, compare with stored state, alert on newly
     bookable sessions, persist updates.
  5. Partial fetch failures never erase state and never mean "no tickets".
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Callable

from .cinemas import discover_cinemas
from .config import Settings, showtimes_url
from .http import HttpClient, HttpFetchError
from .models import CycleReport, Session, ShowtimePage
from .movies import movie_slugs
from .showtimes import parse_showtimes_page
from .state import Decision, SessionStore, StateError, decide_updates, stamp_notified
from .telegram import Notifier, TelegramError, format_alert_chunks

LOCK_TTL_SECONDS = 240


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    slugs = movie_slugs()

    cinemas, discovered = discover_cinemas(client, settings.cinema_slugs_override)
    if not discovered and settings.cinema_slugs_override is None:
        report.errors.append("cinema discovery failed; using fallback list")
    report.cinemas_checked = len(cinemas)

    # Phase 1: one request per cinema (no d param) -> first date + nav dates.
    pages: list[ShowtimePage] = []
    failed = 0
    if cinemas:
        with ThreadPoolExecutor(max_workers=max(1, settings.max_workers)) as pool:
            futures = {
                pool.submit(
                    _fetch_page, client, c.slug, None, slugs
                ): c
                for c in cinemas
            }
            for future in as_completed(futures):
                cinema = futures[future]
                try:
                    page = future.result()
                except HttpFetchError:
                    failed += 1
                    report.errors.append(f"{cinema.slug}: fetch failed")
                    continue
                pages.append(page)

    # Phase 2: remaining dates discovered from the nav (capped per cinema).
    tasks: list[tuple[str, str]] = []  # (cinema_slug, date)
    for page in pages:
        first = page.date or (page.nav_dates[0] if page.nav_dates else "")
        rest = [d for d in page.nav_dates if d != first]
        rest = rest[: max(0, settings.max_dates_per_cinema - 1)]
        tasks += [(page.cinema_slug, d) for d in rest]

    if tasks and time.monotonic() >= deadline:
        report.partial = True
        report.errors.append(
            f"time budget reached; {len(tasks)} date pages left for next cycle"
        )
        tasks = []

    if tasks:
        with ThreadPoolExecutor(max_workers=max(1, settings.max_workers)) as pool:
            futures = {
                pool.submit(_fetch_page, client, c, d, slugs): (c, d)
                for c, d in tasks
            }
            for future in as_completed(futures):
                cinema_slug, date = futures[future]
                try:
                    page = future.result()
                except HttpFetchError:
                    failed += 1
                    report.errors.append(f"{cinema_slug} d={date}: fetch failed")
                    continue
                pages.append(page)

    report.pages_fetched = len(pages)
    report.pages_failed = failed

    if not pages:
        report.ok = False
        if not report.errors:
            report.errors.append("no pages fetched")
        return report

    if failed:
        report.partial = True

    all_sessions: list[Session] = []
    for page in pages:
        all_sessions.extend(page.sessions)
    report.sessions_seen = len(all_sessions)
    report.bookable_seen = sum(1 for s in all_sessions if s.availability.value == "bookable")

    # State comparison. If state cannot be loaded, abort without alerting:
    # a state failure must never cause duplicate notifications.
    try:
        known = store.load_states()
    except StateError:
        report.ok = False
        report.state_error = "load_failed"
        return report

    ts = now()
    decision: Decision = decide_updates(all_sessions, known, ts)
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


def _fetch_page(
    client: HttpClient,
    cinema_slug: str,
    date: str | None,
    slugs: tuple[str, ...],
) -> ShowtimePage:
    url = showtimes_url(cinema_slug, slugs, date)
    resp = client.get(url)
    if resp.status != 200:
        raise HttpFetchError(f"HTTP {resp.status} for {url}")
    return parse_showtimes_page(resp.text, cinema_slug, requested_date=date)
