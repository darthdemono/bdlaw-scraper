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
    "blast": {"host": "blast.org.bd", "sitemaps": ["https://blast.org.bd/sitemap_index.xml"], "follow": False, "delay": 4.0},
    "nbr": {"host": "nbr.gov.bd", "seeds": ["https://nbr.gov.bd/", "https://nbr.gov.bd/regulations/acts/vat-acts/eng"], "follow": True, "delay": 1.5,
            "follow_re": r"nbr\.gov\.bd/(regulations|laws|acts|sro|rules|circular|order)"},
    "bb": {"host": "www.bb.org.bd", "seeds": ["https://www.bb.org.bd/en/index.php/mediaroom/circular"], "follow": False, "delay": 2.0},
    "datagov": {"host": "data.gov.bd", "follow": True, "delay": 10.0,
                "seeds": ["https://data.gov.bd/dataset?query=&sort_by=changed&sort_order=DESC&page=0",
                          "https://data.gov.bd/dataset?query=&sort_by=changed&sort_order=DESC&page=0%2C1"],
                "paged_n": "https://data.gov.bd/dataset?query=&sort_by=changed&sort_order=DESC&page=0%2C{n}",
                "follow_re": r"data\.gov\.bd/dataset/[^/?#]+(/resource/[0-9a-f-]{36})?$",
                "file_re": r"/api/download/",
                "exts": ["csv", "xls", "xlsx", "json", "xml", "pdf", "zip", "doc", "docx", "ods"]},
    "dppold": {"host": "www.dpp.gov.bd", "paged": "https://www.dpp.gov.bd/bgpress/index.php/document/gazettes/{t}/publication_date/{o}", "types": [1, 2], "step": 15, "follow": True, "delay": 1.5,
               "follow_re": r"/document/(get_extraordinary|get_gazette_part)/"},
    "nitimala": {"host": "laws.nitimala.com", "sitemaps": ["https://laws.nitimala.com/sitemap.xml"], "follow": False, "delay": 2.0},
    "sayed": {"host": "laws.sayed.app", "sitemaps": ["https://laws.sayed.app/sitemap.xml"], "follow": False, "delay": 2.0},
    "bengalcode": {"host": "archive.org", "follow": False, "delay": 5.0,
                   "seeds": ["https://archive.org/download/in.ernet.dli.2015.110208/2015.110208.The-Regulations-Of-The-Bengal-Code_djvu.txt",
                             "https://archive.org/download/wbsl.19456/19456_djvu.txt",
                             "https://archive.org/download/unrepealedgener00deptgoog/unrepealedgener00deptgoog_djvu.txt",
                             "https://archive.org/download/dli.bengal.10689.19201/10689.19201_djvu.txt",
                             "https://archive.org/download/in.ernet.dli.2015.35821/2015.35821.The-Bengal-Code--Vol-1_djvu.txt"]},
    "alwakeelo": {"host": "www.alwakeelo.com", "sitemaps": ["https://www.alwakeelo.com/sitemap-statutes-1.xml"], "follow": False, "delay": 2.0},
    "nasir": {"host": "nasirlawsite.com", "seeds": ["https://nasirlawsite.com/"], "follow": True, "delay": 20.0,
              "follow_re": r"nasirlawsite\.com/.*\.(?:htm|html|php)$"},
    "pakistancode": {"host": "pakistancode.gov.pk", "follow": True, "delay": 3.0, "insecure": True,
                     "seeds": [f"https://pakistancode.gov.pk/english/LGu0xBD?year={y}&page=1&action={a}" for y in range(1834, 1972) for a in ("inactive", "active")],
                     "follow_re": r"pakistancode\.gov\.pk/english/(UY2Fq|LGu0xBD\?year=)"},
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
        for t in cfg.get("types", []):
            urls.append(cfg["paged"].format(t=t, o=0))
        con.executemany("INSERT OR IGNORE INTO pages (url) VALUES (?)", [(u,) for u in urls])
        con.commit()
    while True:
        row = con.execute("SELECT url,depth FROM pages WHERE state='todo' ORDER BY depth, rowid LIMIT 1").fetchone()
        if not row:
            return
        url, depth = row
        page = http.fetch(url).decode("utf-8", "ignore")
        fresh = 0
        for href in _RE_HREF.findall(page):
            full = urldefrag(urljoin(url, href))[0]
            if re.search(r"[\s<>\"\\{}|^`]|[\x00-\x1f]", full):
                continue  # code or markup that leaked into an href, not a link
            exts = tuple("." + e for e in cfg.get("exts", ["pdf"]))
            if full.lower().split("?")[0].endswith(exts) or (cfg.get("file_re") and re.search(cfg["file_re"], full)):
                con.execute("INSERT OR IGNORE INTO files (url,found_on) VALUES (?,?)", (full, url))
            elif cfg["follow"] and urlparse(full).netloc.endswith(cfg["host"]) and depth < 3 and not re.search(r"\.(css|js|png|jpe?g|gif|ico|zip)$", full, re.I):
                if cfg.get("follow_re") and not re.search(cfg["follow_re"], full):
                    continue
                cur = con.execute("INSERT OR IGNORE INTO pages (url,depth) VALUES (?,?)", (full, depth + 1))
                fresh += cur.rowcount
        pn = re.search(r"[?&]page=(?:0%2C)?(\d+)$", url) if cfg.get("paged_n") else None
        if pn and fresh:  # a list page that still brought new datasets: ask for the next one
            con.execute("INSERT OR IGNORE INTO pages (url,depth) VALUES (?,0)", (cfg["paged_n"].format(n=int(pn.group(1)) + 1),))
        m = re.search(r"/document/gazettes/(\d+)/publication_date/(\d+)$", url) if cfg.get("paged") else None
        if m and re.search(cfg["follow_re"], page):  # a list page with records: ask for the next offset
            con.execute("INSERT OR IGNORE INTO pages (url,depth) VALUES (?,0)", (cfg["paged"].format(t=m.group(1), o=int(m.group(2)) + cfg["step"]),))
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
        state, size, sha = http.download(url, dest, magic=b"%PDF" if url.lower().split("?")[0].endswith(".pdf") else None)
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
        http = Polite(root, cfg["host"], delay=cfg["delay"], insecure=cfg.get("insecure", False))
        crawl(http, con, cfg, st)
        download(http, con, root, st)
        st.update("done")
    except Stop as e:
        st.update("stopped", reason=str(e))
        raise SystemExit(2)
    except BaseException as e:
        st.update("failed", reason=repr(e))
        raise