# VOX Avengers: Doomsday Monitor

Telegram notification bot that watches the official VOX Cinemas Egypt website
(https://egy.voxcinemas.com/) for **newly bookable advance-showtime sessions**
of:

| Movie | Slug | Movie ID |
| --- | --- | --- |
| Avengers: Doomsday | `avengers-doomsday` | `HO00013527` |
| Avengers: Doomsday (Infinity Vision) | `avengers-doomsday-infinity-vision` | `HO00013676` |

It monitors **all VOX Egypt cinemas** (discovered from the live site),
**both movie versions**, and every **on-sale date**, and sends a Telegram
alert only when a session becomes **actually bookable** — not merely listed.

Bot: `@AvengersVoxWatcherBot` · Production: `https://vox-monitor-tau.vercel.app`

---

## How bookability is determined

The monitor fetches server-rendered showtime pages:

```
GET https://egy.voxcinemas.com/showtimes?c={cinema-slug}&m={movie-slug}&m={movie-slug}&d={YYYYMMDD}
```

VOX itself renders availability into the page (verified against the live site
and the real booking flow):

| Rendered markup | Classification |
| --- | --- |
| `<a class="action showtime" href="https://egy.voxcinemas.com/booking/{id}">10:45am</a>` | **BOOKABLE** — VOX generated a live booking link |
| `<span class="action showtime unavailable">10:30pm</span>` | **UNAVAILABLE** — rendered without a booking link (sold out / closed) |
| Session `<li data-id>` with neither marker | **UNKNOWN** — never alerted |

Sessions are uniquely identified by
`movie_id|cinema_slug|date|time|format|session_id`
(e.g. `HO00013676|city-centre-almaza|20261223|10:45am|MAX|0047-276988`).
Alerts fire only on a transition **to** `bookable`; known bookable sessions
are suppressed via Redis state.

**Important client requirement:** the VOX origin is behind Akamai bot
filtering. Non-browser TLS fingerprints (Python `urllib`, default `curl`)
complete the TCP handshake but receive **zero bytes**. The monitor uses
`curl_cffi` with Chrome TLS/HTTP2 impersonation — this is a hard dependency.

## Architecture

```
api/check.py          Vercel handler (auth → run cycle → JSON report)
api/health.py         Unauthenticated liveness probe
vox_monitor/
  config.py           Settings from env; URL builders
  movies.py           Movie registry (slugs + VOX movie IDs)
  http.py             Chrome-impersonating HTTP client with retries
  showtimes.py        HTML parsers (showtimes pages + cinema index)
  cinemas.py          Live cinema discovery + fallback list
  models.py           Session / availability / report models
  state.py            Upstash Redis store + dedup decision logic
  telegram.py         Message formatting, chunking, sending
  orchestrator.py     One monitoring cycle (fetch → classify → diff → alert)
tests/                51 tests incl. fixtures from the real VOX site
```

One cycle: discover cinemas → fetch each cinema's showtimes page (no `d`
parameter; returns the first on-sale date **and** the list of on-sale dates)
→ fetch the remaining dates with bounded concurrency → normalize → diff
against Redis → send Telegram alerts for newly bookable sessions → persist
state. Partial failures are reported but never erase state and never mean
"no tickets". A Redis lock prevents overlapping cycles.

## Environment variables

Set these in Vercel (never commit them):

| Name | Required | Purpose |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | yes | Existing bot token (`@AvengersVoxWatcherBot`) |
| `TELEGRAM_CHAT_ID` | yes | Your chat ID |
| `UPSTASH_REDIS_REST_URL` | yes | Upstash REST endpoint |
| `UPSTASH_REDIS_REST_TOKEN` | yes | Upstash REST token |
| `MONITOR_SECRET` | yes | Long random secret for `/api/check` |
| `CRON_SECRET` | recommended | For Vercel Cron auth (set equal to `MONITOR_SECRET`) |
| `DRY_RUN` | no | `1` = full cycle, no Telegram, no Redis writes |
| `TEST_NOTIFY_CHAT_ID` | no | Send alerts to a different chat (for testing) |
| `CINEMA_SLUGS` | no | Comma-separated slugs to override discovery |
| `MAX_WORKERS` | no | Concurrent VOX fetches (default 3) |
| `MAX_DATES_PER_CINEMA` | no | Date cap per cinema per cycle (default 21) |
| `CYCLE_BUDGET_SECONDS` | no | Time budget per cycle (default 45) |

## Deployment (Vercel)

1. Push this repository to GitHub (keep it **private**).
2. Import it into Vercel. Python 3.12+ is used automatically.
3. Add all environment variables above (Production + Preview).
4. Deploy. `vercel.json` sets `maxDuration: 60` for `api/check.py`.
5. Verify:
   - `GET https://<project>.vercel.app/api/health` → `{"ok": true, ...}`
   - `GET https://<project>.vercel.app/api/check` with header
     `x-monitor-secret: <MONITOR_SECRET>` → JSON cycle report.

## Scheduling

Vercel Hobby cron supports only **one invocation per day**. A daily cron is
configured (04:15 UTC) as a safety net — it requires `CRON_SECRET` to be set.

For real monitoring, use an external scheduler:

**cron-job.org** (recommended):
- URL: `https://<project>.vercel.app/api/check`
- Method: `GET`
- Header: `x-monitor-secret: <MONITOR_SECRET>`
- Schedule: every **10 minutes** (avoids overloading VOX; ~1.5k page
  fetches/day across all cinemas and dates)

Manual test: the same `curl` command from your machine.

## Test mode

- `DRY_RUN=1` + call `/api/check`: runs a full cycle against real VOX data,
  reports what *would* be notified, touches neither Telegram nor Redis.
- `TEST_NOTIFY_CHAT_ID=<another-chat>`: notifications go to that chat only.
- Local tests: `pip install -r requirements-dev.txt && pytest`

## Redis state

- Hash `vox:doomsday:sessions`: field = session key, value = JSON
  `{status, first_seen, last_seen, notified_at}`.
- Lock `vox:doomsday:lock` (TTL 240 s) prevents overlapping cycles.
- Delete a field (or the hash) to force a fresh alert for a session.
- If Telegram delivery fails, `notified_at` is **not** stamped, so the alert
  is retried on the next cycle.

## Operational notes

- `robots.txt` disallows automated access to `/booking/` URLs; the monitor
  never fetches booking pages — availability comes from the showtimes page
  itself, which is VOX's own server-side classification.
- The monitor makes read-only GET requests, purchases nothing, and never
  touches payment or account flows.
- Opening-day inventory changes live: sessions flip between bookable and
  unavailable within minutes. Every alert reflects the page state at fetch
  time; verify on the linked booking page before paying.
