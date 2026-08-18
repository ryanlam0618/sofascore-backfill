#!/usr/bin/env python3
"""
Mass CloakBrowser test — Part 3: Continue from AFC CL 2022 onwards.
Also covers: AFC CL 2, FA Cup, EFL Cup, Copa del Rey, Coppa Italia,
Coupe de France, DFB Pokal, J.League Cup, Emperor's Cup, Australia Cup, Chinese FA Cup.
"""

import json
import time
from cloakbrowser import launch

with open("data/cloak_test_season_ids.json") as f:
    season_map = json.load(f)

# Already fully tested: skip
skip_comps = {
    "Premier League","La Liga","Serie A","Bundesliga","Ligue 1","J1 League",
    "K League 1","A-League Men","Chinese Super League","UCL","UEL","UECL"
}
# AFC Champions League: already did 2015-2021, continue from 2022
afc_partial_skip_seasons = {"2015","2016","2017","2018","2019","2020","2021"}

ENDPOINTS = [
    "/api/v1/event/{eid}",
    "/api/v1/event/{eid}/incidents",
    "/api/v1/event/{eid}/lineups",
    "/api/v1/event/{eid}/shotmap",
]

print("=== CloakBrowser Mass Test Part 3 ===")
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

def get_one_event_id(ut_id, season_id):
    for r in [1, 2, 3, 4, 5]:
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
                    return ev.get("id"), f"{ev.get('homeTeam',{}).get('name','?')} vs {ev.get('awayTeam',{}).get('name','?')}"
        except:
            pass
    # cuptrees
    url = f"https://www.sofascore.com/api/v1/unique-tournament/{ut_id}/season/{season_id}/cuptrees"
    try:
        resp = page.goto(url, timeout=30000)
        if resp and resp.status == 200:
            body = page.evaluate("() => document.body.innerText")
            data = json.loads(body)
            for cr in data.get("cupRoundTrees", []):
                events = cr.get("events", [])
                if events:
                    ev = events[0]
                    return ev.get("id"), f"{ev.get('homeTeam',{}).get('name','?')} vs {ev.get('awayTeam',{}).get('name','?')} (cup)"
    except:
        pass
    return None, "no event found"

for comp_name, comp_info in season_map.items():
    if comp_name in skip_comps:
        continue
    
    ut_id = comp_info["ut_id"]
    seasons = comp_info["seasons"]
    
    print(f"\n{'='*60}")
    print(f"  {comp_name} (ut_id={ut_id})")
    print(f"{'='*60}")
    
    sorted_seasons = sorted(seasons.items(), key=lambda x: x[0])
    
    for year_label, season_id in sorted_seasons:
        if year_label in ("25/26", "2025", "26/27"):
            continue
        # AFC CL partial skip
        if comp_name == "AFC Champions League" and year_label in afc_partial_skip_seasons:
            continue
        
        print(f"\n  [{year_label}] season_id={season_id}")
        
        eid, match_label = get_one_event_id(ut_id, season_id)
        
        if eid is None:
            print(f"    ⚠️ No event found for {year_label}")
            stats["fail_other"] += 1
            detail_results.append({"comp": comp_name, "season": year_label, "season_id": season_id, "event_id": None, "endpoints": {}, "note": "no event found"})
            # Save even on no-event
            with open("data/cloak_test_mass_results_part3.json", "w") as f:
                json.dump({"stats": stats, "details": detail_results}, f, indent=2, ensure_ascii=False)
            continue
        
        print(f"    Event: {eid} ({match_label})")
        
        # Try event page with shorter timeout
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
            print(f"    Event page: timeout (skipped) ⏭️")
            stats["errors"] += 1
            event_status = "timeout"
        
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
        # Save after each season (EPIPE protection)
        with open("data/cloak_test_mass_results_part3.json", "w") as f:
            json.dump({"stats": stats, "details": detail_results}, f, indent=2, ensure_ascii=False)
        time.sleep(0.3)

browser.close()

with open("data/cloak_test_mass_results_part3.json", "w") as f:
    json.dump({"stats": stats, "details": detail_results}, f, indent=2, ensure_ascii=False)

print(f"\n{'='*60}")
print("FINAL SUMMARY (Part 3)")
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
