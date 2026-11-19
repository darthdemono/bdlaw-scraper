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
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import click
from indic_transliteration import sanscript
from rapidfuzz import fuzz

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
        for x0, y0, x1, _y1, inner in _RE_LINE.findall(body):
            words = [(html.unescape(t), float(a), float(b), float(c), float(d)) for a, b, c, d, t in _RE_WORD_BOX.findall(inner)]
            text = html.unescape(" ".join(_RE_WORD.findall(inner))).strip()
            if text:
                # midpoint, not start: a small-caps headword hangs a few points left of the right column's margin
                lines.append(Line(0 if (float(x0) + float(x1)) / 2 < width / 2 else 1, float(y0), float(x0), text, words))
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


def fold_fragments(entries: list[Entry]) -> list[Entry]:
    """Join the pieces of an entry that were cut mid-sentence: a non-headword that starts in lower case continues the entry before it.

    """
    out: list[Entry] = []
    for e in entries:
        if out and not e.main and e.text[:1].islower() and out[-1].section == e.section:
            out[-1].text += " " + e.text
            out[-1].native += e.native
        else:
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


_ARABIC = dict(zip("بپتٹثجچحخدڈذرڑزژسشصضطظغفقکكگلمنںھہ", "bptttjcckdddzrrzzsssztzgfqkkglmnnhh"))
_FOLD = str.maketrans({"w": "v", "z": "j", "q": "k", "c": "k", "f": "p", "x": "k"})


def skeleton(text: str) -> str:
    """Consonant skeleton of a Latin word (diacritics, vowels, aspiration and the usual spelling variants removed)."""
    t = unicodedata.normalize("NFD", text.lower())
    t = "".join(ch for ch in t if not unicodedata.combining(ch)).translate(_FOLD)
    t = re.sub(r"(sh|ch|[kgtdpbj])h", lambda m: m.group(1)[-1] if m.group(1)[-1] in "kgtdpbj" else "s", t)
    t = t.replace("ch", "k")
    return re.sub(r"[^a-z]|[aeiouy]", "", t)


_INDIC = {"devanagari": sanscript.DEVANAGARI, "bengali": sanscript.BENGALI, "gujarati": sanscript.GUJARATI, "oriya": sanscript.ORIYA,
          "tamil": sanscript.TAMIL, "telugu": sanscript.TELUGU, "kannada": sanscript.KANNADA, "malayalam": sanscript.MALAYALAM}


def native_skeleton(text: str) -> str:
    """Consonant skeleton of a native-script word, via IAST for Indic scripts and a letter table for Arabic script."""
    out = []
    for word in text.split():
        first = next((c for c in word if c.isalpha()), "")
        for name, (lo, hi) in _BLOCKS.items():
            if lo <= first <= hi or (name == "arabic" and "\ufb50" <= first <= "\ufeff"):
                out.append("".join(_ARABIC.get(c, "") for c in word) if name == "arabic" else skeleton(sanscript.transliterate(word, _INDIC[name], sanscript.IAST)))
                break
    return " ".join(w for w in out if w)


_BLOCKS = {"arabic": ("\u0600", "\u077f"), "devanagari": ("\u0900", "\u097f"), "bengali": ("\u0980", "\u09ff"), "gujarati": ("\u0a80", "\u0aff"),
           "oriya": ("\u0b00", "\u0b7f"), "tamil": ("\u0b80", "\u0bff"), "telugu": ("\u0c00", "\u0c7f"), "kannada": ("\u0c80", "\u0cff"),
           "malayalam": ("\u0d00", "\u0d7f")}


def match_score(latin_context: str, native: str) -> int:
    """0 to 100: how well the native word's consonants match the closest Latin word printed around it."""
    target = native_skeleton(native).replace(" ", "")
    if not target:
        return 0
    words = [skeleton(w) for w in re.findall(r"[^\W\d_]+", latin_context)]
    best = max((fuzz.ratio(target, w) for w in words if w), default=0)
    joined = "".join(words)
    return int(max(best, fuzz.partial_ratio(target, joined) if len(target) >= 3 else 0))


def native_span(text: str, code: str, check: bool = False) -> str:
    """The recognised word as an HTML span with its language and direction, so fonts and readers pick it up.

    `check` marks a word whose consonants do not match the Latin word printed beside it."""
    mark = ' class="check"' if check else ""
    first = next((c for c in text if c.isalpha()), "")
    for lo, hi, by_code, default, direction in _SCRIPT_LANG:
        if lo <= first <= hi:
            return f'<span lang="{by_code.get(code, default)}" dir="{direction}"{mark}>{html.escape(text)}</span>'
    return html.escape(text)


def apply_native(entries: list[Entry], recognised: dict[str, str]) -> None:
    """Attach recognised words (id "page:x:y" of the word's first box -> text) to the entries' native groups."""
    for e in entries:
        for n in e.native:
            wid = f"{n['page']}:{n['boxes'][0][0]}:{n['boxes'][0][1]}" if n.get("boxes") else None
            if wid and recognised.get(wid):
                n["text"] = recognised[wid]
                n["score"] = match_score(e.text[:160], n["text"])
                n["check"] = n["score"] < (30 if n["lang"] in ("A", "P", "H") else 40)


def _escape(text: str) -> str:
    """Keep OCR noise from turning into markdown, wikilinks or math."""
    text = text.replace("&", "&amp;").replace("<", "&lt;")
    return re.sub(r"([\\*_`\[\]$|~#])", r"\\\1", text)


def entry_markdown(e: Entry, anchor: str = "") -> str:
    """One entry as a markdown paragraph. Recognised native words become language-tagged spans, the rest stay marked as unread."""
    text = _escape(e.text)
    for n in e.native:
        shown = native_span(n["text"], n["lang"], n.get("check", False)) if n.get("text") else f"[unread {LANG_CODES.get(n['lang'], n['lang'] or '?')}]"
        text = text.replace("⟦native⟧", shown, 1)
    term = _escape(e.term)
    text = f"**{term}**" + text[len(term):] if text.startswith(term) else text
    return text + (f" ^{anchor}" if anchor else "")


def render_markdown(entries: list[Entry], section: str = "glossary") -> str:
    """A to Z markdown of one section in a single document."""
    body: dict[str, list[str]] = {}
    for e in entries:
        if e.section == section:
            body.setdefault(letter_of(e), []).append(entry_markdown(e))
    out = []
    for letter in sorted(body, key=lambda k: (k == "#", k)):
        out += [f"## {letter}", ""] + [t + "\n" for t in body[letter]]
    return "\n".join(out)


def letter_of(e: Entry) -> str:
    first = unicodedata.normalize("NFD", e.term.strip(" '\"“‘(")[:1]).upper()[:1]
    return first if "A" <= first <= "Z" else "Other"


def _names(e: Entry) -> list[str]:
    """The spellings a main headword goes by: "BUDH-GANGA, BooDH-GuNGA" -> both, plain letters, lower case."""
    out = []
    for part in re.split(r",\s*|\s+or\s+", e.term):
        part = "".join(c for c in unicodedata.normalize("NFD", part.lower()) if not unicodedata.combining(c))
        part = re.sub(r"[^a-z\- ]", "", part).strip()
        if part:
            out.append(part)
    return out


_RE_SEE = re.compile(r"\b([Ss]ee) ((?:[A-Z][\w'’\-]*)(?:(?:,| and| or|, and) [A-Z][\w'’\-]*)*)")


def link_see(text: str, targets: dict[str, tuple[str, str]], folder: str) -> str:
    """"See Adasatta and Arsatha" -> wikilinks to the headwords that exist exactly once."""
    def one(m: re.Match) -> str:
        name = m.group(0)
        key = "".join(c for c in unicodedata.normalize("NFD", name.lower()) if not unicodedata.combining(c))
        key = re.sub(r"[^a-z\- ]", "", key).strip()
        hit = targets.get(key)
        return f"[[{folder}/{hit[0]}#^{hit[1]}|{name}]]" if hit else name

    return _RE_SEE.sub(lambda m: m.group(1) + " " + re.sub(r"[A-Z][\w'’\-]*", one, m.group(2)), text)


def render_notes(entries: list[Entry], folder: str = "Wilson Glossary") -> dict[str, str]:
    """One note per letter (glossary, then supplement) plus an index note. File name -> markdown."""
    kept = [e for e in entries if e.section in ("glossary", "supplement")]
    seen: dict[str, set] = {}
    ids: dict[int, str] = {}
    for n, e in enumerate(kept):
        ids[id(e)] = f"w{n}"
        if e.main and e.section == "glossary":
            for name in _names(e):
                seen.setdefault(name, set()).add((letter_of(e), ids[id(e)]))
    targets = {k: next(iter(v)) for k, v in seen.items() if len(v) == 1}
    by_letter: dict[str, dict[str, list[str]]] = {}
    for e in kept:
        md = link_see(entry_markdown(e, ids[id(e)]), targets, folder)
        by_letter.setdefault(letter_of(e), {}).setdefault(e.section, []).append(md)
    letters = sorted(by_letter, key=lambda k: (k == "Other", k))
    notes: dict[str, str] = {}
    for letter in letters:
        parts = by_letter[letter]
        body = [f"## Glossary, {letter}", ""] + [t + "\n" for t in parts.get("glossary", [])]
        if parts.get("supplement"):
            body += [f"## Supplement, {letter}", "", "The supplement at the end of the book, a second A to Z.", ""] + [t + "\n" for t in parts["supplement"]]
        n_entries = sum(len(v) for v in parts.values())
        notes[f"{letter}.md"] = (
            f"---\ntitle: Wilson Glossary, {letter}\ndescription: Entries {letter} of H. H. Wilson's Glossary of Judicial and Revenue Terms (1855), read from a scan by a program.\n"
            f"generated: true\ntags:\n  - Wilson\n---\n\n> [!Info] Generated from a scan\n"
            f"> Text and native-script words were read from a scan of the 1855 book by a program, not typed by hand. Words marked in a different colour did not match their Latin spelling and may be misread.\n\n"
            + "\n".join(body)
        )
    counts = {sec: sum(e.section == sec for e in kept) for sec in ("glossary", "supplement")}
    links = " · ".join(f"[[{folder}/{l}|{l}]]" for l in letters)
    notes["index.md"] = (
        f"---\ntitle: {folder}\ndescription: H. H. Wilson's Glossary of Judicial and Revenue Terms of British India (1855), rebuilt from a scan with its South Asian words.\n"
        f"generated: true\ntags:\n  - Wilson\n---\n\n"
        f"H. H. Wilson, *A Glossary of Judicial and Revenue Terms, and of Useful Words Occurring in Official Documents Relating to the Administration of the Government of British India* (London, 1855). Public domain. Scan: Internet Archive, `glossaryofjudici00wilsuoft`.\n\n"
        f"> [!Info] How this was made\n> A program rebuilt {counts['glossary']:,} entries and {counts['supplement']:,} supplement entries from the scan's text layer, and read the Arabic, Urdu, Devanagari, Bengali, Telugu, Kannada and other scripts from page images. "
        f"Nobody retyped or corrected it, so expect mistakes: lost accents, wrong letters in native scripts (Telugu and Kannada most), and some entries run together. "
        f"Native words that did not match the Latin spelling beside them are marked with a dashed underline. Words that could not be read at all show as `[unread …]`. Check the printed book for anything that matters. The book's own index of words and page numbers is not included.\n\n"
        f"## Letters\n\n{links}\n"
    )
    return notes


@click.command()
@click.option("--pdf", required=True, type=click.Path(exists=True, dir_okay=False), help="The scanned book with its OCR text layer.")
@click.option("--output-json", required=True, type=click.Path(), help="Where the entries are written.")
@click.option("--output-md", default=None, type=click.Path(), help="Also write the glossary section as markdown.")
@click.option("--output-dir", default=None, type=click.Path(), help="Write one note per letter plus an index here (an Obsidian folder).")
@click.option("--native", default=None, type=click.Path(exists=True), help="JSON lines from bdlaw-wilson-native to fill the native-script words.")
def main(pdf: str, output_json: str, output_md: Optional[str], output_dir: Optional[str], native: Optional[str]) -> None:
    """Rebuild Wilson's glossary entries from the PDF text layer."""
    entries = build_entries(Path(pdf))
    if native:
        rows = (json.loads(l) for l in Path(native).read_text(encoding="utf-8").splitlines())
        apply_native(entries, {r["id"]: r["text"] for r in rows if r["status"] == "ok"})
    entries = fold_fragments(entries)
    Path(output_json).write_text(json.dumps([asdict(e) for e in entries], ensure_ascii=False, indent=1), encoding="utf-8")
    if output_md:
        Path(output_md).write_text(render_markdown(entries), encoding="utf-8")
    if output_dir:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        for name, text in render_notes(entries).items():
            (Path(output_dir) / name).write_text(text, encoding="utf-8")
    counts = {sec: sum(e.section == sec for e in entries) for sec in ("glossary", "supplement", "index")}
    click.echo(f"{len(entries)} entries {counts}, {sum(e.main for e in entries)} main headwords -> {output_json}")


if __name__ == "__main__":
    main()
