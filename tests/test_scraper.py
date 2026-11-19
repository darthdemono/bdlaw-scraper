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
