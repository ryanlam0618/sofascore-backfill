#!/usr/bin/env python3
"""gen4_phaseB_cup_retry_probe.py — One probe round against SofaScore seasons endpoint.

Checks whether the current 403 wall (observed ~2026-09-25 22:00 GMT+8 on all pool
IPs + direct) has cleared, using the same request path as the backfill script
(curl_cffi via Webshare pool proxies). Signature switched chrome124 ->
safari17_2_ios 2026-09-26: wall identified as Chrome-family JA3 blocking
(chrome124/chrome131 -> 403; safari17_2_ios/firefox133 -> 200 on same IPs).

Read-only GET /unique-tournament/217/seasons (DFB Pokal — already seeded, no
side effects). One round = N distinct pool IPs, 1 call each, rotating start
offset passed via argv so successive rounds sample different IPs.

Prints single-line JSON: {"ts": ..., "results": [{"ip": ..., "status": ...}, ...], "any_200": bool}
Exit code 0 if any 200 (wall cleared), 1 otherwise.

Usage: .runner-venv/bin/python3 gen4_phaseB_cup_retry_probe.py [offset] [n_ips]
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from curl_cffi import requests as cffi_requests

ROOT = Path(__file__).resolve().parent
POOL_FILE = ROOT / "data/proxy_pools/good_20260917_v2.txt"
API_BASE = "https://api.sofascore.com/api/v1"
URL = API_BASE + "/unique-tournament/217/seasons"
TIMEOUT = 15


def load_pool() -> list[dict]:
    pool = []
    for line in POOL_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":", 3)
        if len(parts) == 4:
            ip, port, user, pw = parts
            pool.append({"ip": ip, "port": port, "user": user, "pw": pw})
    return pool


def probe(proxy: dict) -> int:
    proxy_url = "http://" + proxy["user"] + ":" + proxy["pw"] + "@" + proxy["ip"] + ":" + proxy["port"]
    try:
        r = cffi_requests.get(URL, impersonate="safari17_2_ios",
                              proxies={"http": proxy_url, "https": proxy_url}, timeout=TIMEOUT)
        return r.status_code
    except Exception as e:
        return -1 * min(abs(hash(type(e).__name__)) % 90 + 10, 99)  # negative = transport error


def main() -> int:
    offset = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    n_ips = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    pool = load_pool()
    if not pool:
        print(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "error": "pool empty", "any_200": False}))
        return 1
    results = []
    for i in range(n_ips):
        p = pool[(offset + i) % len(pool)]
        status = probe(p)
        results.append({"ip": p["ip"], "status": status})
        time.sleep(2.0)
    any_200 = any(r["status"] == 200 for r in results)
    print(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                      "results": results, "any_200": any_200}))
    return 0 if any_200 else 1


if __name__ == "__main__":
    sys.exit(main())
