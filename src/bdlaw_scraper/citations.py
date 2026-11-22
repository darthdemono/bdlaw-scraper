"""Link plain-text act citations such as "Penal Code (XLV of 1860)" to the act file.

The source site only links some mentions. A citation names an act by kind, number and year, which
the front matter of every scraped act carries (act_kind, act_no_value, act_year), so the rest can be
resolved without guessing from titles.
"""
from __future__ import annotations

import re
from pathlib import Path

_ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}

_CITE = re.compile(
    r"((?:The\s+)?(?:[A-Z][\w'.-]*(?:\s+|\s+and\s+|\s+of\s+)){0,6}"
    r"(?:Code|Act|Ordinance|Order|Regulation)s?(?:,?\s+\d{4})?)"
    r"(\s*\((?:(?:Act|Ordinance|Ord\.|Order|P\.?O\.?|Regulation)\s+)?(?:No\.?\s*)?([IVXLCDM]+|\d+)\s+of\s+(\d{4})\))"
)
_SKIP = re.compile(r"\[\[.*?\]\]|\[[^\]\n]*\]\([^)\n]*\)|<!--.*?-->", re.S)
_FM = re.compile(r"\A---\n.*?\n---\n", re.S)


def roman(s: str) -> int:
    if s.isdigit():
        return int(s)
    total = 0
    for a, b in zip(s, s[1:] + " "):
        v = _ROMAN[a]
        total += -v if _ROMAN.get(b, 0) > v else v
    return total


def _group(kind: str) -> str:
    k = kind.lower()
    for g in ("ordinance", "order", "regulation"):
        if g in k:
            return g
    return "act"


def build_index(folder: Path) -> dict[tuple[str, int, int], str | None]:
    """(kind group, number, year) -> act stem; None when two acts share the key."""
    idx: dict[tuple[str, int, int], str | None] = {}
    for p in folder.rglob("act-[0-9]*.md"):
        head = p.read_text(encoding="utf-8")[:3000]
        f = {k: (re.search(rf'^{k}: "?([^"\n]*)', head, re.M) or [None, ""])[1] for k in ("act_kind", "act_no_value", "act_year")}
        if not (f["act_no_value"] and f["act_year"]):
            continue
        key = (_group(f["act_kind"]), int(f["act_no_value"]), int(f["act_year"]))
        idx[key] = None if key in idx else p.stem
    return idx


def link_citations(text: str, idx: dict, self_stem: str = "") -> tuple[str, int]:
    """Wrap the name before "(XLV of 1860)" in a wikilink to the act it cites."""
    fm = _FM.match(text)
    head, body = (text[: fm.end()], text[fm.end():]) if fm else ("", text)
    n = 0

    def fix(seg: str) -> str:
        nonlocal n

        def sub(m: re.Match) -> str:
            nonlocal n
            name = m.group(1)
            word = re.search(r"(Code|Act|Ordinance|Order|Regulation)", name)
            try:
                key = (_group(word.group(1)), roman(m.group(3)), int(m.group(4)))
            except KeyError:
                return m.group(0)
            stem = idx.get(key) or (idx.get(("act",) + key[1:]) if key[0] == "act" else None)
            if not stem or stem == self_stem:
                return m.group(0)
            n += 1
            if re.match(r"(?:The\s+)?(?:Code|Act|Ordinance|Order|Regulation)\b", name):
                # the name is cut short ("Services (Reorganization and Conditions) Act"): link the citation
                return f"{name} [[{stem}\\|{m.group(2).strip()}]]"
            return f"[[{stem}\\|{name}]]{m.group(2)}"

        return _CITE.sub(sub, seg)

    out, pos = [], 0
    for m in _SKIP.finditer(body):
        out.append(fix(body[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(fix(body[pos:]))
    return head + "".join(out), n


def link_folder(folder: Path, log=None) -> int:
    idx = build_index(folder)
    total = 0
    for p in folder.rglob("act-[0-9]*.md"):
        t = p.read_text(encoding="utf-8")
        new, n = link_citations(t, idx, p.stem)
        if n:
            p.write_text(new, encoding="utf-8")
            total += n
    if log:
        log.info("Linked %d act citations", total)
    return total


if __name__ == "__main__":
    idx = {("act", 45, 1860): "act-11"}
    s, n = link_citations('x Penal Code (XLV of 1860) y [[act-1|A (XLV of 1860)]] The Penal Code (Act No. XLV of 1860).', idx)
    assert n == 2 and s.startswith("x [[act-11\\|Penal Code]] (XLV of 1860)"), s
    print("ok")
