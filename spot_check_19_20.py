#!/usr/bin/env python3
"""spot_check_19_20.py — 19/20 season spot check (Tier-2 primary, Tier-1 fallback).

Per Duncan dispatch 2026-08-28 11:11 (Kris task). NOT full canary — no protected files.
No IP/event_id/password echoed in stdout; only artifact JSON holds details.
"""
from __future__ import annotations

import asyncio
import csv
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent
DATA_DIR = REPO_ROOT / "data"
COVERAGE_CSV = DATA_DIR / "coverage_audit" / "season_sample_coverage.csv"
POOL_JSON = DATA_DIR / "proxy_audit" / "ip_assignment_phase10_6.json"
OUT_DIR = DATA_DIR / "proxy_audit"

COMPS = ["A-League Men", "Premier League", "La Liga", "Serie A", "UCL", "Ligue 1", "J1 League"]
TARGET_SEASON = "19/20"


def _load_env() -> None:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")


def _build_proxy_url(cfg_user: str, cfg_pass: str, ip: str, port: str) -> str:
    return f"http://{cfg_user}:{cfg_pass}@{ip}:{port}"


async def _tier2_get(fetcher, path: str) -> Dict[str, Any]:
    r = await fetcher._fetch_with_retry_tier(
        tier_name="cloakbrowser", path=path, timeout_ms=45000,
        tier_max_retries=2)
    final = t_final = t2 = t3 = None
    final = t.get("final_attempt") or {}
    return {"status": final.get("status", 0), "ok": bool(t.get("success")),
            "transport": "cloakbrowser"}


async def main() -> None:
    import hashlib
    from gen4_fetcher import Gen4Config, Gen4Fetcher
    from gen4_components import DefaultHeaderFactory, RequestBudget

    _load_env()
    user = os.environ.get("SOFA_PROXY_USER", "***REMOVED***")
    pw = os.environ.get("SOFA_PROXY_PASS", "")
    pool_raw = json.loads(POOL_JSON.read_text())["recommended_pool"]
    pool = [{"ip": p["ip"], "port": p["port"], "user": user, "pass": pw}
            for p in pool_raw]

    # sample events
    events: List[Dict[str, Any]] = []
    with COVERAGE_CSV.open() as f:
        rows = list(csv.DictReader(f))
    for comp in COMPS:
        cands = [r for r in rows if r["competition"] == comp
                 and r["season_label"] == TARGET_SEASON and r["status"] == "ok"]
        if not cands:
            events.append({"competition": comp, "event_id": None, "verdict": "NO_SAMPLE"})
            continue
        def _h(r, comp=comp):
            return hashlib.sha256(f"spot19:{comp}:{r['event_id']}".encode()).hexdigest()
        cands.sort(key=_h)
        events.append({"competition": comp, "event_id": int(cands[0]["event_id"])})

    results: List[Dict[str, Any]] = []
    for i, ev in enumerate(events):
        if ev["event_id"] is None:
            results.append(ev)
            continue
        comp = ev["competition"]
        pidx = int(hashlib.sha256(f"spot19:{comp}".encode()).hexdigest(), 16) % len(pool)
        p = pool[pidx]

        cfg = Gen4Config(write_enabled=False,
                         proxy_server=p["ip"], proxy_port=p["port"],
                         proxy_user=user, proxy_pass=pw)
        jar = None
        headers = DefaultHeaderFactory()
        budget = RequestBudget(max_attempts_per_request=4)

        fetcher = Gen4Fetcher(
            config=cfg,
            curl_cffi_get=lambda url, **kw: __import__("curl_cffi.requests").requests.get(url, impersonate="chrome", **kw),
            cloakbrowser_launcher=lambda **kw: _launch_browser(**kw),
            ssr_fetcher=None,
            warmup_sequence=None,
            cookie_store=None,
            sticky_session=None,
            header_factory=headers,
            budget=budget,
            proxy_rotator=None,
        )
        path = f"/api/v1/event/{ev['event_id']}"
        row: Dict[str, Any] = {"competition": comp, "event_id": ev["event_id"],
                               "tier2": None, "tier1": None}

        try:
            await fetcher.start()
            t0 = time.monotonic()
            t2 = await fetcher._fetch_with_retry_tier(
                tier_name="cloakbrowser", path=path, timeout_ms=45000,
                tier_max_retries=2)
            row["tier2"] = {"status": (t2.get("final_attempt") or {}).get("status", 0),
                            "ok": bool(t2.get("success")),
                            "latency_ms": int((time.monotonic() - t0) * 1000)}

            if not row["tier2"]["ok"]:
                t0 = time.monotonic()
                t1 = await fetcher._fetch_with_retry_tier(
                    tier_name="curl_cffi", path=path, timeout_ms=20000,
                    tier_max_retries=2)
                row["tier1"] = {"status": (t1.get("final_attempt") or {}).get("status", 0),
                                "ok": bool(t1.get("success")),
                                "latency_ms": int((time.monotonic() - t0) * 1000)}
        finally:
            try:
                await fetcher.close()
            except Exception:
                pass

        t2_ok = row["tier2"]["ok"]
        t1_ok = row["tier1"]["ok"] if row.get("tier1") else False
        if t2_ok and t1_ok:
            row["verdict"] = "COMP-OK"
        elif t2_ok or t1_ok:
            row["verdict"] = "COMP-PARTIAL"
        else:
            row["verdict"] = "COMP-BROKEN"
        results.append(row)
        print(f"[spot19] {i+1}/{len(events)} done", flush=True)

    broken = [r for r in results if r.get("verdict") == "COMP-BROKEN"]
    partial = [r for r in results if r.get("verdict") == "COMP-PARTIAL"]
    overall = "universal-broken" if len(broken) >= len(COMPS) - 2 else (
        "aleague-only" if len(broken) <= 1 and all(
            r.get("competition") != "A-League Men" or r.get("verdict") == "COMP-BROKEN"
            for r in broken) else "mixed")

    out = {
        "task": "phase10_6_spot_check_19_20",
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "competitions_checked": len(results),
        "results": results,
        "overall_diagnosis": overall,
        "n_broken": len(broken),
        "n_partial": len(partial),
        "note": "Tier-2 primary + Tier-1 fallback; 1 event per comp; event_id redacted per Option C but stored in artifact.",
    }
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = OUT_DIR / f"spot_check_19_20_{ts}.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"[spot19] written: {path}", flush=True)
    print(f"[spot19] overall: {overall} | broken={len(broken)} partial={len(partial)}", flush=True)
    return out


async def _launch_browser(**kw):
    import cloakbrowser as _c
    return await _c.launch_async(**kw)


if __name__ == "__main__":
    asyncio.run(main())
