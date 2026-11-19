"""
scraper_utils.py
================
Shared HTTP scraping infrastructure used by bdlaw_scraper and clcbd_scraper.

Provides BaseScraper — a rate-limited, retry-backed, optionally cached
requests.Session wrapper — so both scrapers stay in sync on robustness fixes
without duplicating code.

Subclasses override _fetch_raw_inner() and fetch() for domain-specific
behaviour (e.g. returning None on 404, redirect detection).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Optional

import requests
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


class BaseScraper:
    """Rate-limited HTTP client with exponential-backoff retry and disk cache.

    Thread-safe: concurrent callers on the same instance serialize their
    delay intervals via _rate_lock, but actual HTTP I/O runs in parallel.

    Subclass and override _fetch_raw_inner() to customise response handling
    (e.g. treat 404 as None instead of raising). Override fetch() to change
    the return type or add post-processing.
    """

    def __init__(
        self,
        delay: float = 0.3,
        max_retries: int = 4,
        timeout: int = 30,
        cache_dir: Optional[Path] = None,
        force_refresh: bool = False,
        logger_name: str = "scraper",
        headers: Optional[dict] = None,
    ) -> None:
        """
        @param delay         Minimum seconds between requests (per instance).
        @param max_retries   Maximum retry attempts on RequestException.
        @param timeout       Per-request socket timeout in seconds.
        @param cache_dir     Directory for raw HTML cache; None disables.
        @param force_refresh Ignore cache hits and always fetch live.
        @param logger_name   Logger name used in retry sleep messages.
        @param headers       HTTP headers dict merged into the session.
        """
        self.delay = delay
        self.timeout = timeout
        self.cache_dir = cache_dir
        self.force_refresh = force_refresh
        self._last_request = 0.0
        self._rate_lock = threading.Lock()

        self.session = requests.Session()
        if headers:
            self.session.headers.update(headers)

        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)

        self._fetch_raw = retry(
            retry=retry_if_exception_type(requests.RequestException),
            stop=stop_after_attempt(max_retries),
            wait=wait_exponential(multiplier=1, min=2, max=30),
            before_sleep=before_sleep_log(
                logging.getLogger(logger_name), logging.WARNING
            ),
            reraise=True,
        )(self._fetch_raw_inner)

    # ------------------------------------------------------------------
    # Internal helpers (used by subclasses)
    # ------------------------------------------------------------------

    def _throttle(self) -> None:
        """Enforce the minimum delay between requests (thread-safe)."""
        with self._rate_lock:
            elapsed = time.monotonic() - self._last_request
            if elapsed < self.delay:
                time.sleep(self.delay - elapsed)
            self._last_request = time.monotonic()

    def _cache_path(self, url: str) -> Optional[Path]:
        """Return the cache file path for a URL, or None if cache disabled."""
        if not self.cache_dir:
            return None
        key = re.sub(r"[^\w]", "_", url) + ".html"
        return self.cache_dir / key

    def _read_cache(self, url: str) -> Optional[str]:
        """Return cached HTML for url, or None if not cached / force_refresh."""
        p = self._cache_path(url)
        if p and p.exists() and not self.force_refresh:
            return p.read_text(encoding="utf-8")
        return None

    def _write_cache(self, url: str, html: str) -> None:
        """Write html to the cache file for url (no-op if cache disabled)."""
        p = self._cache_path(url)
        if p:
            p.write_text(html, encoding="utf-8")

    # ------------------------------------------------------------------
    # Override points
    # ------------------------------------------------------------------

    def _fetch_raw_inner(self, url: str) -> str:
        """Perform a single throttled GET and return the response body.

        Default implementation raises on any non-2xx response.
        Override to handle 404 as None, detect redirects, etc.

        @param url  Absolute URL to fetch.
        @return     Response body as a string.
        @raises requests.HTTPError  On 4xx/5xx responses.
        """
        self._throttle()
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        return resp.text

    def fetch(self, url: str):
        """Fetch url, using the cache when available.

        Default returns a string (raw HTML). Override in subclasses to
        return BeautifulSoup or Optional[BeautifulSoup].
        """
        cached = self._read_cache(url)
        if cached is not None:
            return cached
        html = self._fetch_raw(url)
        self._write_cache(url, html)
        return html
