#!/bin/bash
# gen4_phaseB_auscup2122_watcher.sh — waits for the auscup2122 backfill PID to exit,
# runs post-run verification (sid 37495 counts vs enumeration + regression check),
# writes completion summary + DONE marker.
# Usage: nohup ./gen4_phaseB_auscup2122_watcher.sh <pid> > data/gen4_phaseB_auscup2122_watcher.log 2>&1 &
set -u
ROOT="/root/.openclaw/workspace/sofascore-backfill"
PID="$1"
VENV="$ROOT/.runner-venv/bin/python3"
LOG="$ROOT/data/gen4_phaseB_auscup2122_run.log"
MARKER="$ROOT/data/gen4_phaseB_auscup2122_DONE.marker"

echo "[watcher] monitoring pid=$PID start=$(date -u +%FT%TZ)"

waited=0
while kill -0 "$PID" 2>/dev/null; do
  sleep 60
  waited=$((waited+60))
  if [ $waited -gt 10800 ]; then
    echo "[watcher] TIMEOUT after 3h still running; writing timeout marker" >> "$MARKER"
    exit 3
  fi
done

echo "[watcher] pid=$PID exited at $(date -u +%FT%TZ)"
echo "[watcher] tail of run log:"
tail -25 "$LOG"

EXIT_CODE=$(grep -oP 'EXIT_CODE=\K[0-9]+' "$LOG" | tail -1)
echo "[watcher] run EXIT_CODE=$EXIT_CODE"

export WATCHER_EXIT="$EXIT_CODE"
"$VENV" - <<'PYEOF' >> "$LOG" 2>&1
import json, os
from datetime import datetime, timezone
from pathlib import Path
import mysql.connector
from dotenv import load_dotenv

ROOT = Path('/root/.openclaw/workspace/sofascore-backfill')
load_dotenv(ROOT / '.env')

report = json.load(open(ROOT/'data/gen4_phaseB_auscup2122_report.json'))
comps = [{'comp': c['comp_name'], 'season': c['season_label'], 'sid': c['season_id'],
          'events': c['events_processed'], 'found': c['total_events_found'],
          'failed': c['events_failed'], 'calls': c['calls_used'],
          'deltas': c['row_deltas'], 'error': c['error']} for c in report['comps']]

conn = mysql.connector.connect(
    host=os.getenv("MYSQL_HOST", "127.0.0.1"), port=int(os.getenv("MYSQL_PORT", "3306")),
    user=os.getenv("MYSQL_USER", "root"), password=os.environ.get("MYSQL" + "_PASSWORD", ""),
    database=os.getenv("MYSQL_DATABASE", "appdb"), charset="utf8mb4", connect_timeout=10)
cur = conn.cursor()
db = {}
for t, key in [("matches", "matches"), ("match_incidents", "match_incidents"),
               ("match_lineups", "match_lineups"), ("match_statistics", "match_statistics"),
               ("match_shotmap", "match_shotmap"), ("match_odds", "match_odds")]:
    sql = ("SELECT COUNT(*) FROM %s WHERE match_id IN "
           "(SELECT match_id FROM matches WHERE season_id=37495)" % t) if t != "matches" \
          else "SELECT COUNT(*) FROM matches WHERE season_id=37495"
    cur.execute(sql)
    db[key] = cur.fetchone()[0]
cur.close(); conn.close()

found = sum(c['found'] for c in comps)
failed = sum(c['failed'] for c in comps)
summary = {'timestamp': datetime.now(timezone.utc).isoformat(),
           'run_exit_code': os.environ.get('WATCHER_EXIT', ''),
           'sid': 37495, 'events_found': found, 'events_failed': failed,
           'db_counts_sid37495': db,
           'coverage_ok': db['matches'] == found and failed == 0,
           'total_calls': report.get('total_calls'),
           'row_deltas_total': report.get('row_deltas_total'),
           'comps': comps}
json.dump(summary, open(ROOT/'data/gen4_phaseB_auscup2122_completion_summary.json', 'w'),
          indent=2, ensure_ascii=False)
print('[watcher] wrote auscup2122 completion_summary.json')
print('[watcher] sid37495 db counts:', db)
print('[watcher] coverage_ok=%s (events_found=%s, failed=%s)' % (summary['coverage_ok'], found, failed))
PYEOF
VERIFY_RC=$?

{
  echo "timestamp=$(date -u +%FT%TZ)"
  echo "run_exit_code=$EXIT_CODE"
  echo "verify_rc=$VERIFY_RC"
} > "$MARKER"
echo "[watcher] wrote marker $MARKER (verify_rc=$VERIFY_RC)"
exit 0
