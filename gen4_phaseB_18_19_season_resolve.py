#!/usr/bin/env python3
"""gen4_phaseB_18_19_season_resolve.py — Resolve 18/19 SofaScore season ids for 17 comps.

READ-ONLY: only calls /unique-tournament/{ut}/seasons and writes a JSON map.
Uses the 20-IP pool (good_20260917_v2.txt) + curl_cffi chrome124 + health-aware pick_ip.
Year candidates: ["18/19", "2018"] (same convention as 17/18 zeroverify).
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    cffi_requests = None
    HAS_CURL_CFFI = False

from ip_health_score import pick_ip, record_outcome, is_available, snapshot, reset

POOL_FILE = ROOT / "data/proxy_pools/good_20260917_v2.txt"
OUTPUT = ROOT / "data/gen4_phaseB_18-19_season_ids.json"
API_BASE = "https://api.sofascore.com/api/v1"
TIMEOUT = 20
MAX_CALLS_PER_IP_BURST = 2

# 24 comps = competitions_10y.yaml (source of truth), incl. big-5 + J1 League + UECL
TARGET_COMPS = [
    {"name": "Premier League", "ut_id": 17, "type": "league"},
    {"name": "La Liga", "ut_id": 8, "type": "league"},
    {"name": "Serie A", "ut_id": 23, "type": "league"},
    {"name": "Bundesliga", "ut_id": 35, "type": "league"},
    {"name": "Ligue 1", "ut_id": 34, "type": "league"},
    {"name": "J1 League", "ut_id": 196, "type": "league"},
    {"name": "K League 1", "ut_id": 410, "type": "league"},
    {"name": "A-League Men", "ut_id": 136, "type": "league"},
    {"name": "Chinese Super League", "ut_id": 649, "type": "league"},
    {"name": "UCL", "ut_id": 7, "type": "continental"},
    {"name": "UEL", "ut_id": 679, "type": "continental"},
    {"name": "UECL", "ut_id": 17015, "type": "continental"},
    {"name": "AFC Champions League", "ut_id": 463, "type": "continental"},
    {"name": "AFC Champions League Two", "ut_id": 668, "type": "continental"},
    {"name": "FA Cup", "ut_id": 19, "type": "cup"},
    {"name": "EFL Cup", "ut_id": 21, "type": "cup"},
    {"name": "Copa del Rey", "ut_id": 329, "type": "cup"},
    {"name": "Coppa Italia", "ut_id": 328, "type": "cup"},
    {"name": "Coupe de France", "ut_id": 335, "type": "cup"},
    {"name": "DFB Pokal", "ut_id": 217, "type": "cup"},
    {"name": "J.League Cup", "ut_id": 101, "type": "cup"},
    {"name": "Emperor's Cup", "ut_id": 323, "type": "cup"},
    {"name": "Australia Cup", "ut_id": 1786, "type": "cup"},
    {"name": "Chinese FA Cup", "ut_id": 882, "type": "cup"},
]

YEAR_CANDIDATES = ["18/19", "2018"]


class HealthProxyPool:
    def __init__(self, pool_file: Path):
        self.pool: List[Dict[str, str]] = []
        self.ip_burst_counts: Dict[str, int] = {}
        self._cursor: int = 0
        self._load(pool_file)
        reset()

    def _load(self, pool_file: Path):
        for line in pool_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(":", 3)
            if len(parts) == 4:
                ip, port, user, pw = parts
                self.pool.append({"ip": ip, "port": port, "user": user, "pw": pw})
                self.ip_burst_counts[ip] = 0
        print(f"[pool] Loaded {len(self.pool)} proxies from {pool_file}")

    def get_next_available(self) -> Optional[Dict[str, str]]:
        if sum(self.ip_burst_counts.values()) > len(self.pool) * MAX_CALLS_PER_IP_BURST:
            for ip in self.ip_burst_counts:
                if self.ip_burst_counts[ip] >= MAX_CALLS_PER_IP_BURST:
                    self.ip_burst_counts[ip] = max(0, self.ip_burst_counts[ip] - 1)
        n = len(self.pool)
        selected = None
        for _ in range(n):
            candidate = self.pool[self._cursor % n]
            self._cursor += 1
            if is_available(candidate["ip"]):
                selected = candidate
                break
        if selected is None:
            selected = pick_ip(self.pool)
        if selected:
            self.ip_burst_counts[selected["ip"]] += 1
        return selected


async def http_get(path: str, proxy: Dict[str, str], timeout: int = TIMEOUT) -> Tuple[Optional[Any], int, Optional[str], float]:
    if not HAS_CURL_CFFI:
        return None, 0, "curl_cffi unavailable", 0.0
    proxy_url = "http://%s:%s@%s:%s" % (proxy["user"], proxy["pw"], proxy["ip"], proxy["port"])
    url = "%s%s" % (API_BASE, path)
    t0 = time.monotonic()
    try:
        response = await asyncio.to_thread(
            cffi_requests.get, url, impersonate="chrome124",
            proxies={"http": proxy_url, "https": proxy_url}, timeout=timeout,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        status = response.status_code
        try:
            body = response.json()
            error = None
        except Exception as e:
            body = None
            error = "invalid_json: %s" % e
        return body, status, error, latency_ms
    except Exception as e:
        return None, 0, "%s: %s" % (type(e).__name__, str(e)[:120]), int((time.monotonic() - t0) * 1000)


async def fetch_with_retry(path: str, pool: HealthProxyPool, max_retries: int = 3
                           ) -> Tuple[Optional[Any], int, Optional[str], Optional[str], int]:
    last_error, last_status, ip_used = None, 0, None
    for attempt in range(max_retries + 1):
        proxy = pool.get_next_available()
        if not proxy:
            await asyncio.sleep(5)
            continue
        ip_used = proxy["ip"]
        body, status, error, latency = await http_get(path, proxy)
        if status == 200 and body is not None:
            record_outcome(ip_used, "success", path=path)
        elif status == 403:
            record_outcome(ip_used, "403", path=path)
        elif status == 404:
            record_outcome(ip_used, "not_found", path=path)
        else:
            record_outcome(ip_used, "fail", path=path, extra={"http": status, "error": error})
        if status == 200 and body is not None:
            return body, status, None, ip_used, attempt + 1
        if status == 404:
            return None, 404, "http_404", ip_used, attempt + 1
        if status == 403 or status >= 500 or status == 0:
            last_error = error or "http_%s" % status
            last_status = status
            await asyncio.sleep(random.uniform(1.0, 2.0))
            continue
        last_error = error or "http_%s" % status
        last_status = status
        await asyncio.sleep(random.uniform(1.0, 2.0))
    return None, last_status, last_error, ip_used, max_retries + 1


async def resolve_comp(pool: HealthProxyPool, comp: dict) -> dict:
    ut_id = comp["ut_id"]
    rec = {"name": comp["name"], "ut_id": ut_id, "type": comp["type"],
           "season_label": "18/19", "season_id": None, "matched_year": None,
           "status": None, "error": None, "available_years": []}
    path = "/unique-tournament/%s/seasons" % ut_id
    body, status, error, ip, tries = await fetch_with_retry(path, pool, max_retries=2)
    rec["status"] = status
    rec["error"] = error
    rec["ip"] = ip
    if status != 200 or body is None:
        rec["error"] = rec["error"] or ("http_%s" % status)
        print("  ✗ %-24s HTTP %s %s" % (comp["name"], status, rec["error"]))
        return rec
    seasons = body.get("seasons", [])
    rec["available_years"] = [s.get("year") for s in seasons][:15]
    for cand in YEAR_CANDIDATES:
        for s in seasons:
            if str(s.get("year")) == cand:
                rec["season_id"] = s.get("id")
                rec["matched_year"] = cand
                break
        if rec["season_id"]:
            break
    if rec["season_id"]:
        print("  ✓ %-24s -> season_id=%s (year=%s)" % (comp["name"], rec["season_id"], rec["matched_year"]))
    else:
        print("  ✗ %-24s no match for %s; years=%s" % (comp["name"], YEAR_CANDIDATES, rec["available_years"][:8]))
    return rec


async def main():
    print("Resolving 18/19 season ids for %d comps" % len(TARGET_COMPS))
    if not POOL_FILE.exists():
        print("✗ Pool file missing: %s" % POOL_FILE)
        sys.exit(1)
    pool = HealthProxyPool(POOL_FILE)
    results = []
    for comp in TARGET_COMPS:
        rec = await resolve_comp(pool, comp)
        results.append(rec)
        await asyncio.sleep(random.uniform(0.5, 1.2))
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "season_label": "18/19",
        "year_candidates": YEAR_CANDIDATES,
        "comps": results,
    }, ensure_ascii=False, indent=2))
    print("\n✓ Wrote %s" % OUTPUT)
    missing = [r["name"] for r in results if r["season_id"] is None]
    print("resolved: %d/%d" % (len(results) - len(missing), len(results)))
    if missing:
        print("UNRESOLVED: %s" % missing)


if __name__ == "__main__":
    asyncio.run(main())