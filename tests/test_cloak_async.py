#!/usr/bin/env python3
"""
Test: CloakBrowser (async) + webshare proxy vs SofaScore
Mimic backfill_runner.py's approach: warm homepage → warm tournament → fetch API
"""

import json
import time
import asyncio
from cloakbrowser import launch_async

PROXY_CONFIG = {
    'server': 'http://p.webshare.io:80',
    'username': 'aeptenjc-rotate',
    'password': 'dztr57tcycoz'
}

BROWSER_BASE = "https://www.sofascore.com"

async def main():
    print("=" * 60)
    print("CloakBrowser Async + Proxy Test")
    print("=" * 60)

    # Launch CloakBrowser with proxy + humanize
    print("\n[1] Launching CloakBrowser...")
    browser = await launch_async(
        headless=True,
        humanize=True,
        proxy=PROXY_CONFIG
    )
    print("    ✅ Browser launched")

    # Create context + page
    context = await browser.new_context()
    page = await context.new_page()
    print("    ✅ Context + page created")

    # Step 1: Warm homepage (establish session cookies)
    print("\n[2] Warming homepage...")
    try:
        resp = await page.goto(f"{BROWSER_BASE}/", timeout=30000, wait_until="domcontentloaded")
        status = resp.status if resp else 0
        print(f"    Homepage status: {status}")
        if status == 200:
            print("    ✅ Homepage OK")
        elif status == 403:
            print("    ❌ Homepage BLOCKED (403)")
        await asyncio.sleep(2)
    except Exception as e:
        print(f"    ❌ Homepage error: {e}")

    # Step 2: Warm tournament page
    print("\n[3] Warming tournament page...")
    tournament_url = f"{BROWSER_BASE}/football/unique-tournament/11/season/61627"
    try:
        resp = await page.goto(tournament_url, timeout=30000, wait_until="domcontentloaded")
        status = resp.status if resp else 0
        print(f"    Tournament status: {status}")
        if status == 200:
            print("    ✅ Tournament page OK")
        elif status == 403:
            print("    ❌ Tournament page BLOCKED (403)")
        await asyncio.sleep(3)
    except Exception as e:
        print(f"    ❌ Tournament error: {e}")

    # Step 3: Fetch API endpoint directly via page.goto
    print("\n[4] Fetching API endpoint via page.goto...")
    api_url = f"{BROWSER_BASE}/api/v1/unique-tournament/11/season/61627/events/round/1"
    try:
        resp = await page.goto(api_url, timeout=30000, wait_until="domcontentloaded")
        status = resp.status if resp else 0
        print(f"    API status: {status}")
        if status == 200:
            body = await page.evaluate("() => document.body.innerText")
            data = json.loads(body)
            events = data.get("events", [])
            print(f"    ✅ Got {len(events)} events")
            if events:
                ev = events[0]
                home = ev.get("homeTeam", {}).get("name", "?")
                away = ev.get("awayTeam", {}).get("name", "?")
                print(f"    First: {home} vs {away}")
        elif status == 403:
            print("    ❌ API BLOCKED (403)")
        else:
            body = await page.evaluate("() => document.body.innerText")
            print(f"    ⚠️ Status: {status}, body: {body[:200] if body else 'empty'}")
    except Exception as e:
        print(f"    ❌ API error: {e}")

    # Step 4: Fetch event detail API
    print("\n[5] Fetching event detail API...")
    event_api_url = f"{BROWSER_BASE}/api/v1/event/12436870"
    try:
        resp = await page.goto(event_api_url, timeout=30000, wait_until="domcontentloaded")
        status = resp.status if resp else 0
        print(f"    Event API status: {status}")
        if status == 200:
            body = await page.evaluate("() => document.body.innerText")
            data = json.loads(body)
            ev = data.get("event", {})
            home = ev.get("homeTeam", {}).get("name", "?")
            away = ev.get("awayTeam", {}).get("name", "?")
            print(f"    ✅ Event: {home} vs {away}")
        elif status == 403:
            print("    ❌ Event API BLOCKED (403)")
        else:
            print(f"    ⚠️ Status: {status}")
    except Exception as e:
        print(f"    ❌ Event API error: {e}")

    # Cleanup
    await page.close()
    await context.close()
    await browser.close()

    print("\n" + "=" * 60)
    print("Done.")
    print("=" * 60)

asyncio.run(main())
