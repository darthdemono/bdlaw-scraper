from bdlaw_scraper.clauses import anchor_items, link_clauses
from bdlaw_scraper.scraper import _split_inline_list_items


def test_clause_reference_chain_is_not_split_into_list_items():
    t = 'the word "Act" in clauses (9), (12), (38), (48) and (50) of section 3 shall be'
    assert _split_inline_list_items(t) == t


def test_each_clause_of_a_chain_gets_its_own_link():
    md = "##### [Section 3](u) ^sec-3\n1. <!--n:(9)--> a\n2. <!--n:(12)--> b\n\nsee clauses (9) and (12) of [[act-1#^sec-3\\|section 3]]\n"
    out, known = anchor_items(md)
    out = link_clauses(out, "1", known | {"sec-3"})
    assert "[[act-1#^sec-3-9\\|(9)]] and [[act-1#^sec-3-12\\|(12)]] of [[act-1#^sec-3\\|section 3]]" in out
