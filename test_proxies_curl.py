#!/usr/bin/env python3
"""
Test Webshare proxies using curl_cffi (same method as backfill_runner.py).
Uses impersonate="chrome" to mimic real Chrome TLS fingerprint.
"""

import time
import csv
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from curl_cffi import requests as curl_requests

PROXY_FILE = "proxy_list.txt"
OUTPUT_FILE = "proxy_test_curl_cffi.csv"
TEST_URL = "https://api.sofascore.com/api/v1/unique-tournament/17/season/76986/rounds"
TIMEOUT = 15
MAX_WORKERS = 5  # fewer workers to avoid rate limiting ourselves

def load_proxies(path):
    proxies = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(":")
            if len(parts) == 4:
                ip, port, user, pwd = parts
                proxies.append({"ip": ip, "port": port, "user": user, "pwd": pwd})
    return proxies

def test_proxy(proxy):
    ip, port, user, pwd = proxy["ip"], proxy["port"], proxy["user"], proxy["pwd"]
    proxy_url = f"http://{user}:{pwd}@{ip}:{port}"
    
    result = {
        "ip": ip,
        "port": port,
        "proxy_url": proxy_url,
        "status": None,
        "http_code": None,
        "response_time_ms": None,
        "error": None,
        "ip_check": None,
        "timestamp": datetime.now().isoformat(),
    }
    
    # Step 1: IP check via curl_cffi
    try:
        t0 = time.time()
        r = curl_requests.get(
            "https://ipv4.webshare.io/",
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=TIMEOUT,
            impersonate="chrome",
        )
        result["ip_check"] = r.text.strip()
        result["response_time_ms"] = int((time.time() - t0) * 1000)
    except Exception as e:
        result["status"] = "IP_CHECK_FAIL"
        result["error"] = str(e)[:200]
        return result
    
    # Step 2: Test SofaScore API with curl_cffi + impersonate=chrome
    try:
        headers = {
            "Accept": "application/json",
        }
        t0 = time.time()
        r = curl_requests.get(
            TEST_URL,
            proxies={"http": proxy_url, "https": proxy_url},
            headers=headers,
            timeout=TIMEOUT,
            impersonate="chrome",
        )
        elapsed = int((time.time() - t0) * 1000)
        result["http_code"] = r.status_code
        result["response_time_ms"] = elapsed
        
        if r.status_code == 200:
            result["status"] = "OK"
        elif r.status_code == 403:
            result["status"] = "BLOCKED_403"
        elif r.status_code == 404:
            result["status"] = "NOT_FOUND_404"
        elif r.status_code == 429:
            result["status"] = "RATE_LIMITED_429"
        else:
            result["status"] = f"HTTP_{r.status_code}"
    except Exception as e:
        result["status"] = "REQUEST_ERROR"
        result["error"] = str(e)[:200]
    
    return result

def main():
    proxies = load_proxies(PROXY_FILE)
    print(f"Loaded {len(proxies)} proxies")
    print(f"Method: curl_cffi + impersonate=chrome")
    print(f"Testing against: {TEST_URL}")
    print(f"Timeout: {TIMEOUT}s | Workers: {MAX_WORKERS}")
    print("=" * 70)
    
    results = []
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(test_proxy, p): p for p in proxies}
        done = 0
        for future in as_completed(futures):
            done += 1
            result = future.result()
            results.append(result)
            status = result["status"]
            ip = result["ip"]
            ms = result.get("response_time_ms", "")
            code = result.get("http_code", "")
            icon = {"OK": "✅", "BLOCKED_403": "🚫", "TIMEOUT": "⏰", "REQUEST_ERROR": "💀", "IP_CHECK_FAIL": "🔌"}.get(status, "❓")
            print(f"[{done:3d}/{len(proxies)}] {icon} {ip}:{result['port']} → {status} (HTTP {code}, {ms}ms)")
    
    # Save CSV
    fieldnames = ["ip", "port", "proxy_url", "status", "http_code", "response_time_ms", "ip_check", "error", "timestamp"]
    with open(OUTPUT_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)
    
    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    
    from collections import Counter
    counts = Counter(r["status"] for r in results)
    for status, count in counts.most_common():
        print(f"  {status}: {count}")
    
    working = [r for r in results if r["status"] == "OK"]
    if working:
        avg_ms = sum(r["response_time_ms"] for r in working) / len(working)
        print(f"\n✅ Working proxies: {len(working)}")
        print(f"   Avg response time: {avg_ms:.0f}ms")
        fastest = sorted(working, key=lambda r: r["response_time_ms"])[:5]
        print("   Fastest 5:")
        for r in fastest:
            print(f"     {r['ip']}:{r['port']} → {r['response_time_ms']}ms (IP: {r['ip_check']})")
    
    blocked = [r for r in results if r["status"] == "BLOCKED_403"]
    if blocked:
        print(f"\n🚫 Blocked by SofaScore (403): {len(blocked)}")
    
    bad = [r for r in results if r["status"] in ("REQUEST_ERROR", "IP_CHECK_FAIL")]
    if bad:
        print(f"\n💀 Bad proxies: {len(bad)}")
        for r in bad:
            print(f"     {r['ip']}:{r['port']} → {r['status']}: {r.get('error', '')[:80]}")
    
    print(f"\nFull results saved to: {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
