# Contributing

Bug fixes, tests, docs and parsers for more official sources are welcome. Open an issue first for anything bigger than a fix.
Security problems are not issues: see [SECURITY.md](SECURITY.md). Be kind: see the [Code of Conduct](CODE_OF_CONDUCT.md).

By contributing you agree your work is licensed under Apache-2.0 and you certify the [Developer Certificate of Origin 1.1](https://developercertificate.org/).

## Setup

```
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## Guidelines

- A converter change needs a test in `tests/`; a bug fix needs a test that fails without it.
- Do not commit scraped data, caches or PDFs. Test fixtures must be small and made up or trimmed.
- Keep the scraper polite: do not raise the default request rate.
- One plain sentence per commit message; group related files into one commit.
