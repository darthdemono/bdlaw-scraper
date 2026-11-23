"""nasirlawsite.com cached Word-HTML pages to structured Markdown with frontmatter.

The pages are Microsoft Word 11 "Save as web page" output (cp1252, mso styles, conditional comments, o:p and st1
tags). Everything Word-specific is dropped; headings, emphasis, links, lists and tables are kept.

usage: python -m bdlaw_scraper.nasir_md CACHE_ROOT OUT_DIR   (CACHE_ROOT holds nasir.db and cache/)
"""
from __future__ import annotations

import codecs
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


def cache_path(root: Path, url: str) -> Path:
    key = hashlib.sha1(url.encode()).hexdigest()
    return root / "cache" / key[:2] / key


MOJIBAKE = re.compile("(?:[\u00c2-\u00f4][\u0080-\u00bf\u0152\u0153\u0160\u0161\u0178\u017d\u017e\u0192\u02c6\u02dc\u2013-\u203a\u20ac\u2122]+)+")


codecs.register_error("c1enc", lambda e: (bytes(ord(c) for c in e.object[e.start:e.end]), e.end))  # the reverse of c1


def _unmojibake(m: re.Match) -> str:
    try:
        return m.group().encode("cp1252", "c1enc").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return m.group()


def decode(raw: bytes) -> str:
    """Some pages are UTF-8 under a windows-1252 label: repair the runs that read as UTF-8 seen through cp1252."""
    return MOJIBAKE.sub(_unmojibake, _decode(raw).replace("\u00ad", "").replace("&shy;", ""))


codecs.register_error("c1", lambda e: (chr(e.object[e.start]), e.start + 1))  # cp1252 leaves 0x81 0x8d 0x8f 0x90 0x9d undefined; keep the byte


def _decode(raw: bytes) -> str:
    m = re.search(rb"charset=([\w-]+)", raw[:3000])
    for enc in ([m.group(1).decode()] if m else []) + ["utf-8", "cp1252"]:
        try:
            return raw.decode(enc, errors="c1") if enc.lower().startswith(("cp1252", "windows-1252")) else raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("cp1252", "c1")


def _ws(text: str) -> str:
    return re.sub(r"[ \t\r\n ]+", " ", text)


def _inline(node, base: str) -> str:
    """Inline Markdown of a node: bold, italic, links, line breaks."""
    out = []
    for ch in node.children:
        if isinstance(ch, Comment):
            continue
        if isinstance(ch, NavigableString):
            out.append(_ws(str(ch)))
            continue
        name = ch.name.lower()
        if name == "br":
            out.append("\n")
        elif name in ("b", "strong"):
            out.append(_wrap("**", _inline(ch, base)))
        elif name in ("i", "em"):
            out.append(_wrap("*", _inline(ch, base)))
        elif name == "sup":
            t = _ws(ch.get_text()).strip()
            out.append(f"<sup>{t}</sup>" if t else "")
        elif name == "a" and ch.get("href") and not ch["href"].startswith(("#", "mailto:")):
            t = _inline(ch, base).strip()
            out.append(f"[{t}]({urljoin(base, ch['href'])})" if t else "")
        elif name in ("img", "script", "style"):
            continue
        else:  # span, font, u, o:p, st1:*, ...
            out.append(_inline(ch, base))
    return "".join(out)


def _wrap(mark: str, text: str) -> str:
    inner = text.strip()
    if not inner:
        return text
    lead = text[: len(text) - len(text.lstrip())]
    tail = text[len(text.rstrip()):]
    return f"{lead}{mark}{inner}{mark}{tail}"


def _clean(text: str) -> str:
    text = re.sub(r"\*\*\s*\*\*|(?<!\*)\*\s\*(?!\*)", " ", text)  # empty emphasis pairs
    text = re.sub(r"\*\*(\s*)\*\*", r"\1", text)
    text = re.sub(r"[ ]*\n[ ]*", "\n", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


def _style(tag: Tag) -> str:
    return str(tag.get("style") or "").replace("\n", " ").lower()


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


def _blocks(parent: Tag):
    for ch in parent.children:
        if isinstance(ch, Tag):
            if ch.name.lower() in ("div", "blockquote", "center", "font", "span", "form") and ch.find(["p", "table", "ul", "ol", "h1", "h2", "h3", "h4", "li"]):
                yield from _blocks(ch)
            else:
                yield ch


def body_to_md(soup: BeautifulSoup, base: str) -> tuple[str, str]:
    """Returns (markdown, first heading text)."""
    for t in soup(["script", "style", "head"]):
        t.decompose()
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    for t in soup.find_all(re.compile(r"^(o|st1|w|v):", re.I)):
        t.unwrap()
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
        txt = re.sub(r"-{2,}", "\u2014", _clean(_inline(blk, base)))
        if not txt or re.fullmatch(r"[\W_]+", txt):
            continue
        if re.match(r"^\W*Updated:", txt):  # the page's "Updated: <date> / <hijri> / <saka>" banner
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
            plain = re.sub(r"[*_]", "", txt).replace("\n", " ").strip()
            if level == 1 and not title:
                title = plain
            out.append("#" * level + " " + plain)
        else:
            out.append(txt.replace("\n", "  \n"))
    return "\n\n".join(out) + "\n", title


SEC = re.compile(r"^\*{0,2}(\d+[A-Z]{0,2})\.\s+(.{2,160}?)\.?\*{0,2}\s*(?:\u2014|\u2013|-)*\s*(.*)$", re.S)
SEC_BOLD = re.compile(r"^\*\*(\d+[A-Z]{0,2})\.\s+(.{2,160}?)\*\*\s*(?:\u2014|\u2013|-)*\s*(.*)$", re.S)
SEC_DASH = re.compile(r"^(\d+[A-Z]{0,2})\.\s+(.{2,160}?)\s*(?:\u2014|\u2013|-){1,}\s*(.*)$", re.S)
CLAUSE = re.compile(r"^\*{0,2}\(([a-z]{1,4}|\d{1,3}[a-z]?|[A-Z]{1,3})\)\*{0,2}\s*(.*)$", re.S)
ROMANS = {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii", "xiii", "xiv", "xv"}
LINE_SPLIT = re.compile(r"  \n(?=\*{0,2}\([a-zA-Z0-9]{1,4}\))")


def _kind(label: str, prev: str | None) -> str:
    """digit, letter, roman or upper: Word flattened the nesting, so it is rebuilt from the label types in order of appearance."""
    if label.isdigit() or re.fullmatch(r"\d+[a-z]", label):
        return "digit"
    if label in ROMANS and not (prev and len(prev) == 1 and label in ("v", "x", "i") and prev in ("u", "w", "h")):
        return "roman"
    return "letter" if label.islower() else "upper"


def _section(block: str):
    for rx in (SEC_BOLD, SEC_DASH):
        m = rx.match(block)
        if m and len(m.group(2)) < 160 and "\n" not in m.group(2):
            return m
    return None


def structure(md: str) -> str:
    """Section headings (##### Section N: Title ^sec-N) and nested clause lists, as in the bdlaws acts."""
    queue = [b for blk in md.split("\n\n") for b in LINE_SPLIT.split(blk)]
    out, sec, n, prev, order, notes = [], None, 0, None, [], []
    while queue:
        block = queue.pop(0)
        m = _section(block) if not block.startswith("#") else None
        if m:
            sec, n, prev, order = m.group(1), 0, None, []
            out.append(f"##### Section {sec}: {m.group(2).strip(' .*')} ^sec-{sec.lower()}")
            if m.group(3).strip():
                queue[0:0] = LINE_SPLIT.split(m.group(3).strip())
            continue
        c = CLAUSE.match(block)
        if c and sec:
            kind = _kind(c.group(1), prev)
            if kind in order:
                del order[order.index(kind) + 1:]  # a shallower label came back: the deeper levels are closed
            else:
                order.append(kind)
            lvl = order.index(kind)
            prev = c.group(1)
            n += 1
            out.append(f"{'    ' * lvl}1. <!--n:({c.group(1)})--> {c.group(2).strip()} ^sec-{sec.lower()}-{n}")
            continue
        if re.match(r"^\*<sup>\w+</sup>", block):  # a page-footer footnote that landed mid-text: moved to the end
            notes.append(re.sub(r"^\*<sup>(\w+)</sup>\**\s*", r"<sup>\1</sup> ", block).rstrip("*"))
            continue
        if out and block[:1].islower() and re.search(r" \^sec-[\w]+-\d+$", out[-1]):  # text that resumes after a page break
            head, _, anchor = out[-1].rpartition(" ^")
            out[-1] = f"{head} {block} ^{anchor}"
            continue
        out.append(block)
    text = "\n\n".join(out + ([ "---", *notes ] if notes else []))
    return re.sub(r"(\^sec-[\w]+-\d+)\n\n(?=\s*1\. <!--n:)", r"\1\n", text)


def classify(url: str) -> str:
    parts = [p for p in urlsplit(url).path.split("/") if p]
    return parts[0] if len(parts) > 1 else "root"


def slug(url: str) -> str:
    p = urlsplit(url).path.strip("/")
    p = re.sub(r"\.html?$", "", p, flags=re.I)
    return re.sub(r"[^\w/\-]+", "-", p)


def convert(raw: bytes, url: str, scraped: str) -> tuple[str, str]:
    soup = BeautifulSoup(decode(raw), PARSER)
    head_title = _ws(soup.title.get_text()).strip() if soup.title else ""
    md, h1 = body_to_md(soup, url)
    md = MOJIBAKE.sub(_unmojibake, re.sub(r"(?<=[\u0080-\u00bf\u20ac\u0152\u0153\u2013-\u203a])\*\*(?=[\u0080-\u00bf])", "", md))  # a tag that split a mojibake run
    md = structure(md)
    title = head_title or h1 or slug(url)
    fm = ["---", f'title: "{title.replace(chr(34), chr(39))}"', f'source_url: {url}', f"source: {SITE}", f"kind: {classify(url)}"]
    m = re.search(rf"(Act|Ordinance|Order|Regulation)\s+No\.?\s*({ROMAN}|\d+)\s+of\s+(\d{{4}})", head_title + " " + md[:600], re.I)
    if m:
        fm += [f'act_kind: "{m.group(1).title()}"', f'act_no: "{m.group(2)}"', f"act_year: {m.group(3)}"]
    fm += [f"scraped_at: {scraped}", "language: en", "---", ""]
    return "\n".join(fm) + "\n" + md, title


def run(root: Path, out: Path) -> None:
    con = sqlite3.connect(f"file:{root / 'nasir.db'}?mode=ro", uri=True, timeout=300)
    urls = [r[0] for r in con.execute("SELECT url FROM pages")]
    con.close()
    done = skipped = 0
    failed: list[tuple[str, str]] = []
    for url in urls:
        p = cache_path(root, url)
        if not p.exists():
            skipped += 1
            continue
        scraped = datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).strftime("%Y-%m-%d")
        try:
            text, _ = convert(p.read_bytes(), url, scraped)
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
    run(Path(sys.argv[1]), Path(sys.argv[2]))
