"""
Skeleton of Wilson's "Glossary of Judicial and Revenue Terms" (1855) from the PDF's own text layer.

The Internet Archive scan carries an OCR text layer, and `pdftotext -bbox-layout` gives
every line a position. That is enough to rebuild the two-column reading order and to find
where each entry starts, which plain OCR text and layout parsers both lose:

  * a page header sits above the first entry line and is dropped, as is the page number,
  * a line belongs to the left or right column by its x position,
  * an entry's first line starts at the column margin, its continuation lines are indented,
  * lines that share a baseline (a native-script word breaks the line in two) are joined.

Native-script words (Arabic, Devanagari, Bengali ...) are garbled by the OCR layer. They
are recorded as `native` placeholders with their language code, not guessed, so a later
pass can recognise the crops and fill them in.

Usage:
  bdlaw-wilson --pdf glossaryofjudici00wilsuoft.pdf --output-json wilson.json
"""

from __future__ import annotations

import html
import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import click

_RE_LINE = re.compile(r'<line xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">(.*?)</line>', re.S)
_RE_PAGE = re.compile(r'<page width="([\d.]+)" height="([\d.]+)">(.*?)</page>', re.S)
_RE_WORD = re.compile(r">([^<]*)</word>")
_RE_WORD_BOX = re.compile(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">([^<]*)</word>')

# language codes Wilson prints after a headword: "KARZ, H.", "Karjkari, Mar.", "(A. ...)"
LANG_CODES = {
    "A": "Arabic", "P": "Persian", "H": "Hindustani", "S": "Sanskrit", "Hind": "Hindi", "Mar": "Marathi",
    "Beng": "Bengali", "Ben": "Bengali", "Tel": "Telugu", "Tam": "Tamil", "Karn": "Kannada", "Kara": "Kannada",
    "Guz": "Gujarati", "Uriya": "Uriya", "Mal": "Malayalam", "Sindhi": "Sindhi", "Pali": "Pali", "Tur": "Turkish",
    "Eng": "English", "Port": "Portuguese", "Fr": "French", "Cing": "Cingalese", "Mahr": "Marathi",
}
_RE_LANG = re.compile(r"\b(" + "|".join(sorted(LANG_CODES, key=len, reverse=True)) + r")\.")
# a parenthesis that holds native script: the OCR layer turns it into symbols and stray capitals
_RE_NATIVE_JUNK = re.compile(r"[\^~£&*<>{}\[\]|\\«»_;]|[^\x00-ɏ]")


@dataclass
class Entry:
    """One glossary entry as read from the page."""

    page: int
    column: int
    term: str
    languages: list[str]
    text: str  # Latin text of the entry with native-script groups replaced by ⟦native⟧
    # {"raw": garbled OCR, "lang": code before it, "page": n, "boxes": [[x0, y0, x1, y1], ...] in PDF points, one per printed line}
    native: list[dict] = field(default_factory=list)
    main: bool = False  # small-caps main headword (True) or italic sub-entry (False)
    section: str = "glossary"  # glossary (A to Z), supplement (a second A to Z), index (word and page list)


@dataclass
class Line:
    column: int
    y: float
    x: float
    text: str
    words: list = field(default_factory=list)  # (text, x0, y0, x1, y1) of every word on the line


def read_pages(pdf: Path) -> list[tuple[float, float, list[Line]]]:
    """Run pdftotext -bbox-layout and return (width, height, lines) per page."""
    raw = subprocess.run(
        ["pdftotext", "-bbox-layout", str(pdf), "-"], check=True, capture_output=True
    ).stdout.decode("utf-8", "replace")
    pages = []
    for w, h, body in _RE_PAGE.findall(raw):
        width = float(w)
        lines = []
        for x0, y0, _x1, _y1, inner in _RE_LINE.findall(body):
            words = [(html.unescape(t), float(a), float(b), float(c), float(d)) for a, b, c, d, t in _RE_WORD_BOX.findall(inner)]
            text = html.unescape(" ".join(_RE_WORD.findall(inner))).strip()
            if text:
                lines.append(Line(0 if float(x0) < width / 2 else 1, float(y0), float(x0), text, words))
        pages.append((width, float(h), lines))
    return pages


def merge_baselines(lines: list[Line], tol: float = 3.0) -> list[Line]:
    """Join the fragments of one printed line (a native word splits it) and put the page in reading order."""
    out: list[Line] = []
    for col in (0, 1):
        col_lines = sorted((l for l in lines if l.column == col), key=lambda l: (l.y, l.x))
        for l in col_lines:
            if out and out[-1].column == col and abs(out[-1].y - l.y) <= tol:
                out[-1] = Line(col, out[-1].y, out[-1].x, out[-1].text + " " + l.text, out[-1].words + l.words)
            else:
                out.append(Line(col, l.y, l.x, l.text, l.words))
    return out


def entry_page(lines: list[Line]) -> bool:
    """A dictionary page has body lines in both columns. Front matter, plates and blanks do not."""
    body = [l for l in lines if l.y >= 72]
    return sum(l.column == 0 for l in body) >= 8 and sum(l.column == 1 for l in body) >= 8


def split_entries(lines: list[Line], height: float) -> list[list[Line]]:
    """Group body lines into entries by indentation. Continuation lines of an entry are indented."""
    body = [l for l in lines if 72 <= l.y <= height - 60 and not l.text.isdigit()]
    groups: list[list[Line]] = []
    for col in (0, 1):
        cl = [l for l in body if l.column == col]
        if not cl:
            continue
        margin = min(l.x for l in cl)
        for l in cl:
            starts = l.x < margin + 11
            if starts or not groups or groups[-1][-1].column != col and not groups[-1]:
                groups.append([l])
            elif groups[-1][-1].column != col:
                groups.append([l])  # first line of a column that continues the entry from the previous column
                groups[-1][0].text = "→ " + groups[-1][0].text
            else:
                groups[-1].append(l)
    return groups


def join_lines(lines: list[Line]) -> str:
    """Join an entry's lines, undoing end-of-line hyphenation ("com-" + "prising")."""
    text = ""
    for l in lines:
        t = l.text.lstrip("→ ").strip()
        if text.endswith("-") and t[:1].islower() and text[-2:-1].isalpha():
            text = text[:-1] + t
        else:
            text = (text + " " + t).strip()
    return text


def _native_groups(text: str) -> tuple[str, list[dict]]:
    """Replace parenthesised native-script groups with ⟦native⟧ and record the garbled OCR."""
    native: list[dict] = []

    def sub(m: re.Match) -> str:
        inner = m.group(1)
        if not _RE_NATIVE_JUNK.search(inner):
            return m.group(0)
        lead = _RE_LANG.match(inner)  # "(A. <arabic>, plur. of ...)": the code inside names the script
        lang = lead.group(1) if lead else next((c for c in reversed(_RE_LANG.findall(text[: m.start()][-60:]))), "")
        native.append({"raw": inner, "lang": lang})
        return f"({lead.group(0)} ⟦native⟧)" if lead else "(⟦native⟧)"

    return re.sub(r"\(([^()]*(?:\([^()]*\)[^()]*)?)\)", sub, text), native


def locate_native(native: list[dict], group: list[Line], page: int) -> None:
    """Give each native group the page and the box of its words, found by searching its OCR text in the entry's words."""
    flat = [(w, i) for i, l in enumerate(group) for w in l.words]
    joined, starts = "", []
    for w, _ in flat:
        starts.append(len(joined))
        joined += w[0] + " "
    cursor = 0
    for n in native:
        n["page"], n["boxes"] = page, []
        at = joined.find(n["raw"].strip(), cursor)
        if at < 0 or not n["raw"].strip():
            continue
        cursor = at + len(n["raw"])
        hit = [flat[k] for k, st in enumerate(starts) if st < cursor and st + len(flat[k][0][0]) > at]
        for line in dict.fromkeys(i for _, i in hit):
            ws = [w for w, i in hit if i == line]
            n["boxes"].append([round(min(w[1] for w in ws), 1), round(min(w[2] for w in ws), 1), round(max(w[3] for w in ws), 1), round(max(w[4] for w in ws), 1)])


def parse_entry(text: str, page: int, column: int, main: bool) -> Optional[Entry]:
    """Split an entry into term, languages and Latin text."""
    clean, native = _native_groups(text)
    head = re.match(r"^(?P<term>[^()]{1,90}?)(?:,\s*|\s+)(?=(?:\(|[A-Z][a-z]*\.|&c\.|corruptly|also|or\b))", clean)
    term = (head.group("term") if head else clean.split(",")[0]).strip(" ,.;")
    if not term or len(term) > 90:
        return None
    langs = []
    for code in _RE_LANG.findall(clean[:200]):
        name = LANG_CODES[code]
        if name not in langs:
            langs.append(name)
    return Entry(page, column, term, langs, clean, native, main)


_RE_INDEX_ROW = re.compile(r"\s\d{3}\s+\d{1,2}\s+\d{1,2}\b|\s\d{3}\s+\d\b")


def assign_sections(entries: list[Entry], first_page: int = 35) -> list[Entry]:
    """Mark front matter out, then glossary, supplement (the alphabet starts again after Z) and index.

    The glossary proper starts at first_page. The supplement begins at the first main
    headword in A after the Z section; the index begins where most entries are rows of page numbers.
    """
    out, seen_z, section = [], False, "glossary"
    index_from = next(
        (p for p in sorted({e.page for e in entries}) if p > first_page + 400
         and sum(bool(_RE_INDEX_ROW.search(e.text)) for e in entries if e.page == p) >= max(3, sum(e.page == p for e in entries) * 0.4)),
        10**6,
    )
    for e in entries:
        if e.page < first_page:
            continue
        letter = e.term[:1].upper()
        if e.main and letter == "Z":
            seen_z = True
        elif section == "glossary" and seen_z and e.main and letter == "A":
            section = "supplement"
        if e.page >= index_from:
            section = "index"
        e.section = section
        out.append(e)
    return out


def build_entries(pdf: Path) -> list[Entry]:
    """All entries of the book in reading order."""
    entries: list[Entry] = []
    for pno, (_w, h, lines) in enumerate(read_pages(pdf), 1):
        merged = merge_baselines(lines)
        if not entry_page(merged):
            continue
        for group in split_entries(merged, h):
            first = group[0].text.lstrip("→ ")
            e = parse_entry(join_lines(group), pno, group[0].column, main=bool(re.match(r"^[A-ZÁÍÚÉÓ][A-ZÁÍÚÉÓ'’\-]{2,}", first)))
            if e is None:
                continue
            locate_native(e.native, group, pno)
            if group[0].text.startswith("→") and entries:  # continues the previous entry
                entries[-1].text += " " + e.text
                entries[-1].native += e.native
            else:
                entries.append(e)
    return assign_sections(entries)


# HTML language tag and writing direction of a recognised word, from its script and the book's language code
_SCRIPT_LANG = [
    ("\u0600", "\u06ff", {"A": "ar", "P": "fa"}, "ur", "rtl"), ("\u0750", "\u077f", {"A": "ar", "P": "fa"}, "ur", "rtl"),
    ("\ufb50", "\ufeff", {"A": "ar", "P": "fa"}, "ur", "rtl"),
    ("\u0900", "\u097f", {"S": "sa", "Mar": "mr", "Mahr": "mr"}, "hi", "ltr"), ("\u0980", "\u09ff", {}, "bn", "ltr"),
    ("\u0a80", "\u0aff", {}, "gu", "ltr"), ("\u0b00", "\u0b7f", {}, "or", "ltr"), ("\u0b80", "\u0bff", {}, "ta", "ltr"),
    ("\u0c00", "\u0c7f", {}, "te", "ltr"), ("\u0c80", "\u0cff", {}, "kn", "ltr"), ("\u0d00", "\u0d7f", {}, "ml", "ltr"),
]


def native_span(text: str, code: str) -> str:
    """The recognised word as an HTML span with its language and direction, so fonts and readers pick it up."""
    first = next((c for c in text if c.isalpha()), "")
    for lo, hi, by_code, default, direction in _SCRIPT_LANG:
        if lo <= first <= hi:
            return f'<span lang="{by_code.get(code, default)}" dir="{direction}">{html.escape(text)}</span>'
    return html.escape(text)


def apply_native(entries: list[Entry], recognised: dict[tuple[int, int], str]) -> None:
    """Attach recognised words ((entry index, word index) -> text) to the entries' native groups."""
    for i, e in enumerate(entries):
        for j, n in enumerate(e.native):
            if recognised.get((i, j)):
                n["text"] = recognised[(i, j)]


def render_markdown(entries: list[Entry], section: str = "glossary") -> str:
    """A to Z markdown of one section. Recognised native words become language-tagged spans, the rest stay marked as unread."""
    body: dict[str, list[str]] = {}
    for e in entries:
        if e.section != section:
            continue
        text = e.text
        for n in e.native:
            shown = native_span(n["text"], n["lang"]) if n.get("text") else f"[unread {LANG_CODES.get(n['lang'], n['lang'] or '?')}]"
            text = text.replace("⟦native⟧", shown, 1)
        head = re.match(re.escape(e.term), text)
        text = f"**{e.term}**" + text[head.end():] if head else text
        letter = e.term[:1].upper() if e.term[:1].isalpha() else "#"
        body.setdefault(letter, []).append(text)
    out = []
    for letter in sorted(body, key=lambda k: (k == "#", k)):
        out += [f"## {letter}", ""] + [t + "\n" for t in body[letter]]
    return "\n".join(out)


@click.command()
@click.option("--pdf", required=True, type=click.Path(exists=True, dir_okay=False), help="The scanned book with its OCR text layer.")
@click.option("--output-json", required=True, type=click.Path(), help="Where the entries are written.")
@click.option("--output-md", default=None, type=click.Path(), help="Also write the glossary section as markdown.")
@click.option("--native", default=None, type=click.Path(exists=True), help="JSON lines from bdlaw-wilson-native to fill the native-script words.")
def main(pdf: str, output_json: str, output_md: Optional[str], native: Optional[str]) -> None:
    """Rebuild Wilson's glossary entries from the PDF text layer."""
    entries = build_entries(Path(pdf))
    if native:
        rows = (json.loads(l) for l in Path(native).read_text(encoding="utf-8").splitlines())
        apply_native(entries, {tuple(r["key"]): r["text"] for r in rows if r["status"] == "ok"})
    Path(output_json).write_text(json.dumps([asdict(e) for e in entries], ensure_ascii=False, indent=1), encoding="utf-8")
    if output_md:
        Path(output_md).write_text(render_markdown(entries), encoding="utf-8")
    counts = {sec: sum(e.section == sec for e in entries) for sec in ("glossary", "supplement", "index")}
    click.echo(f"{len(entries)} entries {counts}, {sum(e.main for e in entries)} main headwords -> {output_json}")


if __name__ == "__main__":
    main()
