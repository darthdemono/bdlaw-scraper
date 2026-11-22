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


def test_inserted_clause_stays_beside_a_parent_that_has_its_own_clauses():
    md = """##### [Section 3: Definitions](u) ^sec-3
1. <!--n:(21)--> Government shall mean-
   1. <!--n:(a)--> before
   2. <!--n:(b)--> after
2. <!--n:(21a)--> Government contracts
"""
    out, _ = anchor_items(nest_inserted_clauses(md))
    assert "\n2. <!--n:(21a)--> Government contracts ^sec-3-21a" in out
