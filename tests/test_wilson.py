from bdlaw_scraper.wilson import (
    Entry,
    Line,
    assign_sections,
    entry_page,
    group_text,
    is_junk,
    split_runons,
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
        Line(0, 90, 56.0, "prising ten villages."),
        Line(0, 100, 47.5, "Karjaddya, Ben. (y) Paying a debt."),
        Line(1, 80, 330.0, "KASA, Tel. (z) A son"),
        Line(1, 90, 343.0, "by a female slave."),
    ]
    groups = split_entries(lines, 761.0)
    assert [len(g) for g in groups] == [2, 1, 2]
    words = lambda t, y: [(w, 0, y, 0, y + 9) for w in t.split()]
    for g in groups[:1]:
        for l in g:
            l.words = words(l.text, l.y)
    assert group_text(groups[0], 300, set())[0].endswith("one comprising ten villages.")


def test_page_without_two_columns_is_not_an_entry_page():
    front = [Line(0, 100 + i * 12, 80, "text") for i in range(20)]
    page = front + [Line(1, 100 + i * 12, 330, "text") for i in range(20)]
    assert not entry_page(front) and entry_page(page)


def test_parse_entry_keeps_languages_and_flags_native_script():
    e = parse_entry("KARZ, H. (A. ⟦native⟧) KARJ, Mar. (⟦native⟧) Debt: in Mohammadan law", 300, 0, True, native=[{"raw": "x", "lang": "A"}, {"raw": "y", "lang": "Mar"}])
    assert e is not None and e.term == "KARZ"
    assert e.languages[:2] == ["Hindustani", "Arabic"] and "Marathi" in e.languages
    assert "⟦native⟧" in e.text and len(e.native) == 2 and e.native[0]["lang"] == "A"


def test_junk_words_become_one_native_marker_with_boxes_and_language():
    line = Line(0, 20, 10, "Abdi, H. (i_jU) Patrimonial, S. (^TW*f, a leaf)")
    line.words = [(w, 10 + 20 * k, 20, 28 + 20 * k, 29) for k, w in enumerate(line.text.split())]
    text, native = group_text([line], 35, set())
    assert text == "Abdi, H. (⟦native⟧) Patrimonial, S. (⟦native⟧, a leaf)"
    assert [n["lang"] for n in native] == ["H", "S"] and native[0]["boxes"] == [[50, 20, 68, 29]] and native[0]["page"] == 35
    assert is_junk("iTcjiSirr") and is_junk("^«$Hf^O") and not is_junk("Abdar") and not is_junk("19th")


def test_run_together_entries_split_after_a_finished_sentence():
    parts = split_runons("JANANI, H. A mother. JANAZA, A. A funeral.", [])
    assert [p for p, _ in parts] == ["JANANI, H. A mother.", "JANAZA, A. A funeral."]
    assert len(split_runons("Abdar, H. A servant; see Ajya, S. portion.", [])) == 1


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


def test_native_words_render_as_language_tagged_spans():
    from bdlaw_scraper.wilson import Entry, apply_native, render_markdown

    e = Entry(35, 0, "Abdi", ["Hindustani"], "Abdi, H. (⟦native⟧) Patrimonial", [{"raw": "x", "lang": "H", "page": 35, "boxes": [[1.0, 2.0, 3.0, 4.0]]}], True)
    apply_native([e], {"35:1.0:2.0": "آبدار"})
    out = render_markdown([e])
    assert '<span lang="ur" dir="rtl">آبدار</span>' in out
    e.native[0].pop("text")
    assert "[unread Hindustani]" in render_markdown([e])


def test_match_score_flags_words_that_do_not_match_the_latin_headword():
    from bdlaw_scraper.wilson import match_score

    assert match_score("Chulat, Mar.", "चुलत") >= 70
    assert match_score("Abdi, H.", "नमूना") < 40


def test_table_page_has_a_row_per_entry_and_see_links_to_the_letter():
    from bdlaw_scraper.wilson import Entry, render_table

    a = Entry(40, 0, "ABDAR", ["Hindustani"], "ABDAR, H. (⟦native⟧) A servant. See Adasatta and Zeta.", [{"raw": "x", "lang": "H", "text": "آبدار"}], True)
    b = Entry(41, 0, "ADASATTA", ["Hindustani"], "ADASATTA, H. An estimate.", [], True)
    page = render_table([a, b])
    assert "| Term | Language | Meaning |" in page and "generated: true" in page
    assert '| **ABDAR** <span lang="ur" dir="rtl">آبدار</span> | Hindustani | A servant.' in page
    assert "[[Wilson Glossary#A\\|Adasatta]]" in page and "Zeta" in page and "#A\\|Zeta" not in page


def test_fold_fragments_joins_lowercase_pieces_to_the_entry_before():
    from bdlaw_scraper.wilson import Entry, fold_fragments

    a = Entry(40, 0, "KARZ", [], "KARZ, H. A debt, a loan,", [], True)
    b = Entry(40, 0, "money lent", [], "money lent at interest.", [{"raw": "x", "lang": "H"}], False)
    c = Entry(40, 0, "Karzi", [], "Karzi, H. Indebted.", [], False)
    out = fold_fragments([a, b, c])
    assert [e.term for e in out] == ["KARZ", "Karzi"] and out[0].text.endswith("money lent at interest.") and len(out[0].native) == 1


def test_vocabulary_check_rejects_shapeless_words_and_keeps_real_ones():
    from bdlaw_scraper.wilson import plausible

    vocab = {"mortgagee", "zodiac", "kshatriya"}
    assert is_junk("fffai", vocab) and is_junk("Stfsf", vocab) and is_junk("iTcjiSirr", vocab)
    assert not is_junk("mortgagee", vocab) and not is_junk("Kshatriya", vocab) and not is_junk("Abdar", vocab)
    assert plausible("Zamindári-ddr", vocab) and not plausible("Strtsk", vocab)


def test_letters_follow_the_running_headword_and_ignore_ocr_jumps():
    from bdlaw_scraper.wilson import Entry, assign_letters

    mk = lambda term, main, sec="glossary": Entry(40, 0, term, [], term, [], main, sec)
    es = [mk("ABDAR", True), mk("Lands held", False), mk("BAKI", True), mk("ZZZ", True), mk("Sub", False), mk("CHAK", True), mk("ABC", True, "supplement")]
    assert assign_letters(es) == ["A", "A", "B", "B", "B", "C", "A"]


def test_small_caps_headwords_with_accents_are_main():
    from bdlaw_scraper.wilson import is_small_caps

    assert is_small_caps("ÁJNÁ, in some dialects") and is_small_caps("BUDH-GANGA, BooDH") and is_small_caps("→ KARZ, H.")
    assert not is_small_caps("Ajná patra, H. Ben.") and not is_small_caps("AB") and not is_small_caps("A. word")


def test_native_script_in_the_text_becomes_language_tagged_spans():
    from bdlaw_scraper.wilson import wrap_scripts

    out = wrap_scripts("Ajir, A. (اجير مشترك) A hireling; S. (साजा) Order, Karjádáya, Ben. (কলাদায) debt")
    assert '<span lang="ar" dir="rtl"' in out and "اجير مشترك</span>" in out
    assert '<span lang="hi" dir="ltr"' not in out or 'lang="sa"' in out  # Devanagari after S. is Sanskrit
    assert 'lang="sa"' in out and 'lang="bn"' in out


def test_readings_replace_the_text_layer_and_undo_hyphenation():
    from bdlaw_scraper.wilson import reading_text
    from bdlaw_scraper.wilson_lines import line_key

    a = Line(0, 100.0, 50.0, "Ajna, H. (^n$T) Order, com-")
    b = Line(0, 110.0, 56.0, "mand.")
    readings = {line_key(35, 0, 100.0): "ÁJNÁ, H. (साजा) Order, com-", line_key(35, 0, 110.0): "mand."}
    text, native = reading_text([a, b], 35, set(), readings)
    assert text == "ÁJNÁ, H. (साजा) Order, command." and native == []


def test_repair_reading_restores_dropped_edge_words_and_replaces_nonsense():
    from bdlaw_scraper.wilson import repair_reading

    lex = {"medan", "law", "peons", "ment", "tax"}
    assert repair_reading("law.", "medan law.", lex) == "medan law."
    assert repair_reading("used al", "peons.", lex) == "peons."
    assert repair_reading("Ábhári Dárogha, H. (آبكاري)", "Abkdri Ddrogha, H. (iCjjta^rj&T)", lex) == "Ábhári Dárogha, H. (آبكاري)"


def test_clean_reading_fixes_lookalikes_marks_foreign_script_and_rejects_hallucinations():
    from bdlaw_scraper.wilson import clean_reading

    assert clean_reading("Тніке, Н. Rice") == "Thike, H. Rice"
    assert clean_reading("Kakka, Karn. (〇〇) A father") is None  # a script the book never uses: fall back to the text layer
    assert clean_reading("XXXXXXXXXXXXXXXX") is None


def test_extended_runon_split_catches_small_caps_with_variants():
    from bdlaw_scraper.wilson import split_runons

    parts = [p for p, _ in split_runons("Gumi, H. A servant. GUMÁSHTA, vernacularly GUMASHTA, H. An agent. Varigára, Karn. A tax-gatherer.", [])]
    assert [p.split(",")[0] for p in parts] == ["Gumi", "GUMÁSHTA", "Varigára"]


def test_clean_reading_turns_latex_fractions_into_text_and_drops_repeated_phrases():
    from bdlaw_scraper.wilson import clean_reading

    assert clean_reading("12\\frac{3}{4} are equal to one rupee") == "12 3/4 are equal to one rupee"
    assert clean_reading("a report drawn up by the collector A report drawn up by the collector") == "a report drawn up by the collector"


def test_a_line_end_read_twice_is_collapsed():
    from bdlaw_scraper.wilson import reading_text
    from bdlaw_scraper.wilson_lines import line_key

    a = Line(0, 100.0, 50.0, "adopting his son-in-law.")
    readings = {line_key(35, 0, 100.0): "adopting his son-in-law. son-in-law."}
    assert reading_text([a], 35, set(), readings)[0] == "adopting his son-in-law."


def test_readings_never_hold_line_breaks_or_latex_and_running_heads_are_dropped():
    from bdlaw_scraper.wilson import clean_reading

    assert clean_reading("Dekh.\n\nSee Amanji.") == "Dekh. See Amanji."
    assert clean_reading("Fine muslin. \\acute{A}bi and \\textbf{word}") == "Fine muslin. Ábi and word"
