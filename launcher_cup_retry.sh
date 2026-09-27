#!/bin/bash
# launcher_cup_retry.sh — relaunch cup-retry backfill with EXIT_CODE sentinel for watcher
# (relaunch 2026-09-25 21:5x after 14:59 process death + container restart 21:52;
#  resume via checkpoint: DFB Pokal 20/21 + 21/22 completed = skipped)
set -a
source /root/.openclaw/workspace/sofascore-backfill/.env
set +a
cd /root/.openclaw/workspace/sofascore-backfill
echo "[launcher] relaunch start=$(date -u +%FT%TZ) wrapper_pid=$$" >> data/gen4_phaseB_cup_retry_run.log
.runner-venv/bin/python3 gen4_phaseB_cup_retry_backfill.py >> data/gen4_phaseB_cup_retry_run.log 2>&1
echo "EXIT_CODE=$?" >> data/gen4_phaseB_cup_retry_run.log
