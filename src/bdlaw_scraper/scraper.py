#!/usr/bin/env python3
"""
Bangladesh Law Scraper
======================
Scrapes acts from bdlaws.minlaw.gov.bd and converts them to Obsidian-compatible
Markdown files with YAML frontmatter, structured headings, callouts, wikilinks,
footnote anchors, and a searchable act-index.md.

Architecture
------------
Fetching
    Scraper — rate-limited, retry-backed HTTP session with optional disk cache.
    Each act needs two pages: act-N.html (ToC) and act-print-N.html (full text).

Parsing
    parse_* functions use BeautifulSoup to extract structured data from the
    site's fixed HTML layout: title, preamble, sections, footnotes, metadata.

Conversion  (html_to_markdown pipeline)
    The raw HTML of each section passes through ~20 sequential transforms:
    table expansion, footnote conversion, annotation bracket stripping,
    list nesting, callout generation (Explanation/Exception/Definition/etc.),
    section cross-linking, and OCR-error correction for Bengali text.

Output  (build_markdown)
    Assembles a YAML frontmatter block (title, description, tags, dates,
    section index) followed by the rendered DocNode tree. DocNode is a
    union of PartItem | ChapterItem | SubChapterItem | SectionItem |
    PreambleItem | FootnotesItem, each rendered at the appropriate heading level.

Batch
    _scrape_all_acts() runs scrape_act() in a ThreadPoolExecutor across acts
    1–1719, then calls generate_index() to write act-index.md.

Wikilink format
    All cross-act links use [[act-N\\|Display Text]] (escaped pipe) for
    compatibility with Obsidian tables and static site generators.

Usage
-----
    Single act : bdlaw-scraper --act 1710
    All acts   : bdlaw-scraper --all --output-dir acts --workers 8
"""

from __future__ import annotations

import json
import logging
import copy
import re
import sys
import threading
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
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
from .http import BaseScraper

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

_BN_MONTH_MAP: dict[str, int] = {
    "জানুয়ারি": 1,
    "জানুয়ারী": 1,
    "ফেব্রুয়ারি": 2,
    "ফেব্রুয়ারী": 2,
    "মার্চ": 3,
    "এপ্রিল": 4,
    "মে": 5,
    "জুন": 6,
    "জুলাই": 7,
    "আগস্ট": 8,
    "আগষ্ট": 8,
    "সেপ্টেম্বর": 9,
    "অক্টোবর": 10,
    "নভেম্বর": 11,
    "ডিসেম্বর": 12,
}
_EN_MONTH_MAP: dict[str, int] = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
_ORDINAL_WORDS: dict[str, int] = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
    "eleventh": 11,
    "twelfth": 12,
    "thirteenth": 13,
    "fourteenth": 14,
    "fifteenth": 15,
    "sixteenth": 16,
    "seventeenth": 17,
    "eighteenth": 18,
    "nineteenth": 19,
    "twentieth": 20,
    "twenty-first": 21,
    "twenty-second": 22,
    "twenty-third": 23,
    "twenty-fourth": 24,
    "twenty-fifth": 25,
    "twenty-sixth": 26,
    "twenty-seventh": 27,
    "twenty-eighth": 28,
    "twenty-ninth": 29,
    "thirtieth": 30,
    "thirty-first": 31,
}
_BN_LETTER_TO_LATIN: dict[str, str] = {
    "ক": "a",
    "খ": "b",
    "গ": "c",
    "ঘ": "d",
    "ঙ": "e",
    "চ": "f",
    "ছ": "g",
    "জ": "h",
    "ঝ": "i",
    "ঞ": "j",
    "ট": "k",
    "ঠ": "l",
    "ড": "m",
    "ঢ": "n",
    "ণ": "o",
    "ত": "p",
    "থ": "q",
    "দ": "r",
    "ধ": "s",
    "ন": "t",
}
_BN_CONS = "কখগঘঙচছজঝঞটঠডঢণতথদধনপফবভমযরলশষসহড়ঢ়য়ৎ"
_BN_VOWELS = "অআইঈউঊঋএঐওঔ"
_BN_DANDA = "৷।॥"

_BLOCK_ELEMENTS = {
    "p",
    "div",
    "li",
    "tr",
    "td",
    "th",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "blockquote",
    "section",
    "article",
    "ul",
    "ol",
}

# List type -> nesting depth
_LIST_DEPTH: dict[str, int] = {
    "decimal": 1,  # (1)(2)(3)  — top level
    "bn_alpha": 1,  # (ক)(খ)(গ)  — top level (Bengali acts)
    "alpha": 2,  # (a)(b)(c)  — sub-items
    "bn_decimal": 2,  # (১)(২)(৩)  — sub-items (Bengali acts)
    "roman": 3,  # (i)(ii)    — sub-sub-items
    "bn_vowel": 3,  # (অ)(আ)     — sub-sub-items (Bengali acts)
}
_LIST_INDENT = 3  # spaces per depth level

# Words that cannot end a sentence — used to detect mid-sentence HTML line breaks
_CANNOT_END_SENTENCE_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "of",
        "for",
        "in",
        "on",
        "at",
        "to",
        "by",
        "with",
        "from",
        "into",
        "upon",
        "about",
        "under",
        "over",
        "after",
        "before",
        "between",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "has",
        "have",
        "had",
        "will",
        "shall",
        "may",
        "can",
        "must",
        "not",
        "and",
        "or",
        "but",
        "that",
        "which",
        "who",
        "if",
        "as",
        "than",
        "whether",
        "setting",
        "including",
        "being",
        "having",
        "making",
        "following",
        "providing",
        "subject",
        "pursuant",
    }
)

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

RE_MULTI_NEWLINE = re.compile(r"\n{3,}")

RE_ALPHA_ITEM = re.compile(r"^\(([a-zA-Z])\)\s+(.*)", re.DOTALL)
RE_ROMAN_ITEM = re.compile(r"^\(([ivxlcdmIVXLCDM]+)\)\s+(.*)", re.DOTALL)
# (1) and inserted numbers such as (3B) or (13AA); (১) and inserted (১ক), (১খ)
RE_DECIMAL_ITEM = re.compile(r"^\((\d+[A-Za-z]{0,2})\)\s+(.*)", re.DOTALL)
# doubled inserted letters: (aa), (bb)
RE_ALPHA_DOUBLE_ITEM = re.compile(r"^\(([a-zA-Z])\1\)\s+(.*)", re.DOTALL)
RE_BN_ALPHA_ITEM = re.compile(r"^\(([" + _BN_CONS + r"]+)\)\s+(.*)", re.DOTALL)
RE_BN_DECIMAL_ITEM = re.compile(r"^\(([০-৯]+[" + _BN_CONS + r"]{0,2})\)\s+(.*)", re.DOTALL)
RE_BN_VOWEL_ITEM = re.compile(r"^\(([" + _BN_VOWELS + r"])\)\s+(.*)", re.DOTALL)

_SECNUM_CORE = r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)?(?:\.[A-Za-z0-9]+)?"
# "23.-24. [Omitted]": two sections omitted together share one heading (the site prints "-24. [Omitted]" as its title)
RE_SECTION_NUM_RANGE = re.compile(r"^(\d+[A-Za-z]{0,2})\.\s*[-–]\s*(\d+[A-Za-z]{0,2})\.(?=\s|$|[\[(])\s*")
RE_SECTION_NUM_BRACKET = re.compile(rf"^\[({_SECNUM_CORE})\s*\.\s*\]?\s*")
RE_SECTION_NUM_PLAIN = re.compile(rf"^({_SECNUM_CORE})\.(?:\s+|$)")
RE_SECTION_NUM_CLAUSE = re.compile(rf"^({_SECNUM_CORE})\.[\(\[]([A-Za-z0-9]+)[\)\]]")
RE_BN_SECTION_NUM_PLAIN = re.compile(
    r"^([০-৯]+[" + _BN_CONS + r"]?(?:\.[০-৯]+)?)[" + _BN_DANDA + r"]\s+"
)
RE_BN_SECTION_NUM_CLAUSE = re.compile(
    r"^([০-৯]+["
    + _BN_CONS
    + r"]?)["
    + _BN_DANDA
    + r"][\(\[]([০-৯"
    + _BN_CONS
    + r"]+)[\)\]]"
)
RE_BN_SECTION_NUM_BRACKET = re.compile(
    r"^\[([০-৯]+[" + _BN_CONS + r"]?(?:\.[০-৯]+)?)[" + _BN_DANDA + r"]\]?\s*"
)

RE_SUBCHAPTER_HEAD = re.compile(r"^([A-Z])\.\s*-\s*(.+)$")

# Bengali "ব্যাখ্যা" (explanation) and "ব্যতিক্রম" (exception) use the same callouts; the separator can be
# "।-", "৷-" (a danda typed with U+09F7), ":" or ".-".
_SEP = r"[-\u2013\u2014.।৷:]"
_RE_EXPL_NUMBERED = re.compile(
    rf"^(?:Explanation|ব্যাখ্যা)\s+([IVXLCDM]+|\d+|[০-৯]+)\s*{_SEP}+\s*(.*)", re.IGNORECASE
)
_RE_EXPL_SINGLE = re.compile(rf"^(?:Explanation|ব্যাখ্যা)\s*{_SEP}+\s*(.*)", re.IGNORECASE)
_RE_EXCEP_NUMBERED = re.compile(
    rf"^(?:Exception[s]?|ব্যতিক্রম)\s+([IVXLCDM]+|\d+|[০-৯]+)\s*{_SEP}+\s*(.*)", re.IGNORECASE
)
_RE_EXCEP_SINGLE = re.compile(rf"^(?:Exception[s]?|ব্যতিক্রম)\s*{_SEP}+\s*(.*)", re.IGNORECASE)
_RE_ILLUSTRATIONS = re.compile(r"^Illustrations?\s*[-\u2013.]?\s*$", re.IGNORECASE)
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

_RE_NEST_EXPL_START = re.compile(r"^> \[!Explanation\]$")
_RE_NEST_SECONDARY_HDR = re.compile(r"^> \[!(Illustrations?|Exception[s]?)\]$")

_RE_HEADING_FOOTNOTE = re.compile(r"\[([0-9]{1,4})([^0-9\]][^\]]*)\]")
_RE_ANNOTATION_BRACKETS = re.compile(
    r"(?<!\[)\[(?!\s*\*)(?!\^)(?!\[)([^\[\]]*)\](?!\])", re.DOTALL
)
_RE_SUBST_LETTER = re.compile(r"\[([A-Z])\]")
_RE_BOILERPLATE_LINE = re.compile(
    r"^\s*(CONTENTS|SECTIONS|SCHEDULE|PREAMBLE|WHEREAS|An Act to |It is hereby|https?://\S+)",
    re.IGNORECASE,
)
_RE_URL_ONLY = re.compile(r"^\s*https?://\S+\s*$")
_RE_TOC_LINK = re.compile(r"^\s*\[.+?\]\(https?://")
_RE_ISOLATED_LABEL = re.compile(
    r"^\(([a-zA-Z0-9" + _BN_CONS + _BN_VOWELS + r"০-৯]+)\)\s*$"
)
_RE_ANNOTATION_BRACKETS_PREAMBLE = re.compile(r"(?<!\[)\[(?!\[)(?!\*\s*\*)([^\[\]]*)\](?!\])", re.DOTALL)  # never a [[wikilink]]
_RE_BDLAWS_ACT = re.compile(r"https?://bdlaws\.minlaw\.gov\.bd/act-(\d+)\.html")
# A Bengali case ending the source leaves outside the link anchor ("সংবিধান</a>ের"). A dependent vowel sign cannot
# start a word, so the ending belongs inside the link text: "[[..|সংবিধানের]]".
# A footnote number the source glues to the front of a heading ("84Part IXA", "32Chapter I"): it is the reference
# to the footnote, not part of the name. Returns the text without it and the "[^84]" reference ("" when none).
_RE_GLUED_FOOTNOTE = re.compile(r"^(\d{1,3})(?=[A-Za-z]{4})")


def _unglue_footnote(text: str) -> tuple[str, str]:
    m = _RE_GLUED_FOOTNOTE.match(text)
    return (text[m.end() :], f"[^{m.group(1)}]") if m else (text, "")


_RE_CASE_ENDING = re.compile(r"[ি-ৌ][ঀ-৿]*(?![ঀ-৿])")


def _act_link_ending(node: object) -> int:
    """Length of the case ending that follows an act link and belongs inside it, else 0."""
    nxt = getattr(node, "next_sibling", None)
    if not isinstance(nxt, NavigableString):
        return 0
    m = _RE_CASE_ENDING.match(str(nxt))
    return m.end() if m else 0


def _is_act_link(node: object) -> bool:
    if getattr(node, "name", None) != "a":
        return False
    return bool(_RE_BDLAWS_ACT.match(urljoin(BASE_URL, str(node.get("href", "")))))  # type: ignore[attr-defined]
_RE_FN_PREFIX = re.compile(r"^(\[\^\d+\])")
_RE_RESIDUAL_HTML = re.compile(r"</?(li|ol|ul|p|div|span|br)[^>]*>", re.IGNORECASE)

_MARGIN_CALLOUTS = [
    (re.compile(r"^Saving[s]?\s*[-\u2013.\u2014]+\s*(.*)", re.I), "Note", "Saving"),
    (re.compile(r"^Repeal[s]?\s*[-\u2013.\u2014]+\s*(.*)", re.I), "Warning", "Repeal"),
    (
        re.compile(r"^Commencement\s*[-\u2013.\u2014]+\s*(.*)", re.I),
        "Info",
        "Commencement",
    ),
    (
        re.compile(r"^Short\s+title\s*[-\u2013.\u2014]+\s*(.*)", re.I),
        "Info",
        "Short title",
    ),
]

_RE_STATUS_OMITTED = re.compile(r"^\[?[Oo]mitted\]?\.?\s*$")
_RE_STATUS_REPEALED = re.compile(r"^\[?[Rr]epealed\]?\.?\s*$")
_RE_PUNISHMENT = re.compile(
    r"(?<!\*)\b(shall be punished with|shall be punished|shall be liable|"
    r"shall be guilty of|imprisonment for life|rigorous imprisonment)\b(?!\*)",
    re.IGNORECASE,
)
# Notices such as "Omitted by section 3 of ..." start with a capital letter. Lower case "repealed by this
# Act" is ordinary statute text and must not be struck through.
_RE_OMITTED_INLINE = re.compile(r"\b(Omitted by [^.;\n]*[.;])")
_RE_REPEALED_INLINE = re.compile(r"\b(Repealed by [^.;\n]*[.;])")
_SEC_NUM = r"\d+[A-Za-zক-হ]?(?:-[A-Za-z0-9]+)?"
# "section 64" or a list such as "sections 64, 65, 66 and 445" / "sections 3 or 4" / "sections 5 to 9".
# A number after the first must not be a quantity ("sections 3 and 4 years").
# Horizontal whitespace: a reference list never runs across a line break, so a list-item number at the start
# of the next line ("section 2, or\n2. imports ...") is not read as one more section of the list.
_HS = r"[^\S\n]"
_RE_SECTION_REF = re.compile(
    rf"(?<!sub-)\b(sections?)\s+({_SEC_NUM}\b"
    rf"(?:(?:{_HS}*,{_HS}*(?:(?:and|or){_HS}+)?|{_HS}+(?:and|or|to){_HS}+){_SEC_NUM}\b"
    r"(?!\s+(?:years?|months?|days?|weeks?|times?|per\s*cent|percent|taka|rupees|hundred)))*)"
    r"(?P<tail>(?:\([A-Za-z0-9]{1,4}\))*)",
    re.IGNORECASE,
)
_RE_SEC_NUM_IN_LIST = re.compile(_SEC_NUM)
# Bengali: "ধারা ৩", "ধারা ১২৮, ১৩২, ১৩৪ ও ১৩৫", "ধারা ৫ হইতে ৯". "উপ-ধারা" (sub-section) is not a section,
# but a tail of it: "ধারা ৫(২)(ক)" and "ধারা ৫ এর উপ-ধারা (২) এর দফা (ক)" stay one reference.
_BN_PART = r"(?:উপ-?ধারা|উপ-?দফা|উপ-?অনুচ্ছেদ|দফা|প্যারা)"
_BN_TAIL = (
    r"(?P<tail>(?:\s*\([^()\s]{1,4}\))*"
    rf"(?:\s*(?:এর\s+|র\s+)?{_BN_PART}\s*(?:\([^()\s]{{1,4}}\))+(?:\s*(?:এর|র))?)*)"
)
_RE_SECTION_REF_BN = re.compile(
    r"(?<!উপ-)(?<!উপ)(ধারাসমূহ|ধারা|অনুচ্ছেদসমূহ|অনুচ্ছেদ|বিধিসমূহ|বিধি|প্রবিধানসমূহ|প্রবিধান)"
    rf"\s+({_SEC_NUM}"
    rf"(?:(?:{_HS}*,{_HS}*|{_HS}+(?:ও|এবং|বা|হইতে){_HS}+){_SEC_NUM}"
    r"(?!\s*(?:বৎসর|বছর|মাস|দিন|টাকা|শতাংশ|জন|টি))\b)*)" + _BN_TAIL
)
# Number first: "৫ ধারা", "৬ ও ৭ ধারার", "১২ নং ধারা". "ধারাবাহিক" (continuous) is not a section.
_RE_SECTION_REF_BN_POST = re.compile(
    rf"(?<![\d\w])(?P<nums>{_SEC_NUM}(?:(?:{_HS}*,{_HS}*|{_HS}+(?:ও|এবং|বা){_HS}+){_SEC_NUM})*)\s*(?:নং\s+)?"
    r"(?P<word>ধারা(?:সমূহ)?)(?:র|য়|য়ের|বলে|টি|গুলি)?(?![ঀ-৿])"
)
_RE_DEFINITION = re.compile(
    r"^(?:In this (?:Act|section|chapter|Part|Code)[,;]?\s+)?"
    r'"([^"]{1,80})"\s+(means?|includes?|denotes?)\s+(.+)',
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# YAML tag-inference
# ---------------------------------------------------------------------------

_TAG_KEYWORDS: dict[str, list[str]] = {
    "CriminalLaw": [
        "criminal",
        "penal code",
        "crime",
        "offence",
        "punishment",
        "magistrate",
        "prosecution",
        "accusation",
        "culpable",
    ],
    "CivilLaw": [
        "civil procedure",
        "civil court",
        "plaintiff",
        "defendant",
        "decree",
        "execution of decree",
        "suit",
    ],
    "ConstitutionalLaw": [
        "constitution",
        "fundamental right",
        "parliament",
        "republic",
        "citizenship",
        "sovereignty",
    ],
    "CommercialLaw": [
        "commerce",
        "contract",
        "sale of goods",
        "company",
        "partnership",
        "arbitration",
        "mercantile",
    ],
    "BankingAndFinance": [
        "bank",
        "banking",
        "financial institution",
        "loan",
        "credit",
        "monetary",
        "interest",
    ],
    "TaxLaw": [
        "tax",
        "revenue",
        "customs",
        "excise",
        "vat",
        "income tax",
        "duty",
        "tariff",
    ],
    "LabourLaw": [
        "labour",
        "labor",
        "employment",
        "worker",
        "factory",
        "wages",
        "trade union",
    ],
    "FamilyLaw": [
        "marriage",
        "divorce",
        "matrimonial",
        "guardian",
        "inheritance",
        "succession",
        "dower",
    ],
    "PropertyLaw": [
        "property",
        "land",
        "tenancy",
        "mortgage",
        "transfer of property",
        "registration",
    ],
    "EvidenceLaw": ["evidence", "witness", "proof", "admissibility"],
    "ProceduralLaw": [
        "procedure code",
        "jurisdiction",
        "appeal",
        "tribunal",
        "high court",
        "supreme court",
    ],
    "EnvironmentalLaw": [
        "environment",
        "pollution",
        "forest",
        "wildlife",
        "biodiversity",
        "conservation",
    ],
    "EducationLaw": [
        "education",
        "university",
        "school",
        "examination",
        "academic",
        "degree",
    ],
    "HealthLaw": [
        "health",
        "medical",
        "hospital",
        "drug",
        "medicine",
        "pharmacy",
        "epidemic",
    ],
    "AdministrativeLaw": [
        "government",
        "administration",
        "ministry",
        "officer",
        "public servant",
        "gazette",
    ],
    "MilitaryLaw": ["army", "navy", "air force", "military", "armed forces", "defence"],
    "SecurityLaw": [
        "security",
        "national security",
        "intelligence",
        "police",
        "special branch",
    ],
    "ImmigrationLaw": [
        "passport",
        "visa",
        "immigration",
        "citizenship",
        "nationality",
        "refugee",
    ],
    "InsuranceLaw": ["insurance", "insurer", "policy", "indemnity", "premium"],
    "IntellectualProperty": [
        "copyright",
        "patent",
        "trademark",
        "intellectual property",
        "design",
    ],
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def setup_logging(verbose: bool) -> logging.Logger:
    """@brief Configure root logger and return the 'bdlaw' logger.

    Uses RichHandler for coloured output when the rich package is available,
    otherwise falls back to a plain timestamped format.

    @param verbose Enable DEBUG level; INFO otherwise.
    @return Configured Logger instance.
    """
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
    """A single numbered or unnumbered section of an act.

    @param heading  Human-readable heading text (e.g. "Short title").
    @param url      Absolute URL to this section's permalink page.
    @param html     BeautifulSoup Tag for the section body HTML (div.txt-details).
    @param number   Parsed section number string (e.g. "4A", "৩"), or None.
    """

    heading: str
    url: str
    html: Tag
    number: Optional[str] = None


@dataclass
class ChapterItem:
    """A chapter heading node (e.g. "CHAPTER II: General Principles").

    @param name  Full chapter label including number and title.
    @param url   Link to chapter page, or "#" if not resolved.
    """

    name: str
    url: str = "#"


@dataclass
class SubChapterItem:
    """A lettered sub-chapter or thematic grouping within a chapter.

    @param letter         Single letter identifying the sub-chapter (e.g. "A").
    @param subtitle       Descriptive title of the sub-chapter.
    @param parent_number  Section number of the preceding section.
    @param url            Link, or "#" if not resolved.
    """

    letter: str
    subtitle: str
    parent_number: str
    url: str = "#"


@dataclass
class PartItem:
    """A top-level Part of an act (e.g. "PART III: Administration").

    @param name  Full label including part number and subtitle.
    @param url   Absolute URL to the part page.
    """

    name: str
    url: str


@dataclass
class PreambleItem:
    """The preamble text of an act, rendered as a Preamble callout.

    @param text  Cleaned plain-text preamble (no HTML, no footnote markers).
    """

    text: str


@dataclass
class FootnotesItem:
    """All footnotes for an act, rendered as a Footnotes section.

    @param notes  List of (footnote_number, markdown_text) pairs.
                  Links inside notes are already converted to [[wikilinks]].
    """

    notes: list[tuple[int, str]]


DocNode = (
    PartItem | ChapterItem | SubChapterItem | SectionItem | PreambleItem | FootnotesItem
)
"""Union type representing every possible structural node of an act document."""


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


class Scraper(BaseScraper):
    """bdlaws-specific HTTP client built on BaseScraper.

    Extends BaseScraper with BeautifulSoup parsing and bdlaws-specific
    headers. All rate limiting, caching, retry, and thread safety come
    from BaseScraper.
    """

    def __init__(
        self,
        delay: float = 0.3,
        max_retries: int = 4,
        timeout: int = 30,
        cache_dir: Optional[Path] = None,
        force_refresh: bool = False,
    ) -> None:
        """@param delay         Minimum seconds to wait between HTTP requests.
        @param max_retries     Maximum retry attempts on RequestException.
        @param timeout         Per-request socket timeout in seconds.
        @param cache_dir       Directory for raw HTML cache; None disables caching.
        @param force_refresh   Ignore cache hits and always fetch live.
        """
        super().__init__(
            delay=delay,
            max_retries=max_retries,
            timeout=timeout,
            cache_dir=cache_dir,
            force_refresh=force_refresh,
            logger_name="bdlaw",
            headers=HEADERS,
        )

    def fetch(self, url: str) -> BeautifulSoup:  # type: ignore[override]
        """Fetch a URL and return a parsed BeautifulSoup tree.

        @param url  Absolute URL to fetch.
        @return     Parsed HTML tree.
        """
        cached = self._read_cache(url)
        if cached is not None:
            logging.getLogger("bdlaw").debug("Cache hit: %s", url)
            return BeautifulSoup(cached, "lxml")
        html = self._fetch_raw(url)
        self._write_cache(url, html)
        return BeautifulSoup(html, "lxml")


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------


def _tag_to_md_text(node: object, scraper: Optional[Scraper] = None) -> str:
    """Recursively convert a BeautifulSoup node to Markdown text.

    Handles the following cases:
    - NavigableString  → NFC-normalised plain text.
    - <br>             → newline.
    - <a href="...">   → [[act-N\\|text]] wikilink for bdlaws internal links,
                         [num] bare ref for digit-only anchors, [text](url) otherwise.
    - Block elements   → wrapped with leading/trailing newlines.
    - All others       → recurse into children, concatenate.

    @param node     A BeautifulSoup Tag or NavigableString.
    @param scraper  Unused; reserved for future authenticated fetches.
    @return         Markdown string representation of the node subtree.
    """
    if isinstance(node, NavigableString):
        s = str(node)
        prev = node.previous_sibling
        if prev is not None and _is_act_link(prev):
            s = s[_act_link_ending(prev) :]
        return unicodedata.normalize("NFC", s)
    if not hasattr(node, "name") or not node.name:
        return ""
    if node.name == "br":
        return "\n"
    if node.name == "a":
        href: str = node.get("href", "")  # type: ignore[union-attr]
        inner = "".join(_tag_to_md_text(c, scraper) for c in node.children)  # type: ignore[union-attr]
        text = " ".join(inner.split())
        if not text:
            return ""
        if href:
            full_href = href if href.startswith("http") else urljoin(BASE_URL, href)
            m = _RE_BDLAWS_ACT.match(full_href)
            if m:
                clean_text = re.sub(r"[\].]+$", "", text).strip()
                end = _act_link_ending(node)
                if end:
                    clean_text += str(node.next_sibling)[:end]  # type: ignore[union-attr]
                return f"[[act-{m.group(1)}\\|{clean_text}]]"
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
    """Return NFC-normalised Unicode string without C1 control characters or byte-order marks."""
    return unicodedata.normalize("NFC", re.sub("[\x80-\x9f]", "", text).replace(chr(0xFEFF), ""))


def _get_text(tag: Tag) -> str:
    """Extract all text from a Tag using newline as separator, then NFC-normalise."""
    return _normalise(tag.get_text(separator="\n"))


def _normalize_secnum(num: str) -> str:
    """Collapse trailing hyphen-suffix into the number (e.g. '4-A' → '4A')."""
    return re.sub(r"-([A-Za-z0-9]+)$", r"\1", num)


def _secnum_to_block_id(num: str) -> str:
    """Convert a section number to a lowercase alphanumeric Obsidian block ID.

    Bengali digits are translated to ASCII, Bengali consonants to their
    _BN_LETTER_TO_LATIN equivalents, and all non-alphanumeric characters dropped.
    Example: '৪ক' → '4a', '3A' → '3a'.

    @param num  Raw section number string (may contain Bengali digits/letters).
    @return     Lowercase ASCII block ID suitable for ^sec-<id> anchors.
    """
    if re.fullmatch(r"\d+[A-Za-z]{0,2}-\d+[A-Za-z]{0,2}", num):
        return num.lower()  # a range of sections keeps its hyphen: "23-24"
    result = num.translate(BENGALI_DIGIT_MAP)
    result = "".join(_BN_LETTER_TO_LATIN.get(ch, ch) for ch in result)
    return re.sub(r"[^a-z0-9]", "", result.lower())


def extract_section_number(raw_text: str) -> Optional[str]:
    """Parse the section number from the raw text of a section body.

    Strips standalone digit lines (page numbers), footnote markers, and
    leading punctuation before trying patterns for both Bengali and Latin
    section numbering schemes (bracketed, plain, and clause forms).

    @param raw_text  Raw get_text() output from a div.txt-details element.
    @return          Normalised section number string, or None if not detected.
    """
    cleaned = "\n".join(
        line
        for line in raw_text.splitlines()
        if not re.fullmatch(r"\s*[\d\u09E6-\u09EF]+\s*", line)
    )
    cleaned = re.sub(r"\[\d+\]", "", cleaned)
    stripped = re.sub(r"^[.\s]+", "", cleaned.lstrip())
    if stripped.startswith("[") and stripped.endswith("]"):
        stripped = stripped[1:-1].lstrip()
    rng = RE_SECTION_NUM_RANGE.match(stripped)
    if rng:
        return f"{rng.group(1)}-{rng.group(2)}"
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
            return _normalize_secnum(m.group(1))
    return None


def parse_footnotes(soup: BeautifulSoup) -> Optional[FootnotesItem]:
    """Extract all footnotes from a div.footnoteListAll container.

    Each li.footnoteList is expected to contain an h6 > sup with the footnote
    number, followed by mixed content (text nodes and <a> links). Links are
    converted to [[wikilinks]] via _tag_to_md_text.

    @param soup  BeautifulSoup tree of the print page.
    @return      FootnotesItem with (number, markdown_text) pairs, or None.
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
        parts: list[str] = []
        for node in li.children:
            if hasattr(node, "name") and node.name == "h6":
                continue
            parts.append(_tag_to_md_text(node))
        text = _normalise(re.sub(r"[\xa0​\s]+", " ", "".join(parts)).strip())
        if text:
            notes.append((num, text))
    return FootnotesItem(notes=notes) if notes else None


def parse_act_date(soup: BeautifulSoup) -> Optional[str]:
    """Parse the enacted date from the site's publish-date paragraph.

    Handles two formats:
    - Bengali digits: "[ ১০ এপ্রিল, ২০২৬ ]"  → "2026-04-10"
    - English ordinal: "[ 11th September, 1836 ]" → "1836-09-11"

    @param soup  BeautifulSoup tree of the ToC or print page.
    @return      ISO 8601 date string (YYYY-MM-DD), or None if not found/parseable.
    """
    p = soup.find("p", class_="publish-date")
    if not p:
        return None
    raw = re.sub(r"[\[\]\s]+", " ", p.get_text()).strip()
    # Bengali digits: "১০ এপ্রিল, ২০২৬"
    m = re.match(r"([০-৯]+)\s+(\S+?)(?:,\s*|\s+)([০-৯]+)\s*$", raw)
    if m:
        month_num = _BN_MONTH_MAP.get(m.group(2).strip(","))
        if month_num:
            try:
                day = int(m.group(1).translate(BENGALI_DIGIT_MAP))
                year = int(m.group(3).translate(BENGALI_DIGIT_MAP))
                return f"{year:04d}-{month_num:02d}-{day:02d}"
            except ValueError:
                pass
    # English: "11th September, 1836"
    m = re.match(
        r"(\d+)(?:st|nd|rd|th)?\s+(\w+?)(?:,\s*|\s+)(\d+)\s*$", raw, re.IGNORECASE
    )
    if m:
        month_num = _EN_MONTH_MAP.get(m.group(2).lower())
        if month_num:
            try:
                day = int(m.group(1))
                year = int(m.group(3))
                return f"{year:04d}-{month_num:02d}-{day:02d}"
            except ValueError:
                pass
    return None


def _parse_date_from_preamble_text(text: str) -> Optional[str]:
    """Extract an enacted date embedded in preamble prose.

    Tries patterns from most to least specific:
    1. "corresponding to the fourth day of November, 1972"
    2. "the fourth day of November, 1972 A.D."
    3. "the fourth day of November, 1972"

    Day can be a word ordinal ("fourth") or a numeric ordinal ("4th").
    Only recognises English month names; skips Bengali calendar months.

    @param text  Cleaned preamble text.
    @return      ISO 8601 date string (YYYY-MM-DD), or None if not found.
    """
    patterns = [
        r"corresponding\s+to\s+the\s+([\w-]+)\s+day\s+of\s+(\w+),?\s+(\d{4})",
        r"\bthe\s+([\w-]+)\s+day\s+of\s+(\w+),?\s+(\d{4})\s+A\.?D\.?",
        r"\bthe\s+([\w-]+)\s+day\s+of\s+(\w+),?\s+(\d{4})",
    ]
    for pat in patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            day_raw = m.group(1).lower()
            month_num = _EN_MONTH_MAP.get(m.group(2).lower())
            if not month_num:
                continue
            day = _ORDINAL_WORDS.get(day_raw)
            if day is None:
                nm = re.match(r"(\d+)(?:st|nd|rd|th)?$", day_raw)
                if nm:
                    day = int(nm.group(1))
            if day and 1 <= day <= 31:
                try:
                    return f"{int(m.group(3)):04d}-{month_num:02d}-{day:02d}"
                except ValueError:
                    pass
    return None


def parse_act_description(soup: BeautifulSoup) -> str:
    """Return the meta description content from a page, trying common tag variants.

    Tries in order: name=description, name=Description, property=og:description,
    name=og:description. Returns the first non-empty content found, normalised
    to a single space-separated line.

    @param soup  BeautifulSoup tree of the ToC page.
    @return      Description string, or "" if none found.
    """
    for attrs in (
        {"name": "description"},
        {"name": "Description"},
        {"property": "og:description"},
        {"name": "og:description"},
    ):
        meta = soup.find("meta", attrs=attrs)
        if meta:
            content = meta.get("content", "").strip()
            if content:
                return " ".join(content.split())
    return ""


def parse_toc(soup: BeautifulSoup, base: str) -> dict[str, str]:
    """Build a heading → absolute-URL map from the act's table of contents page.

    Finds all p.act-section-name links inside section.search-here and resolves
    relative hrefs against base.

    @param soup  BeautifulSoup tree of the ToC page (act-N.html).
    @param base  Base URL for resolving relative links (BASE_URL).
    @return      Dict mapping section heading text to its absolute URL.
    @raises ValueError  If section.search-here is not found.
    """
    container = soup.find("section", class_="search-here")
    if not container:
        raise ValueError("ToC page: cannot find '.search-here' container")
    section_map: dict[str, str] = {}
    for p in container.find_all("p", class_="act-section-name"):
        a = p.find("a")
        if a and a.get("href"):
            heading = _normalise(
                " ".join(a.get_text(separator=" ", strip=True).split())
            )
            section_map[heading] = urljoin(base, a["href"])
    return section_map


# "1[Title", "THE 1[***] X", "1[A B] C": the site puts the footnote digit in front of
# a bracket that is often unbalanced (closing bracket missing or stray)
_RE_TITLE_OMITTED = re.compile(r"\s*\d{1,4}\s*\[\s*(?:\*\s*){3}\]")
_RE_TITLE_LEAD_NUM = re.compile(r"^\s*(\d{1,4})\s*[\[*]")
_RE_TITLE_MID_NUM = re.compile(r"\d{1,4}\s*\[(?!\s*(?:\*\s*){3}\])")


def _clean_title(raw: str) -> str:
    """Strip footnote digits and every stray bracket from an act title.

    @param raw  Text of the title h3.
    @return     Title without footnote markers or brackets.
    """
    raw = _RE_HEADING_FOOTNOTE.sub(r"\2", raw)
    raw = _RE_TITLE_OMITTED.sub("", raw)
    raw = _RE_TITLE_LEAD_NUM.sub("", raw)
    raw = _RE_TITLE_MID_NUM.sub("", raw)
    raw = re.sub(r"^(\d+)([A-Z])", r"\2", raw.strip())
    return re.sub(r"\s{2,}", " ", re.sub(r"[\[\]]", "", raw)).strip()


def parse_act_title(soup: BeautifulSoup) -> str:
    """Extract the act title from the section.bg-act-section h3 element.

    Strips footnote markers (e.g. "[1]"), outer brackets, and leading
    numeric prefixes that appear in the site's OCR output.

    @param soup  BeautifulSoup tree of either the ToC or print page.
    @return      Clean title string, or "" if not found.
    """
    section = soup.find("section", class_="bg-act-section")
    if section:
        h3 = section.find("h3")
        if h3:
            raw = _normalise(" ".join(h3.get_text().split()))
            return _clean_title(raw)
    return ""


def _extract_title_footnote_num(soup: BeautifulSoup) -> Optional[int]:
    """Return the footnote number embedded in the act title h3, if any.

    The site sometimes encodes a footnote reference directly in the title
    as a leading digit prefix (e.g. "7The Penal Code"). This number is
    appended as [^N] to the rendered Markdown title.

    @param soup  BeautifulSoup tree of the ToC or print page.
    @return      Footnote number integer, or None.
    """
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
            m3 = _RE_TITLE_LEAD_NUM.match(raw) or _RE_TITLE_MID_NUM.search(raw) or _RE_TITLE_OMITTED.search(raw)
            if m3:
                return int(re.search(r"\d+", m3.group(0)).group(0))
    return None


# "[Omitted](c)" inside a heading would read as a link to "c"
_RE_LINK_LOOKALIKE = re.compile(r"\[(?!\^)([^\[\]]*)\]\(")


def _clean_heading(raw: str) -> str:
    """Remove footnote markers, outer brackets, and section-number prefixes from a heading string.

    @param raw  Raw heading text from div.txt-head.
    @return     Cleaned heading suitable for use in Markdown headings.
    """
    refs: list[int] = []

    def _grab(m: re.Match) -> str:
        refs.append(int(m.group(1)))
        return m.group(2)

    h = _RE_HEADING_FOOTNOTE.sub(_grab, raw).strip()
    # "33 [Title]": the footnote number sits in front of the bracketed title
    lead = re.match(r"^([0-9]{1,4})\s*\[(.*)\]$", h, re.S)
    if lead:
        refs.append(int(lead.group(1)))
        h = lead.group(2).strip()
    # "word 18 [Title]": the number sits mid-heading, in front of a bracketed word
    def _mid(m: re.Match) -> str:
        refs.append(int(m.group(2)))
        return m.group(1) + "[" + m.group(3) + "]"

    h = re.sub(r"(^|\s)(\d{1,4})\s*\[([^\]\d][^\]]*)\]", _mid, h)
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1].strip()
    h = re.sub(r"^\[\d+\]\s*", "", h)
    h = re.sub(r"\[\s*\*\s*\*\s*\*\s*\]", "[ * * * ]", h)  # omission mark, as in the body
    h = re.sub(r"\[\s*([^\[\]*^]*?)\s*\]", r"\1", h)  # amendment brackets inside a title
    h = re.sub(r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)?(?:\.[A-Za-z0-9]+)?\.\s+", "", h)
    return h.strip() + "".join(f"[^{n}]" for n in dict.fromkeys(refs))


def parse_preamble(soup: BeautifulSoup) -> str:
    """Extract and clean the preamble text of an act.

    Searches for div.preamble-content first, then falls back to the first
    section.padding-bottom-20 that is neither the striped body nor the title
    block. Post-processing strips footnote/omission markers, standalone digit
    lines (page numbers), annotation brackets, and joins continuation lines
    split across paragraph elements.

    @param soup  BeautifulSoup tree of the ToC or print page.
    @return      Cleaned multi-line preamble string, or "" if not found.
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
    preamble_div = copy.copy(preamble_div)
    for span in preamble_div.find_all("span", class_="footnote"):  # type: ignore[union-attr]
        sup = span.find("sup")
        if sup and sup.get_text(strip=True).isdigit():
            span.replace_with(f"\u27e6{int(sup.get_text(strip=True))}\u27e7")  # survives bracket cleanup
    raw = _normalise(_tag_to_md_text(preamble_div))  # keeps the links to other acts; breaks only at real blocks
    raw = re.sub(r"\[\d+\]|\[\*+\s*\*+\]|\^\S+\^", "", raw)
    raw = re.sub(r"[^\S\n]+", " ", raw)
    raw = "\n".join(l.strip() for l in raw.splitlines())
    raw = "\n".join(
        l for l in raw.splitlines() if l and not re.fullmatch(r"[\d\u09E6-\u09EF]+", l)
    )
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
    # a lone "-" or ":" line under text would turn that text into a heading
    raw = "\n".join(l for l in raw.split("\n") if not re.fullmatch(r"\s*[-:=\u2013\u2014]+\s*", l))
    # a reference alone on its line belongs to the line after it, as a trailing mark
    raw = re.sub("^\u27e6(\\d+)\u27e7\n?([^\n]+)", "\\2\u27e6\\1\u27e7", raw, flags=re.M)
    return re.sub("\u27e6(\\d+)\u27e7", r"[^\1]", raw)


def parse_detail(soup: BeautifulSoup, base: str) -> Generator[DocNode, None, None]:
    """Yield DocNode items by walking the print page's structured layout.

    The print page (act-print-N.html) wraps content in div.row elements inside
    section.padding-bottom-20.bg-striped. Each row may contain:
    - div.act-part-group   → PartItem
    - div.act-chapter-group → ChapterItem
    - p.act-section-head   → SubChapterItem
    - div.txt-details      → SectionItem (with optional div.txt-head heading)

    Boilerplate rows (CONTENTS tables, URL-only rows) are silently skipped.
    Yields nodes in document order.

    @param soup  BeautifulSoup tree of the print page.
    @param base  Base URL for resolving relative hrefs.
    @return      Generator of DocNode instances.
    @raises ValueError  If the main container is not found.
    """
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
                part_num, part_ref = _unglue_footnote(_normalise(link.get_text(strip=True)))
                part_url = urljoin(base, link["href"])
                name_p = part_group.find("p", class_="act-part-name")
                subtitle = _normalise(name_p.get_text(strip=True)) if name_p else ""
                full_name = (f"{part_num}: {subtitle}" if subtitle else part_num) + part_ref
                yield PartItem(name=full_name, url=part_url)

        chapter_group = row.find("div", class_="act-chapter-group")
        if chapter_group:
            chap_no_p = row.find("p", class_="act-chapter-no")
            chap_name_p = row.find("p", class_="act-chapter-name")
            if chap_no_p:
                chap_no, chap_ref = _unglue_footnote(_normalise(chap_no_p.get_text(strip=True)))
                chap_name = (
                    _normalise(chap_name_p.get_text(strip=True)) if chap_name_p else ""
                )
                glued = re.match(r"^(\d{1,3})(?=[ঀ-৿])", chap_name)
                if glued:
                    chap_name = chap_name[glued.end():] + f"[^{glued.group(1)}]"
                full_chap = (f"{chap_no}: {chap_name}" if chap_name else chap_no) + chap_ref
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
                yield SubChapterItem(
                    letter="", subtitle=raw, parent_number=last_section_num
                )

        head_div = row.find("div", class_="txt-head")
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
            number = extract_section_number(raw_text)
            rng = re.fullmatch(r"\d+[A-Za-z]{0,2}-(\d+[A-Za-z]{0,2})", number or "")
            if rng:
                heading = re.sub(rf"^[-–]\s*{rng.group(1)}\.\s*", "", heading)
            if number:
                last_section_num = number
            if not _is_boilerplate(raw_text):
                yield SectionItem(
                    heading=heading, url="#", html=details_div, number=number
                )


def resolve_urls(
    nodes: list[DocNode], toc_map: dict[str, str], base: str
) -> list[DocNode]:
    """Resolve "#" placeholder URLs in SectionItems using the ToC heading map.

    First tries an exact case-insensitive match; if that fails, tries substring
    and suffix matching. Mutates nodes in place.

    @param nodes    List of DocNode items from parse_detail.
    @param toc_map  Heading → URL dict from parse_toc.
    @param base     Base URL (unused here, kept for API symmetry).
    @return         The same nodes list (mutated).
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
# Content cleaning pipeline
# ---------------------------------------------------------------------------


def _transform_footnotes(text: str) -> str:
    """Convert inline footnote markers to Markdown footnote reference syntax.

    - "[**]" / "[***]" → "[ * * * ]"  (omission placeholder)
    - "[N]"            → "[^N]"        (numbered footnote reference)
    - "^word^"         → ""            (superscript noise from OCR, never spanning a bracket)

    @param text  Raw section text.
    @return      Text with footnote markers normalised.
    """
    # Superscript noise first, and never across a bracket: "[11]২০১২[12]" holds two real references.
    text = re.sub(r"\^[^\s\[\]^]+\^", "", text)
    text = re.sub(r"\[\*+(?:\s*\*+)+\]", "[ * * * ]", text)
    # "[28[29]text" is two adjacent references, the first with its bracket left open
    # Footnote numbers are ASCII digits; "[২০১২]" is bracketed text, not a footnote.
    text = re.sub(r"\[([0-9]+)\[([0-9]+)\]", r"[\1][\2]", text)
    text = re.sub(r"\[([0-9]+)\]", r"[^\1]", text)
    return text


_RE_OMISSION_MARKER = re.compile(r"\[\^(\d+)\]\s*\[\s*\*\s*\*\s*\*\s*\]")
_RE_OMITTED_TERM = re.compile(
    r'[Tt]he words?\s+[“"]([^”"]+)[”"]\s+(?:was|were)\s+omitted',
    re.IGNORECASE,
)


def _resolve_omissions(text: str, footnote_map: dict) -> str:
    """Replace [^N][ * * * ] omission markers with the original omitted term.

    Looks up footnote N in footnote_map; if the footnote text matches the
    pattern 'The word "X" was omitted', renders [^N][~~"X"~~ omitted].
    Falls back to [^N][omitted] when the term cannot be extracted.

    @param text          Section Markdown text after footnote transformation.
    @param footnote_map  Dict of footnote_number → footnote_text.
    @return              Text with omission markers resolved.
    """

    def replace(m: re.Match) -> str:
        num = int(m.group(1))
        note = footnote_map.get(num, "")
        term = _RE_OMITTED_TERM.search(note)
        if term:
            return f'[^{num}][~~"{term.group(1)}"~~ omitted]'
        return f"[^{num}][omitted]"

    return _RE_OMISSION_MARKER.sub(replace, text)


_RE_LEADING_FN = re.compile(r"^(\[\^\d+\])\s*(?!\[)(?!:)", re.MULTILINE)


def _relocate_leading_footnotes(text: str) -> str:
    """Move [^N] footnote refs from paragraph start to end of that paragraph.

    The site sometimes places editorial footnote markers before the text they
    annotate. Obsidian renders them better at the end of the annotated passage.
    Skips relocation when [^N] is followed by "[" (i.e. it's part of an
    omission construct) or ":" (i.e. it's a footnote definition).

    @param text  Section Markdown text.
    @return      Text with leading footnote refs relocated to paragraph ends.
    """
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        m = _RE_LEADING_FN.match(lines[i])
        if m:
            fn_marker = m.group(1)
            rest = lines[i][m.end() :]
            para = [rest] if rest.strip() else []
            i += 1
            while (
                i < len(lines)
                and lines[i].strip()
                and not _is_block_break(lines[i].strip())
            ):
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
    """Return the inner content of an [annotation] bracket, whitespace-collapsed."""
    return " ".join(m.group(1).split())


_RE_WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")


def _strip_annotation_brackets(text: str) -> str:
    """Strip editorial [annotation] brackets while preserving [[wikilinks]].

    Uses a stash-and-restore strategy so that _RE_ANNOTATION_BRACKETS never
    sees the interior of a wikilink:
    1. Replace all [[...]] with NUL-delimited placeholders.
    2. Apply _RE_ANNOTATION_BRACKETS to strip [editorial] brackets.
    3. Restore wikilinks from the stash.

    @param text  Markdown text that may contain both [[wikilinks]] and
                 [editorial annotations].
    @return      Text with annotation brackets stripped, wikilinks intact.
    """
    wikilinks: list[str] = []

    def _stash(m: re.Match) -> str:
        wikilinks.append(m.group(0))
        return f"\x00wl{len(wikilinks)-1}\x00"

    text = _RE_WIKILINK.sub(_stash, text)
    text = _RE_ANNOTATION_BRACKETS.sub(_handle_annotation_bracket, text)
    for i, wl in enumerate(wikilinks):
        text = text.replace(f"\x00wl{i}\x00", wl)
    return text


def _strip_leading_section_num(text: str) -> str:
    """Remove the section number prefix from the start of section body text.

    Preserves any leading [^N] footnote ref so it can be relocated later.
    Handles Bengali and Latin numbering in plain, bracketed, and clause forms.

    @param text  Raw section body text (already NFC-normalised).
    @return      Text with the leading section number stripped, or the original
                 text unchanged if no recognised pattern is found.
    """
    stripped = text.lstrip()
    fn_prefix = ""
    m_fn = _RE_FN_PREFIX.match(stripped)
    if m_fn:
        fn_prefix = m_fn.group(1)
        stripped = stripped[m_fn.end() :].lstrip()
    stripped = re.sub(r"^[.\s]+", "", stripped)

    def _try_strip(s: str) -> Optional[str]:
        rng = RE_SECTION_NUM_RANGE.match(s)
        if rng:
            return s[rng.end() :]
        for pat in (RE_BN_SECTION_NUM_BRACKET, RE_BN_SECTION_NUM_PLAIN):
            m = pat.match(s)
            if m:
                return s[m.end() :]
        m = RE_BN_SECTION_NUM_CLAUSE.match(s)
        if m:
            dot_pos = next(i for i, c in enumerate(s) if c in _BN_DANDA)
            return s[dot_pos + 1 :]
        for pat in (RE_SECTION_NUM_BRACKET, RE_SECTION_NUM_PLAIN):
            m = pat.match(s)
            if m:
                return s[m.end() :]
        m = RE_SECTION_NUM_CLAUSE.match(s)
        if m:
            return s[s.index(".") + 1 :]
        return None

    result = _try_strip(stripped)
    if result is not None:
        return fn_prefix + result
    return (fn_prefix + stripped) if fn_prefix else text


def _strip_outer_brackets(text: str) -> str:
    """Strip a single pair of wrapping square brackets if they span the full text.

    Uses a depth counter to ensure only truly enclosing brackets are removed.
    A bracket pair like "[a] or [b]" is left intact because the first [ closes
    before the end of the string.

    @param text  Stripped text to examine.
    @return      Unwrapped text, or the original if not fully wrapped.
    """
    text = text.strip()
    if not text.startswith("[") or not text.endswith("]"):
        return text
    depth = 0
    for i, ch in enumerate(text):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if depth == 0:
            if i == len(text) - 1:
                return text[1:-1].strip()
            return text
    return text


def _join_isolated_labels(text: str) -> str:
    """Join an isolated list label like "(a)" onto the following content line.

    Prevents the formatter from treating a label that appears alone on a line
    (due to HTML structure) as a new list item without content.

    @param text  Section text after footnote and bracket transforms.
    @return      Text with isolated labels merged onto their following line.
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


def _is_block_break(s: str) -> bool:
    """Return True if the line starts a new logical block that should not be merged.

    Used by continuation-joining functions to stop merging when they hit a
    callout header, list item, ordinal item, or margin note.

    @param s  A single stripped line of text.
    @return   True if the line starts a new block.
    """
    return bool(
        s.startswith("**")
        or s.startswith(">")
        or s.startswith("#")
        or _classify_item(s) is not None
        or _RE_ORDINAL_ITEM.match(s)
        or _RE_EXPL_NUMBERED.match(s)
        or _RE_EXPL_SINGLE.match(s)
        or _RE_EXCEP_NUMBERED.match(s)
        or _RE_EXCEP_SINGLE.match(s)
        or _RE_ILLUSTRATIONS.match(s)
        or _RE_PROVIDED_STANDALONE.match(s)
        or any(p.match(s) for p, _, _ in _MARGIN_CALLOUTS)
    )


def _join_fragment_lines(text: str) -> str:
    """Merge lines that start with a connector word ("or", "and", "namely", etc.)
    onto the preceding line, then absorb the following non-block line too.

    Handles the site's habit of splitting "..., or" / "..., and" / "..., namely:"
    across separate paragraph elements.

    @param text  Section text after label joining.
    @return      Text with connector-led fragments merged.
    """
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        if _RE_CONNECTOR.match(lines[i]) and out:
            out[-1] = out[-1].rstrip() + " " + lines[i].strip()
            i += 1
            while i < len(lines) and not lines[i].strip():
                i += 1
            if (
                i < len(lines)
                and lines[i].strip()
                and not _is_block_break(lines[i].strip())
            ):
                out[-1] = out[-1].rstrip() + " " + lines[i].strip()
                i += 1
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def _strip_unmatched_open_brackets(text: str) -> str:
    """Remove a leading "[" from lines where it has no closing "]" on the same line.

    Targets orphaned annotation bracket openers left after annotation stripping.
    Preserves [^N] footnote refs, [* * *] omission markers, and [[wikilinks]].

    @param text  Section text.
    @return      Text with unmatched leading brackets removed.
    """
    lines = text.splitlines()
    out = []
    for line in lines:
        s = line.lstrip()
        if (
            s.startswith("[")
            and not s.startswith("[^")
            and not s.startswith("[ *")
            and not s.startswith("[[")
            and "]]" not in s
            and "]" not in s
        ):
            out.append(s[1:])
        else:
            out.append(line)
    return "\n".join(out)


def _fix_omission_marks(text: str) -> str:
    """Write the omission placeholder "[***]" as "[ * * * ]" so it is not read as bold markers."""
    return re.sub(r"\[\s*\*\s*\*\s*\*\s*\]", "[ * * * ]", text)


def _drop_unmatched_closers(text: str) -> str:
    """Remove editorial brackets that survived stripping.

    Two kinds are removed: a "]" that closes no "[", and a "[" ... "]" pair whose ends are on different
    lines. Links, wikilinks, footnote references and omission marks never span lines; a bracket that does
    is an amendment mark around several list items.
    """
    drop: set[int] = set()
    stack: list[int] = []
    for i, ch in enumerate(text):
        if ch == "[":
            stack.append(i)
        elif ch == "]":
            if not stack:
                drop.add(i)
            else:
                start = stack.pop()
                if "\n" in text[start:i]:
                    drop.update((start, i))
    # "[[[act-1|x]]": an editorial "[" left open in front of a wikilink
    drop.update(i for i in stack if text.startswith("[[[", i))
    return "".join(ch for i, ch in enumerate(text) if i not in drop)


def _check_section_status(raw: str) -> Optional[str]:
    """Check if an entire section body is an omitted/repealed status marker.

    @param raw  Full stripped section body text.
    @return     A pre-formatted Obsidian Warning callout string, or None.
    """
    s = raw.strip()
    if _RE_STATUS_OMITTED.match(s):
        return "> [!Warning] Omitted\n> This section has been omitted."
    if _RE_STATUS_REPEALED.match(s):
        return "> [!Warning] Repealed\n> This section has been repealed."
    return None


def _classify_item(line: str) -> Optional[str]:
    """Classify a line as a list item type, or None if not a list item.

    Strips leading [^N] footnote refs and a bare "[" wrapper before pattern
    matching so items like "[^7](a) text" are correctly classified.

    @param line  A single line of section text.
    @return      One of "decimal", "alpha", "roman", "bn_decimal", "bn_alpha",
                 "bn_vowel", or None.
    """
    s = line.strip()
    s = re.sub(r"^(\[\^\d+\]\s*)+", "", s)
    if s.startswith("[") and not s.startswith("[[") and not s.startswith("[^"):
        s = s[1:]
    if RE_BN_DECIMAL_ITEM.match(s):
        return "bn_decimal"
    if RE_BN_ALPHA_ITEM.match(s):
        return "bn_alpha"
    if RE_BN_VOWEL_ITEM.match(s):
        return "bn_vowel"
    if RE_DECIMAL_ITEM.match(s):
        return "decimal"
    m_roman = RE_ROMAN_ITEM.match(s)
    if m_roman and len(m_roman.group(1)) > 1:
        return "roman"
    m_alpha = RE_ALPHA_ITEM.match(s)
    if m_alpha:
        return "roman" if m_alpha.group(1).lower() == "i" else "alpha"
    if RE_ALPHA_DOUBLE_ITEM.match(s):
        return "alpha"
    return None


def _item_marker(line: str) -> str:
    """Return the original list marker of an item line, e.g. "(a)", "(১)", "(ক)", "(iv)".

    The Markdown output renumbers every list as 1. 2. 3., which loses the
    legal numbering. The marker is returned so it can be kept beside the
    item (see convert_lists) and rebuilt faithfully when rendering to HTML.

    @param line  A line classified as a list item by _classify_item.
    @return      The marker including its parentheses, or "" when none matches.
    """
    s = re.sub(r"^(\[\^\d+\]\s*)+", "", line.strip())
    if s.startswith("[") and not s.startswith("[[") and not s.startswith("[^"):
        s = s[1:]
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
            return f"({m.group(1)})"
    m = RE_ALPHA_DOUBLE_ITEM.match(s)
    return f"({m.group(1) * 2})" if m else ""


# Collected per act while building Markdown: one record per list item whose marker
# was replaced by a counter. scrape_act writes it next to the act as numbering JSON.
_NUMBERING = threading.local()


def _record_numbering(marker: str, rendered: int, depth: int, kind: str) -> None:
    items = getattr(_NUMBERING, "items", None)
    if items is not None:
        items.append(
            {"n": len(items) + 1, "marker": marker, "rendered": f"{rendered}.", "depth": depth, "type": kind}
        )


def _extract_item_content(line: str) -> str:
    """Strip the list marker from a line, preserving all leading [^N] footnote refs.

    @param line  A line classified as a list item by _classify_item.
    @return      The item's text content without its "(marker)" prefix.
    """
    s = line.strip()
    fn_prefix = ""
    while True:
        fn_m = re.match(r"^(\[\^\d+\])\s*", s)
        if fn_m:
            fn_prefix += fn_m.group(1) + " "
            s = s[fn_m.end() :]
        else:
            break
    if s.startswith("[") and not s.startswith("[[") and not s.startswith("[^"):
        s = s[1:]
    for pat in (
        RE_ALPHA_ITEM,
        RE_ROMAN_ITEM,
        RE_DECIMAL_ITEM,
        RE_BN_ALPHA_ITEM,
        RE_BN_DECIMAL_ITEM,
        RE_BN_VOWEL_ITEM,
        RE_ALPHA_DOUBLE_ITEM,
    ):
        m = pat.match(s)
        if m:
            return fn_prefix + m.group(2).strip()
    return fn_prefix + s


_RE_INLINE_ITEM_SEP = re.compile(
    r"\s*[;,]\s+(?:(?:and|or)\s+)?\(([a-zA-Z]|\d+)\)(?=\s)",
)
# An amended sub-clause glued to the sentence before it: "...Republic. [18](3)The State shall ...". The footnote
# mark (if any) and "(3)" start a new line; the capital letter right after the bracket marks it as a clause.
# "(3)The State ..." at the start of a line: the marker has no space after it, so give it one and it reads as a list item.
_RE_MARKER_NO_SPACE = re.compile(r"(?m)^((?:\[\^?\d+\][ \t]*)?\((?:\d+[A-Za-z]{0,2}|[a-z])\))(?=[A-Z“\"])")
_RE_GLUED_SUBCLAUSE = re.compile(r"(?<=[.;:])[ \t]+((?:\[\^?\d+\][ \t]*)?\((?:[a-z]|\d+)\))(?=[A-Z])")


# A quoted new section runs its title and first sub-clause together: "77A. Title.-(1) text".
# "(2)" on the next line is already a list item, so "(1)" has to be one too.
_RE_HEADING_THEN_ONE = re.compile(
    r"^([“\"]?\d+[A-Za-z]{0,2}\.\s[^()|\n]{3,150}?\.)\s*-?\s*\((1|১)\)\s+(\S.*)$"
)
_RE_NEXT_IS_TWO = re.compile(r"^\s*(?:\d+\.\s*<!--n:)?\((?:2|২)\)")


def _split_heading_before_first_subclause(text: str) -> str:
    """Move '(1) text' onto its own line when it follows a section title on the same line.

    Only when the next non-blank line starts with '(2)', so ordinary sentences stay as they are.
    """
    lines = text.split("\n")
    for i, line in enumerate(lines):
        m = _RE_HEADING_THEN_ONE.match(line)
        if not m:
            continue
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j < len(lines) and _RE_NEXT_IS_TWO.match(lines[j]):
            lines[i] = f"{m.group(1)}\n\n({m.group(2)}) {m.group(3)}"
    return "\n".join(lines)


def _split_inline_list_items(text: str) -> str:
    """Split inline sub-clauses like '...; (d) text' onto separate lines.

    Handles patterns where list items are written inline separated by semicolons
    or commas, e.g. '...unsound mind ; (d) is or is deemed... ; and (e) has not...'
    """
    # Table rows must stay on one line: a newline inside a cell would break the row.
    split = "\n".join(
        line
        if line.lstrip().startswith("|")
        else _RE_GLUED_SUBCLAUSE.sub(lambda m: "\n" + m.group(1), _RE_INLINE_ITEM_SEP.sub(lambda m: f"\n({m.group(1)})", line))
        for line in text.split("\n")
    )
    return _RE_MARKER_NO_SPACE.sub(r"\1 ", split)


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

        if not stripped or _classify_item(stripped) or _is_block_break(stripped):
            i += 1
            continue

        lw = re.search(r"\b(\w+)\W*$", stripped)
        lw_str = lw.group(1).lower() if lw else ""
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
            out[-1] = out[-1].rstrip() + " " + ns
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
    counts: dict[int, int] = {}
    last_was_list = False
    prev_depth = 0
    type_depth: dict[str, int] = {}

    first_lt = None
    for _line in lines:
        _lt = _classify_item(_line)
        if _lt is not None:
            first_lt = _lt
            break
    effective_depth = dict(_LIST_DEPTH)
    if first_lt == "bn_decimal":
        effective_depth["bn_decimal"] = 1
        effective_depth["bn_alpha"] = 2
    elif first_lt == "bn_alpha":
        effective_depth["bn_alpha"] = 1
        effective_depth["bn_decimal"] = 2
    elif first_lt == "decimal":
        effective_depth["decimal"] = 1
        effective_depth["alpha"] = 2

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
        # An item may only be one level deeper than the item before it. A roman (i) list straight
        # after a paragraph would otherwise be indented 6 spaces and Markdown would show it as code.
        if not last_was_list:
            type_depth = {}
        if lt in type_depth:
            depth = type_depth[lt]  # same kind of marker as an earlier item of this list
        else:
            depth = min(depth, prev_depth + 1 if last_was_list else 1)
            type_depth[lt] = depth
        prev_depth = depth
        pad = " " * (_LIST_INDENT * (depth - 1))
        cpad = " " * (_LIST_INDENT * depth)

        for d in list(counts.keys()):
            if d > depth:
                del counts[d]

        counts[depth] = counts.get(depth, 0) + 1
        idx = counts[depth]

        first = _extract_item_content(lines[i])
        marker = _item_marker(lines[i])
        _record_numbering(marker, idx, depth, lt)
        i += 1

        continuation: list[str] = []
        while i < n:
            next_lt = _classify_item(lines[i])
            if next_lt is not None:
                break
            curr_s = lines[i].strip()
            if _is_block_break(curr_s) and curr_s:
                break
            if curr_s:
                last_c = next((c for c in reversed(continuation) if c), first)
                lw = re.search(r"\b(\w+)\W*$", last_c.rstrip())
                lw_str = lw.group(1).lower() if lw else ""
                if lw_str in _CANNOT_END_SENTENCE_WORDS and not _classify_item(curr_s):
                    if continuation and continuation[-1]:
                        continuation[-1] = continuation[-1].rstrip() + " " + curr_s
                    else:
                        first = first.rstrip() + " " + curr_s
                else:
                    continuation.append(curr_s)
                i += 1
            else:
                j = i + 1
                while j < n and not lines[j].strip():
                    j += 1
                ns = lines[j].strip() if j < n else ""
                if _classify_item(ns) is not None:
                    break
                elif ns and not _is_block_break(ns):
                    last_c = next((c for c in reversed(continuation) if c), first)
                    lw = re.search(r"\b(\w+)\W*$", last_c.rstrip())
                    lw_str = lw.group(1).lower() if lw else ""
                    if lw_str in _CANNOT_END_SENTENCE_WORDS:
                        i = j
                    else:
                        continuation.append("")
                        i += 1
                else:
                    break

        full_item = first
        for cont_line in continuation:
            if cont_line:
                full_item = full_item.rstrip() + " " + cont_line
        # <!--n:(a)--> keeps the original marker. It is invisible in Obsidian and in any
        # Markdown renderer, and md2html uses it to show (a) instead of 1.
        tag = f"<!--n:{marker}--> " if marker else ""
        out.append(f"{pad}{idx}. {tag}{full_item}")
        last_was_list = True

    return "\n".join(out)


def _convert_provided(text: str) -> str:
    """Format "Provided that..." and ordinal item lines as bold inline headings.

    - Standalone "Provided that —" lines become **Provided that**:
    - "Firstly—", "Secondly—", etc. become **Firstly**: content

    @param text  Section text after sentence-continuation joining.
    @return      Text with proviso/ordinal lines reformatted.
    """
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
                ordinal = m.group(1).capitalize()
                content = m.group(2).strip()
                i += 1
                while (
                    i < len(lines)
                    and lines[i].strip()
                    and not _is_block_break(lines[i].strip())
                ):
                    content += " " + lines[i].strip()
                    i += 1
                out.append(f"\n**{ordinal}**: {content}\n")
            else:
                out.append(lines[i])
                i += 1
    return "\n".join(out)


def _in_open_quote(lines: list[str], i: int) -> bool:
    """True when line i sits inside a quotation that opened earlier in the same list item.

    An amendment quotes the text it inserts, and that text can hold its own "ব্যাখ্যা:" line, which is
    part of the quotation and not a callout of the section. The count starts at the nearest list item
    ("(ক) ...") above, since a quotation does not outlive its item.
    """
    start = 0
    for k in range(i - 1, -1, -1):
        if re.match(r"^\s*\([^()\s]{1,5}\)\s", lines[k]):
            start = k
            break
    before = "\n".join(lines[start:i])
    return before.count("\u201c") > before.count("\u201d")


_BN_CALLOUT_WORDS = ("ব্যাখ্যা", "ব্যতিক্রম")


def _convert_numbered_callout(
    text: str, re_numbered: re.Pattern, re_single: re.Pattern, callout_name: str
) -> str:
    """Convert numbered Explanation/Exception blocks to Obsidian numbered callouts.

    Matches lines like "Explanation I— ..." or "Exception 1— ...", collects
    all consecutive numbered items, and wraps them in a "> [!callout_name]"
    block with "1. item" list formatting. Runs convert_lists on each item
    content to handle nested sub-lists within the callout.

    @param text          Section text.
    @param re_numbered   Pattern matching "Name N— content" (the numbered form).
    @param re_single     Pattern matching "Name— content" (the single form,
                         used to distinguish from numbered; not consumed here).
    @param callout_name  Obsidian callout type string (e.g. "Explanation").
    @return              Text with matching blocks replaced by callout syntax.
    """
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        raw_line = lines[i].strip()
        fn_m = _RE_FN_PREFIX.match(raw_line)
        check_line = raw_line[fn_m.end() :].lstrip() if fn_m else raw_line
        if not re_numbered.match(check_line) or (
            check_line.startswith(_BN_CALLOUT_WORDS) and _in_open_quote(lines, i)
        ):
            out.append(lines[i])
            i += 1
            continue
        fn_prefix = fn_m.group(1) if fn_m else ""
        items: list[str] = []
        while i < len(lines):
            raw_l = lines[i].strip()
            fn_m2 = _RE_FN_PREFIX.match(raw_l)
            chk = raw_l[fn_m2.end() :].lstrip() if fn_m2 else raw_l
            m2 = re_numbered.match(chk)
            if m2:
                pfx = (fn_m2.group(1) + " ") if fn_m2 else ""
                content = pfx + m2.group(2).strip()
                i += 1
                while (
                    i < len(lines)
                    and lines[i].strip()
                    and not _is_block_break(lines[i].strip())
                ):
                    content += " " + lines[i].strip()
                    i += 1
                items.append(content)
            elif not lines[i].strip():
                i += 1
                j = i
                while j < len(lines) and not lines[j].strip():
                    j += 1
                chk_j = lines[j].strip() if j < len(lines) else ""
                fn_j = _RE_FN_PREFIX.match(chk_j)
                chk_j = chk_j[fn_j.end() :].lstrip() if fn_j else chk_j
                if j < len(lines) and re_numbered.match(chk_j):
                    continue
                break
            else:
                break
        out.append(f"> [!{callout_name}]")
        if fn_prefix:
            out.append(f"> {fn_prefix}")
        for idx, item in enumerate(items, 1):
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
    """Convert a single-instance Explanation/Exception line to an Obsidian callout.

    Only processes lines that match re_single but NOT re_numbered (numbered
    form is handled by _convert_numbered_callout). Collects continuation lines
    until the next block break.

    @param text          Section text (after numbered callouts are already removed).
    @param re_single     Pattern matching "Name— content" (single form).
    @param re_numbered   Pattern for the numbered form (to avoid double-matching).
    @param callout_name  Obsidian callout type string.
    @return              Text with single-instance callouts wrapped.
    """
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        raw_line = lines[i].strip()
        fn_m = _RE_FN_PREFIX.match(raw_line)
        check_line = raw_line[fn_m.end() :].lstrip() if fn_m else raw_line
        m = re_single.match(check_line)
        if m and check_line.startswith(_BN_CALLOUT_WORDS) and (
            _in_open_quote(lines, i) or not m.group(1).strip()
        ):
            m = None  # inside a quoted amendment, or a bare "ব্যাখ্যা:" with nothing after it
        if m:
            fn_prefix = (fn_m.group(1) + " ") if fn_m else ""
            content = fn_prefix + m.group(1).strip()
            i += 1
            while (
                i < len(lines)
                and lines[i].strip()
                and not _is_block_break(lines[i].strip())
            ):
                content += " " + lines[i].strip()
                i += 1
            out.append(f"> [!{callout_name}]")
            out.append(f"> {content}")
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


_RE_CALLOUT_ITEM = re.compile(r"^((?:> )+)(\d+)\. (<!--n:)")


def _restart_callout_lists(text: str) -> str:
    """Number each list inside a callout from 1.

    Items carry the section's running count ("5. (a) ..."). A list that does not start at 1 cannot
    interrupt a paragraph, so an Illustrations list straight after its heading rendered as plain text.
    The shown marker is the "<!--n:-->" comment, so the digit itself is free to change.

    @param text  Rendered section or document text.
    @return      Text with callout list items numbered from 1 per list.
    """
    out: list[str] = []
    prefix, count = "", 0
    for line in text.split("\n"):
        m = _RE_CALLOUT_ITEM.match(line)
        if m:
            count = count + 1 if m.group(1) == prefix else 1
            prefix = m.group(1)
            line = f"{prefix}{count}. {line[m.end(2) + 2:]}"
        elif not line.startswith(prefix) or not line.strip(">  "):
            prefix, count = "", 0
        out.append(line)
    return "\n".join(out)


_ORDINAL_ITEM_WORDS = (
    "First Second Third Fourth Fifth Sixth Seventh Eighth Ninth Tenth Eleventh Twelfth "
    "Thirteenth Fourteenth Fifteenth Sixteenth Seventeenth Eighteenth Nineteenth Twentieth"
).split()
_ORDINAL_ADVERBS = "Firstly Secondly Thirdly Fourthly Fifthly Sixthly Seventhly Eighthly Ninthly Tenthly".split()
_REFS = r"((?:\[\^\d+\])*)"
# "First.- text" / "Second. text"
_RE_WORD_ITEM = re.compile(r"^" + _REFS + "(" + "|".join(_ORDINAL_ITEM_WORDS) + r")(?:\s*\.\s*-*|\s*-+)\s*(\S.*)$")
# "**Firstly**: text", as written by _convert_provided
_RE_ADVERB_ITEM = re.compile(r"^" + _REFS + r"\*\*(" + "|".join(_ORDINAL_ADVERBS) + r")\*\*:\s*(\S.*)$")
_RE_LONE_REFS = re.compile(r"^\s*(?:\[\^\d+\])+\s*$")
_RE_LIST_LINE = re.compile(r"^(\s*)\d+\. <!--n:")


def _ordinal_match(line: str):
    """Return (refs, word, content) for an ordinal-word item line, else None."""
    m = _RE_WORD_ITEM.match(line) or _RE_ADVERB_ITEM.match(line)
    return m.groups() if m else None


def _convert_word_ordinals(text: str) -> str:
    """Turn "First.- ...", "Second. ...", "**Firstly**: ..." paragraphs into a list that shows the word as its marker.

    Needs at least two such lines in a row (blank lines, callouts and the item's own sub-list between
    them are allowed), so a sentence that merely starts with "First." is left alone. List lines that
    follow an item become its nested list, and an inline "(a)" in the item starts that list.

    @param text  Section text after list conversion.
    @return      Text with ordinal-word runs rewritten as list items.
    """
    lines = text.split("\n")
    hits = [i for i, l in enumerate(lines) if _ordinal_match(l)]
    out = list(lines)

    def between(a: int, b: int) -> bool:
        # text that ends the series: anything but blanks, callouts, lone footnote refs and list lines
        return any(
            l.strip() and not l.startswith(">") and not _RE_LONE_REFS.match(l) and not _RE_LIST_LINE.match(l)
            for l in lines[a + 1 : b]
        )

    def children(j: int) -> list[int]:
        # list lines right after item j (blank lines allowed between them)
        found: list[int] = []
        k = j + 1
        while k < len(lines):
            if _RE_LIST_LINE.match(lines[k]):
                found.append(k)
            elif lines[k].strip():
                break
            k += 1
        return found

    def convert(run: list[int]) -> None:
        for n, j in enumerate(run, 1):
            refs, word, content = _ordinal_match(lines[j])
            pad = " " * len(f"{n}. ")
            kids = children(j)
            sub = re.match(r"^(.*?[-:;,])\s*\(a\)\s+(.*)$", content) if kids else None
            idx = 1
            if sub:
                # "Twelfth.- Every person- (a) ..." then (b), (c): the inline (a) opens the sub-list
                out[j] = f"{n}. <!--n:{word}--> {sub.group(1)}{refs}\n\n{pad}1. <!--n:(a)--> {sub.group(2)}"
                _record_numbering("(a)", 1, 2, "alpha")
                idx = 2
            else:
                out[j] = f"{n}. <!--n:{word}--> {content}{refs}"
            _record_numbering(word, n, 1, "ordinal_word")
            for k in kids:
                m = _RE_LIST_LINE.match(lines[k])
                if m.group(1):  # already nested deeper
                    out[k] = pad + lines[k]
                else:
                    out[k] = pad + f"{idx}. " + re.sub(r"^\d+\. ", "", lines[k])
                    idx += 1
            if j + 2 < len(lines) and _RE_LONE_REFS.match(lines[j + 2]) and not lines[j + 1].strip():
                out[j] += lines[j + 2].strip()
                out[j + 2] = ""
            elif j + 1 < len(lines) and _RE_LONE_REFS.match(lines[j + 1]):
                out[j] += lines[j + 1].strip()
                out[j + 1] = ""

    run: list[int] = []
    for i in hits + [None]:
        restart = i is not None and _ordinal_match(lines[i])[1] in ("First", "Firstly")  # a new series begins
        if run and (i is None or restart or between(run[-1], i)):
            if len(run) >= 2:
                convert(run)
            run = []
        if i is not None:
            run.append(i)
    return "\n".join(out)


def _wrap_illustrations_callout(text: str) -> str:
    """Wrap "Illustration(s)—" header and its following lines in an Obsidian callout.

    @param text  Section text after exception/explanation callouts are processed.
    @return      Text with illustrations wrapped in "> [!Illustrations]" blocks.
    """
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
        while (
            i < len(lines)
            and lines[i].strip()
            and not _is_block_break(lines[i].strip())
        ):
            out.append(f"> {lines[i].strip()}")
            i += 1
    return "\n".join(out)


def _nest_secondary_callouts(text: str) -> str:
    """Nest Illustrations/Exception callouts that appear inside an Explanation callout.

    The site occasionally places an Illustration or Exception block immediately
    after an Explanation. Obsidian renders nested callouts with an extra leading
    "> " prefix. This function adds that prefix when an Explanation callout is
    directly followed by an Illustrations or Exception callout.

    @param text  Section text after all callout wrapping.
    @return      Text with nested secondary callouts indented.
    """
    lines = text.splitlines()
    out = []
    i = 0
    while i < len(lines):
        if _RE_NEST_EXPL_START.match(lines[i]):
            out.append(lines[i])
            i += 1
            while i < len(lines):
                curr = lines[i]
                if _RE_NEST_SECONDARY_HDR.match(curr):
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
                    if j < len(lines) and _RE_NEST_SECONDARY_HDR.match(lines[j]):
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
    """Convert margin-note keywords to Obsidian callouts using _MARGIN_CALLOUTS rules.

    Matches: Saving, Repeal, Commencement, Short title — each mapped to a
    specific callout type (Note/Warning/Info).

    @param text  Section text.
    @return      Text with margin-note lines replaced by callout blocks.
    """
    lines = text.splitlines()
    out = []
    i = 0
    while i < len(lines):
        matched = False
        raw_line = lines[i].strip()
        for pattern, callout_type, label in _MARGIN_CALLOUTS:
            m = pattern.match(raw_line)
            if m:
                content = m.group(1).strip()
                i += 1
                while (
                    i < len(lines)
                    and lines[i].strip()
                    and not _is_block_break(lines[i].strip())
                ):
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
    """Wrap lines matching the definition pattern in a Definition callout.

    Matches: 'In this Act, "term" means content' or '"term" means content'.
    Renders as "> [!Definition]\\n> **term** means content".

    @param text  Section text.
    @return      Text with definition lines wrapped in callout blocks.
    """
    lines = text.splitlines()
    out = []
    for line in lines:
        m = _RE_DEFINITION.match(line.strip())
        if m:
            term = m.group(1).strip()
            verb = m.group(2)
            content = m.group(3).strip()
            out.append("> [!Definition]")
            out.append(f"> **{term}** {verb} {content}")
        else:
            out.append(line)
    return "\n".join(out)


# Bengali counterparts: "দণ্ডিত হইবেন" (shall be punished), "দণ্ডনীয় হইবেন", "দায়ী হইবেন" (liable), the named
# sentences. A trailing case ending (মৃত্যুদণ্ডে) stays inside the bold.
_RE_PUNISHMENT_BN = re.compile(
    r"(?<![ঀ-৿*])((?:যাবজ্জীবন|সশ্রম|বিনাশ্রম)\s+কারাদণ্ড|মৃত্যুদণ্ড|দণ্ডিত\s+হইবেন|দণ্ডনীয়\s+হইবেন|দায়ী\s+হইবেন)"
    r"((?:ে|র|ের|কে)?)(?![ঀ-৿*])"
)


def _bold_punishment_keywords(text: str) -> str:
    """Wrap punishment-related phrases in **bold** for visual emphasis.

    @param text  Section text.
    @return      Text with phrases like "shall be punished with" bolded.
    """
    text = _RE_PUNISHMENT.sub(r"**\1**", text)
    return _RE_PUNISHMENT_BN.sub(r"**\1\2**", text)


def _mark_omitted_repealed(text: str) -> str:
    """Wrap inline "Omitted by..." and "Repealed by..." phrases in ~~strikethrough~~.

    @param text  Section text.
    @return      Text with omission/repeal notices struck through.
    """
    text = _RE_OMITTED_INLINE.sub(r"~~\1~~", text)
    text = _RE_REPEALED_INLINE.sub(r"~~\1~~", text)
    return text


def _linkify_section_refs(
    text: str, act_number: str, known_sections: set = frozenset()
) -> str:
    """Convert "section N" and "ধারা N" references to [[wikilinks]] pointing to block anchors.

    Handles single references and lists ("sections 64, 65, 66 and 445", "ধারা ১২৮, ১৩২ ও ১৩৫").
    Only sections that exist in known_sections (built from the act's own SectionItems) are linked.
    References that name another act's section ("section 5 of the Evidence Act", "... আইনের ধারা ৫")
    are left alone, as are matches already inside a wikilink.

    @param text            Section Markdown text.
    @param act_number      Act number string for the wikilink target (e.g. "1710").
    @param known_sections  Set of block ID strings from this act's sections.
    @return                Text with eligible section references linkified.
    """
    if not act_number:
        return text

    def replace_sec(m: re.Match) -> str:
        prefix = m.group(1)
        items = m.group(2)
        tail = (m.group("tail") or "").rstrip() if "tail" in m.re.groupindex else ""
        first = True
        single = len(_RE_SEC_NUM_IN_LIST.findall(items)) == 1

        def link(n: re.Match) -> str:
            nonlocal first
            num = n.group(0)
            anchor = _secnum_to_block_id(num)
            label = f"{prefix} {num}" if first else num
            first = False
            if anchor not in known_sections:
                return label + (tail if single else "")
            if single and tail:
                label += tail  # "section 5(2)(a)", "ধারা ৫ এর উপ-ধারা (২)": the whole reference is the link
            return f"[[act-{act_number}#^sec-{anchor}\\|{label}]]"

        # Link every number of a list; the first keeps the word ("sections 64"), the rest are bare.
        return _RE_SEC_NUM_IN_LIST.sub(link, items)

    def replace_post(m: re.Match) -> str:
        # "৫ ও ৬ ধারা": link the numbers, leave the word where it is
        def link(n: re.Match) -> str:
            anchor = _secnum_to_block_id(n.group(0))
            return f"[[act-{act_number}#^sec-{anchor}\\|{n.group(0)}]]" if anchor in known_sections else n.group(0)

        return _RE_SEC_NUM_IN_LIST.sub(link, m.group("nums")) + m.string[m.end("nums") : m.end()]

    for regex in (_RE_SECTION_REF, _RE_SECTION_REF_BN, _RE_SECTION_REF_BN_POST):
        post = regex is _RE_SECTION_REF_BN_POST
        result = []
        last = 0
        for m in regex.finditer(text):
            before = text[max(0, m.start() - 10) : m.start()]
            after = text[m.end() : m.end() + 40]
            inside_link = "[[" in before and "]]" not in before
            if inside_link or _names_other_act(text[max(0, m.start() - 30) : m.start()], after):
                result.append(text[last : m.end()])
            else:
                result.append(text[last : m.start()])
                result.append(replace_post(m) if post else replace_sec(m))
            last = m.end()
        result.append(text[last:])
        text = "".join(result)
    return text


_RE_OTHER_ACT_AFTER = re.compile(
    r"^\s+of\s+(?:the\s+)?(?!Act\b|Code\b|Ordinance\b|Order\b|Regulation\b)[A-Z]"
)
_RE_OTHER_ACT_BEFORE_BN = re.compile(
    r"(?:এর|আইনের|অধ্যাদেশের|আদেশের|বিধির|বিধিমালার|সংবিধানের|কোডের|সংহিতার)\s*$"
)
_RE_THIS_ACT_BEFORE_BN = re.compile(r"এই\s+(?:আইনের|অধ্যাদেশের|আদেশের)\s*$")


def _names_other_act(before: str, after: str) -> bool:
    """True when a section reference belongs to a different act than the one being converted."""
    if _RE_OTHER_ACT_AFTER.match(after):
        return True
    return bool(_RE_OTHER_ACT_BEFORE_BN.search(before)) and not _RE_THIS_ACT_BEFORE_BN.search(before)


def _table_to_markdown(table: Tag) -> str:
    """Convert an HTML table to a Markdown table, respecting rowspan and colspan.

    Builds a 2D (row, col) → cell-text grid, expanding spans correctly so that
    merged cells don't shift column positions. Cells covered by a span are left
    empty. Cell text is extracted via _tag_to_md_text so that <a> links become
    [[wikilinks]]. Pipe characters in cell text are escaped as \\|.

    Outputs a blank header row when no <th> elements are present (pure data
    table), to prevent the first data row from being misread as a header.

    @param table  BeautifulSoup Tag for the <table> element.
    @return       GFM Markdown table string, or "" if the table has no content.
    """
    grid: dict[tuple[int, int], str] = {}
    occupied: set[tuple[int, int]] = set()

    row_idx = 0
    for tr in table.find_all("tr"):
        col_idx = 0
        for td in tr.find_all(["td", "th"], recursive=False):
            while (row_idx, col_idx) in occupied:
                col_idx += 1
            try:
                colspan = max(1, int(td.get("colspan", 1)))
            except (ValueError, TypeError):
                colspan = 1
            try:
                rowspan = max(1, int(td.get("rowspan", 1)))
            except (ValueError, TypeError):
                rowspan = 1
            inner = "".join(_tag_to_md_text(c) for c in td.children)
            text = re.sub(r"[\xa0​\s]+", " ", inner).strip().replace("|", "\\|")
            grid[(row_idx, col_idx)] = text
            for r in range(rowspan):
                for c in range(colspan):
                    if r == 0 and c == 0:
                        continue
                    pos = (row_idx + r, col_idx + c)
                    occupied.add(pos)
                    grid.setdefault(pos, "")
            for r in range(1, rowspan):
                occupied.add((row_idx + r, col_idx))
            col_idx += colspan
        row_idx += 1

    if not grid:
        return ""

    max_row = max(r for r, _ in grid) + 1
    max_col = max(c for _, c in grid) + 1

    rows: list[list[str]] = []
    for r in range(max_row):
        row = [grid.get((r, c), "") for c in range(max_col)]
        if any(c.strip() for c in row):
            rows.append(row)
    if not rows:
        return ""

    sep = ["---"] * max_col
    has_real_header = bool(table.find("th"))

    if has_real_header:
        lines = [
            "| " + " | ".join(rows[0]) + " |",
            "| " + " | ".join(sep) + " |",
        ]
        for r in rows[1:]:
            lines.append("| " + " | ".join(r) + " |")
    else:
        blank = [""] * max_col
        lines = [
            "| " + " | ".join(blank) + " |",
            "| " + " | ".join(sep) + " |",
        ]
        for r in rows:
            lines.append("| " + " | ".join(r) + " |")

    return "\n".join(lines)


def _is_boilerplate(raw: str) -> bool:
    """Return True if more than 60% of non-empty lines match boilerplate patterns.

    Boilerplate patterns include CONTENTS/SECTIONS headers, PREAMBLE/WHEREAS
    headings, "An Act to" / "It is hereby" openers, and bare URLs. Used by
    parse_detail to silently skip navigation-only rows.

    @param raw  Raw text from a section body.
    @return     True if this text should be discarded as boilerplate.
    """
    lines = [l for l in raw.splitlines() if l.strip()]
    if not lines:
        return True
    bp = sum(
        1
        for l in lines
        if _RE_BOILERPLATE_LINE.search(l)
        or _RE_URL_ONLY.match(l)
        or _RE_TOC_LINK.match(l)
    )
    return bp / len(lines) > 0.6


def _filter_stray_lines(text: str) -> str:
    """Remove lines that are pure navigational noise or OCR artefacts.

    Drops: lines starting with decorative symbols (♣ § ¶ • etc.), bare URLs,
    markdown TOC links, and standalone CONTENTS/SECTIONS/PREAMBLE labels.
    Preserves the stripped remainder of a decorated line if it contains content.

    @param text  Section or preamble text.
    @return      Text with stray lines removed.
    """
    kept = []
    for line in text.splitlines():
        stripped = re.sub(r"^[♣♦§¶✦✧·•]+\s*", "", line)
        if stripped != line:
            if stripped.strip():
                kept.append(stripped)
            continue
        if _RE_URL_ONLY.match(line):
            continue
        if _RE_TOC_LINK.match(line):
            continue
        if re.match(r"^\s*(CONTENTS|SECTIONS|PREAMBLE)\s*$", line, re.IGNORECASE):
            continue
        kept.append(line)
    return "\n".join(kept)


def html_to_markdown(
    details_div: Tag,
    scraper: Optional[Scraper] = None,
    act_number: str = "",
    known_sections: set = frozenset(),
    footnote_map: Optional[dict] = None,
) -> str:
    """Convert a section's HTML Tag to clean Obsidian Markdown.

    This is the core transformation pipeline. Steps in order:
    1.  Tables → GFM Markdown (colspan/rowspan-aware).
    2.  _tag_to_md_text — recursive HTML→text with wikilink conversion.
    3.  Strip residual HTML tags left by non-standard markup.
    4.  Footnote marker normalisation ([N] → [^N], [***] → [ * * * ]).
    5.  Whitespace normalisation and left-strip of all lines.
    6.  Drop standalone digit lines (page numbers).
    7.  Strip wrapping brackets if the entire text is bracketed.
    8.  Early-return with Warning callout if section is omitted/repealed.
    9.  Strip leading section number.
    10. Filter stray/boilerplate lines.
    11. Strip single-letter editorial substitution brackets like [A].
    12. Strip annotation brackets (preserving [[wikilinks]]).
    13. Fix "].][" residue from wikilink followed by closing bracket.
    14. Strip unmatched leading "[".
    15. Join isolated list labels onto their following content line.
    16. Split inline list items ("; (d) text" → newline).
    17. Join connector-led fragment lines ("or", "and", "namely").
    18. Join mid-sentence HTML line-break splits.
    19. Mark "Omitted by" / "Repealed by" with strikethrough.
    20. Format "Provided that" / ordinal items.
    21. Bold punishment keywords.
    22. Numbered and single Explanation/Exception callouts.
    23. Margin callouts (Saving, Repeal, Commencement, Short title).
    24. Definition callouts.
    25. Nested list conversion.
    26. Illustrations callout.
    27. Nest secondary callouts inside Explanation blocks.
    28. Cross-link section references to [[wikilinks]].
    29. Resolve [^N][ * * * ] omission markers.
    30. Relocate leading footnote refs to paragraph end.
    31. Strip stray ][^N] → [^N].
    32. Strip stray trailing ].
    33. Collapse 3+ consecutive newlines.

    @param details_div   BeautifulSoup Tag for the div.txt-details element.
    @param scraper       Scraper instance (reserved for future use).
    @param act_number    Act number string for intra-act section wikilinks.
    @param known_sections  Set of block IDs from this act's own sections.
    @param footnote_map  Dict of footnote_number → text for omission resolution.
    @return              Clean Markdown string ready for Obsidian.
    """

    for table in details_div.find_all("table"):
        table.replace_with(_table_to_markdown(table) + "\n")

    raw = _tag_to_md_text(details_div, scraper)
    raw = _RE_RESIDUAL_HTML.sub("\n", raw)
    raw = _transform_footnotes(raw)
    raw = re.sub(r"[^\S\n]+", " ", raw)
    raw = "\n".join(line.lstrip() for line in raw.splitlines())
    raw = "\n".join(
        line
        for line in raw.splitlines()
        if not re.fullmatch(r"\s*[\d\u09E6-\u09EF]+\s*", line)
    )
    raw = _strip_outer_brackets(raw)

    badge = _check_section_status(raw)
    if badge:
        return badge

    raw = _strip_leading_section_num(raw)
    raw = _filter_stray_lines(raw)
    raw = _RE_SUBST_LETTER.sub(r"\1", raw)
    raw = _strip_annotation_brackets(raw)
    raw = re.sub(
        r"(?<=\]\])\.?\](?!\])", lambda m: "." if "." in m.group(0) else "", raw
    )
    raw = _strip_unmatched_open_brackets(raw)
    raw = re.sub(r"^\.\s+(?=\()", "", raw.lstrip())
    raw = _join_isolated_labels(raw)
    raw = _split_heading_before_first_subclause(raw)
    raw = _split_inline_list_items(raw)
    raw = _join_fragment_lines(raw)
    raw = _join_sentence_continuations(raw)
    raw = _mark_omitted_repealed(raw)
    raw = _convert_provided(raw)
    raw = _bold_punishment_keywords(raw)
    raw = _convert_numbered_callout(
        raw, _RE_EXPL_NUMBERED, _RE_EXPL_SINGLE, "Explanation"
    )
    raw = _convert_single_callout(
        raw, _RE_EXPL_SINGLE, _RE_EXPL_NUMBERED, "Explanation"
    )
    raw = _convert_numbered_callout(
        raw, _RE_EXCEP_NUMBERED, _RE_EXCEP_SINGLE, "Exception"
    )
    raw = _convert_single_callout(
        raw, _RE_EXCEP_SINGLE, _RE_EXCEP_NUMBERED, "Exception"
    )
    raw = _convert_margin_callouts(raw)
    raw = _convert_definitions(raw)
    raw = convert_lists(raw)
    raw = _wrap_illustrations_callout(raw)
    raw = _convert_word_ordinals(raw)
    raw = _nest_secondary_callouts(raw)
    raw = _linkify_section_refs(raw, act_number, known_sections)
    if footnote_map:
        raw = _resolve_omissions(raw, footnote_map)
    raw = _relocate_leading_footnotes(raw)
    # ][^N] → [^N]: orphaned bracket closer before footnote ref.
    # (?<!\]) excludes ]], (?<!\* ) excludes [ * * * ], (?!\]) avoids ]][ corner.
    raw = re.sub(r"(?<!\])(?<!\* )(?<!\d)\](?!\])(\[\^\d+\])", r"\1", raw)
    # Trailing stray ] from an unclosed editorial bracket; spares [ * * * ] and [^N].
    raw = re.sub(r"(?<!\])(?<!\* )(?<!\d)\]\s*$", "", raw.rstrip())
    raw = _drop_unmatched_closers(raw)
    raw = RE_MULTI_NEWLINE.sub("\n\n", raw)

    return raw.strip()


# ---------------------------------------------------------------------------
# YAML & builder
# ---------------------------------------------------------------------------


def _url_to_section_id(url: str) -> Optional[str]:
    """Extract the numeric section ID from a bdlaws section permalink.

    @param url  Absolute URL like ".../section-42.html".
    @return     The ID string "42", or None if not a section URL.
    """
    m = re.search(r"/section-(\d+)\.html", url)
    return m.group(1) if m else None


def _detect_language(text: str) -> str:
    """Detect whether text is predominantly Bengali or English.

    Counts Unicode characters in the Bengali block (U+0980–U+09FF).
    Threshold of 50 Bengali characters is intentionally loose to handle
    acts with a Bengali title but English body.

    @param text  Sample text (act title + first few section headings).
    @return      "bn" or "en".
    """
    bn_chars = sum(1 for c in text if "ঀ" <= c <= "৿")
    return "bn" if bn_chars > 50 else "en"


def _heading_to_slug(text: str, language: str = "en") -> str:
    """Convert a heading string to a URL-safe slug for YAML section entries.

    For Bengali text, keeps only Bengali-block characters joined by hyphens.
    For English/Latin text, lowercases, strips punctuation, and collapses
    whitespace/underscores to hyphens. Truncated to 60 characters.

    @param text      Heading or section number string.
    @param language  "bn" for Bengali, "en" (default) for Latin.
    @return          URL-safe slug string.
    """
    if language == "bn":
        text = re.sub(r"[^ঀ-৿\s]", "", text)
        return re.sub(r"\s+", "-", text.strip())[:60]
    text = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[\s_]+", "-", text).strip("-")[:60]


def _fix_bengali_ocr(text: str) -> str:
    """Correct the misreads and typing habits in Bengali text from the bdlaws scan.

    - Consonant + ৪ + Consonant → Consonant + ৃ (the vowel sign, misread as ৪).
    - Consonant + ৷ + Consonant → Consonant + ি (the vowel sign, misread as ৷).
    - Any other ৷ (U+09F7, a currency mark) is a danda written with the wrong character → ।.
    - Legacy-font artefacts: "পর্যন্ত্ম" → "পর্যন্ত", "ত্মেগত্রে" → "ক্ষেত্রে".
    - Word-final "ত্" is the khanda ta "ৎ".
    - No space before , ; : । and one space after , ; between words.

    @param text  Bengali section, preamble or whole-document text.
    @return      Text with the errors corrected.
    """
    c = "কখগঘঙচছজঝঞটঠডঢণতথদধনপফবভমযরলশষসহড়ঢ়য়ৎ"
    letter = "ঁ-ঃঅ-হ়া-ৌৎড়ঢ়য়"
    text = re.sub(rf"([{c}])৪([{c}])", r"\1ৃ\2", text)
    text = re.sub(rf"([{c}])৷(?=[{c}])", r"\1ি", text)
    text = text.replace("৷", "।")
    text = re.sub(r"([নস]্ত)্ম", r"\1", text)
    text = re.sub(r"ত্ম([ািীুূৃেৈোৌ])গ", r"ক্ষ\1", text)
    text = re.sub(r"(?<!আ)ত্মগ", "ক্ষ", text)
    text = re.sub(r"ত্(?![ঀ-৿\u200c\u200d])", "ৎ", text)
    text = re.sub(r"(?<=[ঀ-৿]) +([,;:।])", r"\1", text)
    text = re.sub(rf"(?<=[{letter}])([,;])(?=[{letter}])", r"\1 ", text)
    return text


_TAG_EXCLUSIONS: dict[str, list[str]] = {
    "Banking&Finance": [
        "police",
        "criminal",
        "penal",
        "army",
        "navy",
        "air force",
        "intelligence",
    ],
    "Tax Law": ["police", "criminal", "penal", "army", "navy", "intelligence"],
    "Labour Law": ["police", "criminal", "penal", "intelligence"],
    "Property Law": ["police", "criminal", "penal", "intelligence"],
    "Civil Law": ["police act", "criminal procedure", "penal"],
}


def _infer_tags(act_title: str, nodes: list) -> list[str]:
    """Infer Obsidian tags from the act title and section headings.

    Keyword-matches against _TAG_KEYWORDS, then removes tags whose exclusion
    keywords appear in the act title (e.g. "LabourLaw" is excluded if the
    title contains "police" to avoid tagging the Police Act as LabourLaw).
    Always prepends ["Legal", "Bangladesh"]. Returns at most 8 tags.

    @param act_title  Full act title string.
    @param nodes      List of DocNode items (SectionItem headings are sampled).
    @return           List of tag strings for the YAML frontmatter.
    """
    tags = ["Legal", "Bangladesh"]
    combined = (
        act_title.lower()
        + " "
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


def _clean_field(text: str) -> str:
    """Strip control characters and stray zero-width marks from a title or description.

    Zero-width joiners are kept only for Bengali text, where they are part of the spelling.
    """
    text = re.sub("[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", text).replace(chr(0xFEFF), "").replace(chr(0x200B), "")
    if not re.search("[ঀ-৿]", text):
        text = text.replace(chr(0x200C), "").replace(chr(0x200D), "")
    return _fix_omission_marks(" ".join(text.split()))


def _build_yaml_frontmatter(
    act_title: str,
    act_url: str,
    tags: list[str],
    act_number: str = "",
    language: str = "en",
    section_entries: Optional[list] = None,
    enacted_at: Optional[str] = None,
    description: str = "",
) -> str:
    """Build the YAML frontmatter block for an Obsidian Markdown file.

    Field order: title, description, tags, source_url, act_id, year,
    enacted_at, scraped_at, language, sections[].

    @param act_title        Full act title (used for title and year extraction).
    @param act_url          Canonical URL for source_url.
    @param tags             List of tag strings from _infer_tags.
    @param act_number       Numeric ID string for act_id.
    @param language         "en" or "bn" for the language field.
    @param section_entries  List of {id, slug, url} dicts for the sections block.
    @param enacted_at       ISO date string (YYYY-MM-DD) or None.
    @param description      One-line act description for the description field.
    @return                 Complete YAML block including opening and closing "---".
    """
    scraped_at = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tag_lines = "\n".join(f"  - {t}" for t in tags)
    # JSON strings are valid YAML: quotes, backslashes and control characters are escaped for us.
    act_title = _clean_field(act_title)
    description = _clean_field(description)
    safe_title = json.dumps(act_title, ensure_ascii=False)
    safe_desc = json.dumps(description or act_title, ensure_ascii=False)
    ascii_title = act_title.translate(BENGALI_DIGIT_MAP)
    year_m = re.search(r"\b(1[6-9]\d{2}|20\d{2})\b", ascii_title)
    year = year_m.group(1) if year_m else (enacted_at[:4] if enacted_at else "")
    year_line = f"year: {year}\n" if year else ""
    act_id_line = f"act_id: {act_number}\n" if act_number else ""
    enacted_line = f"enacted_at: {enacted_at}\n" if enacted_at else ""
    desc_line = f"description: {safe_desc}\n"
    if section_entries:
        sec_block = "sections:\n" + "".join(
            f"  - id: {e['id']}\n    slug: {e['slug']}\n    url: {e['url']}\n"
            for e in section_entries
        )
    else:
        sec_block = ""
    return (
        f"---\n"
        f"title: {safe_title}\n"
        f"{desc_line}"
        f"tags:\n{tag_lines}\n"
        f"source_url: {act_url}\n"
        f"{act_id_line}"
        f"{year_line}"
        f"{enacted_line}"
        f"scraped_at: {scraped_at}\n"
        f"language: {language}\n"
        f"{sec_block}"
        f"---\n"
    )


def _plain_undefined_refs(text: str) -> str:
    """Turn a [^N] reference that has no [^N]: definition back into plain "[N]" text.

    A bracketed year such as "[1898]" looks like a footnote reference to the converter.

    @param text  Whole rendered document.
    @return      Text with undefined references restored.
    """
    defined = set(re.findall(r"(?m)^\[\^(\d+)\]:", text))
    return re.sub(
        r"\[\^(\d+)\](?!:)",
        lambda m: m.group(0) if m.group(1) in defined else f"[{m.group(1)}]",
        text,
    )


def build_markdown(
    nodes: list[DocNode],
    act_number: str,
    act_title: str = "",
    act_url: str = "",
    scraper: Optional[Scraper] = None,
    title_footnote_num: Optional[int] = None,
    enacted_at: Optional[str] = None,
    description: str = "",
) -> str:
    """Assemble the complete Markdown document from a list of DocNodes.

    Builds YAML frontmatter (language detection, tag inference, section index),
    writes the H1 title, then renders each DocNode at its appropriate heading
    level:
    - PreambleItem   → > [!Preamble] callout
    - PartItem       → ## heading with URL link
    - ChapterItem    → ### heading
    - SubChapterItem → #### heading
    - SectionItem    → ##### or ###### heading + html_to_markdown body
    - FootnotesItem  → ## Footnotes section with [^N]: definitions

    Inserted sections (containing a letter in the number, e.g. "4A") are
    rendered at ###### to distinguish them from regular ##### sections.

    @param nodes             Ordered list of DocNode instances from parse_detail.
    @param act_number        Numeric act ID string.
    @param act_title         Full act title for H1 and frontmatter.
    @param act_url           Canonical URL for frontmatter source_url.
    @param scraper           Passed through to html_to_markdown (reserved).
    @param title_footnote_num  Footnote number to append to the H1, or None.
    @param enacted_at        ISO date string for frontmatter, or None.
    @param description       One-line description for frontmatter.
    @return                  Complete Markdown document as a string.
    """
    footnote_map: dict[int, str] = {}
    for node in nodes:
        if isinstance(node, FootnotesItem):
            for num, text in node.notes:
                footnote_map[num] = text
            break

    sample = act_title + " ".join(
        (node.heading or "") for node in nodes[:10] if isinstance(node, SectionItem)
    )
    language = _detect_language(sample)

    known_sections: set[str] = set()
    used_block_ids: set[str] = set()
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
                        slug = _heading_to_slug(
                            node.heading or node.number or "", language
                        )
                        section_entries.append(
                            {"id": sec_id, "slug": slug, "url": node.url}
                        )

    lines: list[str] = []
    tags = _infer_tags(act_title, nodes)
    lines.append(
        _build_yaml_frontmatter(
            act_title,
            act_url,
            tags,
            act_number=act_number,
            language=language,
            section_entries=section_entries,
            enacted_at=enacted_at,
            description=description,
        )
    )

    if act_title:
        if title_footnote_num is not None:
            lines.append(f"# {_fix_omission_marks(act_title)}[^{title_footnote_num}]")
        else:
            lines.append(f"# {_fix_omission_marks(act_title)}")
        lines.append("")

    for node in nodes:
        if isinstance(node, PreambleItem):
            preamble_text = _filter_stray_lines(node.text)
            preamble_text = _linkify_section_refs(preamble_text, act_number, known_sections)
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
            # One definition per footnote number. (Two notes can share the same text; deduplicating
            # by text used to drop the later number while the body still referenced it.)
            seen_nums: set[int] = set()
            for num, text in node.notes:
                if num in seen_nums:
                    continue
                seen_nums.add(num)
                lines.append(f"[^{num}]: {text}")
            lines.append("")

        elif isinstance(node, PartItem):
            lines.append("")
            name = _fix_omission_marks(node.name)
            ref = re.search(r"\[\^\d+\]$", name)
            if node.url in ("", "#") and "[" in name:
                lines.append(f"## {name}")  # a footnote reference inside a link label does not parse
            elif ref:
                lines.append(f"## [{name[: ref.start()]}]({node.url}){ref.group(0)}")
            else:
                lines.append(f"## [{name}]({node.url})")
            lines.append("")

        elif isinstance(node, ChapterItem):
            lines.append("")
            name = _fix_omission_marks(node.name)
            if node.url in ("", "#") and "[" in name:
                lines.append(f"### {name}")  # a footnote reference inside a link label does not parse
            else:
                lines.append(f"### [{name}]({node.url})")
            lines.append("")

        elif isinstance(node, SubChapterItem):
            if node.letter and node.parent_number:
                heading_text = (
                    f"Section {node.parent_number}{node.letter}: {node.subtitle}"
                )
            else:
                heading_text = node.subtitle
            lines.append("")
            if heading_text.startswith("[") and heading_text.endswith("]") and "[" not in heading_text[1:-1]:
                heading_text = heading_text[1:-1].strip()  # an amendment bracket, not part of the title
            heading_text = _RE_LINK_LOOKALIKE.sub(r"\\[\1\\](", heading_text)
            lines.append(f"#### [{heading_text}]({node.url})")
            lines.append("")

        elif isinstance(node, SectionItem):
            has_heading = bool(node.heading) or bool(node.number)
            if has_heading:
                if node.number:
                    term = (
                        "ধারা"
                        if language == "bn"
                        else ("Article" if "/Article-" in node.url else "Section")
                    )
                    if language != "bn" and re.fullmatch(r"\d+[A-Za-z]{0,2}-\d+[A-Za-z]{0,2}", node.number):
                        term += "s"
                    heading_text = (
                        f"{term} {node.number}: {node.heading}"
                        if node.heading
                        else f"{term} {node.number}"
                    )
                    is_inserted = any(c.isalpha() for c in node.number)
                    level = "######" if is_inserted else "#####"
                    block_id = f"^sec-{_secnum_to_block_id(node.number)}"
                else:
                    heading_text = node.heading
                    level = "#####"
                    block_id = ""
                    if node.url and node.url != "#":
                        sec_id = _url_to_section_id(node.url)
                        if sec_id:
                            block_id = f"^sec-{sec_id}"
                if block_id:
                    # two sections can carry the same number; anchors must stay unique
                    base_id, k = block_id, 2
                    while block_id in used_block_ids:
                        block_id = f"{base_id}-{k}"
                        k += 1
                    used_block_ids.add(block_id)
                block_suffix = f" {block_id}" if block_id else ""
                lines.append("")
                heading_text = _RE_LINK_LOOKALIKE.sub(r"\\[\1\\](", heading_text)
                if node.url in ("", "#") and "[" in heading_text:
                    # a link to nowhere; a footnote reference inside its label stops it parsing
                    lines.append(f"{level} {heading_text}{block_suffix}")
                else:
                    lines.append(f"{level} [{heading_text}]({node.url}){block_suffix}")
                if node.url and node.url != "#":
                    lines.append(f"<!-- url: {node.url} -->")
                lines.append("")

            body = html_to_markdown(
                node.html,
                scraper=scraper,
                act_number=act_number,
                known_sections=known_sections,
                footnote_map=footnote_map,
            )
            if language == "bn":
                body = _fix_bengali_ocr(body)
            lines.append(body)
            lines.append("")

    text = _fix_bengali_ocr(_restart_callout_lists(_plain_undefined_refs("\n".join(lines))))
    from bdlaw_scraper.clauses import anchor_and_link

    return _final_cleanup(anchor_and_link(text, act_number))


def _final_cleanup(md: str) -> str:
    """Strip trailing whitespace from every line and collapse 3+ blank lines to 2.

    @param md  Raw assembled Markdown string.
    @return    Cleaned string ending with a single newline.
    """
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
    force_refresh: bool = False,
) -> None:
    """Scrape one act and write it as an Obsidian Markdown file.

    Fetches two pages per act in parallel (ToC and print page share one
    Scraper; the rate lock serialises their delay intervals):
    - act-N.html       (ToC page): section URL map, title, date, description.
    - act-print-N.html (print page): full section bodies, preamble, footnotes.

    Writes atomically via a .tmp file renamed on success.

    The metadata resolution priority is:
    - enacted_at  : publish-date <p> → preamble prose date → None
    - description : meta description tag → preamble first line → ""

    @param act_number    Numeric act ID string (e.g. "1710").
    @param output        Destination Path for the .md file.
    @param delay         Minimum seconds between HTTP requests.
    @param max_retries   Maximum retry attempts per request.
    @param cache_dir     Directory for HTML cache, or None.
    @param log           Logger instance.
    @param force_refresh Bypass cache and fetch live pages.
    """
    toc_url = f"{BASE_URL}/act-{act_number}.html"
    detail_url = f"{BASE_URL}/act-print-{act_number}.html"
    scraper = Scraper(
        delay=delay,
        max_retries=max_retries,
        cache_dir=cache_dir,
        force_refresh=force_refresh,
    )

    log.info("Fetching act-%s …", act_number)
    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_toc = ex.submit(scraper.fetch, toc_url)
        fut_detail = ex.submit(scraper.fetch, detail_url)
        toc_soup = fut_toc.result()
        detail_soup = fut_detail.result()

    toc_map = parse_toc(toc_soup, BASE_URL)
    log.info("act-%s: %d section links in ToC", act_number, len(toc_map))
    log.info("act-%s: parsing document structure …", act_number)

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
            task = progress.add_task("Parsing nodes ...", total=None)
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

    log.info("Resolving section URLs ...")
    resolve_urls(nodes, toc_map, BASE_URL)

    act_title = parse_act_title(detail_soup) or parse_act_title(toc_soup)
    title_fn_num = _extract_title_footnote_num(
        detail_soup
    ) or _extract_title_footnote_num(toc_soup)
    act_url_str = f"{BASE_URL}/act-{act_number}.html"
    enacted_at = parse_act_date(toc_soup) or parse_act_date(detail_soup)
    description = parse_act_description(toc_soup) or parse_act_description(detail_soup)
    if act_title:
        log.info("Act title: %s", act_title)
    if title_fn_num:
        log.info("Title footnote: [^%d]", title_fn_num)
    if enacted_at:
        log.info("Enacted at: %s", enacted_at)

    preamble = parse_preamble(detail_soup) or parse_preamble(toc_soup)
    if preamble:
        log.info("Preamble found (%d chars)", len(preamble))
        if not enacted_at:
            enacted_at = _parse_date_from_preamble_text(preamble)
            if enacted_at:
                log.info("Enacted at (from preamble): %s", enacted_at)
        if not description:
            description = re.sub(
                r"\[\[[^\]|\\]*\\?\|([^\]]*)\]\]",
                r"\1",
                next((l.strip() for l in preamble.splitlines() if l.strip()), ""),
            )[:200]
        nodes.insert(0, PreambleItem(text=preamble))

    footnotes = parse_footnotes(detail_soup)
    if footnotes:
        log.info("Found %d footnotes", len(footnotes.notes))
        nodes.append(footnotes)

    seen_nums: set[str] = set()
    for node in nodes:
        if isinstance(node, SectionItem) and node.number:
            bid = _secnum_to_block_id(node.number)
            if bid in seen_nums:
                log.warning(
                    "act-%s: duplicate section number %s", act_number, node.number
                )
            seen_nums.add(bid)

    log.info("act-%s: building Markdown …", act_number)
    _NUMBERING.items = []
    md = build_markdown(
        nodes,
        act_number,
        act_title=act_title,
        act_url=act_url_str,
        scraper=scraper,
        title_footnote_num=title_fn_num,
        enacted_at=enacted_at,
        description=description,
    )
    tmp = output.with_suffix(".md.tmp")
    tmp.write_text(md, encoding="utf-8")
    tmp.replace(output)
    numbering = _NUMBERING.items
    _NUMBERING.items = None
    num_dir = output.parent / ".numbering"
    num_dir.mkdir(exist_ok=True)
    (num_dir / f"act-{act_number}.json").write_text(
        json.dumps({"act": int(act_number), "items": numbering}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    log.info("act-%s: saved -> %s  (%d chars)", act_number, output, len(md))


# ---------------------------------------------------------------------------
# Batch scraping
# ---------------------------------------------------------------------------

_PROGRESS_LOCK = threading.Lock()


def _load_progress(output_dir: Path) -> set[int]:
    """Load the set of successfully completed act IDs from the checkpoint file.

    @param output_dir  Directory containing .progress.json.
    @return            Set of completed act ID integers.
    """
    p = output_dir / ".progress.json"
    try:
        return set(json.loads(p.read_text())["completed"])
    except (OSError, KeyError, json.JSONDecodeError):
        return set()


def _save_progress(output_dir: Path, completed: set[int]) -> None:
    """Atomically write the checkpoint file.

    @param output_dir  Directory to write .progress.json into.
    @param completed   Set of completed act ID integers.
    """
    p = output_dir / ".progress.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"completed": sorted(completed)}))
    tmp.replace(p)


def _scrape_all_acts(
    output_dir: Path,
    delay: float,
    max_retries: int,
    cache_dir: Optional[Path],
    workers: int,
    skip_existing: bool,
    act_from: int,
    act_to: int,
    force_refresh: bool,
    log: logging.Logger,
) -> None:
    """Scrape a range of acts in parallel and write Markdown files.

    Uses a ThreadPoolExecutor so multiple acts are fetched concurrently.
    Progress is checkpointed to .progress.json after each success so
    interrupted runs can resume without --skip-existing. After all workers
    finish, generate_index() writes act-index.md in output_dir.parent.

    @param output_dir    Directory to write act-N.md files.
    @param delay         Per-worker minimum seconds between requests.
    @param max_retries   Maximum retry attempts per request per worker.
    @param cache_dir     Shared HTML cache directory, or None.
    @param workers       Number of parallel worker threads.
    @param skip_existing Skip acts whose output file already exists.
    @param act_from      First act ID in the range (inclusive).
    @param act_to        Last act ID in the range (inclusive).
    @param force_refresh Bypass cache for all fetches.
    @param log           Logger instance.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    acts = list(range(act_from, act_to + 1))
    completed = _load_progress(output_dir)

    def run_one(act_num: int) -> tuple[int, str]:
        out_file = output_dir / f"act-{act_num}.md"
        if act_num in completed or (skip_existing and out_file.exists()):
            return act_num, "skip"
        try:
            scrape_act(
                str(act_num),
                out_file,
                delay,
                max_retries,
                cache_dir,
                log,
                force_refresh=force_refresh,
            )
            with _PROGRESS_LOCK:
                completed.add(act_num)
                _save_progress(output_dir, completed)
            return act_num, "ok"
        except Exception as e:
            return act_num, f"fail: {e}"

    ok = skip = fail = 0

    if _RICH:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
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
            progress.update(
                task, description=f"Done: {ok} ok, {skip} skipped, {fail} failed"
            )
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
    relink_missing_acts(output_dir, log)
    generate_index(output_dir, output_dir.parent / "act-index.md", log)


# ---------------------------------------------------------------------------
# Index generation
# ---------------------------------------------------------------------------


_RE_ACT_WIKILINK = re.compile(r"\[\[act-(\d+)(?:#\^[A-Za-z0-9-]+)?\\\|([^\]]*)\]\]")


def relink_missing_acts(output_dir: Path, log: logging.Logger) -> int:
    """Point wikilinks to acts that were not scraped at the official page instead.

    The source site links to act numbers it no longer serves. A wikilink to a file that does not
    exist is a dead link, so it becomes [text](official url).

    @param output_dir  Directory containing the act-*.md files.
    @param log         Logger instance.
    @return            Number of links rewritten.
    """
    present = {p.stem for p in output_dir.glob("act-[0-9]*.md")}
    changed = 0

    def fix(m: re.Match) -> str:
        nonlocal changed
        if f"act-{m.group(1)}" in present:
            return m.group(0)
        changed += 1
        return f"[{m.group(2)}]({BASE_URL}/act-{m.group(1)}.html)"

    for path in output_dir.glob("act-[0-9]*.md"):
        text = path.read_text(encoding="utf-8")
        new = _RE_ACT_WIKILINK.sub(fix, text)
        if new != text:
            path.write_text(new, encoding="utf-8")
    if changed:
        log.info("Pointed %d links to missing acts at the official site", changed)
    return changed


def generate_index(output_dir: Path, index_file: Path, log: logging.Logger) -> None:
    """Read YAML frontmatter from all act-*.md files and write a Markdown index table.

    Scans output_dir for files matching "act-*.md", parses each file's
    YAML frontmatter for act_id, title, and description, then writes
    act-index.md with a GFM table sorted numerically by act_id.

    Act names are rendered as [[act-N\\|Title]] wikilinks. Pipe characters
    in descriptions are escaped as \\| to protect table cell boundaries.

    @param output_dir  Directory containing the act-*.md files.
    @param index_file  Destination path for act-index.md.
    @param log         Logger instance.
    """
    _RE_FM = re.compile(r"^---\n(.*?)\n---", re.DOTALL)

    def _read_fm(text: str) -> dict[str, str]:
        m = _RE_FM.match(text)
        if not m:
            return {}
        result: dict[str, str] = {}
        for line in m.group(1).splitlines():
            kv = re.match(r"^(\w+):\s*(.*)", line)
            if kv:
                result[kv.group(1)] = kv.group(2).strip().strip('"')
        return result

    def _sort_key(p: Path) -> int:
        m = re.search(r"\d+", p.stem)
        return int(m.group()) if m else 0

    rows: list[tuple[str, str, str, str]] = []
    for md_file in sorted(output_dir.glob("act-*.md"), key=_sort_key):
        try:
            fm = _read_fm(md_file.read_text(encoding="utf-8"))
        except OSError:
            continue
        act_id = fm.get("act_id", "").strip()
        title = fm.get("title", "").strip()
        year = fm.get("year", "").strip()
        desc = fm.get("description", "").strip().replace("|", "\\|")
        if act_id and title:
            rows.append((act_id, title, year, desc))

    lines = [
        "# Act Index",
        "",
        f"*{len(rows)} acts*",
        "",
        "| Act Number | Year | Act Name | Description |",
        "| --- | --- | --- | --- |",
    ]
    for act_id, title, year, desc in rows:
        lines.append(f"| {act_id} | {year} | [[act-{act_id}\\|{title}]] | {desc} |")
    lines.append("")

    index_file.write_text("\n".join(lines), encoding="utf-8")
    log.info("Index written -> %s  (%d acts)", index_file, len(rows))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.command()
@click.option(
    "--act",
    "act_number",
    default=None,
    help="Act number from bdlaws.minlaw.gov.bd (e.g. 1710 for CrPC).",
)
@click.option(
    "--all",
    "scrape_all",
    is_flag=True,
    default=False,
    help="Scrape all acts (1–1719) in parallel.",
)
@click.option(
    "--output",
    "-o",
    default=None,
    help="Output Markdown file (single-act mode). Defaults to act-<number>.md.",
)
@click.option(
    "--output-dir",
    default="acts",
    show_default=True,
    help="Output directory for --all mode.",
)
@click.option(
    "--delay",
    "-d",
    default=0.3,
    show_default=True,
    type=float,
    help="Min seconds between HTTP requests per worker.",
)
@click.option(
    "--retries",
    "-r",
    default=4,
    show_default=True,
    type=int,
    help="Max retry attempts per request.",
)
@click.option(
    "--cache-dir",
    default="cache",
    show_default=True,
    type=click.Path(),
    help="Directory for raw HTML cache.",
)
@click.option(
    "--workers",
    "-w",
    default=4,
    show_default=True,
    type=int,
    help="Parallel workers for --all mode.",
)
@click.option(
    "--skip-existing",
    is_flag=True,
    default=False,
    help="Skip acts that already have an output file (--all mode).",
)
@click.option(
    "--from",
    "act_from",
    default=1,
    show_default=True,
    type=int,
    help="First act number to scrape in --all mode.",
)
@click.option(
    "--to",
    "act_to",
    default=1719,
    show_default=True,
    type=int,
    help="Last act number to scrape in --all mode (inclusive).",
)
@click.option(
    "--force-refresh",
    is_flag=True,
    default=False,
    help="Ignore disk cache and fetch live pages.",
)
@click.option(
    "--verbose", "-v", is_flag=True, default=False, help="Enable DEBUG-level logging."
)
def main(
    act_number: Optional[str],
    scrape_all: bool,
    output: Optional[str],
    output_dir: str,
    delay: float,
    retries: int,
    cache_dir: Optional[str],
    workers: int,
    skip_existing: bool,
    act_from: int,
    act_to: int,
    force_refresh: bool,
    verbose: bool,
) -> None:
    """Scrape Bangladesh law acts and save as Obsidian-compatible Markdown.

    Single act:  --act 1710
    All acts:    --all  (parallel; resumes from .progress.json; writes act-index.md)
    Range:       --all --from 500 --to 600
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
                output_dir=Path(output_dir),
                delay=delay,
                max_retries=retries,
                cache_dir=cache_path,
                workers=workers,
                skip_existing=skip_existing,
                act_from=act_from,
                act_to=act_to,
                force_refresh=force_refresh,
                log=log,
            )
        except KeyboardInterrupt:
            log.warning("Interrupted.")
            sys.exit(130)
    else:
        output_path = Path(output) if output else Path(f"act-{act_number}.md")
        try:
            scrape_act(
                act_number=act_number,
                output=output_path,
                delay=delay,
                max_retries=retries,
                cache_dir=cache_path,
                log=log,
                force_refresh=force_refresh,
            )
        except requests.HTTPError as e:
            log.error("HTTP error: %s", e)
            sys.exit(1)
        except requests.ConnectionError as e:
            log.error("Network error: %s", e)
            sys.exit(1)
        except ValueError as e:
            log.error("Parse error: %s", e)
            sys.exit(1)
        except KeyboardInterrupt:
            log.warning("Interrupted.")
            sys.exit(130)


if __name__ == "__main__":
    main()
