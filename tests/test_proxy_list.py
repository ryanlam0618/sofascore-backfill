#!/usr/bin/env python3
"""
Test webshare.io proxy list against SofaScore API
"""

import requests
import sys
import json

# Test endpoint (已知成功)
TEST_URL = "https://www.sofascore.com/api/v1/unique-tournament/11/season/61627/events/round/1"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

# 從 proxy_list.txt 讀取
def load_proxies(path="proxy_list.txt"):
    proxies = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(":")
            if len(parts) == 4:
                ip, port, user, pwd = parts
                proxies.append({
                    "ip": ip, "port": port, "user": user, "pwd": pwd,
                    "url": f"http://{user}:{pwd}@{ip}:{port}"
                })
    return proxies

def test_proxy(proxy):
    try:
        r = requests.get(
            TEST_URL,
            proxies={"http": proxy["url"], "https": proxy["url"]},
            headers=HEADERS,
            timeout=15
        )
        return {
            "ip": proxy["ip"],
            "port": proxy["port"],
            "status": r.status_code,
            "ok": r.status_code == 200,
            "body_len": len(r.text) if r.status_code == 200 else 0
        }
    except requests.exceptions.ProxyError as e:
        return {"ip": proxy["ip"], "port": proxy["port"], "status": "PROXY_ERROR", "ok": False, "error": str(e)[:100]}
    except requests.exceptions.Timeout:
        return {"ip": proxy["ip"], "port": proxy["port"], "status": "TIMEOUT", "ok": False}
    except Exception as e:
        return {"ip": proxy["ip"], "port": proxy["port"], "status": "ERROR", "ok": False, "error": str(e)[:100]}

def main():
    proxies = load_proxies()
    print(f"Loaded {len(proxies)} proxies from proxy_list.txt")
    print(f"Testing against: {TEST_URL}")
    print("=" * 60)

    working = []
    failed = []

    for i, p in enumerate(proxies):
        result = test_proxy(p)
        if result["ok"]:
            print(f"[{i+1:3d}/{len(proxies)}] ✅ {result['ip']}:{result['port']} → {result['status']} ({result['body_len']} bytes)")
            working.append(result)
        else:
            print(f"[{i+1:3d}/{len(proxies)}] ❌ {result['ip']}:{result['port']} → {result['status']} {result.get('error','')}")
            failed.append(result)

    print("=" * 60)
    print(f"Working: {len(working)} / {len(proxies)}")
    
    if working:
        print("\n✅ Working proxies:")
        for w in working:
            print(f"  {w['ip']}:{w['port']} (status={w['status']}, size={w['body_len']})")
        
        # Save working list
        with open("working_proxies.txt", "w") as f:
            for w in working:
                # 找回原始行
                for p in proxies:
                    if p["ip"] == w["ip"] and p["port"] == w["port"]:
                        f.write(f"{p['ip']}:{p['port']}:{p['user']}:{p['pwd']}\n")
                        break
        print("\nSaved to working_proxies.txt")
    else:
        print("\n⚠️  No working proxies found!")

if __name__ == "__main__":
    main()