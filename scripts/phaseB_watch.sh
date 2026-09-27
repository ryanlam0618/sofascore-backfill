#!/bin/bash
# Phase B 17/18 watcher — bulletproof, ALWAYS exits 0.
# Prints a compact status block; caller decides what to report.
set +e
cd /root/.openclaw/workspace/sofascore-backfill 2>/dev/null

echo "=== PhaseB watch $(date '+%F %T %Z') ==="

echo "-- process --"
pgrep -af gen4_phaseB_backfill.py || echo "PROCESS_NOT_RUNNING"

echo "-- evidence --"
EV=/tmp/gen4_phaseB/evidence_17-18.jsonl
if [ -f "$EV" ]; then
  echo "evidence_lines=$(wc -l < "$EV")"
  echo "403_count=$(grep -c '"http": 403' "$EV" 2>/dev/null)"
  echo "last_event: $(tail -1 "$EV" | head -c 300)"
else
  echo "evidence file not found"
fi

echo "-- report --"
if [ -f data/gen4_phaseB_report_17-18.json ]; then
  echo "REPORT_EXISTS"
  head -c 500 data/gen4_phaseB_report_17-18.json
else
  echo "REPORT_NOT_YET (normal)"
fi

echo "-- log tail --"
tail -5 logs/phaseB_17-18_20260915_094016.log 2>/dev/null

echo "==="
exit 0
