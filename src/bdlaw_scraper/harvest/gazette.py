"""Gazette harvest: every extraordinary and weekly gazette record on gazettes.dpp.gov.bd, then its PDF.

Index first (cheap, a few thousand pages), then PDFs (the bulk). Both steps are resumable: pages come from the disk
cache, records are keyed by PDF URL, a PDF is written to ".part" and renamed when whole.
"""

import html
import re
from pathlib import Path

from bdlaw_scraper.harvest.kit import Polite, Status, Stop, db

BASE = "https://gazettes.dpp.gov.bd"
SCHEMA = """CREATE TABLE IF NOT EXISTS records (
  pdf_url TEXT PRIMARY KEY, kind TEXT, year INT, month INT, sl INT, title TEXT, ministry TEXT, date TEXT,
  source_page TEXT, pdf_state TEXT DEFAULT 'todo', pdf_path TEXT, size INT, sha256 TEXT, tries INT DEFAULT 0)"""
_RE_ROW = re.compile(r"<tr>(.*?)</tr>", re.S)
_RE_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_RE_PDF = re.compile(r'href="(https?://[^"]+?\.pdf)"', re.S)


def text(s: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s)).split())


def rows(page: str) -> list[dict]:
    out = []
    for tr in _RE_ROW.findall(page):
        pdf = _RE_PDF.search(tr)
        tds = _RE_TD.findall(tr)
        if pdf and len(tds) >= 4:
            out.append({"pdf_url": pdf.group(1), "sl": int(text(tds[0]) or 0) if text(tds[0]).isdigit() else 0,
                        "title": text(tds[1]), "ministry": text(tds[2]), "date": text(tds[3])})
    return out


def pages_of(listing: str, pattern: str) -> list[str]:
    return list(dict.fromkeys(re.findall(pattern, listing)))


def detail_rows(http: Polite, con, url: str, kind: str, year: int, month: int) -> int:
    """All records of one detail page and its ?page=N continuations; stops when a page adds nothing new."""
    added, seen = 0, set()
    for n in range(1, 400):
        page = http.fetch(url if n == 1 else f"{url}?page={n}").decode("utf-8", "ignore")
        got = [r for r in rows(page) if r["pdf_url"] not in seen]
        if not got:
            break
        for r in got:
            seen.add(r["pdf_url"])
            cur = con.execute("INSERT OR IGNORE INTO records (pdf_url,kind,year,month,sl,title,ministry,date,source_page)"
                              " VALUES (?,?,?,?,?,?,?,?,?)", (r["pdf_url"], kind, year, month, r["sl"], r["title"], r["ministry"], r["date"], url))
            added += cur.rowcount
        con.commit()
    return added


def index(http: Polite, con, st: Status) -> None:
    n = 0
    years = []
    for p in range(1, 12):  # extraordinary: yearly list pages -> months -> detail pages
        page = http.fetch(f"{BASE}/extraordinary-gazettes-yearly" + (f"?page={p}" if p > 1 else "")).decode("utf-8", "ignore")
        new = [y for y in pages_of(page, r"extraordinary-gazettes-monthly/(\d+)") if y not in years]
        if not new:
            break
        years += new
    for y in sorted(years, key=int, reverse=True):
        mpage = http.fetch(f"{BASE}/extraordinary-gazettes-monthly/{y}").decode("utf-8", "ignore")
        for m in sorted(set(pages_of(mpage, rf"extraordinary-gazette-detail/(\d+)/{y}\b")), key=int):
            n += detail_rows(http, con, f"{BASE}/extraordinary-gazette-detail/{m}/{y}", "extraordinary", int(y), int(m))
            st.update("indexing", records=con.execute("SELECT count(*) FROM records").fetchone()[0], at=f"extraordinary {y}-{m}")
    for p in range(1, 400):  # weekly gazettes
        page = http.fetch(f"{BASE}/weekly-gazettes" + (f"?page={p}" if p > 1 else "")).decode("utf-8", "ignore")
        links = pages_of(page, r"weekly-gazette-detail/(\d+/\d+/\d+)")
        if not links:
            break
        for ref in links:
            a, b, y = ref.split("/")
            detail_rows(http, con, f"{BASE}/weekly-gazette-detail/{ref}", "weekly", int(y), int(b))
        st.update("indexing", records=con.execute("SELECT count(*) FROM records").fetchone()[0], at=f"weekly page {p}")


def download(http: Polite, con, root: Path, st: Status) -> None:
    todo = con.execute("SELECT pdf_url,kind,year FROM records WHERE pdf_state IN ('todo','bad') AND tries<3 ORDER BY year DESC, pdf_url").fetchall()
    for i, (url, kind, year) in enumerate(todo):
        dest = root / "pdf" / kind / str(year) / url.rsplit("/", 1)[-1]
        state, size, sha = http.download(url, dest)
        con.execute("UPDATE records SET pdf_state=?, pdf_path=?, size=?, sha256=COALESCE(NULLIF(?,''),sha256), tries=tries+? WHERE pdf_url=?",
                    ("ok" if state in ("ok", "exists") else "bad", str(dest) if state != "bad" else None, size, sha, 1 if state == "bad" else 0, url))
        if i % 25 == 0:
            con.commit()
            c = dict(con.execute("SELECT pdf_state,count(*) FROM records GROUP BY 1").fetchall())
            st.update("downloading", **{f"pdf_{k}": v for k, v in c.items()}, remaining=len(todo) - i)
    con.commit()


def run(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    st = Status(root / "status.json", "gazette")
    st.update("starting")
    con = db(root / "gazette.db")
    con.execute(SCHEMA)
    try:
        http = Polite(root, "gazettes.dpp.gov.bd", delay=1.0)
        index(http, con, st)
        download(http, con, root, st)
        st.update("done")
    except Stop as e:
        st.update("stopped", reason=str(e))
        raise SystemExit(2)
    except BaseException as e:
        st.update("failed", reason=repr(e))
        raise
