#!/usr/bin/env python3
"""Test CloakBrowser against SofaScore — broader test with multiple events & API endpoints."""

import time
import json
from cloakbrowser import launch

print("=== CloakBrowser -> SofaScore Broader Test ===")
print(f"[{time.strftime('%H:%M:%S')}] Launching CloakBrowser (headless)...")

browser = launch(headless=True, humanize=True)
page = browser.new_page()

# Multiple event IDs from different competitions
test_events = [
    (12347993, "FA Cup China"),
    (12436875, "Premier League"),
    (13792057, "Another event"),
    (14023959, "Event 14023959"),
]

api_endpoints = [
    "/api/v1/event/{eid}",
    "/api/v1/event/{eid}/incidents",
    "/api/v1/event/{eid}/lineups",
    "/api/v1/event/{eid}/statistics",
    "/api/v1/event/{eid}/shotmap",
    "/api/v1/event/{eid}/odds/1",
]

results = {"passes": 0, "fails": 0, "details": []}

for eid, label in test_events:
    print(f"\n--- Event {eid} ({label}) ---")
    # Warm event page first
    event_url = f"https://www.sofascore.com/event/{eid}"
    try:
        resp = page.goto(event_url, timeout=30000)
        status = resp.status if resp else 'N/A'
        title = page.title()
        print(f"  Event page: {status} | {title}")
        if status == 200:
            results["passes"] += 1
            results["details"].append(f"event_page_{eid}: 200 ✅")
        else:
            results["fails"] += 1
            results["details"].append(f"event_page_{eid}: {status} ❌")
    except Exception as e:
        results["fails"] += 1
        results["details"].append(f"event_page_{eid}: error ❌ ({e})")
        print(f"  Event page error: {e}")
        continue

    # Test API endpoints
    for endpoint in api_endpoints:
        path = endpoint.format(eid=eid)
        url = f"https://www.sofascore.com{path}"
        try:
            resp = page.goto(url, timeout=30000)
            status = resp.status if resp else 'N/A'
            if status == 200:
                body = page.evaluate("() => document.body.innerText")
                body_len = len(body) if body else 0
                print(f"  {path}: {status} (body: {body_len} chars) ✅")
                results["passes"] += 1
                results["details"].append(f"api_{eid}_{path}: 200 ✅")
            elif status == 403:
                print(f"  {path}: 403 ❌")
                results["fails"] += 1
                results["details"].append(f"api_{eid}_{path}: 403 ❌")
            else:
                print(f"  {path}: {status} ⚠️")
                results["fails"] += 1
                results["details"].append(f"api_{eid}_{path}: {status} ⚠️")
        except Exception as e:
            print(f"  {path}: error ❌ ({e})")
            results["fails"] += 1
            results["details"].append(f"api_{eid}_{path}: error ❌")

    time.sleep(2)  # polite delay between events

# Test tournament API endpoints
print("\n--- Tournament API Tests ---")
tournament_apis = [
    "/api/v1/unique-tournament/17/seasons",
    "/api/v1/unique-tournament/17/season/61627/rounds",
    "/api/v1/unique-tournament/17/season/61627/standings",
]
for path in tournament_apis:
    url = f"https://www.sofascore.com{path}"
    try:
        resp = page.goto(url, timeout=30000)
        status = resp.status if resp else 'N/A'
        if status == 200:
            body = page.evaluate("() => document.body.innerText")
            body_len = len(body) if body else 0
            print(f"  {path}: {status} (body: {body_len} chars) ✅")
            results["passes"] += 1
            results["details"].append(f"tournament_{path}: 200 ✅")
        elif status == 403:
            print(f"  {path}: 403 ❌")
            results["fails"] += 1
            results["details"].append(f"tournament_{path}: 403 ❌")
        else:
            print(f"  {path}: {status} ⚠️")
            results["fails"] += 1
            results["details"].append(f"tournament_{path}: {status} ⚠️")
    except Exception as e:
        print(f"  {path}: error ❌ ({e})")
        results["fails"] += 1
        results["details"].append(f"tournament_{path}: error ❌")

browser.close()

print(f"\n=== SUMMARY ===")
print(f"Passes: {results['passes']}")
print(f"Fails:  {results['fails']}")
print(f"Total:  {results['passes'] + results['fails']}")
print(f"Rate:   {results['passes']/(results['passes']+results['fails'])*100:.1f}%")
print(f"\n[{time.strftime('%H:%M:%S')}] Done.")
