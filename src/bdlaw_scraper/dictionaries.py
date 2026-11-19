#!/usr/bin/env python3
"""
CLCBD Law Portal Scraper
========================
Scrapes three reference collections from clcbd.org and writes clean Markdown tables.

Collections:
  1. Law Dictionary  → https://www.clcbd.org/lawdictionary/{id}.html
  2. Latin Terms     → https://www.clcbd.org/latinterms/{id}.html   (IDs 1–119)
  3. Glossary        → https://www.clcbd.org/glossary/{id}.html

Output files (one per collection):
  lawdictionary.md, latinterms.md, glossary.md

Stopping heuristics for open-ended collections (lawdictionary, glossary):
  - HTTP 404 / redirect away from detail page
  - Empty / boilerplate page (no detail-view table found)
  - Duplicate title seen (server looped back to start)
  - Hard cap: if --max-id not set, stops after seeing 15 consecutive missing pages

Usage:
  python clcbd_scraper.py                         # scrape all three
  python clcbd_scraper.py --collection latinterms
  python clcbd_scraper.py --collection lawdictionary --max-id 1200
  python clcbd_scraper.py --delay 0.5 --retries 5 --cache-dir .cache
  python clcbd_scraper.py --verbose
"""

from __future__ import annotations

import logging
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import click
import requests
from bs4 import BeautifulSoup, Tag
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

# ---------------------------------------------------------------------------
# Rich (optional) — falls back to plain logging gracefully
# ---------------------------------------------------------------------------
try:
    from rich.console import Console
    from rich.logging import RichHandler
    from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn

    _RICH = True
    console = Console(stderr=True)
except ImportError:
    _RICH = False
    console = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BASE_URL = "http://www.clcbd.org"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; CLCBDScraper/1.0; "
        "+https://github.com/darthdemono/clcbd-scraper)"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

# Per-collection config: (url_template, known_max_id or None, output_filename, title)
COLLECTIONS: dict[str, dict] = {
    "latinterms": {
        "url_template": f"{BASE_URL}/latinterms/{{id}}.html",
        "max_id":       119,        # known upper bound; scraper stops here
        "output":       "latinterms.md",
        "title":        "Latin Terms",
        "term_field":   "Title",    # <th> label whose <td> is the term
        "def_field":    "Details",  # <th> label whose <td> is the definition
    },
    "lawdictionary": {
        "url_template": f"{BASE_URL}/lawdictionary/{{id}}.html",
        "max_id":       None,       # open-ended; auto-detect
        "output":       "lawdictionary.md",
        "title":        "Law Dictionary",
        "term_field":   "Title",
        "def_field":    "Details",
    },
    "glossary": {
        "url_template": f"{BASE_URL}/glossary/{{id}}.html",
        "max_id":       None,       # open-ended; auto-detect
        "output":       "glossary.md",
        "title":        "Glossary",
        "term_field":   "Title",
        "def_field":    "Details",
    },
}




# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class Entry:
    id: int
    term: str
    definition: str


# ---------------------------------------------------------------------------
# HTTP / caching
# ---------------------------------------------------------------------------
class Scraper:
    """Thin requests.Session wrapper with retry, rate-limiting, and optional cache."""

    def __init__(
        self,
        delay: float = 0.3,
        max_retries: int = 4,
        timeout: int = 20,
        cache_dir: Optional[Path] = None,
    ) -> None:
        self.delay    = delay
        self.timeout  = timeout
        self.cache_dir = cache_dir
        self._last    = 0.0

        self.session = requests.Session()
        self.session.headers.update(HEADERS)

        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)

        self._fetch_raw = retry(
            retry=retry_if_exception_type(requests.RequestException),
            stop=stop_after_attempt(max_retries),
            wait=wait_exponential(multiplier=1, min=2, max=30),
            before_sleep=before_sleep_log(logging.getLogger("clcbd"), logging.WARNING),
            reraise=True,
        )(self._fetch_raw_inner)

    def _fetch_raw_inner(self, url: str) -> Optional[str]:
        """Return page HTML or None on 404."""
        elapsed = time.monotonic() - self._last
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        resp = self.session.get(url, timeout=self.timeout, allow_redirects=True)
        self._last = time.monotonic()
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        # Detect redirect away from detail page (site sends 200 + redirect to index)
        if _is_index_page(resp.url, url):
            return None
        return resp.text

    def fetch(self, url: str) -> Optional[BeautifulSoup]:
        """
        Returns a BeautifulSoup for *url*, None if the page doesn't exist,
        or raises on unrecoverable HTTP errors.
        Respects cache if --cache-dir given.
        """
        if self.cache_dir:
            key      = re.sub(r"[^\w]", "_", url) + ".html"
            cfile    = self.cache_dir / key
            if cfile.exists():
                logging.getLogger("clcbd").debug("Cache hit: %s", url)
                return BeautifulSoup(cfile.read_text("utf-8"), "lxml")

        html = self._fetch_raw(url)

        if html is None:
            return None

        if self.cache_dir:
            cfile.write_text(html, "utf-8")  # type: ignore[possibly-undefined]

        return BeautifulSoup(html, "lxml")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _is_index_page(final_url: str, requested_url: str) -> bool:
    """
    True if the server redirected us away from the detail page.
    e.g. /lawdictionary/ or /lawdictionary/index.html when ID doesn't exist.
    """
    if final_url == requested_url:
        return False
    # If final URL no longer contains the numeric ID segment, it's an index redirect
    req_path  = requested_url.rsplit("/", 1)[-1]     # e.g. "1234.html"
    final_path = final_url.rsplit("/", 1)[-1]
    return req_path != final_path


def _clean(text: str) -> str:
    """NFC normalise + collapse whitespace."""
    text = unicodedata.normalize("NFC", text)
    return " ".join(text.split())


def _md_escape(text: str) -> str:
    """Escape pipe characters so they don't break Markdown tables."""
    return text.replace("|", "\\|")


def parse_entry(
    soup: BeautifulSoup,
    entry_id: int,
    term_field: str,
    def_field: str,
) -> Optional[Entry]:
    """
    Extract a single Entry from a detail page.

    The page structure is:
      <h3>Term title</h3>
      <table class="detail-view ...">
        <tbody>
          <tr><th>Category</th><td>…</td></tr>
          <tr><th>Title</th><td>…</td></tr>        ← term_field
          <tr><th>Details</th><td>…</td></tr>      ← def_field
          …
        </tbody>
      </table>

    Falls back to <h3> text if the term_field row is missing.
    """
    tbl = soup.find("table", class_="detail-view")
    if not tbl:
        return None  # Not a detail page (index page, error page, etc.)

    fields: dict[str, str] = {}
    for tr in tbl.find_all("tr"):
        th = tr.find("th")
        td = tr.find("td")
        if th and td:
            key = _clean(th.get_text())
            val = _clean(td.get_text())
            fields[key] = val

    # Term: prefer explicit field, fall back to <h3>
    term = fields.get(term_field, "")
    if not term:
        h3 = soup.find("h3")
        term = _clean(h3.get_text()) if h3 else ""
    if not term:
        return None

    definition = fields.get(def_field, "")
    if not definition:
        return None

    return Entry(id=entry_id, term=term, definition=definition)


# ---------------------------------------------------------------------------
# Per-collection scrape loop
# ---------------------------------------------------------------------------

# A "dead" ID is one that yields no new unique entry:
#   • 404 / redirect away from detail page
#   • page has no detail-view table
#   • page is a duplicate of an already-collected term
#
# We stop an open-ended collection only after DEAD_STREAK_LIMIT *consecutive*
# dead IDs, not on the very first duplicate. This tolerates sites that have
# gaps, re-used IDs, or occasional duplicates sprinkled through their ID space.
DEAD_STREAK_LIMIT = 20


def scrape_collection(
    name: str,
    config: dict,
    scraper: Scraper,
    max_id_override: Optional[int],
    log: logging.Logger,
) -> list[Entry]:
    url_tpl    = config["url_template"]
    term_field = config["term_field"]
    def_field  = config["def_field"]
    title      = config["title"]

    hard_max   = max_id_override or config["max_id"]
    open_ended = hard_max is None

    entries: list[Entry] = []
    seen_terms: set[str] = set()

    # consecutive_dead counts IDs in a row that added no new entry
    consecutive_dead = 0
    current_id       = 1

    log.info("── %s ──", title)

    if _RICH:
        prog = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            console=console,
        )
        task = prog.add_task(
            f"[cyan]{name}[/cyan]",
            total=hard_max if hard_max else None,
        )
        prog.start()
    else:
        prog = task = None  # type: ignore[assignment]

    try:
        while True:
            if hard_max and current_id > hard_max:
                log.debug("Reached max ID %d for %s", hard_max, name)
                break

            url = url_tpl.format(id=current_id)
            log.debug("Fetching %s", url)

            # ── fetch ────────────────────────────────────────────────────
            soup: Optional[BeautifulSoup] = None
            try:
                soup = scraper.fetch(url)
            except requests.HTTPError as exc:
                log.warning("HTTP %s on ID %d — skipping", exc.response.status_code, current_id)
            except requests.RequestException as exc:
                log.error("Request failed on ID %d: %s", current_id, exc)

            # ── classify result ──────────────────────────────────────────
            dead = False

            if soup is None:
                # 404, redirect, or network error
                log.debug("Miss on ID %d", current_id)
                dead = True
            else:
                entry = parse_entry(soup, current_id, term_field, def_field)

                if entry is None:
                    # Got a page but no detail-view table (index/listing page)
                    log.debug("No detail-view table on ID %d", current_id)
                    dead = True
                elif entry.term in seen_terms:
                    # Duplicate — skip silently, count as dead
                    log.debug("Duplicate '%s' at ID %d — skipping", entry.term, current_id)
                    dead = True
                else:
                    # Good new entry
                    seen_terms.add(entry.term)
                    entries.append(entry)
                    log.debug("  [%d] %s", entry.id, entry.term[:70])

            # ── update streak counter ────────────────────────────────────
            if dead:
                consecutive_dead += 1
            else:
                consecutive_dead = 0

            current_id += 1
            if _RICH and prog:
                prog.advance(task)

            # ── stopping condition (open-ended only) ─────────────────────
            if open_ended and consecutive_dead >= DEAD_STREAK_LIMIT:
                log.info(
                    "%d consecutive dead IDs — assuming end of %s (last good ID ~%d)",
                    DEAD_STREAK_LIMIT,
                    name,
                    current_id - DEAD_STREAK_LIMIT - 1,
                )
                break

    finally:
        if _RICH and prog:
            prog.stop()

    log.info("Collected %d entries for %s", len(entries), title)
    return entries


# ---------------------------------------------------------------------------
# Markdown writer
# ---------------------------------------------------------------------------
def write_markdown(entries: list[Entry], config: dict, output_path: Path) -> None:
    title = config["title"]
    lines = [
        f"# {title}",
        "",
        f"| Term | Definition |",
        f"|------|------------|",
    ]
    for e in entries:
        term = _md_escape(e.term)
        defn = _md_escape(e.definition)
        lines.append(f"| {term} | {defn} |")
    lines.append("")

    output_path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
@click.command()
@click.option(
    "--collection", "-c",
    type=click.Choice(["all", "latinterms", "lawdictionary", "glossary"]),
    default="all",
    show_default=True,
    help="Which collection(s) to scrape.",
)
@click.option(
    "--output-dir", "-o",
    default=".",
    show_default=True,
    type=click.Path(),
    help="Directory where .md files are written.",
)
@click.option(
    "--max-id", "-m",
    default=None,
    type=int,
    help=(
        "Override upper ID bound for open-ended collections "
        "(lawdictionary, glossary). Ignored for latinterms."
    ),
)
@click.option(
    "--delay", "-d",
    default=0.3,
    show_default=True,
    type=float,
    help="Minimum seconds between HTTP requests.",
)
@click.option(
    "--retries", "-r",
    default=4,
    show_default=True,
    type=int,
    help="Max retry attempts per request (exponential back-off).",
)
@click.option(
    "--cache-dir",
    default=None,
    type=click.Path(),
    help="Directory for raw HTML cache. Skips fetch on subsequent runs.",
)
@click.option(
    "--verbose", "-v",
    is_flag=True,
    default=False,
    help="Enable DEBUG-level logging.",
)
def main(
    collection: str,
    output_dir: str,
    max_id: Optional[int],
    delay: float,
    retries: int,
    cache_dir: Optional[str],
    verbose: bool,
) -> None:
    """Scrape law dictionary, Latin terms, and glossary from clcbd.org."""

    # --- logging ---
    level = logging.DEBUG if verbose else logging.INFO
    if _RICH:
        logging.basicConfig(
            level=level,
            format="%(message)s",
            handlers=[RichHandler(console=console, show_path=False, markup=True)],
        )
    else:
        logging.basicConfig(
            level=level,
            format="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
        )
    log = logging.getLogger("clcbd")

    out_path   = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    cache_path = Path(cache_dir) if cache_dir else None

    scraper = Scraper(delay=delay, max_retries=retries, cache_dir=cache_path)

    targets = (
        list(COLLECTIONS.items())
        if collection == "all"
        else [(collection, COLLECTIONS[collection])]
    )

    for name, cfg in targets:
        try:
            entries = scrape_collection(
                name=name,
                config=cfg,
                scraper=scraper,
                max_id_override=max_id if name != "latinterms" else None,
                log=log,
            )
            dest = out_path / cfg["output"]
            write_markdown(entries, cfg, dest)
            log.info("✅  %s → %s  (%d rows)", name, dest, len(entries))
        except KeyboardInterrupt:
            log.warning("Interrupted — partial output may have been written.")
            sys.exit(130)
        except Exception as e:
            log.error("Failed to scrape %s: %s", name, e)
            if verbose:
                raise


if __name__ == "__main__":
    main()