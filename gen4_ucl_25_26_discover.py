#!/usr/bin/env python3
"""gen4_ucl_25_26_discover.py — UCL 25/26 season_id discovery + smoke test.

Kris explicit task 2026-09-06 23:44 GMT+8 (option (c) together).
- GET /unique-tournament/7/seasons  (UCL ut_id=7)
- Pick season with name containing 2025/2026 (or 25/26 fallback)
- Smoke test: /unique-tournament/7/season/{sid}/events/last -> 200 + events
- Write JSON evidence + update competitions_10y.yaml

No DB write. Read-only probe.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from curl_cffi import requests as cffi_requests

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

POOL_FILE = ROOT / "data/proxy_pools/good_20260906.txt"
YAML_FILE = ROOT / "competitions_10y.yaml"
EVIDENCE = ROOT / "data/gen4_ucl_25_26_discovery.json"

UCL_UT_ID = 7
API = "https://api.sofascore.com/api/v1"


def load_pool():
    pool = []
    for l in POOL_FILE.read_text().splitlines():
        if l.strip() and not l.startswith("#"):
            ip, port, user, pw = l.strip().split(":", 3)
            pool.append({"ip": ip, "port": port, "user": user, "pw": pw})
    assert len(pool) >= 20, f"pool too small: {len(pool)}"
    return pool


def get_json(path, pool, retries=5):
    """Same retry-with-rotation pattern as gen4_stage3_expansion_backfill.fetch_json."""
    last = None
    offset = int(time.time()) % len(pool)
    for attempt in range(retries + 1):
        m = pool[(offset + attempt) % len(pool)]
        proxy = f"http://{m['user']}:{m['pw']}@{m['ip']}:{m['port']}"
        try:
            r = cffi_requests.get(API + path,
                                  impersonate="chrome124",
                                  proxies={"http": proxy, "https": proxy},
                                  timeout=20)
            if r.status_code == 200:
                return r.json(), r.status_code, m["ip"], None
            last = (r.status_code, m["ip"], f"http_{r.status_code}")
        except Exception as e:
            last = (None, m["ip"], f"{type(e).__name__}: {str(e)[:120]}")
    return None, last[0], last[1], last[2]


def main():
    pool = load_pool()
    print(f"[ucl-discover] pool={len(pool)} IPs", flush=True)

    # 1) GET /unique-tournament/7/seasons
    body, http, ip, err = get_json(f"/unique-tournament/{UCL_UT_ID}/seasons", pool)
    if err or http != 200:
        print(f"FAIL seasons list: http={http} err={err}", flush=True)
        return 1
    seasons = body.get("seasons", [])
    print(f"[ucl-discover] /unique-tournament/{UCL_UT_ID}/seasons -> {len(seasons)} seasons (ip={ip})", flush=True)

    # 2) Find 25/26 candidate. Sofascore uses year field {year: 2025/2026} as label.
    candidate = None
    for s in seasons:
        name = (s.get("name") or "")
        year = (s.get("year") or {})
        # Sofascore year shape: {"year": "2025/2026"} per recent API
        year_str = year.get("year") if isinstance(year, dict) else (str(year) if year else "")
        if ("2025/2026" in year_str) or ("2025" in year_str and "2026" in year_str) \
           or ("25/26" in name.lower()):
            candidate = s
            break

    if not candidate:
        # fall back: any season whose id appears in DB seasons table for ut=7
        print("WARN: no obvious 25/26 match in API; will check DB seasons table.", flush=True)
        candidate = None

    sid = candidate["id"] if candidate else None
    candidate_name = (candidate or {}).get("name", "?")
    candidate_year = (candidate or {}).get("year", "?")
    print(f"[ucl-discover] candidate: id={sid} name={candidate_name!r} year={candidate_year}", flush=True)

    # 3) Smoke test: /unique-tournament/7/season/{sid}/events/last
    smoke_http, smoke_ip, smoke_err, smoke_events = None, None, None, []
    if sid:
        body2, http2, ip2, err2 = get_json(f"/unique-tournament/{UCL_UT_ID}/season/{sid}/events/last/0", pool)
        smoke_http, smoke_ip, smoke_err = http2, ip2, err2
        if body2:
            smoke_events = body2.get("events", [])[:5]
        print(f"[ucl-discover] smoke test /events/last/0 -> http={http2} events_count={len(smoke_events)} (ip={ip2})", flush=True)

    evidence = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "task": "ucl_25_26_season_id_discovery",
        "ucl_ut_id": UCL_UT_ID,
        "seasons_returned": len(seasons),
        "candidate": {
            "season_id": sid,
            "name": candidate_name,
            "year": candidate_year,
        },
        "smoke_test": {
            "http": smoke_http,
            "ip": smoke_ip,
            "error": smoke_err,
            "events_count": len(smoke_events),
            "sample_event_ids": [e.get("id") for e in smoke_events[:3]],
        },
    }
    EVIDENCE.write_text(json.dumps(evidence, indent=2))
    print(f"[ucl-discover] evidence: {EVIDENCE}", flush=True)

    if not sid or smoke_http != 200 or not smoke_events:
        print("FAIL: discovery incomplete or smoke test failed", flush=True)
        return 2

    # 4) Update competitions_10y.yaml — update UCL entry
    import yaml
    raw = YAML_FILE.read_text()
    cfg = yaml.safe_load(raw)
    ucl_entry = None
    for c in cfg["competitions"]:
        if c.get("ut_id") == UCL_UT_ID and "UCL" in c["name"]:
            ucl_entry = c
            break
    if ucl_entry is None:
        print(f"FAIL: UCL entry not found in {YAML_FILE}", flush=True)
        return 3

    old_status = ucl_entry.get("status")
    old_sid = ucl_entry.get("season_id_2025_26")
    ucl_entry["season_id_2025_26"] = sid
    ucl_entry["status"] = "discovered_25_26"
    notes_old = ucl_entry.get("notes", "")
    ucl_entry["notes"] = (
        f"{notes_old}; season_id_2025_26={sid} discovered via "
        f"/unique-tournament/7/seasons + smoke /events/last/0 (2026-09-06 Kris task, Forge execute)"
    ).strip("; ")

    YAML_FILE.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True, width=120))
    print(f"[ucl-discover] yaml updated: UCL season_id_2025_26 {old_sid} -> {sid} status {old_status} -> discovered_25_26", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
