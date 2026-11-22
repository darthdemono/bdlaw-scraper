from bdlaw_scraper.citations import link_citations, roman
from bdlaw_scraper.scraper import RE_BN_SECTION_NUM_PLAIN


def test_roman():
    assert roman("XLV") == 45 and roman("IV") == 4 and roman("12") == 12


def test_penal_code_is_linked():
    idx = {("act", 45, 1860): "act-11"}
    out, n = link_citations("Penal Code (XLV of 1860) এর অধীন", idx)
    assert n == 1 and out.startswith("[[act-11\\|Penal Code]] (XLV of 1860)")


def test_existing_links_and_unknown_acts_untouched():
    idx = {("act", 45, 1860): "act-11"}
    text = "[[act-11\\|Penal Code (XLV of 1860)]] and Foo Act (IX of 1900)"
    assert link_citations(text, idx) == (text, 0)


def test_bengali_section_number_with_space_before_danda():
    assert RE_BN_SECTION_NUM_PLAIN.match("১৮ । এই আইনের").group(1) == "১৮"
