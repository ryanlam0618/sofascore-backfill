#!/bin/bash
# gen4_phaseB_cup_retry_watcher.sh — waits for the cup-retry backfill PID to exit,
# then runs post-run verification (5-cup scope guard vs baseline + completion summary)
# and writes a DONE marker.
# Usage: nohup ./gen4_phaseB_cup_retry_watcher.sh <pid> > data/gen4_phaseB_cup_retry_watcher.log 2>&1 &
set -u
ROOT="/root/.openclaw/workspace/sofascore-backfill"
PID="$1"
VENV="$ROOT/.runner-venv/bin/python3"
LOG="$ROOT/data/gen4_phaseB_cup_retry_run.log"
MARKER="$ROOT/data/gen4_phaseB_cup_retry_DONE.marker"

echo "[watcher] monitoring pid=$PID start=$(date -u +%FT%TZ)"

# Wait for process exit (up to 13h)
waited=0
while kill -0 "$PID" 2>/dev/null; do
  sleep 60
  waited=$((waited+60))
  if [ $waited -gt 46800 ]; then
    echo "[watcher] TIMEOUT after 13h still running; writing timeout marker" >> "$MARKER"
    exit 3
  fi
done

echo "[watcher] pid=$PID exited at $(date -u +%FT%TZ)"
echo "[watcher] tail of run log:"
tail -40 "$LOG"

# Determine exit code from log sentinel
EXIT_CODE=$(grep -oP 'EXIT_CODE=\K[0-9]+' "$LOG" | tail -1)
echo "[watcher] run EXIT_CODE=$EXIT_CODE"

# Run verification (standalone scope-guard check)
echo "[watcher] running post-run verification..."
"$VENV" "$ROOT/gen4_phaseB_cup_retry_verify_scope.py" >> "$LOG" 2>&1
VERIFY_RC=$?
echo "[watcher] verify rc=$VERIFY_RC"

# Build completion summary (per-comp coverage from report JSON)
export WATCHER_EXIT="$EXIT_CODE"
"$VENV" - <<'PYEOF' >> "$LOG" 2>&1
import json, os
from datetime import datetime, timezone
from pathlib import Path
ROOT = Path('/root/.openclaw/workspace/sofascore-backfill')
try:
    report = json.load(open(ROOT/'data/gen4_phaseB_cup_retry_report.json'))
    comps = [{'comp': c['comp_name'], 'season': c['season_label'], 'sid': c['season_id'],
              'matched_year': c['matched_year'], 'events': c['events_processed'],
              'found': c['total_events_found'], 'failed': c['events_failed'],
              'calls': c['calls_used'], 'deltas': c['row_deltas'],
              'error': c['error']} for c in report['comps']]
    summary = {'timestamp': datetime.now(timezone.utc).isoformat(),
               'run_exit_code': os.environ.get('WATCHER_EXIT',''),
               'total_comps': report['total_comps'], 'total_events': report['total_events'],
               'total_calls': report['total_calls'], 'row_deltas_total': report['row_deltas_total'],
               'known_gaps': report['known_gaps'], 'comps': comps}
    json.dump(summary, open(ROOT/'data/gen4_phaseB_cup_retry_completion_summary.json','w'), indent=2, ensure_ascii=False)
    print('[watcher] wrote completion_summary.json')
except Exception as e:
    print('[watcher] completion summary failed:', e)
try:
    vs = json.load(open(ROOT/'data/gen4_phaseB_cup_retry_scope_verify.json'))
    print('[watcher] scope_guard_ok=%s regressions=%s' % (vs.get('scope_guard_ok'), len(vs.get('regressions',[]))))
except Exception as e:
    print('[watcher] scope_verify read failed:', e)
PYEOF

{
  echo "timestamp=$(date -u +%FT%TZ)"
  echo "run_exit_code=$EXIT_CODE"
  echo "verify_rc=$VERIFY_RC"
} > "$MARKER"
echo "[watcher] wrote marker $MARKER"
exit 0
