import json

from bdlaw_scraper.harvest.courts import parse
from bdlaw_scraper.harvest.gazette import rows
from bdlaw_scraper.harvest.kit import Status


def test_gazette_rows_read_title_ministry_date_and_pdf():
    page = '<table><tr><th>SL</th></tr><tr><td>1</td><td>Notice A</td><td>Ministry of X</td><td>28/05/2023</td><td><a href="https://gazettes.dpp.gov.bd/storage/uploads/1_2.pdf"btn btn-success> Download </a></td></tr></table>'
    assert rows(page) == [{"pdf_url": "https://gazettes.dpp.gov.bd/storage/uploads/1_2.pdf", "sl": 1, "title": "Notice A", "ministry": "Ministry of X", "date": "28/05/2023"}]


def test_court_rows_read_case_number_and_upload_date():
    page = '<table><tr class="r"><td>1</td><td><a href="https://supremecourt.gov.bd/resources/documents/9_CA73of2023.pdf">Civil Appeal 73/2023<br>(.../)</a> Uploaded on : 25-OCT-26</td><td>State Vs X</td></tr></table>'
    got = parse(page)
    assert got[0]["file"] == "9_CA73of2023.pdf" and got[0]["case_no"] == "Civil Appeal 73/2023" and got[0]["uploaded"] == "25-OCT-26"


def test_status_file_is_written_whole(tmp_path):
    st = Status(tmp_path / "status.json", "job")
    st.update("running", records=3)
    body = json.loads((tmp_path / "status.json").read_text())
    assert body["state"] == "running" and body["records"] == 3 and not (tmp_path / "status.tmp").exists()


def test_subordinate_court_list_cards_are_read():
    from bdlaw_scraper.harvest.decision import names, parse_list

    page = '<!-- Ticket item START --><div><a href="https://decision.bdcourts.gov.bd/home/judgement/abc123="><h5> GR 176/2017 State vs X </h5></a><h6>13 Sep, 2026</h6><p>drugs case</p></div>'
    assert parse_list(page) == [{"token": "abc123=", "title": "GR 176/2017 State vs X", "date": "13 Sep, 2026", "kind": "drugs case"}]

    class Fake:
        def fetch(self, url, data=None, cache=True):
            assert cache is False  # the court lists are never written to disk
            return b'[{"id":"1","name_en":"Barguna","name_bn":"x","login_id":"u","password":"secret"}]'

    assert names(Fake(), "u") == [{"id": "1", "name_en": "Barguna", "name_bn": "x"}]
