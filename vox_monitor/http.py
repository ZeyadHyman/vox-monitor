"""HTTP client for VOX with Chrome TLS impersonation, retries, and timeouts.

The VOX Egypt origin sits behind Akamai bot filtering that accepts the TLS
and HTTP/2 fingerprints of real browsers and silently drops connections from
clients with other fingerprints (verified: stdlib urllib and default curl
connect at TCP level but receive zero bytes). curl_cffi's ``impersonate``
mode reproduces Chrome's fingerprints, which is required for reliable access.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol


class HttpFetchError(Exception):
    """All retry attempts for a URL failed."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    text: str


class HttpClient(Protocol):
    def get(self, url: str) -> HttpResponse: ...


_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class VoxHttpClient:
    """Synchronous GET client using curl_cffi Chrome impersonation."""

    def __init__(
        self,
        timeout: float = 12.0,
        max_retries: int = 1,
        backoff_base: float = 1.5,
    ) -> None:
        try:
            from curl_cffi import requests as cffi_requests
        except ImportError as exc:  # pragma: no cover - deployment misconfig
            raise HttpFetchError(
                "curl_cffi is not installed; it is required to reach VOX"
            ) from exc
        self._requests = cffi_requests
        self._timeout = timeout
        self._max_retries = max(0, max_retries)
        self._backoff_base = backoff_base

    def get(self, url: str) -> HttpResponse:
        last_error: Exception | None = None
        attempts = self._max_retries + 1
        for attempt in range(attempts):
            try:
                resp = self._requests.get(
                    url,
                    impersonate="chrome",
                    timeout=self._timeout,
                    allow_redirects=True,
                )
            except Exception as exc:  # curl_cffi raises various network errors
                last_error = exc
                if attempt < attempts - 1:
                    time.sleep(self._backoff_base * (attempt + 1))
                continue
            if resp.status_code in _RETRYABLE_STATUS and attempt < attempts - 1:
                last_error = HttpFetchError(f"HTTP {resp.status_code} for {url}")
                time.sleep(self._backoff_base * (attempt + 1))
                continue
            return HttpResponse(status=resp.status_code, text=resp.text)
        raise HttpFetchError(f"GET {url} failed after {attempts} attempts: {last_error}")
