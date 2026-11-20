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
    "Eng": "English", "Hindi": "Hindi", "Thug": "Thug argot", "Tibet": "Tibetan", "Tarn": "Tamil", "Mai": "Malayalam", "II": "Hindustani",
    "Kar": "Kannada", "Mah": "Marathi", "Panj": "Panjabi", "Pers": "Persian", "Arab": "Arabic", "Sans": "Sanskrit", "Telu": "Telugu", "Port": "Portuguese", "Fr": "French", "Cing": "Cingalese", "Mahr": "Marathi",
}
# "H.", "Mar." but also "Hindi (" and "Uriya (": the longer names are printed in full, without a period
_RE_LANG = re.compile(r"\b(" + "|".join(sorted(LANG_CODES, key=len, reverse=True)) + r")(?:\.|(?=,?\s*\())")
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


def start_boundary(xs: list[float]) -> float:
    """The x below which a line starts an entry, for one column of one page.

    Lines start at the margin (small caps hang a little further left) or are continuation lines indented about
    6 pt more. Two-means over the line starts finds the gap between the two groups, whatever this page's margin is.
    """
    xs = [x for x in xs if x < min(xs) + 25]
    lo, hi = min(xs), max(xs)
    if hi - lo < 4:
        return lo + 11  # a single group: all starts, as before
    for _ in range(20):
        mid = (lo + hi) / 2
        a, b = [x for x in xs if x < mid], [x for x in xs if x >= mid]
        lo, hi = (sum(a) / len(a) if a else lo), (sum(b) / len(b) if b else hi)
    return (lo + hi) / 2


def split_entries(lines: list[Line], height: float) -> list[list[Line]]:
    """Group body lines into entries by indentation. Continuation lines of an entry are indented."""
    body = [l for l in lines if 72 <= l.y <= height - 60 and not l.text.isdigit()]
    groups: list[list[Line]] = []
    for col in (0, 1):
        cl = [l for l in body if l.column == col]
        if not cl:
            continue
        boundary = start_boundary([l.x for l in cl])
        for l in cl:
            starts = l.x < boundary
            if starts or not groups or groups[-1][-1].column != col and not groups[-1]:
                groups.append([l])
            elif groups[-1][-1].column != col:
                groups.append([l])  # first line of a column that continues the entry from the previous column
                groups[-1][0].text = "→ " + groups[-1][0].text
            else:
                groups[-1].append(l)
    return groups


# Word-level reading of an entry: junk words (the OCR layer's reading of native script) become one ⟦native⟧ marker each.
_RE_TOKEN = re.compile(r"^([(\[“‘\"]*)(.*?)([)\],.;:!?”’\"]*)$", re.S)
_JUNK_CHARS = re.compile(r"[\^~£&*<>{}|\\«»_$@=+#%]|[^\x00-\u024f\u1e00-\u1eff\u2000-\u206f]")
_CASE_SWITCH = re.compile(r"[a-z][A-Z]")
_LETTER_DIGIT = re.compile(r"[A-Za-z]\d|\d[A-Za-z]")
_COMMON = {"a", "an", "and", "or", "the", "of", "in", "to", "is", "as", "by", "for", "with", "from"}


_WORDS_FILE = Path("/usr/share/dict/words")
# the OCR layer reads the accented á of Wilson's romanisation as a d ("Bhdt" for Bhát), so d counts as a vowel here
_VOWELS = "aeiouyáíúéóāīūàèìòùâêîôûd"


def fold(word: str) -> str:
    """Lower case, accents removed: the key for every vocabulary lookup."""
    return "".join(c for c in unicodedata.normalize("NFD", word.lower()) if not unicodedata.combining(c))


def english_words(path: Path = _WORDS_FILE) -> set[str]:
    """The system's English word list (empty when the file is missing)."""
    return {fold(w.strip()) for w in path.read_text(encoding="utf-8", errors="ignore").splitlines()} if path.exists() else set()


def plausible(word: str, vocab: set[str]) -> bool:
    """Is this letters-only word a real English word, a romanised South Asian word the book uses, or at least shaped like one?

    The vocabulary decides first: English words and words the book repeats are accepted as they are.
    Anything else must look like romanisation: a vowel, no letter three times in a row, no run of five consonants,
    and not absurdly long. That rejects the OCR's `fffai`, `Stfsf` and `iTcjiSirr`.
    """
    parts = [p for p in re.split(r"[-'’]", word) if p]
    if not parts or re.fullmatch(r"[ivxlcdm]+", word.lower()):  # roman numerals
        return True
    for part in parts:
        f = fold(part)
        if f in vocab or len(f) <= 2:
            continue
        if not any(c in _VOWELS for c in part.lower()):
            return False
        if re.search(r"([^d])\1\1", f) or re.search(rf"[^{_VOWELS}]{{5,}}", part.lower()) or len(f) > 18:
            return False
    return True


def is_junk(core: str, vocab: Optional[set[str]] = None) -> bool:
    """A word that cannot be English or Wilson's romanisation: symbols, another script, a capital inside a word, digits inside a word.

    With a `vocab` (see `plausible`) a letters-only word that is not a plausible word is junk too.
    """
    if not core or core in ("&c", "&"):  # "&c." is et cetera
        return False
    if _JUNK_CHARS.search(core) or _CASE_SWITCH.search(core):
        return True
    if _LETTER_DIGIT.search(core):
        return not re.fullmatch(r"\d+(st|nd|rd|th|d|s)?|[A-Za-z]\d+|\d+[A-Za-z]", core)
    if vocab is not None and re.fullmatch(r"[^\W\d_]+(?:[-'’][^\W\d_]+)*", core):
        return not plausible(core, vocab)
    return False


def build_lexicon(pages: list[tuple[float, float, list["Line"]]]) -> set[str]:
    """The vocabulary: the English word list plus every word the book itself repeats (its romanised terms) at least four times."""
    from collections import Counter

    count = Counter(
        fold(m.group(2)) for _w, _h, lines in pages for l in lines for t in l.text.split()
        if (m := _RE_TOKEN.match(t)) and m.group(2).isalpha() and not is_junk(m.group(2)) and plausible(m.group(2), set())
    )
    return english_words() | {w for w, c in count.items() if c >= 4}


def group_text(group: list["Line"], page: int, lex: set[str]) -> tuple[str, list[dict]]:
    """The text of one entry (its lines in order) with every run of junk words replaced by ⟦native⟧.

    Returns (text, native) where each native is {"raw", "lang", "page", "boxes"} (boxes in PDF points, one per printed line).
    """
    toks = []  # [lead, core, trail, line index, box, junk]
    for li, l in enumerate(group):
        for text, x0, y0, x1, y1 in l.words:
            m = _RE_TOKEN.match(text.strip())
            lead, core, trail = m.groups() if m else ("", text, "")
            if "°" in core and re.search(r"[A-Za-z]°|°[A-Za-z]", core):
                core = core.replace("°", "o")  # the layer reads an o as a degree sign: "fr°m", "°f"
            toks.append([lead, core, trail, li, [x0, y0, x1, y1], is_junk(core, lex)])
    # a parenthesis right after a language code holds the native word; it is junk unless it has real English in it
    for i, t in enumerate(toks):
        if t[0].startswith("(") and i and toks[i - 1][1].rstrip(".") in LANG_CODES and not t[5]:
            end = next((k for k in range(i, min(i + 6, len(toks))) if toks[k][2].startswith(")")), None)
            span = toks[i : (end + 1 if end is not None else i + 1)]
            if not any(fold(w[1]) in lex and len(w[1]) >= 3 for w in span):
                for w in span:
                    w[5] = True
    # one short ordinary word between two junk words belongs to the same native run
    for i in range(1, len(toks) - 1):
        if not toks[i][5] and toks[i - 1][5] and toks[i + 1][5] and len(toks[i][1]) <= 3 and toks[i][1].lower() not in _COMMON and fold(toks[i][1]) not in lex:
            toks[i][5] = True
    out, native, i, prev_line = "", [], 0, 0
    while i < len(toks):
        lead, core, trail, li, box, junk = toks[i]
        j = i
        if junk:
            while j + 1 < len(toks) and toks[j + 1][5]:
                j += 1
            run = toks[i : j + 1]
            lang = next((c.rstrip(".") for c in (t[1] for t in reversed(toks[max(0, i - 8) : i])) if c.rstrip(".") in LANG_CODES), "")
            boxes: list[list[float]] = []
            for line in dict.fromkeys(t[3] for t in run):
                bs = [t[4] for t in run if t[3] == line]
                boxes.append([round(min(b[0] for b in bs), 1), round(min(b[1] for b in bs), 1), round(max(b[2] for b in bs), 1), round(max(b[3] for b in bs), 1)])
            native.append({"raw": " ".join(t[1] for t in run), "lang": lang, "page": page, "boxes": boxes})
            piece = lead + "⟦native⟧" + toks[j][2]
        else:
            piece = lead + core + trail
        if out.endswith("-") and li != prev_line and piece[:1].islower() and len(out) > 1 and out[-2].isalpha():
            out = out[:-1] + piece  # undo end-of-line hyphenation
        else:
            out = (out + " " + piece).strip()
        prev_line = toks[j][3]
        i = j + 1
    return re.sub(r"\)\)(?!\))", ")", out) if out.count(")") > out.count("(") else out, native


_LANG_ALT = "|".join(sorted(LANG_CODES, key=len, reverse=True))
# a new headword and its language code after a finished sentence: two entries that ran into one line
_CAP = "[A-ZÀ-ÖØ-Þ]"
_SMALL = rf"{_CAP}[A-ZÀ-ÖØ-Þ'’\-]{{2,}}(?:,| or)[^.()]{{0,90}}?,? (?:{_LANG_ALT})\.\s|{_CAP}[A-ZÀ-ÖØ-Þ'’\-]{{2,}}, (?:corruptly|vernacularly|erroneously),? {_CAP}"  # a small-caps headword with variants and its code
_RE_RUNON = re.compile(
    # a headword and its language code after a finished sentence: "... slave. GUMÁSHTA, vernacularly GUMASHTA, H. (...",
    # "... a tax. Varigára, Karn. (...". Capitalised headwords need the code within a few words; small caps may carry variants,
    # and may follow a sentence whose full stop the scan lost ("... of the Mohammadans DAHR, DUHR, H. (...").
    rf"(?:(?<=[.;:?)])\s+(?=(?:{_CAP}[\w'’\-]{{2,}}(?:(?:,| or) {_CAP}[\w'’\-]+)*,? (?:{_LANG_ALT})\.\s|{_SMALL}))"
    rf"|(?<=[a-zà-ÿ])(?<!corruptly)(?<!vernacularly)(?<!erroneously)(?<!also)(?<!or)(?<!and)(?<!written)\s+(?={_SMALL}))"
)


def split_runons(text: str, native: list[dict]) -> list[tuple[str, list[dict]]]:
    """Split one text into entries where a headword with its language code starts after a finished sentence."""
    parts = _RE_RUNON.split(text)
    out, k = [], 0
    for part in parts:
        n = part.count("⟦native⟧")
        got = native[k : k + n]
        k += n
        if out and not _has_definition(out[-1][0]):  # variants of one headword each carry a code and a native word: one entry
            out[-1] = (out[-1][0] + " " + part, out[-1][1] + got)
        else:
            out.append((part, got))
    return out


_FILLER = {"and", "or", "also", "corruptly", "erroneously", "for", "the", "of", "in", "with", "see", "vulgarly", "properly"}


def _has_definition(part: str) -> bool:
    """False for a chunk that ends right after the native word of its language code: it is a headword with no meaning yet."""
    if not part.rstrip(" .,;").endswith(")") and not part.rstrip().endswith("⟦native⟧"):
        return True
    outside = re.sub(r"\([^()]*(?:\([^()]*\)[^()]*)*\)", " ", part)
    words = [w for w in re.findall(r"\b[a-z]{2,}\b", outside) if w not in _FILLER]
    return len(words) >= 2


def parse_entry(text: str, page: int, column: int, main: bool, native: Optional[list[dict]] = None) -> Optional[Entry]:
    """Split an entry into term, languages and Latin text. `native` are the groups behind the ⟦native⟧ markers."""
    clean, native = text.lstrip(" .,;:"), native or []
    head = re.match(r"^(?P<term>[^()]{1,90}?)(?:,\s*|\s+)(?=(?:\(|[A-Z][a-z]*\.|[A-Z][a-z]+,?\s*\(|&c\.|corruptly|also|or\b))", clean)
    term = (head.group("term") if head else clean.split(",")[0]).strip(" ,.;")
    if not term or len(term) > 90:
        return None
    langs = []
    for code in _RE_LANG.findall(clean[:200]):
        name = LANG_CODES[code]
        if name not in langs:
            langs.append(name)
    if not langs and re.match(r"^[^()]{1,90}?[,\s]\(\?\)", clean):
        langs = ["unknown"]  # Wilson prints (?) where he does not know the language
    return Entry(page, column, term, langs, clean, native, main)




def assign_sections(entries: list[Entry], first_page: int = 35, index_page: int = 653) -> list[Entry]:
    """Mark front matter out, then glossary, supplement (the alphabet starts again after Z) and index.

    The glossary proper starts at first_page. The supplement begins at the first main
    headword in A after the Z section; the index begins where most entries are rows of page numbers.
    """
    out, seen_z, section = [], False, "glossary"
    index_from = index_page  # the book's index of words and page numbers starts on page 653
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
        if re.match(r"^\d+ \d+ \d+ ", e.text):
            continue  # a row of the index (word, page, column, line) that strayed into the glossary
        if out and not e.main and (re.match(r"^\d+(?:st|nd|rd|th)?\b", e.term) or e.text[:1].islower() or (not e.languages and len(e.term.split()) > 4)) and out[-1].section == e.section:
            out[-1].text += " " + e.text
            out[-1].native += e.native
        else:
            out.append(e)
    return out


def _bare(e: Entry) -> bool:
    outside = re.sub(r"\([^()]*(?:\([^()]*\)[^()]*)*\)", " ", e.text)
    return bool(e.languages or "⟦native⟧" in e.text) and not [w for w in re.findall(r"\b[a-z]{2,}\b", outside) if w not in _FILLER]


def merge_bare(entries: list[Entry]) -> list[Entry]:
    """A headword with a language code and no meaning shares the meaning of the entry after it ("Nápita, H." / "Nai, Mar. ...")."""
    out: list[Entry] = []
    for i, e in enumerate(entries):
        if i + 1 < len(entries) and e.section == entries[i + 1].section and e.page == entries[i + 1].page and _bare(e) and len(e.text) < 120:
            nxt = entries[i + 1]
            nxt.text = e.text.rstrip(" .") + ", " + nxt.text
            nxt.native = e.native + nxt.native
            nxt.main = nxt.main or e.main
            nxt.languages = list(dict.fromkeys(e.languages + nxt.languages))
            nxt.term = e.term
        else:
            out.append(e)
    return out


_RE_TAGS = re.compile(r"</?[a-zA-Z][^>]*>")


def is_small_caps(first: str) -> bool:
    """A main headword: its first word is all capitals, three letters or more ("AJNA", "ÁJNÁ", "BUDH-GANGA,")."""
    word = re.split(r"[,\s(]", first.lstrip("→ ").strip(), maxsplit=1)[0]
    letters = re.sub(r"[^\w]|\d|_", "", word)
    return len(letters) >= 3 and letters.isupper()


# letters of other alphabets that the image reader mixes into Latin words
_HOMOGLYPHS = str.maketrans({
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T", "У": "Y", "Х": "X", "І": "I", "Ј": "J", "Ѕ": "S",
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s", "ԁ": "d", "һ": "h", "ӏ": "l", "в": "b", "к": "k", "м": "m", "н": "h", "т": "t",
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X",
    "ο": "o", "ν": "v", "ι": "i", "κ": "k", "ρ": "p", "α": "a",
})
# scripts that never occur in the book: whatever the reader put there is a misreading of a native word
_FOREIGN = re.compile("[\u0400-\u04ff\u0370-\u03ff\u0590-\u05ff\u0a00-\u0a7f\u0d80-\u0dff\u0e00-\u0e7f\u1100-\u11ff\u3000-\u30ff\u3400-\u9fff\uac00-\ud7af]+")
_REPEATS = re.compile(r"(\S)\1{5,}")
_CAPS_PAREN = re.compile(r"\((?:\s*[A-Z]{2,}\s*)+\)|\(\s*[A-Z]{1,3}(?:\s*[A-Z]{1,3}){0,3}\s*[,)]")  # "(EXMICH)", "( TITTE )": a native word read as capital Latin
_LOOP = re.compile(r"(\S+(?:\s+\S+){0,5}?)(?:\s+\1){2,}")  # a phrase said three times running: the reader looped on a native word
_SINGLES = re.compile(r"(?<!\S)(?:\S\s+){4,}(?!\S)|(?<!\S)(?:\S\s+){3}\S(?!\S)")  # four or more one-character tokens in a row: "( E C C C"


def _noise(g: str) -> bool:
    """Parenthesised capitals and digits with no lowercase, no full stop and not a year: a native word read as Latin symbols."""
    g = g.strip()
    return g == g.upper() and "." not in g and len(g) >= 2 and not re.fullmatch(r"\d{1,4}|[?\d/ –-]{1,9}", g) and any(c.isupper() or c.isdigit() for c in g)


def clean_reading(text: str) -> Optional[str]:
    """Tidy one image reading: no tags, Latin look-alikes put right, foreign-script guesses marked unread.

    Returns None when the reading is a hallucination (a character repeated six times or more) or holds a script the book
    does not use (the reader guessed at a Telugu or Kannada word): the text layer is used instead for that line.
    """
    text = _RE_TAGS.sub("", text).translate(_HOMOGLYPHS)
    text = re.sub(r"\s+", " ", text)  # a reading may hold line breaks: they would end a table row
    text = re.sub(r"\\frac\{(\d+)\}\{(\d+)\}", r" \1/\2", text)  # fractions and accents come back as LaTeX
    text = re.sub(r"\\(acute|grave|hat|tilde|bar|breve|dot)\{(\w)\}", lambda m: m.group(2) + {"acute": "\u0301", "grave": "\u0300", "hat": "\u0302", "tilde": "\u0303", "bar": "\u0304", "breve": "\u0306", "dot": "\u0307"}[m.group(1)], text)
    for _ in range(3):
        text = re.sub(r"\\[A-Za-z]+\{([^{}]*)\}", r"\1", text)
    text = unicodedata.normalize("NFC", text.replace("\\", ""))
    text = re.sub(r"(.{15,}?)\s+\1(?=\s|$)", r"\1", text, flags=re.I)  # the reader sometimes says a phrase twice
    if _CAPS_PAREN.search(text) or any(_noise(g) for g in re.findall(r"\(([^()]{2,30})\)", text)) or _REPEATS.search(text) or _LOOP.search(text) or _SINGLES.search(text):
        return None
    return None if _FOREIGN.search(text) else text.strip()


def repair_reading(reading: str, layer: str, lex: set[str]) -> str:
    """Fix what image reading gets wrong on a line whose text layer is plain English.

    Short lines lose their first or last word ("medan law." read as "law."), and a few come back as nonsense.
    A first or last word of the layer that is real English and missing from the reading is put back;
    a plain-English layer line the reading barely resembles replaces the reading.
    """
    from difflib import SequenceMatcher

    toks = layer.split()
    cores = [_RE_TOKEN.match(t).group(2) if _RE_TOKEN.match(t) else t for t in toks]
    if not toks or any(is_junk(c, lex) for c in cores):
        return reading  # the layer is itself damaged here (native script): trust the reading
    seen = {fold(x) for x in re.findall(r"[^\W\d_]+", reading)}
    if SequenceMatcher(None, fold(layer), fold(reading)).ratio() < 0.55:
        return layer
    rw = reading.split()
    close = lambda a, bs: any(SequenceMatcher(None, fold(a), fold(b)).ratio() >= 0.7 for b in bs)
    if len(rw) < len(toks) and len(cores[0]) >= 3 and cores[0].isalpha() and not close(cores[0], rw[:2]):
        reading = toks[0] + " " + reading  # the reader cut the first word off the line
    if len(rw) < len(toks) and len(toks) > 1 and len(cores[-1]) >= 3 and cores[-1].isalpha() and not close(cores[-1], rw[-2:]):
        reading = reading + " " + toks[-1]
    return reading


def reading_text(group: list["Line"], page: int, lex: set[str], readings: dict[str, str]) -> tuple[str, list[dict]]:
    """The entry's text from the image readings of its lines; a line with no reading falls back to the text layer."""
    from bdlaw_scraper.wilson_lines import line_key

    out, native = "", []
    for l in group:
        got = readings.get(line_key(page, l.column, l.y))
        piece = None
        if got:
            cleaned = clean_reading(got)
            piece = repair_reading(cleaned, l.text, lex) if cleaned else None
        if piece is None:  # no reading, or an unusable one: the text layer, with its junk words marked
            piece, nat = group_text([l], page, lex)
            native += nat
        if out.endswith("-") and piece[:1].islower() and len(out) > 1 and out[-2].isalpha():
            out = out[:-1] + piece  # undo end-of-line hyphenation
        else:
            out = (out + " " + piece).strip()
    # a line end read twice ("his son-in-law. son-in-law."): drop the immediate repeat of a chunk of five letters or more
    return re.sub(r"(\S{5,}(?:\s\S+){0,2}?)\s+\1(?=\s|$)", r"\1", out, flags=re.I), native


def build_entries(pdf: Path, readings: Optional[dict[str, str]] = None) -> list[Entry]:
    """All entries of the book in reading order.

    `readings` maps a line key (see wilson_lines.line_key) to the line as read from the page image. Where a line has
    one it is used, with its accents and native-script words in place; otherwise the text layer is used.
    """
    pages = read_pages(pdf)
    lex = build_lexicon(pages)
    entries: list[Entry] = []
    for pno, (_w, h, lines) in enumerate(pages, 1):
        merged = merge_baselines(lines)
        if not entry_page(merged):
            continue
        for group in split_entries(merged, h):
            text, native = reading_text(group, pno, lex, readings or {})
            continues = group[0].text.startswith("→")
            if re.fullmatch(r"(GLOSSARY\.?\s*)?[A-Z]{1,4}\.?(\s+[A-Z]{1,4}\.?)?", text.strip()):
                continue  # a running head ("GLOSSARY. AB") that sits inside the body area
            for n, (part, nat) in enumerate(split_runons(text, native)):
                # small caps come back from the image reading in mixed case, so the first line's text layer decides for the first part
                e = parse_entry(part, pno, group[0].column, main=is_small_caps(group[0].text if n == 0 else part), native=nat)
                if e is None:
                    continue
                if continues and n == 0 and entries:  # continues the previous entry
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


_SCRIPT_RUN = re.compile(
    "[" + "".join(lo + "-" + hi for lo, hi in [("\u0600", "\u06ff"), ("\u0750", "\u077f"), ("\ufb50", "\ufdff"), ("\ufe70", "\ufeff"), ("\u0900", "\u097f"), ("\u0980", "\u09ff"),
                                                    ("\u0a80", "\u0aff"), ("\u0b00", "\u0b7f"), ("\u0b80", "\u0bff"), ("\u0c00", "\u0c7f"), ("\u0c80", "\u0cff"), ("\u0d00", "\u0d7f")]) + "]"
    "+(?:[ \u060c,]+[\u0600-\u06ff\u0750-\u077f\ufb50-\ufdff\ufe70-\ufeff\u0900-\u097f\u0980-\u09ff\u0a80-\u0aff\u0b00-\u0b7f\u0b80-\u0bff\u0c00-\u0c7f\u0c80-\u0cff\u0d00-\u0d7f]+)*"
)


def wrap_scripts(text: str, headword: str = "") -> str:
    """Wrap every run of native script in the text (already HTML-escaped) in a language-tagged span.

    The language comes from the script and, where the script is shared, from the language code printed before it.
    A word whose consonants do not match the Latin spelling around it gets class="check".
    """
    def one(m: re.Match) -> str:
        codes = _RE_LANG.findall(text[max(0, m.start() - 70): m.start()])
        code = codes[-1] if codes else ""
        score = match_score(re.sub(r"<[^>]*>", "", headword + " " + text[max(0, m.start() - 160): m.start() + 160]), m.group(0))
        return native_span(m.group(0), code, score < (30 if code in ("A", "P", "H", "Hind") else 40))

    return _SCRIPT_RUN.sub(one, text)


def native_span(text: str, code: str, check: bool = False) -> str:
    """The recognised word as an HTML span with its language and direction, so fonts and readers pick it up.

    `check` marks a word whose consonants do not match the Latin word printed beside it."""
    mark = ' class="check"' if check else ""
    first = next((c for c in text if c.isalpha()), "")
    for lo, hi, by_code, default, direction in _SCRIPT_LANG:
        if lo <= first <= hi:
            return f'<span lang="{by_code.get(code, default)}" dir="{direction}"{mark}>{html.escape(text)}</span>'
    return html.escape(text)


def _mixed_script(text: str) -> bool:
    """One word holding letters of two scripts (Bengali with a Devanagari ra): the reader mixed up look-alike shapes."""
    blocks = {(ord(c) - 0x0600) >> 7 for c in text if ord(c) >= 0x0600 and c.isalpha()}  # 128-code blocks: one per Indic script
    return len(blocks) > 1 and not (blocks <= {0, 1, 2, 3})  # the Arabic blocks 0600-07FF and 0750 may legitimately mix


def apply_native(entries: list[Entry], recognised: dict[str, str]) -> None:
    """Attach recognised words (id "page:x:y" of the word's first box -> text) to the entries' native groups."""
    for e in entries:
        for n in e.native:
            wid = f"{n['page']}:{n['boxes'][0][0]}:{n['boxes'][0][1]}" if n.get("boxes") else None
            if wid and recognised.get(wid):
                n["text"] = recognised[wid]
                n["score"] = match_score(e.text[:160], n["text"])
                n["check"] = n["score"] < (30 if n["lang"] in ("A", "P", "H") else 40) or _mixed_script(n["text"])


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


def assign_letters(entries: list[Entry]) -> list[str]:
    """The A to Z section of each entry: the letter of the latest main headword, so sub-entries and OCR-garbled headwords stay in place.

    A headword whose letter jumps backwards or more than three ahead is an OCR error and is ignored. The supplement starts again at A.
    """
    out, current, section = [], "A", ""
    for e in entries:
        if e.section != section:
            section, current = e.section, "A"
        if e.main:
            letter = letter_of(e)
            if letter != "Other" and current <= letter <= chr(ord(current) + 3):
                current = letter
        out.append(current)
    return out


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


def link_see(text: str, targets: dict[str, tuple[str, str]], folder: str, pipe: str = "|") -> str:
    """"See Adasatta and Arsatha" -> wikilinks to the headwords that exist exactly once."""
    def one(m: re.Match) -> str:
        name = m.group(0)
        key = "".join(c for c in unicodedata.normalize("NFD", name.lower()) if not unicodedata.combining(c))
        key = re.sub(r"[^a-z\- ]", "", key).strip()
        hit = targets.get(key)
        return (f"[[{folder}{hit[0]}{pipe}{name}]]" if hit[0].startswith("#") else f"[[{folder}/{hit[0]}#^{hit[1]}{pipe}{name}]]") if hit else name

    return _RE_SEE.sub(lambda m: m.group(1) + " " + re.sub(r"[A-Z][\w'’\-]*", one, m.group(2)), text)


_RE_CODES = re.compile(rf"^(?:(?:(?:{_LANG_ALT})(?:\.|(?=,?\s*\())|\(\?\))(?:\s*&amp;c\.)?[,\s]*)+")


def _split_entry(e: Entry) -> tuple[str, str, str]:
    """(headword with its native words, languages, meaning) of one entry as markdown cells."""
    marks = iter(e.native)

    def shown(n: dict) -> str:
        return native_span(n["text"], n["lang"], n.get("check", False)) if n.get("text") else f"[unread {LANG_CODES.get(n['lang'], n['lang'] or '?')}]"

    def fill(text: str) -> str:
        """Native script printed in the text becomes spans; markers left by the text layer are filled from the recognised words."""
        text = wrap_scripts(text, e.text[:90])
        for n in marks:
            if "⟦native⟧" not in text:
                break
            text = text.replace("⟦native⟧", shown(n), 1)
        return text.replace("⟦native⟧", "[unread]")

    text = _escape(re.sub(r"\s+", " ", e.text))
    term = _escape(re.sub(r"\s+", " ", e.term))
    if not e.languages and len(e.term.split()) > 4:  # a sentence, not a headword: the entry's tail cut loose
        return "", "", fill(text)
    rest = text[len(term):] if text.startswith(term) else text
    rest = _RE_CODES.sub("", rest.lstrip(" ,")).lstrip(" ,")
    head = f"**{term}**" if text.startswith(term) else ""
    first = re.match(r"^\(([^()]*)\)[,.;:]?\s*", rest)
    if first and ("⟦native⟧" in first.group(1) or _SCRIPT_RUN.search(first.group(1))) and not re.search(r"[A-Za-z]{4,}", first.group(1).replace("⟦native⟧", "")):
        head += " " + fill(first.group(1))
        rest = rest[first.end():]
    return head.strip(), ", ".join(e.languages), fill(rest).strip()


_SCAN = "https://archive.org/details/glossaryofjudici00wilsuoft"


def render_table(entries: list[Entry], title: str = "Wilson Glossary") -> str:
    """The whole glossary as one note: an A to Z section per letter, each a table of term, language and meaning.

    The supplement follows the main glossary under the same letter headings. "See X" references link to the
    letter section that holds the headword, when exactly one headword has that name.
    """
    kept = [e for e in entries if e.section in ("glossary", "supplement")]
    seen: dict[str, set] = {}
    letters_of = assign_letters(kept)
    for e, letter in zip(kept, letters_of):
        if e.main and e.section == "glossary":
            for name in _names(e):
                seen.setdefault(name, set()).add(letter)
    targets = {k: (f"#{next(iter(v))}", "") for k, v in seen.items() if len(v) == 1}
    by_letter: dict[str, dict[str, list[str]]] = {}
    for e, letter in zip(kept, letters_of):
        term, lang, meaning = _split_entry(e)
        meaning = link_see(meaning, {k: (v[0], "") for k, v in targets.items()}, title, pipe="\\|")  # a pipe inside a table cell must be escaped
        by_letter.setdefault(letter, {}).setdefault(e.section, []).append(f"| {term} | {lang} | {meaning} | [{e.page}]({_SCAN}/page/n{e.page - 1}) |")
    letters = sorted(by_letter)
    counts = {sec: sum(e.section == sec for e in kept) for sec in ("glossary", "supplement")}
    out = [
        f"---\ntitle: {title}\ndescription: H. H. Wilson's Glossary of Judicial and Revenue Terms of British India (1855), rebuilt from a scan with its South Asian words.\ngenerated: true\ntags:\n  - Wilson\n---\n",
        "H. H. Wilson, *A Glossary of Judicial and Revenue Terms, and of Useful Words Occurring in Official Documents Relating to the Administration of the Government of British India* (London, 1855). Public domain. Scan: Internet Archive, `glossaryofjudici00wilsuoft`.\n",
        f"> [!Info] How this was made\n> A program read the book line by line from page images and rebuilt {counts['glossary']:,} entries and {counts['supplement']:,} supplement entries, with the accents and the Arabic, Urdu, Devanagari, Bengali, Telugu, Kannada and other scripts in place. "
        "Nobody retyped or corrected it, so expect mistakes: wrong letters in native scripts (Telugu and Kannada most), and some entries cut or run together. "
        "A native word with a dashed underline did not match the Latin spelling beside it. `[unread …]` marks a word that could not be read. A spot check of random rows found roughly one in five with a defect. The Scan column links each row to its page in the Internet Archive scan: check it there for anything that matters. "
        "The book's own index of words and page numbers is not included.\n",
        "**Letters:** " + " · ".join(f"[[{title}#{l}|{l}]]" for l in letters) + "\n",
    ]
    for letter in letters:
        parts = by_letter[letter]
        out += [f"## {letter}", "", "| Term | Language | Meaning | Scan |", "|---|---|---|---|"] + parts.get("glossary", [])
        if parts.get("supplement"):
            out += ["", f"### Supplement, {letter}", "", "The supplement at the end of the book, a second A to Z.", "", "| Term | Language | Meaning | Scan |", "|---|---|---|---|"] + parts["supplement"]
        out.append("")
    return "\n".join(out)


@click.command()
@click.option("--pdf", required=True, type=click.Path(exists=True, dir_okay=False), help="The scanned book with its OCR text layer.")
@click.option("--output-json", required=True, type=click.Path(), help="Where the entries are written.")
@click.option("--output-md", default=None, type=click.Path(), help="Also write the glossary section as markdown.")
@click.option("--output-table", default=None, type=click.Path(), help="Write the whole glossary as one note with a table per letter.")
@click.option("--lines", default=None, type=click.Path(exists=True), help="JSON lines from bdlaw-wilson-lines: the book read line by line from the page images.")
@click.option("--native", default=None, type=click.Path(exists=True), help="JSON lines from bdlaw-wilson-native to fill the native-script words.")
def main(pdf: str, output_json: str, output_md: Optional[str], output_table: Optional[str], lines: Optional[str], native: Optional[str]) -> None:
    """Rebuild Wilson's glossary entries from the PDF text layer."""
    readings = {r["key"]: r["text"] for r in map(json.loads, Path(lines).read_text(encoding="utf-8").splitlines())} if lines else None
    entries = build_entries(Path(pdf), readings)
    if native:
        rows = (json.loads(l) for l in Path(native).read_text(encoding="utf-8").splitlines())
        apply_native(entries, {r["id"]: r["text"] for r in rows if r["status"] == "ok"})
    entries = merge_bare(fold_fragments(entries))
    Path(output_json).write_text(json.dumps([asdict(e) for e in entries], ensure_ascii=False, indent=1), encoding="utf-8")
    if output_md:
        Path(output_md).write_text(render_markdown(entries), encoding="utf-8")
    if output_table:
        Path(output_table).write_text(render_table(entries), encoding="utf-8")
    counts = {sec: sum(e.section == sec for e in entries) for sec in ("glossary", "supplement", "index")}
    click.echo(f"{len(entries)} entries {counts}, {sum(e.main for e in entries)} main headwords -> {output_json}")


if __name__ == "__main__":
    main()
