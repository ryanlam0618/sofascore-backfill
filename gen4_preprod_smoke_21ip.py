#!/usr/bin/env python3
"""gen4_preprod_smoke_21ip.py — Phase 11 pre-production gate (Kris 00:50 GMT+8 Option A).

84 calls: event 14025013 x 4 endpoints x 21 good IPs (data/proxy_pools/good_21.txt),
chrome124, sequential, read-only. Pass bar: 84/84 (100%) per Main's gate; any fail
-> escalate, no auto-rollback.
"""
import json, sys, time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
POOL = (ROOT / "data/proxy_pools/good_21.txt").read_text().splitlines()
EID = 14025013
EP = ["", "/lineups", "/statistics", "/incidents"]
BASE = "https://api.sofascore.com/api/v1/event/%s%s"
OUT = ROOT / "data" / "gen4_preprod_smoke_21ip.json"
EV = Path("/tmp/gen4_preprod_smoke/evidence.jsonl")
EV.parent.mkdir(exist_ok=True)

pool = []
for l in POOL:
    if l.strip() and not l.startswith("#"):
        ip, port, user, pw = l.strip().split(":", 3)
        pool.append((ip, port, user, pw))
assert len(pool) == 21

from curl_cffi import requests as cffi_requests
rows = []
for ip, port, user, pw in pool:
    proxy = "http://%s:%s@%s:%s" % (user, pw, ip, port)
    for tail in EP:
        t0 = time.monotonic(); status, nb, reason = None, 0, None
        try:
            r = cffi_requests.get(BASE % (EID, tail), impersonate="chrome124",
                                  proxies={"http": proxy, "https": proxy}, timeout=20)
            status, nb = r.status_code, len(r.content or b"")
            reason = "ok" if status == 200 else f"http_{status}"
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"[:200]
        row = {"ip": ip, "endpoint": tail or "/", "status": status,
               "bytes": nb, "latency_ms": round((time.monotonic()-t0)*1000, 1),
               "reason": reason}
        rows.append(row)
        with EV.open("a") as f: f.write(json.dumps(row) + "\n")

passed = sum(1 for r in rows if r["status"] == 200)
out = {"timestamp": datetime.now(timezone.utc).isoformat(), "stage": "pre_prod_smoke",
       "pool_size": len(pool), "calls": len(rows), "passed": passed,
       "pass_rate_pct": round(100.0*passed/len(rows), 2),
       "failed_cells": [r for r in rows if r["status"] != 200],
       "verdict": "PASS" if passed == len(rows) else "FAIL",
       "evidence_jsonl": str(EV)}
OUT.write_text(json.dumps(out, indent=2))
print(json.dumps({k: out[k] for k in ("calls", "passed", "pass_rate_pct", "verdict")}, indent=2))
