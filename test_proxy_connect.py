#!/usr/bin/env python3
"""Quick test: try each good proxy IP for CONNECT tunnel to Sofascore."""
import asyncio
from curl_cffi import requests as curl_requests

GOOD_PROXIES_PATH = "/root/.openclaw/workspace/sofascore-backfill/data/proxy_audit/good_proxies_20260827_192348.txt"
TARGET_URL = "https://www.sofascore.com/"
TIMEOUT = 15

def load_proxies():
    proxies = []
    with open(GOOD_PROXIES_PATH) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(":")
            if len(parts) >= 4:
                ip, port, user, pwd = parts[0], parts[1], parts[2], parts[3]
                proxies.append((f"{ip}:{port}", user, pwd))
    return proxies

async def test_one(ip_port, user, pwd, idx, total):
    proxy_url = f"http://{user}:{pwd}@{ip_port}"
    print(f"[{idx}/{total}] Testing {ip_port} ... ", end="", flush=True)
    t0 = asyncio.get_event_loop().time()
    try:
        resp = await asyncio.to_thread(
            curl_requests.get, TARGET_URL,
            proxies={"http": proxy_url, "https": proxy_url},
            impersonate="chrome", timeout=TIMEOUT
        )
        dt = int((asyncio.get_event_loop().time() - t0) * 1000)
        status = getattr(resp, "status_code", 0)
        if status == 200:
            print(f"✅ 200 OK ({dt}ms)")
            return ip_port, True, status, dt
        else:
            print(f"❌ HTTP {status} ({dt}ms)")
            return ip_port, False, status, dt
    except Exception as e:
        dt = int((asyncio.get_event_loop().time() - t0) * 1000)
        print(f"❌ ERROR: {e} ({dt}ms)")
        return ip_port, False, 0, dt

async def main():
    proxies = load_proxies()
    print(f"Loaded {len(proxies)} proxies from good_proxies file\n")
    working = []
    for i, (ip_port, user, pwd) in enumerate(proxies, 1):
        ip_port, ok, status, dt = await test_one(ip_port, user, pwd, i, len(proxies))
        if ok:
            working.append((ip_port, status, dt))
    print(f"\n=== SUMMARY ===")
    print(f"Working: {len(working)}/{len(proxies)}")
    for ip_port, status, dt in working:
        print(f"  {ip_port} -> {status} ({dt}ms)")
    if working:
        print(f"\nFirst working IP: {working[0][0]}")
    else:
        print("\n⚠️  NO WORKING PROXIES FOUND")

if __name__ == "__main__":
    asyncio.run(main())