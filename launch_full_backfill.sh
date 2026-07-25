#!/bin/bash
# SofaScore Backfill — Full Parallel Launch
# 24 competitions × 2 batches = 48 processes
# Batch 1: 2015-2020, Batch 2: 2021-2025

cd /root/.openclaw/workspace/sofascore-backfill

VENV=".runner-venv/bin/python3"
LOGDIR="logs/backfill"
mkdir -p "$LOGDIR"

COMPETITIONS=(
  "Premier League"
  "La Liga"
  "Serie A"
  "Bundesliga"
  "Ligue 1"
  "J1 League"
  "K League 1"
  "A-League Men"
  "Chinese Super League"
  "UCL"
  "UEL"
  "UECL"
  "AFC Champions League"
  "AFC Champions League Two"
  "FA Cup"
  "EFL Cup"
  "Copa del Rey"
  "Coppa Italia"
  "Coupe de France"
  "DFB Pokal"
  "J.League Cup"
  "Emperor's Cup"
  "Australia Cup"
  "Chinese FA Cup"
)

echo "=== SofaScore Backfill Full Launch ==="
echo "Start time: $(date)"
echo "Total processes: ${#COMPETITIONS[@]} competitions × 2 batches = 48"
echo ""

for comp in "${COMPETITIONS[@]}"; do
  # Slugify competition name for session key and log file
  slug=$(echo "$comp" | tr '[:upper:]' '[:lower:]' | tr ' ' '-' | tr -cd 'a-z0-9-')

  # Batch 1: 2015-2020
  key1="sofa-b1-${slug}"
  log1="${LOGDIR}/batch1_${slug}.log"
  echo "Launching Batch 1: $comp (2015-2020) → $log1"
  setsid nohup $VENV backfill_runner.py \
    --competition "$comp" \
    --from-year 2015 --to-year 2020 \
    --sticky-session-key "$key1" \
    > "$log1" 2>&1 &

  # Batch 2: 2021-2025
  key2="sofa-b2-${slug}"
  log2="${LOGDIR}/batch2_${slug}.log"
  echo "Launching Batch 2: $comp (2021-2025) → $log2"
  setsid nohup $VENV backfill_runner.py \
    --competition "$comp" \
    --from-year 2021 --to-year 2025 \
    --sticky-session-key "$key2" \
    > "$log2" 2>&1 &

  # Small stagger between competition pairs to avoid thundering herd
  sleep 2
done

echo ""
echo "=== All 48 processes launched ==="
echo "End time: $(date)"
echo "Monitor with: tail -f logs/backfill/batch*.log"
echo "Check status: ls -la logs/smoke_status_*.json"
