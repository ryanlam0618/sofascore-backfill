#!/usr/bin/env python3
"""
Test each Webshare proxy against SofaScore API.
Reports: working, slow, 403-blocked, timeout, connection-error, bad-proxy.
"""

import sys
import time
import csv
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

PROXY_FILE = "proxy_list.txt"
OUTPUT_FILE = "proxy_test_results.csv"
TEST_URL = "https://api.sofascore.com/api/v1/unique-tournament/17/season/76986/rounds"
TIMEOUT = 15  # seconds per request
MAX_WORKERS = 10  # parallel tests (don't hammer too hard)
AUTH_USER = "aeptenjc"
AUTH_PASS = "dztr57tcycoz"

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
    """Test a single proxy against SofaScore API. Returns result dict."""
    ip, port, user, pwd = proxy["ip"], proxy["port"], proxy["user"], proxy["pwd"]
    proxy_url = f"http://{user}:{pwd}@{ip}:{port}"
    proxies = {"http": proxy_url, "https": proxy_url}
    
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
    
    # Step 1: Quick IP check
    try:
        t0 = time.time()
        r = requests.get("https://ipv4.webshare.io/", proxies=proxies, timeout=TIMEOUT)
        result["ip_check"] = r.text.strip()
        result["response_time_ms"] = int((time.time() - t0) * 1000)
    except requests.exceptions.ProxyError as e:
        result["status"] = "PROXY_ERROR"
        result["error"] = str(e)[:200]
        return result
    except requests.exceptions.ConnectionError as e:
        result["status"] = "CONN_ERROR"
        result["error"] = str(e)[:200]
        return result
    except requests.exceptions.Timeout:
        result["status"] = "IP_CHECK_TIMEOUT"
        result["error"] = "IP check timed out"
        return result
    except Exception as e:
        result["status"] = "UNKNOWN_ERROR"
        result["error"] = str(e)[:200]
        return result
    
    # Step 2: Test against SofaScore API
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        }
        t0 = time.time()
        r = requests.get(TEST_URL, proxies=proxies, headers=headers, timeout=TIMEOUT)
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
    except requests.exceptions.ProxyError as e:
        result["status"] = "PROXY_ERROR"
        result["error"] = str(e)[:200]
    except requests.exceptions.ConnectionError as e:
        result["status"] = "CONN_ERROR"
        result["error"] = str(e)[:200]
    except requests.exceptions.Timeout:
        result["status"] = "TIMEOUT"
        result["error"] = f"Timed out after {TIMEOUT}s"
    except Exception as e:
        result["status"] = "UNKNOWN_ERROR"
        result["error"] = str(e)[:200]
    
    return result

def main():
    proxies = load_proxies(PROXY_FILE)
    print(f"Loaded {len(proxies)} proxies")
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
            icon = {"OK": "✅", "BLOCKED_403": "🚫", "TIMEOUT": "⏰", "PROXY_ERROR": "💀", "CONN_ERROR": "🔌"}.get(status, "❓")
            print(f"[{done:3d}/{len(proxies)}] {icon} {ip}:{result['port']} → {status} (HTTP {code}, {ms}ms)")
    
    # Save to CSV
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
        # Show fastest 5
        fastest = sorted(working, key=lambda r: r["response_time_ms"])[:5]
        print("   Fastest 5:")
        for r in fastest:
            print(f"     {r['ip']}:{r['port']} → {r['response_time_ms']}ms (IP: {r['ip_check']})")
    
    blocked = [r for r in results if r["status"] == "BLOCKED_403"]
    if blocked:
        print(f"\n🚫 Blocked by SofaScore (403): {len(blocked)}")
        for r in blocked:
            print(f"     {r['ip']}:{r['port']}")
    
    bad = [r for r in results if r["status"] in ("PROXY_ERROR", "CONN_ERROR", "TIMEOUT")]
    if bad:
        print(f"\n💀 Bad proxies: {len(bad)}")
        for r in bad:
            print(f"     {r['ip']}:{r['port']} → {r['status']}: {r.get('error', '')[:80]}")
    
    print(f"\nFull results saved to: {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
