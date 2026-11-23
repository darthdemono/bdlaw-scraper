"""nasirlawsite.com cached Word-HTML pages to bdlaws-style Markdown.

The pages are Microsoft Word 11 "Save as web page" output (cp1252 or UTF-8, mso styles, conditional comments, o:p and
st1 tags) or text pasted from pakistancode.gov.pk. Everything Word-specific is dropped. What is kept and rebuilt:
headings, emphasis, links, tables, section headings with anchors, nested clause lists, a Preamble callout, the
enactment date and act number in the front matter, footnotes (both source styles) and wikilinks to other acts.

usage: python -m bdlaw_scraper.nasir_md CACHE_ROOT OUT_DIR [--acts VAULT_ACTS_DIR] [--only LIST.csv]
"""
from __future__ import annotations

import codecs
import csv
import hashlib
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag
from bs4.element import Comment, NavigableString

try:
    import lxml  # noqa: F401  (flattens Word's unclosed tags; html.parser nests them hundreds deep)
    PARSER = "lxml"
except ImportError:
    PARSER = "html.parser"
sys.setrecursionlimit(20000)
SITE = "nasirlawsite.com"
ROMAN = r"[IVXLCDM]+"
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], 1)}


# ---------------------------------------------------------------- decoding

def cache_path(root: Path, url: str) -> Path:
    key = hashlib.sha1(url.encode()).hexdigest()
    return root / "cache" / key[:2] / key


MOJIBAKE = re.compile("(?:[Â-ô][\u0080-¿ŒœŠšŸŽžƒˆ˜–-›€™]+)+")
codecs.register_error("c1enc", lambda e: (bytes(ord(c) for c in e.object[e.start:e.end]), e.end))  # the reverse of c1
codecs.register_error("c1", lambda e: (chr(e.object[e.start]), e.start + 1))  # cp1252 leaves 0x81 0x8d 0x8f 0x90 0x9d undefined; keep the byte


def _unmojibake(m: re.Match) -> str:
    try:
        return m.group().encode("cp1252", "c1enc").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return m.group()


def _decode(raw: bytes) -> str:
    m = re.search(rb"charset=([\w-]+)", raw[:3000])
    for enc in ([m.group(1).decode()] if m else []) + ["utf-8", "cp1252"]:
        try:
            return raw.decode(enc, errors="c1") if enc.lower().startswith(("cp1252", "windows-1252")) else raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("cp1252", "c1")


def decode(raw: bytes) -> str:
    """Some pages are UTF-8 under a windows-1252 label: repair the runs that read as UTF-8 seen through cp1252."""
    text = MOJIBAKE.sub(_unmojibake, _decode(raw).replace("­", "").replace("&shy;", ""))
    text = re.sub(r"<!\[if [^\]]*\]>.*?<!\[endif\]>", "", text, flags=re.S)  # Word's downlevel-revealed duplicates of footnote marks
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


# ---------------------------------------------------------------- inline

LEGACY_FONT = re.compile(r"jameel|nastaleeq|noori|nafees|urdu|alvi|lateef|mehr", re.I)


def _ws(text: str) -> str:
    return re.sub(r"[ \t\r\n ]+", " ", text)


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("*", "\\*")


def _style(tag: Tag) -> str:
    return str(tag.get("style") or "").replace("\n", " ").lower()


def _wrap(mark: str, text: str) -> str:
    inner = text.strip()
    if not inner:
        return text
    lead = text[: len(text) - len(text.lstrip())]
    tail = text[len(text.rstrip()):]
    return f"{lead}{mark}{inner}{mark}{tail}"


def _inline(node, base: str) -> str:
    """Inline Markdown of a node: bold, italic, links, line breaks, footnote markers."""
    out = []
    for ch in node.children:
        if isinstance(ch, Comment):
            continue
        if isinstance(ch, NavigableString):
            out.append(_esc(_ws(str(ch))))
            continue
        name = ch.name.lower()
        if name == "br":
            out.append("\n")
        elif LEGACY_FONT.search(_style(ch)):
            raw = _ws(ch.get_text()).strip()
            out.append(f"[Urdu script omitted]<!-- legacy-font text: {raw.replace('--', '- -')} -->" if raw else "")
        elif name in ("b", "strong"):
            out.append(_wrap("**", _inline(ch, base)))
        elif name in ("i", "em"):
            out.append(_wrap("*", _inline(ch, base)))
        elif name == "sup":
            t = _ws(ch.get_text()).strip()
            if re.fullmatch(r"[_\W]+", t or "_"):
                continue
            out.append(t if t.startswith("⟦") else (f"<sup>{_esc(t)}</sup>" if t else ""))
        elif name == "a" and ch.get("href") and not ch["href"].startswith(("#", "mailto:")):
            t = _inline(ch, base).strip()
            out.append(f"[{t}]({urljoin(base, ch['href'])})" if t else "")
        elif name in ("img", "script", "style"):
            continue
        else:  # span, font, u, o:p, st1:*, ...
            out.append(_inline(ch, base))
    return "".join(out)


def _merge_emphasis(text: str) -> str:
    """Word splits one run of bold or italic text into many spans: join them, drop empty marks."""
    for _ in range(3):
        text = re.sub(r"\*\*(\s*)\*\*", r"\1", text)
        text = re.sub(r"(?<![*\\])\*(\s+)\*(?!\*)", r"\1", text)
        text = re.sub(r"\*\*([^*\n]*?)\*\*(\s*)\*\*([^*\n]*?)\*\*", lambda m: f"**{m.group(1)}{m.group(2)}{m.group(3)}**", text)
    text = re.sub(r"\*+(⟦[^⟧]+⟧)\*+", r"\1", text)
    return text


def _clean(text: str) -> str:
    text = _merge_emphasis(text)
    text = re.sub(r"[ ]*\n[ ]*", "\n", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


def _font_pt(tag: Tag) -> float:
    sizes = [float(x) for x in re.findall(r"font-size:\s*([\d.]+)pt", " ".join(_style(t) for t in tag.find_all(True)))]
    return max(sizes) if sizes else 0.0


def _is_bold_all(tag: Tag) -> bool:
    text = _ws(tag.get_text()).strip()
    if not text:
        return False
    bold = "".join(_ws(b.get_text()) for b in tag.find_all(["b", "strong"], recursive=True))
    return len(bold.replace(" ", "")) >= len(text.replace(" ", "")) * 0.95


def _table(tag: Tag, base: str) -> str:
    rows = []
    for tr in tag.find_all("tr"):
        cells = [_clean(_inline(td, base)).replace("\n", " ").replace("|", "\\|") for td in tr.find_all(["td", "th"], recursive=False)]
        if any(cells):
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


# ---------------------------------------------------------------- footnote extraction (HTML level)

def _prepare(soup: BeautifulSoup, base: str) -> dict[str, str]:
    """Replace footnote reference anchors by markers and lift the footnote bodies out. Returns Word notes by number."""
    wnotes: dict[str, str] = {}
    for div in soup.find_all(id=re.compile(r"^ftn\d+$")):  # Word style: <div id=ftnN> at the end of the page
        n = div["id"][3:]
        for a in div.find_all("a", attrs={"name": re.compile(r"^_ftn\d+$")}):
            a.decompose()
        txt = _clean(_inline(div, base))
        txt = _note_text(txt)
        wnotes[n] = txt
        div.decompose()
    for a in soup.find_all("a", attrs={"name": re.compile(r"^_ftnref\d+$")}):
        n = a["name"][7:]
        nxt = a.find_next_sibling()
        if nxt is not None and re.fullmatch(r"\s*\[?\d+\]?\s*", nxt.get_text()):
            nxt.decompose()
        a.replace_with(NavigableString(f"⟦W:{n}⟧"))
    for p in soup.find_all("p"):  # pakistancode style: italic paragraph that opens with a linked number
        first = next((c for c in p.children if isinstance(c, Tag)), None)
        if first is not None and first.name == "i":
            sup = first.find("sup")
            link = sup.find("a", href=re.compile("pakistancode")) if sup else None
            if link and re.fullmatch(r"\d+", link.get_text().strip()):
                n = link.get_text().strip()
                sup.decompose()
                p.insert(0, NavigableString(f"⟦NP:{n}⟧"))
    for sup in soup.find_all("sup"):
        link = sup.find("a", href=re.compile("pakistancode"))
        if link and re.fullmatch(r"\d+", link.get_text().strip()):
            sup.replace_with(NavigableString(f"⟦P:{link.get_text().strip()}⟧"))
    return wnotes


def _note_text(txt: str) -> str:
    txt = re.sub(r"^[*\s]*(?:\\?\[\d+\\?\][*\s]*)+", "", txt)
    txt = re.sub(r"\s*Go to \[.*$", "", txt, flags=re.S)
    txt = re.sub(r"^<sup>\\?\[?\d+\\?\]?</sup>\**\s*", "", txt.strip())
    return _balance(txt)


def _balance(text: str) -> str:
    """A line whose bold or italic marks do not pair up loses them all: wrong emphasis is better than a page of stray stars."""
    plain = re.sub(r"\\\*", "", text)
    if plain.count("**") % 2 or len(re.findall(r"(?<!\*)\*(?!\*)", plain)) % 2:
        return re.sub(r"(?<!\\)\*+", "", text)
    return text


def resolve_footnotes(blocks: list[str], wnotes: dict[str, str]) -> tuple[list[str], list[str]]:
    """Marker pass: turn markers into [^n] in order of first reference and return the footnote definitions."""
    refs: list[tuple[str, str]] = []
    pending: dict[str, list[int]] = {}
    target: dict[int, str] = {}
    defs: dict[str, str] = {}
    out: list[str] = []

    def mark(m: re.Match) -> str:
        kind, n = m.group(1), m.group(2)
        refs.append((kind, n))
        k = len(refs) - 1
        if kind == "W":
            target[k] = f"W{n}"
        else:
            pending.setdefault(n, []).append(k)
        return f"⟦R{k}⟧"

    def plain_sups(b: str) -> str:
        n0 = re.match(r"^<sup>(\d+)</sup>\s*(?!\[)(\S)", b)
        if n0 and not b.startswith("#"):  # a note at the foot of the page: it does not open with the amended words in [ ]
            return f"⟦NP:{n0.group(1)}⟧ " + b[n0.end() - 1:]
        return re.sub(r"<sup>(\d{1,3})</sup>", r"⟦P:\1⟧", b)

    blocks = [plain_sups(b) for b in blocks]
    for b in blocks:
        m = re.match(r"^⟦NP:(\d+)⟧\s*(.*)$", b, re.S)
        if m:
            n, text = m.group(1), _note_text(m.group(2))
            key = f"P{len(defs)}"
            defs[key] = text
            for k in pending.pop(n, []):
                target[k] = key
            continue
        out.append(re.sub(r"⟦([WP]):(\d+)⟧", mark, b))
    for k, (kind, n) in enumerate(refs):
        if k not in target:
            target[k] = ""
    number: dict[str, int] = {}
    for k in range(len(refs)):
        t = target[k]
        if t and t not in number and (defs.get(t) or wnotes.get(t[1:])):
            number[t] = len(number) + 1

    def final(m: re.Match) -> str:
        k = int(m.group(1))
        t = target[k]
        return f"[^{number[t]}]" if t in number else ""

    out = [re.sub(r"⟦R(\d+)⟧", final, b) for b in out]
    texts = {number[t]: (defs.get(t) or wnotes.get(t[1:], "")) for t in number}
    return [_strip_amendment_brackets(b) for b in out], [f"[^{i}]: {_balance(texts[i])}" for i in sorted(texts)]


def _strip_amendment_brackets(text: str) -> str:
    """The footnote already says the words were substituted or inserted, so the [ ] around them is dropped, as bdlaws does."""
    def fix(m: re.Match) -> str:
        i, depth = m.end(), 1
        while i < len(text) and depth:
            depth += {"[": 1, "]": -1}.get(text[i], 0)
            i += 1
        return m.group(1) + text[m.end():i - 1] + "\u0000" if depth == 0 else m.group(0)

    for _ in range(40):
        m = re.search(r"(\[\^\d+\])\s*\[(?!\^)", text)
        if not m:
            break
        i, depth = m.end(), 1
        while i < len(text) and depth:
            depth += {"[": 1, "]": -1}.get(text[i], 0)
            i += 1
        if depth:
            break
        inner = text[m.end():i - 1]
        inner = "~~\\* \\* \\*~~" if re.fullmatch(r"\s*(\\\*\s*)+", inner) else inner
        text = text[: m.start()] + m.group(1) + inner + text[i:]
    return text


# ---------------------------------------------------------------- body to blocks

def _blocks(parent: Tag):
    for ch in parent.children:
        if isinstance(ch, Tag):
            if ch.name.lower() in ("div", "blockquote", "center", "font", "span", "form") and ch.find(["p", "table", "ul", "ol", "h1", "h2", "h3", "h4", "li"]):
                yield from _blocks(ch)
            else:
                yield ch


NAV = re.compile(r"^(?:Go to\b.*|(?:\[[^\]]*\]\([^)]*\)[\s|:\-–]*)+|\W*Updated:.*|\W*Home\W*)$", re.S)


def body_to_blocks(soup: BeautifulSoup, base: str) -> tuple[list[str], str, dict[str, str]]:
    for t in soup(["script", "style", "head"]):
        t.decompose()
    for t in soup.find_all(re.compile(r"^(o|st1|w|v):", re.I)):
        t.unwrap()
    wnotes = _prepare(soup, base)
    body = soup.body or soup
    out: list[str] = []
    title = ""
    for blk in _blocks(body):
        name = blk.name.lower()
        if name == "table":
            md = _table(blk, base)
            if md:
                out.append(md)
            continue
        if name in ("ul", "ol"):
            for i, li in enumerate(blk.find_all("li", recursive=False), 1):
                txt = _clean(_inline(li, base))
                if txt:
                    out.append(f"{i}. {txt}" if name == "ol" else f"- {txt}")
            continue
        txt = re.sub(r"-{2,}", "—", _clean(_inline(blk, base)))
        if not txt or re.fullmatch(r"[\W_]+", txt) or NAV.match(txt):
            continue
        center = "center" in (blk.get("align") or "") or "text-align:center" in _style(blk).replace(" ", "")
        m = re.match(r"h([1-6])$", name)
        if m:
            level = int(m.group(1))
        elif center and _is_bold_all(blk) and len(txt) < 130 and not re.match(r"^\W*(An?|To|The)\s+(Act|Ordinance|Order|Bill|Regulation|make|provide|consolidate|amend)", txt):
            level = 1 if not title else (2 if _font_pt(blk) >= 14 else 3)
        elif _is_bold_all(blk) and len(txt) < 120 and re.match(r"^\W*(CHAPTER|PART|SCHEDULE|SECTION|ARTICLE)\b", txt, re.I):
            level = 2
        else:
            level = 0
        if level:
            plain = re.sub(r"(?<!\\)[*_]", "", txt).replace("\n", " ").strip()
            if level == 1 and not title:
                title = re.sub(r"⟦[^⟧]+⟧", "", plain).strip()
            out.append("#" * level + " " + plain)
        else:
            out.append(txt.replace("\n", "  \n"))
    return out, title, wnotes


# ---------------------------------------------------------------- sections and clauses

LEAD = r"^((?:\[\^\d+\]\s*)*)[.;:\s]*"
SEC_BOLD = re.compile(LEAD + r"\*\*(\d+[A-Z]{0,2})\.\s+(.{2,160}?)\*\*\s*(?:—|–|-)*\s*(.*)$", re.S)
SEC_DASH = re.compile(LEAD + r"(\d+[A-Z]{0,2})\.\s+(.{2,110}?)\s*(?:—|–|-)+\s*(.*)$", re.S)
CLAUSE = re.compile(LEAD + r"\*{0,2}(?:\(([a-z]{1,4}|\d{1,3}[a-z]?|[A-Z]{1,3})\)|([a-z])\))\*{0,2}\s*(.*)$", re.S)
LABEL_ONLY = re.compile(r"^\*{0,2}\(([a-zA-Z0-9]{1,4})\)\*{0,2}$")
ARTICLE = re.compile(r"^#{1,3}\s+Article\s+(\S+)", re.I)
ROMANS = {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii", "xiii", "xiv", "xv"}
LINE_SPLIT = re.compile(r"  \n(?=\*{0,2}(?:\([a-zA-Z0-9]{1,4}\)|[a-z]\)))")
PREAMBLE = re.compile(r"^\W*(?:\*\*)?(?:Preamble\W*|An\s+(?:Act|Ordinance|Order|Regulation)\s+to\b|A\s+Bill\b|WHEREAS\b|AND\s+WHEREAS\b|Whereas\b|NOW,?\s+THEREFORE\b|Now,?\s+therefore\b|It\s+is\s+hereby\s+enacted\b|Be\s+it\s+enacted\b)", re.I)
DATE = re.compile(r"^\W*\[?\s*(\d{1,2})\s*(?:st|nd|rd|th)?\s+([A-Za-z]+),?\s+(\d{4})\s*\]?\W*$")
CALLOUT = re.compile(r"^\*{0,2}(Explanation|Exception|Illustrations?)\*{0,2}\s*(\d*)\s*[.\u2013\u2014:-]*\*{0,2}\s*(.*)$", re.S | re.I)
ACTNO = re.compile(r"^\W*\(?(?:ACT|ORDINANCE|ORDER|REGULATION|PRESIDENT.S ORDER)\s+(?:No\.?)?\s*[\w ]+\s+of\s+\d{4}\W*$", re.I)


def _kind(label: str, prev: str | None, nxt: str | None) -> str:
    """digit, letter, roman or upper. i, v and x are roman when the roman successor follows, letters when the alphabet runs through them."""
    if label.isdigit() or re.fullmatch(r"\d+[a-z]", label):
        return "digit"
    if label in ("i", "v", "x"):
        roman_next = {"i": "ii", "v": "vi", "x": "xi"}[label]
        if nxt == roman_next:
            return "roman"
        if prev == {"i": "h", "v": "u", "x": "w"}[label] or nxt == {"i": "j", "v": "w", "x": "y"}[label]:
            return "letter"
        return "roman"
    if label in ROMANS:
        return "roman"
    return "letter" if label.islower() else "upper"


def _plain(block: str) -> tuple[str, list[int], str]:
    """The block without footnote marks, emphasis and brackets, an index map back into it, and the marks that were dropped."""
    out, idx, marks = [], [], []
    i = 0
    while i < len(block) and len(out) < 220:
        m = re.match(r"\[\^\d+\]", block[i:])
        if m:
            marks.append((i, m.group()))
            i += m.end()
        elif block.startswith("**", i) or block[i] in "[]":
            i += 2 if block.startswith("**", i) else 1
        else:
            out.append(block[i])
            idx.append(i)
            i += 1
    return "".join(out), idx, marks


DASHED = re.compile(r"^[\s.;:]*(\d+[A-Z]{0,2})\.\s+([^.\u2014\u2013]{2,110}?)\s*\.?\s*(?:[\u2014\u2013]|-{1,2})\s*")
PERIOD = re.compile(r"^[\s.;:]*(\d+[A-Z]{0,2})\.\s+([^.:\u2014\u2013]{2,90}?)[.:]\s*(?=\S)")
NUMONLY = re.compile(r"^((?:\[\^\d+\]\s*)*)[\s.;:]*\*\*(\d+[A-Z]{0,2})\.\s*[\u2014\u2013-]*\s*\*\*\s*[\u2014\u2013-]*\s*(.*)$", re.S)
BOLDTITLE = re.compile(r"^((?:\[\^\d+\]\s*)*)[\s.;:\[]*\*\*\[?(\d+(?:\.?[A-Z]{1,2})?)\.\s+(.{2,260}?)\*\*(.*)$", re.S)


def _section(block: str, period_mode: bool = False):
    """(lead marks, number, title, rest) when the block opens a section, else None."""
    if block.startswith("#") or block.startswith(">"):
        return None
    bm = BOLDTITLE.match(block)
    if bm and "\n" not in bm.group(3):
        return bm.group(1), bm.group(2), re.sub(r"\s+", " ", bm.group(3)).strip(" .*:"), re.sub(r"^[\s:.\u2014\u2013-]+", "", bm.group(4))
    nm = NUMONLY.match(block)
    if nm:  # a bold number with no title: the section has no marginal note
        return nm.group(1), nm.group(2), "", nm.group(3)
    plain, idx, allmarks = _plain(block)
    bold = bool(re.match(r"^[^\w]{0,4}\*\*[^\w]{0,3}\d", block))
    for rx, ok in ((DASHED, True), (PERIOD, bold or period_mode)):
        m = rx.match(plain)
        if m and ok and "\n" not in m.group(2) and not re.search(r"[;:]\s+[A-Z]", m.group(2)):
            end = idx[m.end() - 1] + 1 if m.end() - 1 < len(idx) else len(block)
            rest = re.sub(r"^[\s*\]\u2014\u2013.-]+", "", block[end:])
            return "".join(t for pos, t in allmarks if pos < end), m.group(1), m.group(2).strip(" .*"), rest
    return None


def _clause(block: str):
    m = CLAUSE.match(block)
    return (m.group(1), m.group(2) or m.group(3), m.group(4)) if m else None


def structure(blocks: list[str]) -> tuple[list[str], dict]:
    """Section headings (##### Section N: Title ^sec-N), nested clause lists, Preamble callout, enactment date."""
    meta: dict = {}
    queue: list[str] = []
    for blk in blocks:
        parts = LINE_SPLIT.split(blk)
        queue.extend(parts)
    merged: list[str] = []
    i = 0
    while i < len(queue):  # a label on a line of its own, the text in the next block
        if LABEL_ONLY.match(queue[i]) and i + 1 < len(queue) and not queue[i + 1].startswith("#"):
            merged.append(f"{queue[i]} {queue[i + 1]}")
            i += 2
        else:
            merged.append(queue[i])
            i += 1
    queue = merged

    def next_label(j: int) -> str | None:
        for b in queue[j:j + 4]:
            c = _clause(b)
            if c:
                return c[0] or c[1]
        return None

    nums = [int(m[1]) for b in queue if (m := _section(b, True)) and m[1].isdigit()]
    period_mode = len(nums) >= 3 and nums == sorted(nums) and nums[0] == 1  # numbered "N. Title. Text" blocks that run 1, 2, 3...
    out: list[str] = []
    sec: str | None = None
    n = 0
    prev: str | None = None
    order: list[str] = []
    pre: list[str] = []
    pre_at: int | None = None
    started = False
    j = 0
    while j < len(queue):
        block = queue[j]
        j += 1
        if "enacted_at" not in meta and not started and not block.startswith("#"):
            clean = re.sub(r"\s+", " ", re.sub(r"</?sup>", " ", block.replace("*", "")))
            block = clean if DATE.match(clean) else block
            d = DATE.match(block)
            if d and d.group(2).lower() in MONTHS:
                meta["enacted_at"] = f"{int(d.group(3)):04d}-{MONTHS[d.group(2).lower()]:02d}-{int(d.group(1)):02d}"
                continue
            a = re.search(r"\[\s*(\d{1,2})\s*(?:st|nd|rd|th)?\s+([A-Za-z]+),?\s+(\d{4})\.?\s*\]", block) or re.search(r"\(\s*(\d{1,2})\s*(?:st|nd|rd|th)?\s+([A-Za-z]+),?\s+(\d{4})\.?\s*\)", block)
            if a and a.group(2).lower() in MONTHS:
                meta["enacted_at"] = f"{int(a.group(3)):04d}-{MONTHS[a.group(2).lower()]:02d}-{int(a.group(1)):02d}"
            elif len(block) < 90 and not re.search(r"Gazette|Gaz\.|dated|No\.", block) and (f := re.search(r"(\d{1,2})\s*(?:st|nd|rd|th)?\s+([A-Za-z]+),?\s+(\d{4})", block)) and f.group(2).lower() in MONTHS:
                meta["enacted_at"] = f"{int(f.group(3)):04d}-{MONTHS[f.group(2).lower()]:02d}-{int(f.group(1)):02d}"
            else:
                u = re.match(r"^\W*([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})\W*$", block)
                if u and u.group(1).lower() in MONTHS:
                    meta["enacted_at"] = f"{int(u.group(3)):04d}-{MONTHS[u.group(1).lower()]:02d}-{int(u.group(2)):02d}"
                    continue
        if not started and ACTNO.match(re.sub(r"</?sup>|\u27e6[^\u27e7]*\u27e7|\d{1,2}(?=ACT|ORD)", "", re.sub(r"^#+\s*", "", block))):
            continue
        a = ARTICLE.match(block)
        if a:
            sec, n, prev, order, started = f"art-{a.group(1).lower()}", 0, None, [], True
            out.append(block)
            continue
        m = _section(block, period_mode)
        if m:
            started = True
            lead, sec, ttl, rest = m
            n, prev, order = 0, None, []
            out.append(f"##### Section {sec}{': ' + ttl if ttl else ''} ^sec-{sec.lower()}")
            rest = (lead + rest).strip()
            if rest:
                queue[j:j] = LINE_SPLIT.split(rest)
            continue
        if not started and PREAMBLE.match(block):
            pre.append(block)
            if pre_at is None:
                pre_at = len(out)
                out.append("\u0000PRE")
            continue
        c = _clause(block) if sec else None
        if not c and sec and sec.startswith("art-") and (np := re.match(r"^(\d{1,2})\.\s+(\S.*)$", block, re.S)):
            c = ("", np.group(1), np.group(2))  # a numbered paragraph of a treaty article
        if c:
            lead, label, text = c
            kind = _kind(label, prev, next_label(j))
            prev_is_item = bool(out) and bool(re.search(r" \^[\w-]+-\d+$", out[-1]))
            if not prev_is_item:
                order = []
            if kind in order:
                del order[order.index(kind) + 1:]
            else:
                order.append(kind)
            lvl = order.index(kind)
            prev = label
            n += 1
            out.append(f"{'    ' * lvl}1. <!--n:({label})--> {(lead + text).strip()} ^sec-{sec.lower().replace('art-', 'art-')}-{n}")
            continue
        co = CALLOUT.match(block)
        if co:
            out.append(f"> [!{co.group(1).title().rstrip('s') if co.group(1).lower() != 'illustrations' else 'Illustrations'}]\n> " + co.group(3).strip().replace("\n", "\n> "))
            continue
        ch = re.match(r"^#{1,3}\s+((?:Chapter|Part)\s+\S+)\s*$", block, re.I)
        if ch and j < len(queue) and re.match(r"^#{1,4}\s+[^#]{2,100}$", queue[j]):
            out.append("### " + ch.group(1) + ": " + re.sub(r"^#+\s+", "", queue[j]))
            j += 1
            continue
        if out and block[:1].islower() and re.search(r" \^sec-[\w-]+-\d+$", out[-1]):  # text that resumes after a page break
            head, _, anchor = out[-1].rpartition(" ^")
            out[-1] = f"{head} {block} ^{anchor}"
            continue
        out.append(block)
    if pre_at is not None:
        pre = [re.sub(r"^#+\s*", "", re.sub(r"^\W*Preamble\W*", "", p.replace("**", "").strip(), flags=re.I)) or p for p in pre]
        out[pre_at] = "> [!Preamble]\n" + "\n>\n".join("> " + p.replace("\n", "\n> ") for p in pre)
        meta["description"] = re.sub(r"\s+", " ", re.sub(r"[*>]", "", pre[0])).strip()[:300]
    out = [b.replace(" ^", " ^", 1) for b in out]
    text = "\n\n".join(out)
    text = re.sub(r"(\^[\w-]+-\d+)\n\n(?=\s*1\. <!--n:)", r"\1\n", text)
    return text.split("\n\n"), meta


# ---------------------------------------------------------------- links

_NORM = re.compile(r"[^a-z0-9 ]")


def norm_title(t: str) -> str:
    t = re.sub(r"\(.*?\)", "", t)
    t = re.sub(r"['’`]", "", t.strip())
    t = re.sub(r"^the\s+", "", t, flags=re.I)
    return re.sub(r"\s+", " ", _NORM.sub(" ", t.lower())).strip()


def build_title_index(acts: Path) -> dict[tuple[str, int], str]:
    """(normalised title without year, year) -> act stem, for citations that give a year but no number."""
    idx: dict[tuple[str, int], str] = {}
    for p in acts.rglob("act-[0-9]*.md"):
        head = p.read_text(encoding="utf-8")[:1500]
        t = re.search(r'^title: "(.*)"', head, re.M)
        y = re.search(r"^act_year: (\d{4})", head, re.M)
        if t and y:
            idx.setdefault((norm_title(re.sub(r",?\s*\d{4}\s*$", "", t.group(1))), int(y.group(1))), p.stem)
    return idx


def build_stem_titles(acts: Path) -> dict[str, str]:
    """act stem -> normalised title without year, to check that a (number, year) hit really is the cited act."""
    out: dict[str, str] = {}
    for p in acts.rglob("act-[0-9]*.md"):
        t = re.search(r'^title: "(.*)"', p.read_text(encoding="utf-8")[:800], re.M)
        if t:
            out[p.stem] = norm_title(re.sub(r",?\s*\d{4}\s*$", "", t.group(1)))
    return out


def link_cites(text: str, cidx: dict, stems: dict[str, str]) -> str:
    """Link "Penal Code (XLV of 1860)" by number and year, but only when the name agrees with the target's title:
    a Pakistani Act No. X of 1997 is not the Bangladeshi Act No. X of 1997."""
    import difflib
    from bdlaw_scraper.citations import _CITE, _group, roman

    def fix(seg: str) -> str:
        def sub(m: re.Match) -> str:
            name = m.group(1)
            stem = cidx.get((_group(m.group(0)), roman(m.group(3)), int(m.group(4))))
            if not stem:
                return m.group(0)
            want = norm_title(re.sub(r"[,\s]*\d{4}\s*$", "", name))
            if want and difflib.SequenceMatcher(None, want, stems.get(stem, "")).ratio() >= 0.72:
                return f"[[{stem}\\|{name}]]{m.group(2)}"
            return m.group(0)
        return _CITE.sub(sub, seg)

    out, pos = [], 0
    for m in _SKIP.finditer(text):
        out.append(fix(text[pos:m.start()]))
        out.append(m.group())
        pos = m.end()
    out.append(fix(text[pos:]))
    return "".join(out)


_TITLED = re.compile(r"((?:The\s+)?(?:[A-Z][\w'’.-]*(?:\s+|\s+and\s+|\s+of\s+|\s*,\s*|\s+\(\w+\)\s+)){0,7}?(?:Code|Act|Ordinance|Order|Regulation))s?,?\s+(\d{4})\b")
_SKIP = re.compile(r"\[\[.*?\]\]|\[[^\]\n]*\]\([^)\n]*\)|<!--.*?-->|\[\^\d+\]", re.S)


def link_titles(text: str, tidx: dict, self_stem: str = "") -> str:
    """Wrap "Name Act, 1913" (a year, no number) in a wikilink when exactly that act is in the index."""
    def fix(seg: str) -> str:
        def sub(m: re.Match) -> str:
            stem = tidx.get((norm_title(re.sub(r"^The\s+", "", m.group(1))), int(m.group(2))))
            return f"[[{stem}\\|{m.group(0)}]]" if stem and stem != self_stem else m.group(0)
        return _TITLED.sub(sub, seg)

    out, pos = [], 0
    for m in _SKIP.finditer(text):
        out.append(fix(text[pos:m.start()]))
        out.append(m.group())
        pos = m.end()
    out.append(fix(text[pos:]))
    return "".join(out)


def link_sections(text: str, stem: str) -> str:
    """"section 5" -> a link to that section's anchor in this same act, unless the text goes on "of the <Other Act>"."""
    have = set(re.findall(r"\^sec-([\w]+)\n?$", text, re.M))
    secs = {m.lower() for m in re.findall(r"^##### Section (\w+):", text, re.M)}

    def fix(seg: str) -> str:
        def sub(m: re.Match) -> str:
            tail = seg[m.end():m.end() + 25]
            if m.group(2).lower() not in secs or re.match(r"\s*(?:\(\w+\)\s*)*(?:of|in)\s+(?:the|this\s+(?!Act|Ordinance))", tail) and not re.match(r"\s*(?:\(\w+\)\s*)*of\s+this", tail):
                return m.group(0)
            return f"[[#^sec-{m.group(2).lower()}\\|{m.group(0)}]]"
        return re.sub(r"\b([Ss]ections?)\s+(\d+[A-Z]{0,2})\b", sub, seg)

    out, pos = [], 0
    for m in re.finditer(_SKIP.pattern + r"|^#+ .*$", text, re.S | re.M):
        out.append(fix(text[pos:m.start()]))
        out.append(m.group())
        pos = m.end()
    out.append(fix(text[pos:]))
    return "".join(out)


# ---------------------------------------------------------------- page

def classify(url: str) -> str:
    parts = [p for p in urlsplit(url).path.split("/") if p]
    return parts[0] if len(parts) > 1 else "root"


def slug(url: str) -> str:
    p = urlsplit(url).path.strip("/")
    p = re.sub(r"\.html?$", "", p, flags=re.I)
    return re.sub(r"[^\w/\-]+", "-", p)


def _q(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', "'")


def _block_before_actno(blocks: list[str]) -> str:
    """The title is the block just above the "(Act XVI of 1927)" line when the first heading is only that line."""
    for k, b in enumerate(blocks[1:], 1):
        if ACTNO.match(re.sub(r"^#+\s*", "", b)) and not blocks[k - 1].startswith((">", "|")):
            t = re.sub(r"\[\^\d+\]|</?sup>|(?<!\\)\*|\u27e6[^\u27e7]*\u27e7", "", re.sub(r"\[\[[^\]|]*\\?\|([^\]]*)\]\]", r"\1", blocks[k - 1]))
            t = re.sub(r"^#+\s*", "", t).strip()
            return re.sub(r"\s+", " ", t) if 3 < len(t) < 160 else ""
    return ""


def _title_block(blocks: list[str], title: str) -> list[str]:
    """The block that only repeats the title (often with a footnote mark) becomes the # heading; other # lines are demoted."""
    want = norm_title(title)
    out, done = [], False
    for b in blocks:
        plain = re.sub(r"\[\^\d+\]|</?sup>|\*|\\", "", re.sub(r"\[\[[^\]|]*\\?\|([^\]]*)\]\]", r"\1", b)).strip()
        if not done and not b.startswith(("#", ">")) and norm_title(plain) == want:
            out.append("# " + title + "".join(re.findall(r"\[\^\d+\]", b)))
            done = True
        elif re.match(r"^#\s", b):
            if not done and norm_title(b[2:]) == want:
                out.append(b)
                done = True
            elif not b.startswith("# " + title):
                out.append("##" + b[1:])
        else:
            out.append(b)
    return out


def convert(raw: bytes, url: str, scraped: str, cidx: dict | None = None, tidx: dict | None = None, stems: dict | None = None) -> tuple[str, str]:
    soup = BeautifulSoup(decode(raw), PARSER)
    head_title = _ws(soup.title.get_text()).strip() if soup.title else ""
    blocks, h1, wnotes = body_to_blocks(soup, url)
    blocks, defs = resolve_footnotes(blocks, wnotes)
    before = _block_before_actno(blocks)  # must run before structure() drops the "(Act XVI of 1927)" line
    blocks, meta = structure(blocks)
    from_head = re.sub(r"\s*\((?:[^()]*\d{4}[^()]*)\)\s*$", "", head_title)
    title = h1 if h1 and not ACTNO.match(h1) and not re.match(r"^\(", h1) else (before or from_head)
    title = re.sub(r"\s+", " ", title or slug(url)).strip()
    blocks = _title_block(blocks, title)
    body = "\n\n".join(blocks)
    stem = "nasir/" + slug(url)
    cite = re.search(rf"(Act|Ordinance|Order|Regulation)\s+No\.?\s*({ROMAN}|\d+)\s+of\s+(\d{{4}})", head_title + " " + body[:800], re.I)
    fm = ["---", f'title: "{_q(title)}"']
    if meta.get("description"):
        fm.append(f'description: "{_q(meta["description"])}"')
    fm += ["tags:", "  - Legal", "  - Pakistan", f"source_url: {url}", f"source: {SITE}", f"kind: {classify(url)}"]
    year = re.search(r"\b(1[6-9]\d\d|20[0-2]\d)\b", title)
    if cite:
        from bdlaw_scraper.citations import roman
        kind, no, yr = cite.group(1).title(), cite.group(2).upper(), int(cite.group(3))
        fm += [f"year: {yr}"]
        if meta.get("enacted_at"):
            fm.append(f"enacted_at: {meta['enacted_at']}")
        fm += [f'act_kind: "{kind}"', f'act_no: "{no}"', f"act_no_value: {roman(no)}", f"act_year: {yr}", f'act_citation: "{kind} No. {no} of {yr}"',
               "aliases:", f'  - "{kind} No. {no} of {yr}"', f'  - "{kind} {no} of {yr}"', f'  - "{no} of {yr}"', f'  - "{kind} No. {roman(no)} of {yr}"', f'  - "{stem}"']
    else:
        if year:
            fm.append(f"year: {year.group(1)}")
        if meta.get("enacted_at"):
            fm.append(f"enacted_at: {meta['enacted_at']}")
        fm += ["aliases:", f'  - "{stem}"']
    fm += [f"scraped_at: {scraped}", "language: en", "---", ""]
    def linked(block: str) -> str:
        if block.startswith("#"):
            return block
        if cidx is not None:
            block = link_cites(block, cidx, stems or {})
        return link_titles(block, tidx, "") if tidx else block

    body = link_sections("\n\n".join(_balance(linked(b)) if not b.startswith("#") else b for b in body.split("\n\n")), stem)
    if not body.lstrip().startswith("# "):
        body = f"# {title}\n\n" + body
    if defs:
        body += "\n\n## Footnotes\n\n" + "\n".join(defs)
    return "\n".join(fm) + "\n" + body.rstrip() + "\n", title


def run(root: Path, out: Path, acts: Path | None = None, only: set[str] | None = None) -> None:
    con = sqlite3.connect(f"file:{root / 'nasir.db'}?mode=ro", uri=True, timeout=300)
    urls = [r[0] for r in con.execute("SELECT url FROM pages")]
    con.close()
    cidx = tidx = stems = None
    if acts:
        from bdlaw_scraper.citations import build_index
        cidx, tidx, stems = build_index(acts), build_title_index(acts), build_stem_titles(acts)
    done = skipped = 0
    failed: list[tuple[str, str]] = []
    for url in urls:
        if only is not None and url not in only:
            continue
        p = cache_path(root, url)
        if not p.exists():
            skipped += 1
            continue
        scraped = datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).strftime("%Y-%m-%d")
        try:
            text, _ = convert(p.read_bytes(), url, scraped, cidx, tidx, stems)
        except Exception as e:  # one bad page must not stop the run; it is listed at the end
            failed.append((url, repr(e)[:120]))
            continue
        dest = out / (slug(url) + ".md")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        done += 1
    print(f"converted {done}, no cache {skipped}, failed {len(failed)}")
    for u, e in failed[:50]:
        print("FAILED PAGE", u, e)


if __name__ == "__main__":
    args = sys.argv[1:]
    acts = Path(args[args.index("--acts") + 1]).expanduser() if "--acts" in args else None
    only = None
    if "--only" in args:
        only = {r["url"] for r in csv.DictReader(open(args[args.index("--only") + 1]))}
    run(Path(args[0]), Path(args[1]), acts, only)
