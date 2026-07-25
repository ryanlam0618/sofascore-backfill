#!/usr/bin/env bash
# Relaunch crashed/missing backfill processes with new code
set -euo pipefail

VENV=".runner-venv/bin/python3"
LOGDIR="logs/backfill"
mkdir -p "$LOGDIR"

# Get alive session keys
alive_keys=$(pgrep -af 'backfill_runner' 2>/dev/null | grep -o -- '--sticky-session-key [^ ]*' | sed 's/--sticky-session-key //' | sort || true)

# Competition name → slug mapping
declare -A COMP_MAP
COMP_MAP=(
  ["A-League Men"]="a-league-men"
  ["AFC Champions League"]="afc-champions-league"
  ["AFC Champions League Two"]="afc-champions-league-two"
  ["Australia Cup"]="australia-cup"
  ["Bundesliga"]="bundesliga"
  ["Chinese FA Cup"]="chinese-fa-cup"
  ["Chinese Super League"]="chinese-super-league"
  ["Copa del Rey"]="copa-del-rey"
  ["Coppa Italia"]="coppa-italia"
  ["Coupe de France"]="coupe-de-france"
  ["DFB Pokal"]="dfb-pokal"
  ["EFL Cup"]="efl-cup"
  ["Emperor's Cup"]="emperors-cup"
  ["FA Cup"]="fa-cup"
  ["J1 League"]="j1-league"
  ["J.League Cup"]="jleague-cup"
  ["K League 1"]="k-league-1"
  ["La Liga"]="la-liga"
  ["Ligue 1"]="ligue-1"
  ["Premier League"]="premier-league"
  ["Serie A"]="serie-a"
  ["UCL"]="ucl"
  ["UECL"]="uecl"
  ["UEL"]="uel"
)

launched=0
skipped=0

for comp in "${!COMP_MAP[@]}"; do
  slug="${COMP_MAP[$comp]}"
  for batch in 1 2; do
    if [ "$batch" -eq 1 ]; then
      from=2015; to=2020
    else
      from=2021; to=2025
    fi

    key="sofa-b${batch}-${slug}"

    # Check if already running
    if echo "$alive_keys" | grep -qx "$key"; then
      echo "⏭ SKIP $key (already running)"
      skipped=$((skipped + 1))
      continue
    fi

    # Check if this is UECL batch1 (no seasons before 2021)
    if [ "$slug" = "uecl" ] && [ "$batch" -eq 1 ]; then
      echo "⏭ SKIP $key (UECL started 2021, no 2015-2020 seasons)"
      skipped=$((skipped + 1))
      continue
    fi

    log="${LOGDIR}/relaunch_b${batch}_${slug}.log"
    echo "🚀 Launching $key → $log"
    setsid nohup $VENV backfill_runner.py \
      --competition "$comp" \
      --from-year "$from" --to-year "$to" \
      --sticky-session-key "$key" \
      > "$log" 2>&1 &

    launched=$((launched + 1))
    sleep 2
  done
done

echo ""
echo "✅ Done: $launched launched, $skipped skipped"
echo "Total running: $(pgrep -c -f backfill_runner || echo 0)"
