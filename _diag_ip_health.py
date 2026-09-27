#!/usr/bin/env python3
"""Live per-IP health probe — answer Kris: 'use which proxy ip? was it the proxy problem?'"""
import os, re, sys, json, subprocess, time
from pathlib import Path

# Load .env from sofascore-backfill runner
env_path = Path("/root/.openclaw/workspace/sofascore-backfill/.env")
env = {}
for line in env_path.read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")

PROXY_USER = env.get("SOFA_PROXY_USER", "***REMOVED***-rotate")
PROXY_PASS = env.get("SOFA_PROXY_PASS", "")
PROXY_HOST = env.get("SOFA_PROXY_HOST", "p.webshare.io")
PROXY_PORT = env.get("SOFA_PROXY_PORT", "80")

print(f"proxy creds: user={PROXY_USER} host={PROXY_HOST}:{PROXY_PORT} pass_len={len(PROXY_PASS)}")

# Pool
pool_path = Path("/root/.openclaw/workspace/sofascore-backfill/data/proxy_pools/good_phaseA_20260909.txt")
pool = [l.strip() for l in pool_path.read_text().splitlines() if l.strip() and not l.startswith("#")]
print(f"pool size: {len(pool)}\n")

# Use curl-cffi via runner venv for proper TLS fingerprint
sys.path.insert(0, "/root/.openclaw/workspace/sofascore-backfill/.runner-venv/lib/python3.11/site-packages")
try:
    from curl_cffi import requests as cc_requests
    use_lib = "curl_cffi"
except ImportError:
    use_lib = "subprocess_curl"

print(f"using lib: {use_lib}")

test_urls = [
    "https://www.sofascore.com/api/v1/event/14024019",
    "https://www.sofascore.com/api/v1/event/14024019/lineups",
]

results = {}
for ip in pool:
    proxy_url = f"http://{PROXY_USER}:{PROXY_PASS}@{PROXY_HOST}:{PROXY_PORT}"
    proxy_dict = {"http": proxy_url, "https": proxy_url}
    res_for_ip = []
    for url in test_urls:
        if use_lib == "curl_cffi":
            try:
                r = cc_requests.get(url, impersonate="chrome124", proxies=proxy_dict, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
                code = r.status_code
                body_head = (r.text[:80] if r.text else "").replace("\n"," ")
                res_for_ip.append((code, body_head))
            except Exception as e:
                res_for_ip.append(("ERR", str(e)[:60]))
        else:
            # curl fallback
            cmd = ["curl","-sS","-o","/tmp/_probe.body","-w","%{http_code}","--connect-timeout","8","--max-time","12",
                   "-x", proxy_url,
                   "-H","User-Agent: Mozilla/5.0 Chrome/124",
                   url]
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                code = p.stdout.strip()
                body = open("/tmp/_probe.body").read()[:80].replace("\n"," ") if Path("/tmp/_probe.body").exists() else ""
                res_for_ip.append((code, body))
            except Exception as e:
                res_for_ip.append(("ERR", str(e)[:60]))
    results[ip] = res_for_ip

# Summary
ok_ips = [ip for ip,rs in results.items() if all(r[0]=="200" for r in rs)]
blocked_ips = [ip for ip,rs in results.items() if any(r[0] in ("403","429") for r in rs)]
ambig_ips = [ip for ip,rs in results.items() if ip not in ok_ips and ip not in blocked_ips]

print(f"\n=== POOL HEALTH ===")
print(f"OK (all 200):  {len(ok_ips)}/{len(pool)}")
print(f"BLOCKED (any 403/429): {len(blocked_ips)}")
print(f"AMBIGUOUS (timeout/other): {len(ambig_ips)}\n")

print("--- Per-IP results ---")
for ip in pool:
    rs = results[ip]
    short = []
    for c, b in rs:
        short.append(f"{c}({b[:40]!r})")
    tag = "✅" if ip in ok_ips else ("❌" if ip in blocked_ips else "❓")
    print(f"  {tag}  {ip:18s}  {', '.join(short)}")

print(f"\n=== VERDICT ===")
pct_ok = len(ok_ips)/len(pool)*100
if pct_ok >= 80:
    verdict = "POOL_HEALTHY — most IPs OK; 403s are transient burst"
elif pct_ok >= 50:
    verdict = "POOL_DEGRADED — half IPs blocked; Phase A burn is real"
else:
    verdict = "POOL_BURNED — majority reputation-flagged; needs refresh"
print(verdict)
