#!/usr/bin/env python3
"""gen4_fp_v2_canary.py — GOOD-IP pool generalization canary (Plan v0.2-rev1, Kris P1 approved 2026-08-30 23:51 GMT+8).

Matrix: 5 COMP-BROKEN historical events x 4 endpoints x chrome124 x sha256(event)%22 GOOD-IP = 20 calls hard cap.

Modes:
  --dry-run : assemble matrix + assert invariants only, ZERO network calls (P3)
  --smoke   : first event, first 2 endpoints, 2 calls (P4)
  (default) : full 20 calls (P5)

Invariants:
  write_enabled=False; sequential (concurrency 1); no retries; protected files
  mtime asserted before/after; early-stop on 5 consecutive 403s or 5 consecutive
  conn/timeout errors; creds runtime-read, never logged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
GOOD_FILE = REPO_ROOT / "data" / "proxy_audit" / "good_proxies_20260827_192348.txt"
EVIDENCE_DIR = Path("/tmp/gen4_fp_v2_canary")
OUT_JSON = REPO_ROOT / "data" / "gen4_fp_v2_canary.json"
PLAN = REPO_ROOT / "docs" / "gen4_fingerprint_v2_plan.md"

PROTECTED = [REPO_ROOT / "gen4_fetcher.py", REPO_ROOT / "fixed_pool_rotator.py",
             REPO_ROOT / "backfill_runner.py"]

EVENTS = [
    ("A-League Men", 8351333),
    ("La Liga", 8280661),
    ("Ligue 1", 8245839),
    ("Serie A", 8337618),
    ("UCL", 8390922),
]
ENDPOINTS = ["", "/lineups", "/statistics", "/incidents"]
BASE = "https://api.sofascore.com/api/v1/event/{eid}{tail}"
IMPERSONATE = "chrome124"
TIMEOUT_S = 20
BUDGET = 20
EARLY_STOP = 5
WRITE_ENABLED = False


def load_pool():
    pool = []
    for line in GOOD_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        ip, port, user, pw = line.split(":", 3)
        pool.append({"ip": ip, "port": port, "user": user, "pw": pw})
    return pool


def ip_for_event(pool, eid: int) -> dict:
    idx = int(hashlib.sha256(str(eid).encode()).hexdigest(), 16) % len(pool)
    return pool[idx], idx


def snapshot_mtimes() -> dict:
    return {str(p): p.stat().st_mtime for p in PROTECTED}


def assert_mtimes(before: dict):
    for path, mt in before.items():
        now = Path(path).stat().st_mtime
        assert now == mt, f"protected file modified: {path}"


def build_matrix(pool):
    cells = []
    for comp, eid in EVENTS:
        member, idx = ip_for_event(pool, eid)
        for tail in ENDPOINTS:
            cells.append({"competition": comp, "event_id": eid, "endpoint": tail or "/",
                          "ip": member["ip"], "ip_index": idx})
    return cells


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()

    assert not WRITE_ENABLED, "write must stay disabled"
    assert PLAN.exists(), "plan doc missing"
    before_mt = snapshot_mtimes()

    pool = load_pool()
    assert len(pool) == 22, f"pool size {len(pool)} != 22"
    matrix = build_matrix(pool)

    if args.dry_run:
        print(json.dumps({
            "mode": "dry-run", "network_calls": 0, "matrix_size": len(matrix),
            "pool_size": len(pool),
            "impersonate": IMPERSONATE,
            "protected_mtimes_ok": True,
            "matrix_preview": matrix,
        }, indent=2))
        return 0

    cells = matrix[:2] if args.smoke else matrix
    budget_cap = len(cells)

    from curl_cffi import requests as cffi_requests

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    evidence = EVIDENCE_DIR / "evidence.jsonl"

    rows = []
    consec_403 = consec_err = 0
    aborted = None
    used = 0
    for cell in cells:
        if used >= min(BUDGET, budget_cap):
            aborted = f"budget_cap_{min(BUDGET, budget_cap)}"
            break
        if consec_403 >= EARLY_STOP:
            aborted = "early_stop_5x403"
            break
        if consec_err >= EARLY_STOP:
            aborted = "early_stop_5xconnerr"
            break

        member = next(m for m in pool if m["ip"] == cell["ip"])
        url = BASE.format(eid=cell["event_id"], tail="" if cell["endpoint"] == "/" else cell["endpoint"])
        proxy = f"http://{member['user']}:{member['pw']}@{member['ip']}:{member['port']}"
        used += 1
        t0 = time.monotonic()
        status, nbytes, reason = None, 0, None
        try:
            r = cffi_requests.get(url, impersonate=IMPERSONATE,
                                  proxies={"http": proxy, "https": proxy},
                                  timeout=TIMEOUT_S)
            status, nbytes = r.status_code, len(r.content or b"")
            reason = "ok" if status == 200 else f"http_{status}"
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"[:200]
        latency = round((time.monotonic() - t0) * 1000, 1)

        if status == 200:
            consec_403 = consec_err = 0
        elif status == 403:
            consec_403 += 1; consec_err = 0
        elif status is None:
            consec_err += 1; consec_403 = 0
        else:
            consec_403 = consec_err = 0

        row = {**{k: cell[k] for k in ("competition", "event_id", "endpoint", "ip", "ip_index")},
               "status": status, "bytes": nbytes, "latency_ms": latency, "reason": reason}
        rows.append(row)
        with evidence.open("a") as fh:
            fh.write(json.dumps(row) + "\n")

    assert_mtimes(before_mt)

    n_pass = sum(1 for r in rows if r["status"] == 200)
    total = len(rows)
    pass_pct = round(100.0 * n_pass / total, 1) if total else 0.0
    per_event = {}
    per_endpoint = {}
    for r in rows:
        ke = per_event.setdefault(f"{r['competition']}_{r['event_id']}", {"passed": 0, "failed": 0})
        ke["passed" if r["status"] == 200 else "failed"] += 1
        kp = per_endpoint.setdefault(r["endpoint"], {"passed": 0, "failed": 0})
        kp["passed" if r["status"] == 200 else "failed"] += 1

    if aborted:
        verdict = "ABORTED"
    elif n_pass == total:
        verdict = "PASS_STRICT"
    elif total and n_pass / total >= 0.8:
        verdict = "PASS_OVERALL_WITH_FAILURES"
    else:
        verdict = "FAIL"

    out = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "plan": "docs/gen4_fingerprint_v2_plan.md v0.2-rev1 (Kris P1 approved 2026-08-30 23:51 GMT+8)",
        "mode": "smoke" if args.smoke else "full",
        "write_enabled": WRITE_ENABLED,
        "impersonate": IMPERSONATE,
        "pool_size": len(pool),
        "calls_used": used,
        "budget_cap": min(BUDGET, budget_cap),
        "aborted": aborted,
        "overall": {"total": total, "passed": n_pass, "failed": total - n_pass,
                    "pass_rate_pct": pass_pct},
        "per_event": per_event,
        "per_endpoint": per_endpoint,
        "failed_cells": [r for r in rows if r["status"] != 200],
        "verdict": verdict,
        "evidence_jsonl": str(evidence),
    }
    OUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(json.dumps({"verdict": verdict, "passed": n_pass, "total": total,
                      "aborted": aborted}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
