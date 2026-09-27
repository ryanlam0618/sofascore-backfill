#!/usr/bin/env python3
"""gen4_stage3_phase2_1_smoke.py — Phase 2.1 smoke (Kris approved 2026-09-03 00:04).

Deltas vs Phase 2:
- Tiered retry (Duncan spec):
    worst-4  (Emperor's Cup, J.League Cup, Chinese FA Cup, ACL Two): 3-retry, 15-fail early stop
    partial-4(K League 1, CSL, Copa del Rey, Coupe de France):       2-retry, 12-fail early stop
    pass-11 (all others):                                            1-retry, 10-fail early stop
- shotmap HTTP 404 classified as `no_data` (data availability, not infra failure).
  Endpoint gate = ok / (ok + fail_infra); no_data excluded from denominator.

Sampling unchanged: sha256("phase2-v1:{ut}:{sid}:{eid}") deterministic 5 events/comp.
Read-only: NO MySQL writes.
Output: data/season_25_26_phase2_1_smoke.json
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from curl_cffi import requests as cffi_requests

ROOT = Path(__file__).resolve().parent
POOL_FILE = ROOT / "data/proxy_pools/good_21.txt"
YAML_FILE = ROOT / "competitions_10y.yaml"
OUT = ROOT / "data/season_25_26_phase2_1_smoke.json"
TIMEOUT = 20

DONE = {"Premier League", "La Liga", "Serie A", "Bundesliga", "UCL"}
EVENTS_PER_COMP = 5

TIER_3RETRY = {"Emperor's Cup", "J.League Cup", "Chinese FA Cup", "AFC Champions League Two"}
TIER_2RETRY = {"K League 1", "Chinese Super League", "Copa del Rey", "Coupe de France"}
# all others -> 1-retry

TAIL = {"event": "", "lineups": "/lineups", "statistics": "/statistics",
        "incidents": "/incidents", "odds": "/odds/1/all", "shotmap": "/shotmap"}
ORDER = ["event", "lineups", "statistics", "incidents", "odds", "shotmap"]

pool = []
for l in POOL_FILE.read_text().splitlines():
    if l.strip() and not l.startswith("#"):
        ip, port, user, pw = l.strip().split(":", 3)
        pool.append({"ip": ip, "port": port, "user": user, "pw": pw})
assert len(pool) == 21
n = {"n": 0}


def nxt():
    m = pool[n["n"] % len(pool)]
    n["n"] += 1
    return m


def fetch(path, tries):
    """Returns dict: ok / http / ip / lat / size / error / body(optional)."""
    last = None
    for _ in range(tries + 1):  # tries = retries, so attempts = tries + 1
        m = nxt()
        proxy = "http://" + m["user"] + ":" + m["pw"] + "@" + m["ip"] + ":" + m["port"]
        t0 = time.monotonic()
        try:
            r = cffi_requests.get("https://api.sofascore.com/api/v1" + path,
                                  impersonate="chrome124",
                                  proxies={"http": proxy, "https": proxy},
                                  timeout=TIMEOUT)
            lat = round((time.monotonic() - t0) * 1000)
            size = len(r.content) if r.content else 0
            if r.status_code == 200:
                body = None
                try:
                    body = r.json()
                except Exception:
                    pass
                return {"ok": True, "http": 200, "ip": m["ip"], "lat": lat, "size": size, "body": body}
            if r.status_code == 404:
                return {"ok": False, "http": 404, "ip": m["ip"], "lat": lat, "size": size,
                        "no_data": True, "error": "404"}
            last = {"http": r.status_code, "ip": m["ip"], "lat": lat, "error": f"http_{r.status_code}"}
        except Exception as e:
            last = {"http": None, "ip": m.get("ip"), "lat": round((time.monotonic() - t0) * 1000),
                    "error": f"{type(e).__name__}: {str(e)[:120]}"}
        # next attempt -> new IP
    return {"ok": False, **{k: last.get(k) for k in ("http", "ip", "lat", "error")}}


def tier(name):
    if name in TIER_3RETRY:
        return 3, 15
    if name in TIER_2RETRY:
        return 2, 12
    return 1, 10


def pick5(cands, ut, sid):
    key = lambda eid: hashlib.sha256(f"phase2-v1:{ut}:{sid}:{eid}".encode()).hexdigest()
    return sorted(cands, key=key)[:EVENTS_PER_COMP]


def main() -> int:
    comps = [c for c in yaml.safe_load(YAML_FILE.read_text())["competitions"]
             if c["name"] not in DONE]

    per_ip = {}
    total_calls = 0
    run_t = time.monotonic()
    comp_results = {}

    for c in comps:
        name, ut, sid = c["name"], c["ut_id"], c["season_id_2025_26"]
        retries, early_stop = tier(name)
        print(f"\n=== {name} (ut={ut}, season={sid}, retry={retries}, stop={early_stop}) ===", flush=True)

        # enumerate candidates (single body fetch per page)
        cands = []
        for page in range(3):
            r = fetch(f"/unique-tournament/{ut}/season/{sid}/events/last/{page}", 1)
            total_calls += 1
            d = per_ip.setdefault(r.get("ip") or "none", {"ok": 0, "fail": 0})
            if r["ok"] and isinstance(r.get("body"), dict):
                d["ok"] += 1
                cands.extend(e["id"] for e in r["body"].get("events", []) if isinstance(e, dict) and e.get("id"))
            else:
                d["fail"] += 1
                print(f"  enum page {page}: FAIL {r.get('error')}", flush=True)

        cands = list(dict.fromkeys(cands))
        if not cands:
            comp_results[name] = {"status": "fail_enum", "tier_retry": retries}
            print(f"  no candidates -> FAIL", flush=True)
            continue
        chosen = pick5(cands, ut, sid)
        print(f"  candidates={len(cands)}, chosen={chosen}", flush=True)

        per_ep = {ep: {"ok": 0, "fail": 0, "nodata": 0, "lat": [], "size": []} for ep in ORDER}
        fails_in_row = 0
        for eid in chosen:
            for ep in ORDER:
                r = fetch(f"/event/{eid}{TAIL[ep]}", retries)
                total_calls += 1
                d = per_ip.setdefault(r.get("ip") or "none", {"ok": 0, "fail": 0})
                if r["ok"]:
                    d["ok"] += 1
                    per_ep[ep]["ok"] += 1
                    per_ep[ep]["lat"].append(r["lat"])
                    per_ep[ep]["size"].append(r["size"])
                    fails_in_row = 0
                elif r.get("no_data"):
                    d["ok"] += 1  # healthy IP, endpoint just lacks data
                    per_ep[ep]["nodata"] += 1
                else:
                    d["fail"] += 1
                    per_ep[ep]["fail"] += 1
            if fails_in_row >= early_stop:
                print(f"  !! early stop ({early_stop} consecutive fails)", flush=True)
                break

        ep_summary = {}
        t_ok = t_infra = 0
        for ep, st in per_ep.items():
            infra_denom = st["ok"] + st["fail"]  # no_data excluded
            ep_summary[ep] = {
                "ok": st["ok"], "fail_infra": st["fail"], "no_data": st["nodata"],
                "infra_rate": round(100.0 * st["ok"] / infra_denom, 1) if infra_denom else None,
                "avg_lat_ms": round(sum(st["lat"]) / len(st["lat"])) if st["lat"] else None,
                "avg_size": round(sum(st["size"]) / len(st["size"])) if st["size"] else None,
            }
            t_ok += st["ok"]
            t_infra += infra_denom
        rate = round(100.0 * t_ok / t_infra, 2) if t_infra else None
        comp_results[name] = {"status": "pass" if rate == 100 else ("partial" if (rate or 0) >= 80 else "fail"),
                              "infra_rate": rate, "tier_retry": retries,
                              "endpoints": ep_summary, "events": chosen}
        print(f"  -> infra_pass={rate}% (ok={t_ok}/{t_infra})", flush=True)

    # aggregate (fixed, no nested-generator bug)
    A = {"ok": 0, "req": 0}
    per_ep_total = {ep: {"ok": 0, "fail": 0, "nodata": 0} for ep in ORDER}
    for res in comp_results.values():
        eps = res.get("endpoints")
        if not eps:
            continue
        for ep in ORDER:
            A["ok"] += eps[ep]["ok"]
            A["req"] += eps[ep]["ok"] + eps[ep]["fail_infra"]
            per_ep_total[ep]["ok"] += eps[ep]["ok"]
            per_ep_total[ep]["fail"] += eps[ep]["fail_infra"]
            per_ep_total[ep]["nodata"] += eps[ep]["no_data"]
    for ep in ORDER:
        d_ = per_ep_total[ep]["ok"] + per_ep_total[ep]["fail"]
        per_ep_total[ep]["infra_rate"] = round(100.0 * per_ep_total[ep]["ok"] / d_, 1) if d_ else None

    overall = round(100.0 * A["ok"] / A["req"], 2) if A["req"] else None
    verdict = "PASS" if overall is not None and overall >= 95 else "FAIL"
    out = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "phase": "2.1_smoke",
        "note": "shotmap-404 classified no_data; infra rate excludes no_data",
        "total_calls": total_calls,
        "duration_s": round(time.monotonic() - run_t, 1),
        "overall_infra_rate": overall,
        "per_competition": {k: {"infra_rate": v.get("infra_rate"), "status": v["status"],
                                "tier": v.get("tier_retry")} for k, v in comp_results.items()},
        "per_endpoint": per_ep_total,
        "per_ip": per_ip,
        "verdict": verdict,
    }
    OUT.write_text(json.dumps(out, indent=2))
    print("\n" + "=" * 60)
    print(json.dumps({"verdict": verdict, "overall_infra": overall,
                      "calls": total_calls, "duration_s": out["duration_s"]}, indent=2))
    print(json.dumps({e: per_ep_total[e] for e in ORDER}, indent=2))
    print(f"artifact: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())