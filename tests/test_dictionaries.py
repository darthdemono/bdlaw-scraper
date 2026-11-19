from pathlib import Path

from bdlaw_scraper.dictionaries import (
    COLLECTIONS,
    Entry,
    extract_references,
    read_table_entries,
    tidy_text,
    write_markdown,
)


def refs(text):
    notes = []
    return extract_references(text, notes), notes


def test_bracket_source_becomes_footnote():
    out, notes = refs("A gift. [Wilson’s Glossary] More text. [Mitra’s Dictionary].")
    assert out == "A gift.[^1] More text.[^2]"
    assert notes == ["Wilson’s Glossary", "Mitra’s Dictionary"]


def test_case_citation_in_parentheses_is_moved_with_inner_number():
    out, notes = refs("Fixed by contract. ( Abdul Vs. Rahim, 1997, 26 CLC (AD) [491] ) Next.")
    assert out == "Fixed by contract.[^1] Next."
    assert notes == ["Abdul Vs. Rahim, 1997, 26 CLC (AD) [491]"]


def test_plain_parentheses_numbers_and_status_notes_stay():
    text = "Held (a) and (b) as in [5422] and [Now stands repealed]"
    out, notes = refs(text)
    assert out == text and notes == []


def test_numbering_continues_across_calls():
    notes = []
    extract_references("a [X]", notes)
    assert extract_references("b [Y]", notes) == "b[^2]"


def test_tidy_text_spacing():
    assert tidy_text("Means,to  settle ( x ) .") == "Means, to settle (x)."


def test_write_markdown_sorts_and_lists_footnotes(tmp_path: Path):
    entries = [Entry(1, "Beta", "Two. [Src B]"), Entry(2, "alpha", "One. [Src A]"), Entry(3, "'Zed", "Z.")]
    out = tmp_path / "g.md"
    write_markdown(entries, COLLECTIONS["glossary"], out)
    text = out.read_text()
    assert text.index("## A") < text.index("## B") < text.index("## Z")
    assert "| **alpha** | One.[^1] |" in text
    assert text.rstrip().endswith("[^2]: Src B")
    assert read_table_entries(out)[0].term == "**alpha**"
