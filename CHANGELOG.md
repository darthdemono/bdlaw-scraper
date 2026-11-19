# Changelog

## Unreleased

### Added

- Original clause numbering is kept: each list item carries its marker in a `<!--n:(a)-->` comment, and every act gets a `.numbering/act-N.json` sidecar file.
- Packaged as `bdlaw-scraper` with the commands `bdlaw-scraper`, `bdlaw-dictionaries`, `bdlaw-translate` and `bdlaw-pdf2md`.

### Fixed

- Cross-references to several sections at once ("sections 64, 65, 66 and 445") only linked the first number. Every known section in the list is now linked; a quantity after "and" ("sections 3 and 4 years") is left alone.
- Bengali references: "ধারা ৫(২)(ক)" and "ধারা ৫ এর উপ-ধারা (২) এর দফা (ক)" are one link to the section, number-first forms ("৫ ও ৬ ধারা") link their numbers, and "বিধি" and "প্রবিধান" count like "ধারা". "উপ-ধারা" and "উপধারা" are never taken for a section. English "section 5(2)(a)" links the same way.
- The preamble keeps the links to the acts it names and its sentences are no longer split at each link, and section references inside it are linked.
- Bengali text: a danda typed as "৷" (U+09F7) is written as "।", which also stops "২৪ক৷" turning into "২৪কি". Legacy-font artefacts ("পর্যন্ত্ম", "ত্মেগত্রে") are repaired, word-final "ত্" becomes "ৎ", and spacing around , ; : । is fixed.
- Bengali "ব্যাখ্যা" and "ব্যতিক্রম" become Explanation and Exception callouts (not inside a quoted amendment), and Bengali punishment phrases are bold as in English.
- "Firstly: ...", "Secondly: ..." paragraphs are lists too, and the list that follows an item (for example (1) and (2) under "Fourthly") nests under it. An inline "(a)" in the item opens that sub-list.
- "First.- ...", "Second. ..." paragraphs (section 21 of the Penal Code and similar) are now real lists with the word as the marker.
- Illustration lists inside callouts carried the section's running number ("5.") and rendered as plain text. Each callout list now starts at 1.
- Headings that link nowhere and hold a footnote reference showed as literal "[...](#)" (328 headings in 117 acts). They are plain headings now, and amendment brackets inside a title are dropped.
- A preamble ending in a lone dash turned its text into a heading.
- Footnote references were lost in many places, leaving about 600 notes without a reference: Bengali-digit brackets were read as references, a footnote number in front of a bracketed word in a heading ("Prohibited 18 [entity]") or glued to a chapter name was dropped, the preamble lost its references, and a closer before an adjacent reference ("335[1995]") ate the first reference's bracket. Notes now keep their references. A bracketed year with no matching note stays plain text.
- Editorial brackets that span several lines or sit open in front of a wikilink ("[[[act-354|..]]") are removed, and an amendment bracket around a sub-chapter heading no longer becomes a broken wikilink.
- Footnote numbers are de-duplicated, nested "[28[29]" references split into two, block ids are unique, and a "[Omitted](c)" inside a heading is escaped so it is not read as a link.
- Section lists in other forms: Bengali section numbers, a reference to another act is left unlinked, double-letter and suffix list markers ("(aa)", "(2A)"), and table rows are no longer split by list conversion.
- Frontmatter values are quoted as JSON and links to acts that do not exist point to the official site.
- A list that starts with a deep marker such as roman (i) straight after a paragraph was indented 6 spaces and shown as a code block (138 items in 92 acts). Items now start at the top level and nest only one level at a time.
- Acts with an Explanation callout followed by an Illustration or Exception failed with `NameError: _RE_SECONDARY_HDR` (121 acts).

### Removed

- The Pinecone upload scripts and the `.env` handling that only they used.
