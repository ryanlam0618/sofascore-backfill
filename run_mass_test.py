#!/usr/bin/env python3
"""
Unified mass test runner using ProxyManager.
Runs all 24 competitions × 10 seasons, saves incrementally.
Skips event HTML page load (only calls API endpoints).
Auto-restarts browser + cooldowns on 403.
"""

import json
import time
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from rotate_proxy import ProxyManager, save_json

# ---- Config ----
with open("data/cloak_test_season_ids.json") as f:
    season_map = json.load(f)

try:
    import yaml
    with open("competitions_10y.yaml") as f:
        config = yaml.safe_load(f)
    comp_config = {c["name"]: c for c in config["competitions"]}
except:
    comp_config = {}

ENDPOINTS = [
    ("event", "/api/v1/event/{eid}"),
    ("incidents", "/api/v1/event/{eid}/incidents"),
    ("lineups", "/api/v1/event/{eid}/lineups"),
    ("shotmap", "/api/v1/event/{eid}/shotmap"),
]

OUTPUT_FILE = "data/cloak_test_mass_results.json"


def get_event_id(pm, ut_id, season_id, comp_name):
    """Get one event ID for a season."""
    cfg = comp_config.get(comp_name, {})
    mode = cfg.get("season_mode", "national")

    # Try rounds 1-8
    for r in range(1, 9):
        url = f"https://www.sofascore.com/api/v1/unique-tournament/{ut_id}/season/{season_id}/events/round/{r}"
        status, data = pm.api_get_json(url)
        if status == 200 and data:
            events = data.get("events", [])
            if events:
                ev = events[0]
                eid = ev.get("id")
                home = ev.get("homeTeam", {}).get("name", "?")
                away = ev.get("awayTeam", {}).get("name", "?")
                return eid, f"{home} vs {away}"
        time.sleep(0.2)

    # Try cuptrees for cup/international
    if mode in ("cup", "international"):
        url = f"https://www.sofascore.com/api/v1/unique-tournament/{ut_id}/season/{season_id}/cuptrees"
        status, data = pm.api_get_json(url)
        if status == 200 and data:
            # cupTrees > rounds > blocks > events
            for tree in data.get("cupTrees", []):
                for r in tree.get("rounds", []):
                    for b in r.get("blocks", []):
                        events = b.get("events", [])
                        if events:
                            eid = events[0] if isinstance(events[0], int) else events[0].get("id")
                            parts = b.get("participants", [])
                            home = parts[0].get("team", {}).get("name", "?") if len(parts) > 0 else "?"
                            away = parts[1].get("team", {}).get("name", "?") if len(parts) > 1 else "?"
                            return eid, f"{home} vs {away}"
                for cr in tree.get("cupRoundTrees", []):
                    events = cr.get("events", [])
                    if events:
                        ev = events[0]
                        return ev.get("id"), f"{ev.get('homeTeam',{}).get('name','?')} vs {ev.get('awayTeam',{}).get('name','?')}"
            # Top-level cupRoundTrees
            for cr in data.get("cupRoundTrees", []):
                events = cr.get("events", [])
                if events:
                    ev = events[0]
                    return ev.get("id"), f"{ev.get('homeTeam',{}).get('name','?')} vs {ev.get('awayTeam',{}).get('name','?')}"

    return None, "no event found"


def run():
    pm = ProxyManager(OUTPUT_FILE)
    pm.launch()

    stats = {"pass": 0, "fail_403": 0, "fail_404": 0, "fail_other": 0, "errors": 0}
    detail_results = []

    # Load existing results if any (for resume)
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE) as f:
            existing = json.load(f)
        stats = existing.get("stats", stats)
        detail_results = existing.get("details", [])
        done_keys = {(d["comp"], d["season"]) for d in detail_results}
        print(f"Resuming from {len(detail_results)} existing results")
    else:
        done_keys = set()

    total_comps = 0
    total_seasons = 0

    for comp_name, comp_info in season_map.items():
        ut_id = comp_info["ut_id"]
        seasons = comp_info["seasons"]
        total_comps += 1

        print(f"\n{'='*60}")
        print(f"  {comp_name} (ut_id={ut_id})")
        print(f"{'='*60}")

        sorted_seasons = sorted(seasons.items(), key=lambda x: x[0])

        for year_label, season_id in sorted_seasons:
            if year_label in ("25/26", "2025", "26/27"):
                continue

            key = (comp_name, year_label)
            if key in done_keys:
                print(f"  [{year_label}] already done, skipping")
                continue

            total_seasons += 1
            pm.maybe_restart()
            print(f"\n  [{year_label}] season_id={season_id}")

            eid, match_label = get_event_id(pm, ut_id, season_id, comp_name)

            if eid is None:
                print(f"    ⚠️ {match_label}")
                stats["fail_other"] += 1
                detail_results.append({
                    "comp": comp_name, "season": year_label, "season_id": season_id,
                    "event_id": None, "note": match_label, "endpoints": {}
                })
                done_keys.add(key)
                save_json(OUTPUT_FILE, {"stats": stats, "details": detail_results})
                continue

            print(f"    Event: {eid} ({match_label})")

            ep_results = {}
            for ep_name, ep_template in ENDPOINTS:
                url = f"https://www.sofascore.com{ep_template.format(eid=eid)}"
                status, body = pm.api_get(url)

                if status == 200:
                    body_len = len(body) if body else 0
                    ep_results[ep_name] = 200
                    ep_results[f"{ep_name}_len"] = body_len
                    stats["pass"] += 1
                    print(f"    {ep_name}: 200 ({body_len}ch) ✅")
                elif status == 403:
                    ep_results[ep_name] = 403
                    stats["fail_403"] += 1
                    print(f"    {ep_name}: 403 ❌")
                elif status == 404:
                    ep_results[ep_name] = 404
                    stats["fail_404"] += 1
                    print(f"    {ep_name}: 404 ⚠️ (no data)")
                elif status == "error":
                    ep_results[ep_name] = "error"
                    stats["errors"] += 1
                    print(f"    {ep_name}: error ❌")
                else:
                    ep_results[ep_name] = status
                    stats["fail_other"] += 1
                    print(f"    {ep_name}: {status} ⚠️")

                time.sleep(0.3)

            detail_results.append({
                "comp": comp_name, "season": year_label, "season_id": season_id,
                "event_id": eid, "match": match_label, "endpoints": ep_results
            })
            done_keys.add(key)
            save_json(OUTPUT_FILE, {"stats": stats, "details": detail_results})
            time.sleep(0.3)

    pm.close()

    # Final summary
    total = stats['pass'] + stats['fail_403'] + stats['fail_404'] + stats['fail_other'] + stats['errors']
    print(f"\n{'='*60}")
    print("FINAL SUMMARY")
    print(f"{'='*60}")
    print(f"Competitions:      {total_comps}")
    print(f"Seasons tested:    {total_seasons}")
    print(f"Total API calls:    {total}")
    print(f"  ✅ Pass (200):     {stats['pass']}")
    print(f"  ❌ 403 blocked:    {stats['fail_403']}")
    print(f"  ⚠️ 404 no data:    {stats['fail_404']}")
    print(f"  ⚠️ Other:          {stats['fail_other']}")
    print(f"  ❌ Errors:         {stats['errors']}")
    if total:
        print(f"  403 rate:          {stats['fail_403']/total*100:.1f}%")
        print(f"  Success rate:      {stats['pass']/total*100:.1f}%")
    print(f"\n[{time.strftime('%H:%M:%S')}] Done.")


if __name__ == "__main__":
    run()
