#!/usr/bin/env python3
"""
CLCBD Legal Reference Dictionary Scraper
=========================================
Scrapes the three legal reference *dictionary* collections from clcbd.org and
writes Obsidian-compatible Markdown with YAML frontmatter and per-term headings.

Scope
-----
This scraper covers only the reference/dictionary section of clcbd.org — not
full act text, case law, or judgments. The three collections are:
  1. Law Dictionary  — plain-language definitions of Bangladeshi legal terms
  2. Latin Terms     — Latin maxims used in Bangladeshi courts (IDs 1–119, fixed)
  3. Glossary        — additional legal vocabulary

Output (one .md file per collection)
--------------------------------------
Each file has a YAML frontmatter block followed by one `##` heading per term
and its definition as a plain paragraph. This layout is intentionally LLM-friendly:
  - Terms are retrievable by heading
  - Definitions sit in plain paragraphs (no table cell escaping)
  - `---` separators mark entry boundaries for semantic chunking

Stopping heuristics for open-ended collections (lawdictionary, glossary)
--------------------------------------------------------------------------
  - HTTP 404 / redirect away from the detail page
  - Page with no detail-view table (index or error page)
  - Duplicate term already collected (server looped back)
  - Stops after DEAD_STREAK_LIMIT consecutive misses (default 20)

Usage
-----
  python clcbd_scraper.py                              # scrape all three
  python clcbd_scraper.py --collection latinterms
  python clcbd_scraper.py --collection lawdictionary --max-id 1200
  python clcbd_scraper.py --from-id 50                # resume from ID 50
  python clcbd_scraper.py --skip-existing              # skip already-written files
  python clcbd_scraper.py --delay 0.5 --retries 5 --cache-dir .cache
  python clcbd_scraper.py --force-refresh              # bypass cache
  python clcbd_scraper.py --verbose
"""

from __future__ import annotations

import logging
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
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
from .http import BaseScraper

# ---------------------------------------------------------------------------
# Rich (optional) — falls back to plain logging gracefully
# ---------------------------------------------------------------------------
try:
    from rich.console import Console
    from rich.logging import RichHandler
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
    )

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
        "+https://github.com/darthdemono/bdlaw-scraper)"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

# Per-collection config: (url_template, known_max_id or None, output_filename, title)
COLLECTIONS: dict[str, dict] = {
    "latinterms": {
        "url_template": f"{BASE_URL}/latinterms/{{id}}.html",
        "max_id": 119,  # known upper bound; scraper stops here
        "output": "latinterms.md",
        "title": "Latin Terms",
        "term_field": "Title",  # <th> label whose <td> is the term
        "def_field": "Details",  # <th> label whose <td> is the definition
    },
    "lawdictionary": {
        "url_template": f"{BASE_URL}/lawdictionary/{{id}}.html",
        "max_id": None,  # open-ended; auto-detect
        "output": "lawdictionary.md",
        "title": "Law Dictionary",
        "term_field": "Title",
        "def_field": "Details",
    },
    "glossary": {
        "url_template": f"{BASE_URL}/glossary/{{id}}.html",
        "max_id": None,  # open-ended; auto-detect
        "output": "glossary.md",
        "title": "Glossary",
        "term_field": "Title",
        "def_field": "Details",
    },
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class Entry:
    """One dictionary entry scraped from a clcbd.org detail page.

    @param id          Numeric page ID from the site URL.
    @param term        The term or Latin maxim (heading text).
    @param definition  Plain-text definition or explanation.
    @param category    Optional category label (e.g. "Civil Law"), may be "".
    """

    id: int
    term: str
    definition: str
    category: str = ""


# ---------------------------------------------------------------------------
# HTTP / caching
# ---------------------------------------------------------------------------
class Scraper(BaseScraper):
    """clcbd-specific HTTP client built on BaseScraper.

    Overrides _fetch_raw_inner to return None on 404 and on redirect
    away from the requested detail page (the site sends 200 + redirect
    to its index when an ID doesn't exist).
    """

    def __init__(
        self,
        delay: float = 0.3,
        max_retries: int = 4,
        timeout: int = 20,
        cache_dir: Optional[Path] = None,
        force_refresh: bool = False,
    ) -> None:
        super().__init__(
            delay=delay,
            max_retries=max_retries,
            timeout=timeout,
            cache_dir=cache_dir,
            force_refresh=force_refresh,
            logger_name="clcbd",
            headers=HEADERS,
        )

    def _fetch_raw_inner(self, url: str) -> Optional[str]:  # type: ignore[override]
        """Return page HTML, or None on 404 / index redirect."""
        self._throttle()
        resp = self.session.get(url, timeout=self.timeout, allow_redirects=True)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        if _is_index_page(resp.url, url):
            return None
        return resp.text

    def fetch(self, url: str) -> Optional[BeautifulSoup]:  # type: ignore[override]
        """Return a BeautifulSoup for url, None if the page doesn't exist.

        Uses the disk cache when available (unless force_refresh is set).
        """
        cached = self._read_cache(url)
        if cached is not None:
            logging.getLogger("clcbd").debug("Cache hit: %s", url)
            return BeautifulSoup(cached, "lxml")

        html = self._fetch_raw(url)
        if html is None:
            return None

        self._write_cache(url, html)
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
    req_path = requested_url.rsplit("/", 1)[-1]  # e.g. "1234.html"
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

    category = fields.get("Category", fields.get("Type", ""))
    return Entry(id=entry_id, term=term, definition=definition, category=category)


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
    from_id: int = 1,
) -> list[Entry]:
    """Scrape all entries from one clcbd.org dictionary collection.

    Iterates IDs sequentially from from_id, stopping at max_id or after
    DEAD_STREAK_LIMIT consecutive misses for open-ended collections.

    @param name            Collection key (e.g. "lawdictionary").
    @param config          Config dict from COLLECTIONS.
    @param scraper         Scraper instance.
    @param max_id_override Override the collection's built-in max_id cap.
    @param log             Logger instance.
    @param from_id         First ID to fetch (for resuming partial runs).
    @return                List of Entry objects in ID order.
    """
    url_tpl    = config["url_template"]
    term_field = config["term_field"]
    def_field  = config["def_field"]
    title      = config["title"]

    hard_max   = max_id_override or config["max_id"]
    open_ended = hard_max is None

    entries: list[Entry] = []
    seen_terms: set[str] = set()

    consecutive_dead = 0
    current_id = from_id

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
                log.warning(
                    "HTTP %s on ID %d — skipping", exc.response.status_code, current_id
                )
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
                    log.debug(
                        "Duplicate '%s' at ID %d — skipping", entry.term, current_id
                    )
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

_RE_CASE = re.compile(r"\b(?:Vs?\.?|vs\.?)\s|\b(?:DLR|BLD|CLC|MLR|ADC|BLT|BLC|AIR)\b|\b(?:Act|Ordinance|Order)\b.*\d{4}")
_RE_STATUS = re.compile(r"(?i)(?:now stands repealed|spent|sic|repealed|omitted)\.?")


def _balanced(s: str, i: int) -> int:
    """Index of the bracket closing the one at s[i], or -1."""
    o, c = s[i], "]" if s[i] == "[" else ")"
    depth = 0
    for j in range(i, len(s)):
        depth += (s[j] == o) - (s[j] == c)
        if depth == 0:
            return j
    return -1


def _is_reference(open_ch: str, inner: str) -> bool:
    """True for a source citation: any [..] except bare numbers and status
    notes, or a (..) holding a case, report or act citation."""
    if open_ch == "[":
        return bool(inner) and not inner.isdigit() and not _RE_STATUS.fullmatch(inner)
    return len(inner) > 15 and bool(_RE_CASE.search(inner))


def extract_references(definition: str, notes: list[str]) -> str:
    """Replace each source citation in a definition with a footnote marker.

    Citations are appended to notes (marker numbers continue across calls),
    so a whole page can share one footnote list. A full stop that trailed a
    bracketed citation is dropped, the marker already follows the sentence.
    """
    out: list[str] = []
    i = 0
    while i < len(definition):
        ch = definition[i]
        j = _balanced(definition, i) if ch in "[(" else -1
        inner = re.sub(r"\s+", " ", definition[i + 1 : j]).strip(" .;") if j > 0 else ""
        if j > 0 and _is_reference(ch, inner):
            notes.append(re.sub(r"^(see also|see|cited by),?\s+", lambda m: m.group(0).capitalize(), inner, flags=re.I))
            out = ["".join(out).rstrip(), f"[^{len(notes)}]"]
            i = j + 1
            if ch == "[" and definition[i : i + 1] == ".":
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def tidy_text(t: str) -> str:
    """Normalise whitespace and spacing around punctuation and markers."""
    t = " ".join(unicodedata.normalize("NFC", t).split())
    t = re.sub(r"\(\s+", "(", t)
    t = re.sub(r"\s+([,;:.)])", r"\1", t)
    t = re.sub(r"([,;:])(?=[A-Za-z\u201c\u2018])", r"\1 ", t)
    t = re.sub(r"(\[\^\d+\])(?=[A-Za-z\u201c\u2018(])", r"\1 ", t)
    return t.replace("()", "").strip()


def _sort_key(term: str) -> str:
    return re.sub(r"^[^A-Za-z0-9]+", "", term).casefold()


def _term_key(term: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", term.lower()).strip()


def merge_duplicates(entries: list[Entry]) -> list[Entry]:
    """Merge entries whose terms differ only in case or punctuation.

    Identical definitions collapse to one. A definition that is wholly
    contained in another is dropped. Definitions that really differ are all
    kept, in one entry, separated by a blank line (<br><br>).
    """
    groups: dict[str, list[Entry]] = {}
    for e in entries:
        groups.setdefault(_term_key(e.term), []).append(e)
    merged = []
    for group in groups.values():
        first = group[0]
        parts: list[str] = []
        for e in group:
            d = " ".join(e.definition.split())
            if any(d in p for p in parts):
                continue
            parts = [p for p in parts if p not in d] + [d]
        term = first.term.rstrip(" :")
        merged.append(Entry(first.id, term, "<br><br>".join(parts), first.category))
    return merged


_RE_YEAR = re.compile(r"\b(1[6-9]\d\d|20\d\d)\b")
_RE_SECTION_BEFORE = re.compile(
    r"(?i)\b((?:sections?|articles?|rules?)\s+)(\d+[A-Za-z]{0,2}(?:\s*\([^()]*\))*"
    r"(?:\s*(?:,|and|&)\s*\d+[A-Za-z]{0,2}(?:\s*\([^()]*\))*)*)[\s,;]*(?:of\s+)?(?:the\s+)?$"
)
_RE_NUM = re.compile(r"(\d+[A-Za-z]{0,2})((?:\s*\([^()]*\))*)")


def _name_key(name: str) -> str:
    name = name.replace("&", " and ").replace("\u2019", "'")
    name = re.sub(r"^the\s+", "", name.strip(), flags=re.I)
    name = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
    name = " ".join(w[:-1] if len(w) > 3 and w.endswith("s") else w for w in name.split())  # Instrument/Instruments
    return _ALIASES.get(name, name)


# How the dictionaries write a few common acts versus their official titles
_ALIASES = {
    "criminal procedure code 1898": "code of criminal procedure 1898",
    "civil procedure code 1908": "code of civil procedure 1908",
}


class ActLinks:
    """Resolves act names found in footnotes to the act files of a vault.

    Built from the `title:` line of each act-N.md in acts_dir. A name only
    links when exactly one act has it. A section named just before the act
    links to that section's block anchor, when the act file really has it.
    """

    def __init__(self, acts_dir: Path) -> None:
        self.dir = Path(acts_dir)
        self.by_key: dict[str, set[str]] = {}
        for f in self.dir.glob("act-*.md"):
            with f.open(encoding="utf-8") as fh:
                head = [next(fh, "") for _ in range(4)]
            m = next((re.match(r'title:\s*"?(.*?)"?\s*$', l) for l in head if l.startswith("title:")), None)
            if m:
                self.by_key.setdefault(_name_key(m.group(1)), set()).add(f.stem)
        self._text: dict[str, str] = {}

    def _has_anchor(self, act: str, anchor: str) -> bool:
        if act not in self._text:
            self._text[act] = (self.dir / f"{act}.md").read_text(encoding="utf-8")
        return bool(anchor) and re.search(rf"\^sec-{anchor}(?![a-z0-9])", self._text[act]) is not None

    def _sections(self, before: str, act: str) -> str:
        m = _RE_SECTION_BEFORE.search(before)
        if not m:
            return before
        from .scraper import _secnum_to_block_id

        def link(n: re.Match) -> str:
            anchor = _secnum_to_block_id(n.group(1))
            if not self._has_anchor(act, anchor):
                return n.group(0)
            return f"[[{act}#^sec-{anchor}|{n.group(0)}]]"

        return before[: m.start()] + m.group(1) + _RE_NUM.sub(link, m.group(2)) + before[m.end(2) :]

    def link(self, note: str) -> str:
        """Wikilink every act named in a footnote (and the section before it)."""
        out, pos = [], 0
        for ym in _RE_YEAR.finditer(note):
            if ym.start() < pos:
                continue
            words = list(re.finditer(r"[^\s,;]+", note[max(pos, ym.start() - 160) : ym.start()]))
            base = max(pos, ym.start() - 160)
            for k in range(min(len(words), 14), 0, -1):
                start = base + words[-k].start()
                if note[start : start + 4].lower() == "the ":
                    continue
                acts = self.by_key.get(_name_key(note[start : ym.end()]))
                if acts and len(acts) == 1:
                    act = next(iter(acts))
                    out.append(self._sections(note[pos:start], act))
                    out.append(f"[[{act}|{note[start:ym.end()]}]]")
                    pos = ym.end()
                    break
        return "".join(out) + note[pos:]


def read_table_entries(path: Path) -> list[Entry]:
    """Read entries back from an older two-column Term | Definition table."""
    rows = [l for l in path.read_text(encoding="utf-8").split("\n") if l.startswith("|")][2:]
    entries = []
    for n, row in enumerate(rows, 1):
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        if len(cells) >= 2 and cells[0] and cells[1]:
            entries.append(Entry(id=n, term=cells[0], definition=cells[1]))
    return entries


def write_markdown(
    entries: list[Entry], config: dict, output_path: Path, acts: Optional[ActLinks] = None
) -> None:
    """Write entries as an Obsidian-compatible page, A to Z.

    Layout: YAML frontmatter, a jump line, then one section per initial
    letter holding a Term | Definition table. Source citations are taken out
    of the definitions and listed as footnotes at the end of the page.
    Writes atomically via a .tmp file renamed on success.

    @param entries      List of Entry objects to render.
    @param config       Collection config dict (for title and url_template).
    @param output_path  Destination .md file path.
    """
    title = config["title"]
    source_url = config["url_template"].replace("/{id}.html", "/")
    entries = merge_duplicates(entries)
    notes: list[str] = []
    sections: dict[str, list[tuple[str, str]]] = {}
    for e in sorted(entries, key=lambda e: _sort_key(e.term)):
        term = tidy_text(e.term)
        letter = _sort_key(term)[:1].upper()
        sections.setdefault(letter if letter.isalpha() else "0-9", []).append(
            (term, tidy_text(extract_references(e.definition, notes)))
        )
    order = sorted(sections, key=lambda k: (k == "0-9", k))

    lines = [
        "---",
        f'title: "{title}"',
        f"URL: {source_url}",
        f"description: {len(entries)} entries from clcbd.org, sorted A to Z, with the source of each definition in the footnotes.",
        f"datetime: {datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}",
        "tags:",
        "  - Legal",
        "  - Bangladesh",
        "---",
        "",
        f"*{len(entries)} entries.* Jump to: " + " ".join(f"[{k}](#{k.lower()})" for k in order),
        "",
    ]
    for k in order:
        lines += [f"## {k}", "", "| Term | Definition |", "| --- | --- |"]
        lines += [f"| **{t.replace('|', chr(92) + '|')}** | {d.replace('|', chr(92) + '|')} |" for t, d in sections[k]]
        lines.append("")
    if notes:
        # Same heading the acts carry: it keeps the markdown readable in Obsidian, the site swaps it for its own label.
        lines += ["## Footnotes", ""]
        lines += [f"[^{i}]: {acts.link(n) if acts else n}" for i, n in enumerate(notes, 1)]

    tmp = output_path.with_suffix(".md.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(output_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
@click.command()
@click.option(
    "--collection",
    "-c",
    type=click.Choice(["all", "latinterms", "lawdictionary", "glossary"]),
    default="all",
    show_default=True,
    help="Which collection(s) to scrape.",
)
@click.option(
    "--output-dir",
    "-o",
    default=".",
    show_default=True,
    type=click.Path(),
    help="Directory where .md files are written.",
)
@click.option(
    "--max-id",
    "-m",
    default=None,
    type=int,
    help=(
        "Override upper ID bound for open-ended collections "
        "(lawdictionary, glossary). Ignored for latinterms."
    ),
)
@click.option(
    "--delay",
    "-d",
    default=0.3,
    show_default=True,
    type=float,
    help="Minimum seconds between HTTP requests.",
)
@click.option(
    "--retries",
    "-r",
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
    "--from-id",
    default=1,
    show_default=True,
    type=int,
    help="First entry ID to fetch. Use to resume a partial run.",
)
@click.option(
    "--skip-existing",
    is_flag=True,
    default=False,
    help="Skip collections whose output .md file already exists.",
)
@click.option(
    "--force-refresh",
    is_flag=True,
    default=False,
    help="Ignore disk cache and fetch live pages.",
)
@click.option(
    "--acts-dir",
    type=click.Path(exists=True, file_okay=False),
    default=None,
    help="Folder of act-N.md files. Acts named in footnotes become [[wikilinks]] to them.",
)
@click.option(
    "--reformat",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help=(
        "Offline: re-render an older Term | Definition table file for the chosen "
        "collection in the current layout (A to Z, footnoted sources). No fetching."
    ),
)
@click.option(
    "--verbose",
    "-v",
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
    from_id: int,
    skip_existing: bool,
    force_refresh: bool,
    acts_dir: Optional[str],
    reformat: Optional[str],
    verbose: bool,
) -> None:
    """Scrape legal reference dictionaries (Law Dictionary, Latin Terms, Glossary) from clcbd.org."""

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

    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    cache_path = Path(cache_dir) if cache_dir else None

    acts = ActLinks(Path(acts_dir)) if acts_dir else None
    if reformat:
        if collection == "all":
            raise click.UsageError("--reformat needs a single --collection")
        cfg = COLLECTIONS[collection]
        entries = read_table_entries(Path(reformat))
        write_markdown(entries, cfg, out_path / cfg["output"], acts)
        log.info("Reformatted %d entries from %s", len(entries), reformat)
        return

    scraper = Scraper(
        delay=delay,
        max_retries=retries,
        cache_dir=cache_path,
        force_refresh=force_refresh,
    )

    targets = (
        list(COLLECTIONS.items())
        if collection == "all"
        else [(collection, COLLECTIONS[collection])]
    )

    for name, cfg in targets:
        dest = out_path / cfg["output"]
        if skip_existing and dest.exists():
            log.info("Skipping %s — %s already exists", name, dest)
            continue
        try:
            entries = scrape_collection(
                name=name,
                config=cfg,
                scraper=scraper,
                max_id_override=max_id if name != "latinterms" else None,
                log=log,
                from_id=from_id,
            )
            write_markdown(entries, cfg, dest, acts)
            log.info("✅  %s → %s  (%d entries)", name, dest, len(entries))
        except KeyboardInterrupt:
            log.warning("Interrupted — partial output may have been written.")
            sys.exit(130)
        except Exception as e:
            log.error("Failed to scrape %s: %s", name, e)
            if verbose:
                raise


if __name__ == "__main__":
    main()
