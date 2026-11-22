from bdlaw_scraper.clauses import anchor_items, nest_inserted_clauses

MD = """##### [Section 3: Definitions](u) ^sec-3
1. <!--n:(1)--> abet
2. <!--n:(1a)--> Act of Parliament
3. <!--n:(2)--> act
4. <!--n:(2a)--> Advocate
   1. <!--n:(a)--> first
   2. <!--n:(b)--> second
"""


def test_inserted_clause_nests_under_its_parent_and_keeps_its_id():
    out, known = anchor_items(nest_inserted_clauses(MD))
    assert "   1. <!--n:(1a)--> Act of Parliament ^sec-3-1a" in out
    assert "      1. <!--n:(a)--> first ^sec-3-2a-a" in out
    assert {"sec-3-1", "sec-3-1a", "sec-3-2", "sec-3-2a"} <= known
