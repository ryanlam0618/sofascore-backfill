#!/usr/bin/env python3
"""
Test CloakBrowser + webshare proxy against SofaScore API.
Simple test: fetch one event endpoint and report status.
"""

import json
import time
from cloakbrowser import launch

PROXY_CONFIG = {
    'server': 'http://p.webshare.io:80',
    'username': 'aeptenjc-rotate',
    'password': 'dztr57tcycoz'
}

# Known working endpoint from previous tests
TEST_URL = "https://www.sofascore.com/api/v1/unique-tournament/11/season/61627/events/round/1"

print("=" * 60)
print("CloakBrowser + Proxy Test")
print("=" * 60)

print(f"\nLaunching CloakBrowser with proxy...")
browser = launch(
    headless=True,
    humanize=True,
    proxy=PROXY_CONFIG
)

page = browser.new_page()

print(f"Testing: {TEST_URL}")
try:
    resp = page.goto(TEST_URL, timeout=30000)
    status = resp.status if resp else 0
    print(f"\nStatus: {status}")
    
    if status == 200:
        body = page.evaluate("() => document.body.innerText")
        data = json.loads(body)
        events = data.get("events", [])
        print(f"✅ SUCCESS! Got {len(events)} events")
        if events:
            ev = events[0]
            home = ev.get("homeTeam", {}).get("name", "?")
            away = ev.get("awayTeam", {}).get("name", "?")
            print(f"   First event: {home} vs {away}")
    elif status == 403:
        print(f"❌ BLOCKED (403)")
    else:
        print(f"⚠️ Status: {status}")
        body = page.evaluate("() => document.body.innerText")
        print(f"   Body: {body[:200] if body else 'empty'}")
except Exception as e:
    print(f"❌ Error: {e}")

# Also test event detail endpoint
EVENT_URL = "https://www.sofascore.com/api/v1/event/12436870"
print(f"\nTesting: {EVENT_URL}")
try:
    resp = page.goto(EVENT_URL, timeout=30000)
    status = resp.status if resp else 0
    print(f"Status: {status}")
    if status == 200:
        body = page.evaluate("() => document.body.innerText")
        data = json.loads(body)
        ev = data.get("event", {})
        home = ev.get("homeTeam", {}).get("name", "?")
        away = ev.get("awayTeam", {}).get("name", "?")
        print(f"✅ SUCCESS! Event: {home} vs {away}")
    elif status == 403:
        print(f"❌ BLOCKED (403)")
    else:
        print(f"⚠️ Status: {status}")
except Exception as e:
    print(f"❌ Error: {e}")

page.close()
browser.close()
print(f"\nDone.")
