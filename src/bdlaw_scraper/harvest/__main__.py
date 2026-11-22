import sys
from pathlib import Path

JOBS = {"gazette": "bdlaw_scraper.harvest.gazette", "scob": "bdlaw_scraper.harvest.scob", "courts": "bdlaw_scraper.harvest.courts"}

if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] not in JOBS:
        raise SystemExit("usage: python -m bdlaw_scraper.harvest {gazette|scob|courts} ROOT_DIR")
    import importlib

    importlib.import_module(JOBS[sys.argv[1]]).run(Path(sys.argv[2]))
