#!/usr/bin/env python3
"""
bench_method_c_retry.py — Re-run Method C (gen3 StableProxyFetcher) ALONE

Date:           2026-08-23 (retry after first run was killed by exec timeout)
Purpose:        Get partial results for Method C; A and B are already 100% (logged).
Safety:         Halt on first all-IP-bad streak; preserve partial results.
Previous:       logs/bench_targeted_20260823_retry.log (Method A 100%, Method B 100%)
Output:         data/method_benchmark_method_c_retry_20260823_HHMMSS.json
Log:            logs/bench_method_c_retry_20260823.log

NOTE: gen3's PROXY dict has hardcoded credentials (see stable_proxy_fetch.py:34-37).
      This is by design; we don't override it. gen3 uses SAME proxy pool as A/B.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import statistics
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = REPO_ROOT / "data"
ARTIFACT_DIR.mkdir(exist_ok=True)
TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
ARTIFACT_PATH = ARTIFACT_DIR / f"method_benchmark_method_c_retry_{TIMESTAMP}.json"
LOG_PATH = REPO_ROOT / "logs" / f"bench_method_c_retry_20260823.log"

# ── Config ─────────────────────────────────────────────────────────────────
TEST_EVENTS = [
    (14025013, "PL 25/26", "Liverpool vs Bournemouth"),
    (12436870, "PL 24/25", "Man Utd vs Fulham"),
    (11352303, "PL 23/24", "Burnley vs Man City"),
    (8896967,  "PL 20/21", "Fulham vs Arsenal"),
    (7827861,  "PL 18/19", "Man Utd vs Leicester"),
    (14056037, "Bundesliga 25/26", "Bayern vs RB Leipzig"),
    (12764526, "UCL 24/25", "Young Boys vs Aston Villa"),
    (13335241, "K League 2025", "Pohang vs Daejeon"),
    (11917915, "J1 2024", "Sanfrecce vs Urawa"),
    (12060227, "CSL 2024", "Shandong vs Changchun"),
]
ENDPOINTS = ["event", "incidents"]

# Cooldowns
COOLDOWN_PER_EVENT = 10
COOLDOWN_AFTER_BAD_IP_RUN = 30  # longer cooldown if multiple bad IPs in a row

# Safety (tighter for retry run)
MAX_EVENTS = 10
MAX_BAD_IP_STREAK = 3   # 3 events in a row where ALL 5 attempts were bad → halt
MAX_RUNTIME_SEC = 1500  # 25 min hard wall-time cap (under 30-min exec limit)

# ── Logging ────────────────────────────────────────────────────────────────
START_TS = datetime.now(timezone.utc).isoformat()

def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    # also append to file (unbuffered)
    try:
        with LOG_PATH.open("a") as f:
            f.write(line + "\n")
    except Exception:
        pass

# ── Metrics ────────────────────────────────────────────────────────────────
records = []
halted = False
halt_reason = None
partial_run = False
config_failures = []

def check_bad_ip_streak() -> bool:
    """Check if last 3 events all had 0 successful IP attempts."""
    if len(records) == 0:
        return False
    # Group by eid, count events where every attempt was bad
    eid_attempts = {}
    for r in records:
        eid_attempts.setdefault(r["eid"], []).append(r["status"])
    bad_streak = sum(
        1 for eid, statuses in eid_attempts.items()
        if all(s != 200 for s in statuses)
    )
    return bad_streak >= MAX_BAD_IP_STREAK


# ── Run ────────────────────────────────────────────────────────────────────
def run_method_c() -> None:
    global halted, halt_reason, partial_run
    log("=== bench_method_c_retry.py START ===")
    log(f"Events: {len(TEST_EVENTS)}, Endpoints: {ENDPOINTS}")
    log(f"Safety: max_bad_ip_streak={MAX_BAD_IP_STREAK}, max_runtime={MAX_RUNTIME_SEC}s")

    sys.path.insert(0, str(REPO_ROOT))
    sys.path.insert(0, str(REPO_ROOT / ".runner-venv" / "lib" / "python3.11" / "site-packages"))
    from stable_proxy_fetch import StableProxyFetcher

    fetcher = StableProxyFetcher()
    log(f"Fetcher initialized (gen3 StableProxyFetcher, CloakBrowser subprocess)")

    wall_start = time.time()

    for idx, (eid, season, match) in enumerate(TEST_EVENTS, 1):
        elapsed = time.time() - wall_start
        if elapsed > MAX_RUNTIME_SEC:
            halted = True
            halt_reason = f"wall-time cap {MAX_RUNTIME_SEC}s reached after {idx-1} events"
            partial_run = True
            log(f"⚠ HALTED: {halt_reason}")
            break

        if check_bad_ip_streak():
            halted = True
            halt_reason = f"MAX_BAD_IP_STREAK={MAX_BAD_IP_STREAK} events with all-bad IPs"
            partial_run = True
            log(f"⚠ HALTED: {halt_reason}")
            break

        log(f"--- Event {idx}/10: eid={eid} ({season} — {match}) [elapsed {elapsed:.0f}s] ---")

        # ── ONE subprocess captures BOTH endpoints ──
        t0 = time.time()
        result = None
        fetch_error = None
        try:
            result = fetcher.fetch_event(eid)
        except Exception as e:
            fetch_error = str(e)[:200]
            log(f"  fetch_event raised: {e}")

        dt = time.time() - t0

        if result is None:
            log(f"  ❌ fetch_event returned None ({dt:.1f}s) err={fetch_error}")
            for ep in ENDPOINTS:
                rec = {
                    "eid": eid, "season": season, "match": match, "endpoint": ep,
                    "status": 0, "latency_s": round(dt / len(ENDPOINTS), 3),
                    "transport": "subprocess_fail", "error": "fetch_event returned None",
                }
                records.append(rec)
            time.sleep(COOLDOWN_AFTER_BAD_IP_RUN if dt > 120 else COOLDOWN_PER_EVENT)
            continue

        api_ok = result.get("api_ok", 0)
        api_403 = result.get("api_403", 0)
        retries = result.get("retries", 0)
        log(f"  ✅ result ({dt:.1f}s, retries={retries}, api_ok={api_ok}, api_403={api_403})")

        for ep in ENDPOINTS:
            ep_data = result.get("data", {}).get(ep, None)
            ep_status_map = result.get("api_status", {})
            ep_status = 200 if ep_data is not None else ep_status_map.get(ep, 0)
            if ep_status == 407:
                config_failures.append({"eid": eid, "endpoint": ep, "status": 407})
            rec = {
                "eid": eid, "season": season, "match": match, "endpoint": ep,
                "status": ep_status, "latency_s": round(dt, 3),
                "transport": "cloakbrowser_subprocess",
                "error": None if ep_status == 200 else f"missing in capture",
                "retries": retries, "api_ok": api_ok, "api_403": api_403,
            }
            records.append(rec)
            log(f"    ep={ep} → {ep_status}")

        time.sleep(COOLDOWN_PER_EVENT)

    # ── Summary ──
    total = len(records)
    success = sum(1 for r in records if r["status"] == 200)
    blocked = sum(1 for r in records if r["status"] == 403)
    other = total - success - blocked
    valid = total - len(config_failures)
    sr = success / valid if valid else 0.0
    br = blocked / valid if valid else 0.0
    lats = [r["latency_s"] for r in records if r["status"] == 200]
    p50 = statistics.median(lats) if lats else None
    p90 = statistics.quantiles(lats, n=10)[8] if len(lats) >= 10 else (max(lats) if lats else None)

    log("=" * 60)
    log(f"Method C summary: {total} req, 200={success}, 403={blocked}, other={other}, 407={len(config_failures)}")
    log(f"success_rate={sr:.2%}, block_rate={br:.2%}, latency_p50={p50:.2f}s, p90={p90:.2f}s" if p50 else f"success_rate={sr:.2%}")
    log(f"halted={halted}, partial_run={partial_run}, reason={halt_reason}")

    # ── Persist ──
    payload = {
        "started_at": START_TS,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "proxy_configured": True,  # gen3 uses hardcoded creds, not env
        "previous_results": {
            "Method A": "100% success (logs/bench_targeted_20260823_retry.log)",
            "Method B": "100% success (logs/bench_targeted_20260823_retry.log)",
        },
        "config": {
            "test_events": [{"eid": e, "season": s, "match": ma} for e, s, ma in TEST_EVENTS],
            "endpoints": ENDPOINTS,
            "safety": {
                "max_bad_ip_streak": MAX_BAD_IP_STREAK,
                "max_runtime_sec": MAX_RUNTIME_SEC,
                "cooldown_per_event": COOLDOWN_PER_EVENT,
                "cooldown_after_bad_ip_run": COOLDOWN_AFTER_BAD_IP_RUN,
            },
        },
        "summary": {
            "total_requests": total,
            "success_200": success,
            "blocked_403": blocked,
            "other": other,
            "config_failures_407": len(config_failures),
            "success_rate": round(sr, 4),
            "block_rate": round(br, 4),
            "latency_p50_s": round(p50, 3) if p50 else None,
            "latency_p90_s": round(p90, 3) if p90 else None,
            "halted": halted,
            "halt_reason": halt_reason,
            "partial_run": partial_run,
        },
        "records": records,
        "config_failures": config_failures,
    }
    ARTIFACT_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    log(f"[SAVE] → {ARTIFACT_PATH}")


if __name__ == "__main__":
    # Truncate log
    LOG_PATH.write_text("")
    run_method_c()
