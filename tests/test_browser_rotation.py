#!/usr/bin/env python3
"""
Test browser rotation timing:
When the same browser makes too many requests, SofaScore detects and blocks it.
This script tests different rotation intervals to find the optimal value.

Target: Find the threshold where browser gets blocked.
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

# Configuration
PROXY_CONFIG = {
    'server': 'http://p.webshare.io:80',
    'username': 'aeptenjc-rotate',
    'password': 'dztr57tcycoz'
}

# Test tracking
request_count = 0
blocked = False
blocked_at = None
results = []
# Maximum requests before we stop (to avoid long runs if never blocked)
MAX_REQUESTS = 500

# Rotation settings
ROTATE_EVERY_N = 0  # 0 means no rotation - test when we get blocked
ROTATE_BROWSER = False  # Whether to rotate browser on each N requests

def signal_handler(sig, frame):
    print(f"\n[Signal] Received interrupt at request {request_count}")
    print("Saving results...")
    save_results()
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

def save_results():
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"browser_rotation_test_{timestamp}.json"
    with open(filename, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"Results saved to {filename}")

def test_single_request(browser, url, description):
    """Test a single request and return status"""
    global request_count, blocked, blocked_at
    
    if blocked:
        return {"status": "already_blocked", "skipped": True}
    
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
            "timestamp": time.time()
        }
        
        if status == 403:
            print(f"    📋 BLOCKED at request #{request_count}!")
            blocked = True
            blocked_at = request_count
            result["blocked"] = True
        elif status == 200:
            print(f"    ✅ OK (200)")
            result["blocked"] = False
        else:
            print(f"    ⚠️ Status: {status}")
            result["blocked"] = False
        
        results.append(result)
        page.close()
        return result
        
    except Exception as e:
        print(f"    ❌ Error: {str(e)[:100]}")
        result = {
            "request_num": request_count,
            "url": url,
            "description": description,
            "error": str(e),
            "blocked": False
        }
        results.append(result)
        return result

def main():
    global blocked, blocked_at
    
    print("=" * 60)
    print("Browser Rotation Timing Test")
    print("Testing when SofaScore blocks a browser")
    print("=" * 60)
    
    # Test target - get events from different seasons
    base_url = "https://www.sofascore.com"
    
    # Use a specific season with multiple events
    ut_id = 11  # Premier League
    test_events = []
    
    # First, get some event URLs to test
    print("\n🔍 Preparing test URLs...")
    
    try:
        browser = launch(
            headless=False,  # Show browser for debugging
            humanize=True,
            proxy=PROXY_CONFIG
        )
        
        # Get a list of events from a season
        test_url = f"https://www.sofascore.com/api/v1/unique-tournament/{ut_id}/season/61627/events/round/1"
        page = browser.new_page()
        resp = page.goto(test_url, timeout=30000)
        
        if resp and resp.status == 200:
            body = page.evaluate("() => document.body.innerText")
            data = json.loads(body)
            events = data.get("events", [])[:20]  # Get first 20 events
            
            for ev in events:
                eid = ev.get("id")
                home = ev.get("homeTeam", {}).get("name", "")
                away = ev.get("awayTeam", {}).get("name", "")
                test_events.append({
                    "url": f"{base_url}/event/{eid}",
                    "description": f"{home} vs {away}",
                    "eid": eid
                })
        
        page.close()
        print(f"✅ Prepared {len(test_events)} test events")
        
        # Now test without rotation - see when we get blocked
        print("\n" + "=" * 60)
        print("Starting rotation-free test...")
        print("Expected: Should get blocked around 150 requests")
        print("=" * 60)
        
        for i, event in enumerate(test_events):
            if blocked:
                print(f"\n🚨 Browser was blocked at request #{blocked_at}")
                break
            
            test_single_request(
                browser,
                event["url"],
                event["description"]
            )
            
            # Add a small delay to simulate human behavior
            time.sleep(0.5)
        
        browser.close()
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
    
    # Summary
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    print(f"Total requests: {request_count}")
    if blocked:
        print(f"🚨 BLOCKED at request #{blocked_at}")
    else:
        print(f"✅ Not blocked in {request_count} requests")
    
    save_results()

if __name__ == "__main__":
    main()