#!/usr/bin/env python3
"""Round-A: probe remaining untested IPs in the 2026-09-12 pool once each."""
import json, random, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_pool_20260912 import parse_pool, probe, PROBE_URLS, TRAIL, utc

done = set()
if TRAIL.exists():
    for l in TRAIL.open():
        done.add(json.loads(l)["key"])

pool = parse_pool()
remaining = [m for m in pool if f"{m['ip']}:{m['port']}" not in done]
print(f"remaining: {len(remaining)}", flush=True)
n200 = 0
for i, m in enumerate(remaining):
    res = probe(m, PROBE_URLS[0])
    res["ts"] = utc()
    with TRAIL.open("a") as f:
        f.write(json.dumps({"round": "1b", "key": f"{m['ip']}:{m['port']}", **res}) + "\n")
    if res["status"] == 200:
        n200 += 1
    print(f"[{i+1}/{len(remaining)}] {m['ip']} -> {res['status'] or res['err']}", flush=True)
    if i < len(remaining) - 1:
        time.sleep(random.uniform(1.5, 2.5))
print(f"Round-A done: {n200} new 200s", flush=True)
