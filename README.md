# bdlaw-scraper

Scrape the acts and ordinances of Bangladesh from the official Bangladesh Laws website, `bdlaws.minlaw.gov.bd`, into clean Markdown with YAML front matter.
The output works in Obsidian, in any static site generator, and as input for LLM tools: one file per act, stable links between acts, footnotes kept, and the **original clause numbering preserved**.

The 1,600+ acts are English and Bengali, from 1836 to today. This repository holds the code only; the text of the laws belongs to its publisher (see [Responsible use](#responsible-use)).

## What you get

For every act, `act-<N>.md`:

```markdown
---
title: "The White Phosphorus Matches Prohibition Act, 1913"
description: "An Act to prohibit the importation, manufacture and sale of matches ..."
tags: [Legal, Bangladesh]
source_url: http://bdlaws.minlaw.gov.bd/act-100.html
act_id: 100
year: 1913
enacted_at: 1913-03-07
scraped_at: 2026-11-19
language: en
sections: [ { id: 24478, slug: short-title..., url: http://bdlaws.minlaw.gov.bd/act-100/section-24478.html }, ... ]
---

##### [Section 1: Short title](http://bdlaws.minlaw.gov.bd/act-100/section-24478.html) ^sec-1

1. <!--n:(1)--> This Act may be called the [[act-100\|White Phosphorus Matches Prohibition Act, 1913]].
2. <!--n:(2)--> It extends to the whole of [^1]Bangladesh.

## Footnotes

[^1]: The word "Bangladesh" was substituted, for the word "Pakistan" by ...
```

- **Headings** for parts, chapters and sections; each section heading links to the official page and carries a block id (`^sec-1`) so other acts can link to it: `[[act-100#^sec-6|section 6]]`.
- **Links between acts** are Obsidian wikilinks with an escaped pipe, `[[act-N\|Title]]`, which also survive Markdown tables.
- **Footnotes** are real Markdown footnotes (`[^1]`), including amendment and repeal notes.
- **Callouts** for Explanation, Exception, Illustrations and definitions.
- **`act-index.md`**: a table of every act with its number, year, title and description.

### Original numbering

Markdown cannot show `(ক)`, `(১)`, `(a)` or `(iv)` as list markers, so most converters turn legal numbering into `1. 2. 3.` and lose it. This scraper keeps the real marker next to each item in an invisible comment
(`1. <!--n:(ক)--> text`). Obsidian and other Markdown tools ignore the comment; a renderer can read it and show the real marker. See [docs/numbering.md](docs/numbering.md).
Every act also gets a sidecar file, `.numbering/act-<N>.json`, listing each item's original marker, the number it was rendered with, and its depth.

## Install

Python 3.10 or newer.

```
git clone https://github.com/darthdemono/bdlaw-scraper
cd bdlaw-scraper
python -m venv .venv && . .venv/bin/activate
pip install -e .
```

## Use

```
bdlaw-scraper --act 1710                          # one act, to act-1710.md
bdlaw-scraper --all --output-dir acts --workers 8 # every act, resumes if interrupted
bdlaw-scraper --all --from 500 --to 600           # a range
bdlaw-scraper --all --force-refresh               # ignore the HTML cache
```

Raw pages are cached in `cache/`, so a second run (for example after changing the converter) needs no network and no extra load on the source site.
`--all` writes `act-index.md` and keeps a `.progress.json` checkpoint; `--skip-existing` resumes. About 110 act numbers in the 1 to 1719 range do not exist; they are reported as failed and are expected.

Other tools:

- `bdlaw-dictionaries`: the Law Dictionary, Latin Terms and Glossary from `clcbd.org`, one Markdown file per collection.
- `bdlaw-translate` (optional, `pip install -e ".[translate]"`): translate a Bengali act to English with NLLB-200 running locally via CTranslate2. Needs a few GB of disk and runs on CPU or GPU.
- `bdlaw-pdf2md`: reformat the output of `marker_single` for a PDF of rules into the same Markdown conventions. Needs `marker` installed separately.

## Responsible use

- The scraper rate-limits itself (`--delay`, default 0.3 s per worker), retries with back-off, and caches every page. Use few workers and keep the cache.
- The laws and their official translations are published by the Government of Bangladesh; `clcbd.org` content belongs to its owners. Check the terms of each site before republishing anything you download.
- A scraped copy is a snapshot. It can be out of date, incomplete or wrong, and it is not legal advice. Link back to the official page; every act records it in `source_url`.

## Development

```
pip install -e ".[dev]"
pytest
```

`src/bdlaw_scraper/`: `scraper.py` (fetch, parse, convert, write), `http.py` (rate-limited cached session), `dictionaries.py`, `translate.py`, `pdf2md.py`. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Licence

Apache-2.0, see [LICENSE](LICENSE) and [NOTICE](NOTICE).
