#!/usr/bin/env python3
"""gen4_stage3_phase2_smoke.py — Phase 2 smoke test (Kris-approved 2026-09-02, 6 endpoints).

Scope: 19 comps x 5 events x 6 endpoints = 570 fetches (plus ~57 enumeration calls).
Read-only smoke: NO MySQL writes.

Endpoints: event, lineups, statistics, incidents, odds(1/all), shotmap.

Sampling: enumerate candidates via events/last pages 0..2, deterministically pick 5
via sha256("phase2-v1:{ut_id}:{season_id}:{event_id}") ascending (Phase 10 pattern).

Safety: 21-IP round-robin, 1-retry-next-IP, 10 consecutive fail -> abort, per-IP drift log.

Output: data/season_25_26_phase2_smoke.json
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
OUT = ROOT / "data/season_25_26_phase2_smoke.json"
TIMEOUT = 20

DONE = {"Premier League", "La Liga", "Serie A", "Bundesliga", "UCL"}
EVENTS_PER_COMP = 5
ENUM_PAGES = 3          # 0..2 of events/last -> ~60-90 candidates
TAIL = {"event": "", "lineups": "/lineups", "statistics": "/statistics",
        "incidents": "/incidents", "odds": "/odds/1/all", "shotmap": "/shotmap"}
ORDER = ["event", "lineups", "statistics", "incidents", "odds", "shotmap"]
MAX_RETRY = 2
EARLY_STOP = 10

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


def get(path, tries=MAX_RETRY):
    last = None
    for _ in range(tries):
        m = nxt()
        proxy = "http://" + m["user"] + ":" + m["pw"] + "@" + m["ip"] + ":" + m["port"]
        t0 = time.monotonic()
        try:
            r = cffi_requests.get("https://api.sofascore.com/api/v1" + path,
                                  impersonate="chrome124",
                                  proxies={"http": proxy, "https": proxy},
                                  timeout=TIMEOUT)
            lat = round((time.monotonic() - t0) * 1000)
            if r.status_code == 200:
                return {"ok": True, "status": 200, "ip": m["ip"], "lat": lat,
                        "size": len(r.content) if r.content else len(json.dumps(r.json()))}
            last = {"status": r.status_code}
        except Exception as e:
            last = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
    return {"ok": False, "status": last.get("status"), "error": last.get("error", "unknown")}


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
    abort = None

    for c in comps:
        name, ut, sid = c["name"], c["ut_id"], c["season_id_2025_26"]
        print(f"\n=== {name} (ut={ut}, season={sid}) ===", flush=True)

        # enumerate candidate events
        cands = []
        for page in range(ENUM_PAGES):
            r = get(f"/unique-tournament/{ut}/season/{sid}/events/last/{page}")
            total_calls += 1
            d = per_ip.setdefault(r.get("ip") or "none", {"ok": 0, "fail": 0})
            if r["ok"]:
                # r has no body here; re-get body? -> we need events; track separately
                pass
            if not r["ok"]:
                d["fail"] += 1
                print(f"  enum page {page}: FAIL {r.get('error') or r.get('status')}", flush=True)
                continue
            d["ok"] += 1
            # Need the actual body; modify approach: separate fetch that returns body
            # (kept simple: second lightweight call to same URL path with body)
            # NOTE: we re-request the page with body capture below
            body_r = api_get_body(f"/unique-tournament/{ut}/season/{sid}/events/last/{page}")
            total_calls += 1
            if body_r and body_r.get("events"):
                cands.extend(e["id"] for e in body_r["events"] if isinstance(e, dict) and e.get("id"))

        cands = list(dict.fromkeys(cands))
        if not cands:
            comp_results[name] = {"status": "fail_enum", "candidates": 0}
            print(f"  no candidates -> FAIL", flush=True)
            continue
        chosen = pick5(cands, ut, sid)
        print(f"  candidates={len(cands)}, chosen={chosen}", flush=True)

        per_ep = {ep: {"ok": 0, "fail": 0, "lat": [], "size": []} for ep in ORDER}
        fails_in_row = 0
        ev_fail = 0
        for eid in chosen:
            for ep in ORDER:
                r = get(f"/event/{eid}{TAIL[ep]}")
                total_calls += 1
                d = per_ip.setdefault(r.get("ip") or "none", {"ok": 0, "fail": 0})
                if r["ok"]:
                    d["ok"] += 1
                    per_ep[ep]["ok"] += 1
                    per_ep[ep]["lat"].append(r["lat"])
                    per_ep[ep]["size"].append(r["size"])
                    fails_in_row = 0
                else:
                    d["fail"] += 1
                    per_ep[ep]["fail"] += 1
                    fails_in_row += 1
                    ev_fail += 1
            if fails_in_row >= EARLY_STOP:
                abort = f"early_stop_10x at {name}"
                break
        ep_summary = {}
        for ep, stt in per_ep.items():
            ep_summary[ep] = {
                "ok": stt["ok"], "fail": stt["fail"],
                "avg_lat_ms": round(sum(stt["lat"]) / len(stt["lat"])) if stt["lat"] else None,
                "avg_size": round(sum(stt["size"]) / len(stt["size"])) if stt["size"] else None,
            }
        total_ok = sum(s["ok"] for s in ep_summary.values())
        total_req = sum(s["ok"] + s["fail"] for s in ep_summary.values())
        rate = round(100.0 * total_ok / total_req, 1) if total_req else 0.0
        comp_results[name] = {"status": "pass" if rate == 100.0 else ("partial" if rate >= 80 else "fail"),
                              "pass_rate": rate, "endpoints": ep_summary, "events": chosen}
        print(f"  -> {rate}% ({total_ok}/{total_req})", flush=True)
        if abort:
            break

    # aggregate
    all_ok = 0
    all_req = 0
    per_ep_total = {ep: {"ok": 0, "fail": 0} for ep in ORDER}
    for res in comp_results.values():
        eps = res.get("endpoints")
        if not eps:
            continue
        for ep in ORDER:
            o = eps[ep]["ok"]
            f = eps[ep]["fail"]
            all_ok += o
            all_req += o + f
            per_ep_total[ep]["ok"] += o
            per_ep_total[ep]["fail"] += f
    overall = round(100.0 * all_ok / all_req, 2) if all_req else None
    per_comp_summary = {k: {"rate": v.get("pass_rate"), "status": v["status"]} for k, v in comp_results.items()}
    for ep in ORDER:
        o = per_ep_total[ep]["ok"]
        f = per_ep_total[ep]["fail"]
        per_ep_total[ep]["rate"] = round(100.0 * o / (o + f), 1) if o + f else None

    verdict = "ABORTED" if abort else ("PASS" if overall is not None and overall >= 95 else "FAIL")
    out = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "phase": "2_smoke",
        "spec": {"comps": 19, "events_per_comp": EVENTS_PER_COMP, "endpoints": ORDER},
        "total_calls": total_calls,
        "duration_s": round(time.monotonic() - run_t, 1),
        "overall_pass_rate": overall,
        "per_competition": per_comp_summary,
        "per_endpoint": per_ep_total,
        "per_ip": per_ip,
        "aborted": abort,
        "verdict": verdict,
    }
    OUT.write_text(json.dumps(out, indent=2))
    print("\n" + "=" * 60)
    print(json.dumps({"verdict": verdict, "overall": overall, "calls": total_calls,
                      "duration_s": out["duration_s"]}, indent=2))
    print(json.dumps(per_ep_total, indent=2))
    print(f"artifact: {OUT}")
    return 0


def api_get_body(path):
    """Lightweight body fetch for enumeration (returns parsed JSON or None)."""
    m = nxt()
    proxy = "http://" + m["user"] + ":" + m["pw"] + "@" + m["ip"] + ":" + m["port"]
    try:
        r = cffi_requests.get("https://api.sofascore.com/api/v1" + path,
                              impersonate="chrome124",
                              proxies={"http": proxy, "https": proxy},
                              timeout=TIMEOUT)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


if __name__ == "__main__":
    sys.exit(main())