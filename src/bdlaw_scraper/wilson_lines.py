"""
Read every printed line of Wilson's glossary from the page images with surya.

The scan's own text layer loses every accent (á is read as d) and turns each native-script word
into symbols. Reading whole lines from the images gives both back at once: accented Latin
(Ajná, ÁJNÁ) and the Arabic, Devanagari, Bengali, Telugu ... words in place, in Unicode.
The text layer is still used for what it does well: where each line is on the page, which
column it is in, and where each entry starts.

Usage:
  bdlaw-wilson-lines --pdf book.pdf --output lines.jsonl
Rerunning resumes: lines already in the output are skipped.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import click

from bdlaw_scraper.wilson import entry_page, merge_baselines, read_pages

PT_TO_PX = 300 / 72
PAD = (3, 6, 3, 3)  # left, top, right, bottom in points: native glyphs stand taller than the box the text layer gives them


def line_key(page: int, column: int, y: float) -> str:
    """Stable id of a printed line: page, column and top position."""
    return f"{page}:{column}:{y:.1f}"


def line_boxes(pdf: Path) -> dict[int, list[tuple[str, tuple[float, float, float, float], str]]]:
    """For every entry page: (key, box in PDF points, text-layer text) of each body line, in reading order."""
    out: dict[int, list] = {}
    for pno, (_w, h, lines) in enumerate(read_pages(pdf), 1):
        merged = merge_baselines(lines)
        if not entry_page(merged):
            continue
        rows = []
        for l in merged:
            if not l.words or l.y < 72 or l.y > h - 60 or l.text.isdigit():
                continue
            x0, y0 = min(w[1] for w in l.words), min(w[2] for w in l.words)
            x1, y1 = max(w[3] for w in l.words), max(w[4] for w in l.words)
            rows.append((line_key(pno, l.column, l.y), (x0 - PAD[0], y0 - PAD[1], x1 + PAD[2], y1 + PAD[3]), l.text))
        out[pno] = rows
    return out


@click.command()
@click.option("--pdf", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--output", required=True, type=click.Path(), help="JSON lines: {key, text}.")
@click.option("--limit", default=0, help="Stop after this many lines (0 = all), for trial runs.")
@click.option("--first-page", default=0, help="Skip pages before this one.")
@click.option("--last-page", default=10**6, help="Skip pages after this one (the index starts near 653).")
@click.option("--batch", default=32, help="Lines per surya call.")
@click.option("--scale", default=1.4, help="Enlarge each crop by this factor before reading.")
def main(pdf: str, output: str, limit: int, first_page: int, last_page: int, batch: int, scale: float) -> None:
    """Read every body line of the glossary from the page images."""
    from PIL import Image
    from surya.foundation import FoundationPredictor
    from surya.recognition import RecognitionPredictor

    boxes = line_boxes(Path(pdf))
    out = Path(output)
    done = {json.loads(l)["key"] for l in out.read_text(encoding="utf-8").splitlines()} if out.exists() else set()
    pages = [(p, [r for r in rows if r[0] not in done]) for p, rows in sorted(boxes.items()) if first_page <= p <= last_page]
    pages = [(p, rows) for p, rows in pages if rows]
    rec = RecognitionPredictor(FoundationPredictor())
    count = 0

    with tempfile.TemporaryDirectory() as t, out.open("a", encoding="utf-8") as fh:
        tmp = Path(t)

        def render(page: int) -> "Image.Image":
            base = tmp / f"pg{page}"
            subprocess.run(["pdftoppm", "-r", "300", "-f", str(page), "-l", str(page), "-png", "-singlefile", pdf, str(base)], check=True)
            im = Image.open(f"{base}.png").convert("RGB")
            Path(f"{base}.png").unlink()
            return im

        def flush(keys: list[str], crops: list) -> None:
            res = rec(crops, bboxes=[[[0, 0, c.width, c.height]] for c in crops], recognition_batch_size=batch)
            for key, r in zip(keys, res):
                fh.write(json.dumps({"key": key, "text": " ".join(x.text for x in r.text_lines).strip()}, ensure_ascii=False) + "\n")
            fh.flush()

        keys: list[str] = []
        crops: list = []
        with ThreadPoolExecutor(2) as renderer:
            ahead = [renderer.submit(render, p) for p, _ in pages[:2]]
            for n, (page, rows) in enumerate(pages, 1):
                im, ahead[n - 1] = ahead[n - 1].result(), None
                if n + 1 < len(pages):
                    ahead.append(renderer.submit(render, pages[n + 1][0]))
                for key, (x0, y0, x1, y1), _text in rows:
                    c = im.crop((int(x0 * PT_TO_PX), int(y0 * PT_TO_PX), int(x1 * PT_TO_PX), int(y1 * PT_TO_PX)))
                    keys.append(key)
                    crops.append(c.resize((int(c.width * scale), int(c.height * scale))))
                    count += 1
                if len(keys) >= batch * 2:
                    flush(keys, crops)
                    keys, crops = [], []
                if n % 10 == 0:
                    click.echo(f"page {page} ({n}/{len(pages)}), {count} lines")
                if limit and count >= limit:
                    break
        if keys:
            flush(keys, crops)
    click.echo(f"done: {count} lines -> {output}")


if __name__ == "__main__":
    main()
