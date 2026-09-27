#!/usr/bin/env python3
"""Probe 30 new Webshare IPs against SofaScore with curl_cffi chrome124 (fp-v2).
2 probes per IP: /api/v1/config/all then /api/v1/event/14025013.
Verdict: GOOD (both 200 + valid JSON), PARTIAL (one pass), BAD (all fail).
"""
import json, time, sys
from curl_cffi import requests as cr

POOL = "/root/.openclaw/workspace/sofascore-backfill/data/proxy_pools/webshare_30_20260917.txt"
OUT  = "/root/.openclaw/workspace/sofascore-backfill/data/proxy_pools/webshare_30_20260917_audit.json"

TARGETS = [
    ("config", "https://www.sofascore.com/api/v1/config/all"),
    ("event",  "https://www.sofascore.com/api/v1/event/14025013"),
]

ips = []
for line in open(POOL):
    line = line.strip()
    if not line: continue
    ip, port, user, pwd = line.split(":")
    ips.append((ip, port, user, pwd))

results = []
for i, (ip, port, user, pwd) in enumerate(ips, 1):
    proxy = f"http://{user}:{pwd}@{ip}:{port}"
    rec = {"ip": ip, "port": port, "probes": []}
    for name, url in TARGETS:
        t0 = time.time()
        try:
            r = cr.get(url, impersonate="chrome124",
                       proxies={"http": proxy, "https": proxy}, timeout=25)
            ms = int((time.time()-t0)*1000)
            ok_json = False
            try: r.json(); ok_json = True
            except Exception: pass
            rec["probes"].append({"target": name, "status": r.status_code,
                                  "ms": ms, "json": ok_json,
                                  "bytes": len(r.content)})
        except Exception as e:
            rec["probes"].append({"target": name, "status": "err",
                                  "ms": int((time.time()-t0)*1000),
                                  "error": str(e)[:120]})
        time.sleep(0.8)
    ok = sum(1 for p in rec["probes"] if p.get("status") == 200 and p.get("json"))
    rec["verdict"] = "GOOD" if ok == 2 else ("PARTIAL" if ok == 1 else "BAD")
    results.append(rec)
    print(f"[{i:02d}/30] {ip}:{port} -> "
          + " / ".join(str(p.get("status")) for p in rec["probes"])
          + f" => {rec['verdict']}", flush=True)
    time.sleep(1.0)

json.dump({"pool": POOL, "results": results,
           "summary": {
               "GOOD": sum(1 for r in results if r["verdict"]=="GOOD"),
               "PARTIAL": sum(1 for r in results if r["verdict"]=="PARTIAL"),
               "BAD": sum(1 for r in results if r["verdict"]=="BAD")}},
          open(OUT, "w"), indent=2)
print("DONE", json.dumps([r["verdict"] for r in results]))
