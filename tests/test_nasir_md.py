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
