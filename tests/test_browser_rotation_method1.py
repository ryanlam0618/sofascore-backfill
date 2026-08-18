#!/usr/bin/env python3
"""
Browser Rotation Method 1 Test:
Test when SofaScore blocks a browser during continuous requests.
This script tests the optimal rotation interval.

Observation: Browser gets blocked after ~150 requests if not rotated.

Method 1: Rotate browser every N requests
"""

import json
import time
import signal
import sys
from datetime import datetime

# Import CloakBrowser
try:
    from cloakbrowser import launch
except ImportError:
    print("CloakBrowser not found. Installing...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "cloakbrowser"])
    from cloakbrowser import launch

# Configuration - same as test_cloakbrowser_mass.py
PROXY_CONFIG = {
    'server': 'http://p.webshare.io:80',
    'username': 'aeptenjc-rotate',
    'password': 'dztr57tcycoz'
}

# Test parameters
MAX_REQUESTS = 300  # Maximum to test
ROTATE_EVERY_N = 0  # Set to 0 to test when blocked, or set N to rotate every N requests

# Tracking
request_count = 0
blocked = False
blocked_at_request = None
results = []

def signal_handler(sig, frame):
    print(f"\n[INFO] Received interrupt at request {request_count}")
    print("Saving results...")
    save_results()
    sys.exit(0)

def save_results():
    global results
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"browser_rotation_method1_test_{timestamp}.json"
    with open(filename, "w") as f:
        json.dump({
            "config": {
                "max_requests": MAX_REQUESTS,
                "rotate_every_n": ROTATE_EVERY_N,
                "proxy": PROXY_CONFIG
            },
            "results": results,
            "summary": {
                "total_requests": request_count,
                "blocked": blocked,
                "blocked_at": blocked_at_request if blocked else None
            }
        }, f, indent=2, ensure_ascii=False)
    print(f"[INFO] Results saved to {filename}")

def create_browser():
    """Create a new browser instance with proxy - Method 1"""
    return launch(
        headless=True,  # Set to False if you want to see the browser
        humanize=True,
        proxy=PROXY_CONFIG
    )

def test_request(browser, url, description):
    """Test a single request"""
    global request_count, blocked, blocked_at_request
    
    if blocked:
        return {"status": "skipped", "reason": "already_blocked"}
    
    request_count += 1
    print(f"\n[{request_count}] Testing: {description}")
    print(f"    URL: {url}")
    
    try:
        page = browser.new_page()
        resp = page.goto(url, timeout=30000)
        status = resp.status if resp else 0
        
        result = {
            "request_num": request_count,
            "url": url,
            "description": description,
            "status": status,
            "blocked": False,
            "timestamp": time.time()
        }
        
        if status == 403:
            print(f"    ⛔️ BLOCKED at request #{request_count}!")
            blocked = True
            blocked_at_request = request_count
            result["blocked"] = True
        elif status == 200:
            print(f"    ✅ OK (200)")
        else:
            print(f"    ⚠️ Status: {status}")
        
        results.append(result)
        page.close()
        return result
        
    except Exception as e:
        error_msg = str(e)[:100]
        print(f"    ❌ Error: {error_msg}")
        result = {
            "request_num": request_count,
            "url": url,
            "description": description,
            "error": error_msg,
            "blocked": False
        }
        results.append(result)
        return result

def main():
    global blocked, blocked_at_request, browser, page
    
    # Setup signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    print("=" * 60)
    print("Browser Rotation Method 1 Test")
    print(f"Max requests: {MAX_REQUESTS}")
    print(f"Rotate every N requests: {'OFF' if ROTATE_EVERY_N == 0 else ROTATE_EVERY_N}")
    print("=" * 60)
    
    # Get test events
    ut_id = 11  # Premier League
    test_events = []
    
    print("\n🔍 Preparing test events...")
    
    browser = create_browser()
    page = browser.new_page()
    
    # Fetch event URLs
    test_url = f"https://www.sofascore.com/api/v1/unique-tournament/{ut_id}/season/61627/events/round/1"
    resp = page.goto(test_url, timeout=30000)
    
    if resp and resp.status == 200:
        body = page.evaluate("() => document.body.innerText")
        data = json.loads(body)
        events = data.get("events", [])[:50]  # Get 50 events for testing
        
        for ev in events:
            eid = ev.get("id")
            home = ev.get("homeTeam", {}).get("name", "")
            away = ev.get("awayTeam", {}).get("name", "")
            test_events.append({
                "url": f"https://www.sofascore.com/event/{eid}",
                "description": f"{home} vs {away}",
                "eid": eid
            })
    
    page.close()
    print(f"✅ Prepared {len(test_events)} test events")
    
    # Main test loop
    print("\n" + "=" * 60)
    print("Starting test...")
    print("=" * 60)
    
    page = browser.new_page()
    
    for i, event in enumerate(test_events):
        if blocked:
            print(f"\n🚨 Browser was blocked at request #{blocked_at_request}")
            break
        
        if request_count >= MAX_REQUESTS:
            print(f"\n⚠️ Reached max request limit ({MAX_REQUESTS})")
            break
        
        test_request(browser, event["url"], event["description"])
        
        # Method 1: Rotate browser every N requests
        if ROTATE_EVERY_N > 0 and request_count % ROTATE_EVERY_N == 0:
            print(f"\n🔄 Rotating browser (every {ROTATE_EVERY_N} requests)...")
            page.close()
            browser.close()
            time.sleep(1)
            browser = create_browser()
            page = browser.new_page()
        
        # Small delay to simulate human behavior
        time.sleep(0.3)
    
    # Cleanup
    try:
        page.close()
        browser.close()
    except:
        pass
    
    # Summary
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    print(f"Total requests: {request_count}")
    print(f"Blocked: {blocked}")
    if blocked:
        print(f"Blocked at request: #{blocked_at_request}")
    
    # Calculate blocking point
    if blocked and blocked_at_request:
        print(f"\n📊 Guess: Block threshold is around {blocked_at_request} requests")
        if blocked_at_request > 100:
            print(f"   Suggestion: Rotate every {min(blocked_at_request // 2, 50)} to 100 requests")
    
    save_results()

if __name__ == "__main__":
    main()