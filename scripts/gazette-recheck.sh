#!/usr/bin/env bash
# Daily: probe one gazette PDF. On HTTP 200 with a PDF body, reset 'bad' rows to 'todo' and restart the harvest job.
set -u
ROOT=/mnt/big_chungus/BDLAW-harvest/gazette
LOG=$ROOT/recheck.log
say() { echo "$(date -u +%FT%TZ) $*" >> "$LOG"; }
UNIT=bdlaw-harvest-gazette
systemctl --user is-active --quiet "$UNIT" && { say "job running, skip"; exit 0; }
url=$(python3 -I - <<'P'
import sqlite3
c=sqlite3.connect('file:/mnt/big_chungus/BDLAW-harvest/gazette/gazette.db?mode=ro',uri=True,timeout=120)
print(c.execute("select pdf_url from records order by year desc limit 1").fetchone()[0])
P
)
head=$(curl -s -m 60 -A 'bdlaw-harvest recheck' "$url" | head -c 4)
if [ "$head" != "%PDF" ]; then say "still broken: $url"; exit 0; fi
say "PDF served: $url, resetting bad rows and restarting"
python3 -I - <<'P'
import sqlite3
c=sqlite3.connect('/mnt/big_chungus/BDLAW-harvest/gazette/gazette.db',timeout=120)
n=c.execute("update records set pdf_state='todo', tries=0 where pdf_state='bad'").rowcount
c.commit(); print(n)
P
systemd-run --user --unit=$UNIT --setenv=PYTHONPATH=/home/darthdemono/Code/penalcode-harvest/src --working-directory=/home/darthdemono/Code/penalcode-harvest python3 -m bdlaw_scraper.harvest gazette "$ROOT" >> "$LOG" 2>&1
say "restarted"
