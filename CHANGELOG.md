# Changelog

## Unreleased

### Added

- Original clause numbering is kept: each list item carries its marker in a `<!--n:(a)-->` comment, and every act gets a `.numbering/act-N.json` sidecar file.
- Packaged as `bdlaw-scraper` with the commands `bdlaw-scraper`, `bdlaw-dictionaries`, `bdlaw-translate` and `bdlaw-pdf2md`.

### Fixed

- A list that starts with a deep marker such as roman (i) straight after a paragraph was indented 6 spaces and shown as a code block (138 items in 92 acts). Items now start at the top level and nest only one level at a time.
- Acts with an Explanation callout followed by an Illustration or Exception failed with `NameError: _RE_SECONDARY_HDR` (121 acts).

### Removed

- The Pinecone upload scripts and the `.env` handling that only they used.
