"""Anchors for every list item of an act, and links between clauses.

A section's list items get a block id built from the section's own id and the item markers on the way down to
it: item (c) of (1) in section 117 is "^sec-117-1-c". The id sits at the end of the item's line, so Obsidian and
the vellum site both resolve "[[act-367#^sec-117-1-c|...]]" to that exact clause, and anyone can link to it.

References such as "clause (3) of article 102" or "sub-section (2) of section 5" are then linked to the clause
itself, not only to its section. Only ids that exist are linked, so a reference never points at nothing.
"""

import re

_RE_SEC_HEAD = re.compile(r"^#{2,6} .*? \^(sec-[\w-]+)\s*$")
_RE_LIST_LINE = re.compile(r"^(\s*)\d+\. <!--n:\(([^()]{1,6})\)--> ")
_RE_HAS_ID = re.compile(r" \^[A-Za-z0-9-]+$")
_RE_WIKI = re.compile(r"\[\[[^\]]*\]\]")
_MARK = r"\([A-Za-z0-9]{1,4}\)"
_LEVEL = rf"(?:sub-?)?(?:clause|section|paragraph|proviso)s?\s+{_MARK}(?:\s*(?:,|and|or)\s*{_MARK})*\s+of\s+"
_ROOT_PLAIN = r"(?:sections?|articles?)\s+(\d+[A-Z]{0,2})\b"
_ROOT_LINK = r"\[\[act-(\d+)#\^(sec-[\w-]+)\\\|([^\]]*)\]\]"
_RE_NESTED = re.compile(rf"(?P<lev>(?:{_LEVEL}){{1,3}})(?:(?P<link>{_ROOT_LINK})|(?P<plain>{_ROOT_PLAIN}))", re.IGNORECASE)
# A section link that already names a clause in its label ("section 5(2)(a)", "ধারা ৬(১) এর দফা (ক)") goes to that
# clause. Latin and Bengali markers alike: they are transliterated to ASCII ids.
_RE_SEC_LINK = re.compile(r"\[\[act-(\d+)#\^(sec-[a-z0-9]+)\\\|([^\]]*)\]\]")
_RE_LABEL_MARK = re.compile(r"\(([^()\s]{1,4})\)")
_RE_LABEL_NUM = re.compile(r"(?<![\w(])[0-9০-৯]+[A-Za-zক-হ]?(?![\w)])")
_RE_REL_BN = re.compile(r"(?:উপ-?ধারা|দফা)\s*\(([^()\s]{1,4})\)(?!\s*(?:এর|র)?\s*(?:ধারা|অনুচ্ছেদ))")
_RE_RELATIVE = re.compile(rf"\b(?:sub-?section|clause)\s+\(([A-Za-z0-9]{{1,4}})\)(?!\s*(?:,|and|or|of)\b)", re.IGNORECASE)


def _mark_id(mark: str) -> str:
    from bdlaw_scraper.scraper import _secnum_to_block_id

    return _secnum_to_block_id(mark)


_RE_INSERTED = re.compile(r"^(\d+)[a-z]{1,2}$")
_RE_PLAIN_NUM = re.compile(r"^(\d+)$")


def _inserted_of(mark: str, lines: list[str], i: int, depth: int) -> int | None:
    """Depth of the clause ("21") this inserted clause ("21a") belongs to, or None when it is not an inserted clause."""
    m = _RE_INSERTED.match(mark)
    if not m:
        return None
    for j in range(i - 1, -1, -1):
        p = _RE_LIST_LINE.match(lines[j])
        if p and len(p.group(1)) // 3 < depth:
            pm = _RE_INSERTED.match(p.group(2)) or _RE_PLAIN_NUM.match(p.group(2))
            if pm and pm.group(1) == m.group(1):
                return len(p.group(1)) // 3
            if len(p.group(1)) // 3 == 0:
                return None
        if lines[j].startswith("#"):
            break
    return None


def nest_inserted_clauses(md: str) -> str:
    """Indent inserted clauses ("(1a)", "(1b)") under the clause they were inserted after ("(1)")."""
    lines = md.split("\n")
    out: list[str] = []
    base = ""        # digits of the last plain top-level clause
    inserted = False  # inside an indented inserted clause: its own children move in too
    k = 0
    kids = False
    for line in lines:
        m = _RE_LIST_LINE.match(line)
        if m and not m.group(1):
            mark = m.group(2)
            pm = _RE_PLAIN_NUM.match(mark)
            im = _RE_INSERTED.match(mark)
            if pm:
                base, inserted, k, kids = pm.group(1), False, 0, False
            elif im and im.group(1) == base:
                inserted, k = True, k + 1
                pad = "      " if kids else "   "  # a parent with its own (a), (b): the inserted run goes under the last of them
                out.append(pad + re.sub(r"^\d+\.", f"{k}.", line, count=1))
                continue
            else:
                base, inserted = "", False
        elif line.startswith("#"):
            base, inserted = "", False
        elif m and base and not inserted:
            kids = True  # an indented list line right after a plain clause: its own sub-clauses
            out.append(line)
            continue
        elif inserted and line.startswith("   ") and line.strip():
            out.append(("      " if kids else "   ") + line)
            continue
        elif line.strip() and not line.startswith(" "):
            inserted = False
        out.append(line)
    return "\n".join(out)


def anchor_items(md: str) -> tuple[str, set[str]]:
    """Put a block id at the end of every single-line list item. Returns the Markdown and the ids written."""
    lines = md.split("\n")
    known: set[str] = set()
    base = ""
    stack: dict[int, str] = {}
    hidden: set[int] = set()
    for i, line in enumerate(lines):
        head = _RE_SEC_HEAD.match(line)
        if head:
            base, stack, hidden = head.group(1), {}, set()
            continue
        if line.startswith("#"):
            base, stack, hidden = "", {}, set()
            continue
        m = _RE_LIST_LINE.match(line)
        if not m or not base:
            continue
        depth = len(m.group(1)) // 3
        for d in [d for d in stack if d >= depth]:
            del stack[d]
        hidden = {d for d in hidden if d < depth}
        mark = _mark_id(m.group(2))
        stack[depth] = mark
        pd = _inserted_of(m.group(2), lines, i, depth) if depth else None
        if pd is not None:
            hidden.update(range(pd, depth))  # (1a) sits inside (1) but keeps the id "1a"
        if not mark or any(d not in stack for d in range(depth)) or _RE_HAS_ID.search(line):
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if nxt.strip() and not _RE_LIST_LINE.match(nxt) and not nxt.startswith("#"):
            continue  # a multi-line item: the id would have to sit on its last line
        ident = base + "".join("-" + stack[d] for d in range(depth + 1) if d not in hidden)
        if ident in known:
            continue
        known.add(ident)
        lines[i] = f"{line} ^{ident}"
    return "\n".join(lines), known


def link_clauses(md: str, act_number: str, known: set[str]) -> str:
    """Link "clause (3) of article 102" style references, and "sub-section (2)" within the same section."""
    from bdlaw_scraper.scraper import _secnum_to_block_id

    def target(base: str, marks_by_level: list[list[str]]) -> str:
        path: list[str] = []
        for marks in marks_by_level:
            if len(marks) != 1:
                break
            path.append(_mark_id(marks[0]))
        while path:
            cand = base + "".join("-" + p for p in path)
            if cand in known:
                return cand
            path.pop()
        return base

    def nested(m: re.Match) -> str:
        levels = [re.findall(_MARK, part) for part in re.split(r"\bof\s+", m.group("lev"), flags=re.I) if part.strip()]
        levels = [[x.strip("()") for x in lv] for lv in reversed(levels)]
        if m.group("link"):
            act, base, label = m.group(3), m.group(4), m.group(5)
            if act != act_number:
                return m.group(0)
            shown = m.group("lev") + label
        else:
            base = "sec-" + _secnum_to_block_id(m.group(7))
            if base not in known_sections:
                return m.group(0)
            shown = m.group(0)
        if len(levels) == 1 and len(levels[0]) > 1:
            # "clauses (9), (12) and (38) of section 3": every clause gets its own link, then the section
            def one(x: re.Match) -> str:
                ident = f"{base}-{_mark_id(x.group(0).strip('()'))}"
                return f"[[act-{act_number}#^{ident}\\|{x.group(0)}]]" if ident in known else x.group(0)

            root = label if m.group("link") else m.group("plain")
            return f"{re.sub(_MARK, one, m.group('lev'))}[[act-{act_number}#^{base}\\|{root}]]"
        return f"[[act-{act_number}#^{target(base, levels)}\\|{shown}]]"

    def deepen(m: re.Match) -> str:
        act, base, label = m.groups()
        marks = _RE_LABEL_MARK.findall(label)
        if act != act_number or not marks or len(_RE_LABEL_NUM.findall(label)) != 1:
            return m.group(0)
        deep = target(base, [[x] for x in marks])
        return m.group(0) if deep == base else f"[[act-{act_number}#^{deep}\\|{label}]]"

    known_sections = {k for k in known if re.fullmatch(r"sec-[a-z0-9]+", k)}
    lines = md.split("\n")
    cur = ""
    in_notes = False
    for i, line in enumerate(lines):
        head = _RE_SEC_HEAD.match(line)
        if head:
            cur = head.group(1)
        if line.startswith("## Footnotes"):
            in_notes, cur = True, ""
        if line.startswith("#") or line.startswith("---") or "<!--" in line.split("<!--n:")[0][:4]:
            continue
        line = _RE_SEC_LINK.sub(deepen, _RE_NESTED.sub(nested, line))
        if cur and not in_notes:
            def rel(m: re.Match, cur=cur) -> str:
                ident = f"{cur}-{_mark_id(m.group(1))}"
                return f"[[act-{act_number}#^{ident}\\|{m.group(0)}]]" if ident in known else m.group(0)

            parts = re.split(f"({_RE_WIKI.pattern})", line)
            line = "".join(p if p.startswith("[[") else _RE_REL_BN.sub(rel, _RE_RELATIVE.sub(rel, p)) for p in parts)
        lines[i] = line
    return "\n".join(lines)


def anchor_and_link(md: str, act_number: str) -> str:
    # the YAML front matter is not text to link: "\|" inside its quoted description is an invalid escape
    head = ""
    fm = re.match(r"---\n.*?\n---\n", md, re.DOTALL)
    if fm:
        head, md = md[: fm.end()], md[fm.end() :]
    md, known = anchor_items(nest_inserted_clauses(md))
    sections = set(re.findall(r"\^(sec-[a-z0-9]+)\s*$", md, flags=re.M))
    md = link_clauses(md, act_number, known | sections)
    return head + link_footnote_clauses(md, act_number)

_FN_REF = re.compile(r"\[\^(\d+)\](?!:)")
_FN_DEF = re.compile(r"^\[\^(\d+)\]: (.*)$")
_FN_CLAUSES = re.compile(r"(?i)\b(?:clauses?|sub-?sections?|paragraphs?)\s+((?:\([0-9A-Za-z]+\)(?:\s*,\s*|\s+(?:and|or)\s+)?)+)")
_SEC_ID = re.compile(r"\^(sec-[a-z0-9]+)\s*$")


def link_footnote_clauses(md: str, act_number: str) -> str:
    """"Clause (21) was substituted ..." in a footnote links to that clause of the section carrying the note."""
    lines = md.split("\n")
    anchors = set(re.findall(r"\^(sec-[a-z0-9-]+)\s*$", md, flags=re.M))
    where: dict[str, str] = {}
    cur = ""
    for ln in lines:
        if ln.startswith("#"):
            m = _SEC_ID.search(ln)
            cur = m.group(1) if m else cur
        elif not _FN_DEF.match(ln):
            for n in _FN_REF.findall(ln):
                where.setdefault(n, cur)
    for i, ln in enumerate(lines):
        d = _FN_DEF.match(ln)
        if not d or not where.get(d.group(1)):
            continue
        sec = where[d.group(1)]

        def fix(m: re.Match) -> str:
            def one(x: re.Match) -> str:
                a = f"{sec}-{x.group(1).lower()}"
                return f"[[act-{act_number}#^{a}\\|({x.group(1)})]]" if a in anchors else x.group(0)
            return re.sub(r"\(([0-9A-Za-z]+)\)", one, m.group(0))

        lines[i] = f"[^{d.group(1)}]: " + _FN_CLAUSES.sub(fix, d.group(2))
    return "\n".join(lines)
