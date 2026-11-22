import sys
from pathlib import Path

JOBS = {"gazette": "bdlaw_scraper.harvest.gazette", "scob": "bdlaw_scraper.harvest.scob", "courts": "bdlaw_scraper.harvest.courts", "decision": "bdlaw_scraper.harvest.decision"}
MIRRORS = ("ain", "bdpil", "bdcode", "dppold", "blast", "nbr", "bb", "datagov", "nitimala", "nasir", "pakistancode", "sayed")

if __name__ == "__main__":
    if len(sys.argv) < 3 or (sys.argv[1] not in JOBS and sys.argv[1] not in MIRRORS):
        raise SystemExit("usage: python -m bdlaw_scraper.harvest {gazette|scob|courts|ain|bdpil|bdcode|dppold|blast|nbr|bb|datagov|nitimala|nasir|pakistancode|sayed|decision} ROOT_DIR")
    import importlib

    if sys.argv[1] in MIRRORS:
        importlib.import_module("bdlaw_scraper.harvest.mirror").run_source(Path(sys.argv[2]), sys.argv[1])
    else:
        importlib.import_module(JOBS[sys.argv[1]]).run(Path(sys.argv[2]))