# Changelog

## Unreleased

### Added

- Original clause numbering is kept: each list item carries its marker in a `<!--n:(a)-->` comment, and every act gets a `.numbering/act-N.json` sidecar file.
- Packaged as `bdlaw-scraper` with the commands `bdlaw-scraper`, `bdlaw-dictionaries`, `bdlaw-translate` and `bdlaw-pdf2md`.

### Fixed

- Acts with an Explanation callout followed by an Illustration or Exception failed with `NameError: _RE_SECONDARY_HDR` (121 acts).

### Removed

- The Pinecone upload scripts and the `.env` handling that only they used.
