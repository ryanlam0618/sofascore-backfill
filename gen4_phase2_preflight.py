#!/usr/bin/env python3
"""Phase 2 pre-flight: probe /odds/1/all and /shotmap on known-good events.

KRIS DECISION: 6 endpoints per Phase 2 — event, lineups, statistics, incidents,
odds, shotmap. This script validates the 2 previously-unvalidated endpoints.
Only runs the bulk smoke if both return 200 with sane payloads.
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

def get(path, tries=2):
    for attempt in range(tries):
        m = nxt()
        proxy = "http://" + m["user"] + ":" + m["pw"] + "@" + m["ip"] + ":" + m["port"]
        t0 = time.monotonic()
        try:
            r = cffi_requests.get(
                "https://api.sofascore.com/api/v1" + path,
                impersonate="chrome124",
                proxies={"http": proxy, "https": proxy},
                timeout=20,
            )
            latency_ms = round((time.monotonic() - t0) * 1000, 0)
            if r.status_code == 200:
                body = r.json()
                size = len(r.content) if hasattr(r, "content") else len(json.dumps(body))
                return {
                    "ok": True, "http": 200, "ip": m["ip"], "latency_ms": latency_ms,
                    "size_bytes": size, "body": body, "error": None,
                }
            return {
                "ok": False, "http": r.status_code, "ip": m["ip"], "latency_ms": latency_ms,
                "size_bytes": 0, "body": None, "error": f"http_{r.status_code}",
            }
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"[:200]
    return {"ok": False, "http": None, "ip": None, "latency_ms": latency_ms if 'latency_ms' in dir() else -1,
            "size_bytes": 0, "body": None, "error": last_err}

# ── Known-good test event (from Kris's earlier Stage 2 canary) ──
TEST_EVENT_ID = 14025013

ENDPOINTS_TO_PROBE = [
    ("odds", "/event/{eid}/odds/1/all"),
    ("shotmap", "/event/{eid}/shotmap"),
    ("event", "/event/{eid}"),           # control (known good)
    ("statistics", "/event/{eid}/statistics"),  # control (known good)
]

print(f"=== PRE-FLIGHT: probing {len(ENDPOINTS_TO_PROBE)} endpoints on event {TEST_EVENT_ID} ===\n")

results = {}
for name, tmpl in ENDPOINTS_TO_PROBE:
    path = tmpl.format(eid=TEST_EVENT_ID)
    r = get(path)
    r["body_sample"] = json.dumps(r["body"])[:500] if r["body"] else None
    r.pop("body")  # don't store full body
    results[name] = r
    status = "✅" if r["ok"] else "❌"
    print(f"{status} {name:15s}  http={r['http']}  latency={r['latency_ms']}ms  size={r['size_bytes']}B  ip={r['ip']}  err={r['error']}")

# ── Decide: proceed or abort ──
critical_ok = all(results[e]["ok"] for e in ("odds", "shotmap"))
control_ok = all(results[e]["ok"] for e in ("event", "statistics"))

summary = {
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "test_event": TEST_EVENT_ID,
    "endpoint_results": {k: {kk: vv for kk, vv in v.items() if kk != "body"} for k, v in results.items()},
    "critical_endpoints_ok": critical_ok,
    "control_endpoints_ok": control_ok,
    "recommendation": "PROCEED" if (critical_ok and control_ok) else "ABORT_or_reduced_scope",
}

if not critical_ok:
    failed = [e for e in ("odds", "shotmap") if not results[e]["ok"]]
    summary["detail"] = f"Critical endpoint failure(s): {failed}. Recommend dropping to 4-endpoint mode."

print(f"\n{'='*60}")
print(f"critical (odds,shotmap): {'PASS' if critical_ok else 'FAIL'}")
print(f"control (event,stats):   {'PASS' if control_ok else 'FAIL'}")
print(f"recommendation: {summary['recommendation']}")

OUT = ROOT / "data/gen4_phase2_preflight.json"
OUT.write_text(json.dumps(summary, indent=2))
print(f"artifact: {OUT}")