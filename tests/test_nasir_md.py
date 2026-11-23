from bdlaw_scraper import nasir_md as n

HTML = b"""<html><head><title>The Test Ordinance, 1971 (Ordinance No. IX of 1971)</title></head><body><div class=Section1>
<p align=center><b>The Test Ordinance, 1971</b></p>
<p><b>1. Short title.</b>\xe2\x80\x94(1) This Ordinance may be cited as the Test Ordinance.</p>
<p>(a) first estab\xc2\xadlished thing;<br>(b) second thing.</p>
<p>(2) It extends to the whole of Pakistan.</p>
</div></body></html>"""


def test_sections_clauses_and_frontmatter():
    text, title = n.convert(HTML, "http://nasirlawsite.com/laws/test.htm", "2026-11-23")
    assert 'act_no: "IX"' in text and "act_year: 1971" in text
    assert "##### Section 1: Short title ^sec-1" in text
    assert "<!--n:(1)-->" in text and "<!--n:(a)-->" in text and "<!--n:(b)-->" in text
    assert "­" not in text and "established" in text


def test_mojibake_is_repaired():
    raw = "<html><body><p>for â€œthe areasâ€\u009d</p></body></html>".encode("cp1252", "c1enc")
    text, _ = n.convert(raw, "http://nasirlawsite.com/laws/x.htm", "2026-11-23")
    assert "“the areas”" in text


WORD_FN = b"""<html><body>
<p align=center><b>The Foot Act, 1950</b></p>
<p align=center>(Act No. V of 1950)</p>
<p align=right>[4<sup>th</sup> March 1950]</p>
<p>An Act to test footnotes.</p>
<p>WHEREAS it is expedient to test;</p>
<p><b>1. Short title.</b> (1) This Act may be called the Foot Act, 1950.<a href="#_ftn1" name="_ftnref1"><sup>[1]</sup></a></p>
<p><b>2. Definitions.</b> In this Act, <a href="#_ftn2" name="_ftnref2"><sup>[2]</sup></a>[the word] means a word.</p>
<div id=ftn1><p><a href="#_ftnref1" name="_ftn1">[1]</a> Inserted by Act II of 1951.</p></div>
<div id=ftn2><p><a href="#_ftnref2" name="_ftn2">[2]</a> Substituted for "term".</p></div>
</body></html>"""


def test_word_footnotes_date_preamble_and_bracket_removal():
    text, _ = n.convert(WORD_FN, "http://nasirlawsite.com/laws/foot.htm", "2026-11-23")
    assert "enacted_at: 1950-03-04" in text
    assert "> [!Preamble]" in text and "An Act to test footnotes." in text
    assert "##### Section 2: Definitions ^sec-2" in text
    assert "[^1]" in text and "[^1]: Inserted by Act II of 1951." in text
    assert "[^2]the word means a word" in text  # the [ ] around amended words is gone, the footnote says it
    assert "ACT No" not in text and "Act No. V of 1950" not in text.split("---", 2)[2]


def test_unbalanced_emphasis_is_dropped_not_left_as_stray_stars():
    assert n._balance("see *ibid. and more") == "see ibid. and more"
    assert n._balance("keep *this* and **that**") == "keep *this* and **that**"
