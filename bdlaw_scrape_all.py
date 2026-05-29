#!/usr/bin/env python3
"""
Batch runner for Bangladesh Law Scraper

Calls:
    python bdlaw_scraper.py --act XX --cache-dir cache/ -o repo/act-XX.md
"""

import subprocess
import time
from pathlib import Path


SCRAPER_SCRIPT = "bdlaw_scraper.py"
CACHE_DIR = "cache"
OUTPUT_DIR = Path("repo")


def run_scraper(act_number: int, delay: float = 0.3) -> None:
    output_file = OUTPUT_DIR / f"act-{act_number}.md"

    # if output_file.exists():
    #     print(f"[SKIP] act-{act_number} already exists")
    #     return

    cmd = [
        "python",
        SCRAPER_SCRIPT,
        "--act", str(act_number),
        "--cache-dir", CACHE_DIR,
        "-o", str(output_file),
    ]

    print(f"[RUN ] act-{act_number}")
    try:
        subprocess.run(cmd, check=True)
        print(f"[ OK ] act-{act_number}")
    except subprocess.CalledProcessError as e:
        print(f"[FAIL] act-{act_number} ({e})")

    time.sleep(delay)


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 🔥 Full range: 1 → 1719
    for act in range(1, 1720):  # 1720 is exclusive
        run_scraper(act)


if __name__ == "__main__":
    main()