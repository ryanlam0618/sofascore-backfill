#!/usr/bin/env python3
"""gen4_stage1_dryrun_100events.py — Phase 11 Stage 1 (Kris rollout path 2026-08-31 00:50).

Stage 1: 100 random real events, Dry-run fetch /event/{id} via 21-IP good pool
(chrome124, sequential). NO MySQL writes. Event IDs sampled live from season
events endpoints across mixed leagues/recent seasons (those sampling calls use
the same pool, extra 10 calls max).

Pass bar: >=98% (98/100); abort on 5 consecutive 403s / 5 consecutive conn errs.
"""
import json, random, sys, time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
POOL_FILE = ROOT / "data/proxy_pools/good_21.txt"
EV = Path("/tmp/gen4_stage1/evidence.jsonl")
OUT = ROOT / "data/gen4_stage1_dryrun.json"
EV.parent.mkdir(exist_ok=True)

# mixed recent seasons: (name, ut_id, season_id, pages)
SOURCES = [
    ("PL 25/26", 17, 76986, [0, 1]),
    ("La Liga 25/26", 8, 77537, [0, 1]),
    ("Bundesliga 25/26", 35, 77333, [0, 1]),
    ("UCL 25/26", 7, 76953, [0]),
    ("Serie A 25/26", 23, 77559, [0, 1]),
]
TARGET_N = 100
TIMEOUT = 20
pool = []
for l in POOL_FILE.read_text().splitlines():
    if l.strip() and not l.startswith("#"):
        ip, port, user, pw = l.strip().split(":", 3)
        pool.append((ip, port, user, pw))
assert len(pool) == 21

from curl_cffi import requests as cffi_requests

def fetch(url, i):
    ip, port, user, pw = pool[i % len(pool)]
    proxy = "http://%s:%s@%s:%s" % (user, pw, ip, port)
    t0 = time.monotonic()
    try:
        r = cffi_requests.get(url, impersonate="chrome124",
                              proxies={"http": proxy, "https": proxy}, timeout=TIMEOUT)
        return ip, r.status_code, len(r.content or b""), round((time.monotonic()-t0)*1000, 1), r.json() if r.status_code == 200 else None, None
    except Exception as e:
        return ip, None, 0, round((time.monotonic()-t0)*1000, 1), None, f"{type(e).__name__}: {e}"[:200]

# sample event ids
ids, seen = [], set()
attempt = 0
for name, ut, sid, pages in SOURCES:
    for p in pages:
        url = f"https://api.sofascore.com/api/v1/unique-tournament/{ut}/season/{sid}/events/last/{p}"
        ip, st, nb, lat, data, err = fetch(url, attempt); attempt += 1
        ev = [e["id"] for e in (data or {}).get("events", []) if e.get("id")]
        for e in ev:
            if e not in seen: seen.add(e); ids.append(e)
random.Random(20260831).shuffle(ids)
ids = ids[:TARGET_N]
print(f"sampled {len(ids)} events from {attempt} sampling calls", flush=True)

rows = []
consec403 = consecerr = 0
aborted = None
for k, eid in enumerate(ids):
    if consec403 >= 5: aborted = "early_stop_5x403"; break
    if consecerr >= 5: aborted = "early_stop_5xconnerr"; break
    ip, st, nb, lat, _, err = fetch(f"https://api.sofascore.com/api/v1/event/{eid}", attempt + k)
    if st == 200: consec403 = consecerr = 0
    elif st == 403: consec403 += 1; consecerr = 0
    elif st is None: consecerr += 1; consec403 = 0
    else: consec403 = consecerr = 0
    row = {"event_id": eid, "ip": ip, "status": st, "bytes": nb,
           "latency_ms": lat, "reason": "ok" if st == 200 else (err or f"http_{st}")}
    rows.append(row)
    with EV.open("a") as f: f.write(json.dumps(row) + "\n")

passed = sum(1 for r in rows if r["status"] == 200)
pct = round(100.0*passed/len(rows), 2) if rows else 0
per_ip = {}
for r in rows:
    d = per_ip.setdefault(r["ip"], {"passed": 0, "failed": 0})
    d["passed" if r["status"] == 200 else "failed"] += 1

verdict = ("ABORTED" if aborted else
           "PASS" if rows and passed/len(rows) >= 0.98 else "FAIL")
out = {"timestamp": datetime.now(timezone.utc).isoformat(), "stage": 1,
       "mode": "dry_run_no_mysql_write",
       "sampled_events": len(ids), "calls": len(rows) + attempt,
       "dryrun_calls": len(rows), "passed": passed, "pass_rate_pct": pct,
       "aborted": aborted, "per_ip": per_ip,
       "failed": [r for r in rows if r["status"] != 200], "verdict": verdict,
       "evidence_jsonl": str(EV)}
OUT.write_text(json.dumps(out, indent=2))
print(json.dumps({"verdict": verdict, "passed": passed, "total": len(rows),
                  "pct": pct, "aborted": aborted}, indent=2))
