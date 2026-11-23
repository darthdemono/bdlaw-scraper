"""Download the gazette PDFs the dppold crawl has already found, without waiting for the crawl to end.

The crawl and this run share one SQLite file (WAL) and one host, so the delay here is longer than the crawl's.
usage: PYTHONPATH=src python3 scripts/dppold-pdfs.py /mnt/big_chungus/BDLAW-harvest/dppold
"""
import sys
from pathlib import Path

from bdlaw_scraper.harvest.kit import Polite, Status, Stop, db
from bdlaw_scraper.harvest.mirror import SOURCES, download

root = Path(sys.argv[1])
st = Status(root / "status-pdfs.json", "dppold-pdfs")
st.update("starting")
con = db(root / "dppold.db")
try:
    download(Polite(root, SOURCES["dppold"]["host"], delay=2.0), con, root, st)
    st.update("done")
except Stop as e:
    st.update("stopped", reason=str(e))
    raise SystemExit(2)
except BaseException as e:
    st.update("failed", reason=repr(e))
    raise
