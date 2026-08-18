#!/usr/bin/env python3
"""Test CloakBrowser against SofaScore — see if it bypasses 403."""

import time
from cloakbrowser import launch

print("=== CloakBrowser -> SofaScore Test ===")
print(f"[{time.strftime('%H:%M:%S')}] Launching CloakBrowser (headless)...")

browser = launch(
    headless=True,
    humanize=True,
)

page = browser.new_page()

# Test 1: SofaScore homepage
print(f"[{time.strftime('%H:%M:%S')}] Test 1: Loading SofaScore homepage...")
try:
    resp = page.goto("https://www.sofascore.com", timeout=30000)
    status = resp.status if resp else 'N/A'
    title = page.title()
    print(f"  Status: {status}")
    print(f"  Title: {title}")
    if status == 200:
        print("  ✅ Homepage loaded OK")
    elif status == 403:
        print("  ❌ Homepage got 403")
    else:
        print(f"  ⚠️ Homepage got status {status}")
except Exception as e:
    print(f"  ❌ Homepage failed: {e}")

# Test 2: A real event page
event_url = "https://www.sofascore.com/event/12347993"
print(f"\n[{time.strftime('%H:%M:%S')}] Test 2: Loading event page {event_url}...")
try:
    resp = page.goto(event_url, timeout=30000)
    status = resp.status if resp else 'N/A'
    title = page.title()
    print(f"  Status: {status}")
    print(f"  Title: {title}")
    if status == 200:
        print("  ✅ Event page loaded OK — no 403!")
    elif status == 403:
        print("  ❌ Event page got 403 — CloakBrowser did NOT bypass")
    else:
        print(f"  ⚠️ Event page got status {status}")
except Exception as e:
    print(f"  ❌ Event page failed: {e}")

# Test 3: API endpoint via browser
api_url = "https://www.sofascore.com/api/v1/event/12347993"
print(f"\n[{time.strftime('%H:%M:%S')}] Test 3: Loading API endpoint {api_url}...")
try:
    resp = page.goto(api_url, timeout=30000)
    status = resp.status if resp else 'N/A'
    print(f"  Status: {status}")
    if status == 200:
        body = page.evaluate("() => document.body.innerText")
        print(f"  Body preview: {body[:300]}")
        print("  ✅ API endpoint OK — no 403!")
    elif status == 403:
        print("  ❌ API got 403 — CloakBrowser did NOT bypass")
    else:
        print(f"  ⚠️ API got status {status}")
except Exception as e:
    print(f"  ❌ API endpoint failed: {e}")

# Test 4: Tournament page
tourney_url = "https://www.sofascore.com/tournament/football/england/premier-league/17"
print(f"\n[{time.strftime('%H:%M:%S')}] Test 4: Loading tournament page {tourney_url}...")
try:
    resp = page.goto(tourney_url, timeout=30000)
    status = resp.status if resp else 'N/A'
    title = page.title()
    print(f"  Status: {status}")
    print(f"  Title: {title}")
    if status == 200:
        print("  ✅ Tournament page OK")
    elif status == 403:
        print("  ❌ Tournament page got 403")
    else:
        print(f"  ⚠️ Tournament page got status {status}")
except Exception as e:
    print(f"  ❌ Tournament page failed: {e}")

browser.close()
print(f"\n[{time.strftime('%H:%M:%S')}] Done. Browser closed.")
