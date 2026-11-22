"""Supreme Court Online Bulletin (SCOB): whole-issue PDFs plus the per-case PDFs, with the "Key Ratio" text.

Issue list pages carry, for every reported case, the parties and a one-sentence key ratio: ready-made material for
the judicial dictionary, so the list pages are kept in the cache and the text goes into the database.
"""

import re
from pathlib import Path
from urllib.parse import urljoin

from bdlaw_scraper.harvest.gazette import text
from bdlaw_scraper.harvest.kit import Polite, Status, Stop, db

HOST = "www.supremecourt.gov.bd"
START = f"https://{HOST}/web/index.php?lang=&menu=10&page=bulletin.php"
SCHEMA = """CREATE TABLE IF NOT EXISTS bulletin (
  pdf_url TEXT PRIMARY KEY, kind TEXT, issue INT, div_id INT, title TEXT, key_ratio TEXT, source_page TEXT,
  pdf_state TEXT DEFAULT 'todo', pdf_path TEXT, size INT, sha256 TEXT, tries INT DEFAULT 0)"""
_RE_LIST = re.compile(r'href="(\?page=bulletin/[^"]*bulletin_list[^"]*issue=(\d+))"')
_RE_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_RE_PDF = re.compile(r'href="([^"]*resources/bulletin/[^"]+?\.pdf)"')


def index(http: Polite, con, st: Status) -> None:
    start = http.fetch(START).decode("utf-8", "ignore")
    base = f"https://{HOST}/web/"
    for rel in dict.fromkeys(_RE_PDF.findall(start)):
        m = re.match(r"(\d+)_SCOB_", rel.rsplit("/", 1)[-1])
        con.execute("INSERT OR IGNORE INTO bulletin (pdf_url,kind,issue,source_page,title) VALUES (?,?,?,?,?)",
                    (urljoin(base, rel), "issue", int(m.group(1)) if m else None, START, rel.rsplit("/", 1)[-1]))
    for rel, issue in dict.fromkeys(_RE_LIST.findall(start)):
        url = urljoin(base, rel.replace("&amp;", "&"))
        div = 1 if "div_id=1" in url else 2
        page = http.fetch(url).decode("utf-8", "ignore")
        for tr in _RE_ROW.findall(page):
            pdf = _RE_PDF.search(tr)
            if pdf and "<tr" not in tr:
                cells = [text(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
                con.execute("INSERT OR IGNORE INTO bulletin (pdf_url,kind,issue,div_id,title,key_ratio,source_page) VALUES (?,?,?,?,?,?,?)",
                            (urljoin(url, pdf.group(1)), "case", int(issue), div, cells[1] if len(cells) > 1 else "", cells[2] if len(cells) > 2 else "", url))
        con.commit()
        st.update("indexing", pdfs=con.execute("SELECT count(*) FROM bulletin").fetchone()[0], at=url[-60:])


def download(http: Polite, con, root: Path, st: Status) -> None:
    todo = con.execute("SELECT pdf_url,issue FROM bulletin WHERE pdf_state IN ('todo','bad') AND tries<3 ORDER BY issue DESC").fetchall()
    for i, (url, issue) in enumerate(todo):
        name = re.sub(r"[^\w.\-]+", "_", url.split("resources/bulletin/")[-1])
        dest = root / "pdf" / f"issue{issue}" / name
        state, size, sha = http.download(url, dest)
        con.execute("UPDATE bulletin SET pdf_state=?, pdf_path=?, size=?, sha256=COALESCE(NULLIF(?,''),sha256), tries=tries+? WHERE pdf_url=?",
                    ("ok" if state in ("ok", "exists") else "bad", str(dest) if state != "bad" else None, size, sha, 1 if state == "bad" else 0, url))
        if i % 25 == 0:
            con.commit()
            c = dict(con.execute("SELECT pdf_state,count(*) FROM bulletin GROUP BY 1").fetchall())
            st.update("downloading", **{f"pdf_{k}": v for k, v in c.items()}, remaining=len(todo) - i)
    con.commit()


def run(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    st = Status(root / "status.json", "scob")
    st.update("starting")
    con = db(root / "scob.db")
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
