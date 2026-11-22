"""Subordinate court judgments (decision.bdcourts.gov.bd): every court, every judgment page.

The site lists a court's judgments at home/get_jmnts/<division>-<district>-<court>-<type>?page=N (5 a page; a page
past the end repeats the last one, so a page that adds nothing new ends the walk) and serves each judgment as an HTML
page whose text is in the Bijoy ANSI font encoding (decoded later, in the refining step).

The district and court lists come back as JSON that also carries login names and passwords for the court accounts.
Those fields are never kept: the JSON is fetched without caching and only ids and names are read.
"""

import json
import re
from pathlib import Path

from bdlaw_scraper.harvest.gazette import text
from bdlaw_scraper.harvest.kit import Polite, Status, Stop, db

HOST = "decision.bdcourts.gov.bd"
BASE = f"https://{HOST}/home"
SCHEMA = """CREATE TABLE IF NOT EXISTS judgments (token TEXT PRIMARY KEY, division INT, district INT, court INT, ctype INT,
  title TEXT, date TEXT, kind TEXT, detail_state TEXT DEFAULT 'todo', tries INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS walked (combo TEXT PRIMARY KEY, pages INT, found INT)"""
_RE_CARD = re.compile(r"Ticket item START(.*?)(?=Ticket item START|$)", re.S)


def parse_list(page: str) -> list[dict]:
    out = []
    for card in _RE_CARD.findall(page):
        tok = re.search(r"home/judgement/([\w=%\-]+)", card)
        if not tok:
            continue
        h5, h6 = re.search(r"<h5[^>]*>(.*?)</h5>", card, re.S), re.search(r"<h6[^>]*>(.*?)</h6>", card, re.S)
        kinds = [text(p) for p in re.findall(r"<p[^>]*>(.*?)</p>", card, re.S) if text(p)]
        out.append({"token": tok.group(1), "title": text(h5.group(1)) if h5 else "", "date": text(h6.group(1)) if h6 else "", "kind": " | ".join(kinds)})
    return out


def names(http: Polite, url: str) -> list[dict]:
    """[{id, name_en, name_bn}] from a JSON endpoint; any credential field in the answer is dropped on the spot."""
    try:
        rows = json.loads(http.fetch(url, cache=False).decode("utf-8", "ignore") or "[]")
    except ValueError:
        return []
    return [{"id": r["id"], "name_en": r.get("name_en", ""), "name_bn": r.get("name_bn", "")} for r in rows if "id" in r]


def walk(http: Polite, con, div: int, dis: int, court: int, ctype: int) -> int:
    combo = f"{div}-{dis}-{court}-{ctype}"
    if con.execute("SELECT 1 FROM walked WHERE combo=?", (combo,)).fetchone():
        return -1
    seen, n = set(), 0
    for n in range(1, 400):
        got = parse_list(http.fetch(f"{BASE}/get_jmnts/{combo}?page={n}").decode("utf-8", "ignore"))
        fresh = [g for g in got if g["token"] not in seen]
        if not fresh:
            n -= 1
            break
        for g in fresh:
            seen.add(g["token"])
            con.execute("INSERT OR IGNORE INTO judgments (token,division,district,court,ctype,title,date,kind) VALUES (?,?,?,?,?,?,?,?)",
                        (g["token"], div, dis, court, ctype, g["title"], g["date"], g["kind"]))
    con.execute("INSERT OR REPLACE INTO walked (combo,pages,found) VALUES (?,?,?)", (combo, n, len(seen)))
    con.commit()
    return len(seen)


def details(http: Polite, con, st: Status) -> None:
    for (tok,) in con.execute("SELECT token FROM judgments WHERE detail_state!='ok' AND tries<3").fetchall():
        body = http.fetch(f"{BASE}/judgement/{tok}")
        con.execute("UPDATE judgments SET detail_state=?, tries=tries+? WHERE token=?", ("ok" if body else "bad", 0 if body else 1, tok))
    con.commit()


def run(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    st = Status(root / "status.json", "decision")
    st.update("starting")
    con = db(root / "decision.db")
    con.executescript(SCHEMA)
    try:
        http = Polite(root, HOST, delay=1.5)
        for div in range(1, 9):
            for d in names(http, f"{BASE}/get_district/{div}"):
                for c in names(http, f"{BASE}/get_court/{d['id']}"):
                    found = walk(http, con, div, int(d["id"]), int(c["id"]), 0)
                    if found >= 150:  # a court with a long list: split it by case type as well
                        for ctype in (1, 2, 3, 4):
                            walk(http, con, div, int(d["id"]), int(c["id"]), ctype)
                    details(http, con, st)
                    st.update("harvesting", judgments=con.execute("SELECT count(*) FROM judgments").fetchone()[0],
                              pages_saved=con.execute("SELECT count(*) FROM judgments WHERE detail_state='ok'").fetchone()[0],
                              at=f"{d['name_en']} / {c['name_en']}"[:80])
        st.update("done")
    except Stop as e:
        st.update("stopped", reason=str(e))
        raise SystemExit(2)
    except BaseException as e:
        st.update("failed", reason=repr(e))
        raise
