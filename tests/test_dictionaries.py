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
    assert "\n## Footnotes\n\n[^1]: Src A\n[^2]: Src B" in text
    assert read_table_entries(out)[0].term == "**alpha**"


def test_merge_duplicates_keeps_different_definitions():
    from bdlaw_scraper.dictionaries import merge_duplicates

    out = merge_duplicates(
        [
            Entry(1, "Fact", "A fact is a thing."),
            Entry(2, "Fact:", "A fact is a thing. More."),
            Entry(3, "Good faith", "One."),
            Entry(4, "Good Faith", "Two."),
            Entry(5, "Same", "x"),
            Entry(6, "same", "x"),
        ]
    )
    by = {e.term: e.definition for e in out}
    assert by["Fact"] == "A fact is a thing. More."
    assert by["Good faith"] == "One.<br><br>Two."
    assert by["Same"] == "x" and len(out) == 3


def test_act_links_names_and_sections(tmp_path: Path):
    from bdlaw_scraper.dictionaries import ActLinks

    (tmp_path / "act-5.md").write_text('---\ntitle: "The Evidence Act, 1872"\n---\nx ^sec-30\n')
    (tmp_path / "act-9.md").write_text('---\ntitle: "The Penal Code, 1860"\n---\nx\n')
    links = ActLinks(tmp_path)
    assert links.link("See, section 30, the Evidence Act, 1872 (Act No. I of 1872)") == (
        "See, section [[act-5#^sec-30|30]], the [[act-5|Evidence Act, 1872]] (Act No. I of 1872)"
    )
    # section 3 is not an anchor (only ^sec-30 is): the act links, the section does not
    assert links.link("section 3, the Evidence Act, 1872") == "section 3, the [[act-5|Evidence Act, 1872]]"
    assert links.link("Section 99 of the Penal Code, 1860") == "Section 99 of the [[act-9|Penal Code, 1860]]"
    assert links.link("Unknown Act, 1999") == "Unknown Act, 1999"


def test_act_titles_with_a_trailing_note_still_link(tmp_path: Path):
    from bdlaw_scraper.dictionaries import ActLinks

    (tmp_path / "act-7.md").write_text('---\ntitle: "The State Acquisition and Tenancy Act, 1950 (East Bengal Act)"\n---\n')
    assert ActLinks(tmp_path).link("the State Acquisition and Tenancy Act, 1950 (XXVIII of 1951)") == (
        "the [[act-7|State Acquisition and Tenancy Act, 1950]] (XXVIII of 1951)"
    )
