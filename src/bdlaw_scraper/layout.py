"""Year folders for the vault: acts/1897/act-73.md.

Wikilinks resolve by file name, so moving a file never breaks a link between acts. The old flat address
(acts/act-73) stays alive as an alias that the site turns into a redirect.
"""
from __future__ import annotations

import re
from pathlib import Path

_FM = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def _year(fm: str) -> str:
    for key in ("year", "act_year"):
        m = re.search(rf'^{key}: "?(\d{{4}})', fm, re.M)
        if m:
            return m.group(1)
    return "undated"


def _with_alias(fm: str, alias: str) -> str:
    line = f'  - "{alias}"'
    if line in fm:
        return fm
    if re.search(r"^aliases:\s*$", fm, re.M):
        return re.sub(r"^(aliases:\s*\n(?:  - .*\n?)*)", lambda m: m.group(1).rstrip("\n") + "\n" + line + "\n", fm, count=1, flags=re.M).rstrip("\n")
    return fm + "\naliases:\n" + line


def organize(folder: Path) -> int:
    """Move every act-N.md under folder (flat or already filed) into folder/<year>/ and add its old address as an alias."""
    moved = 0
    for p in sorted(folder.rglob("act-[0-9]*.md")):
        text = p.read_text(encoding="utf-8")
        m = _FM.match(text)
        if not m:
            continue
        year = _year(m.group(1))
        fm = _with_alias(m.group(1), f"acts/{p.stem}")
        new = f"---\n{fm}\n---\n" + text[m.end():]
        dest = folder / year / p.name
        dest.parent.mkdir(exist_ok=True)
        if new != text or dest != p:
            dest.write_text(new, encoding="utf-8")
            if dest != p:
                p.unlink()
                moved += 1
    return moved


if __name__ == "__main__":
    import sys
    print(organize(Path(sys.argv[1])))
