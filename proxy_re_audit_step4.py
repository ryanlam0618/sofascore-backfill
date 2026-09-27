#!/usr/bin/env python3
"""proxy_re_audit_step4.py — Phase 10.6 Step 4: multi-endpoint x multi-age re-audit.

Per plan v0.5 §7: probe each IP against 4 endpoints x 3 event ages = 12 probes.
Verdict: GOOD (all 12 = 200) / PARTIAL (mixed) / STALE (all 403) / BROKEN (conn/407).

Probe event IDs picked from stable known events (not echoed in outputs; only
keyed by age label in artifact — Option C expanded policy).

Security: credentials via os.environ only; never written to artifacts/logs.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent
OUT_DIR = REPO_ROOT / "data" / "proxy_audit"
SRC_LIST = OUT_DIR / "good_proxies_20260827_211530.txt"

BASE = "https://www.sofascore.com/api/v1"
PROBES: Dict[str, List[str]] = {
    # age -> list of endpoint paths (fixture event ids per age, stable)
    "age_hist": [
        f"{BASE}/event/6767929",
        f"{BASE}/event/6767929/incidents",
        f"{BASE}/event/6767929/lineups",
        f"{BASE}/event/6767929/statistics",
    ],
    "age_mid": [
        f"{BASE}/event/8896967",
        f"{BASE}/event/8896967/incidents",
        f"{BASE}/event/8896967/lineups",
        f"{BASE}/event/8896967/statistics",
    ],
    "age_recent": [
        f"{BASE}/event/14025013",
        f"{BASE}/event/14025013/incidents",
        f"{BASE}/event/14025013/lineups",
        f"{BASE}/event/14025013/statistics",
    ],
}
CONCURRENCY = 10
TIMEOUT_S = 10


def _load_pw() -> tuple[str, str]:
    envp = REPO_ROOT / ".env"
    user = os.environ.get("SOFA_PROXY_USER", "***REMOVED***")
    pw = os.environ.get("SOFA_PROXY_PASS", "")
    if not pw:
        for line in envp.read_text().splitlines():
            line = line.strip()
            if line.startswith("SOFA_PROXY_PASS="):
                pw = line.split("=", 1)[1].strip()
    if not pw:
        raise RuntimeError("SOFA_PROXY_PASS missing")
    return user, pw


def _probe(url: str, proxy_url: str) -> Dict[str, Any]:
    from curl_cffi import requests as cr
    t0 = time.monotonic()
    try:
        r = cr.get(url, proxies={"http": proxy_url, "https": proxy_url},
                   impersonate="chrome", timeout=TIMEOUT_S)
        return {"status": r.status_code,
                "latency_ms": int((time.monotonic() - t0) * 1000)}
    except Exception as e:
        return {"status": 0, "latency_ms": int((time.monotonic() - t0) * 1000),
                "error": type(e).__name__}


async def main() -> None:
    user, pw = _load_pw()
    proxies = []
    for line in SRC_LIST.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        ip, port, _u, _p = line.split(":", 3)
        proxies.append({"ip": ip, "port": port})
    print(f"[step4] {len(proxies)} IPs x 12 probes", flush=True)

    rows: List[Dict[str, Any]] = []
    loop = asyncio.get_event_loop()

    def run_ip(p: Dict[str, str]) -> Dict[str, Any]:
        proxy_url = f"http://{user}:{pw}@{p['ip']}:{p['port']}"
        results: Dict[str, Dict[str, int]] = {}
        lat: List[int] = []
        for age, urls in PROBES.items():
            codes: List[int] = []
            for url in urls:
                r = _probe(url, proxy_url)
                codes.append(r["status"])
                lat.append(r["latency_ms"])
            results[age] = {"codes": codes, "ok": sum(1 for c in codes if c == 200)}
        n200 = sum(v["ok"] for v in results.values())
        n403 = sum(v["codes"].count(403) for v in results.values())
        n407 = sum(v["codes"].count(407) for v in results.values())
        n0 = sum(v["codes"].count(0) for v in results.values())
        if n200 == 12:
            verdict = "GOOD"
        elif n200 > 0:
            verdict = "PARTIAL"
        elif n403 > 0 and n200 == 0:
            verdict = "STALE"
        else:
            verdict = "BROKEN"
        return {"ip": p["ip"], "port": p["port"], "verdict": verdict,
                "n200": n200, "n403": n403, "n407": n407, "n0": n0,
                "latency_avg_ms": int(sum(lat) / len(lat)),
                "per_age": {a: v["ok"] for a, v in results.items()}}

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = [loop.run_in_executor(ex, run_ip, p) for p in proxies]
        for i, f in enumerate(asyncio.as_completed(futs), 1):
            rows.append(await f)
            if i % 5 == 0:
                print(f"[step4] {i}/{len(proxies)}", flush=True)

    summary = {}
    for r in rows:
        summary[r["verdict"]] = summary.get(r["verdict"], 0) + 1
    good = [r for r in rows if r["verdict"] == "GOOD"]
    good.sort(key=lambda r: r["latency_avg_ms"])

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    audit = {
        "task": "phase10_6_step4_reaudit",
        "method": "multi_endpoint_multi_age",
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "n_proxies": len(rows),
        "probes_per_ip": 12,
        "ages": ["hist", "mid", "recent"],
        "summary": summary,
        "password_status": "rotated_fresh",
        "rows": rows,
        "note": "Old event ids and IPs stored; per Option C expanded they are kept "
                "only in this internal artifact, never echoed in handoffs.",
    }
    audit_path = OUT_DIR / f"proxy_re_audit_v2_{ts}.json"
    audit_path.write_text(json.dumps(audit, indent=2, ensure_ascii=False))

    assign = {
        "generated_utc": audit["started_utc"],
        "audit_file": str(audit_path.relative_to(REPO_ROOT)),
        "pool_size": len(good),
        "recommended_pool": [{"ip": g["ip"], "port": g["port"],
                              "latency_avg_ms": g["latency_avg_ms"]} for g in good],
        "note": "Phase 10.6 canary per-competition sticky source.",
    }
    assign_path = OUT_DIR / "ip_assignment_phase10_6.json"
    assign_path.write_text(json.dumps(assign, indent=2, ensure_ascii=False))
    print(f"[step4] verdicts: {summary}", flush=True)
    print(f"[step4] pool: {len(good)} GOOD IPs", flush=True)
    print(f"[step4] artifacts: {audit_path.name}, {assign_path.name}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
