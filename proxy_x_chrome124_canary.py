#!/usr/bin/env python3
"""proxy_x_chrome124_canary.py — Phase 11 prerequisite (Kris 2026-08-30 14:50 GMT+8, option A).

Mission: sieve the 21-IP Webshare fixed pool for IPs that still return HTTP 200
via the `curl_cffi + impersonate=chrome124` triple-combo path, before Phase 11
locks the rotation pool.

Hard constraints (Kris):
  - write_enabled=False — pure GET, no MySQL writes.
  - Budget <= 30 calls (21 IPs + retry buffer; 1 call per IP).
  - Read-only; evidence under /tmp/proxy_x_chrome124_canary/.
  - Credentials from the good_proxies file only — never logged/hard-coded.

Outputs:
  - /tmp/proxy_x_chrome124_canary/evidence.jsonl  (per-IP raw rows, ip only — NO creds)
  - data/proxy_x_chrome124_canary.json            (summary; passed/failed + conclusion)
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from fixed_pool_rotator import FixedPoolRotator  # noqa: E402

AUDIT_JSON = REPO_ROOT / "data" / "proxy_audit" / "proxy_audit_20260827_192348.json"
GOOD_PROXIES = REPO_ROOT / "data" / "proxy_audit" / "good_proxies_20260827_192348.txt"
EVIDENCE_DIR = Path("/tmp/proxy_x_chrome124_canary")
OUT_JSON = REPO_ROOT / "data" / "proxy_x_chrome124_canary.json"

TARGET_URL = "https://api.sofascore.com/api/v1/event/14025013"
EVENT_ID = 14025013
IMPERSONATE = "chrome124"
TIMEOUT_S = 20
BUDGET_MAX_CALLS = 30          # Kris cap
WRITE_ENABLED = False          # locked off


def run() -> dict:
    assert not WRITE_ENABLED, "write must stay disabled"
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    evidence_path = EVIDENCE_DIR / "evidence.jsonl"

    rotator = FixedPoolRotator.from_audit(str(AUDIT_JSON), str(GOOD_PROXIES), EVENT_ID)
    pool = rotator._pool  # same-repo read; creds never leave this process
    calls_used = 0

    from curl_cffi import requests as cffi_requests

    rows = []
    for member in pool:
        if calls_used >= BUDGET_MAX_CALLS:
            break
        ip, port = member["ip"], member["port"]
        proxy_url = f"http://{member['user']}:{member['pw']}@{ip}:{port}"
        calls_used += 1
        t0 = time.monotonic()
        status, nbytes, err = None, 0, None
        try:
            r = cffi_requests.get(
                TARGET_URL,
                impersonate=IMPERSONATE,
                proxies={"http": proxy_url, "https": proxy_url},
                timeout=TIMEOUT_S,
            )
            status = r.status_code
            nbytes = len(r.content or b"")
        except Exception as exc:  # proxy down, timeout, TLS error, ...
            err = f"{type(exc).__name__}: {exc}"[:300]
        latency_ms = round((time.monotonic() - t0) * 1000, 1)

        reason = None
        if status == 200:
            reason = "ok"
        elif err:
            reason = err
        else:
            reason = f"http_{status}"

        row = {"ip": ip, "port": port, "status": status, "bytes": nbytes,
               "latency_ms": latency_ms, "reason": reason}
        rows.append(row)
        with evidence_path.open("a") as fh:
            fh.write(json.dumps(row) + "\n")

    passed = [r["ip"] for r in rows if r["status"] == 200]
    failed = [{"ip": r["ip"], "status": r["status"], "reason": r["reason"]}
              for r in rows if r["status"] != 200]

    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mission": "Phase 11 prerequisite — IP x chrome124 canary (Kris 14:50 GMT+8 option A)",
        "target_url": TARGET_URL,
        "impersonate": IMPERSONATE,
        "write_enabled": WRITE_ENABLED,
        "calls_used": calls_used,
        "budget_max_calls": BUDGET_MAX_CALLS,
        "total_ips": len(rows),
        "passed_count": len(passed),
        "failed_count": len(failed),
        "passed_ips": passed,
        "failed_ips": failed,
        "conclusion": (f"{len(passed)}/{len(rows)} IPs passed; "
                       f"recommend rotating pool = passed_ips"),
        "evidence_jsonl": str(evidence_path),
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    return result


if __name__ == "__main__":
    res = run()
    print(json.dumps({k: res[k] for k in ("total_ips", "passed_count",
                                          "failed_count", "calls_used",
                                          "conclusion")}, ensure_ascii=False, indent=2))
