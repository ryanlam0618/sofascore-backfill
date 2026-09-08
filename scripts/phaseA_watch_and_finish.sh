#!/bin/bash
# Phase A watcher: waits for all core backfill procs to exit, then runs the
# Stage-4c-style new-endpoints batch (h2h/votes/graph/odds/average-positions/
# best-players) over seasons 15/16 + 16/17. Writes completion marker at end.
set -u
cd /root/.openclaw/workspace/sofascore-backfill
VENV=.runner-venv/bin/python
LOGDIR=logs/phaseA
mkdir -p "$LOGDIR"
echo "watcher start $(date)" > "$LOGDIR/watcher.log"

while pgrep -f "gen4_phaseA_backfill.py --resume" > /dev/null; do
  sleep 60
done
echo "core done $(date) — starting new-endpoints batch" >> "$LOGDIR/watcher.log"

$VENV gen4_stage4c_batch_C.py \
  --season-labels "15/16,16/17" \
  --pool-file data/proxy_pools/good_phaseA_20260909.txt \
  --report-file data/gen4_phaseA_newendpoints_report.json \
  --evidence-file data/gen4_phaseA_newendpoints_evidence.jsonl \
  >> "$LOGDIR/newendpoints.log" 2>&1

echo "PHASE_A_COMPLETE $(date)" >> "$LOGDIR/watcher.log"
