#!/usr/bin/env python3
"""Test CloakBrowser against SofaScore 2015-16 season event — old data coverage."""

import time
import json
from cloakbrowser import launch

print("=== CloakBrowser -> SofaScore 2015-16 Season Test ===")
print(f"[{time.strftime('%H:%M:%S')}] Launching CloakBrowser (headless)...")

browser = launch(headless=True, humanize=True)
page = browser.new_page()

# 2015-16 Premier League Round 1: Man Utd vs Tottenham
eid = 6767929
label = "Man Utd vs Tottenham (PL 15/16 R1)"

# Event page first
print(f"\n[{time.strftime('%H:%M:%S')}] Loading event page for {eid} ({label})...")
try:
    resp = page.goto(f"https://www.sofascore.com/event/{eid}", timeout=30000)
    status = resp.status if resp else 'N/A'
    title = page.title()
    print(f"  Status: {status} | Title: {title}")
    if status == 200:
        print("  ✅ Event page OK")
    else:
        print(f"  ❌ Event page got {status}")
except Exception as e:
    print(f"  ❌ Event page error: {e}")

# Test all data endpoints
endpoints = [
    f"/api/v1/event/{eid}",
    f"/api/v1/event/{eid}/incidents",
    f"/api/v1/event/{eid}/lineups",
    f"/api/v1/event/{eid}/statistics",
    f"/api/v1/event/{eid}/shotmap",
    f"/api/v1/event/{eid}/odds/1",
    f"/api/v1/event/{eid}/comments",
    f"/api/v1/event/{eid}/momentum",
]

passes = 0
fails_403 = 0
fails_404 = 0
fails_other = 0

for path in endpoints:
    url = f"https://www.sofascore.com{path}"
    print(f"\n[{time.strftime('%H:%M:%S')}] {path}")
    try:
        resp = page.goto(url, timeout=30000)
        status = resp.status if resp else 'N/A'
        if status == 200:
            body = page.evaluate("() => document.body.innerText")
            body_len = len(body) if body else 0
            # Try to parse JSON and show key info
            try:
                data = json.loads(body)
                keys = list(data.keys())[:5] if isinstance(data, dict) else f"list[{len(data)}]"
                print(f"  Status: 200 | Body: {body_len} chars | Keys: {keys}")
            except:
                print(f"  Status: 200 | Body: {body_len} chars")
            print("  ✅ OK")
            passes += 1
        elif status == 403:
            print(f"  ❌ 403 — BLOCKED")
            fails_403 += 1
        elif status == 404:
            body = page.evaluate("() => document.body.innerText")
            print(f"  ⚠️ 404 — endpoint not available for this event")
            print(f"     Body: {body[:200]}")
            fails_404 += 1
        else:
            print(f"  ⚠️ Status: {status}")
            fails_other += 1
    except Exception as e:
        print(f"  ❌ Error: {e}")
        fails_other += 1
    time.sleep(1)

# Also test a few more 2015-16 events
print(f"\n{'='*60}")
print("Testing more 2015-16 events...")
print('='*60)

# Get round 2 events
resp = page.goto(f"https://www.sofascore.com/api/v1/unique-tournament/17/season/10356/events/round/2", timeout=30000)
body = page.evaluate("() => document.body.innerText")
data = json.loads(body)
events_r2 = data.get('events', [])

extra_events = events_r2[:3]
for ev in extra_events:
    eid2 = ev.get('id')
    home = ev.get('homeTeam', {}).get('name', '?')
    away = ev.get('awayTeam', {}).get('name', '?')
    print(f"\n--- {eid2}: {home} vs {away} ---")
    
    # warm event page
    try:
        page.goto(f"https://www.sofascore.com/event/{eid2}", timeout=30000)
    except:
        pass
    
    for ep in [f"/api/v1/event/{eid2}", f"/api/v1/event/{eid2}/incidents", f"/api/v1/event/{eid2}/lineups", f"/api/v1/event/{eid2}/shotmap"]:
        url = f"https://www.sofascore.com{ep}"
        try:
            resp = page.goto(url, timeout=30000)
            status = resp.status if resp else 'N/A'
            body = page.evaluate("() => document.body.innerText")
            body_len = len(body) if body else 0
            if status == 200:
                print(f"  {ep}: 200 ({body_len} chars) ✅")
                passes += 1
            elif status == 403:
                print(f"  {ep}: 403 ❌")
                fails_403 += 1
            elif status == 404:
                print(f"  {ep}: 404 ⚠️ (no data)")
                fails_404 += 1
            else:
                print(f"  {ep}: {status} ⚠️")
                fails_other += 1
        except Exception as e:
            print(f"  {ep}: error ❌ ({e})")
            fails_other += 1
        time.sleep(1)

browser.close()

print(f"\n{'='*60}")
print("SUMMARY")
print('='*60)
print(f"Total passes:  {passes}")
print(f"403 blocked:    {fails_403}")
print(f"404 no data:    {fails_404}")
print(f"Other errors:   {fails_other}")
print(f"403 rate:       {fails_403/(passes+fails_403+fails_404+fails_other)*100:.1f}%" if (passes+fails_403+fails_404+fails_other) else "N/A")
print(f"\n[{time.strftime('%H:%M:%S')}] Done.")
