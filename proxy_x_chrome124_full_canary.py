#!/usr/bin/env python3
"""proxy_x_chrome124_full_canary.py — Phase 11 prerequisite v2 (Kris 2026-08-30 15:16 GMT+8).

Mission: sieve the FULL 100-IP Webshare pool (not just the 21 shortlisted IPs)
for IPs that still return HTTP 200 via curl_cffi + impersonate=chrome124.

Source list: data/webshare_proxy_pool.txt (100 x ip:port:user:pass, runtime-read;
credentials never logged).

Hard constraints (Kris):
  - write_enabled=False — pure GET, no MySQL writes.
  - Budget <= 110 calls.
  - Read-only; evidence under /tmp/proxy_x_chrome124_full_canary/.
  - Optional concurrency: bounded 10-way parallel.

Outputs:
  - /tmp/proxy_x_chrome124_full_canary/evidence.jsonl
  - data/proxy_x_chrome124_full_canary.json
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
# NOTE (2026-08-30): data/webshare_proxy_pool.txt + proxy_list.txt are STALE
# (June/Aug old lists; all 407). Current authoritative 100-IP pool is the
# latest Webshare dashboard export (Aug 28), matching audit 20260827_192348.
POOL_FILE = Path("/root/.openclaw/media/inbound/Webshare_100_proxies_8---95ae8c18-b195-49a3-99df-96c7897c2184.txt")
EVIDENCE_DIR = Path("/tmp/proxy_x_chrome124_full_canary")
OUT_JSON = REPO_ROOT / "data" / "proxy_x_chrome124_full_canary.json"

TARGET_URL = "https://api.sofascore.com/api/v1/event/14025013"
IMPERSONATE = "chrome124"
TIMEOUT_S = 20
CONCURRENCY = 10
BUDGET_MAX_CALLS = 110
WRITE_ENABLED = False


# Post-rotation credentials: the 100-IP dashboard export's embedded creds are
# STALE (12-char pw → 407 on all IPs). Valid post-rotation creds are held in
# the latest good_proxies audit list (proved by 21/21 pass at 14:5x and the
# Phase-11 gate "Webshare password rotation"). Account-wide static creds apply
# to every IP in the current 100-IP list. Runtime file read only — never logged.
CREDS_FILE = REPO_ROOT / "data" / "proxy_audit" / "good_proxies_20260827_192348.txt"


def load_pool() -> list[dict]:
    cred_line = next(l for l in CREDS_FILE.read_text().splitlines()
                     if l.strip() and not l.startswith("#"))
    _, _, user, pw = cred_line.split(":", 3)  # authoritative post-rotation creds
    pool = []
    for line in POOL_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        ip, port = line.split(":", 3)[:2]
        pool.append({"ip": ip, "port": port, "user": user, "pw": pw})
    return pool


def probe_one(member: dict) -> dict:
    from curl_cffi import requests as cffi_requests
    proxy_url = f"http://{member['user']}:{member['pw']}@{member['ip']}:{member['port']}"
    t0 = time.monotonic()
    status, nbytes, err = None, 0, None
    try:
        r = cffi_requests.get(
            TARGET_URL,
            impersonate=IMPERSONATE,
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=TIMEOUT_S,
        )
        status, nbytes = r.status_code, len(r.content or b"")
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"[:300]
    latency_ms = round((time.monotonic() - t0) * 1000, 1)
    reason = "ok" if status == 200 else (err or f"http_{status}")
    return {"ip": member["ip"], "port": member["port"], "status": status,
            "bytes": nbytes, "latency_ms": latency_ms, "reason": reason}


async def run() -> dict:
    assert not WRITE_ENABLED, "write must stay disabled"
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    evidence_path = EVIDENCE_DIR / "evidence.jsonl"
    pool = load_pool()
    sem = asyncio.Semaphore(CONCURRENCY)
    calls = {"used": 0}

    async def guarded(member):
        async with sem:
            if calls["used"] >= BUDGET_MAX_CALLS:
                return {"ip": member["ip"], "port": member["port"], "status": None,
                        "bytes": 0, "latency_ms": 0, "reason": "skipped_budget"}
            calls["used"] += 1
            return await asyncio.to_thread(probe_one, member)

    rows = await asyncio.gather(*(guarded(m) for m in pool))
    with evidence_path.open("a") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")

    passed = [r["ip"] for r in rows if r["status"] == 200]
    failed = [{"ip": r["ip"], "status": r["status"], "reason": r["reason"]}
              for r in rows if r["status"] != 200]

    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mission": "Phase 11 prerequisite v2 — full 100-IP x chrome124 canary (Kris 15:16 GMT+8)",
        "target_url": TARGET_URL,
        "impersonate": IMPERSONATE,
        "write_enabled": WRITE_ENABLED,
        "concurrency": CONCURRENCY,
        "calls_used": calls["used"],
        "budget_max_calls": BUDGET_MAX_CALLS,
        "total_ips": len(rows),
        "passed_count": len(passed),
        "failed_count": len(failed),
        "passed_ips": passed,
        "failed_ips": failed,
        "conclusion": (f"{len(passed)}/{len(rows)} IPs passed; "
                       f"rotation pool = passed_ips; dropped = failed_ips"),
        "evidence_jsonl": str(evidence_path),
    }
    OUT_JSON.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    return result


if __name__ == "__main__":
    res = asyncio.run(run())
    print(json.dumps({k: res[k] for k in ("total_ips", "passed_count", "failed_count",
                                          "calls_used", "conclusion")}, indent=2))
