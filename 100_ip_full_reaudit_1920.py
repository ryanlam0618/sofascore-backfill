#!/usr/bin/env python3
"""100_ip_full_reaudit_19_20.py — Single-event re-audit of 100 Webshare IPs on 19/20 history.

Event: 8351333 (A-League Men 19/20). API prober.
Client: curl_cffi impersonate=chrome (browser fingerprint) — NOT default urllib.
No retries, 10s per IP. ~100 requests.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent
SRC_JSON = REPO_ROOT / "data" / "proxy_audit" / "proxy_audit_20260827_211530.json"
TARGET_URL = "https://www.sofascore.com/api/v1/event/8351333"


def _h8(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:8]


async def main() -> None:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
    user = os.environ.get("SOFA_PROXY_USER", "***REMOVED***")
    pw = os.environ.get("SOFA_PROXY_PASS", "")
    if not pw:
        print("missing password, abort", flush=True)
        return

    rows = json.loads(SRC_JSON.read_text())["rows"]
    print(f"[reaudit-100ip] {len(rows)} IPs", flush=True)

    loop = asyncio.get_event_loop()

    def probe(r):
        ip = r.get("ip") or r.get("proxy_ip") or "?"
        port = r.get("port") or r.get("proxy_port") or "?"
        proxy_url = "http://" + user + ":" + pw + "@" + str(ip) + ":" + str(port)
        t0 = time.monotonic()
        try:
            from curl_cffi import requests as cr
            resp = cr.get(TARGET_URL, proxies={"http": proxy_url, "https": proxy_url},
                          impersonate="chrome", timeout=10)
            status = resp.status_code
            latency = int((time.monotonic() - t0) * 1000)
            body_ok = False
            if status == 200:
                try:
                    d = resp.json()
                    body_ok = isinstance(d, dict) and "event" in d
                except Exception:
                    body_ok = False
            return {"ip_index": _h8(f"{ip}:{port}"), "status": status, "latency_ms": latency,
                    "body_ok": body_ok, "verdict": "PASS" if (status == 200 and body_ok) else ("FAIL-403" if status == 403 else f"FAIL-{status}")}
        except Exception as e:
            return {"ip_index": _h8(f"{ip}:{port}"), "status": 0, "latency_ms": int((time.monotonic() - t0) * 1000),
                    "body_ok": False, "verdict": f"FAIL-{type(e).__name__}"}

    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = [loop.run_in_executor(ex, probe, r) for r in rows]
        results = []
        for i, f in enumerate(asyncio.as_completed(futs), 1):
            results.append(await f)
            if i % 10 == 0:
                print(f"[reaudit-100ip] {i}/{len(rows)}", flush=True)

    n_pass = sum(1 for r in results if r["verdict"] == "PASS")
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = REPO_ROOT / "data" / "proxy_audit" / f"100_ip_full_reaudit_{ts}.json"
    json.dump({
        "task": "100_ip_full_reaudit_19_20",
        "target": "api/v1/event/8351333",
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "n_ips": len(results),
        "n_pass": n_pass,
        "results": sorted(results, key=lambda x: x["latency_ms"]),
    }, open(out, "w"), indent=2)
    print(f"n_pass={n_pass}/{len(results)}", flush=True)
    print(f"written: {out}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
