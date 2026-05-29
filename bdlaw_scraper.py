#!/usr/bin/env python3
"""
Bangladesh Law Scraper v3.6
============================
Changes vs v3.5:
  - convert_lists: proper depth-based nesting (decimal->alpha->roman = 1->2->3)
  - _classify_item / _extract_item_content: strip leading [^N] before matching
    so "[^7](a) text" is correctly classified as alpha depth-2
  - Trailing stray ] cleanup at end of section bodies
  - _convert_numbered_callout runs convert_lists on item content (handles sub-lists)
"""

from __future__ import annotations

import logging
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, Optional
from urllib.parse import urljoin

import click
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup, NavigableString, Tag
from tenacity import (
    before_sleep_log, retry, retry_if_exception_type,
    stop_after_attempt, wait_exponential,
)

try:
    from rich.console import Console
    from rich.logging import RichHandler
    from rich.progress import (
        BarColumn, MofNCompleteColumn, Progress, SpinnerColumn,
        TextColumn, TimeElapsedColumn,
    )
    _RICH = True
    console = Console(stderr=True)
except ImportError:
    _RICH = False
    console = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "http://bdlaws.minlaw.gov.bd"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; BDLawScraper/3.6; "
        "+https://github.com/yourname/bdlaw-scraper)"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

BENGALI_DIGIT_MAP = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
_BN_LETTER_TO_LATIN: dict[str, str] = {
    'ক': 'a', 'খ': 'b', 'গ': 'c', 'ঘ': 'd', 'ঙ': 'e',
    'চ': 'f', 'ছ': 'g', 'জ': 'h', 'ঝ': 'i', 'ঞ': 'j',
    'ট': 'k', 'ঠ': 'l', 'ড': 'm', 'ঢ': 'n', 'ণ': 'o',
    'ত': 'p', 'থ': 'q', 'দ': 'r', 'ধ': 's', 'ন': 't',
}
_BN_CONS   = "কখগঘঙচছজঝঞটঠডঢণতথদধনপফবভমযরলশষসহড়ঢ়য়ৎ"
_BN_VOWELS = "অআইঈউঊঋএঐওঔ"
_BN_DANDA  = "৷।॥"

_BLOCK_ELEMENTS = {
    "p", "div", "li", "tr", "td", "th",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "section", "article", "ul", "ol",
}

# List type -> nesting depth
_LIST_DEPTH: dict[str, int] = {
    "decimal":    1,  # (1)(2)(3)  — top level
    "bn_alpha":   1,  # (ক)(খ)(গ)  — top level (Bengali acts)
    "alpha":      2,  # (a)(b)(c)  — sub-items
    "bn_decimal": 2,  # (১)(২)(৩)  — sub-items (Bengali acts)
    "roman":      3,  # (i)(ii)    — sub-sub-items
    "bn_vowel":   3,  # (অ)(আ)     — sub-sub-items (Bengali acts)
}
_LIST_INDENT = 3  # spaces per depth level

# Words that cannot end a sentence — used to detect mid-sentence HTML line breaks
_CANNOT_END_SENTENCE_WORDS = frozenset({
    'a', 'an', 'the', 'of', 'for', 'in', 'on', 'at', 'to', 'by', 'with',
    'from', 'into', 'upon', 'about', 'under', 'over', 'after', 'before',
    'between', 'is', 'are', 'was', 'were', 'be', 'been', 'has', 'have',
    'had', 'will', 'shall', 'may', 'can', 'must', 'not', 'and', 'or',
    'but', 'that', 'which', 'who', 'if', 'as', 'than', 'whether',
    'setting', 'including', 'being', 'having', 'making', 'following',
    'providing', 'subject', 'pursuant',
})

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

RE_MULTI_NEWLINE = re.compile(r"\n{3,}")

RE_ALPHA_ITEM      = re.compile(r"^\(([a-zA-Z])\)\s+(.*)",           re.DOTALL)
RE_ROMAN_ITEM      = re.compile(r"^\(([ivxlcdmIVXLCDM]+)\)\s+(.*)", re.DOTALL)
RE_DECIMAL_ITEM    = re.compile(r"^\((\d+)\)\s+(.*)",                re.DOTALL)
RE_BN_ALPHA_ITEM   = re.compile(r"^\(([" + _BN_CONS  + r"]+)\)\s+(.*)", re.DOTALL)
RE_BN_DECIMAL_ITEM = re.compile(r"^\(([০-৯]+)\)\s+(.*)",             re.DOTALL)
RE_BN_VOWEL_ITEM   = re.compile(r"^\(([" + _BN_VOWELS + r"])\)\s+(.*)", re.DOTALL)

_SECNUM_CORE = r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)?(?:\.[A-Za-z0-9]+)?"
RE_SECTION_NUM_BRACKET = re.compile(rf"^\[({_SECNUM_CORE})\s*\.\s*\]?\s*")
RE_SECTION_NUM_PLAIN   = re.compile(rf"^({_SECNUM_CORE})\.(?:\s+|$)")
RE_SECTION_NUM_CLAUSE  = re.compile(rf"^({_SECNUM_CORE})\.[\(\[]([A-Za-z0-9]+)[\)\]]")
RE_BN_SECTION_NUM_PLAIN   = re.compile(r"^([০-৯]+[" + _BN_CONS + r"]?(?:\.[০-৯]+)?)[" + _BN_DANDA + r"]\s+")
RE_BN_SECTION_NUM_CLAUSE  = re.compile(r"^([০-৯]+[" + _BN_CONS + r"]?)[" + _BN_DANDA + r"][\(\[]([০-৯" + _BN_CONS + r"]+)[\)\]]")
RE_BN_SECTION_NUM_BRACKET = re.compile(r"^\[([০-৯]+[" + _BN_CONS + r"]?(?:\.[০-৯]+)?)[" + _BN_DANDA + r"]\]?\s*")

RE_SUBCHAPTER_HEAD = re.compile(r"^([A-Z])\.\s*-\s*(.+)$")

_RE_EXPL_NUMBERED  = re.compile(r"^Explanation\s+([IVXLCDM]+|\d+)\s*[-\u2013.]+\s*(.*)",  re.IGNORECASE)
_RE_EXPL_SINGLE    = re.compile(r"^Explanation\s*[-\u2013.]+\s*(.*)",                       re.IGNORECASE)
_RE_EXCEP_NUMBERED = re.compile(r"^Exception[s]?\s+([IVXLCDM]+|\d+)\s*[-\u2013.\u2014]+\s*(.*)", re.IGNORECASE)
_RE_EXCEP_SINGLE   = re.compile(r"^Exception[s]?\s*[-\u2013.\u2014]+\s*(.*)",                     re.IGNORECASE)
_RE_ILLUSTRATIONS  = re.compile(r"^Illustrations?\s*[-\u2013.]?\s*$",                             re.IGNORECASE)
_RE_PROVIDED_STANDALONE = re.compile(
    r"^Provid(?:ed|e)\s*(?:further\s*)?(?:that\s*)?[-\u2013.:]+\s*$", re.IGNORECASE
)
_RE_ORDINAL_ITEM = re.compile(
    r"^(Firstly|Secondly|Thirdly|Fourthly|Fifthly"
    r"|Sixthly|Seventhly|Eighthly|Ninthly|Tenthly)\s*[-\u2013.,]+\s*(.*)",
    re.IGNORECASE,
)
_RE_CONNECTOR = re.compile(
    r"^\s*((?:,\s*)?(?:or|and)(?:\s+the)?|,\s*as\s+the\s+case\s+may\s+be[,.]?"
    r"|as\s+the\s+case\s+may\s+be[,.]?|,\s*namely[,:]?|,\s*that\s+is\s+to\s+say[,:]?)\s*$",
    re.IGNORECASE,
)

_RE_HEADING_FOOTNOTE    = re.compile(r"\[(\d{1,4})([^\d\]][^\]]*)\]")
_RE_ANNOTATION_BRACKETS = re.compile(r"(?<!\[)\[(?!\s*\*)(?!\^)(?!\[)([^\[\]]*)\](?!\])", re.DOTALL)
_RE_SUBST_LETTER        = re.compile(r"\[([A-Z])\]")
_RE_BOILERPLATE_LINE    = re.compile(
    r"^\s*(CONTENTS|SECTIONS|SCHEDULE|PREAMBLE|WHEREAS|An Act to |It is hereby|https?://\S+)",
    re.IGNORECASE,
)
_RE_URL_ONLY    = re.compile(r"^\s*https?://\S+\s*$")
_RE_TOC_LINK    = re.compile(r"^\s*\[.+?\]\(https?://")
_RE_ISOLATED_LABEL = re.compile(
    r"^\(([a-zA-Z0-9" + _BN_CONS + _BN_VOWELS + r"০-৯]+)\)\s*$"
)
_RE_ANNOTATION_BRACKETS_PREAMBLE = re.compile(r"\[(?!\*\s*\*)(.*?)\]", re.DOTALL)
_RE_BDLAWS_ACT   = re.compile(r"https?://bdlaws\.minlaw\.gov\.bd/act-(\d+)\.html")
_RE_FN_PREFIX    = re.compile(r"^(\[\^\d+\])")
_RE_FN_INLINE    = re.compile(r"^\[\^\d+\]\s*")   # for stripping before list-marker check
_RE_RESIDUAL_HTML = re.compile(r"</?(li|ol|ul|p|div|span|br)[^>]*>", re.IGNORECASE)

_MARGIN_CALLOUTS = [
    (re.compile(r"^Saving[s]?\s*[-\u2013.\u2014]+\s*(.*)",      re.I), "Note",    "Saving"),
    (re.compile(r"^Repeal[s]?\s*[-\u2013.\u2014]+\s*(.*)",      re.I), "Warning", "Repeal"),
    (re.compile(r"^Commencement\s*[-\u2013.\u2014]+\s*(.*)",    re.I), "Info",    "Commencement"),
    (re.compile(r"^Short\s+title\s*[-\u2013.\u2014]+\s*(.*)",   re.I), "Info",    "Short title"),
]

_RE_STATUS_OMITTED  = re.compile(r"^\[?[Oo]mitted\]?\.?\s*$")
_RE_STATUS_REPEALED = re.compile(r"^\[?[Rr]epealed\]?\.?\s*$")
_RE_PUNISHMENT = re.compile(
    r"(?<!\*)\b(shall be punished with|shall be punished|shall be liable|"
    r"shall be guilty of|imprisonment for life|rigorous imprisonment)\b(?!\*)",
    re.IGNORECASE,
)
_RE_OMITTED_INLINE  = re.compile(r"\b(Omitted by [^.;]*[.;])",  re.IGNORECASE)
_RE_REPEALED_INLINE = re.compile(r"\b(Repealed by [^.;]*[.;])", re.IGNORECASE)
_RE_SECTION_REF     = re.compile(r"\b(sections?)\s+(\d+[A-Za-z]?(?:-[A-Za-z0-9]+)?)\b", re.IGNORECASE)
_RE_DEFINITION      = re.compile(
    r'^(?:In this (?:Act|section|chapter|Part|Code)[,;]?\s+)?'
    r'"([^"]{1,80})"\s+(means?|includes?|denotes?)\s+(.+)',
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# YAML tag-inference
# ---------------------------------------------------------------------------

_TAG_KEYWORDS: dict[str, list[str]] = {
    "Criminal Law":       ["criminal", "penal code", "crime", "offence", "punishment",
                           "magistrate", "prosecution", "accusation", "culpable"],
    "Civil Law":          ["civil procedure", "civil court", "plaintiff", "defendant",
                           "decree", "execution of decree", "suit"],
    "Constitutional Law": ["constitution", "fundamental right", "parliament",
                           "republic", "citizenship", "sovereignty"],
    "Commercial Law":     ["commerce", "contract", "sale of goods", "company",
                           "partnership", "arbitration", "mercantile"],
    "Banking & Finance":  ["bank", "banking", "financial institution", "loan",
                           "credit", "monetary", "interest"],
    "Tax Law":            ["tax", "revenue", "customs", "excise", "vat",
                           "income tax", "duty", "tariff"],
    "Labour Law":         ["labour", "labor", "employment", "worker",
                           "factory", "wages", "trade union"],
    "Family Law":         ["marriage", "divorce", "matrimonial",
                           "guardian", "inheritance", "succession", "dower"],
    "Property Law":       ["property", "land", "tenancy", "mortgage",
                           "transfer of property", "registration"],
    "Evidence Law":       ["evidence", "witness", "proof", "admissibility"],
    "Procedural Law":     ["procedure code", "jurisdiction", "appeal",
                           "tribunal", "high court", "supreme court"],
    "Environmental Law":  ["environment", "pollution", "forest",
                           "wildlife", "biodiversity", "conservation"],
    "Education Law":      ["education", "university", "school",
                           "examination", "academic", "degree"],
    "Health Law":         ["health", "medical", "hospital", "drug",
                           "medicine", "pharmacy", "epidemic"],
    "Administrative Law": ["government", "administration", "ministry",
                           "officer", "public servant", "gazette"],
    "Military Law":       ["army", "navy", "air force", "military",
                           "armed forces", "defence"],
    "Security Law":       ["security", "national security", "intelligence",
                           "police", "special branch"],
    "Immigration Law":    ["passport", "visa", "immigration",
                           "citizenship", "nationality", "refugee"],
    "Insurance Law":      ["insurance", "insurer", "policy", "indemnity", "premium"],
    "Intellectual Property": ["copyright", "patent", "trademark",
                              "intellectual property", "design"],
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def setup_logging(verbose: bool) -> logging.Logger:
    level = logging.DEBUG if verbose else logging.INFO
    if _RICH:
        logging.basicConfig(
            level=level, format="%(message)s",
            handlers=[RichHandler(console=console, show_path=False, markup=True)],
        )
    else:
        logging.basicConfig(level=level,
                            format="%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%H:%M:%S")
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
    letter: str
    subtitle: str
    parent_number: str
    url: str = "#"


@dataclass
class PartItem:
    name: str
    url: str


@dataclass
class PreambleItem:
    text: str


@dataclass
class FootnotesItem:
    notes: list[tuple[int, str]]


DocNode = PartItem | ChapterItem | SubChapterItem | SectionItem | PreambleItem | FootnotesItem


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


class Scraper:
    def __init__(self, delay: float = 0.3, max_retries: int = 4,
                 timeout: int = 30, cache_dir: Optional[Path] = None) -> None:
        self.delay = delay
        self.timeout = timeout
        self.cache_dir = cache_dir
        self._last_request = 0.0
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)
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
        if self.cache_dir:
            cache_key = re.sub(r"[^\w]", "_", url) + ".html"
            cache_file = self.cache_dir / cache_key
            if cache_file.exists():
                logging.getLogger("bdlaw").debug("Cache hit: %s", url)
                return BeautifulSoup(cache_file.read_text(encoding="utf-8"), "lxml")
        html = self._fetch_raw(url)
        if self.cache_dir:
            cache_file.write_text(html, encoding="utf-8")  # type: ignore[possibly-undefined]
        return BeautifulSoup(html, "lxml")


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------


def _tag_to_md_text(node: object, scraper: Optional[Scraper] = None) -> str:
    if isinstance(node, NavigableString):
        return unicodedata.normalize("NFC", str(node))
    if not hasattr(node, "name") or not node.name:
        return ""
    if node.name == "br":
        return "\n"
    if node.name == "a":
        href: str = node.get("href", "")  # type: ignore[union-attr]
        inner = "".join(_tag_to_md_text(c, scraper) for c in node.children)  # type: ignore[union-attr]
        text  = " ".join(inner.split())
        if not text:
            return ""
        if href:
            full_href = href if href.startswith("http") else urljoin(BASE_URL, href)
            m = _RE_BDLAWS_ACT.match(full_href)
            if m:
                clean_text = re.sub(r'[\].]+$', '', text).strip()
                return f"[[act-{m.group(1)}|{clean_text}]]"
            if re.fullmatch(r"\d+", text):
                return f"[{text}]"
            return f"[{text}]({full_href})"
        return inner
    content = "".join(_tag_to_md_text(c, scraper) for c in node.children)  # type: ignore[union-attr]
    if node.name in _BLOCK_ELEMENTS:
        return "\n" + content + "\n"
    return content


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _normalise(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _get_text(tag: Tag) -> str:
    return _normalise(tag.get_text(separator="\n"))


def _bn_digits_to_int(s: str) -> Optional[int]:
    try:
        return int(s.translate(BENGALI_DIGIT_MAP))
    except (ValueError, AttributeError):
        return None


def _normalize_secnum(num: str) -> str:
    return re.sub(r"-([A-Za-z0-9]+)$", r"\1", num)


def _secnum_to_block_id(num: str) -> str:
    result = num.translate(BENGALI_DIGIT_MAP)
    result = "".join(_BN_LETTER_TO_LATIN.get(ch, ch) for ch in result)
    return re.sub(r"[^a-z0-9]", "", result.lower())


def extract_section_number(raw_text: str) -> Optional[str]:
    cleaned = "\n".join(
        line for line in raw_text.splitlines()
        if not re.fullmatch(r"\s*[\d\u09E6-\u09EF]+\s*", line)
    )
    cleaned = re.sub(r"\[\d+\]", "", cleaned)
    stripped = re.sub(r"^[.\s]+", "", cleaned.lstrip())
    if stripped.startswith("[") and stripped.endswith("]"):
        stripped = stripped[1:-1].lstrip()
    for pat in (
        RE_BN_SECTION_NUM_BRACKET, RE_BN_SECTION_NUM_PLAIN, RE_BN_SECTION_NUM_CLAUSE,
        RE_SECTION_NUM_BRACKET, RE_SECTION_NUM_PLAIN, RE_SECTION_NUM_CLAUSE,
    ):
        m = pat.match(stripped)
        if m:
            return _normalize_secnum(m.group(1))
    return None


def parse_footnotes(soup: BeautifulSoup) -> Optional[FootnotesItem]:
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
        parts: list[str] = []
        for node in li.children:
            if hasattr(node, "name") and node.name == "h6":
                continue
            parts.append(node.get_text() if hasattr(node, "get_text") else str(node))
        text = _normalise(" ".join(" ".join(parts).split()))
        if text:
            notes.append((num, text))
    return FootnotesItem(notes=notes) if notes else None


def parse_toc(soup: BeautifulSoup, base: str) -> dict[str, str]:
    container = soup.find("section", class_="search-here")
    if not container:
        raise ValueError("ToC page: cannot find '.search-here' container")
    section_map: dict[str, str] = {}
    for p in container.find_all("p", class_="act-section-name"):
        a = p.find("a")
        if a and a.get("href"):
            heading = _normalise(" ".join(a.get_text(separator=" ", strip=True).split()))
            section_map[heading] = urljoin(base, a["href"])
    return section_map


def parse_act_title(soup: BeautifulSoup) -> str:
    section = soup.find("section", class_="bg-act-section")
    if section:
        h3 = section.find("h3")
        if h3:
            raw = _normalise(" ".join(h3.get_text().split()))
            raw = _RE_HEADING_FOOTNOTE.sub(r"\2", raw)
            raw = raw.strip("[]").strip()
            raw = re.sub(r"^(\d+)([A-Z])", r"\2", raw)
            return raw
    return ""


def _extract_title_footnote_num(soup: BeautifulSoup) -> Optional[int]:
    section = soup.find("section", class_="bg-act-section")
    if section:
        h3 = section.find("h3")
        if h3:
            raw = _normalise(" ".join(h3.get_text().split()))
            m = _RE_HEADING_FOOTNOTE.search(raw)
            if m:
                return int(m.group(1))
            m2 = re.match(r"^(\d+)[A-Z]", raw.strip())
            if m2:
                return int(m2.group(1))
    return None


def _clean_heading(raw: str) -> str:
    h = _RE_HEADING_FOOTNOTE.sub(r"\2", raw).strip()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1].strip()
    h = re.sub(r"^\[\d+\]\s*", "", h)
    h = re.sub(r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)?(?:\.[A-Za-z0-9]+)?\.\s+", "", h)
    return h.strip()


def parse_preamble(soup: BeautifulSoup) -> str:
    preamble_div = soup.find("div", class_="preamble-content")
    if not preamble_div:
        for sec in soup.find_all("section", class_="padding-bottom-20"):
            classes = sec.get("class", [])
            if "bg-striped" not in classes and "bg-act-section" not in classes:
                preamble_div = sec
                break
    if not preamble_div:
        return ""
    raw = preamble_div.get_text(separator="\n")  # type: ignore[union-attr]
    raw = re.sub(r"\[\d+\]|\[\*+\s*\*+\]|\^\S+\^", "", raw)
    raw = re.sub(r"[^\S\n]+", " ", raw)
    raw = "\n".join(l.strip() for l in raw.splitlines())
    raw = "\n".join(l for l in raw.splitlines() if l and not re.fullmatch(r"[\d\u09E6-\u09EF]+", l))
    for marker in ("\nCONTENTS", "\nSECTIONS"):
        idx = raw.find(marker)
        if idx != -1:
            raw = raw[:idx]
    raw = _RE_ANNOTATION_BRACKETS_PREAMBLE.sub(r"\1", raw)
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
    container = soup.find("section", class_="padding-bottom-20 bg-striped")
    if not container:
        raise ValueError("Detail page: cannot find main container")
    last_section_num: str = ""
    for row in container.find_all("div", class_="row", recursive=False):
        part_group = row.find("div", class_="act-part-group")
        if part_group:
            no_p = part_group.find("p", class_="act-part-no")
            link = no_p.find("a") if no_p else part_group.find("a")
            if link and link.get("href"):
                part_num  = _normalise(link.get_text(strip=True))
                part_url  = urljoin(base, link["href"])
                name_p    = part_group.find("p", class_="act-part-name")
                subtitle  = _normalise(name_p.get_text(strip=True)) if name_p else ""
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
            raw = _normalise(" ".join(sec_head.get_text(separator=" ", strip=True).split()))
            m = RE_SUBCHAPTER_HEAD.match(raw)
            if m:
                yield SubChapterItem(letter=m.group(1), subtitle=m.group(2).strip(),
                                     parent_number=last_section_num)
            elif raw:
                yield SubChapterItem(letter="", subtitle=raw, parent_number=last_section_num)

        head_div    = row.find("div", class_="txt-head")
        details_div = row.find("div", class_="txt-details")
        if details_div:
            if head_div:
                raw_heading = _normalise(
                    " ".join(head_div.get_text(separator=" ", strip=True).split())
                )
                heading = _clean_heading(raw_heading)
            else:
                heading = ""
            raw_text = _get_text(details_div)
            number   = extract_section_number(raw_text)
            if number:
                last_section_num = number
            if not _is_boilerplate(raw_text):
                yield SectionItem(heading=heading, url="#", html=details_div, number=number)


def resolve_urls(nodes: list[DocNode], toc_map: dict[str, str], base: str) -> list[DocNode]:
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
# Content cleaning pipeline
# ---------------------------------------------------------------------------


def _transform_footnotes(text: str) -> str:
    text = re.sub(r"\[\*+(?:\s*\*+)+\]", "[ * * * ]", text)
    text = re.sub(r"\[(\d+)\]", r"[^\1]", text)
    text = re.sub(r"\^\S+\^", "", text)
    return text


_RE_OMISSION_MARKER = re.compile(r'\[\^(\d+)\]\s*\[\s*\*\s*\*\s*\*\s*\]')
_RE_OMITTED_TERM    = re.compile(
    r'[Tt]he words?\s+[“"]([^”"]+)[”"]\s+(?:was|were)\s+omitted',
    re.IGNORECASE,
)


def _resolve_omissions(text: str, footnote_map: dict) -> str:
    def replace(m: re.Match) -> str:
        num  = int(m.group(1))
        note = footnote_map.get(num, "")
        term = _RE_OMITTED_TERM.search(note)
        if term:
            return f'[^{num}][~~"{term.group(1)}"~~ omitted]'
        return f"[^{num}][omitted]"
    return _RE_OMISSION_MARKER.sub(replace, text)


_RE_LEADING_FN = re.compile(r'^(\[\^\d+\])\s*(?!\[)(?!:)', re.MULTILINE)


def _relocate_leading_footnotes(text: str) -> str:
    """Move [^N] from paragraph start to paragraph end."""
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        m = _RE_LEADING_FN.match(lines[i])
        if m:
            fn_marker = m.group(1)
            rest      = lines[i][m.end():]
            para = [rest] if rest.strip() else []
            i += 1
            while i < len(lines) and lines[i].strip() and not _is_block_break(lines[i].strip()):
                para.append(lines[i])
                i += 1
            if para:
                para[-1] = para[-1].rstrip() + fn_marker
                out.extend(para)
            else:
                out.append(fn_marker)
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def _handle_annotation_bracket(m: re.Match) -> str:
    """Strip annotation brackets returning plain content (no highlighting)."""
    return " ".join(m.group(1).split())


_RE_WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")

def _strip_annotation_brackets(text: str) -> str:
    """Strip editorial [annotation] brackets while preserving [[wikilinks]]."""
    # Phase 1: replace wikilinks with placeholders
    wikilinks: list[str] = []
    def _stash(m: re.Match) -> str:
        wikilinks.append(m.group(0))
        return f"\x00wl{len(wikilinks)-1}\x00"
    text = _RE_WIKILINK.sub(_stash, text)
    # Phase 2: strip annotation brackets (now safe — no [[ or ]] inside)
    text = _RE_ANNOTATION_BRACKETS.sub(_handle_annotation_bracket, text)
    # Phase 3: restore wikilinks
    for i, wl in enumerate(wikilinks):
        text = text.replace(f"\x00wl{i}\x00", wl)
    return text


def _strip_leading_section_num(text: str) -> str:
    stripped = text.lstrip()
    fn_prefix = ""
    m_fn = _RE_FN_PREFIX.match(stripped)
    if m_fn:
        fn_prefix = m_fn.group(1)
        stripped  = stripped[m_fn.end():].lstrip()
    stripped = re.sub(r"^[.\s]+", "", stripped)

    def _try_strip(s: str) -> Optional[str]:
        for pat in (RE_BN_SECTION_NUM_BRACKET, RE_BN_SECTION_NUM_PLAIN):
            m = pat.match(s)
            if m:
                return s[m.end():]
        m = RE_BN_SECTION_NUM_CLAUSE.match(s)
        if m:
            dot_pos = next(i for i, c in enumerate(s) if c in _BN_DANDA)
            return s[dot_pos + 1:]
        for pat in (RE_SECTION_NUM_BRACKET, RE_SECTION_NUM_PLAIN):
            m = pat.match(s)
            if m:
                return s[m.end():]
        m = RE_SECTION_NUM_CLAUSE.match(s)
        if m:
            return s[s.index(".") + 1:]
        return None

    result = _try_strip(stripped)
    if result is not None:
        return fn_prefix + result
    return (fn_prefix + stripped) if fn_prefix else text


def _strip_outer_brackets(text: str) -> str:
    text = text.strip()
    if not text.startswith("[") or not text.endswith("]"):
        return text
    depth = 0
    for i, ch in enumerate(text):
        if ch == "[":   depth += 1
        elif ch == "]": depth -= 1
        if depth == 0:
            if i == len(text) - 1:
                return text[1:-1].strip()
            return text
    return text


def _join_isolated_labels(text: str) -> str:
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


def _is_block_break(s: str) -> bool:
    return bool(
        s.startswith("**") or s.startswith(">") or s.startswith("#")
        or _classify_item(s) is not None
        or _RE_ORDINAL_ITEM.match(s)
        or _RE_EXPL_NUMBERED.match(s) or _RE_EXPL_SINGLE.match(s)
        or _RE_EXCEP_NUMBERED.match(s) or _RE_EXCEP_SINGLE.match(s)
        or _RE_ILLUSTRATIONS.match(s)
        or _RE_PROVIDED_STANDALONE.match(s)
        or any(p.match(s) for p, _, _ in _MARGIN_CALLOUTS)
    )


def _join_fragment_lines(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        if _RE_CONNECTOR.match(lines[i]) and out:
            out[-1] = out[-1].rstrip() + " " + lines[i].strip()
            i += 1
            while i < len(lines) and not lines[i].strip():
                i += 1
            if i < len(lines) and lines[i].strip() and not _is_block_break(lines[i].strip()):
                out[-1] = out[-1].rstrip() + " " + lines[i].strip()
                i += 1
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def _strip_unmatched_open_brackets(text: str) -> str:
    lines = text.splitlines()
    out = []
    for line in lines:
        s = line.lstrip()
        if (
            s.startswith("[") and not s.startswith("[^")
            and not s.startswith("[ *") and not s.startswith("[[")
            and "]]" not in s and "]" not in s
        ):
            out.append(s[1:])
        else:
            out.append(line)
    return "\n".join(out)


def _check_section_status(raw: str) -> Optional[str]:
    s = raw.strip()
    if _RE_STATUS_OMITTED.match(s):
        return "> [!Warning] Omitted\n> This section has been omitted."
    if _RE_STATUS_REPEALED.match(s):
        return "> [!Warning] Repealed\n> This section has been repealed."
    return None


def _classify_item(line: str) -> Optional[str]:
    """
    Classify a line as a list item type, or None if not a list item.
    Strips ALL leading [^N] refs then a bare [ wrapper before classifying.
    """
    s = line.strip()
    s = re.sub(r'^(\[\^\d+\]\s*)+', '', s)
    if s.startswith("[") and not s.startswith("[[") and not s.startswith("[^"):
        s = s[1:]
    if RE_BN_DECIMAL_ITEM.match(s): return "bn_decimal"
    if RE_BN_ALPHA_ITEM.match(s):   return "bn_alpha"
    if RE_BN_VOWEL_ITEM.match(s):   return "bn_vowel"
    if RE_DECIMAL_ITEM.match(s):    return "decimal"
    m_roman = RE_ROMAN_ITEM.match(s)
    if m_roman and len(m_roman.group(1)) > 1:
        return "roman"
    m_alpha = RE_ALPHA_ITEM.match(s)
    if m_alpha:
        return "roman" if m_alpha.group(1).lower() == "i" else "alpha"
    return None


def _extract_item_content(line: str) -> str:
    """Strip the list marker, preserving ALL leading [^N] refs."""
    s = line.strip()
    fn_prefix = ""
    while True:
        fn_m = re.match(r'^(\[\^\d+\])\s*', s)
        if fn_m:
            fn_prefix += fn_m.group(1) + " "
            s = s[fn_m.end():]
        else:
            break
    if s.startswith("[") and not s.startswith("[[") and not s.startswith("[^"):
        s = s[1:]
    for pat in (
        RE_ALPHA_ITEM, RE_ROMAN_ITEM, RE_DECIMAL_ITEM,
        RE_BN_ALPHA_ITEM, RE_BN_DECIMAL_ITEM, RE_BN_VOWEL_ITEM,
    ):
        m = pat.match(s)
        if m:
            return fn_prefix + m.group(2).strip()
    return fn_prefix + s


_RE_INLINE_ITEM_SEP = re.compile(
    r'\s*[;,]\s+(?:(?:and|or)\s+)?\(([a-zA-Z]|\d+)\)(?=\s)',
)


def _split_inline_list_items(text: str) -> str:
    """Split inline sub-clauses like '...; (d) text' onto separate lines.

    Handles patterns where list items are written inline separated by semicolons
    or commas, e.g. '...unsound mind ; (d) is or is deemed... ; and (e) has not...'
    """
    return _RE_INLINE_ITEM_SEP.sub(lambda m: f'\n({m.group(1)})', text)


def _join_sentence_continuations(text: str) -> str:
    """Merge paragraph fragments split mid-sentence at HTML line breaks.

    Only merges when the preceding line ends with a word that cannot end a
    sentence (preposition, article, auxiliary verb, etc.) and the following
    non-blank line is not a list item or block-break.
    """
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    n = len(lines)

    while i < n:
        out.append(lines[i])
        stripped = lines[i].strip()

        if (not stripped
                or _classify_item(stripped)
                or _is_block_break(stripped)):
            i += 1
            continue

        lw = re.search(r'\b(\w+)\W*$', stripped)
        lw_str = lw.group(1).lower() if lw else ''
        if lw_str not in _CANNOT_END_SENTENCE_WORDS:
            i += 1
            continue

        # Look ahead past blank lines for a continuation fragment
        j = i + 1
        while j < n and not lines[j].strip():
            j += 1
        if j >= n:
            i += 1
            continue

        ns = lines[j].strip()
        if ns and not _classify_item(ns) and not _is_block_break(ns):
            out[-1] = out[-1].rstrip() + ' ' + ns
            i = j + 1
        else:
            i += 1

    return "\n".join(out)


def convert_lists(text: str) -> str:
    """
    Convert list items to nested Markdown ordered lists.

    Depth hierarchy (applied consistently across Latin and Bengali):
      decimal  / bn_alpha   -> depth 1  (1. 2. 3.)
      alpha    / bn_decimal -> depth 2     (1. 2. 3. indented 3 spaces)
      roman    / bn_vowel   -> depth 3        (1. 2. 3. indented 6 spaces)

    Transitioning from a deeper type back to a shallower type resets the
    deeper item counts, so (1)(a)(b)(2) renders as:

        1. first
           1. sub-a
           2. sub-b
        2. second
    """
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    n = len(lines)
    counts: dict[int, int] = {}   # running item count per depth
    last_was_list = False

    # Detect primary list type from first list item to set context-aware depths
    first_lt = None
    for _line in lines:
        _lt = _classify_item(_line)
        if _lt is not None:
            first_lt = _lt
            break
    effective_depth = dict(_LIST_DEPTH)
    if first_lt == "bn_decimal":
        effective_depth["bn_decimal"] = 1
        effective_depth["bn_alpha"]   = 2
    elif first_lt == "bn_alpha":
        effective_depth["bn_alpha"]   = 1
        effective_depth["bn_decimal"] = 2
    elif first_lt == "decimal":
        effective_depth["decimal"] = 1
        effective_depth["alpha"]   = 2

    while i < n:
        lt = _classify_item(lines[i])

        if lt is None:
            if not lines[i].strip() and last_was_list:
                j = i + 1
                while j < n and not lines[j].strip():
                    j += 1
                if j < n and _classify_item(lines[j].strip()) is not None:
                    i += 1
                    continue  # suppress blank line between consecutive list items
            if lines[i].strip():
                last_was_list = False
            out.append(lines[i].rstrip())
            i += 1
            continue

        depth = effective_depth.get(lt, 1)
        pad   = " " * (_LIST_INDENT * (depth - 1))
        cpad  = " " * (_LIST_INDENT * depth)          # continuation indent

        # Reset item counts for depths deeper than current (we returned to a parent)
        for d in list(counts.keys()):
            if d > depth:
                del counts[d]

        counts[depth] = counts.get(depth, 0) + 1
        idx = counts[depth]

        first = _extract_item_content(lines[i])
        i += 1

        # Collect continuation lines for this item
        continuation: list[str] = []
        while i < n:
            next_lt = _classify_item(lines[i])
            if next_lt is not None:
                break   # next list item
            curr_s = lines[i].strip()
            if _is_block_break(curr_s) and curr_s:
                break
            if curr_s:
                last_c = next((c for c in reversed(continuation) if c), first)
                lw = re.search(r'\b(\w+)\W*$', last_c.rstrip())
                lw_str = lw.group(1).lower() if lw else ''
                if (lw_str in _CANNOT_END_SENTENCE_WORDS
                        and not _classify_item(curr_s)):
                    if continuation and continuation[-1]:
                        continuation[-1] = continuation[-1].rstrip() + ' ' + curr_s
                    else:
                        first = first.rstrip() + ' ' + curr_s
                else:
                    continuation.append(curr_s)
                i += 1
            else:
                # blank line: peek ahead
                j = i + 1
                while j < n and not lines[j].strip():
                    j += 1
                ns = lines[j].strip() if j < n else ""
                if _classify_item(ns) is not None:
                    break  # next list item follows
                elif ns and not _is_block_break(ns):
                    last_c = next((c for c in reversed(continuation) if c), first)
                    lw = re.search(r'\b(\w+)\W*$', last_c.rstrip())
                    lw_str = lw.group(1).lower() if lw else ''
                    if lw_str in _CANNOT_END_SENTENCE_WORDS:
                        i = j  # skip blank(s); continuation collected next iter
                    else:
                        continuation.append("")  # preserve paragraph break in item
                        i += 1
                else:
                    break

        out.append(f"{pad}{idx}. {first}")
        for cont_line in continuation:
            if cont_line:
                out.append(f"{cpad}{cont_line}")
            else:
                out.append("")
        last_was_list = True

    return "\n".join(out)


def _convert_provided(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if _RE_PROVIDED_STANDALONE.match(stripped):
            keyword = re.sub(r"[-\u2013.:]+$", "", stripped).strip()
            out.append(f"\n**{keyword.capitalize()}**:\n")
            i += 1
        else:
            m = _RE_ORDINAL_ITEM.match(stripped)
            if m:
                ordinal  = m.group(1).capitalize()
                content  = m.group(2).strip()
                i += 1
                while i < len(lines) and lines[i].strip() and not _is_block_break(lines[i].strip()):
                    content += " " + lines[i].strip()
                    i += 1
                out.append(f"\n**{ordinal}**: {content}\n")
            else:
                out.append(lines[i])
                i += 1
    return "\n".join(out)


def _convert_numbered_callout(
    text: str, re_numbered: re.Pattern, re_single: re.Pattern, callout_name: str
) -> str:
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        raw_line = lines[i].strip()
        fn_m = _RE_FN_PREFIX.match(raw_line)
        check_line = raw_line[fn_m.end():].lstrip() if fn_m else raw_line
        if not re_numbered.match(check_line):
            out.append(lines[i])
            i += 1
            continue
        fn_prefix = fn_m.group(1) if fn_m else ""
        items: list[str] = []
        while i < len(lines):
            raw_l = lines[i].strip()
            fn_m2 = _RE_FN_PREFIX.match(raw_l)
            chk   = raw_l[fn_m2.end():].lstrip() if fn_m2 else raw_l
            m2    = re_numbered.match(chk)
            if m2:
                pfx     = (fn_m2.group(1) + " ") if fn_m2 else ""
                content = pfx + m2.group(2).strip()
                i += 1
                while i < len(lines) and lines[i].strip() and not _is_block_break(lines[i].strip()):
                    content += " " + lines[i].strip()
                    i += 1
                items.append(content)
            elif not lines[i].strip():
                i += 1
                j = i
                while j < len(lines) and not lines[j].strip():
                    j += 1
                chk_j = lines[j].strip() if j < len(lines) else ""
                fn_j  = _RE_FN_PREFIX.match(chk_j)
                chk_j = chk_j[fn_j.end():].lstrip() if fn_j else chk_j
                if j < len(lines) and re_numbered.match(chk_j):
                    continue
                break
            else:
                break
        out.append(f"> [!{callout_name}]")
        if fn_prefix:
            out.append(f"> {fn_prefix}")
        for idx, item in enumerate(items, 1):
            # Also run convert_lists on item content for any sub-lists
            converted = convert_lists(item.strip())
            item_lines = converted.splitlines()
            if item_lines:
                out.append(f"> {idx}. {item_lines[0]}")
                for cont in item_lines[1:]:
                    out.append(f">    {cont}" if cont.strip() else "> ")
    return "\n".join(out)


def _convert_single_callout(
    text: str, re_single: re.Pattern, re_numbered: re.Pattern, callout_name: str
) -> str:
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        raw_line = lines[i].strip()
        fn_m = _RE_FN_PREFIX.match(raw_line)
        check_line = raw_line[fn_m.end():].lstrip() if fn_m else raw_line
        m = re_single.match(check_line)
        if m:
            fn_prefix = (fn_m.group(1) + " ") if fn_m else ""
            content   = fn_prefix + m.group(1).strip()
            i += 1
            while i < len(lines) and lines[i].strip() and not _is_block_break(lines[i].strip()):
                content += " " + lines[i].strip()
                i += 1
            out.append(f"> [!{callout_name}]")
            out.append(f"> {content}")
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def _wrap_illustrations_callout(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        if not _RE_ILLUSTRATIONS.match(lines[i].strip()):
            out.append(lines[i])
            i += 1
            continue
        i += 1
        while i < len(lines) and not lines[i].strip():
            i += 1
        out.append("> [!Illustrations]")
        while i < len(lines) and lines[i].strip() and not _is_block_break(lines[i].strip()):
            out.append(f"> {lines[i].strip()}")
            i += 1
    return "\n".join(out)


def _nest_secondary_callouts(text: str) -> str:
    _RE_EXPL_START    = re.compile(r"^> \[!Explanation\]$")
    _RE_SECONDARY_HDR = re.compile(r"^> \[!(Illustrations?|Exception[s]?)\]$")
    lines = text.splitlines()
    out   = []
    i     = 0
    while i < len(lines):
        if _RE_EXPL_START.match(lines[i]):
            out.append(lines[i])
            i += 1
            while i < len(lines):
                curr = lines[i]
                if _RE_SECONDARY_HDR.match(curr):
                    out.append("> " + curr)
                    i += 1
                    while i < len(lines) and lines[i].startswith("> "):
                        out.append("> " + lines[i])
                        i += 1
                    break
                elif not curr.strip():
                    j = i + 1
                    while j < len(lines) and not lines[j].strip():
                        j += 1
                    if j < len(lines) and _RE_SECONDARY_HDR.match(lines[j]):
                        i = j
                        continue
                    out.append(curr)
                    i += 1
                elif curr.startswith("> "):
                    out.append(curr)
                    i += 1
                else:
                    break
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def _convert_margin_callouts(text: str) -> str:
    lines = text.splitlines()
    out   = []
    i     = 0
    while i < len(lines):
        matched = False
        raw_line = lines[i].strip()
        for pattern, callout_type, label in _MARGIN_CALLOUTS:
            m = pattern.match(raw_line)
            if m:
                content = m.group(1).strip()
                i += 1
                while i < len(lines) and lines[i].strip() and not _is_block_break(lines[i].strip()):
                    content += " " + lines[i].strip()
                    i += 1
                out.append(f"> [!{callout_type}] {label}")
                if content:
                    out.append(f"> {content}")
                matched = True
                break
        if not matched:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def _convert_definitions(text: str) -> str:
    lines = text.splitlines()
    out   = []
    for line in lines:
        m = _RE_DEFINITION.match(line.strip())
        if m:
            term    = m.group(1).strip()
            verb    = m.group(2)
            content = m.group(3).strip()
            out.append("> [!Definition]")
            out.append(f"> **{term}** {verb} {content}")
        else:
            out.append(line)
    return "\n".join(out)


def _bold_punishment_keywords(text: str) -> str:
    return _RE_PUNISHMENT.sub(r"**\1**", text)


def _mark_omitted_repealed(text: str) -> str:
    text = _RE_OMITTED_INLINE.sub(r"~~\1~~",  text)
    text = _RE_REPEALED_INLINE.sub(r"~~\1~~", text)
    return text


def _linkify_section_refs(text: str, act_number: str, known_sections: set = frozenset()) -> str:
    if not act_number:
        return text

    def replace_sec(m: re.Match) -> str:
        prefix = m.group(1)
        num    = m.group(2)
        anchor = _secnum_to_block_id(num)
        if anchor not in known_sections:
            return m.group(0)
        return f"[[act-{act_number}#^sec-{anchor}|{prefix} {num}]]"

    result = []
    last   = 0
    for m in _RE_SECTION_REF.finditer(text):
        before = text[max(0, m.start()-10):m.start()]
        if "[[" in before and "]]" not in before:
            result.append(text[last:m.end()])
        else:
            result.append(text[last:m.start()])
            result.append(replace_sec(m))
        last = m.end()
    result.append(text[last:])
    return "".join(result)


def _table_to_markdown(table: Tag) -> str:
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
    return "\n".join(
        ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(sep) + " |"]
        + ["| " + " | ".join(r) + " |" for r in rows[1:]]
    )


def _is_boilerplate(raw: str) -> bool:
    lines = [l for l in raw.splitlines() if l.strip()]
    if not lines:
        return True
    bp = sum(
        1 for l in lines
        if _RE_BOILERPLATE_LINE.search(l) or _RE_URL_ONLY.match(l) or _RE_TOC_LINK.match(l)
    )
    return bp / len(lines) > 0.6


def _filter_stray_lines(text: str) -> str:
    kept = []
    for line in text.splitlines():
        stripped = re.sub(r"^[♣♦§¶✦✧·•]+\s*", "", line)
        if stripped != line:
            if stripped.strip():
                kept.append(stripped)
            continue
        if _RE_URL_ONLY.match(line): continue
        if _RE_TOC_LINK.match(line): continue
        if re.match(r"^\s*(CONTENTS|SECTIONS|PREAMBLE)\s*$", line, re.IGNORECASE): continue
        kept.append(line)
    return "\n".join(kept)


def html_to_markdown(
    details_div: Tag,
    scraper: Optional[Scraper] = None,
    act_number: str = "",
    known_sections: set = frozenset(),
    footnote_map: dict = {},
) -> str:
    """Full pipeline: HTML Tag -> clean Obsidian Markdown."""

    for table in details_div.find_all("table"):
        table.replace_with(_table_to_markdown(table) + "\n")

    raw = _tag_to_md_text(details_div, scraper)
    raw = _RE_RESIDUAL_HTML.sub("\n", raw)
    raw = _transform_footnotes(raw)
    raw = re.sub(r"[^\S\n]+", " ", raw)
    raw = "\n".join(line.lstrip() for line in raw.splitlines())
    raw = "\n".join(
        line for line in raw.splitlines()
        if not re.fullmatch(r"\s*[\d\u09E6-\u09EF]+\s*", line)
    )
    raw = _strip_outer_brackets(raw)

    badge = _check_section_status(raw)
    if badge:
        return badge

    raw = _strip_leading_section_num(raw)
    raw = _filter_stray_lines(raw)
    raw = _RE_SUBST_LETTER.sub(r"\1", raw)
    raw = _RE_ANNOTATION_BRACKETS.sub(_handle_annotation_bracket, raw)
    raw = re.sub(r'(?<=\]\])\.?\](?!\])', lambda m: '.' if '.' in m.group(0) else '', raw)
    raw = _strip_unmatched_open_brackets(raw)
    raw = re.sub(r"^\.\s+(?=\()", "", raw.lstrip())
    raw = _join_isolated_labels(raw)
    raw = _split_inline_list_items(raw)
    raw = _join_fragment_lines(raw)
    raw = _join_sentence_continuations(raw)
    raw = _mark_omitted_repealed(raw)
    raw = _convert_provided(raw)
    raw = _bold_punishment_keywords(raw)
    raw = _convert_numbered_callout(raw, _RE_EXPL_NUMBERED, _RE_EXPL_SINGLE, "Explanation")
    raw = _convert_single_callout(raw, _RE_EXPL_SINGLE, _RE_EXPL_NUMBERED, "Explanation")
    raw = _convert_numbered_callout(raw, _RE_EXCEP_NUMBERED, _RE_EXCEP_SINGLE, "Exception")
    raw = _convert_single_callout(raw, _RE_EXCEP_SINGLE, _RE_EXCEP_NUMBERED, "Exception")
    raw = _convert_margin_callouts(raw)
    raw = _convert_definitions(raw)
    raw = convert_lists(raw)
    raw = _wrap_illustrations_callout(raw)
    raw = _nest_secondary_callouts(raw)
    raw = _linkify_section_refs(raw, act_number, known_sections)
    if footnote_map:
        raw = _resolve_omissions(raw, footnote_map)
    raw = _relocate_leading_footnotes(raw)
    # Remove stray trailing ] from unclosed editorial brackets,
    # but not the ] that closes a [ * * * ] omission marker or a [^N] footnote ref.
    raw = re.sub(r"(?<!\])(?<!\* )(?<!\d)\]\s*$", "", raw.rstrip())
    raw = RE_MULTI_NEWLINE.sub("\n\n", raw)

    return raw.strip()


# ---------------------------------------------------------------------------
# YAML & builder
# ---------------------------------------------------------------------------


def _url_to_section_id(url: str) -> Optional[str]:
    m = re.search(r"/section-(\d+)\.html", url)
    return m.group(1) if m else None


def _detect_language(text: str) -> str:
    bn_chars = sum(1 for c in text if "ঀ" <= c <= "৿")
    return "bn" if bn_chars > 50 else "en"


def _heading_to_slug(text: str, language: str = "en") -> str:
    if language == "bn":
        text = re.sub(r"[^ঀ-৿\s]", "", text)
        return re.sub(r"\s+", "-", text.strip())[:60]
    text = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[\s_]+", "-", text).strip("-")[:60]


def _fix_bengali_ocr(text: str) -> str:
    _BN_C = "কখগঘঙচছজঝঞটঠডঢণতথদধনপফবভমযরলশষসহড়ঢ়য়ৎ"
    text = re.sub(
        r"([" + _BN_C + r"])৪([" + _BN_C + r"])",
        r"\1ৃ\2", text,
    )
    text = re.sub(
        r"([" + _BN_C + r"])৷(?=[" + _BN_C + r"\s])",
        r"\1ি", text,
    )
    return text


_TAG_EXCLUSIONS: dict[str, list[str]] = {
    "Banking & Finance": ["police", "criminal", "penal", "army", "navy", "air force", "intelligence"],
    "Tax Law":           ["police", "criminal", "penal", "army", "navy", "intelligence"],
    "Labour Law":        ["police", "criminal", "penal", "intelligence"],
    "Property Law":      ["police", "criminal", "penal", "intelligence"],
    "Civil Law":         ["police act", "criminal procedure", "penal"],
}


def _infer_tags(act_title: str, nodes: list) -> list[str]:
    tags = ["Legal", "Bangladesh"]
    combined = (
        act_title.lower() + " "
        + " ".join(
            n.heading for n in nodes if isinstance(n, SectionItem) and n.heading
        ).lower()[:4000]
    )
    title_lower = act_title.lower()
    for tag, keywords in _TAG_KEYWORDS.items():
        if any(kw in combined for kw in keywords):
            exclusions = _TAG_EXCLUSIONS.get(tag, [])
            if any(ex in title_lower for ex in exclusions):
                continue
            tags.append(tag)
    return tags[:8]


def _build_yaml_frontmatter(
    act_title: str,
    act_url: str,
    tags: list[str],
    act_number: str = "",
    language: str = "en",
    section_entries: list = [],
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tag_lines = "\n".join(f"  - {t}" for t in tags)
    safe_title = act_title.replace('"', '\\"')
    year_m = re.search(r"\b(1[6-9]\d{2}|20\d{2})\b", act_title)
    year_line = f"year: {year_m.group(1)}\n" if year_m else ""
    act_id_line = f"act_id: {act_number}\n" if act_number else ""
    if section_entries:
        sec_block = "sections:\n" + "".join(
            f"  - id: {e['id']}\n    slug: {e['slug']}\n    url: {e['url']}\n"
            for e in section_entries
        )
    else:
        sec_block = ""
    return (
        f"---\n"
        f'title: "{safe_title}"\n'
        f"tags:\n{tag_lines}\n"
        f"scraped_at: {now}\n"
        f"source_url: {act_url}\n"
        f"{act_id_line}"
        f"{year_line}"
        f"language: {language}\n"
        f"{sec_block}"
        f"---\n"
    )


def build_markdown(
    nodes: list[DocNode],
    act_number: str,
    act_title: str = "",
    act_url: str = "",
    scraper: Optional[Scraper] = None,
    title_footnote_num: Optional[int] = None,
) -> str:
    # Build footnote lookup for omission resolution
    footnote_map: dict[int, str] = {}
    for node in nodes:
        if isinstance(node, FootnotesItem):
            for num, text in node.notes:
                footnote_map[num] = text
            break

    # Detect language from preamble + first few sections (must run before slug loop)
    sample = act_title + " ".join(
        (node.heading or "") for node in nodes[:10] if isinstance(node, SectionItem)
    )
    language = _detect_language(sample)

    # Collect known section IDs and section frontmatter entries
    known_sections: set[str] = set()
    seen_section_ids: set[str] = set()
    section_entries: list[dict] = []
    for node in nodes:
        if isinstance(node, SectionItem):
            if node.number:
                known_sections.add(_secnum_to_block_id(node.number))
            if node.url and node.url != "#":
                sec_id = _url_to_section_id(node.url)
                if sec_id:
                    known_sections.add(sec_id)
                    if sec_id not in seen_section_ids:
                        seen_section_ids.add(sec_id)
                        slug = _heading_to_slug(node.heading or node.number or "", language)
                        section_entries.append({"id": sec_id, "slug": slug, "url": node.url})

    lines: list[str] = []
    tags = _infer_tags(act_title, nodes)
    lines.append(_build_yaml_frontmatter(
        act_title, act_url, tags,
        act_number=act_number, language=language, section_entries=section_entries,
    ))

    if act_title:
        if title_footnote_num is not None:
            lines.append(f"# {act_title}[^{title_footnote_num}]")
        else:
            lines.append(f"# {act_title}")
        lines.append("")

    for node in nodes:
        if isinstance(node, PreambleItem):
            preamble_text = _filter_stray_lines(node.text)
            if language == "bn":
                preamble_text = _fix_bengali_ocr(preamble_text)
            lines.append("")
            lines.append("> [!Preamble]")
            for pline in preamble_text.splitlines():
                lines.append(f"> {pline}" if pline.strip() else ">")
            lines.append("")

        elif isinstance(node, FootnotesItem):
            lines.append("")
            lines.append("## Footnotes")
            lines.append("")
            seen: dict[str, int] = {}
            deduped: list[tuple[int, str]] = []
            for num, text in node.notes:
                if text not in seen:
                    seen[text] = num
                    deduped.append((num, text))
            for num, text in deduped:
                lines.append(f"[^{num}]: {text}")
            lines.append("")

        elif isinstance(node, PartItem):
            lines.append("")
            lines.append(f"## [{node.name}]({node.url})")
            lines.append("")

        elif isinstance(node, ChapterItem):
            lines.append("")
            lines.append(f"### [{node.name}]({node.url})")
            lines.append("")

        elif isinstance(node, SubChapterItem):
            if node.letter and node.parent_number:
                heading_text = f"Section {node.parent_number}{node.letter}: {node.subtitle}"
            else:
                heading_text = node.subtitle
            lines.append("")
            lines.append(f"#### [{heading_text}]({node.url})")
            lines.append("")

        elif isinstance(node, SectionItem):
            has_heading = bool(node.heading) or bool(node.number)
            if has_heading:
                if node.number:
                    term         = "সেকশন" if language == "bn" else ("Article" if "/Article-" in node.url else "Section")
                    heading_text = (
                        f"{term} {node.number}: {node.heading}"
                        if node.heading else f"{term} {node.number}"
                    )
                    is_inserted = any(c.isalpha() for c in node.number)
                    level       = "######" if is_inserted else "#####"
                    block_id    = f"^sec-{_secnum_to_block_id(node.number)}"
                else:
                    heading_text = node.heading
                    level        = "#####"
                    block_id     = ""
                    if node.url and node.url != "#":
                        sec_id = _url_to_section_id(node.url)
                        if sec_id:
                            block_id = f"^sec-{sec_id}"
                block_suffix = f" {block_id}" if block_id else ""
                lines.append("")
                lines.append(f"{level} [{heading_text}]({node.url}){block_suffix}")
                if node.url and node.url != "#":
                    lines.append(f"<!-- url: {node.url} -->")
                lines.append("")

            body = html_to_markdown(
                node.html, scraper=scraper, act_number=act_number,
                known_sections=known_sections, footnote_map=footnote_map,
            )
            if language == "bn":
                body = _fix_bengali_ocr(body)
            lines.append(body)
            lines.append("")

    return _final_cleanup("\n".join(lines))


def _final_cleanup(md: str) -> str:
    cleaned = "\n".join(line.rstrip() for line in md.split("\n"))
    return RE_MULTI_NEWLINE.sub("\n\n", cleaned).strip() + "\n"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def scrape_act(
    act_number: str, output: Path, delay: float,
    max_retries: int, cache_dir: Optional[Path], log: logging.Logger,
) -> None:
    toc_url    = f"{BASE_URL}/act-{act_number}.html"
    detail_url = f"{BASE_URL}/act-print-{act_number}.html"
    scraper    = Scraper(delay=delay, max_retries=max_retries, cache_dir=cache_dir)

    log.info("Fetching ToC page: %s", toc_url)
    toc_soup = scraper.fetch(toc_url)
    toc_map  = parse_toc(toc_soup, BASE_URL)
    log.info("Found %d section links in ToC", len(toc_map))

    log.info("Fetching detail page: %s", detail_url)
    detail_soup = scraper.fetch(detail_url)
    log.info("Parsing document structure ...")

    if _RICH:
        nodes: list[DocNode] = []
        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
            BarColumn(), MofNCompleteColumn(), TimeElapsedColumn(), console=console,
        ) as progress:
            task = progress.add_task("Parsing nodes ...", total=None)
            for node in parse_detail(detail_soup, BASE_URL):
                nodes.append(node)
                if isinstance(node, SectionItem):
                    progress.advance(task)
            progress.update(task, total=len(nodes), completed=len(nodes))
    else:
        nodes = list(parse_detail(detail_soup, BASE_URL))

    section_count = sum(1 for n in nodes if isinstance(n, SectionItem))
    log.info("Parsed %d nodes (%d sections, %d chapters, %d parts)",
             len(nodes), section_count,
             sum(1 for n in nodes if isinstance(n, ChapterItem)),
             sum(1 for n in nodes if isinstance(n, PartItem)))

    log.info("Resolving section URLs ...")
    resolve_urls(nodes, toc_map, BASE_URL)

    act_title    = parse_act_title(detail_soup) or parse_act_title(toc_soup)
    title_fn_num = _extract_title_footnote_num(detail_soup) or _extract_title_footnote_num(toc_soup)
    act_url_str  = f"{BASE_URL}/act-{act_number}.html"
    if act_title:     log.info("Act title: %s", act_title)
    if title_fn_num:  log.info("Title footnote: [^%d]", title_fn_num)

    preamble = parse_preamble(detail_soup) or parse_preamble(toc_soup)
    if preamble:
        log.info("Preamble found (%d chars)", len(preamble))
        nodes.insert(0, PreambleItem(text=preamble))

    footnotes = parse_footnotes(detail_soup)
    if footnotes:
        log.info("Found %d footnotes", len(footnotes.notes))
        nodes.append(footnotes)

    log.info("Building Markdown ...")
    md = build_markdown(
        nodes, act_number,
        act_title=act_title, act_url=act_url_str,
        scraper=scraper, title_footnote_num=title_fn_num,
    )
    output.write_text(md, encoding="utf-8")
    log.info("Saved -> %s  (%d chars)", output, len(md))


# ---------------------------------------------------------------------------
# Batch scraping
# ---------------------------------------------------------------------------


def _scrape_all_acts(
    output_dir: Path, delay: float, max_retries: int,
    cache_dir: Optional[Path], workers: int, skip_existing: bool,
    log: logging.Logger,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    acts = list(range(1, 1720))

    def run_one(act_num: int) -> tuple[int, str]:
        out_file = output_dir / f"act-{act_num}.md"
        if skip_existing and out_file.exists():
            return act_num, "skip"
        try:
            scrape_act(str(act_num), out_file, delay, max_retries, cache_dir, log)
            return act_num, "ok"
        except Exception as e:
            return act_num, f"fail: {e}"

    ok = skip = fail = 0

    if _RICH:
        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
            BarColumn(), MofNCompleteColumn(), TimeElapsedColumn(), console=console,
        ) as progress:
            task = progress.add_task("Scraping acts ...", total=len(acts))
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {ex.submit(run_one, n): n for n in acts}
                for future in as_completed(futures):
                    act_num, status = future.result()
                    if status == "ok":
                        ok += 1
                    elif status == "skip":
                        skip += 1
                    else:
                        fail += 1
                        log.warning("act-%d: %s", act_num, status)
                    progress.advance(task)
            progress.update(task, description=f"Done: {ok} ok, {skip} skipped, {fail} failed")
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(run_one, n): n for n in acts}
            for future in as_completed(futures):
                act_num, status = future.result()
                if status == "ok":
                    ok += 1
                    print(f"[ OK ] act-{act_num}")
                elif status == "skip":
                    skip += 1
                    print(f"[SKIP] act-{act_num}")
                else:
                    fail += 1
                    print(f"[FAIL] act-{act_num}: {status}")
        print(f"Done: {ok} ok, {skip} skipped, {fail} failed")

    log.info("Batch complete: %d ok, %d skipped, %d failed", ok, skip, fail)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.command()
@click.option("--act", "act_number", default=None,
              help="Act number from bdlaws.minlaw.gov.bd (e.g. 1710 for CrPC).")
@click.option("--all", "scrape_all", is_flag=True, default=False,
              help="Scrape all acts (1–1719) in parallel.")
@click.option("--output", "-o", default=None,
              help="Output Markdown file (single-act mode). Defaults to act-<number>.md.")
@click.option("--output-dir", default="acts", show_default=True,
              help="Output directory for --all mode.")
@click.option("--delay", "-d", default=0.3, show_default=True, type=float,
              help="Min seconds between HTTP requests per worker.")
@click.option("--retries", "-r", default=4, show_default=True, type=int,
              help="Max retry attempts per request.")
@click.option("--cache-dir", default="cache", show_default=True, type=click.Path(),
              help="Directory for raw HTML cache.")
@click.option("--workers", "-w", default=4, show_default=True, type=int,
              help="Parallel workers for --all mode.")
@click.option("--skip-existing", is_flag=True, default=False,
              help="Skip acts that already have an output file (--all mode).")
@click.option("--verbose", "-v", is_flag=True, default=False,
              help="Enable DEBUG-level logging.")
def main(
    act_number: Optional[str], scrape_all: bool, output: Optional[str],
    output_dir: str, delay: float, retries: int, cache_dir: Optional[str],
    workers: int, skip_existing: bool, verbose: bool,
) -> None:
    """Scrape Bangladesh law acts and save as Obsidian-compatible Markdown.

    Single act:  --act 1710
    All acts:    --all  (parallel, uses cache by default)
    """
    log = setup_logging(verbose)


    if not act_number and not scrape_all:
        raise click.UsageError("Provide --act <number> or --all")
    if act_number and scrape_all:
        raise click.UsageError("--act and --all are mutually exclusive")

    cache_path = Path(cache_dir) if cache_dir else None

    if scrape_all:
        try:
            _scrape_all_acts(
                output_dir=Path(output_dir), delay=delay, max_retries=retries,
                cache_dir=cache_path, workers=workers, skip_existing=skip_existing,
                log=log,
            )
        except KeyboardInterrupt:
            log.warning("Interrupted."); sys.exit(130)
    else:
        output_path = Path(output) if output else Path(f"act-{act_number}.md")
        try:
            scrape_act(act_number=act_number, output=output_path,
                       delay=delay, max_retries=retries,
                       cache_dir=cache_path, log=log)
        except requests.HTTPError as e:
            log.error("HTTP error: %s", e); sys.exit(1)
        except requests.ConnectionError as e:
            log.error("Network error: %s", e); sys.exit(1)
        except ValueError as e:
            log.error("Parse error: %s", e); sys.exit(1)
        except KeyboardInterrupt:
            log.warning("Interrupted."); sys.exit(130)


if __name__ == "__main__":
    main()