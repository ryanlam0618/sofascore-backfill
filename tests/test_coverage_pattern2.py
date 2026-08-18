#!/usr/bin/env python3
"""
Coverage pattern test — Part 2: continue from K League 1 onwards.
"""

import json
import time
from cloakbrowser import launch

with open("data/cloak_test_season_ids.json") as f:
    season_map = json.load(f)

import yaml
with open("competitions_10y.yaml") as f:
    config = yaml.safe_load(f)
comp_config = {c["name"]: c for c in config["competitions"]}

EVENTS_PER_SEASON = 10

# Already done: PL, La Liga, Serie A, Bundesliga, Ligue 1, J1 League (partial)
done_comps = {"Premier League","La Liga","Serie A","Bundesliga","Ligue 1"}
j1_partial = True  # J1 did 2015-2023

print("=== Coverage Pattern Test Part 2 ===")
print(f"[{time.strftime('%H:%M:%S')}] Launching...")

browser = launch(
    headless=True,
    humanize=True,
    proxy={
        'server': 'http://p.webshare.io:80',
        'username': 'aeptenjc-rotate',
        'password': 'dztr57tcycoz'
    }
)
page = browser.new_page()

all_results = {}

def get_events_from_round(ut_id, season_id, round_n):
    url = f"https://www.sofascore.com/api/v1/unique-tournament/{ut_id}/season/{season_id}/events/round/{round_n}"
    try:
        resp = page.goto(url, timeout=30000)
        if resp and resp.status == 200:
            body = page.evaluate("() => document.body.innerText")
            data = json.loads(body)
            events = data.get("events", [])
            return [(ev.get("id"), ev.get("homeTeam",{}).get("name","?"), ev.get("awayTeam",{}).get("name","?")) for ev in events]
    except:
        pass
    return []

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
    time.sleep(0.2)
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
    time.sleep(0.2)
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

for comp_name, comp_info in season_map.items():
    if comp_name in done_comps:
        continue
    
    ut_id = comp_info["ut_id"]
    seasons = comp_info["seasons"]
    cfg = comp_config.get(comp_name, {})
    season_mode = cfg.get("season_mode", "national")
    
    all_results[comp_name] = {}
    print(f"\n{'='*70}")
    print(f"  {comp_name} (ut_id={ut_id}, mode={season_mode})")
    print(f"{'='*70}")
    
    sorted_seasons = sorted(seasons.items(), key=lambda x: x[0])
    
    for year_label, season_id in sorted_seasons:
        if year_label in ("25/26", "2025", "26/27"):
            continue
        # J1 partial skip
        if comp_name == "J1 League":
            j1_years = ["2015","2016","2017","2018","2019","2020","2021","2022","2023"]
            if year_label in j1_years:
                continue
        
        event_list = []
        if season_mode in ("national", "uefa"):
            for r in range(1, 8):
                events = get_events_from_round(ut_id, season_id, r)
                event_list.extend(events)
                if len(event_list) >= EVENTS_PER_SEASON:
                    break
        else:
            event_list = get_events_from_cuptrees(ut_id, season_id)
        
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
            time.sleep(0.2)
        
        all_results[comp_name][year_label] = season_results
        
        # Save after each season (EPIPE protection)
        with open("data/cloak_coverage_pattern_part2.json", "w") as f:
            json.dump(all_results, f, indent=2, ensure_ascii=False)
        
        n = len(season_results)
        print(f"  [{year_label}] {n} events | cov: 1×{cov_counts['cov_1']}, -1×{cov_counts['cov_neg1']}, other×{cov_counts['cov_other']} | "
              f"lineups: ✅{lu_counts[200]} ❌{lu_counts[404]} other×{lu_counts['other']} | "
              f"shotmap: ✅{sm_counts[200]} ❌{sm_counts[404]} other×{sm_counts['other']}")
        
        time.sleep(0.3)

browser.close()

with open("data/cloak_coverage_pattern_part2.json", "w") as f:
    json.dump(all_results, f, indent=2, ensure_ascii=False)

print(f"\nSAVED to data/cloak_coverage_pattern_part2.json")
print(f"[{time.strftime('%H:%M:%S')}] Done.")
