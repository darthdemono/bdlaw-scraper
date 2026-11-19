# Original list numbering

The official acts use clause markers such as `(1)`, `(a)`, `(iv)`, `(১)`, `(ক)` and `(অ)`. Converting to Markdown ordered lists loses them, because Markdown only has `1. 2. 3.`.

## What the scraper writes

Each list item is written as a normal Markdown ordered list item. The original marker is kept in an HTML comment directly after the list marker:

```
1. <!--n:(১)--> প্রথম দফা
   1. <!--n:(ক)--> উপ-দফা
   2. <!--n:(খ)--> উপ-দফা
2. <!--n:(২)--> দ্বিতীয় দফা
```

- Nesting is the Markdown nesting (three spaces per level), so every Markdown tool still understands the structure.
- Obsidian, GitHub and other renderers hide the comment and show `1. 2.`.
- A renderer that wants the legal numbering reads `<!--n:...-->` and prints the marker instead of the counter.

Depth rules: decimal and Bengali consonant markers are level 1, letters and Bengali digits level 2, roman numerals and Bengali vowels level 3. The first marker type in a section decides which of the two Bengali families is the top level.

## Sidecar file

`.numbering/act-<N>.json` next to the Markdown files:

```json
{ "act": 1106, "items": [ { "n": 1, "marker": "(১)", "rendered": "1.", "depth": 1, "type": "bn_decimal" } ] }
```

`n` is the item's position in the act, `rendered` the counter used in the Markdown, `type` one of `decimal`, `alpha`, `roman`, `bn_decimal`, `bn_alpha`, `bn_vowel`.
It is an audit trail: it records every place the Markdown counter differs from the official marker.
