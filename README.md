# VOX Almaza Ticket Monitor

Telegram notification bot that watches the official VOX Cinemas Egypt website
(https://egy.voxcinemas.com/) for **newly bookable sessions at City Centre
Almaza** — for **every movie** playing there, discovered automatically from
the live site (no hardcoded movie list).

Monitoring scope: **City Centre Almaza only** (hard-coded constant; no
discovery, no override). New films appear and disappear on their own — the
bot picks them up on the next cycle.

Bot: `@AvengersVoxWatcherBot` · Production: `https://vox-monitor-tau.vercel.app`

---

## How bookability is determined

The monitor fetches the server-rendered showtimes page with **no `m=`
parameters**, which makes VOX render **every movie** at the cinema:

```
GET https://egy.voxcinemas.com/showtimes?c=city-centre-almaza[&d=YYYYMMDD]
```

VOX itself renders availability into the page (verified against the live site
and the real booking flow):

| Rendered markup | Classification |
| --- | --- |
| `<a class="action showtime" href="https://egy.voxcinemas.com/booking/{id}">10:45am</a>` | **BOOKABLE** — VOX generated a live booking link |
| `<span class="action showtime unavailable">10:30pm</span>` | **UNAVAILABLE** — rendered without a booking link (sold out / closed) |
| Session `<li data-id>` with neither marker | **UNKNOWN** — never alerted |

Each movie article (`<article class="movie-compare" data-slug="…">`)
contributes its slug, `<h2>` name and hero-image movie id (`HO…`), so sessions
are uniquely identified by
`movie_id|cinema_slug|date|time|format|session_id`
(e.g. `HO00013676|city-centre-almaza|20261223|10:45am|MAX|0047-276988`).
Alerts fire only on a transition **to** `bookable`; known bookable sessions
are suppressed via Redis state.

The movie set is **date-dependent**: different on-sale dates list different
films, so the cycle unions movies across the no-date page and every date in
the page's date navigation.

**Important client requirement:** the VOX origin is behind Akamai bot
filtering. Non-browser TLS fingerprints (Python `urllib`, default `curl`)
complete the TCP handshake but receive **zero bytes**. The monitor uses
`curl_cffi` with Chrome TLS/HTTP2 impersonation — this is a hard dependency.

## Architecture

```
api/check.py          Vercel handler (auth → run cycle → JSON report)
api/health.py         Unauthenticated liveness probe
vox_monitor/
  config.py           Settings from env; showtimes URL builder
  movies.py           Dynamic Movie model + cross-page movie union
  http.py             Chrome-impersonating HTTP client with retries
  showtimes.py        HTML parsers (showtimes pages: sessions + movies)
  cinemas.py          Single monitored-cinema constant (Almaza)
  models.py           Session / availability / report models
  state.py            Upstash Redis store + dedup decision logic
  telegram.py         Message formatting, chunking, sending
  orchestrator.py     One monitoring cycle (fetch → classify → diff → alert)
tests/                97 tests incl. fixtures from the real VOX site
```

One cycle: fetch Almaza's no-date showtimes page (first on-sale date + date
navigation + current movie list) → fetch the remaining dates with bounded
concurrency and a per-cycle time budget (deadline cancels queued fetches) →
union all discovered movies → normalize sessions → prune state for past
dates → diff against Redis → send Telegram alerts for newly bookable
sessions → persist state. Partial failures are reported but never erase
state and never mean "no tickets". A Redis lock prevents overlapping cycles.

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
| `MAX_WORKERS` | no | Concurrent VOX fetches (default 3) |
| `MAX_DATES_PER_CINEMA` | no | Date cap per cycle (default 60; name kept for compat) |
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
- Schedule: every **10 minutes** (avoids overloading VOX)

Manual test: the same `curl` command from your machine.

## Test mode

- `DRY_RUN=1` + call `/api/check`: runs a full cycle against real VOX data,
  reports what *would* be notified, touches neither Telegram nor Redis.
- `TEST_NOTIFY_CHAT_ID=<another-chat>`: notifications go to that chat only.
- Local tests: `pip install -r requirements-dev.txt && pytest`
- Real-Upstash integration tests (cross-process persistence) run when
  `UPSTASH_REDIS_REST_URL`/`UPSTASH_REDIS_REST_TOKEN` are set in the
  environment or in a local gitignored `.env.integration`; they use a
  `test:vox-monitor:` key prefix and never touch production keys.

## Redis state

- Hash `vox:monitor:sessions`: field = session key, value = JSON
  `{status, first_seen, last_seen, notified_at}`.
- Lock `vox:monitor:lock` (TTL 240 s) prevents overlapping cycles.
- Delete a field (or the hash) to force a fresh alert for a session.
- If Telegram delivery fails, `notified_at` is **not** stamped, so the alert
  is retried on the next cycle.
- Fields whose session date has passed are pruned automatically at the end
  of a real (non-dry-run) cycle.

## Operational notes

- `robots.txt` disallows automated access to `/booking/` URLs; the monitor
  never fetches booking pages — availability comes from the showtimes page
  itself, which is VOX's own server-side classification.
- The monitor makes read-only GET requests, purchases nothing, and never
  touches payment or account flows.
- Opening-day inventory changes live: sessions flip between bookable and
  unavailable within minutes. Every alert reflects the page state at fetch
  time; verify on the linked booking page before paying.
