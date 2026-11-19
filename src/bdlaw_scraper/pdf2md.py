#!/usr/bin/env python3
"""
bdlaw_marker2md.py — marker_single → bdlaw-style Markdown
===========================================================
Runs marker_single on a PDF, then reformats the output into the
structured Markdown format matching bdlaw_scraper.py conventions.

Usage:
  python bdlaw_marker2md.py Chapter-I.pdf
  python bdlaw_marker2md.py Chapter-I.pdf -o output.md --title "HCD Rules 1973"
  python bdlaw_marker2md.py --no-run existing.md   # reformat already-converted .md
"""

from __future__ import annotations

import re
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Optional

import click

# ---------------------------------------------------------------------------
# Bengali constants
# ---------------------------------------------------------------------------
_BN_CONS = "কখগঘঙচছজঝঞটঠডঢণতথদধনপফবভমযরলশষসহড়ঢ়য়ৎ"

# ---------------------------------------------------------------------------
# Markup strippers
# ---------------------------------------------------------------------------


def strip_markup(s: str) -> str:
    """Remove <sup>, bold **, leading [, trailing ], trailing '."""
    s = re.sub(r"<sup>[^<]*</sup>", "", s)
    s = re.sub(r"\*{1,2}([^*]+)\*{1,2}", r"\1", s)
    s = re.sub(r"^\[+", "", s)  # leading [[ or [
    s = re.sub(r"[\]'\"]\s*$", "", s)  # trailing ] ' "
    return unicodedata.normalize("NFC", s).strip()


def strip_bullet(line: str) -> Optional[str]:
    """Remove leading '- ' or '* ' at any indent. Returns content or None."""
    m = re.match(r"^(\s*)[-*]\s+(.*)", line, re.DOTALL)
    return m.group(2) if m else None


def bullet_indent(line: str) -> int:
    """Return indent level (spaces) of a bullet line."""
    m = re.match(r"^(\s*)[-*]", line)
    return len(m.group(1)) if m else 0


# ---------------------------------------------------------------------------
# Section line detector
# ---------------------------------------------------------------------------

# After stripping bullet + markup, section content looks like:
#   "1. Citation.- These Rules..."
#   "2. Construction of references.- (1) Unless..."
#   "3. Bangla reference to rule etc.-Where..."
#   "4. Publication of authentic Bangla translation.-The..."
#   "1A. Short title.-"
RE_SEC_CONTENT = re.compile(
    r"^(\d+[A-Za-z]?)\.\s+"  # group 1: section number
    r"(.+?)"  # group 2: heading (non-greedy)
    r"\.-\s*"  # separator ".-"
    r"(.*)$",  # group 3: optional inline body
    re.DOTALL,
)


def parse_section_bullet(raw_line: str) -> Optional[tuple[str, str, str]]:
    """
    Try to parse a bullet line as a section start.
    Returns (number, heading, body_start) or None.
    """
    content = strip_bullet(raw_line)
    if content is None:
        return None
    content = strip_markup(content)
    # Must start with a digit
    if not content or not content[0].isdigit():
        return None
    m = RE_SEC_CONTENT.match(content)
    if not m:
        return None
    num = m.group(1)
    heading = m.group(2).strip().rstrip(".-").strip()
    body = m.group(3).strip()
    return num, heading, body


# ---------------------------------------------------------------------------
# Clause classifier
# ---------------------------------------------------------------------------

# Real roman numerals: only multi-char, OR single 'i'/'v'/'x'
_ROMAN_MULTI = re.compile(
    r"^(ii|iii|iv|vi|vii|viii|ix|xi|xii|xiii|xiv|xv|xx)$", re.IGNORECASE
)
_ROMAN_SINGLE = re.compile(r"^[ivx]$", re.IGNORECASE)
_BN_LABEL = re.compile(r"^[" + _BN_CONS + r"০-৯]+$")


def classify_label(label: str) -> str:
    """Return 'decimal'|'roman'|'alpha'|'bn'."""
    if label.isdigit() or re.fullmatch(r"[০-৯]+", label):
        return "decimal"
    if _BN_LABEL.match(label):
        return "bn"
    if _ROMAN_MULTI.match(label) or (len(label) == 1 and _ROMAN_SINGLE.match(label)):
        return "roman"
    return "alpha"


RE_CLAUSE_CONTENT = re.compile(
    r"^\(([A-Za-z\d" + _BN_CONS + r"০-৯]+)\)\s+(.*)", re.DOTALL
)


def parse_clause(content: str) -> Optional[tuple[str, str]]:
    """Try to parse '(a) text' → (label, text). Returns None if not a clause."""
    m = RE_CLAUSE_CONTENT.match(content.strip())
    return (m.group(1), m.group(2).strip()) if m else None


# ---------------------------------------------------------------------------
# List renderer
# ---------------------------------------------------------------------------


def render_clauses(items: list[tuple[str, str]], kind: str) -> str:
    if kind == "decimal":
        return "\n".join(f"{label}. {text}" for label, text in items)
    style = "lower-roman" if kind == "roman" else "lower-alpha"
    inner = "".join(f"<li>{text}</li>" for _, text in items)
    return f'<ol style="list-style-type: {style};">{inner}</ol>'


# ---------------------------------------------------------------------------
# Footnote detection
# ---------------------------------------------------------------------------

RE_FOOTNOTE_LINE = re.compile(
    r"^(Chapter\s+[IVX\d]|This citation|[A-Z].{10,}"
    r"(?:Notification|inserted|citation|published|continuity|renumbered))"
)

# ---------------------------------------------------------------------------
# Core reformatter
# ---------------------------------------------------------------------------


def reformat(raw_md: str, title: str = "", source: str = "") -> str:
    lines = raw_md.splitlines()
    out: list[str] = []

    if title:
        src = f" — {source}" if source else ""
        out.append(f"# {title}{src}\n")

    # ── State
    current_chapter: Optional[tuple[str, str]] = None  # (num, title)
    current_section_num: str = ""
    current_section_heading: str = ""
    section_body: list[str] = []  # rendered body items (strings)
    pending: list[tuple[str, str]] = []  # current clause group
    pending_kind: str = ""
    footnotes: list[str] = []

    def flush_pending():
        nonlocal pending, pending_kind
        if pending:
            section_body.append(render_clauses(pending, pending_kind))
        pending = []
        pending_kind = ""

    def flush_section():
        nonlocal current_section_num, current_section_heading, section_body
        flush_pending()
        if current_section_num:
            lvl = "######" if re.search(r"[A-Za-z]", current_section_num) else "#####"
            heading_text = f"Section {current_section_num}: {current_section_heading}"
            out.append(f"\n{lvl} {heading_text}\n")
            # Join plain-text body items into single lines; keep rendered lists as-is
            for part in section_body:
                out.append(part)
            out.append("")
        current_section_num = ""
        current_section_heading = ""
        section_body = []

    def flush_chapter():
        nonlocal current_chapter
        flush_section()
        if current_chapter:
            num, title_ch = current_chapter
            label = f"Chapter {num}"
            if title_ch:
                label += f": {title_ch}"
            out.append(f"\n### {label}\n")
        current_chapter = None

    def add_body_text(text: str):
        """Add a plain-text paragraph to the current section (single line)."""
        flush_pending()
        text = text.strip()
        if text:
            section_body.append(text)

    def add_clause(label: str, content: str):
        nonlocal pending, pending_kind
        kind = classify_label(label)
        if pending_kind and kind != pending_kind:
            flush_pending()
        pending_kind = kind
        pending.append((label, content))

    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        stripped = line.rstrip()

        # ── blank
        if not stripped.strip():
            i += 1
            continue

        # ── Markdown heading lines (##, ###)
        if stripped.startswith("#"):
            # Chapter?
            m = re.match(
                r"^#{1,3}\s+\d*\[?Chapter[-\s]([IVXivx\dA-Za-z]+)\]?", stripped, re.I
            )
            if m:
                flush_chapter()
                num = m.group(1).upper()
                # peek for title
                title_ch = ""
                j = i + 1
                while j < n and not lines[j].strip():
                    j += 1
                if j < n:
                    m2 = re.match(
                        r"^#{1,3}\s+([A-Z][A-Z\s\-&,]{3,})$", lines[j].rstrip()
                    )
                    if m2:
                        title_ch = m2.group(1).strip().title()
                        i = j
                current_chapter = (num, title_ch)
                i += 1
                continue

            # Part?
            m = re.match(
                r"^#{1,3}\s+Part\s+([IVXivx\d]+)[.\s]*[-–]?\s*(.*)?$", stripped, re.I
            )
            if m:
                flush_chapter()
                label = f"Part {m.group(1).upper()}"
                sub = (m.group(2) or "").strip(" -–")
                if sub:
                    label += f": {sub.title()}"
                out.append(f"\n## {label}\n")
                i += 1
                continue

            # ALL-CAPS title (chapter title not consumed, or standalone)
            m = re.match(r"^#{1,3}\s+([A-Z][A-Z\s\-&,]{3,})$", stripped)
            if m:
                text = m.group(1).strip()
                if current_chapter and not current_chapter[1]:
                    current_chapter = (current_chapter[0], text.title())
                else:
                    flush_section()
                    out.append(f"\n#### {text.title()}\n")
                i += 1
                continue

            # generic heading — skip or pass through
            i += 1
            continue

        # ── Bullet lines
        if re.match(r"^\s*[-*]\s", stripped):
            content_raw = strip_bullet(stripped)
            indent = bullet_indent(stripped)

            # Try section start (only at top indent, i.e. indent <= 2)
            if indent <= 2:
                sec = parse_section_bullet(stripped)
                if sec:
                    flush_section()
                    # Emit chapter heading now if pending
                    if current_chapter and not any(
                        "Chapter" in l for l in out[-5:] if l.strip()
                    ):
                        flush_chapter_heading()  # see below
                    num, heading, body_start = sec
                    current_section_num = num
                    current_section_heading = heading
                    if body_start:
                        # body_start may itself start with a clause "(1) ..."
                        clause = parse_clause(body_start)
                        if clause:
                            add_clause(*clause)
                        else:
                            add_body_text(body_start)
                    i += 1
                    continue

            # Try clause: "(a) text" or "(1) text" at any indent
            content = strip_markup(content_raw or "")
            clause = parse_clause(content)
            if clause and current_section_num:
                add_clause(*clause)
                i += 1
                continue

            # Plain bullet body text
            if content and current_section_num:
                # Could be a decimal sub-clause like "(2) Where..."
                cl2 = parse_clause(content)
                if cl2:
                    add_clause(*cl2)
                else:
                    if RE_FOOTNOTE_LINE.match(content):
                        footnotes.append(content)
                    else:
                        add_body_text(content)
                i += 1
                continue

            # Bullet before any section — could be footnote text
            if content and RE_FOOTNOTE_LINE.match(content):
                footnotes.append(content)
            i += 1
            continue

        # ── Plain text (no bullet, no heading)
        content = strip_markup(stripped)
        if not content:
            i += 1
            continue
        if RE_FOOTNOTE_LINE.match(content):
            footnotes.append(content)
        elif current_section_num:
            add_body_text(content)
        i += 1

    # ── Final flush
    # Emit chapter heading if it hasn't been emitted yet
    flush_section()
    if current_chapter:
        num, title_ch = current_chapter
        label = f"Chapter {num}"
        if title_ch:
            label += f": {title_ch}"
        out.append(f"\n### {label}\n")

    if footnotes:
        out.append("\n## Footnotes\n")
        for fn in footnotes:
            out.append(f"— {fn}")
        out.append("")

    md = "\n".join(out)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip() + "\n"


# The chapter heading needs to be emitted BEFORE the first section.
# Patch: emit chapter in flush_chapter, but call it lazily.
# We do this by restructuring flush_chapter to be called at section start.
# Simpler: just sort out by doing a two-pass approach.
# Actually let's just reorder output in post-processing.


def reformat_final(raw_md: str, title: str = "", source: str = "") -> str:
    """
    Two-pass: first collect all nodes, then render in correct order.
    """
    lines = raw_md.splitlines()

    # ── Node types
    nodes: list[dict] = []

    current_chapter: Optional[tuple[str, str]] = None
    current_section: Optional[dict] = None
    pending: list[tuple[str, str]] = []
    pending_kind: str = ""
    footnotes: list[str] = []

    def flush_pending_clauses():
        nonlocal pending, pending_kind
        if pending and current_section is not None:
            current_section["body"].append(render_clauses(pending, pending_kind))
        pending = []
        pending_kind = ""

    def flush_section():
        nonlocal current_section
        flush_pending_clauses()
        if current_section:
            nodes.append(current_section)
        current_section = None

    def flush_chapter():
        nonlocal current_chapter
        flush_section()
        if current_chapter:
            nodes.append(
                {
                    "type": "chapter",
                    "num": current_chapter[0],
                    "title": current_chapter[1],
                }
            )
        current_chapter = None

    def new_section(num: str, heading: str):
        nonlocal current_section
        # Emit chapter node before first section
        nonlocal current_chapter
        if current_chapter:
            flush_section()
            nodes.append(
                {
                    "type": "chapter",
                    "num": current_chapter[0],
                    "title": current_chapter[1],
                }
            )
            current_chapter = None
        else:
            flush_section()
        current_section = {
            "type": "section",
            "num": num,
            "heading": heading,
            "body": [],
        }

    def add_clause_node(label: str, content: str):
        nonlocal pending, pending_kind
        kind = classify_label(label)
        if pending_kind and kind != pending_kind:
            flush_pending_clauses()
        pending_kind = kind
        pending.append((label, content))

    def add_body(text: str):
        flush_pending_clauses()
        if current_section is not None and text.strip():
            current_section["body"].append(text.strip())

    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        stripped = line.rstrip()

        if not stripped.strip():
            i += 1
            continue

        # Heading lines
        if stripped.startswith("#"):
            # Chapter
            m = re.match(
                r"^#{1,3}\s+\d*\[?Chapter[-\s]([IVXivx\dA-Za-z]+)\]?", stripped, re.I
            )
            if m:
                flush_chapter()
                num = m.group(1).upper()
                title_ch = ""
                j = i + 1
                while j < n and not lines[j].strip():
                    j += 1
                if j < n:
                    m2 = re.match(
                        r"^#{1,3}\s+([A-Z][A-Z\s\-&,]{3,})$", lines[j].rstrip()
                    )
                    if m2:
                        title_ch = m2.group(1).strip().title()
                        i = j
                current_chapter = (num, title_ch)
                i += 1
                continue

            # Part
            m = re.match(
                r"^#{1,3}\s+Part\s+([IVXivx\d]+)[.\s]*[-–]?\s*(.*)?$", stripped, re.I
            )
            if m:
                flush_chapter()
                nodes.append(
                    {
                        "type": "part",
                        "num": m.group(1).upper(),
                        "title": (m.group(2) or "").strip(" -–"),
                    }
                )
                i += 1
                continue

            # ALL-CAPS title
            m = re.match(r"^#{1,3}\s+([A-Z][A-Z\s\-&,]{3,})$", stripped)
            if m:
                text = m.group(1).strip()
                if current_chapter and not current_chapter[1]:
                    current_chapter = (current_chapter[0], text.title())
                i += 1
                continue

            i += 1
            continue

        # Bullet lines
        if re.match(r"^\s*[-*]\s", stripped):
            content_raw = strip_bullet(stripped) or ""
            indent = bullet_indent(stripped)

            # Section start (top-level bullet, starts with digit)
            if indent <= 2:
                sec = parse_section_bullet(stripped)
                if sec:
                    num, heading, body_start = sec
                    new_section(num, heading)
                    if body_start:
                        cl = parse_clause(body_start)
                        if cl:
                            add_clause_node(*cl)
                        else:
                            add_body(body_start)
                    i += 1
                    continue

            # Clause or body
            content = strip_markup(content_raw)
            cl = parse_clause(content)
            if cl and current_section:
                add_clause_node(*cl)
            elif content:
                if RE_FOOTNOTE_LINE.match(content):
                    footnotes.append(content)
                elif current_section:
                    add_body(content)
            i += 1
            continue

        # Plain text
        content = strip_markup(stripped)
        if not content:
            i += 1
            continue
        if RE_FOOTNOTE_LINE.match(content):
            footnotes.append(content)
        elif current_section:
            add_body(content)
        i += 1

    flush_chapter()

    # ── Render
    out: list[str] = []
    if title:
        src = f" — {source}" if source else ""
        out.append(f"# {title}{src}\n")

    for node in nodes:
        if node["type"] == "part":
            label = f"Part {node['num']}"
            if node["title"]:
                label += f": {node['title'].title()}"
            out.append(f"\n## {label}\n")

        elif node["type"] == "chapter":
            label = f"Chapter {node['num']}"
            if node["title"]:
                label += f": {node['title']}"
            out.append(f"\n### {label}\n")

        elif node["type"] == "section":
            num = node["num"]
            lvl = "######" if re.search(r"[A-Za-z]", num) else "#####"
            out.append(f"\n{lvl} Section {num}: {node['heading']}\n")
            for part in node["body"]:
                out.append(part)
            out.append("")

    if footnotes:
        out.append("\n## Footnotes\n")
        for fn in footnotes:
            out.append(f"— {fn}")
        out.append("")

    md = "\n".join(out)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip() + "\n"


# ---------------------------------------------------------------------------
# marker_single runner
# ---------------------------------------------------------------------------


def run_marker(pdf_path: Path) -> Path:
    click.echo(f"Running marker_single on {pdf_path.name} …")
    result = subprocess.run(
        ["marker_single", str(pdf_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"marker_single failed:\n{result.stderr}")

    for line in (result.stdout + result.stderr).splitlines():
        m = re.search(r"Saved markdown to (.+)", line)
        if m:
            out_dir = Path(m.group(1).strip())
            mds = list(out_dir.glob("*.md"))
            if mds:
                return mds[0]

    import glob as _glob

    stem = pdf_path.stem
    for pattern in [
        f"conversion_results/{stem}/{stem}.md",
        f"/home/*/.local/lib/python*/site-packages/conversion_results/{stem}/{stem}.md",
        f"{Path.home()}/.local/lib/python*/site-packages/conversion_results/{stem}/{stem}.md",
    ]:
        found = _glob.glob(pattern)
        if found:
            return Path(found[0])

    raise RuntimeError(
        "Could not locate marker output .md file. "
        "Run marker_single manually then use --no-run <file.md>"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.command()
@click.argument("input_path", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "-o", "--output", default=None, help="Output .md path. Defaults to <input>.md"
)
@click.option("--title", "-t", default="", help="Document title for the H1 heading.")
@click.option("--source", "-s", default="", help="Source note appended after title.")
@click.option(
    "--no-run",
    is_flag=True,
    default=False,
    help="INPUT_PATH is an already-converted marker .md file; skip marker_single.",
)
def main(
    input_path: str, output: Optional[str], title: str, source: str, no_run: bool
) -> None:
    """Convert a Bangladesh legal PDF via marker_single → bdlaw-style Markdown."""
    inp = Path(input_path)

    if no_run:
        raw_md_path = inp
    else:
        try:
            raw_md_path = run_marker(inp)
            click.echo(f"marker output: {raw_md_path}")
        except RuntimeError as e:
            click.echo(str(e), err=True)
            sys.exit(1)

    raw_md = raw_md_path.read_text(encoding="utf-8")
    doc_title = title or inp.stem.replace("_", " ").replace("-", " ").title()

    click.echo("Reformatting …")
    formatted = reformat_final(raw_md, title=doc_title, source=source)

    out_path = Path(output) if output else inp.with_suffix(".md")
    out_path.write_text(formatted, encoding="utf-8")
    click.echo(f"✅  Saved → {out_path}  ({len(formatted):,} chars)")


if __name__ == "__main__":
    main()
