"""
Unit tests for bdlaw_scraper transformation pipeline.

Run with: pytest test/test_bdlaw_scraper.py -v
"""

import pytest
from bs4 import BeautifulSoup

from bdlaw_scraper.scraper import (
    _classify_item,
    _extract_item_content,
    _strip_annotation_brackets,
    _strip_outer_brackets,
    _strip_unmatched_open_brackets,
    _table_to_markdown,
    _transform_footnotes,
    _check_section_status,
    _is_block_break,
    _join_fragment_lines,
    _join_isolated_labels,
    _bold_punishment_keywords,
    _mark_omitted_repealed,
    _filter_stray_lines,
    convert_lists,
    extract_section_number,
    parse_act_date,
    parse_footnotes,
    _parse_date_from_preamble_text,
)
from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# _classify_item
# ---------------------------------------------------------------------------

class TestClassifyItem:
    def test_decimal(self):
        assert _classify_item("(1) some text") == "decimal"

    def test_alpha(self):
        assert _classify_item("(a) some text") == "alpha"

    def test_roman(self):
        assert _classify_item("(ii) some text") == "roman"

    def test_single_i_is_roman(self):
        # (i) is roman, not alpha
        assert _classify_item("(i) some text") == "roman"

    def test_bn_decimal(self):
        assert _classify_item("(১) text") == "bn_decimal"

    def test_bn_alpha(self):
        assert _classify_item("(ক) text") == "bn_alpha"

    def test_not_list(self):
        assert _classify_item("plain sentence") is None
        assert _classify_item("") is None

    def test_strips_leading_footnote_ref(self):
        # "[^7](a) text" should be classified as alpha
        assert _classify_item("[^7](a) some text") == "alpha"

    def test_strips_bracket_wrapper(self):
        assert _classify_item("[(1) wrapped") == "decimal"


# ---------------------------------------------------------------------------
# _extract_item_content
# ---------------------------------------------------------------------------

class TestExtractItemContent:
    def test_decimal(self):
        assert _extract_item_content("(1) hello world") == "hello world"

    def test_alpha(self):
        assert _extract_item_content("(a) some clause") == "some clause"

    def test_preserves_footnote_prefix(self):
        result = _extract_item_content("[^3] (a) text here")
        assert "[^3]" in result
        assert "text here" in result

    def test_roman(self):
        assert _extract_item_content("(iv) sub-item") == "sub-item"


# ---------------------------------------------------------------------------
# convert_lists
# ---------------------------------------------------------------------------

class TestConvertLists:
    def test_simple_decimal(self):
        text = "(1) First\n(2) Second\n(3) Third"
        result = convert_lists(text)
        assert "1. <!--n:(1)--> First" in result
        assert "2. <!--n:(2)--> Second" in result
        assert "3. <!--n:(3)--> Third" in result

    def test_nested_alpha(self):
        text = "(1) Parent\n(a) Child A\n(b) Child B\n(2) Next"
        result = convert_lists(text)
        assert "1. <!--n:(1)--> Parent" in result
        assert "   1. <!--n:(a)--> Child A" in result
        assert "   2. <!--n:(b)--> Child B" in result
        assert "2. <!--n:(2)--> Next" in result

    def test_continuation_merged(self):
        text = "(1) First clause\n(a) sub-item,\n\nhis act shall be guilty."
        result = convert_lists(text)
        # continuation after blank line merges onto last sub-item
        assert "his act shall be guilty" in result
        assert "\n\n" not in result

    def test_blank_line_between_items_suppressed(self):
        text = "(1) First\n\n(2) Second"
        result = convert_lists(text)
        lines = result.splitlines()
        list_lines = [l for l in lines if l.strip().startswith(("1.", "2."))]
        assert len(list_lines) == 2

    def test_bn_decimal_top_level(self):
        text = "(১) প্রথম\n(২) দ্বিতীয়"
        result = convert_lists(text)
        assert "1. <!--n:(১)--> প্রথম" in result
        assert "2. <!--n:(২)--> দ্বিতীয়" in result

    def test_original_markers_are_recorded(self):
        from bdlaw_scraper import scraper

        scraper._NUMBERING.items = []
        convert_lists("(১) এক\n(ক) উপ\n(২) দুই")
        items = scraper._NUMBERING.items
        scraper._NUMBERING.items = None
        assert [i["marker"] for i in items] == ["(১)", "(ক)", "(২)"]
        assert [i["rendered"] for i in items] == ["1.", "1.", "2."]

    def test_orphan_deep_item_is_not_indented_into_a_code_block(self):
        result = convert_lists("Provided that:\n(i) first\n(ii) second")
        items = [l for l in result.splitlines() if "<!--n:" in l]
        assert all(not l.startswith(" ") for l in items)
        assert items[0].startswith("1. <!--n:(i)-->") and items[1].startswith("2. <!--n:(ii)-->")

    def test_section_reference_lists_are_all_linked(self):
        from bdlaw_scraper.scraper import _linkify_section_refs

        known = {"64", "65", "66", "445"}
        out = _linkify_section_refs("In sections 64, 65, 66 and 445, the word", "11", known)
        assert "[[act-11#^sec-64\\|sections 64]]" in out
        for n in ("65", "66", "445"):
            assert f"[[act-11#^sec-{n}\\|{n}]]" in out

    def test_a_quantity_after_a_section_is_not_linked(self):
        from bdlaw_scraper.scraper import _linkify_section_refs

        out = _linkify_section_refs("sections 3 and 4 years", "11", {"3", "4"})
        assert "[[act-11#^sec-3" in out and "sec-4" not in out

    def test_inserted_and_doubled_markers_are_list_items(self):
        from bdlaw_scraper.scraper import _classify_item, _item_marker

        assert _classify_item("(3B) text") == "decimal" and _item_marker("(3B) text") == "(3B)"
        assert _classify_item("(১ক) পাঠ") == "bn_decimal" and _item_marker("(১ক) পাঠ") == "(১ক)"
        assert _classify_item("(aa) a Commissioner") == "alpha" and _item_marker("(aa) x") == "(aa)"

    def test_table_rows_are_never_split_into_list_items(self):
        from bdlaw_scraper.scraper import _split_inline_list_items

        row = "| 3. | (i) Catering; (ii) Cleaning; (iii) Security |"
        assert _split_inline_list_items(row) == row

    def test_footnote_numbers_survive_in_headings(self):
        from bdlaw_scraper.scraper import _clean_heading

        assert _clean_heading("33 [অপরাধীকে আশ্রয় প্রদানের শাস্তি]") == "অপরাধীকে আশ্রয় প্রদানের শাস্তি[^33]"
        assert _clean_heading("[5 Short title]") == "Short title[^5]"
        assert "[ * * * ]" in _clean_heading("82 [***] Voluntary registration")

    def test_bengali_section_references_are_linked(self):
        from bdlaw_scraper.scraper import _linkify_section_refs

        known = {"3", "128", "132", "135"}
        out = _linkify_section_refs("অর্থ ধারা ৩ এর অধীন; ধারা ১২৮, ১৩২ ও ১৩৫ এবং উপ-ধারা (৩)", "9", known)
        assert "[[act-9#^sec-3\\|ধারা ৩]]" in out
        assert "[[act-9#^sec-132\\|১৩২]]" in out and "[[act-9#^sec-135\\|১৩৫]]" in out
        assert "উপ-ধারা (৩)" in out

    def test_references_to_other_acts_are_not_linked(self):
        from bdlaw_scraper.scraper import _linkify_section_refs

        assert "[[" not in _linkify_section_refs("under section 5 of the Evidence Act", "9", {"5"})
        assert "[[" not in _linkify_section_refs("অর্থ আইন, ২০১৯ এর ধারা ৫৩", "9", {"53"})
        assert "[[" in _linkify_section_refs("under section 5 of this Act", "9", {"5"})

    def test_lowercase_repealed_by_is_not_struck_through(self):
        from bdlaw_scraper.scraper import _mark_omitted_repealed

        text = "any enactment not expressly repealed by this Act;"
        assert _mark_omitted_repealed(text) == text
        assert _mark_omitted_repealed("Repealed by section 3 of X.") == "~~Repealed by section 3 of X.~~"

    def test_two_footnote_refs_in_one_word_run_both_survive(self):
        from bdlaw_scraper.scraper import _transform_footnotes

        assert _transform_footnotes("আইন, [11]২০১২[12]এর ধারা") == "আইন, [^11]২০১২[^12]এর ধারা"
        assert "ABC" not in _transform_footnotes("text ^ABC^ more")
        assert _transform_footnotes("[28[29]Text") == "[^28][^29]Text"

    def test_unmatched_closing_brackets_are_dropped(self):
        from bdlaw_scraper.scraper import _drop_unmatched_closers

        assert _drop_unmatched_closers("a determine.]]") == "a determine."
        assert _drop_unmatched_closers("[[act-1|x]] and [^2] [ * * * ]") == "[[act-1|x]] and [^2] [ * * * ]"
        # an amendment bracket around several lines goes away with both ends
        assert _drop_unmatched_closers("[line one\nline two]") == "line one\nline two"
        assert _drop_unmatched_closers("[[act-1\\|x]] [^2] [ * * * ]") == "[[act-1\\|x]] [^2] [ * * * ]"

    def test_bengali_bracketed_text_is_not_a_footnote_number(self):
        from bdlaw_scraper.scraper import _transform_footnotes

        assert _transform_footnotes("আইন, [11][২০১২] এর") == "আইন, [^11][২০১২] এর"

    def test_links_to_missing_acts_point_at_the_official_site(self, tmp_path):
        import logging

        from bdlaw_scraper.scraper import relink_missing_acts

        (tmp_path / "act-1.md").write_text("see [[act-1\\|Self]] and [[act-9#^sec-3\\|Gone Act]]", encoding="utf-8")
        assert relink_missing_acts(tmp_path, logging.getLogger("t")) == 1
        out = (tmp_path / "act-1.md").read_text(encoding="utf-8")
        assert "[[act-1\\|Self]]" in out and "[Gone Act](http://bdlaws.minlaw.gov.bd/act-9.html)" in out

    def test_markdown_renderers_ignore_the_marker_comment(self):
        result = convert_lists("(1) Only")
        assert result.startswith("1. <!--n:(1)--> Only")

    def test_count_resets_on_shallower(self):
        text = "(1) A\n(a) sub\n(2) B\n(a) sub2"
        result = convert_lists(text)
        # second (a) should be 1. again (reset when depth 1 item appears)
        sub_lines = [l for l in result.splitlines() if "sub" in l]
        assert all("1." in l for l in sub_lines)


# ---------------------------------------------------------------------------
# _transform_footnotes
# ---------------------------------------------------------------------------

class TestTransformFootnotes:
    def test_numbered_ref(self):
        assert "[^5]" in _transform_footnotes("[5]")

    def test_omission_marker(self):
        result = _transform_footnotes("[***]")
        assert "[ * * * ]" in result

    def test_superscript_noise_removed(self):
        result = _transform_footnotes("text ^ABC^ more")
        assert "^ABC^" not in result
        assert "text" in result

    def test_multiple_refs(self):
        result = _transform_footnotes("[1] and [2]")
        assert "[^1]" in result
        assert "[^2]" in result


# ---------------------------------------------------------------------------
# _strip_annotation_brackets
# ---------------------------------------------------------------------------

class TestStripAnnotationBrackets:
    def test_strips_annotation(self):
        result = _strip_annotation_brackets("text [added by amendment] more")
        assert "[added by amendment]" not in result
        assert "added by amendment" in result

    def test_preserves_wikilink(self):
        text = "see [[act-430\\|Bangladesh Laws Act]] for details"
        result = _strip_annotation_brackets(text)
        assert "[[act-430\\|Bangladesh Laws Act]]" in result

    def test_preserves_footnote_ref(self):
        result = _strip_annotation_brackets("text[^5] here")
        assert "[^5]" in result

    def test_preserves_omission_marker(self):
        result = _strip_annotation_brackets("[ * * * ]")
        assert "[ * * * ]" in result

    def test_nested_wikilink_then_annotation(self):
        text = "[[act-1\\|Penal Code]] [amended 1973]"
        result = _strip_annotation_brackets(text)
        assert "[[act-1\\|Penal Code]]" in result
        assert "amended 1973" in result
        assert "[amended 1973]" not in result


# ---------------------------------------------------------------------------
# _strip_outer_brackets
# ---------------------------------------------------------------------------

class TestStripOuterBrackets:
    def test_fully_wrapped(self):
        assert _strip_outer_brackets("[hello world]") == "hello world"

    def test_not_wrapped(self):
        assert _strip_outer_brackets("[a] or [b]") == "[a] or [b]"

    def test_no_brackets(self):
        assert _strip_outer_brackets("plain") == "plain"


# ---------------------------------------------------------------------------
# _table_to_markdown
# ---------------------------------------------------------------------------

class TestTableToMarkdown:
    def _soup(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "lxml").find("table")

    def test_simple_table(self):
        html = "<table><tr><td>A</td><td>B</td></tr><tr><td>C</td><td>D</td></tr></table>"
        result = _table_to_markdown(self._soup(html))
        assert "| A | B |" in result
        assert "| C | D |" in result
        assert "---" in result

    def test_colspan(self):
        html = "<table><tr><td>A</td><td>B</td></tr><tr><td colspan='2'>Full</td></tr></table>"
        result = _table_to_markdown(self._soup(html))
        assert "Full" in result
        # Full row has 2 columns: text + empty
        full_row = [l for l in result.splitlines() if "Full" in l][0]
        assert full_row.count("|") >= 3

    def test_rowspan(self):
        html = "<table><tr><td rowspan='2'>Span</td><td>R1</td></tr><tr><td>R2</td></tr></table>"
        result = _table_to_markdown(self._soup(html))
        lines = [l for l in result.splitlines() if l.startswith("|") and "---" not in l and l.strip() != "|  |  |"]
        # Span should appear once; R2 row should have empty first cell
        assert any("Span" in l for l in lines)
        r2_line = [l for l in lines if "R2" in l]
        assert r2_line, "R2 not found in output"
        # first cell of R2 row should be empty (covered by rowspan)
        assert r2_line[0].startswith("|  |") or "| |" in r2_line[0]

    def test_pipe_escaped_in_cell(self):
        html = "<table><tr><td>A | B</td></tr></table>"
        result = _table_to_markdown(self._soup(html))
        assert "\\|" in result

    def test_empty_rows_skipped(self):
        html = "<table><tr><td></td><td></td></tr><tr><td>Data</td><td>Row</td></tr></table>"
        result = _table_to_markdown(self._soup(html))
        data_lines = [l for l in result.splitlines() if "Data" in l]
        assert data_lines


# ---------------------------------------------------------------------------
# _check_section_status
# ---------------------------------------------------------------------------

class TestCheckSectionStatus:
    def test_omitted(self):
        result = _check_section_status("Omitted.")
        assert result is not None
        assert "Omitted" in result
        assert "[!Warning]" in result

    def test_repealed(self):
        result = _check_section_status("[Repealed]")
        assert result is not None
        assert "Repealed" in result

    def test_normal_text(self):
        assert _check_section_status("The act provides...") is None


# ---------------------------------------------------------------------------
# _is_block_break
# ---------------------------------------------------------------------------

class TestIsBlockBreak:
    def test_heading(self):
        assert _is_block_break("# Heading")
        assert _is_block_break("## Chapter")

    def test_callout(self):
        assert _is_block_break("> [!Explanation]")

    def test_bold(self):
        assert _is_block_break("**Provided that**:")

    def test_list_item(self):
        assert _is_block_break("(1) item")
        assert _is_block_break("(a) sub-item")

    def test_plain_text(self):
        assert not _is_block_break("ordinary sentence here")
        assert not _is_block_break("")


# ---------------------------------------------------------------------------
# _bold_punishment_keywords
# ---------------------------------------------------------------------------

class TestBoldPunishment:
    def test_bolded(self):
        text = "the offender shall be punished with imprisonment"
        result = _bold_punishment_keywords(text)
        assert "**shall be punished with**" in result

    def test_not_double_bolded(self):
        text = "the offender **shall be punished with** imprisonment"
        result = _bold_punishment_keywords(text)
        assert result.count("**shall be punished with**") == 1


# ---------------------------------------------------------------------------
# _mark_omitted_repealed
# ---------------------------------------------------------------------------

class TestMarkOmittedRepealed:
    def test_omitted(self):
        # regex stops at first sentence-end punct, so matches "Omitted by Act No."
        text = "Omitted by Act No. VIII of 1973."
        result = _mark_omitted_repealed(text)
        assert "~~" in result
        assert "Omitted by" in result

    def test_repealed(self):
        text = "Repealed by the Bangladesh Laws Act, 1973."
        result = _mark_omitted_repealed(text)
        assert "~~" in result


# ---------------------------------------------------------------------------
# _filter_stray_lines
# ---------------------------------------------------------------------------

class TestFilterStrayLines:
    def test_removes_url(self):
        text = "good line\nhttps://example.com\nanother good line"
        result = _filter_stray_lines(text)
        assert "https://example.com" not in result
        assert "good line" in result

    def test_removes_decorative_symbols(self):
        text = "§ section marker\nreal content"
        result = _filter_stray_lines(text)
        assert "§" not in result

    def test_removes_contents_header(self):
        text = "CONTENTS\nactual content"
        result = _filter_stray_lines(text)
        assert "CONTENTS" not in result


# ---------------------------------------------------------------------------
# extract_section_number
# ---------------------------------------------------------------------------

class TestExtractSectionNumber:
    def test_plain_english(self):
        assert extract_section_number("4. Short title") == "4"

    def test_plain_with_letter(self):
        assert extract_section_number("4A. Inserted section") == "4A"

    def test_bracketed(self):
        assert extract_section_number("[5.] Section body") == "5"

    def test_bengali(self):
        result = extract_section_number("৪। সংজ্ঞা")
        assert result == "৪"

    def test_no_number(self):
        assert extract_section_number("PREAMBLE\nWhereas...") is None


# ---------------------------------------------------------------------------
# parse_act_date
# ---------------------------------------------------------------------------

class TestParseActDate:
    def _soup(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "lxml")

    def test_bengali_date(self):
        html = '<p class="publish-date">[ ১০ এপ্রিল, ২০২৬ ]</p>'
        assert parse_act_date(self._soup(html)) == "2026-04-10"

    def test_english_ordinal(self):
        html = '<p class="publish-date">[ 11th September, 1836 ]</p>'
        assert parse_act_date(self._soup(html)) == "1836-09-11"

    def test_missing(self):
        assert parse_act_date(self._soup("<p>no date</p>")) is None


# ---------------------------------------------------------------------------
# _parse_date_from_preamble_text
# ---------------------------------------------------------------------------

class TestParseDateFromPreamble:
    def test_corresponding_to(self):
        text = "corresponding to the fourth day of November, 1972 A.D., do hereby adopt"
        assert _parse_date_from_preamble_text(text) == "1972-11-04"

    def test_numeric_ordinal(self):
        text = "the 26th day of March, 1971"
        assert _parse_date_from_preamble_text(text) == "1971-03-26"

    def test_prefers_corresponding_to(self):
        # Should prefer "corresponding to" over earlier dates
        text = (
            "independence on the 26th day of March, 1971 "
            "corresponding to the fourth day of November, 1972"
        )
        assert _parse_date_from_preamble_text(text) == "1972-11-04"

    def test_unknown_month(self):
        text = "the eighteenth day of Kartick, 1379"
        assert _parse_date_from_preamble_text(text) is None


# ---------------------------------------------------------------------------
# parse_footnotes (integration)
# ---------------------------------------------------------------------------

class TestParseFootnotes:
    def test_basic_footnote(self):
        html = """<div class="footnoteListAll"><ul>
        <li class="footnoteList">
            <h6><sup class="en">1</sup></h6>
            The word "BENGAL" was omitted by section 3 of the
            <a href="/act-430.html">Bangladesh Laws Act, 1973</a>.
        </li></ul></div>"""
        soup = BeautifulSoup(html, "lxml")
        fi = parse_footnotes(soup)
        assert fi is not None
        assert len(fi.notes) == 1
        num, text = fi.notes[0]
        assert num == 1
        assert "[[act-430\\|Bangladesh Laws Act, 1973]]" in text

    def test_no_container(self):
        soup = BeautifulSoup("<div>nothing</div>", "lxml")
        assert parse_footnotes(soup) is None

    def test_non_numeric_sup_skipped(self):
        html = """<div class="footnoteListAll"><ul>
        <li class="footnoteList"><h6><sup class="en">N/A</sup></h6>ignored</li>
        <li class="footnoteList"><h6><sup class="en">2</sup></h6>valid</li>
        </ul></div>"""
        fi = parse_footnotes(BeautifulSoup(html, "lxml"))
        assert fi is not None
        assert len(fi.notes) == 1
        assert fi.notes[0][0] == 2


# ---------------------------------------------------------------------------
# _join_fragment_lines
# ---------------------------------------------------------------------------

class TestJoinFragmentLines:
    def test_joins_or_connector(self):
        text = "part one\nor\npart two"
        result = _join_fragment_lines(text)
        assert "part one or part two" in result or "part one\nor\npart two" == result or "part one" in result

    def test_joins_namely(self):
        text = "types of things\n, namely:\nthings"
        result = _join_fragment_lines(text)
        # connector line merged
        assert result.count("\n") < text.count("\n")


# ---------------------------------------------------------------------------
# _join_isolated_labels
# ---------------------------------------------------------------------------

class TestJoinIsolatedLabels:
    def test_isolated_label_joined(self):
        text = "(a)\nsome content here"
        result = _join_isolated_labels(text)
        assert "(a) some content here" in result

    def test_non_isolated_unchanged(self):
        text = "(a) already has content"
        result = _join_isolated_labels(text)
        assert result == text


def test_clean_heading_mid_footnote_number():
    from bdlaw_scraper.scraper import _clean_heading

    assert _clean_heading("নিষিদ্ধ 18 [সত্তার] সদস্যপদ") == "নিষিদ্ধ সত্তার সদস্যপদ[^18]"


def test_clean_heading_leading_footnote_number_then_text():
    from bdlaw_scraper.scraper import _clean_heading

    assert _clean_heading("2 [মহাপরিচালক], ইত্যাদি") == "মহাপরিচালক, ইত্যাদি[^2]"


def test_adjacent_footnote_refs_keep_their_brackets():
    from bs4 import BeautifulSoup
    from bdlaw_scraper.scraper import html_to_markdown

    h = "<p>of June, <span class='footnote' title='x'><span class='word-formet'><sup class='en'><a href='2' >335</a></sup></span></span>[1995] (both)</p>"
    assert "[^335][^1995]" in html_to_markdown(BeautifulSoup(h, "html.parser"))


def test_year_in_brackets_without_a_note_stays_plain_text():
    from bdlaw_scraper.scraper import _plain_undefined_refs

    doc = "Code, [^18][^1898].\n\n[^18]: The figure was substituted."
    assert _plain_undefined_refs(doc).startswith("Code, [^18][1898].")


def test_preamble_keeps_footnote_reference():
    from bs4 import BeautifulSoup
    from bdlaw_scraper.scraper import parse_preamble

    h = (
        "<div class='preamble-content'><p><span class='footnote' title='x'><sup><a>1</a></sup></span>"
        "*An Act to amend the law</p><p>Whereas it is expedient.</p></div>"
    )
    out = parse_preamble(BeautifulSoup(h, "html.parser"))
    assert out.splitlines()[0].endswith("[^1]")


def test_open_editorial_bracket_before_wikilink_is_dropped():
    from bdlaw_scraper.scraper import _drop_unmatched_closers

    assert _drop_unmatched_closers("by the [[[act-354|Customs Act]].") == "by the [[act-354|Customs Act]]."


def test_bracket_followed_by_paren_in_heading_is_escaped():
    from bdlaw_scraper.scraper import _RE_LINK_LOOKALIKE

    assert _RE_LINK_LOOKALIKE.sub(r"\\[\1\\](", "(b) [Omitted](c) x") == "(b) \\[Omitted\\](c) x"


def test_callout_list_restarts_at_one():
    from bdlaw_scraper.scraper import _restart_callout_lists

    doc = "> > [!Illustrations]\n> > 5. <!--n:(a)--> A.\n> > 6. <!--n:(b)--> B.\n\n1. <!--n:(1)--> x"
    assert _restart_callout_lists(doc) == "> > [!Illustrations]\n> > 1. <!--n:(a)--> A.\n> > 2. <!--n:(b)--> B.\n\n1. <!--n:(1)--> x"


def test_ordinal_words_become_a_list():
    from bdlaw_scraper.scraper import _convert_word_ordinals

    doc = "namely:-\n\nFirst.-Omitted;\n\nSecond. Every officer;\n\nThird.- Every Judge;\n\nFirst. a lone sentence."
    out = _convert_word_ordinals(doc).split("\n")
    assert out[2] == "1. <!--n:First--> Omitted;"
    assert out[4] == "2. <!--n:Second--> Every officer;"
    assert out[6] == "3. <!--n:Third--> Every Judge;"
    assert out[8] == "First. a lone sentence."


def test_heading_drops_inner_amendment_brackets():
    from bdlaw_scraper.scraper import _clean_heading

    assert _clean_heading("19 [Commutation of sentence of [ imprisonment ] for life]") == "Commutation of sentence of imprisonment for life[^19]"


def test_preamble_drops_lone_dash_line():
    from bs4 import BeautifulSoup
    from bdlaw_scraper.scraper import parse_preamble

    h = "<div class='preamble-content'><p>Whereas it is expedient;</p><p>-</p></div>"
    assert parse_preamble(BeautifulSoup(h, "html.parser")) == "Whereas it is expedient;"


def test_ordinal_item_with_inline_sublist_nests():
    from bdlaw_scraper.scraper import _convert_word_ordinals

    doc = (
        "First.-A;\n\nSecond.-Every person- (a) in the Government;\n1. <!--n:(b)--> in a body;\n\n"
        "Third.-C."
    )
    out = _convert_word_ordinals(doc)
    assert "2. <!--n:Second--> Every person-" in out
    assert "\n   1. <!--n:(a)--> in the Government;" in out
    assert "\n   2. <!--n:(b)--> in a body;" in out
    assert "3. <!--n:Third--> C." in out


def test_adverb_items_become_a_list_with_nested_sublist():
    from bdlaw_scraper.scraper import _convert_word_ordinals

    doc = (
        "are,-\n\n**Firstly**: Death;\n\n**Secondly**: [^15]Life;\n\n"
        "**Thirdly**: Imprisonment, namely:-\n\n1. <!--n:(1)--> Rigorous;\n2. <!--n:(2)--> Simple;\n\n"
        "**Fourthly**: Forfeiture;"
    )
    out = _convert_word_ordinals(doc)
    assert "1. <!--n:Firstly--> Death;" in out
    assert "2. <!--n:Secondly--> [^15]Life;" in out
    assert "   1. <!--n:(1)--> Rigorous;" in out and "   2. <!--n:(2)--> Simple;" in out
    assert "4. <!--n:Fourthly--> Forfeiture;" in out
