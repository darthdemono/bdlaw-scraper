"""Generic mirror: cache every page of a small site (from its sitemaps or from seed pages) and download its PDFs.

Used for ain.bd (judgment index), bdpil.org (public interest litigation cases) and bdcode.gov.bd (Bangladesh Code
volumes). Pages stay in the disk cache so parsing can happen later; nothing here tries to understand them.
"""

import re
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

from bdlaw_scraper.harvest.kit import Polite, Status, Stop, db

SOURCES = {
    "ain": {"host": "ain.bd", "sitemaps": ["https://ain.bd/sitemap_index.xml"], "follow": False, "delay": 1.5},
    "bdpil": {"host": "bdpil.org", "sitemaps": ["https://bdpil.org/wp-sitemap.xml"], "follow": False, "delay": 1.5},
    "bdcode": {"host": "bdcode.gov.bd", "seeds": ["https://bdcode.gov.bd/", "https://bdcode.gov.bd/about-bangladesh-code.html",
               "https://bdcode.gov.bd/bangladesh-code-chronological-index.html", "https://bdcode.gov.bd/bangladesh-code-alphabetical-index.html"],
               "follow": True, "delay": 1.5},
}
SCHEMA = """CREATE TABLE IF NOT EXISTS pages (url TEXT PRIMARY KEY, state TEXT DEFAULT 'todo', depth INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS files (url TEXT PRIMARY KEY, found_on TEXT, state TEXT DEFAULT 'todo', path TEXT, size INT, sha256 TEXT, tries INT DEFAULT 0)"""
_RE_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")
_RE_HREF = re.compile(r'href=["\']([^"\'#]+(?:#[^"\']*)?)["\']', re.I)


def sitemap_urls(http: Polite, url: str, depth: int = 0) -> list[str]:
    out = []
    for loc in _RE_LOC.findall(http.fetch(url).decode("utf-8", "ignore")):
        if loc.endswith(".xml") and depth < 3:
            out += sitemap_urls(http, loc, depth + 1)
        else:
            out.append(loc)
    return out


def crawl(http: Polite, con, cfg: dict, st: Status) -> None:
    if not con.execute("SELECT 1 FROM pages LIMIT 1").fetchone():
        urls = [u for sm in cfg.get("sitemaps", []) for u in sitemap_urls(http, sm)] + cfg.get("seeds", [])
        con.executemany("INSERT OR IGNORE INTO pages (url) VALUES (?)", [(u,) for u in urls])
        con.commit()
    while True:
        row = con.execute("SELECT url,depth FROM pages WHERE state='todo' ORDER BY depth, rowid LIMIT 1").fetchone()
        if not row:
            return
        url, depth = row
        page = http.fetch(url).decode("utf-8", "ignore")
        for href in _RE_HREF.findall(page):
            full = urldefrag(urljoin(url, href))[0]
            if full.lower().endswith(".pdf"):
                con.execute("INSERT OR IGNORE INTO files (url,found_on) VALUES (?,?)", (full, url))
            elif cfg["follow"] and urlparse(full).netloc.endswith(cfg["host"]) and depth < 3 and not re.search(r"\.(css|js|png|jpe?g|gif|ico|zip)$", full, re.I):
                con.execute("INSERT OR IGNORE INTO pages (url,depth) VALUES (?,?)", (full, depth + 1))
        con.execute("UPDATE pages SET state='done' WHERE url=?", (url,))
        done = con.execute("SELECT count(*) FROM pages WHERE state='done'").fetchone()[0]
        if done % 20 == 0:
            con.commit()
            st.update("crawling", pages_done=done, pages_todo=con.execute("SELECT count(*) FROM pages WHERE state='todo'").fetchone()[0],
                      pdfs_found=con.execute("SELECT count(*) FROM files").fetchone()[0])


def download(http: Polite, con, root: Path, st: Status) -> None:
    todo = con.execute("SELECT url FROM files WHERE state IN ('todo','bad') AND tries<3").fetchall()
    for i, (url,) in enumerate(todo):
        p = urlparse(url)
        dest = root / "pdf" / p.netloc / re.sub(r"[^\w.\-/]+", "_", p.path.lstrip("/"))
        state, size, sha = http.download(url, dest)
        con.execute("UPDATE files SET state=?, path=?, size=?, sha256=COALESCE(NULLIF(?,''),sha256), tries=tries+? WHERE url=?",
                    ("ok" if state in ("ok", "exists") else "bad", str(dest) if state != "bad" else None, size, sha, 1 if state == "bad" else 0, url))
        if i % 25 == 0:
            con.commit()
            st.update("downloading", remaining=len(todo) - i)
    con.commit()


def run_source(root: Path, name: str) -> None:
    cfg = SOURCES[name]
    root.mkdir(parents=True, exist_ok=True)
    st = Status(root / "status.json", name)
    st.update("starting")
    con = db(root / f"{name}.db")
    con.executescript(SCHEMA)
    try:
        http = Polite(root, cfg["host"], delay=cfg["delay"])
        crawl(http, con, cfg, st)
        download(http, con, root, st)
        st.update("done")
    except Stop as e:
        st.update("stopped", reason=str(e))
        raise SystemExit(2)
    except BaseException as e:
        st.update("failed", reason=repr(e))
        raise
