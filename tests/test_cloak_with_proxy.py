#!/usr/bin/env python3
"""
Test: CloakBrowser + proxy vs SofaScore - single event
Same as test_cloak_direct.py but WITH proxy.
"""

import json
import time
from cloakbrowser import launch

BROWSER_BASE = "https://www.sofascore.com"

EVENT_ID = 12436870
EVENT_PAGE_URL = f"{BROWSER_BASE}/event/{EVENT_ID}"
API_URLS = [
    f"{BROWSER_BASE}/api/v1/event/{EVENT_ID}",
    f"{BROWSER_BASE}/api/v1/event/{EVENT_ID}/incidents",
    f"{BROWSER_BASE}/api/v1/event/{EVENT_ID}/lineups",
    f"{BROWSER_BASE}/api/v1/event/{EVENT_ID}/shotmap",
]

PROXY_CONFIG = {
    'server': 'http://p.webshare.io:80',
    'username': 'aeptenjc-rotate',
    'password': 'dztr57tcycoz'
}

print("=" * 60)
print("CloakBrowser + Proxy Test (WITH PROXY)")
print("=" * 60)

print("\n[1] Launching CloakBrowser with proxy...")
browser = launch(
    headless=True,
    humanize=True,
    proxy=PROXY_CONFIG,
)
page = browser.new_page()
print("    ✅ Browser launched (with proxy)")

# Step 1: Warm homepage
print("\n[2] Warming homepage...")
try:
    resp = page.goto(f"{BROWSER_BASE}/", timeout=30000)
    status = resp.status if resp else 0
    print(f"    Homepage: {status}")
    time.sleep(2)
except Exception as e:
    print(f"    ❌ Homepage error: {e}")

# Step 2: Visit event page
print(f"\n[3] Visiting event page: {EVENT_PAGE_URL}")
try:
    resp = page.goto(EVENT_PAGE_URL, timeout=30000)
    status = resp.status if resp else 0
    print(f"    Event page: {status}")
    if status == 200:
        print("    ✅ Event page OK")
    elif status == 403:
        print("    ❌ Event page BLOCKED (403)")
    time.sleep(2)
except Exception as e:
    print(f"    ❌ Event page error: {e}")

# Step 3: Fetch API endpoints directly
print(f"\n[4] Testing API endpoints (direct page.goto):")
for url in API_URLS:
    ep_name = url.split("/")[-1]
    try:
        resp = page.goto(url, timeout=30000)
        status = resp.status if resp else 0
        if status == 200:
            body = page.evaluate("() => document.body.innerText")
            body_len = len(body) if body else 0
            print(f"    {ep_name}: ✅ 200 ({body_len} bytes)")
        elif status == 403:
            print(f"    {ep_name}: ❌ 403")
        elif status == 404:
            print(f"    {ep_name}: ⚠️ 404 (no data)")
        else:
            print(f"    {ep_name}: ⚠️ {status}")
    except Exception as e:
        print(f"    {ep_name}: ❌ Error: {str(e)[:80]}")
    time.sleep(1)

# Cleanup
page.close()
browser.close()

print("\n" + "=" * 60)
print("Done.")
print("=" * 60)
