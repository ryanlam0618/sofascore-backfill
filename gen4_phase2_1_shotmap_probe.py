#!/usr/bin/env python3
"""Phase 2.1 shotmap diagnostic probe — root-cause the 78.9% shotmap pass rate.

Kris directive (2026-09-02 23:52): probe /shotmap on 3 events x 5 sequential calls
(single-IP-per-call rotation across the 21-IP pool), log per-attempt:
http code, latency, payload size, response headers (Server/Via/X-Cache/CF-Cache-Status).

Hypotheses: A=Varnish payload-size limit, B=IP-reputation, C=rate limit, D=data availability.
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from curl_cffi import requests as cffi_requests

ROOT = Path(__file__).resolve().parent
POOL_FILE = ROOT / "data/proxy_pools/good_21.txt"

pool = []
for l in POOL_FILE.read_text().splitlines():
    if l.strip() and not l.startswith("#"):
        ip, port, user, pw = l.strip().split(":", 3)
        pool.append({"ip": ip, "port": port, "user": user, "pw": pw})
n = {"n": 0}

def nxt():
    m = pool[n["n"] % len(pool)]
    n["n"] += 1
    return m

# Three test events:
# 1. Known-good baseline (PL, has shotmap in Stage 2)
# 2. Emperor's Cup event (from Phase 2 smoke, one that failed)
# 3. J.League Cup event (from Phase 2 smoke)
EVENTS = [
    ("baseline_pl", 14025013, "Premier League"),
    ("emperor_cup", 16869681, "Emperor's Cup (Phase2 fail)"),
    ("jleague_cup", None, "J.League Cup (need fresh event)"),  # will resolve below
]

# First resolve a J.League Cup event id via events/last
def resolve_jleague_event():
    while True:
        m = nxt()
        proxy = "http://" + m["user"] + ":" + m["pw"] + "@" + m["ip"] + ":" + m["port"]
        try:
            r = cffi_requests.get(
                "https://api.sofascore.com/api/v1/unique-tournament/101/season/97720/events/last/0",
                impersonate="chrome124", proxies={"http": proxy, "https": proxy}, timeout=20)
            if r.status_code == 200:
                evs = r.json().get("events", [])
                if evs:
                    return evs[0]["id"]
        except Exception:
            continue

jlc_eid = resolve_jleague_event()
if jlc_eid:
    EVENTS[2] = ("jleague_cup", jlc_eid, "J.League Cup")
    print(f"[resolve] J.League Cup event = {jlc_eid}")
else:
    print("[resolve] WARNING: could not fetch J.League Cup event id")

# Probe loop: 5 sequential calls per event per endpoint
# Also probe one OTHER cup event for comparison (Emperor's Cup success one)
results = []
for tag, eid, desc in EVENTS:
    if eid is None:
        continue
    print(f"\n=== {desc} (event {eid}) ===")
    for attempt in range(5):
        m = nxt()
        proxy = "http://" + m["user"] + ":" + m["pw"] + "@" + m["ip"] + ":" + m["port"]
        t0 = time.monotonic()
        rec = {"tag": tag, "event": eid, "attempt": attempt + 1, "ip": m["ip"]}
        try:
            r = cffi_requests.get(
                f"https://api.sofascore.com/api/v1/event/{eid}/shotmap",
                impersonate="chrome124", proxies={"http": proxy, "https": proxy}, timeout=25)
            rec["latency_ms"] = round((time.monotonic() - t0) * 1000)
            rec["http"] = r.status_code
            rec["size"] = len(r.content)
            # capture interesting headers
            hdrs = {k: v for k, v in r.headers.items() if k.lower() in
                    ("server", "via", "x-cache", "cf-cache-status", "x-served-by", "content-length", "content-type")}
            rec["headers"] = hdrs
            if r.status_code == 200:
                try:
                    body = r.json()
                    rec["shots"] = len(body.get("shotmap", [])) if isinstance(body, dict) else None
                    rec["has_stats"] = isinstance(body, dict) and len(body) > 0
                except Exception:
                    rec["shots"] = None
            rec["ok"] = r.status_code == 200
        except Exception as e:
            rec.update({"ok": False, "error": f"{type(e).__name__}: {e}"[:200],
                        "latency_ms": round((time.monotonic() - t0) * 1000)})
        results.append(rec)
        mark = "✅" if rec["ok"] else "❌"
        shots = rec.get("shots", "?")
        print(f"  {mark} attempt{rec['attempt']} ip={rec['ip']:<16s} http={rec.get('http')}  "
              f"lat={rec['latency_ms']:<5}ms size={rec.get('size',0):>6,}B shots={shots} "
              f"err={rec.get('error', '')[:30]}")

# Summary per tag
print("\n" + "=" * 70)
for tag, eid, desc in EVENTS:
    if eid is None:
        continue
    tag_res = [r for r in results if r["tag"] == tag]
    ok_c = sum(1 for r in tag_res if r["ok"])
    avg_lat = round(sum(r["latency_ms"] for r in tag_res) / len(tag_res))
    avg_sz = round(sum(r.get("size", 0) for r in tag_res) / len(tag_res))
    shots_list = [r.get("shots") for r in tag_res if r.get("shots") is not None]
    print(f"{desc}: {ok_c}/{len(tag_res)} ok, avg_lat={avg_lat}ms avg_sz={avg_sz}B, shots={shots_list}")

OUT = ROOT / "data/gen4_phase2_1_shotmap_diagnostic.json"
OUT.write_text(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(),
                           "events_tested": len([e for _, e, _ in EVENTS if e is not None]),
                           "results": results}, indent=2))
print(f"\nartifact: {OUT}")