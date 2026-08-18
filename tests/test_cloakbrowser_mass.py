#!/usr/bin/env python3
"""
Mass CloakBrowser test: 24 competitions × 10 seasons × 1 event per season.
Tests event page + key API endpoints. Reports 403/404/200 for each.
"""

import json
import time
from cloakbrowser import launch

# Load season IDs
with open("data/cloak_test_season_ids.json") as f:
    season_map = json.load(f)

# Load competition config for season_mode
import yaml
with open("competitions_10y.yaml") as f:
    config = yaml.safe_load(f)

comp_config = {c["name"]: c for c in config["competitions"]}

# Target seasons (15/16 through 24/25)
# Different competitions use different year labels
season_labels = [
    "15/16","16/17","17/18","18/19","19/20","20/21","21/22","22/23","23/24","24/25",
    "2015","2016","2017","2018","2019","2020","2021","2022","2023","2024","2025",
]

# API endpoints to test per event
ENDPOINTS = [
    "/api/v1/event/{eid}",
    "/api/v1/event/{eid}/incidents",
    "/api/v1/event/{eid}/lineups",
    "/api/v1/event/{eid}/shotmap",
]

print("=== CloakBrowser Mass Test: 24 competitions × 10 seasons ===")
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

stats = {"pass": 0, "fail_403": 0, "fail_404": 0, "fail_other": 0, "errors": 0}
detail_results = []

def get_one_event_id(ut_id, season_id, season_mode, comp_name):
    """Get one event ID for a season. Try rounds endpoint first, then cup trees."""
    # Try round 1 events (league mode)
    url = f"https://www.sofascore.com/api/v1/unique-tournament/{ut_id}/season/{season_id}/events/round/1"
    try:
        resp = page.goto(url, timeout=30000)
        status = resp.status if resp else 0
        if status == 200:
            body = page.evaluate("() => document.body.innerText")
            data = json.loads(body)
            events = data.get("events", [])
            if events:
                ev = events[0]
                eid = ev.get("id")
                home = ev.get("homeTeam", {}).get("name", "?")
                away = ev.get("awayTeam", {}).get("name", "?")
                return eid, f"{home} vs {away}"
    except:
        pass
    
    # Try round 2, 3 for cups
    for r in [2, 3, 4, 5]:
        url = f"https://www.sofascore.com/api/v1/unique-tournament/{ut_id}/season/{season_id}/events/round/{r}"
        try:
            resp = page.goto(url, timeout=30000)
            status = resp.status if resp else 0
            if status == 200:
                body = page.evaluate("() => document.body.innerText")
                data = json.loads(body)
                events = data.get("events", [])
                if events:
                    ev = events[0]
                    eid = ev.get("id")
                    home = ev.get("homeTeam", {}).get("name", "?")
                    away = ev.get("awayTeam", {}).get("name", "?")
                    return eid, f"{home} vs {away}"
        except:
            pass
    
    # Try cup trees endpoint for cup/international competitions
    url = f"https://www.sofascore.com/api/v1/unique-tournament/{ut_id}/season/{season_id}/cuptrees"
    try:
        resp = page.goto(url, timeout=30000)
        status = resp.status if resp else 0
        if status == 200:
            body = page.evaluate("() => document.body.innerText")
            data = json.loads(body)
            # Look for cup tree rounds
            cup_rounds = data.get("cupRoundTrees", [])
            for cr in cup_rounds:
                events = cr.get("events", [])
                if events:
                    ev = events[0]
                    eid = ev.get("id")
                    home = ev.get("homeTeam", {}).get("name", "?")
                    away = ev.get("awayTeam", {}).get("name", "?")
                    return eid, f"{home} vs {away} (cup)"
    except:
        pass
    
    # Last resort: try events/round/1 with different URL pattern
    # Some international competitions use different event fetch methods  
    # Try discovered events via search
    url = f"https://www.sofascore.com/api/v1/unique-tournament/{ut_id}/season/{season_id}/standings"
    try:
        resp = page.goto(url, timeout=30000)
        # not an event endpoint, but let's see if season exists
    except:
        pass
    
    return None, "no event found"

total_comps = 0
total_seasons_tested = 0

for comp_name, comp_info in season_map.items():
    ut_id = comp_info["ut_id"]
    seasons = comp_info["seasons"]
    cfg = comp_config.get(comp_name, {})
    season_mode = cfg.get("season_mode", "national")
    
    total_comps += 1
    print(f"\n{'='*60}")
    print(f"  {comp_name} (ut_id={ut_id}, mode={season_mode})")
    print(f"{'='*60}")
    
    # Sort seasons chronologically
    sorted_seasons = sorted(seasons.items(), key=lambda x: x[0])
    
    for year_label, season_id in sorted_seasons:
        if year_label.startswith("25/") or year_label == "2025":
            # Skip 25/26 for now, focus on 15/16 to 24/25
            if year_label == "25/26" or year_label == "2025":
                continue
        
        total_seasons_tested += 1
        print(f"\n  [{year_label}] season_id={season_id}")
        
        # Get one event
        eid, match_label = get_one_event_id(ut_id, season_id, season_mode, comp_name)
        
        if eid is None:
            print(f"    ⚠️ No event found for {year_label}")
            stats["fail_other"] += 1
            detail_results.append({
                "comp": comp_name, "season": year_label, "season_id": season_id,
                "event_id": None, "match": None,
                "endpoints": {}, "note": "no event found"
            })
            # Save even on no-event (EPIPE protection)
            with open("data/cloak_test_mass_results.json", "w") as f:
                json.dump({"stats": stats, "details": detail_results}, f, indent=2, ensure_ascii=False)
            continue
        
        print(f"    Event: {eid} ({match_label})")
        
        # Try event page (HTML) with shorter timeout — skip on timeout to not block API calls
        event_status = 0
        try:
            resp = page.goto(f"https://www.sofascore.com/event/{eid}", timeout=15000)
            event_status = resp.status if resp else 0
            if event_status == 200:
                print(f"    Event page: 200 ✅")
                stats["pass"] += 1
            elif event_status == 403:
                print(f"    Event page: 403 ❌")
                stats["fail_403"] += 1
            else:
                print(f"    Event page: {event_status} ⚠️")
                stats["fail_other"] += 1
        except Exception as e:
            err_msg = str(e)[:50]
            print(f"    Event page: timeout (skipped) ⏭️")
            stats["errors"] += 1
            event_status = "timeout"
        
        ep_results = {"event_page": event_status}
        
        # Test API endpoints
        for ep_template in ENDPOINTS:
            path = ep_template.format(eid=eid)
            url = f"https://www.sofascore.com{path}"
            try:
                resp = page.goto(url, timeout=30000)
                status = resp.status if resp else 0
                ep_key = ep_template.split("/")[-1].replace("{eid}","").strip("/") or "event"
                
                if status == 200:
                    body = page.evaluate("() => document.body.innerText")
                    body_len = len(body) if body else 0
                    print(f"    {ep_key}: 200 ({body_len}ch) ✅")
                    stats["pass"] += 1
                elif status == 403:
                    print(f"    {ep_key}: 403 ❌")
                    stats["fail_403"] += 1
                elif status == 404:
                    print(f"    {ep_key}: 404 ⚠️ (no data)")
                    stats["fail_404"] += 1
                else:
                    print(f"    {ep_key}: {status} ⚠️")
                    stats["fail_other"] += 1
                
                ep_results[ep_key] = status
            except Exception as e:
                print(f"    {ep_key}: error ❌")
                stats["errors"] += 1
                ep_results[ep_key] = "error"
            
            time.sleep(0.5)
        
        detail_results.append({
            "comp": comp_name, "season": year_label, "season_id": season_id,
            "event_id": eid, "match": match_label,
            "endpoints": ep_results
        })
        
        # Save after each season (EPIPE protection)
        with open("data/cloak_test_mass_results.json", "w") as f:
            json.dump({"stats": stats, "details": detail_results}, f, indent=2, ensure_ascii=False)
        
        time.sleep(0.5)

browser.close()

# Save detailed results (already saved incrementally, but save final copy)
with open("data/cloak_test_mass_results.json", "w") as f:
    json.dump({"stats": stats, "details": detail_results}, f, indent=2, ensure_ascii=False)

# Summary
print(f"\n{'='*60}")
print("FINAL SUMMARY")
print(f"{'='*60}")
print(f"Competitions tested: {total_comps}")
print(f"Seasons tested:     {total_seasons_tested}")
print(f"Total API calls:     {stats['pass'] + stats['fail_403'] + stats['fail_404'] + stats['fail_other'] + stats['errors']}")
print(f"  ✅ Pass (200):      {stats['pass']}")
print(f"  ❌ 403 blocked:     {stats['fail_403']}")
print(f"  ⚠️ 404 no data:     {stats['fail_404']}")
print(f"  ⚠️ Other status:    {stats['fail_other']}")
print(f"  ❌ Errors:          {stats['errors']}")
total = stats['pass'] + stats['fail_403'] + stats['fail_404'] + stats['fail_other'] + stats['errors']
if total:
    print(f"  403 rate:           {stats['fail_403']/total*100:.1f}%")
    print(f"  Success rate:       {stats['pass']/total*100:.1f}%")
print(f"\nResults saved to data/cloak_test_mass_results.json")
print(f"[{time.strftime('%H:%M:%S')}] Done.")
