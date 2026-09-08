#!/bin/bash
# Phase A launcher — one process per competition (2 seasons each in-process),
# staggered 5s; then a watcher runs Stage-4c-style new-endpoint batch for
# season labels 15/16,16/17 once all core procs are done.
# Kris green light: 2026-09-09 00:36 GMT+8. Engine: Gen4 fp-v2 via gen4_phaseA_backfill.py.
set -u
cd /root/.openclaw/workspace/sofascore-backfill
VENV=.runner-venv/bin/python
LOGDIR=logs/phaseA
mkdir -p "$LOGDIR" /tmp/gen4_phaseA

COMPS=(
"Premier League" "La Liga" "Serie A" "Bundesliga" "Ligue 1" "J1 League"
"K League 1" "A-League Men" "Chinese Super League" "UCL" "UEL"
"AFC Champions League" "AFC Champions League Two" "FA Cup" "EFL Cup"
"Copa del Rey" "Coppa Italia" "Coupe de France" "DFB Pokal" "J.League Cup"
"Emperor's Cup" "Australia Cup" "Chinese FA Cup"
)
# UECL intentionally absent (did not exist pre-21/22).

for comp in "${COMPS[@]}"; do
  slug=$(echo "$comp" | tr '[:upper:]' '[:lower:]' | tr ' ' '-' | tr -cd 'a-z0-9-')
  PHASE_COMPS="$comp" PHASE_TAG="$slug" setsid nohup $VENV gen4_phaseA_backfill.py --resume \
    > "$LOGDIR/core_${slug}.log" 2>&1 &
  echo "started $comp pid=$!"
  sleep 5
done

echo "ALL CORE PROCS LAUNCHED $(date)" > "$LOGDIR/launcher_state.txt"
echo "$!" > /dev/null
