#!/usr/bin/env python3
"""
Coverage pattern test — Part 3: Cup competitions only, 5 events per season (faster).
"""

import json
import time
from cloakbrowser import launch

with open("data/cloak_test_season_ids.json") as f:
    season_map = json.load(f)

EVENTS_PER_SEASON = 5

# Already done in part 1/2: PL, La Liga, Serie A, Bundesliga, Ligue 1, J1, K League,
# A-League, CSL, UCL, UEL, UECL, AFC CL, AFC CL Two (2015-2018 partial)
done_comps = {
    "Premier League","La Liga","Serie A","Bundesliga","Ligue 1",
    "J1 League","K League 1","A-League Men","Chinese Super League",
    "UCL","UEL","UECL"
}
acl_partial = {"2015","2016","2017","2018"}  # part2 did these years
acl2_partial = {"2015","2016","2017","2018"}

print("=== Coverage Pattern Test Part 3 (Cups) ===")
print(f"[{time.strftime('%H:%M:%S')}] Launching...")

browser = launch(headless=True, humanize=True)
page = browser.new_page()

all_results = {}

def get_events_from_cuptrees(ut_id, season_id):
    url = f"https://www.sofascore.com/api/v1/unique-tournament/{ut_id}/season/{season_id}/cuptrees"
    try:
        resp = page.goto(url, timeout=30000)
        if resp and resp.status == 200:
            body = page.evaluate("() => document.body.innerText")
            data = json.loads(body)
            eids = []
            for tree in data.get("cupTrees", []):
                for r in tree.get("rounds", []):
                    for b in r.get("blocks", []):
                        events = b.get("events", [])
                        for e in events:
                            eid = e if isinstance(e, int) else e.get("id")
                            parts = b.get("participants", [])
                            home = parts[0].get("team",{}).get("name","?") if len(parts)>0 else "?"
                            away = parts[1].get("team",{}).get("name","?") if len(parts)>1 else "?"
                            eids.append((eid, home, away))
            return eids
    except:
        pass
    return []

def check_event(eid):
    result = {"coverage": -99, "has_ps": None, "lineups": 0, "shotmap": 0, "lineups_players": 0, "shotmap_shots": 0}
    try:
        resp = page.goto(f"https://www.sofascore.com/api/v1/event/{eid}", timeout=30000)
        if resp and resp.status == 200:
            body = page.evaluate("() => document.body.innerText")
            data = json.loads(body)
            ev = data.get("event", data)
            result["coverage"] = ev.get("coverage", -99)
            result["has_ps"] = ev.get("hasEventPlayerStatistics", None)
        else:
            return result
    except:
        return result
    time.sleep(0.15)
    try:
        resp = page.goto(f"https://www.sofascore.com/api/v1/event/{eid}/lineups", timeout=30000)
        if resp and resp.status == 200:
            result["lineups"] = 200
            body = page.evaluate("() => document.body.innerText")
            data = json.loads(body)
            result["lineups_players"] = len(data.get("home",{}).get("players",[]))
        else:
            result["lineups"] = resp.status if resp else 0
    except:
        result["lineups"] = -1
    time.sleep(0.15)
    try:
        resp = page.goto(f"https://www.sofascore.com/api/v1/event/{eid}/shotmap", timeout=30000)
        if resp and resp.status == 200:
            result["shotmap"] = 200
            body = page.evaluate("() => document.body.innerText")
            data = json.loads(body)
            result["shotmap_shots"] = len(data.get("shotmap",[]))
        else:
            result["shotmap"] = resp.status if resp else 0
    except:
        result["shotmap"] = -1
    return result

# Cups to test
cups = ["FA Cup","EFL Cup","Copa del Rey","Coppa Italia","Coupe de France",
        "DFB Pokal","J.League Cup","Emperor's Cup","Australia Cup","Chinese FA Cup"]

for comp_name in cups:
    if comp_name not in season_map:
        continue
    
    comp_info = season_map[comp_name]
    ut_id = comp_info["ut_id"]
    seasons = comp_info["seasons"]
    
    all_results[comp_name] = {}
    print(f"\n{'='*70}")
    print(f"  {comp_name} (ut_id={ut_id})")
    print(f"{'='*70}")
    
    sorted_seasons = sorted(seasons.items(), key=lambda x: x[0])
    
    for year_label, season_id in sorted_seasons:
        if year_label in ("25/26", "2025", "26/27"):
            continue
        
        event_list = get_events_from_cuptrees(ut_id, season_id)
        # Try later rounds for cups (R3+ has better data)
        # Filter to get events with home+away teams (skip byes)
        event_list = event_list[:EVENTS_PER_SEASON]
        
        if not event_list:
            print(f"  [{year_label}] No events found")
            all_results[comp_name][year_label] = []
            continue
        
        season_results = []
        cov_counts = {"cov_1": 0, "cov_neg1": 0, "cov_other": 0}
        lu_counts = {200: 0, 404: 0, "other": 0}
        sm_counts = {200: 0, 404: 0, "other": 0}
        
        for eid, home, away in event_list:
            r = check_event(eid)
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
            time.sleep(0.15)
        
        all_results[comp_name][year_label] = season_results
        
        n = len(season_results)
        print(f"  [{year_label}] {n} events | cov: 1×{cov_counts['cov_1']}, -1×{cov_counts['cov_neg1']}, other×{cov_counts['cov_other']} | "
              f"lineups: ✅{lu_counts[200]} ❌{lu_counts[404]} other×{lu_counts['other']} | "
              f"shotmap: ✅{sm_counts[200]} ❌{sm_counts[404]} other×{sm_counts['other']}")
        
        time.sleep(0.3)

browser.close()

# Save to file
with open("data/cloak_coverage_pattern_cups.json", "w") as f:
    json.dump(all_results, f, indent=2, ensure_ascii=False)

print(f"\nSAVED to data/cloak_coverage_pattern_cups.json")
print(f"[{time.strftime('%H:%M:%S')}] Done.")
