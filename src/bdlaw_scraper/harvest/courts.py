"""Supreme Court of Bangladesh judgments (Appellate Division and High Court Division).

The public page posts to judgments/judgment_list.php (found in the page's own script); with only a year it returns
every judgment uploaded for that year, so the walk is division x year. PDFs are then fetched one by one.
"""

import re
from datetime import date
from pathlib import Path

from bdlaw_scraper.harvest.gazette import text
from bdlaw_scraper.harvest.kit import Polite, Status, Stop, db

HOST = "www.supremecourt.gov.bd"
LIST = f"https://{HOST}/web/judgments/judgment_list.php"
DOC = f"https://{HOST}/resources/documents/"
SCHEMA = """CREATE TABLE IF NOT EXISTS judgments (
  pdf_url TEXT PRIMARY KEY, div_id INT, year INT, case_no TEXT, row_text TEXT, uploaded TEXT,
  pdf_state TEXT DEFAULT 'todo', pdf_path TEXT, size INT, sha256 TEXT, tries INT DEFAULT 0)"""
_RE_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_RE_DOC = re.compile(r'href="(?:https?://[^"/]+/|\.\./)resources/documents/([^"]+?\.pdf)"')
_RE_UP = re.compile(r"Uploaded on\s*:\s*([0-9A-Za-z-]+)")


def parse(page: str) -> list[dict]:
    out = []
    for tr in _RE_ROW.findall(page):
        m = _RE_DOC.search(tr)
        if not m or "<tr" in tr:
            continue
        body = text(tr)
        anchor = re.search(r"<a[^>]*documents[^>]*>(.*?)</a>", tr, re.S)
        case_no = text(re.split(r"<br\s*/?>", anchor.group(1))[0]) if anchor else ""
        up = _RE_UP.search(body)
        out.append({"file": m.group(1), "case_no": case_no, "row_text": body, "uploaded": up.group(1) if up else ""})
    return out


def year_of(case_no: str, uploaded: str, fallback: int) -> int:
    m = re.search(r"(19|20)\d\d", case_no.split("/")[-1] if "/" in case_no else case_no)
    return int(m.group(0)) if m else fallback


def index(http: Polite, con, st: Status) -> None:
    this_year = date.today().year
    for div in (1, 2):
        for year in list(range(this_year + 1, 1971, -1)) + [0]:
            data = {"div_id": div, "type_id": "", "case_number": "", "year": year or "", "case_type_id": "", "parties": "", "short_desc": ""}
            page = http.fetch(LIST, data).decode("utf-8", "ignore")
            for r in parse(page):
                con.execute("INSERT OR IGNORE INTO judgments (pdf_url,div_id,year,case_no,row_text,uploaded) VALUES (?,?,?,?,?,?)",
                            (DOC + r["file"], div, year_of(r["case_no"], r["uploaded"], year), r["case_no"], r["row_text"], r["uploaded"]))
            con.commit()
            st.update("indexing", judgments=con.execute("SELECT count(*) FROM judgments").fetchone()[0], at=f"div {div} year {year}")


def download(http: Polite, con, root: Path, st: Status) -> None:
    todo = con.execute("SELECT pdf_url,div_id,year FROM judgments WHERE pdf_state IN ('todo','bad') AND tries<3 ORDER BY year DESC").fetchall()
    for i, (url, div, year) in enumerate(todo):
        dest = root / "pdf" / f"div{div}" / str(year) / url.rsplit("/", 1)[-1]
        state, size, sha = http.download(url, dest)
        con.execute("UPDATE judgments SET pdf_state=?, pdf_path=?, size=?, sha256=COALESCE(NULLIF(?,''),sha256), tries=tries+? WHERE pdf_url=?",
                    ("ok" if state in ("ok", "exists") else "bad", str(dest) if state != "bad" else None, size, sha, 1 if state == "bad" else 0, url))
        if i % 25 == 0:
            con.commit()
            c = dict(con.execute("SELECT pdf_state,count(*) FROM judgments GROUP BY 1").fetchall())
            st.update("downloading", **{f"pdf_{k}": v for k, v in c.items()}, remaining=len(todo) - i)
    con.commit()


def run(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    st = Status(root / "status.json", "courts")
    st.update("starting")
    con = db(root / "courts.db")
    con.execute(SCHEMA)
    try:
        http = Polite(root, HOST, delay=1.5)
        index(http, con, st)
        download(http, con, root, st)
        st.update("done")
    except Stop as e:
        st.update("stopped", reason=str(e))
        raise SystemExit(2)
    except BaseException as e:
        st.update("failed", reason=repr(e))
        raise