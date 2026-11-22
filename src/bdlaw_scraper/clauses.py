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


def anchor_items(md: str) -> tuple[str, set[str]]:
    """Put a block id at the end of every single-line list item. Returns the Markdown and the ids written."""
    lines = md.split("\n")
    known: set[str] = set()
    base = ""
    stack: dict[int, str] = {}
    for i, line in enumerate(lines):
        head = _RE_SEC_HEAD.match(line)
        if head:
            base, stack = head.group(1), {}
            continue
        if line.startswith("#"):
            base, stack = "", {}
            continue
        m = _RE_LIST_LINE.match(line)
        if not m or not base:
            continue
        depth = len(m.group(1)) // 3
        for d in [d for d in stack if d >= depth]:
            del stack[d]
        mark = _mark_id(m.group(2))
        stack[depth] = mark
        if not mark or any(d not in stack for d in range(depth)) or _RE_HAS_ID.search(line):
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if nxt.strip() and not _RE_LIST_LINE.match(nxt) and not nxt.startswith("#"):
            continue  # a multi-line item: the id would have to sit on its last line
        ident = base + "".join("-" + stack[d] for d in range(depth + 1))
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
    md, known = anchor_items(md)
    sections = set(re.findall(r"\^(sec-[a-z0-9]+)\s*$", md, flags=re.M))
    return head + link_clauses(md, act_number, known | sections)