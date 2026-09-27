#!/bin/bash
# gen4_phaseB_2021-25_watcher.sh — waits for the backfill PID to exit, then runs
# post-run verification (2021-25 growth + scope guard + match-row coverage) and
# writes a marker. Usage: nohup ./gen4_phaseB_2021-25_watcher.sh <pid> > watcher.log 2>&1 &
set -u
ROOT="/root/.openclaw/workspace/sofascore-backfill"
PID="$1"
VENV="$ROOT/.runner-venv/bin/python"
LOG="$ROOT/data/gen4_phaseB_2021-25_run.log"
PIDFILE="$ROOT/data/gen4_phaseB_2021-25_pid.txt"
MARKER="$ROOT/data/gen4_phaseB_2021-25_DONE.marker"

echo "[watcher] monitoring pid=$PID start=$(date -u +%FT%TZ)"

# Wait for process exit (up to 26h)
waited=0
while kill -0 "$PID" 2>/dev/null; do
  sleep 60
  waited=$((waited+60))
  if [ $waited -gt 259200 ]; then
    echo "[watcher] TIMEOUT after 72h still running; writing timeout marker" >> "$MARKER"
    exit 3
  fi
done

echo "[watcher] pid=$PID exited at $(date -u +%FT%TZ)"
echo "[watcher] tail of run log:"
tail -40 "$LOG"

# Determine exit code from log sentinel
EXIT_CODE=$(grep -oP 'EXIT_CODE=\K[0-9]+' "$LOG" | tail -1)
echo "[watcher] run EXIT_CODE=$EXIT_CODE"

# Run verification (standalone scope-guard + growth + match-row check)
echo "[watcher] running post-run verification..."
"$VENV" gen4_phaseB_2021-25_verify_scope.py >> "$LOG" 2>&1
VERIFY_RC=$?
echo "[watcher] verify rc=$VERIFY_RC"

# Build completion summary (append, keep run log intact)
export WATCHER_EXIT="$EXIT_CODE"
"$VENV" - <<'PYEOF' >> "$LOG" 2>&1
import json, os
from pathlib import Path
ROOT = Path('/root/.openclaw/workspace/sofascore-backfill')
try:
    report = json.load(open(ROOT/'data/gen4_phaseB_2021-25_backfill_report.json'))
    comps = [{'comp': c['comp_name'], 'season': c['season_label'], 'sid': c['season_id'],
              'matched': c.get('matched_year'), 'events': c['events_processed'],
              '/': c['total_events_found'], 'failed': c['events_failed'],
              'calls': c['calls_used'], 'deltas': c['row_deltas'],
              'minute_null': c.get('minute_null_incidents'), 'error': c.get('error')}
             for c in report['comps']]
    summary = {'timestamp': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
               'run_exit_code': os.environ.get('WATCHER_EXIT',''),
               'total_comps': report['total_comps'], 'total_events': report['total_events'],
               'total_calls': report['total_calls'], 'row_deltas_total': report['row_deltas_total'],
               'known_gaps': report.get('known_gaps', []),
               'comps': comps}
    json.dump(summary, open(ROOT/'data/gen4_phaseB_2021-25_completion_summary.json','w'), indent=2, ensure_ascii=False)
    print('[watcher] wrote completion_summary.json')
except Exception as e:
    print('[watcher] completion summary failed:', e)
try:
    vs = json.load(open(ROOT/'data/gen4_phaseB_2021-25_scope_verify.json'))
    print('[watcher] scope_guard_ok=%s regressions=%s all_match_rows_ok=%s' % (
        vs.get('scope_guard_ok'), len(vs.get('regressions',[])), vs.get('all_match_rows_ok')))
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
