from bdlaw_scraper.wilson import (
    Entry,
    Line,
    assign_sections,
    entry_page,
    join_lines,
    merge_baselines,
    parse_entry,
    render_markdown,
    split_entries,
)


def test_merge_baselines_joins_fragments_of_one_printed_line():
    lines = [Line(0, 218.0, 168.5, "Paying a debt."), Line(0, 217.7, 54.0, "Karjaddya, Ben. (^f?fa)"), Line(0, 235.0, 53.8, "next")]
    merged = merge_baselines(lines)
    assert [m.text for m in merged] == ["Karjaddya, Ben. (^f?fa) Paying a debt.", "next"]


def test_entries_split_by_indentation_and_columns():
    lines = [
        Line(0, 80, 47.2, "KARYAD, Mar. (x) A petty mahal; one com-"),
        Line(0, 90, 60.0, "prising ten villages."),
        Line(0, 100, 54.0, "Karjaddya, Ben. (y) Paying a debt."),
        Line(1, 80, 330.0, "KASA, Tel. (z) A son"),
        Line(1, 90, 343.0, "by a female slave."),
    ]
    groups = split_entries(lines, 761.0)
    assert [len(g) for g in groups] == [2, 1, 2]
    assert join_lines(groups[0]).endswith("one comprising ten villages.")


def test_page_without_two_columns_is_not_an_entry_page():
    front = [Line(0, 100 + i * 12, 80, "text") for i in range(20)]
    page = front + [Line(1, 100 + i * 12, 330, "text") for i in range(20)]
    assert not entry_page(front) and entry_page(page)


def test_parse_entry_keeps_languages_and_flags_native_script():
    e = parse_entry("KARZ, H. (A. V*/) KARJ, Mar. (^) Debt: in Mohammadan law", 300, 0, True)
    assert e is not None and e.term == "KARZ"
    assert e.languages[:2] == ["Hindustani", "Arabic"] and "Marathi" in e.languages
    assert "⟦native⟧" in e.text and len(e.native) == 2 and e.native[0]["lang"] == "A"


def test_sections_glossary_supplement_and_front_matter_dropped():
    def ent(page, term, main=True):
        return Entry(page, 0, term, [], term, main=main)

    entries = [ent(5, "TITLE"), ent(40, "ABAD"), ent(500, "ZULM"), ent(610, "ABKAR"), ent(611, "Sub", False)]
    out = assign_sections(entries, first_page=35)
    assert [(e.term, e.section) for e in out] == [("ABAD", "glossary"), ("ZULM", "glossary"), ("ABKAR", "supplement"), ("Sub", "supplement")]


def test_markdown_groups_by_letter_and_marks_native_words():
    e = parse_entry("ABAD, H. (A. ^^) Cultivated.", 40, 0, True)
    md = render_markdown([e])
    assert md.startswith("## A") and "**ABAD**, H. (A. [native Arabic])" not in md or "[native" in md
    assert "**ABAD**" in md
