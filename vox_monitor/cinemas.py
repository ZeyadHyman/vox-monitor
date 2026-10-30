"""Monitoring scope: City Centre Almaza ONLY.

There is deliberately no cinema discovery and no configurable override:
the project scope forbids scanning any other branch (City Centre
Alexandria, Mall of Egypt, ...). A single hard-coded cinema means the
cycle issues one showtimes page per date and can never drift into other
locations, and no request to /cinemas is needed at all.
"""

from __future__ import annotations

from .models import Cinema

MONITORED_CINEMA = Cinema(slug="city-centre-almaza", name="City Centre Almaza")
