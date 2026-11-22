from bdlaw_scraper.clauses import anchor_and_link, anchor_items

ACT = """# T

##### [Section 102: Writs](u) ^sec-102

1. <!--n:(1)--> First.
2. <!--n:(2)--> Second.
3. <!--n:(3)--> Third.
   1. <!--n:(a)--> Detail.

##### [Section 117: Tribunals](u) ^sec-117

1. <!--n:(1)--> Law to which clause (3) of article 102 applies, or sub-clause (a) of clause (3) of article 102, or clause (9) of article 102.
2. <!--n:(2)--> See sub-section (1); and [[act-7#^sec-102\\|section 102(3)(a)]].
"""


def test_every_list_item_gets_a_block_id_from_its_path():
    out, known = anchor_items(ACT)
    assert "Third. ^sec-102-3" in out and "Detail. ^sec-102-3-a" in out
    assert {"sec-102-1", "sec-102-3-a", "sec-117-2"} <= known


def test_references_link_to_the_clause_not_only_the_section():
    out = anchor_and_link(ACT, "7")
    assert "[[act-7#^sec-102-3\\|clause (3) of article 102]]" in out
    assert "[[act-7#^sec-102-3-a\\|sub-clause (a) of clause (3) of article 102]]" in out
    # a clause that does not exist falls back to the section
    assert "[[act-7#^sec-102\\|clause (9) of article 102]]" in out
    # a bare "sub-section (1)" is the one in the same section; a section link naming a clause is deepened
    assert "[[act-7#^sec-117-1\\|sub-section (1)]]" in out
    assert "[[act-7#^sec-102-3-a\\|section 102(3)(a)]]" in out


def test_bengali_markers_and_references_work_the_same_way():
    md = """# T

##### [ধারা ৫: বিধান](u) ^sec-5

1. <!--n:(১)--> ক্ষমতা
   1. <!--n:(ক)--> প্রথম
2. <!--n:(২)--> উপ-ধারা (১) এবং [[act-9#^sec-5\\|ধারা ৫(১)(ক)]] দেখুন।
"""
    out = anchor_and_link(md, "9")
    assert "ক্ষমতা ^sec-5-1" in out and "প্রথম ^sec-5-1-a" in out
    assert "[[act-9#^sec-5-1\\|উপ-ধারা (১)]]" in out
    assert "[[act-9#^sec-5-1-a\\|ধারা ৫(১)(ক)]]" in out