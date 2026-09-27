#!/usr/bin/env python3
"""multitransport_multileague_canary.py — Phase 11 gate (Kris 2026-08-30 19:15 GMT+8 option A).

Matrix: 5 events (multi-league) x 22 passed IPs x 2 transports = 220 calls.
  Tier 1: curl_cffi + impersonate="chrome124" + Webshare IP
  Tier 2: CloakBrowser 0.5.10 launch_context(headless, proxy, ua=Chrome/124)

Rules: write_enabled=False; budget <= 250; per combo 1 GET /event/{id};
non-200 -> recorded, never halts. Evidence -> /tmp/multitransport_multileague_canary/.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
PREV_CANARY = REPO_ROOT / "data" / "proxy_x_chrome124_full_canary.json"
CREDS_FILE = REPO_ROOT / "data" / "proxy_audit" / "good_proxies_20260827_192348.txt"
EVIDENCE_DIR = Path("/tmp/multitransport_multileague_canary")
OUT_JSON = REPO_ROOT / "data" / "multitransport_multileague_canary.json"

EVENTS = [
    ("PL_14025013", "PL 25/26", 14025013),           # Liverpool vs Bournemouth
    ("PL_12436875", "PL 24/25", 12436875),           # approved smoke event (61627)
    ("Bundesliga_14065228", "Bundesliga 25/26", 14065228),  # Dortmund vs Freiburg
    ("UCL_13412185", "UCL 24/25", 13412185),         # Real Madrid vs Man City
    ("KLeague_14918106", "K League 1 2025", 14918106),      # Daejeon vs FC Seoul
]
API = "https://api.sofascore.com/api/v1/event/{eid}"
UA_CHROME124 = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
TIMEOUT_S = 25
CONCURRENCY = 5
BUDGET_MAX_CALLS = 250
WRITE_ENABLED = False

calls = {"used": 0}
lock = asyncio.Lock()


def load_creds() -> tuple[str, str]:
    line = next(l for l in CREDS_FILE.read_text().splitlines()
                if l.strip() and not l.startswith("#"))
    _, _, user, pw = line.split(":", 3)
    return user, pw


def load_ips() -> list[str]:
    return json.loads(PREV_CANARY.read_text())["passed_ips"]


async def budget_guard(row: dict) -> bool:
    async with lock:
        if calls["used"] >= BUDGET_MAX_CALLS:
            row.update(status=None, reason="skipped_budget")
            return False
        calls["used"] += 1
        return True


async def tier1(ip: str, port: str, user: str, pw: str, key: str, eid: int) -> dict:
    row = {"transport": "curl_cffi_chrome124", "event_key": key, "event_id": eid,
           "ip": ip, "status": None, "bytes": 0, "latency_ms": 0, "reason": None}
    if not await budget_guard(row):
        return row
    from curl_cffi import requests as cffi_requests
    t0 = time.monotonic()
    try:
        proxy_url = f"http://{user}:{pw}@{ip}:{port}"
        r = cffi_requests.get(API.format(eid=eid), impersonate="chrome124",
                              proxies={"http": proxy_url, "https": proxy_url},
                              timeout=TIMEOUT_S)
        row.update(status=r.status_code, bytes=len(r.content or b""),
                   reason="ok" if r.status_code == 200 else f"http_{r.status_code}")
    except Exception as exc:
        row["reason"] = f"{type(exc).__name__}: {exc}"[:200]
    row["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
    return row


async def tier2(ip: str, port: str, user: str, pw: str, key: str, eid: int) -> dict:
    row = {"transport": "cloakbrowser_launch_context", "event_key": key, "event_id": eid,
           "ip": ip, "status": None, "bytes": 0, "latency_ms": 0, "reason": None}
    if not await budget_guard(row):
        return row
    import cloakbrowser as cb
    t0 = time.monotonic()
    ctx = None
    try:
        ctx = await cb.launch_context_async(
            headless=True,
            proxy=f"http://{user}:{pw}@{ip}:{port}",
            user_agent=UA_CHROME124,
            humanize=False,
        )
        page = await ctx.new_page()
        resp = await page.goto(API.format(eid=eid), wait_until="domcontentloaded",
                               timeout=TIMEOUT_S * 1000)
        status = resp.status if resp else 0
        body = ""
        if status == 200:
            try:
                body = await page.evaluate("() => document.body.innerText")
            except Exception:
                body = ""
        row.update(status=status, bytes=len(body.encode()) if body else 0,
                   reason="ok" if status == 200 else f"http_{status}")
    except Exception as exc:
        row["reason"] = f"{type(exc).__name__}: {exc}"[:200]
    finally:
        row["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
        if ctx is not None:
            try:
                bc = getattr(ctx, "browser", None)
                if bc is not None:
                    await bc.close()
                await ctx.close()
            except Exception:
                pass
    return row


async def run() -> dict:
    assert not WRITE_ENABLED
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    evidence = EVIDENCE_DIR / "evidence.jsonl"

    user, pw = load_creds()
    # port per IP comes from good list (uniform 4-5 digit static ports)
    ip_ports = {}
    for l in CREDS_FILE.read_text().splitlines():
        if l.strip() and not l.startswith("#"):
            p = l.split(":")
            ip_ports[p[0]] = p[1]
    ips = load_ips()

    sem = asyncio.Semaphore(CONCURRENCY)

    async def guarded(fn, *a):
        async with sem:
            return await fn(*a)

    tasks = []
    for key, _league, eid in EVENTS:
        for ip in ips:
            port = ip_ports.get(ip, "80")
            tasks.append(guarded(tier1, ip, port, user, pw, key, eid))
            tasks.append(guarded(tier2, ip, port, user, pw, key, eid))

    rows = []
    for fut in asyncio.as_completed(tasks):
        row = await fut
        rows.append(row)
        with evidence.open("a") as fh:
            fh.write(json.dumps(row) + "\n")

    # aggregate
    results: dict = {}
    failures = []
    for transport in ("curl_cffi_chrome124", "cloakbrowser_launch_context"):
        results[transport] = {}
        for key, league, eid in EVENTS:
            subset = [r for r in rows if r["transport"] == transport and r["event_key"] == key]
            passed = [r for r in subset if r["status"] == 200]
            failed = [r for r in subset if r["status"] != 200]
            results[transport][key] = {
                "league": league, "event_id": eid,
                "passed": len(passed), "failed": len(failed),
                "failed_ips": [{"ip": r["ip"], "status": r["status"],
                                "reason": r["reason"]} for r in failed],
            }
            failures.extend(results[transport][key]["failed_ips"])

    total = len(rows)
    total_pass = sum(1 for r in rows if r["status"] == 200)
    pass_rate = round(100.0 * total_pass / total, 2) if total else 0.0
    out = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mission": "Phase 11 gate: 5-event multi-league x 22 IP x 2-transport canary (Kris 19:15 GMT+8)",
        "write_enabled": WRITE_ENABLED,
        "transports": ["curl_cffi_chrome124", "cloakbrowser_launch_context"],
        "events": {k: {"league": lg, "event_id": eid} for k, lg, eid in EVENTS},
        "pool_size": len(ips),
        "results": results,
        "overall": {
            "total_calls": total,
            "budget_max_calls": BUDGET_MAX_CALLS,
            "pass_rate_pct": pass_rate,
            "any_failures": bool(failures),
            "failures_detail": failures,
        },
        "phase11_recommendation": "GREEN-LIGHT" if not failures else "RED-FLAG",
        "evidence_jsonl": str(evidence),
    }
    OUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    return out


if __name__ == "__main__":
    res = asyncio.run(run())
    print(json.dumps({"total": res["overall"]["total_calls"],
                      "pass_rate_pct": res["overall"]["pass_rate_pct"],
                      "recommendation": res["phase11_recommendation"]}, indent=2))
