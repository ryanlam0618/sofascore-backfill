#!/usr/bin/env python3
"""
Test: CloakBrowser + network capture method (mimic backfill_runner.py)
Instead of page.goto() to API endpoint, visit the event page and intercept
the browser's own API responses via context.on("response").
"""

import json
import time
import asyncio
import random
from cloakbrowser import launch_async

PROXY_CONFIG = {
    'server': 'http://p.webshare.io:80',
    'username': 'aeptenjc-rotate',
    'password': 'dztr57tcycoz'
}

BROWSER_BASE = "https://www.sofascore.com"

async def main():
    print("=" * 60)
    print("CloakBrowser + Network Capture Test")
    print("=" * 60)

    # Launch
    print("\n[1] Launching CloakBrowser...")
    browser = await launch_async(
        headless=True,
        humanize=True,
        proxy=PROXY_CONFIG
    )
    context = await browser.new_context()
    page = await context.new_page()
    print("    ✅ Browser launched")

    # Network capture setup
    response_cache = {}
    response_waiters = {}

    async def handle_response(response):
        try:
            url = response.url
            if not url.startswith(BROWSER_BASE + "/api/v1/"):
                return
            path = url[len(BROWSER_BASE):]
            payload = {"status": response.status, "body": None}
            try:
                payload["body"] = await response.json()
            except Exception:
                try:
                    payload["body"] = await response.text()
                except Exception:
                    payload["body"] = None
            response_cache[path] = payload
            print(f"    📦 Captured: {path} → {response.status}")
            waiter = response_waiters.pop(path, None)
            if waiter and not waiter.done():
                waiter.set_result(payload)
        except Exception:
            return

    context.on("response", handle_response)

    async def wait_for_api(path, timeout_s=15):
        """Wait for a specific API path to be captured."""
        if path in response_cache:
            return response_cache[path]
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        response_waiters[path] = fut
        try:
            return await asyncio.wait_for(fut, timeout_s)
        except asyncio.TimeoutError:
            response_waiters.pop(path, None)
            return None

    # Step 1: Warm homepage
    print("\n[2] Warming homepage...")
    try:
        resp = await page.goto(f"{BROWSER_BASE}/", timeout=30000, wait_until="domcontentloaded")
        status = resp.status if resp else 0
        print(f"    Homepage: {status}")
        await asyncio.sleep(2)
    except Exception as e:
        print(f"    ❌ Homepage error: {e}")

    # Step 2: Visit tournament page (triggers API calls naturally)
    print("\n[3] Visiting tournament page...")
    tournament_url = f"{BROWSER_BASE}/football/unique-tournament/11/season/61627"
    try:
        resp = await page.goto(tournament_url, timeout=30000, wait_until="domcontentloaded")
        status = resp.status if resp else 0
        print(f"    Tournament page: {status}")
        await asyncio.sleep(3)

        # Humanize
        try:
            await page.mouse.move(random.randint(150, 600), random.randint(120, 420), steps=10)
            await asyncio.sleep(0.5)
            await page.mouse.wheel(0, random.randint(250, 700))
            await asyncio.sleep(1)
        except Exception:
            pass
    except Exception as e:
        print(f"    ❌ Tournament error: {e}")

    # Step 3: Check what API responses were captured
    print("\n[4] Captured API responses so far:")
    if response_cache:
        for path, payload in response_cache.items():
            status = payload["status"]
            body = payload.get("body")
            body_len = len(str(body)) if body else 0
            print(f"    {path} → {status} ({body_len} bytes)")
    else:
        print("    (no API responses captured)")

    # Step 4: Visit event page (triggers event API calls)
    print("\n[5] Visiting event page...")
    event_url = f"{BROWSER_BASE}/event/12436870"
    try:
        resp = await page.goto(event_url, timeout=30000, wait_until="domcontentloaded")
        status = resp.status if resp else 0
        print(f"    Event page: {status}")
        await asyncio.sleep(3)

        # Humanize
        try:
            await page.mouse.move(random.randint(150, 600), random.randint(120, 420), steps=10)
            await asyncio.sleep(0.5)
            await page.mouse.wheel(0, random.randint(250, 700))
            await asyncio.sleep(1)
        except Exception:
            pass
    except Exception as e:
        print(f"    ❌ Event page error: {e}")

    # Step 5: Check all captured API responses
    print("\n[6] All captured API responses:")
    if response_cache:
        for path, payload in response_cache.items():
            status = payload["status"]
            body = payload.get("body")
            body_len = len(str(body)) if body else 0
            print(f"    {path} → {status} ({body_len} bytes)")
            if status == 200 and body:
                # Show brief preview
                preview = str(body)[:150]
                print(f"      preview: {preview}")
    else:
        print("    (no API responses captured)")

    # Step 6: Try waiting for specific API paths
    print("\n[7] Waiting for specific API endpoints...")
    target_paths = [
        "/api/v1/unique-tournament/11/season/61627/events/round/1",
        "/api/v1/event/12436870",
        "/api/v1/event/12436870/incidents",
        "/api/v1/event/12436870/lineups",
    ]
    for path in target_paths:
        result = await wait_for_api(path, timeout_s=5)
        if result:
            print(f"    ✅ {path} → {result['status']}")
        else:
            print(f"    ⏰ {path} → not captured (timeout)")

    # Cleanup
    await page.close()
    await context.close()
    await browser.close()

    print("\n" + "=" * 60)
    print("Done.")
    print("=" * 60)

asyncio.run(main())
