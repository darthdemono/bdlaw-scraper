#!/usr/bin/env python3
"""
Bangladesh Law Scraper — improved & robust edition
====================================================
Scrapes acts from bdlaws.minlaw.gov.bd and produces clean Markdown files.

Improvements over v1:
  - CLI via Click (--act, --output, --delay, --retries, --verbose)
  - Retry logic with exponential backoff (tenacity)
  - Per-request rate-limiting to be a polite scraper
  - Rich progress bar + structured logging
  - Strict HTML→Markdown pipeline with lxml parser for speed
  - Smarter list grouping: alpha / roman / decimal
  - Bengali list support: (ক)(খ)(গ) → lower-alpha ol; (১)(২)(৩) → decimal
  - Bengali section-number extraction (১৷, ২৷ danda style)
  - Section-number extraction handles all edge cases
  - Idempotent output: skips fetch if --cache dir present
  - Proper Unicode normalisation (handles BDLaw's mixed encoding)
  - Configurable: scrape any act by number, not just one hard-coded ID
  - Detailed error messages with context for every failure point

Usage examples:
  python bdlaw_scraper.py --act 1710                        # CrPC
  python bdlaw_scraper.py --act 927                         # Tobacco Control Act (Bengali)
  python bdlaw_scraper.py --act 1710 --output crpc.md
  python bdlaw_scraper.py --act 1710 --delay 0.5 --retries 5
  python bdlaw_scraper.py --act 1710 --cache-dir .cache --verbose
"""

from __future__ import annotations

import logging
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generator, Optional
from urllib.parse import urljoin

import click
import requests
from bs4 import BeautifulSoup, Tag
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)

# ---------------------------------------------------------------------------
# Try to use Rich for pretty output; fall back to plain logging gracefully
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
        TimeElapsedColumn,
    )

    _RICH = True
    console = Console(stderr=True)
except ImportError:  # pragma: no cover
    _RICH = False
    console = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BASE_URL = "http://bdlaws.minlaw.gov.bd"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; BDLawScraper/2.0; "
        "+https://github.com/yourname/bdlaw-scraper)"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ---------------------------------------------------------------------------
# Bengali support constants
# ---------------------------------------------------------------------------

# Map Bengali digits → ASCII digits for int() conversion
BENGALI_DIGIT_MAP = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")

# Bengali consonants used as legal list markers (ক খ গ … and compound কক, কখ)
# U+0995–U+09B9 covers the standard consonants; extras for common ligatures.
_BN_CONS = "কখগঘঙচছজঝঞটঠডঢণতথদধনপফবভমযরলশষসহড়ঢ়য়ৎ"

# Bengali vowels used as sub-list markers: (অ)(আ)(ই)(ঈ)(উ)(ঊ)
# Appear in Section 10-style warning lists nested within (ক)(খ)(গ) items.
_BN_VOWELS = "অআইঈউঊঋএঐওঔ"

# Bengali danda variants used as section-terminating punctuation
# U+0964 = ।  (devanagari danda, also used in Bengali)
# U+0965 = ॥  (double danda — rare but present)
# U+09F7 = ৷  (Bengali currency numerator four — BDLaw uses this as danda)
_BN_DANDA = "৷।॥"

# ---------------------------------------------------------------------------
# Regex patterns compiled once at module level
# ---------------------------------------------------------------------------
RE_FOOTNOTE_INLINE = re.compile(r"\[\d+\]|\[\*+\s*\*+\]|\^\S+\^")
RE_WHITESPACE_COLLAPSE = re.compile(r"[ \t]+")
RE_MULTI_NEWLINE = re.compile(r"\n{3,}")

# Latin list-item patterns
RE_ALPHA_ITEM    = re.compile(r"^\(([a-zA-Z])\)\s+(.*)",            re.DOTALL)
RE_ROMAN_ITEM    = re.compile(r"^\(([ivxlcdmIVXLCDM]+)\)\s+(.*)",  re.DOTALL)
RE_DECIMAL_ITEM  = re.compile(r"^\((\d+)\)\s+(.*)",                 re.DOTALL)

# Bengali list-item patterns
# (ক) text   or   (কক) text  (compound label like "কক" for sub-sub items)
RE_BN_ALPHA_ITEM   = re.compile(
    r"^\(([" + _BN_CONS + r"]+)\)\s+(.*)", re.DOTALL
)
# (১) text   or   (১২) text
RE_BN_DECIMAL_ITEM = re.compile(r"^\(([০-৯]+)\)\s+(.*)", re.DOTALL)

# (অ) text  (আ) text  (ই) text … — Bengali vowel sub-list markers
RE_BN_VOWEL_ITEM = re.compile(
    r"^\(([" + _BN_VOWELS + r"])\)\s+(.*)", re.DOTALL
)

# Section-number patterns — Latin
# Handles "54A.", "[54A.]", "4A.", "102." etc.
RE_SECTION_NUM_BRACKET = re.compile(r"^\[([A-Za-z0-9]+(?:\.[A-Za-z0-9]+)?)\s*\.\s*\]?\s*")
RE_SECTION_NUM_PLAIN   = re.compile(r"^([A-Za-z0-9]+(?:\.[A-Za-z0-9]+)?)\.\s+")
# Handles '140.(1) text' — section number immediately followed by a clause marker
RE_SECTION_NUM_CLAUSE  = re.compile(r"^([A-Za-z0-9]+(?:\.[A-Za-z0-9]+)?)\.[\(\[]([A-Za-z0-9]+)[\)\]]")

# Bengali section-number patterns
# "১৷ "  "২।"  "৬ক।"  "১০৷(১)"  "৬ক।(১)"
# The optional [_BN_CONS]? suffix handles sub-sections like ৬ক, ৬খ, ৬গ, ৬ঘ.
RE_BN_SECTION_NUM_PLAIN  = re.compile(
    r"^([০-৯]+[" + _BN_CONS + r"]?(?:\.[০-৯]+)?)[" + _BN_DANDA + r"]\s+"
)
RE_BN_SECTION_NUM_CLAUSE = re.compile(
    r"^([০-৯]+[" + _BN_CONS + r"]?)[" + _BN_DANDA + r"][\(\[]([০-৯" + _BN_CONS + r"]+)[\)\]]"
)
# Bracketed Bengali: "[৬ক।]" / "[১৷]" style (amended/inserted provisions)
RE_BN_SECTION_NUM_BRACKET = re.compile(
    r"^\[([০-৯]+[" + _BN_CONS + r"]?(?:\.[০-৯]+)?)[" + _BN_DANDA + r"]\]?\s*"
)

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging(verbose: bool) -> logging.Logger:
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
    return logging.getLogger("bdlaw")


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class SectionItem:
    heading: str
    url: str
    html: Tag
    number: Optional[str] = None


@dataclass
class ChapterItem:
    name: str
    url: str = "#"

@dataclass
class SubChapterItem:
    """Sub-chapter letter-head from <p class="act-section-head">, e.g. 'A.-Classes of Criminal Courts'."""
    letter: str          # "A", "B", etc.
    subtitle: str        # "Classes of Criminal Courts", "Territorial Divisions", etc.
    parent_number: str   # last section number seen before this head, e.g. "6"
    url: str = "#"

@dataclass
class PartItem:
    name: str
    url: str


@dataclass
class PreambleItem:
    """Act preamble / WHEREAS clause, captured from div.preamble-content."""
    text: str


@dataclass
class FootnotesItem:
    """Footnote list from div.footnoteListAll, appended at end of document."""
    notes: list[tuple[int, str]]


# Union type for document tree nodes
DocNode = PartItem | ChapterItem | SubChapterItem | SectionItem | PreambleItem | FootnotesItem


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

class Scraper:
    """Thin wrapper around requests.Session with retry & rate-limiting."""

    def __init__(
        self,
        delay: float = 0.3,
        max_retries: int = 4,
        timeout: int = 30,
        cache_dir: Optional[Path] = None,
    ) -> None:
        self.delay = delay
        self.timeout = timeout
        self.cache_dir = cache_dir
        self._last_request = 0.0

        self.session = requests.Session()
        self.session.headers.update(HEADERS)

        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)

        # Attach retry logic via tenacity
        self._fetch_raw = retry(
            retry=retry_if_exception_type(requests.RequestException),
            stop=stop_after_attempt(max_retries),
            wait=wait_exponential(multiplier=1, min=2, max=30),
            before_sleep=before_sleep_log(logging.getLogger("bdlaw"), logging.WARNING),
            reraise=True,
        )(self._fetch_raw_inner)

    def _fetch_raw_inner(self, url: str) -> str:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        self._last_request = time.monotonic()
        return resp.text

    def fetch(self, url: str) -> BeautifulSoup:
        """Fetch *url*, honouring cache if configured."""
        if self.cache_dir:
            cache_key = re.sub(r"[^\w]", "_", url) + ".html"
            cache_file = self.cache_dir / cache_key
            if cache_file.exists():
                logging.getLogger("bdlaw").debug("Cache hit: %s", url)
                html = cache_file.read_text(encoding="utf-8")
                return BeautifulSoup(html, "lxml")
        html = self._fetch_raw(url)
        if self.cache_dir:
            cache_file.write_text(html, encoding="utf-8")  # type: ignore[possibly-undefined]
        return BeautifulSoup(html, "lxml")


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _normalise(text: str) -> str:
    """Unicode NFC + collapse whitespace."""
    return unicodedata.normalize("NFC", text)


def _get_text(tag: Tag) -> str:
    return _normalise(tag.get_text(separator="\n"))


def _bn_digits_to_int(s: str) -> Optional[int]:
    """Convert a string of Bengali digits (or mixed) to int, or None on failure."""
    try:
        return int(s.translate(BENGALI_DIGIT_MAP))
    except (ValueError, AttributeError):
        return None


def extract_section_number(raw_text: str) -> Optional[str]:
    """
    Return the section number from raw section body text, or None.
    Handles:
      Latin:
        "54A. text"       — plain number + space
        "[54A.] text"     — bracketed (inserted provision)
        "140.(1) text"    — number immediately followed by clause marker
      Bengali:
        "১৷ text"         — Bengali digit + danda + space
        "২। text"         — danda variant
        "[১৷] text"       — bracketed Bengali
        "৪৷(ক) text"      — Bengali digit + danda + clause marker
    """
    # 1. Drop lines that are only a bare digit / Bengali digit (stray footnote refs).
    cleaned = "\n".join(
        line for line in raw_text.splitlines()
        if not re.fullmatch(r"\s*[\d০-৯]+\s*", line)
    )
    stripped = cleaned.lstrip()

    # 2. Unwrap outer [ ] for inserted/amended provisions.
    if stripped.startswith("[") and stripped.endswith("]"):
        stripped = stripped[1:-1].lstrip()

    # 3. Bengali patterns first (more specific), then Latin.
    for pat in (
        RE_BN_SECTION_NUM_BRACKET,
        RE_BN_SECTION_NUM_PLAIN,
        RE_BN_SECTION_NUM_CLAUSE,
        RE_SECTION_NUM_BRACKET,
        RE_SECTION_NUM_PLAIN,
        RE_SECTION_NUM_CLAUSE,
    ):
        m = pat.match(stripped)
        if m:
            return m.group(1)
    return None


def parse_footnotes(soup: BeautifulSoup) -> Optional["FootnotesItem"]:
    """
    Extract numbered footnotes from div.footnoteListAll.
    Each <li class="footnoteList"> contains <h6><sup>N</sup></h6> + text.
    Returns None if no footnotes found.
    """
    container = soup.find("div", class_="footnoteListAll")
    if not container:
        return None
    notes: list[tuple[int, str]] = []
    for li in container.find_all("li", class_="footnoteList"):
        sup = li.find("sup")
        if not sup:
            continue
        try:
            num = int(sup.get_text(strip=True))
        except ValueError:
            continue
        h6 = li.find("h6")
        parts: list[str] = []
        for node in li.children:
            if hasattr(node, "name") and node.name == "h6":
                continue
            text = node.get_text() if hasattr(node, "get_text") else str(node)
            parts.append(text)
        text = _normalise(" ".join(" ".join(parts).split()))
        if text:
            notes.append((num, text))
    return FootnotesItem(notes=notes) if notes else None


def parse_toc(soup: BeautifulSoup, base: str) -> dict[str, str]:
    """
    Parse the normal (ToC) page and return {normalised_heading: url}.
    """
    container = soup.find("section", class_="search-here")
    if not container:
        raise ValueError("ToC page: cannot find '.search-here' container")

    section_map: dict[str, str] = {}
    for p in container.find_all("p", class_="act-section-name"):
        a = p.find("a")
        if a and a.get("href"):
            heading = _normalise(" ".join(a.get_text(separator=" ", strip=True).split()))
            url = urljoin(base, a["href"])
            section_map[heading] = url
    return section_map


def parse_act_title(soup: BeautifulSoup) -> str:
    """
    Extract the act title from the detail page <h3> inside .bg-act-section.
    Falls back to an empty string if the element is not found.
    """
    section = soup.find("section", class_="bg-act-section")
    if section:
        h3 = section.find("h3")
        if h3:
            return _normalise(" ".join(h3.get_text().split()))
    return ""


# Regex: strip annotation brackets [text] → text, but leave [* * *] intact
_RE_ANNOTATION_BRACKETS = re.compile(r"\[(?!\*\s*\*)(.*?)\]", re.DOTALL)


def parse_preamble(soup: BeautifulSoup) -> str:
    """
    Extract and clean the act preamble from div.preamble-content.
    """
    preamble_div = soup.find("div", class_="preamble-content")

    if not preamble_div:
        for sec in soup.find_all("section", class_="padding-bottom-20"):
            classes = sec.get("class", [])
            if "bg-striped" not in classes and "bg-act-section" not in classes:
                preamble_div = sec
                break

    if not preamble_div:
        return ""

    raw = preamble_div.get_text(separator="\n")

    raw = RE_FOOTNOTE_INLINE.sub("", raw)
    raw = re.sub(r"[^\S\n]+", " ", raw)
    raw = "\n".join(l.strip() for l in raw.splitlines())
    raw = "\n".join(
        l for l in raw.splitlines()
        if l and not re.fullmatch(r"[\d০-৯]+", l)
    )

    for marker in ("\nCONTENTS", "\nSECTIONS"):
        idx = raw.find(marker)
        if idx != -1:
            raw = raw[:idx]

    raw = _RE_ANNOTATION_BRACKETS.sub(r"\1", raw)

    lines = raw.splitlines()
    joined: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        j = i + 1
        while j < len(lines) and not lines[j]:
            j += 1
        next_line = lines[j] if j < len(lines) else ""
        is_continuation = bool(next_line) and (
            next_line[0] in ";,)"
            or (not re.search(r"[.;:!?]$", line) and next_line[0].islower())
        )
        if line and is_continuation:
            sep = "" if next_line[0] in ";,)" else " "
            joined.append(line + sep + next_line)
            i = j + 1
        else:
            joined.append(line)
            i += 1

    raw = re.sub(r"\n{3,}", "\n\n", "\n".join(joined)).strip()

    if raw.startswith("[") and "]" not in raw.split("\n")[0]:
        raw = raw[1:].lstrip()

    return raw


def parse_detail(soup: BeautifulSoup, base: str) -> Generator[DocNode, None, None]:
    """
    Walk the detail (print) page and yield typed DocNodes in document order.
    """
    container = soup.find("section", class_="padding-bottom-20 bg-striped")
    if not container:
        raise ValueError("Detail page: cannot find main container")

    last_section_num: str = ""

    for row in container.find_all("div", class_="row", recursive=False):
        part_group = row.find("div", class_="act-part-group")
        if part_group:
            no_p   = part_group.find("p", class_="act-part-no")
            link   = no_p.find("a") if no_p else part_group.find("a")
            if link and link.get("href"):
                part_num = _normalise(link.get_text(strip=True))
                part_url = urljoin(base, link["href"])
                name_p   = part_group.find("p", class_="act-part-name")
                subtitle = _normalise(name_p.get_text(strip=True)) if name_p else ""
                full_name = f"{part_num}: {subtitle}" if subtitle else part_num
                yield PartItem(name=full_name, url=part_url)

        chapter_group = row.find("div", class_="act-chapter-group")
        if chapter_group:
            chap_no_p   = row.find("p", class_="act-chapter-no")
            chap_name_p = row.find("p", class_="act-chapter-name")
            if chap_no_p:
                chap_no   = _normalise(chap_no_p.get_text(strip=True))
                chap_name = _normalise(chap_name_p.get_text(strip=True)) if chap_name_p else ""
                full_chap = f"{chap_no}: {chap_name}" if chap_name else chap_no
                yield ChapterItem(name=full_chap)

        for sec_head in row.find_all("p", class_="act-section-head"):
            raw = _normalise(
                " ".join(sec_head.get_text(separator=" ", strip=True).split())
            )
            m = RE_SUBCHAPTER_HEAD.match(raw)
            if m:
                yield SubChapterItem(
                    letter=m.group(1),
                    subtitle=m.group(2).strip(),
                    parent_number=last_section_num,
                )
            elif raw:
                yield SubChapterItem(letter="", subtitle=raw, parent_number=last_section_num)

        head_div    = row.find("div", class_="txt-head")
        details_div = row.find("div", class_="txt-details")

        if details_div:
            if head_div:
                heading = _normalise(
                    " ".join(head_div.get_text(separator=" ", strip=True).split())
                )
            else:
                heading = ""
            raw_text = _get_text(details_div)
            number   = extract_section_number(raw_text)
            if number:
                last_section_num = number
            if not _is_boilerplate(raw_text):
                yield SectionItem(heading=heading, url="#", html=details_div, number=number)


def resolve_urls(
    nodes: list[DocNode],
    toc_map: dict[str, str],
    base: str,
) -> list[DocNode]:
    """
    Fill in real URLs for SectionItems and ChapterItems using the ToC map.
    """
    lower_map = {k.lower(): v for k, v in toc_map.items()}

    for node in nodes:
        if isinstance(node, SectionItem) and node.url == "#":
            key = node.heading.lower()
            if key in lower_map:
                node.url = lower_map[key]
            else:
                for k, u in lower_map.items():
                    if k.endswith(key) or key in k:
                        node.url = u
                        break
    return nodes


# ---------------------------------------------------------------------------
# Content cleaning & conversion
# ---------------------------------------------------------------------------

def _strip_footnotes(text: str) -> str:
    """Remove inline footnote markers like [1], [**], ^note^."""
    return RE_FOOTNOTE_INLINE.sub("", text)


def _strip_leading_section_num(text: str) -> str:
    """
    Remove leading section number from body text. Handles:
      Latin:
        "54A. rest"    → "rest"
        "[54A.] rest"  → "rest"
        "140.(1) rest" → "(1) rest"
      Bengali:
        "১৷ rest"      → "rest"
        "৪৷(ক) rest"   → "(ক) rest"
        "[১৷] rest"    → "rest"
    """
    stripped = text.lstrip()

    # Bengali: bracketed, plain, then clause-style
    for pat in (RE_BN_SECTION_NUM_BRACKET, RE_BN_SECTION_NUM_PLAIN):
        m = pat.match(stripped)
        if m:
            return text[text.index(stripped) + m.end():]
    m = RE_BN_SECTION_NUM_CLAUSE.match(stripped)
    if m:
        # Strip "১৷", leave "(ক)" for list converter
        dot_pos = next(
            i for i, c in enumerate(stripped) if c in _BN_DANDA
        )
        return text[text.index(stripped) + dot_pos + 1:]

    # Latin: plain, bracketed, clause-style
    for pat in (RE_SECTION_NUM_BRACKET, RE_SECTION_NUM_PLAIN):
        m = pat.match(stripped)
        if m:
            return text[text.index(stripped) + m.end():]
    m = RE_SECTION_NUM_CLAUSE.match(stripped)
    if m:
        dot_pos = stripped.index(".")
        return text[text.index(stripped) + dot_pos + 1:]

    return text


def _strip_outer_brackets(text: str) -> str:
    """Remove surrounding [ ] that BDLaw sometimes wraps entire sections in."""
    text = text.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1].strip()
    return text


# Isolated label regex — includes Bengali consonant markers and Bengali digits
_RE_ISOLATED_LABEL = re.compile(
    r"^\(([a-zA-Z0-9" + _BN_CONS + _BN_VOWELS + r"০-৯]+)\)\s*$"
)


def _join_isolated_labels(text: str) -> str:
    """
    Join an isolated list label "(N)" / "(a)" / "(ক)" that sits on its own
    line with the next non-blank line.
    """
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        if _RE_ISOLATED_LABEL.match(lines[i].strip()):
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines):
                out.append(lines[i].strip() + " " + lines[j].strip())
                i = j + 1
                continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def _join_continuation_lines(text: str) -> str:
    """
    Join a line with the next when the next starts with continuation
    punctuation (; , )) or a lowercase letter mid-sentence.
    """
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        next_line = lines[j].strip() if j < len(lines) else ""
        if line.strip() and next_line:
            if next_line[0] in ";,)":
                out.append(line.rstrip() + next_line)
                i = j + 1
                continue
            if (not re.search(r"[.;:!?]$", line.strip())
                    and next_line[0].islower()):
                out.append(line.rstrip() + " " + next_line)
                i = j + 1
                continue
        out.append(line)
        i += 1
    return "\n".join(out)


# Patterns for boilerplate content that should be dropped entirely
_RE_BOILERPLATE_LINE = re.compile(
    r"^\s*"
    r"(CONTENTS|SECTIONS|SCHEDULE|PREAMBLE|WHEREAS|An Act to |It is hereby|"
    r"https?://\S+)",
    re.IGNORECASE,
)
_RE_URL_ONLY   = re.compile(r"^\s*https?://\S+\s*$")
_RE_TOC_LINK   = re.compile(r"^\s*\[.+?\]\(https?://")
RE_SUBCHAPTER_HEAD = re.compile(r"^([A-Z])\.\s*-\s*(.+)$")
_RE_SUBST_LETTER = re.compile(r"\[([A-Z])\]")


def _table_to_markdown(table: Tag) -> str:
    """Convert HTML <table> → GFM Markdown table string."""
    rows: list[list[str]] = []
    for tr in table.find_all("tr"):
        cells = [
            " ".join(td.get_text(separator=" ", strip=True).split())
            for td in tr.find_all(["td", "th"])
        ]
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    col_count = max(len(r) for r in rows)
    rows = [r + [""] * (col_count - len(r)) for r in rows]
    sep = ["---"] * col_count
    lines = [
        "| " + " | ".join(rows[0]) + " |",
        "| " + " | ".join(sep)     + " |",
    ] + ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def _is_boilerplate(raw: str) -> bool:
    """
    Return True if the text consists entirely (or almost entirely) of
    boilerplate: act preamble, WHEREAS clauses, CONTENTS headers,
    or bare ToC links.
    """
    lines = [l for l in raw.splitlines() if l.strip()]
    if not lines:
        return True
    boilerplate_count = sum(
        1 for l in lines
        if _RE_BOILERPLATE_LINE.search(l) or _RE_URL_ONLY.match(l) or _RE_TOC_LINK.match(l)
    )
    return boilerplate_count / len(lines) > 0.6


def _filter_stray_lines(text: str) -> str:
    """
    Remove individual lines that are clearly not legal content:
    bare URLs, TOC link lines, and CONTENTS/SECTIONS headers.
    """
    kept = []
    for line in text.splitlines():
        if _RE_URL_ONLY.match(line):
            continue
        if _RE_TOC_LINK.match(line):
            continue
        if re.match(r"^\s*(CONTENTS|SECTIONS)\s*$", line, re.IGNORECASE):
            continue
        kept.append(line)
    return "\n".join(kept)


def _classify_item(line: str) -> Optional[str]:
    """
    Return 'alpha', 'roman', 'decimal', 'bn_alpha', 'bn_decimal', or None.

    Bengali:
      (ক)(খ)(গ) … → 'bn_alpha'   (rendered as lower-alpha <ol>)
      (১)(২)(৩) … → 'bn_decimal' (rendered as 1. 2. 3.)

    Latin (unchanged):
      Decimal  (1)(2)(3) → 'decimal'
      Multi-char roman (ii)(iii) → 'roman'
      Single 'i' → 'roman'
      Any other single letter → 'alpha'
    """
    s = line.strip()

    # Bengali decimal: (১), (২), (৩) …
    if RE_BN_DECIMAL_ITEM.match(s):
        return "bn_decimal"

    # Bengali alpha: (ক), (খ), (গ), (কক) …
    if RE_BN_ALPHA_ITEM.match(s):
        return "bn_alpha"

    # Bengali vowel sub-list: (অ), (আ), (ই), (ঈ), (উ), (ঊ) …
    # Always nested inside a parent (ক)(খ) item — treated as continuation
    # during parent collection, then resolved by recursive convert_lists.
    if RE_BN_VOWEL_ITEM.match(s):
        return "bn_vowel"

    # Latin decimal
    if RE_DECIMAL_ITEM.match(s):
        return "decimal"

    # Multi-char roman: ii, iii, iv, vi …
    m_roman = RE_ROMAN_ITEM.match(s)
    if m_roman and len(m_roman.group(1)) > 1:
        return "roman"

    # Single-character Latin label
    m_alpha = RE_ALPHA_ITEM.match(s)
    if m_alpha:
        label = m_alpha.group(1).lower()
        return "roman" if label == "i" else "alpha"

    return None


def _extract_item_content(line: str) -> str:
    """Strip the leading (x) label from a list-item line (Latin or Bengali)."""
    s = line.strip()
    for pat in (
        RE_ALPHA_ITEM,
        RE_ROMAN_ITEM,
        RE_DECIMAL_ITEM,
        RE_BN_ALPHA_ITEM,
        RE_BN_DECIMAL_ITEM,
        RE_BN_VOWEL_ITEM,
    ):
        m = pat.match(s)
        if m:
            return m.group(2).strip()
    return s


def _get_decimal_num(line: str) -> Optional[int]:
    """
    Extract the integer value from a decimal list-item line.
    Handles both Latin (1)(2) and Bengali (১)(২).
    Returns None if not a decimal item.
    """
    s = line.strip()
    m = RE_DECIMAL_ITEM.match(s)
    if m:
        return int(m.group(1))
    m = RE_BN_DECIMAL_ITEM.match(s)
    if m:
        return _bn_digits_to_int(m.group(1))
    return None


def convert_lists(text: str) -> str:
    """
    Group consecutive list items of the same type into proper Markdown/HTML.

    Rules:
      - (a)(b)…      → <ol style="list-style-type: lower-alpha;">
      - (i)(ii)…     → <ol style="list-style-type: lower-roman;">
      - (1)(2)…      → Markdown  1. 2. …
      - (ক)(খ)(গ)…   → <ol style="list-style-type: lower-alpha;">
      - (১)(২)(৩)…   → Markdown  1. 2. … (Bengali digits converted to Arabic)
      - Single isolated alpha/roman/bn_alpha items → plain text (no wrapper)
      - Single decimal / bn_decimal items → always rendered as N. (clause ref)
    """
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    n = len(lines)

    while i < n:
        lt = _classify_item(lines[i])
        if lt is None:
            out.append(lines[i].rstrip())
            i += 1
            continue

        # Collect all items of *this* list type, tolerating blank lines
        items: list[list[str]] = []
        while i < n:
            if _classify_item(lines[i]) != lt and lines[i].strip():
                break
            if not lines[i].strip():
                i += 1
                continue

            item_lines = [lines[i]]
            i += 1
            while i < n:
                next_lt = _classify_item(lines[i])
                if next_lt == lt:
                    break
                if next_lt is not None and next_lt != "bn_vowel":
                    break  # bn_vowel items are sub-items; absorb as continuation
                if lines[i].strip():
                    item_lines.append(lines[i])
                    i += 1
                else:
                    j = i + 1
                    while j < n and not lines[j].strip():
                        j += 1
                    if j < n and _classify_item(lines[j]) == lt:
                        break
                    elif j < n and not _classify_item(lines[j]):
                        item_lines.append("")
                        i += 1
                    else:
                        break
            items.append(item_lines)

        if not items:
            continue

        if lt in ("decimal", "bn_decimal"):
            # Always render with actual label numbers; Bengali digits → Arabic.
            for item_lines in items:
                num = _get_decimal_num(item_lines[0])
                content = " ".join(
                    [_extract_item_content(item_lines[0])]
                    + [l.strip() for l in item_lines[1:] if l.strip()]
                )
                out.append(f"{num}. {content}" if num is not None else content)

        elif lt in ("alpha", "roman", "bn_alpha", "bn_vowel"):
            if len(items) == 1:
                # Single item — emit as plain prose, no list wrapper.
                first = _extract_item_content(items[0][0])
                rest = items[0][1:]
                out.append("\n".join([first] + rest).strip())
            else:
                style = (
                    "lower-roman" if lt == "roman" else "lower-alpha"
                )
                html_parts = [f'<ol style="list-style-type: {style};">']
                for item_lines in items:
                    content_lines = [_extract_item_content(item_lines[0])] + [
                        l for l in item_lines[1:]
                    ]
                    # Recursively convert any bn_vowel sub-lists absorbed
                    # as continuation into proper nested <ol> elements.
                    content = convert_lists("\n".join(content_lines).strip())
                    html_parts.append(f"<li>{content}</li>")
                html_parts.append("</ol>")
                out.append("".join(html_parts))

    return "\n".join(out)


def html_to_markdown(details_div: Tag) -> str:
    """Full pipeline: HTML tag → clean Markdown string."""

    for table in details_div.find_all("table"):
        table.replace_with(_table_to_markdown(table) + "\n")

    raw = _get_text(details_div)

    # 1. Remove inline footnote markers e.g. [1], [**]
    raw = _strip_footnotes(raw)

    # 2. Collapse horizontal whitespace (preserve newlines)
    raw = re.sub(r"[^\S\n]+", " ", raw)

    # 3. Remove lines that are *only* bare integers / Bengali digits
    raw = "\n".join(
        line for line in raw.splitlines()
        if not re.fullmatch(r"\s*[\d০-৯]+\s*", line)
    )

    # 4. Remove surrounding [ ] brackets BDLaw adds to inserted provisions
    raw = _strip_outer_brackets(raw)

    # 5. Strip leading section number from body
    raw = _strip_leading_section_num(raw)

    # 5b. Remove stray URL-only lines and ToC link lines
    raw = _filter_stray_lines(raw)

    # 5c. Strip single-letter substitution markers [A], [B] … then general
    #     annotation brackets.
    raw = _RE_SUBST_LETTER.sub(r"\1", raw)
    raw = _RE_ANNOTATION_BRACKETS.sub(r"\1", raw)

    # 5d. Strip orphan leading ". " before a clause marker.
    raw = re.sub(r"^\.\s+(?=\()", "", raw.lstrip())

    # 5e. Join isolated clause labels with their following content line.
    raw = _join_isolated_labels(raw)

    # 6. Convert list items to proper Markdown / HTML
    raw = convert_lists(raw)

    # 7. Collapse excess blank lines
    raw = RE_MULTI_NEWLINE.sub("\n\n", raw)

    return raw.strip()


# ---------------------------------------------------------------------------
# Markdown builder
# ---------------------------------------------------------------------------

def build_markdown(
    nodes: list[DocNode],
    act_number: str,
    act_title: str = "",
    act_url: str = "",
) -> str:
    lines: list[str] = []
    if act_title:
        url_part = f"({act_url})" if act_url else ""
        lines.append(f"# [{act_title}]{url_part}")
        lines.append("")

    for node in nodes:
        if isinstance(node, PreambleItem):
            lines.append("")
            lines.append(node.text)
            lines.append("")
        elif isinstance(node, FootnotesItem):
            lines.append("\n## Footnotes\n")
            for num, text in node.notes:
                lines.append(f"{num}. {text}")
            lines.append("")
        elif isinstance(node, PartItem):
            lines.append(f"\n## [{node.name}]({node.url})\n")
        elif isinstance(node, ChapterItem):
            lines.append(f"\n### [{node.name}]({node.url})\n")
        elif isinstance(node, SubChapterItem):
            if node.letter and node.parent_number:
                heading_text = f"Section {node.parent_number}{node.letter}: {node.subtitle}"
            else:
                heading_text = node.subtitle
            lines.append(f"\n#### [{heading_text}]({node.url})\n")
        elif isinstance(node, SectionItem):
            if node.number:
                term = "Article" if "/Article-" in node.url else "Section"
                heading_text = f"{term} {node.number}: {node.heading}"
                is_inserted = any(c.isalpha() for c in node.number)
                level = "######" if is_inserted else "#####"
            else:
                heading_text = node.heading
                level = "#####"
            lines.append(f"\n{level} [{heading_text}]({node.url})\n")
            lines.append(html_to_markdown(node.html))
            lines.append("")

    return _final_cleanup("\n".join(lines))


def _final_cleanup(md: str) -> str:
    """Remove trailing spaces and ensure single trailing newline."""
    cleaned = "\n".join(line.rstrip() for line in md.split("\n"))
    return RE_MULTI_NEWLINE.sub("\n\n", cleaned).strip() + "\n"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def scrape_act(
    act_number: str,
    output: Path,
    delay: float,
    max_retries: int,
    cache_dir: Optional[Path],
    log: logging.Logger,
) -> None:
    toc_url = f"{BASE_URL}/act-{act_number}.html"
    detail_url = f"{BASE_URL}/act-print-{act_number}.html"

    scraper = Scraper(delay=delay, max_retries=max_retries, cache_dir=cache_dir)

    log.info("Fetching ToC page: %s", toc_url)
    toc_soup = scraper.fetch(toc_url)
    toc_map = parse_toc(toc_soup, BASE_URL)
    log.info("Found %d section links in ToC", len(toc_map))

    log.info("Fetching detail page: %s", detail_url)
    detail_soup = scraper.fetch(detail_url)

    log.info("Parsing document structure …")

    if _RICH:
        nodes: list[DocNode] = []
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Parsing nodes …", total=None)
            for node in parse_detail(detail_soup, BASE_URL):
                nodes.append(node)
                if isinstance(node, SectionItem):
                    progress.advance(task)
            progress.update(task, total=len(nodes), completed=len(nodes))
    else:
        nodes = list(parse_detail(detail_soup, BASE_URL))

    section_count = sum(1 for n in nodes if isinstance(n, SectionItem))
    log.info(
        "Parsed %d nodes (%d sections, %d chapters, %d parts)",
        len(nodes),
        section_count,
        sum(1 for n in nodes if isinstance(n, ChapterItem)),
        sum(1 for n in nodes if isinstance(n, PartItem)),
    )

    log.info("Resolving section URLs …")
    resolve_urls(nodes, toc_map, BASE_URL)

    log.info("Building Markdown …")
    act_title = parse_act_title(detail_soup)
    act_url   = f"{BASE_URL}/act-{act_number}.html"
    if act_title:
        log.info("Act title: %s", act_title)
    preamble = parse_preamble(detail_soup) or parse_preamble(toc_soup)
    if preamble:
        log.info("Preamble found (%d chars)", len(preamble))
        nodes.insert(0, PreambleItem(text=preamble))
    footnotes = parse_footnotes(detail_soup)
    if footnotes:
        log.info("Found %d footnotes", len(footnotes.notes))
        nodes.append(footnotes)
    md = build_markdown(nodes, act_number, act_title=act_title, act_url=act_url)

    output.write_text(md, encoding="utf-8")
    log.info("✅  Saved → %s  (%d chars)", output, len(md))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option(
    "--act", "act_number",
    required=True,
    help="Act number from bdlaws.minlaw.gov.bd (e.g. 1710 for CrPC).",
)
@click.option(
    "--output", "-o",
    default=None,
    help="Output Markdown file path. Defaults to act-<number>.md.",
)
@click.option(
    "--delay", "-d",
    default=0.3,
    show_default=True,
    type=float,
    help="Minimum seconds between HTTP requests (rate limiting).",
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
    help="Directory to cache raw HTML pages. Skips fetch on subsequent runs.",
)
@click.option(
    "--verbose", "-v",
    is_flag=True,
    default=False,
    help="Enable DEBUG-level logging.",
)
def main(
    act_number: str,
    output: Optional[str],
    delay: float,
    retries: int,
    cache_dir: Optional[str],
    verbose: bool,
) -> None:
    """Scrape a Bangladesh law act and save it as a clean Markdown file."""
    log = setup_logging(verbose)

    output_path = Path(output) if output else Path(f"act-{act_number}.md")
    cache_path = Path(cache_dir) if cache_dir else None

    try:
        scrape_act(
            act_number=act_number,
            output=output_path,
            delay=delay,
            max_retries=retries,
            cache_dir=cache_path,
            log=log,
        )
    except requests.HTTPError as e:
        log.error("HTTP error fetching page: %s", e)
        sys.exit(1)
    except requests.ConnectionError as e:
        log.error("Network connection error: %s", e)
        sys.exit(1)
    except ValueError as e:
        log.error("Parsing error: %s", e)
        sys.exit(1)
    except KeyboardInterrupt:
        log.warning("Interrupted by user.")
        sys.exit(130)


if __name__ == "__main__":
    main()