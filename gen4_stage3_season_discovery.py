#!/usr/bin/env python3
"""gen4_stage3_season_discovery.py — Phase 1: resolve 25/26 season_id for 19 comps.

For each competition in competitions_10y.yaml (excluding the 5 already-done:
Premier League, La Liga, Serie A, Bundesliga, UCL), call
GET /api/v1/unique-tournament/{ut_id}/seasons via gen4 fp v2 (chrome124 curl_cffi)
+ 21-IP good pool + rotate-per-request, then match the season whose `year`/`name`
normalizes to contain "25/26".

Output:
  - data/season_25_26_discovery.json  (list {name, ut_id, season_id_2025_26, status})
  - updates competitions_10y.yaml in-sync (season_id_2025_26 + status)

Safety: read/write local files + HTTP GET only. No DB writes here.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

POOL_FILE = ROOT / "data/proxy_pools/good_21.txt"
YAML_FILE = ROOT / "competitions_10y.yaml"
OUT = ROOT / "data/season_25_26_discovery.json"
TIMEOUT = 20

# The 5 competitions already backfilled in Stage 2 (skip discovery for them).
DONE_COMPETITIONS = {"Premier League", "La Liga", "Serie A", "Bundesliga", "UCL"}

from curl_cffi import requests as cffi_requests


def load_pool() -> list[dict]:
    pool = []
    for raw in POOL_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        ip, port, user, pw = line.split(":", 3)
        pool.append({"ip": ip, "port": port, "user": user, "pw": pw})
    if len(pool) != 21:
        raise RuntimeError(f"expected 21 proxies, got {len(pool)}")
    return pool


pool = load_pool()
_ip_counter = {"n": 0}


def next_proxy() -> dict:
    m = pool[_ip_counter["n"] % len(pool)]
    _ip_counter["n"] += 1
    return m


def api_get(path: str, max_tries: int = 2):
    last_error = None
    for _ in range(max_tries):
        m = next_proxy()
        proxy = f"http://{m['user']}:{m['pw']}@{m['ip']}:{m['port']}"
        try:
            r = cffi_requests.get(
                "https://api.sofascore.com/api/v1" + path,
                impersonate="chrome124",
                proxies={"http": proxy, "https": proxy},
                timeout=TIMEOUT,
            )
            if r.status_code == 200:
                return r.json(), m["ip"], None
            last_error = f"http_{r.status_code}"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"[:150]
    return None, None, last_error


def norm(value) -> str:
    return str(value or "").strip().lower().replace(" ", "").replace("-", "")


def match_25_26(season: dict) -> bool:
    for key in ("year", "name", "seasonLabel"):
        v = norm(season.get(key))
        if "25/26" in v or "202526" in v or ("2025" in v and "2026" in v):
            return True
    return False


def load_targets():
    data = yaml.safe_load(YAML_FILE.read_text())
    comps = data["competitions"]
    targets = [
        c for c in comps
        if c.get("name") not in DONE_COMPETITIONS
    ]
    return comps, targets


def main() -> int:
    comps, targets = load_targets()
    print(f"[discovery] {len(targets)} competitions to discover (excluding 5 done)", flush=True)

    results = []
    per_ip = {}
    for c in targets:
        name = c.get("name")
        ut_id = c.get("ut_id")
        if not ut_id:
            results.append({"name": name, "ut_id": None, "season_id_2025_26": None, "status": "blocked", "reason": "no ut_id"})
            print(f"[{name}] BLOCKED: no ut_id", flush=True)
            continue

        data, ip, err = api_get(f"/unique-tournament/{ut_id}/seasons")
        d = per_ip.setdefault(ip or "none", {"ok": 0, "fail": 0})
        if data is None:
            d["fail"] += 1
            results.append({"name": name, "ut_id": ut_id, "season_id_2025_26": None, "status": "blocked", "reason": f"seasons fetch: {err}"})
            print(f"[{name}] ut_id={ut_id} BLOCKED: {err}", flush=True)
            continue

        d["ok"] += 1
        seasons = data.get("seasons", [])
        if not isinstance(seasons, list):
            results.append({"name": name, "ut_id": ut_id, "season_id_2025_26": None, "status": "blocked", "reason": "seasons malformed"})
            print(f"[{name}] ut_id={ut_id} BLOCKED: seasons malformed", flush=True)
            continue

        found = None
        for s in seasons:
            if isinstance(s, dict) and match_25_26(s):
                found = s
                break
        if found and found.get("id"):
            sid = int(found["id"])
            results.append({"name": name, "ut_id": ut_id, "season_id_2025_26": sid, "status": "discovered_25_26", "season_year": found.get("year"), "season_name": found.get("name")})
            print(f"[{name}] ut_id={ut_id} -> season_id={sid} (year={found.get('year')!r} name={found.get('name')!r})", flush=True)
        else:
            # Report available years to aid debugging
            years = [str(s.get("year")) for s in seasons if isinstance(s, dict)]
            results.append({"name": name, "ut_id": ut_id, "season_id_2025_26": None, "status": "blocked", "reason": "25/26 season not found", "available_years": years})
            print(f"[{name}] ut_id={ut_id} BLOCKED: no 25/26 season. available={years[:12]}", flush=True)

    # ── Write discovery JSON ──
    discovered = [r for r in results if r["status"] == "discovered_25_26"]
    blocked = [r for r in results if r["status"] == "blocked"]
    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_targets": len(targets),
        "discovered": len(discovered),
        "blocked": len(blocked),
        "per_ip": per_ip,
        "results": results,
    }
    OUT.write_text(json.dumps(summary, indent=2))

    # ── Update competitions_10y.yaml in-sync ──
    by_name = {r["name"]: r for r in results}
    for c in comps:
        r = by_name.get(c.get("name"))
        if not r:
            continue
        if r["status"] == "discovered_25_26":
            c["season_id_2025_26"] = r["season_id_2025_26"]
            c["status"] = "discovered_25_26"
            c.pop("reason", None)
        else:
            c["status"] = "blocked"
    # rewrite yaml with same top-level structure
    new_yaml = {"competitions": []}
    # preserve any other top-level keys
    full = yaml.safe_load(YAML_FILE.read_text())
    full["competitions"] = comps
    YAML_FILE.write_text(yaml.safe_dump(full, sort_keys=False, allow_unicode=True))

    print(f"\n[discovery] DONE discovered={len(discovered)} blocked={len(blocked)}", flush=True)
    print(f"[discovery] artifact: {OUT}", flush=True)
    print(json.dumps({"discovered": len(discovered), "blocked": len(blocked),
                      "gate_17_of_19": len(discovered) >= 17}, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())