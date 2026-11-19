"""
Recognise the native-script words of Wilson's glossary from page crops.

`bdlaw-wilson` records the page and box of every native-script group. This pass crops
each box from the page image and reads it with surya (all scripts, no language hint) and,
when the result is in the wrong script for the entry's language code, with tesseract
using the matching language pack. Only characters of the expected script are kept, so
surya's stray parentheses and English glosses drop out. A word with no result stays
`unresolved`; nothing is guessed.

Usage:
  bdlaw-wilson-native --pdf book.pdf --entries wilson.json --output wilson-native.jsonl
Rerunning resumes: finished words in the output file are skipped.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import click

# Unicode ranges per script, and the tesseract language pack for each
SCRIPTS = {
    "arabic": ("؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿", "urd+ara+fas"),
    "devanagari": ("ऀ-ॿ", "hin+san+mar"),
    "bengali": ("ঀ-৿", "ben"),
    "gujarati": ("઀-૿", "guj"),
    "oriya": ("଀-୿", "ori"),
    "tamil": ("஀-௿", "tam"),
    "telugu": ("ఀ-౿", "tel"),
    "kannada": ("ಀ-೿", "kan"),
    "malayalam": ("ഀ-ൿ", "mal"),
}
# language code printed in the book -> scripts it can be written in (H is Urdu or Devanagari)
CODE_SCRIPTS = {
    "A": ["arabic"], "P": ["arabic"], "H": ["arabic", "devanagari"], "S": ["devanagari"], "Hind": ["devanagari", "arabic"],
    "Mar": ["devanagari"], "Mahr": ["devanagari"], "Beng": ["bengali"], "Ben": ["bengali"], "Tel": ["telugu"],
    "Tam": ["tamil"], "Karn": ["kannada"], "Kara": ["kannada"], "Guz": ["gujarati"], "Uriya": ["oriya"],
    "Mal": ["malayalam"], "Sindhi": ["arabic"], "Pali": ["devanagari"],
}
PAD_TOP_PT, PAD_SIDE_PT = 8, 2  # the OCR box sits lower than the native glyphs, which are taller than Latin


def script_text(text: str, scripts: list[str]) -> str:
    """The runs of `text` written in the given scripts, joined by spaces."""
    ranges = "".join(SCRIPTS[s][0] for s in scripts)
    return " ".join(re.findall(rf"[{ranges}]+(?:[ ,]+[{ranges}]+)*", text)).strip()


def allowed_scripts(lang: str) -> list[str]:
    return CODE_SCRIPTS.get(lang, list(SCRIPTS))  # unknown code: accept any script


def tesseract(image_path: str, scripts: list[str]) -> str:
    out = subprocess.run(
        ["tesseract", image_path, "-", "-l", "+".join(SCRIPTS[s][1] for s in scripts), "--psm", "7"],
        capture_output=True, text=True,
    ).stdout
    return script_text(out, scripts)


def jobs(entries: list[dict]):
    """(entry index, native index, page, lang, boxes) for every native word that has a box."""
    for i, e in enumerate(entries):
        for j, n in enumerate(e["native"]):
            if n.get("boxes"):
                yield i, j, n["page"], n["lang"], n["boxes"]


@click.command()
@click.option("--pdf", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--entries", required=True, type=click.Path(exists=True, dir_okay=False), help="JSON written by bdlaw-wilson.")
@click.option("--output", required=True, type=click.Path(), help="JSON lines, one per native word.")
@click.option("--limit", default=0, help="Stop after this many words (0 = all), for trial runs.")
@click.option("--batch", default=24, help="Crops per surya call.")
def main(pdf: str, entries: str, output: str, limit: int, batch: int) -> None:
    """Read the native-script words of Wilson's glossary from page crops."""
    from PIL import Image
    from surya.foundation import FoundationPredictor
    from surya.recognition import RecognitionPredictor

    data = json.loads(Path(entries).read_text(encoding="utf-8"))
    out = Path(output)
    done = {tuple(json.loads(l)["key"]) for l in out.read_text().splitlines()} if out.exists() else set()
    todo = [j for j in jobs(data) if (j[0], j[1]) not in done]
    if limit:
        todo = todo[:limit]
    rec = RecognitionPredictor(FoundationPredictor())
    k = 300 / 72
    by_page: dict[int, list] = {}
    for j in todo:
        by_page.setdefault(j[2], []).append(j)

    def flush(pending: list, images: list, tmp: Path, fh) -> None:
        results = rec(images, bboxes=[[[0, 0, im.width, im.height]] for im in images], recognition_batch_size=16)  # each crop is one line: no detection pass if images else []
        grouped: dict[tuple, dict] = {}
        texts = [script_text(" ".join(l.text for l in res.text_lines), allowed_scripts(lang)) for (_k, lang, _i), res in zip(pending, results)]
        retry = [n for n, t in enumerate(texts) if not t]  # wrong or no script from surya: tesseract, four at a time
        for n in retry:
            images[n].save(tmp / f"{n}.png")
        with ThreadPoolExecutor(4) as pool:
            second = list(pool.map(lambda n: tesseract(str(tmp / f"{n}.png"), allowed_scripts(pending[n][1])), retry))
        for n, t in zip(retry, second):
            texts[n] = t
        for n, ((key, lang, idx), text) in enumerate(zip(pending, texts)):
            g = grouped.setdefault(key, {"key": list(key), "lang": lang, "parts": []})
            g["parts"].append({"text": text, "engine": ("tesseract" if n in retry else "surya") if text else None})
        for g in grouped.values():
            texts = [p["text"] for p in g["parts"] if p["text"]]
            g["text"] = " ".join(texts)
            g["status"] = "ok" if texts and len(texts) == len(g["parts"]) else "unresolved"
            g["engine"] = sorted({p["engine"] for p in g["parts"] if p["engine"]})
            fh.write(json.dumps(g, ensure_ascii=False) + "\n")
        fh.flush()

    with tempfile.TemporaryDirectory() as t, out.open("a", encoding="utf-8") as fh:
        tmp = Path(t)
        def render(page: int) -> "Image.Image":
            base = tmp / f"pg{page}"
            subprocess.run(["pdftoppm", "-r", "300", "-f", str(page), "-l", str(page), "-png", "-singlefile", pdf, str(base)], check=True)
            im = Image.open(f"{base}.png").convert("RGB")
            Path(f"{base}.png").unlink()
            return im

        pending, images = [], []
        pages = sorted(by_page.items())
        with ThreadPoolExecutor(2) as renderer:  # render the next pages while the GPU reads this one
            ahead = [renderer.submit(render, pg) for pg, _ in pages[:2]]
            for n_page, (page, group) in enumerate(pages, 1):
                im, ahead[n_page - 1] = ahead[n_page - 1].result(), None  # drop the future so old pages are freed
                if n_page + 1 < len(pages):
                    ahead.append(renderer.submit(render, pages[n_page + 1][0]))
                for i, j, _p, lang, boxes in group:
                    for b, (x0, y0, x1, y1) in enumerate(boxes):
                        c = im.crop((int((x0 - PAD_SIDE_PT) * k), int((y0 - PAD_TOP_PT) * k), int((x1 + PAD_SIDE_PT) * k), int((y1 + 1) * k)))
                        pending.append(((i, j), lang, b))
                        images.append(c.resize((c.width * 2, c.height * 2)))
                if len(images) >= batch:
                    flush(pending, images, tmp, fh)
                    pending, images = [], []
                if n_page % 20 == 0:
                    click.echo(f"page {page} ({n_page}/{len(by_page)})")
        flush(pending, images, tmp, fh)
    click.echo(f"done -> {output}")


if __name__ == "__main__":
    main()
