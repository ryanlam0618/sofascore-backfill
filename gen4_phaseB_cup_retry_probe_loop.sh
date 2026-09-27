#!/bin/bash
# gen4_phaseB_cup_retry_probe_loop.sh — waits out the SofaScore 403 wall, then signals.
# Runs up to 36 rounds x 20 min (12h). Each round probes 5 pool IPs (rotating
# offset) against the seasons endpoint. First round with any 200 writes
# UNBLOCKED marker; exhausting all rounds writes STILL_BLOCKED marker.
# Designed to be run under setsid so gateway restarts don't kill it.
set -u
ROOT="/root/.openclaw/workspace/sofascore-backfill"
VENV="$ROOT/.runner-venv/bin/python3"
PROBE_LOG="$ROOT/data/gen4_phaseB_cup_retry_probe.log"
UNBLOCKED="$ROOT/data/gen4_phaseB_cup_retry_UNBLOCKED.marker"
STILL_BLOCKED="$ROOT/data/gen4_phaseB_cup_retry_STILL_BLOCKED.marker"

echo "[probe_loop] start $(date -u +%FT%TZ) — 403 wall confirmed at 2026-09-25T14:01Z+14:16Z" >> "$PROBE_LOG"

for round in $(seq 0 35); do
  offset=$(( (round * 5) % 20 ))
  ts=$(date -u +%FT%TZ)
  out=$("$VENV" "$ROOT/gen4_phaseB_cup_retry_probe.py" "$offset" 5 2>&1)
  rc=$?
  echo "[probe_loop] round=$round ts=$ts rc=$rc out=$out" >> "$PROBE_LOG"
  if [ $rc -eq 0 ]; then
    echo "UNBLOCKED at $ts (round=$round)" > "$UNBLOCKED"
    echo "$out" >> "$UNBLOCKED"
    echo "[probe_loop] WALL CLEARED — UNBLOCKED marker written $(date -u +%FT%TZ)" >> "$PROBE_LOG"
    exit 0
  fi
  if [ $round -lt 35 ]; then
    sleep 1200
  fi
done

echo "STILL_BLOCKED after 36 rounds (12h) $(date -u +%FT%TZ)" > "$STILL_BLOCKED"
echo "[probe_loop] STILL BLOCKED after 12h — marker written" >> "$PROBE_LOG"
exit 1
