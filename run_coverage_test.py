#!/usr/bin/env python3
"""
Coverage pattern test using ProxyManager.
24 competitions × 10 seasons × 10 events per season.
Checks coverage field, lineups status, shotmap status for each event.
"""

import json
import time
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from rotate_proxy import ProxyManager, save_json

with open("data/cloak_test_season_ids.json") as f:
    season_map = json.load(f)

try:
    import yaml
    with open("competitions_10y.yaml") as f:
        config = yaml.safe_load(f)
    comp_config = {c["name"]: c for c in config["competitions"]}
except:
    comp_config = {}

EVENTS_PER_SEASON = 10
OUTPUT_FILE = "data/cloak_coverage_pattern_10x10.json"


def get_events_from_rounds(pm, ut_id, season_id, max_rounds=8):
    """Get up to EVENTS_PER_SEASON event IDs from rounds."""
    events = []
    for r in range(1, max_rounds + 1):
        url = f"https://www.sofascore.com/api/v1/unique-tournament/{ut_id}/season/{season_id}/events/round/{r}"
        status, data = pm.api_get_json(url)
        if status == 200 and data:
            for ev in data.get("events", []):
                events.append((ev.get("id"), ev.get("homeTeam", {}).get("name", "?"), ev.get("awayTeam", {}).get("name", "?")))
                if len(events) >= EVENTS_PER_SEASON:
                    return events
        time.sleep(0.2)
    return events[:EVENTS_PER_SEASON]


def get_events_from_cuptrees(pm, ut_id, season_id):
    """Get events from cup trees."""
    events = []
    url = f"https://www.sofascore.com/api/v1/unique-tournament/{ut_id}/season/{season_id}/cuptrees"
    status, data = pm.api_get_json(url)
    if status != 200 or not data:
        return events

    for tree in data.get("cupTrees", []):
        for r in tree.get("rounds", []):
            for b in r.get("blocks", []):
                for e in b.get("events", []):
                    eid = e if isinstance(e, int) else e.get("id")
                    parts = b.get("participants", [])
                    home = parts[0].get("team", {}).get("name", "?") if len(parts) > 0 else "?"
                    away = parts[1].get("team", {}).get("name", "?") if len(parts) > 1 else "?"
                    events.append((eid, home, away))
        for cr in tree.get("cupRoundTrees", []):
            for ev in cr.get("events", []):
                events.append((ev.get("id"), ev.get("homeTeam", {}).get("name", "?"), ev.get("awayTeam", {}).get("name", "?")))

    for cr in data.get("cupRoundTrees", []):
        for ev in cr.get("events", []):
            events.append((ev.get("id"), ev.get("homeTeam", {}).get("name", "?"), ev.get("awayTeam", {}).get("name", "?")))

    return events[:EVENTS_PER_SEASON]


def check_event(pm, eid):
    """Check coverage, lineups, shotmap for one event."""
    result = {"coverage": -99, "has_ps": None, "lineups": 0, "shotmap": 0, "lineups_players": 0, "shotmap_shots": 0}

    # Get event data
    status, data = pm.api_get_json(f"https://www.sofascore.com/api/v1/event/{eid}")
    if status != 200 or not data:
        return result
    ev = data.get("event", data)
    result["coverage"] = ev.get("coverage", -99)
    result["has_ps"] = ev.get("hasEventPlayerStatistics", None)

    time.sleep(0.2)

    # Lineups
    status, data = pm.api_get_json(f"https://www.sofascore.com/api/v1/event/{eid}/lineups")
    if status == 200 and data:
        result["lineups"] = 200
        result["lineups_players"] = len(data.get("home", {}).get("players", []))
    else:
        result["lineups"] = status if status != "error" else 0

    time.sleep(0.2)

    # Shotmap
    status, data = pm.api_get_json(f"https://www.sofascore.com/api/v1/event/{eid}/shotmap")
    if status == 200 and data:
        result["shotmap"] = 200
        result["shotmap_shots"] = len(data.get("shotmap", []))
    else:
        result["shotmap"] = status if status != "error" else 0

    return result


def run():
    pm = ProxyManager(OUTPUT_FILE)
    pm.launch()

    all_results = {}

    # Load existing for resume
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE) as f:
            all_results = json.load(f)
        print(f"Resuming from {sum(len(v) for v in all_results.values())} existing season results")

    for comp_name, comp_info in season_map.items():
        ut_id = comp_info["ut_id"]
        seasons = comp_info["seasons"]
        cfg = comp_config.get(comp_name, {})
        mode = cfg.get("season_mode", "national")

        if comp_name not in all_results:
            all_results[comp_name] = {}

        print(f"\n{'='*70}")
        print(f"  {comp_name} (ut_id={ut_id}, mode={mode})")
        print(f"{'='*70}")

        sorted_seasons = sorted(seasons.items(), key=lambda x: x[0])

        for year_label, season_id in sorted_seasons:
            if year_label in ("25/26", "2025", "26/27"):
                continue

            if year_label in all_results[comp_name]:
                print(f"  [{year_label}] already done, skipping")
                continue

            pm.maybe_restart()
            print(f"\n  [{year_label}] season_id={season_id}")

            # Collect events
            if mode in ("national", "uefa"):
                event_list = get_events_from_rounds(pm, ut_id, season_id)
            else:
                event_list = get_events_from_cuptrees(pm, ut_id, season_id)
                if not event_list:
                    event_list = get_events_from_rounds(pm, ut_id, season_id)

            event_list = event_list[:EVENTS_PER_SEASON]

            if not event_list:
                print(f"    No events found")
                all_results[comp_name][year_label] = []
                save_json(OUTPUT_FILE, all_results)
                continue

            season_results = []
            cov_counts = {"cov_1": 0, "cov_neg1": 0, "cov_other": 0}
            lu_counts = {200: 0, 404: 0, "other": 0}
            sm_counts = {200: 0, 404: 0, "other": 0}

            for eid, home, away in event_list:
                pm.maybe_restart()
                r = check_event(pm, eid)
                season_results.append({"eid": eid, "home": home, "away": away, **r})

                c = r["coverage"]
                if c == 1: cov_counts["cov_1"] += 1
                elif c == -1: cov_counts["cov_neg1"] += 1
                else: cov_counts["cov_other"] += 1

                l = r["lineups"]
                if l in (200, 404): lu_counts[l] += 1
                else: lu_counts["other"] += 1

                s = r["shotmap"]
                if s in (200, 404): sm_counts[s] += 1
                else: sm_counts["other"] += 1

                time.sleep(0.2)

            all_results[comp_name][year_label] = season_results
            save_json(OUTPUT_FILE, all_results)

            n = len(season_results)
            print(f"    {n} events | cov: 1×{cov_counts['cov_1']}, -1×{cov_counts['cov_neg1']}, other×{cov_counts['cov_other']} | "
                  f"lineups: ✅{lu_counts[200]} ❌{lu_counts[404]} other×{lu_counts['other']} | "
                  f"shotmap: ✅{sm_counts[200]} ❌{sm_counts[404]} other×{sm_counts['other']}")
            time.sleep(0.3)

    pm.close()

    print(f"\n{'='*70}")
    print("SAVED to data/cloak_coverage_pattern_10x10.json")
    print(f"[{time.strftime('%H:%M:%S')}] Done.")


if __name__ == "__main__":
    run()
