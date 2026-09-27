#!/usr/bin/env python3
"""gen4_fp_v2_wide_canary.py — Path 1 (Plan v0.3, Kris 2026-08-31 00:06 GMT+8, Main P1' audit approved).

Full 22-IP pool generalization: 22 IPs x 5 events x 4 endpoints x chrome124
= 440 calls hard cap. Event-major order (event -> IP -> endpoint).

Modes:
  --dry-run : assemble matrix + invariants only, ZERO network (P3')
  --smoke   : IP[0] x event[0] x 4 endpoints = 4 calls (P4')
  (default) : full 440 (P5')

Invariants: write_enabled=False; sequential; no retries; protected mtime assert;
early-stop 5 consecutive 403s or 5 consecutive conn errors; creds runtime-only.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
GOOD_FILE = REPO_ROOT / "data" / "proxy_audit" / "good_proxies_20260827_192348.txt"
EVIDENCE_DIR = Path("/tmp/gen4_fp_v2_wide_canary")
OUT_JSON = REPO_ROOT / "data" / "gen4_fp_v2_wide_canary.json"
PLAN = REPO_ROOT / "docs" / "gen4_fingerprint_v2_plan.md"
PROTECTED = [REPO_ROOT / "gen4_fetcher.py", REPO_ROOT / "fixed_pool_rotator.py",
             REPO_ROOT / "backfill_runner.py"]

EVENTS = [("A-League Men", 8351333), ("La Liga", 8280661), ("Ligue 1", 8245839),
          ("Serie A", 8337618), ("UCL", 8390922)]
ENDPOINTS = ["", "/lineups", "/statistics", "/incidents"]
BASE = "https://api.sofascore.com/api/v1/event/{eid}{tail}"
IMPERSONATE = "chrome124"
TIMEOUT_S = 20
BUDGET = 440
EARLY_STOP = 5
WRITE_ENABLED = False


def load_pool():
    pool = []
    for line in GOOD_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            ip, port, user, pw = line.split(":", 3)
            pool.append({"ip": ip, "port": port, "user": user, "pw": pw})
    return pool


def snapshot_mtimes():
    return {str(p): p.stat().st_mtime for p in PROTECTED}


def assert_mtimes(before):
    for path, mt in before.items():
        assert Path(path).stat().st_mtime == mt, f"protected file modified: {path}"


def build_matrix(pool, ip_indices=None, event_indices=None):
    """Event-major: for event -> for ip -> for endpoint."""
    cells = []
    for ei, (comp, eid) in enumerate(EVENTS):
        if event_indices is not None and ei not in event_indices:
            continue
        for ii, m in enumerate(pool):
            if ip_indices is not None and ii not in ip_indices:
                continue
            for tail in ENDPOINTS:
                cells.append({"competition": comp, "event_id": eid,
                              "endpoint": tail or "/", "ip": m["ip"], "ip_index": ii})
    return cells


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()

    assert not WRITE_ENABLED
    assert PLAN.exists()
    before_mt = snapshot_mtimes()
    pool = load_pool()
    assert len(pool) == 22

    if args.dry_run:
        matrix = build_matrix(pool)
        print(json.dumps({"mode": "dry-run", "network_calls": 0,
                          "matrix_size": len(matrix), "pool_size": len(pool),
                          "order": "event-major",
                          "protected_mtimes_ok": True}, indent=2))
        return 0

    cells = (build_matrix(pool, ip_indices={0}, event_indices={0}) if args.smoke
             else build_matrix(pool))

    from curl_cffi import requests as cffi_requests
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    evidence = EVIDENCE_DIR / "evidence.jsonl"

    rows, consec_403, consec_err, aborted, used = [], 0, 0, None, 0
    for cell in cells:
        if used >= BUDGET:
            aborted = "budget_cap_440"; break
        if consec_403 >= EARLY_STOP:
            aborted = "early_stop_5x403"; break
        if consec_err >= EARLY_STOP:
            aborted = "early_stop_5xconnerr"; break

        m = pool[cell["ip_index"]]
        url = BASE.format(eid=cell["event_id"],
                          tail="" if cell["endpoint"] == "/" else cell["endpoint"])
        proxy = "http://" + m["user"] + ":" + m["pw"] + "@" + m["ip"] + ":" + m["port"]
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

        row = {**cell, "status": status, "bytes": nbytes,
               "latency_ms": latency, "reason": reason}
        rows.append(row)
        with evidence.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        if used % 50 == 0:
            print(f"progress {used}/{len(cells)}", flush=True)

    assert_mtimes(before_mt)

    total = len(rows)
    passed = sum(1 for r in rows if r["status"] == 200)
    pct = round(100.0 * passed / total, 2) if total else 0.0

    def breakdown(key):
        d: dict = {}
        for r in rows:
            k = r[key] if key != "ip" else f"{r['ip']} (idx {r['ip_index']})"
            e = d.setdefault(str(k), {"passed": 0, "failed": 0})
            e["passed" if r["status"] == 200 else "failed"] += 1
        return d

    if aborted:
        verdict = "ABORTED"
    elif passed == total:
        verdict = "PASS_STRICT"
    elif total and passed / total >= 0.8:
        verdict = "PASS_OVERALL_WITH_FAILURES"
    else:
        verdict = "FAIL"

    out = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "plan": "docs/gen4_fingerprint_v2_plan.md v0.3 (Path 1, Kris 2026-08-31 00:06 GMT+8)",
        "mode": "smoke" if args.smoke else "full",
        "order": "event-major",
        "write_enabled": WRITE_ENABLED,
        "impersonate": IMPERSONATE,
        "pool_size": len(pool),
        "calls_used": used, "budget_cap": BUDGET, "aborted": aborted,
        "overall": {"total": total, "passed": passed, "failed": total - passed,
                    "pass_rate_pct": pct},
        "per_ip": breakdown("ip"),
        "per_event": breakdown("competition"),
        "per_endpoint": breakdown("endpoint"),
        "failed_cells": [r for r in rows if r["status"] != 200],
        "verdict": verdict,
        "evidence_jsonl": str(evidence),
    }
    OUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(json.dumps({"verdict": verdict, "passed": passed, "total": total,
                      "pct": pct, "aborted": aborted}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
