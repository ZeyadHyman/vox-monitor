"""Shared helpers for real-Upstash integration tests.

Secrets are read from the environment or from a gitignored ``.env.integration``
file at the repository root. Values are NEVER printed, logged, or written to
any file by this module.
"""

from __future__ import annotations

import os
from pathlib import Path

# Every key the integration tests create lives under this prefix so they can
# never collide with production state (vox:doomsday:sessions / vox:doomsday:lock).
TEST_PREFIX = "test:vox-monitor:"

REPO_ROOT = Path(__file__).resolve().parents[1]
CREDS_FILE = REPO_ROOT / ".env.integration"


def load_upstash_creds() -> tuple[str, str] | None:
    """Return ``(REST_URL, REST_TOKEN)`` or ``None`` when unavailable."""
    url = os.environ.get("UPSTASH_REDIS_REST_URL", "").strip().strip('"')
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "").strip().strip('"')
    if not (url and token) and CREDS_FILE.exists():
        for line in CREDS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key == "UPSTASH_REDIS_REST_URL" and not url:
                url = value
            elif key == "UPSTASH_REDIS_REST_TOKEN" and not token:
                token = value
    if url.startswith("https://") and len(token) >= 8:
        return url, token
    return None
