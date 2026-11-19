from bdlaw_scraper.wilson_native import allowed_scripts, script_text


def test_script_text_keeps_only_expected_script():
    assert script_text("(प्रियतामही) from the S. word", ["devanagari"]) == "प्रियतामही"
    assert script_text("(P. جيشي)", ["arabic"]) == "جيشي"
    assert script_text("(Latin only)", ["telugu"]) == ""


def test_hindustani_may_be_urdu_or_devanagari_and_unknown_code_accepts_all():
    assert allowed_scripts("H") == ["arabic", "devanagari"]
    assert "telugu" in allowed_scripts("")
