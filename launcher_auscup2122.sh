#!/bin/bash
# launcher_auscup2122.sh — launch AusCup 21/22 (sid 37495) mini-backfill with
# EXIT_CODE sentinel for the watcher. Approved by Main Agent 2026-09-26 (decision A).
set -a
. /root/.openclaw/workspace/sofascore-backfill/.env
set +a
cd /root/.openclaw/workspace/sofascore-backfill
echo "[launcher] auscup2122 start=$(date -u +%FT%TZ) wrapper_pid=$$" >> data/gen4_phaseB_auscup2122_run.log
.runner-venv/bin/python3 gen4_phaseB_auscup2122_backfill.py >> data/gen4_phaseB_auscup2122_run.log 2>&1
echo "EXIT_CODE=$?" >> data/gen4_phaseB_auscup2122_run.log
