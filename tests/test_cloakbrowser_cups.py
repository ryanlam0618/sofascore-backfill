#!/usr/bin/env python3
"""
Mass CloakBrowser test — Part 4: Cup competitions.
FA Cup, EFL Cup, Copa del Rey, Coppa Italia, Coupe de France, DFB Pokal,
J.League Cup, Emperor's Cup, Australia Cup, Chinese FA Cup.
Uses cuptrees endpoint to find event IDs.
"""

import json
import time
from cloakbrowser import launch

with open("data/cloak_test_season_ids.json") as f:
    season_map = json.load(f)

# Only cup competitions
cup_comps = [
    "FA Cup","EFL Cup","Copa del Rey","Coppa Italia","Coupe de France",
    "DFB Pokal","J.League Cup","Emperor's Cup","Australia Cup","Chinese FA Cup"
]

ENDPOINTS = [
    "/api/v1/event/{eid}",
    "/api/v1/event/{eid}/incidents",
    "/api/v1/event/{eid}/lineups",
    "/api/v1/event/{eid}/shotmap",
]

print("=== CloakBrowser Mass Test Part 4 (Cups) ===")
print(f"[{time.strftime('%H:%M:%S')}] Launching...")

browser = launch(headless=True, humanize=True)
page = browser.new_page()

stats = {"pass": 0, "fail_403": 0, "fail_404": 0, "fail_other": 0, "errors": 0}
detail_results = []

def get_cup_event_id(ut_id, season_id):
    """Get one event ID from cup trees."""
    url = f"https://www.sofascore.com/api/v1/unique-tournament/{ut_id}/season/{season_id}/cuptrees"
    try:
        resp = page.goto(url, timeout=30000)
        status = resp.status if resp else 0
        if status != 200:
            return None, f"cuptrees status {status}"
        body = page.evaluate("() => document.body.innerText")
        data = json.loads(body)
        
        trees = data.get("cupTrees", [])
        for tree in trees:
            rounds = tree.get("rounds", [])
            for r in rounds:
                blocks = r.get("blocks", [])
                for b in blocks:
                    events = b.get("events", [])
                    if events:
                        eid = events[0] if isinstance(events[0], int) else events[0].get("id")
                        participants = b.get("participants", [])
                        home = participants[0].get("team", {}).get("name", "?") if len(participants) > 0 else "?"
                        away = participants[1].get("team", {}).get("name", "?") if len(participants) > 1 else "?"
                        return eid, f"{home} vs {away}"
        
        # Also try cupRoundTrees
        for tree in trees:
            cup_rounds = tree.get("cupRoundTrees", [])
            for cr in cup_rounds:
                events = cr.get("events", [])
                if events:
                    ev = events[0]
                    eid = ev.get("id")
                    home = ev.get("homeTeam", {}).get("name", "?")
                    away = ev.get("awayTeam", {}).get("name", "?")
                    return eid, f"{home} vs {away}"
    except Exception as e:
        return None, f"error: {e}"
    
    return None, "no event in cuptrees"

for comp_name in cup_comps:
    if comp_name not in season_map:
        print(f"\n  {comp_name} — not in season map, skipping")
        continue
    
    comp_info = season_map[comp_name]
    ut_id = comp_info["ut_id"]
    seasons = comp_info["seasons"]
    
    print(f"\n{'='*60}")
    print(f"  {comp_name} (ut_id={ut_id})")
    print(f"{'='*60}")
    
    sorted_seasons = sorted(seasons.items(), key=lambda x: x[0])
    
    for year_label, season_id in sorted_seasons:
        if year_label in ("25/26", "2025", "26/27"):
            continue
        
        print(f"\n  [{year_label}] season_id={season_id}")
        
        eid, match_label = get_cup_event_id(ut_id, season_id)
        
        if eid is None:
            print(f"    ⚠️ No event found: {match_label}")
            stats["fail_other"] += 1
            detail_results.append({"comp": comp_name, "season": year_label, "season_id": season_id, "event_id": None, "note": match_label})
            continue
        
        print(f"    Event: {eid} ({match_label})")
        
        # Warm event page
        try:
            resp = page.goto(f"https://www.sofascore.com/event/{eid}", timeout=30000)
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
            print(f"    Event page: error ❌")
            stats["errors"] += 1
            event_status = 0
        
        ep_results = {"event_page": event_status}
        
        for ep_template in ENDPOINTS:
            path = ep_template.format(eid=eid)
            url = f"https://www.sofascore.com{path}"
            ep_key = ep_template.split("/")[-1].replace("{eid}","").strip("/")
            try:
                resp = page.goto(url, timeout=30000)
                status = resp.status if resp else 0
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
            time.sleep(0.3)
        
        detail_results.append({"comp": comp_name, "season": year_label, "season_id": season_id, "event_id": eid, "match": match_label, "endpoints": ep_results})
        time.sleep(0.3)

browser.close()

with open("data/cloak_test_mass_results_cups.json", "w") as f:
    json.dump({"stats": stats, "details": detail_results}, f, indent=2, ensure_ascii=False)

print(f"\n{'='*60}")
print("FINAL SUMMARY (Cups)")
print(f"{'='*60}")
total = stats['pass'] + stats['fail_403'] + stats['fail_404'] + stats['fail_other'] + stats['errors']
print(f"Total API calls:  {total}")
print(f"  ✅ Pass (200):   {stats['pass']}")
print(f"  ❌ 403 blocked:  {stats['fail_403']}")
print(f"  ⚠️ 404 no data:  {stats['fail_404']}")
print(f"  ⚠️ Other:        {stats['fail_other']}")
print(f"  ❌ Errors:       {stats['errors']}")
if total:
    print(f"  403 rate:        {stats['fail_403']/total*100:.1f}%")
    print(f"  Success rate:    {stats['pass']/total*100:.1f}%")
print(f"\n[{time.strftime('%H:%M:%S')}] Done.")
