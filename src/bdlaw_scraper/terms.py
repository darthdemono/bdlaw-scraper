"""
Defined-terms glossary built from the definitions inside the acts themselves.

Reads the act-N.md files (the scraper's own output), pulls out every
"term" means ... definition, groups them by term, orders the wordings by the
date of the act, and writes one note per initial letter:

  * the newest wording is the current definition,
  * older wordings stay underneath, with what changed,
  * the same wording in several acts is listed once, with all its acts,
  * a definition that only points at another act is listed as a reference.

A definition is scoped to its own act ("In this Act ..."). The newest wording is
the newest act in the vault, not a ruling on which one applies.

Usage:
  bdlaw-terms --acts-dir ~/Documents/Shared/BDLAW/acts
"""

from __future__ import annotations

import difflib
import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import click

_BN = re.compile(r"[ঀ-৿]")

# Section headings that hold definitions ("Section 2: Definitions", "ধারা ২: সংজ্ঞা")
_RE_DEF_TITLE = re.compile(r"definition|interpretation|সংজ্ঞা", re.I)
_RE_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_RE_SEC_HEADING = re.compile(r"^#{3,6}\s+\[?(?P<title>[^\]\n]*?)\]?(?:\([^)]*\))?\s*\^sec-(?P<id>[a-z0-9]+)\s*$")
_RE_SEC_NUM = re.compile(r"^(?:Section|ধারা|Article|Rule|বিধি)\s+(?P<num>[^:\s]+)", re.I)
_RE_MARK = re.compile(r"<!--n:(.*?)-->\s*")
_RE_TOP_ITEM = re.compile(r"^\d+\.\s+(?:<!--n:(?P<mark>.*?)-->\s*)?(?P<text>.*)$")
_RE_SUB_ITEM = re.compile(r"^(?P<indent>\s+)\d+\.\s+(?:<!--n:(?P<mark>.*?)-->\s*)?(?P<text>.*)$")
_RE_FOOTNOTE_DEF = re.compile(r"^\[\^(\d+)\]:\s*(.*)$", re.M)

_Q_OPEN, _Q_CLOSE = "“\"‘'`", "”\"’'"
_RE_QUOTED = re.compile(rf"^(?:\*\*)?[{_Q_OPEN}]+\s*(?P<t>[^{_Q_CLOSE}]{{1,100}}?)\s*[{_Q_CLOSE}]+(?:\*\*)?(?P<rest>.*)$", re.S)
_RE_ALT = re.compile(rf"^\s*(?:,|or|and|এবং|বা|ও)\s*[{_Q_OPEN}]+(?P<t>[^{_Q_CLOSE}]{{1,100}}?)\s*[{_Q_CLOSE}]+(?P<rest>.*)$", re.S)
_RE_BOLD = re.compile(r"^\*\*(?P<t>[^*]{1,100})\*\*(?P<rest>.*)$", re.S)

# "has the same meaning as in the Transfer of Property Act": points elsewhere, is not a wording of its own
_RE_BY_REF = re.compile(
    r"same meaning|meaning assigned|meaning (?:given|respectively assigned)|as defined in|defined in|সংজ্ঞায়িত|অর্থে ব্যবহৃত|যে অর্থে",
    re.I,
)
_RE_THIS_ACT = re.compile(r"\bthis (?:Act|Ordinance|Code|Order|Regulation|Rules?|Law)\b|এই (?:আইন|অধ্যাদেশ|বিধিমালা|প্রবিধানমালা|আদেশ)")
# "...; [^8](xxa) “Militia” means ...": several definitions run together on one source line
_RE_INLINE_NEXT = re.compile(
    rf";\s*(?:\[\^\d+\])*\s*\((?:[a-z]{{1,4}}|[ivx]+|[\d]{{1,3}}[A-Za-z]?)\)\s*(?=[{_Q_OPEN}]+[^{_Q_CLOSE}]{{1,100}}[{_Q_CLOSE}]+\s*(?:means|includes|অর্থ))"
)
_RE_OMITTED_MARK = re.compile(r"\s*\[+\^?\d*\]*\[?\s*(?:omitted|repealed)\s*\]+", re.I)
_RE_WIKILINK = re.compile(r"\[\[([^\]|\\]+)(?:\\?\|([^\]]*))?\]\]")


@dataclass
class Definition:
    """One "term" means ... definition found in one act."""

    terms: list[str]
    text: str
    subitems: list[tuple[int, str, str]]  # (depth, marker, text)
    act: str  # "act-24"
    act_title: str
    year: int
    date: str
    section_id: str
    section_label: str
    notes: list[str] = field(default_factory=list)  # footnote texts of this definition
    by_ref: bool = False

    @property
    def lang(self) -> str:
        return "bn" if _BN.search(self.terms[0]) else "en"

    @property
    def link(self) -> str:
        target = f"{self.act}#^sec-{self.section_id}" if self.section_id else self.act
        title = _unwiki(self.act_title)
        label = f"{title}, {self.section_label}" if self.section_label else title
        return f"[[{target}|{label}]]"


def _unwiki(text: str) -> str:
    return _RE_WIKILINK.sub(lambda m: m.group(2) or m.group(1), text)


def _norm_key(term: str) -> str:
    """Grouping key for a term: NFC, no quotes or trailing gloss, casefolded."""
    t = unicodedata.normalize("NFC", term)
    t = re.sub(r"\s*\([^)]*\)\s*$", "", t)
    t = re.sub(rf"[{_Q_OPEN}{_Q_CLOSE}*]+", "", t)
    return re.sub(r"\s+", " ", t).strip(" .,;:-").casefold()


def _norm_text(text: str) -> str:
    """Comparison form of a wording: no links, notes or punctuation, "this Act" for any enactment word."""
    t = _unwiki(_RE_MARK.sub("", text))
    t = re.sub(r"\[\^\d+\]", "", t)
    t = _RE_THIS_ACT.sub("this_act", t)
    t = re.sub(r"[^\wঀ-৿]+", " ", t.casefold())
    return t.strip()


def _clean_text(text: str) -> str:
    t = _RE_OMITTED_MARK.sub("", text.replace("\\|", "|")).strip()
    t = re.sub(r"\s*[;,]\s*(?:and|or|এবং|বা)?\s*$", "", t)
    return t.rstrip(" ;").strip()


def _split_term(text: str) -> Optional[tuple[list[str], str]]:
    """Split "“term” means ..." into (terms, rest). Returns None when the text is not a definition."""
    m = _RE_QUOTED.match(text) or _RE_BOLD.match(text)
    if not m:
        return None
    terms, rest = [m.group("t").strip()], m.group("rest")
    while (alt := _RE_ALT.match(rest)) is not None:
        terms.append(alt.group("t").strip())
        rest = alt.group("rest")
    rest = rest.strip()
    if not terms[0] or not rest:
        return None
    return terms, rest


def _frontmatter(text: str) -> dict:
    out: dict = {}
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return out
    for line in m.group(1).split("\n"):
        k, sep, v = line.partition(":")
        if sep and not line.startswith(" "):
            out[k.strip()] = v.strip().strip('"')
    return out


def parse_act(text: str, act: str) -> list[Definition]:
    """Extract every definition from one act's markdown.

    Definitions are read from sections whose heading says Definitions, Interpretation or
    সংজ্ঞা (numbered list items and plain "term" means lines) and from > [!Definition]
    callouts anywhere in the act. Nested items under a definition are kept with it.

    @param text  Full markdown of the act, frontmatter included.
    @param act   File stem used for wikilinks ("act-24").
    @return      Definitions in document order.
    """
    fm = _frontmatter(text)
    title = fm.get("title", act)
    year = int(fm["year"]) if fm.get("year", "").isdigit() else 0
    date = fm.get("enacted_at") or (f"{year}-01-01" if year else "0000-00-00")
    notes = {n: t for n, t in _RE_FOOTNOTE_DEF.findall(text)}
    body = text.split("\n## Footnotes", 1)[0]

    defs: list[Definition] = []
    cur: Optional[Definition] = None
    in_defs = False
    sec_id = sec_label = ""
    from_callout = False
    pending_callout = False

    def start(terms: list[str], rest: str) -> Definition:
        chunks = _RE_INLINE_NEXT.split(rest)
        d: Optional[Definition] = None
        for i, chunk in enumerate(chunks):
            if i:
                sp = _split_term(chunk.strip())
                if not sp:
                    continue
                terms, chunk = sp
            t = _clean_text(chunk)
            d = Definition(terms, t, [], act, title, year, date, sec_id, sec_label)
            for n in re.findall(r"\[\^(\d+)\]", t):
                if n in notes:
                    d.notes.append(notes[n])
            d.by_ref = bool(_RE_BY_REF.search(t)) and len(_norm_text(t)) < 300
            defs.append(d)
        return d if d is not None else defs[-1]

    for raw in body.split("\n"):
        line = raw.rstrip()
        hm = _RE_HEADING.match(line)
        if hm:
            sm = _RE_SEC_HEADING.match(line)
            if sm:
                sec_id = sm.group("id")
                label = sm.group("title")
                nm = _RE_SEC_NUM.match(label)
                sec_label = (("ধারা " if _BN.search(label) else "section ") + nm.group("num").rstrip(".,")) if nm else ""
                in_defs = bool(_RE_DEF_TITLE.search(label))
            cur, pending_callout = None, False
            continue
        if line.startswith(">"):
            inner = line.lstrip("> ").strip()
            if re.match(r"\[!Definition\]", inner, re.I):
                pending_callout, cur = True, None
                continue
            if pending_callout and inner:
                sp = _split_term(inner)
                if sp:
                    cur = start(*sp)
                    from_callout = True
                pending_callout = False
                continue
            if re.match(r"\[!", inner):  # another callout ends a definition
                cur = None
            elif cur is not None and from_callout and inner:
                cur.text += " " + _clean_text(inner)
            continue
        pending_callout = False
        if not line.strip():
            continue
        if not in_defs and not (cur is not None and from_callout):
            cur = None
            continue
        m = _RE_TOP_ITEM.match(line)
        if m:
            sp = _split_term(m.group("text").strip())
            if sp:
                cur, from_callout = start(*sp), False
            elif cur is not None:
                cur.subitems.append((0, (m.group("mark") or "").strip(), _clean_text(m.group("text"))))
            continue
        sm2 = _RE_SUB_ITEM.match(line)
        if sm2 and in_defs and (sp := _split_term(sm2.group("text").strip())):
            cur, from_callout = start(*sp), False  # a definition nested by a numbering quirk is still a definition
            continue
        if sm2 and cur is not None:
            depth = max(1, len(sm2.group("indent").expandtabs(3)) // 3)
            cur.subitems.append((depth, (sm2.group("mark") or "").strip(), _clean_text(sm2.group("text"))))
            continue
        if in_defs:
            plain = _RE_MARK.sub("", line.strip())
            sp = _split_term(plain)
            if sp:
                cur, from_callout = start(*sp), False
            elif cur is not None and not cur.subitems:
                cur.text += " " + _clean_text(plain)
    return [d for d in defs if d.text]


# ---------------------------------------------------------------------------
# Grouping and rendering
# ---------------------------------------------------------------------------
@dataclass
class Wording:
    """One distinct wording of a term and the acts that use it, newest act first."""

    defs: list[Definition]

    @property
    def date(self) -> str:
        return max(d.date for d in self.defs)

    @property
    def lead(self) -> Definition:
        return max(self.defs, key=lambda d: (d.date, d.act))


def group_terms(defs: list[Definition]) -> dict[str, list[Definition]]:
    """Group definitions by (language, normalised term)."""
    out: dict[str, list[Definition]] = {}
    for d in defs:
        for t in d.terms[:1]:
            out.setdefault(f"{d.lang}:{_norm_key(t)}", []).append(d)
    return out


def wordings(defs: list[Definition]) -> list[Wording]:
    """Distinct wordings of a term, newest first. Same wording in several acts is one Wording."""
    by: dict[str, list[Definition]] = {}
    for d in defs:
        if d.by_ref:
            continue
        key = _norm_text(d.text + " " + " ".join(f"{n}.{m}.{t}" for n, m, t in d.subitems))
        by.setdefault(key, []).append(d)
    ws = [Wording(sorted(v, key=lambda d: (d.date, d.act), reverse=True)) for v in by.values()]
    return sorted(ws, key=lambda w: (w.date, w.lead.act), reverse=True)


def word_diff(old: str, new: str) -> str:
    """What the newer wording removed (~~struck~~) and added (**bold**) compared with the older one."""
    a, b = _unwiki(_RE_MARK.sub("", old)).split(), _unwiki(_RE_MARK.sub("", new)).split()
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    if sm.ratio() < 0.35:
        return "rewritten"
    out: list[str] = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            out.append(" ".join(a[i1:i2]))
            continue
        if i2 > i1:
            out.append("~~" + " ".join(a[i1:i2]) + "~~")
        if j2 > j1:
            out.append("**" + " ".join(b[j1:j2]) + "**")
    return " ".join(out)


def _slug(term: str, taken: set[str]) -> str:
    ascii_ = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", term).encode("ascii", "ignore").decode().lower()).strip("-")
    slug = f"term-{ascii_}" if ascii_ and not _BN.search(term) else "term-" + hashlib.sha1(term.encode()).hexdigest()[:8]
    base, n = slug, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    taken.add(slug)
    return slug


def _wording_text(d: Definition, term: str, notes: list[str]) -> list[str]:
    """Callout body lines for one wording (term, text, nested items), footnotes appended to notes."""
    def refs(text: str) -> str:
        return re.sub(r"\[\^\d+\]", "", text)  # act footnote numbers mean nothing here: notes are renumbered

    first = refs(d.text)
    q = "“%s”" % term
    lines = [f"> {q} {first}"]
    for depth, mark, text in d.subitems:
        lines.append(">" + "  " * depth + " - " + (f"{mark} " if mark else "") + refs(text))
    for n in d.notes:
        notes.append(n)
        lines[0] += f"[^t{len(notes)}]"
    return lines


def _acts_line(ws: list[Definition], limit: int = 6) -> tuple[str, bool]:
    links = [d.link for d in ws]
    return ", ".join(links), len(links) > limit


def render_term(term: str, defs: list[Definition], slug: str, notes: list[str]) -> str:
    """Markdown block for one term: current wording, other acts with it, earlier wordings, references."""
    ws = wordings(defs)
    refs = sorted((d for d in defs if d.by_ref), key=lambda d: (d.date, d.act), reverse=True)
    out = [f"### {term} ^{slug}", ""]
    if ws:
        cur = ws[0]
        lead = cur.lead
        year = lead.year or ""
        out.append(f"> [!Definition] Current wording ({year}) — {lead.link}")
        out += _wording_text(lead, term, notes)
        out.append("")
        others = [d for d in cur.defs if d is not lead]
        if others:
            line, fold = _acts_line(others)
            out += [f"Same wording in {len(others)} other act{'s' if len(others) > 1 else ''}:", ""]
            out += ([f"> [!Note]- Acts with the same wording", f"> {line}"] if fold else [line])
            out.append("")
        if len(ws) > 1:
            out += ["**Earlier wordings**", ""]
            for i, old in enumerate(ws[1:], 1):
                newer = ws[i - 1]
                ol = old.lead
                n_more = len(old.defs) - 1
                more = f" and {n_more} other act{'s' if n_more > 1 else ''}" if n_more else ""
                out.append(f"> [!Note]- {ol.year or ''} — {ol.link}{more}")
                out += _wording_text(ol, term, notes)
                diff = word_diff(old.lead.text, newer.lead.text)
                out += [">", f"> Changed in the newer wording: {diff}"]
                if more:
                    out += [">", "> Same wording: " + ", ".join(d.link for d in old.defs[1:])]
                out.append("")
    if refs:
        out += ["**Defined by reference**", ""]
        out += [f"- {d.link}: {_clean_text(d.text)}" for d in refs]
        out.append("")
    return "\n".join(out)


def _letter(term: str) -> str:
    t = re.sub(r"^[^\wঀ-৿]+", "", term)
    c = t[:1]
    if _BN.match(c):
        return c
    return c.upper() if c.isalpha() else "0-9"


def build(acts_dir: Path) -> tuple[dict[str, str], int, int]:
    """Parse every act and render the glossary notes.

    @param acts_dir  Folder of act-N.md files.
    @return          ({note title: markdown}, term count, definition count).
    """
    all_defs: list[Definition] = []
    for f in sorted(acts_dir.glob("act-*.md")):
        all_defs += parse_act(f.read_text(encoding="utf-8"), f.stem)
    groups = group_terms(all_defs)
    by_letter: dict[str, list[tuple[str, list[Definition]]]] = {}
    for defs in groups.values():
        display = max((d.terms[0] for d in defs), key=lambda t: sum(1 for d in defs if d.terms[0] == t))
        by_letter.setdefault(_letter(display), []).append((display, defs))
    notes_out: dict[str, str] = {}
    today = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    for letter, items in by_letter.items():
        items.sort(key=lambda it: it[0].casefold())
        notes: list[str] = []
        taken: set[str] = set()
        blocks = [render_term(t, d, _slug(t, taken), notes) for t, d in items]
        title = f"Defined Terms {letter}"
        body = "\n".join(blocks)
        body = re.sub(r"\[\^t(\d+)\]", r"[^\1]", body)
        foot = ["", "## Footnotes", ""] + [f"[^{i}]: {n}" for i, n in enumerate(notes, 1)] if notes else []
        lang = "bn" if _BN.match(letter) else "en"
        fm = [
            "---", f'title: "{title}"', f"description: Terms beginning with {letter} as defined in the acts, newest wording first, with earlier wordings.",
            f"datetime: {today}", "tags:", "  - Legal", "  - Bangladesh", "  - Glossary", f"language: {lang}", f"terms: {len(items)}", "generated: true", "---", "",
            f"Terms beginning with **{letter}**: {len(items)}. Newest wording first; older wordings follow.", "",
            "> [!Info] Generated, not written by hand",
            "> A program built this page from the text of the acts. Every definition is copied word for word from the act and section linked beside it; none was written or edited by the site owner. See [[Defined Terms]] for how it works.", "",
        ]
        notes_out[title] = "\n".join(fm) + body + "\n".join(foot) + "\n"
    letters = sorted(by_letter, key=lambda k: (bool(_BN.match(k)), k == "0-9", k))
    index = [
        "---", 'title: "Defined Terms"',
        "description: Glossary of terms defined inside Bangladesh acts, with the newest wording first and earlier wordings kept to show how each term changed.",
        f"datetime: {today}", "tags:", "  - Legal", "  - Bangladesh", "  - Glossary", "---", "",
        f"Terms defined in the acts of Bangladesh: {len(groups)} terms from {len(all_defs)} definitions.", "",
        "- Each term shows its **current wording**, which is the wording in the newest act that defines it, then the **earlier wordings** with what changed.",
        "- The same wording in several acts is shown once, with every act that uses it.",
        "- A definition that only points at another act is listed under *Defined by reference*.", "",
        "## How this glossary is made", "",
        "This glossary is **generated by a program**, not written by the site owner or by anyone else. No definition on these pages is an original definition or an interpretation: each one is copied word for word from the act and section linked next to it.", "",
        "The program (`bdlaw-terms`, part of the open-source bdlaw-scraper) works in these steps:", "",
        "1. It reads every act in this collection and finds the sentences of the form \"term\" means ... / \"term\" includes ... / \"term\" অর্থ ... inside the definition sections (Definitions, Interpretation, সংজ্ঞা) and in definition callouts.",
        "2. It groups the definitions by the term, keeping English and Bengali terms apart.",
        "3. It orders the wordings by the date the act was enacted. The newest wording is shown as the *current wording*, the others as *earlier wordings*.",
        "4. It treats wordings that are the same except for punctuation and the words \"this Act\" or \"this Ordinance\" as one wording, and lists all the acts that use it.",
        "5. It compares each earlier wording with the next newer one word by word. The ~~struck~~ and **bold** marks show words the newer wording removed or added; this is a mechanical comparison, not commentary. A very different pair is marked *rewritten*.",
        "6. The amendment footnotes attached to a definition are copied from the act.", "",
        "**Limits.** A definition applies only to the act it appears in (\"In this Act ...\"). The newest wording here is the newest act in this collection, not a ruling on which definition applies to a given case. Because the program works from patterns, it can miss a definition, split or merge a term, or order wordings wrongly when an act has no enactment date. Always check the linked act, and the official source, before relying on a definition. This is not legal advice.", "",
        "## Letters", "",
        "  ".join(f"[[Defined Terms {k}|{k}]] ({len(by_letter[k])})" for k in letters), "",
    ]
    notes_out["Defined Terms"] = "\n".join(index) + "\n"
    return notes_out, len(groups), len(all_defs)


@click.command()
@click.option("--acts-dir", required=True, type=click.Path(exists=True, file_okay=False), help="Folder of act-N.md files.")
@click.option("--output-dir", default=None, type=click.Path(), help="Where the notes go (default: 'Defined Terms' next to the acts folder).")
def main(acts_dir: str, output_dir: Optional[str]) -> None:
    """Build the defined-terms glossary from the definitions inside the acts."""
    src = Path(acts_dir)
    out = Path(output_dir) if output_dir else src.parent / "Defined Terms"
    out.mkdir(parents=True, exist_ok=True)
    notes, n_terms, n_defs = build(src)
    for old in out.glob("Defined Terms*.md"):
        old.unlink()
    for title, text in notes.items():
        (out / f"{title}.md").write_text(text, encoding="utf-8")
    click.echo(f"{n_terms} terms from {n_defs} definitions in {len(notes)} notes -> {out}")


if __name__ == "__main__":
    main()
