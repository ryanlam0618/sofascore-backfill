#!/usr/bin/env python3
"""fk_9521037_diag.py — Read-only diagnostic for event 9521037 FK 1216 failure.

Two GETs via the same request path as the backfill (curl_cffi safari17_2_ios
via Webshare pool):
  1. /unique-tournament/1786/seasons   -> authoritative AusCup season ids
  2. /event/9521037                    -> the event's own season/tournament/teams

Writes payloads to data/fk_9521037_diag.json. Read-only: zero DB writes.
Usage: .runner-venv/bin/python3 fk_9521037_diag.py
"""
from __future__ import annotations
import json, time
from datetime import datetime, timezone
from pathlib import Path
from curl_cffi import requests as cffi_requests

ROOT = Path(__file__).resolve().parent
POOL_FILE = ROOT / "data/proxy_pools/good_20260917_v2.txt"
API_BASE = "https://api.sofascore.com/api/v1"
OUT = ROOT / "data/fk_9521037_diag.json"


def load_pool():
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


def fetch(url, pool, max_tries=3):
    for i in range(max_tries):
        p = pool[i % len(pool)]
        proxy_url = "http://" + p["user"] + ":" + p["pw"] + "@" + p["ip"] + ":" + p["port"]
        try:
            r = cffi_requests.get(url, impersonate="safari17_2_ios",
                                  proxies={"http": proxy_url, "https": proxy_url}, timeout=15)
            if r.status_code == 200:
                return {"status": 200, "ip": p["ip"], "attempt": i + 1, "body": r.json()}
            last = {"status": r.status_code, "ip": p["ip"], "attempt": i + 1, "body": None}
        except Exception as e:
            last = {"status": -1, "ip": p["ip"], "attempt": i + 1, "error": str(e)[:120], "body": None}
        time.sleep(2.0)
    return last


def main():
    pool = load_pool()
    out = {"ts": datetime.now(timezone.utc).isoformat(), "calls": 0, "results": {}}

    seasons = fetch(API_BASE + "/unique-tournament/1786/seasons", pool)
    out["calls"] += seasons.get("attempt", 0)
    out["results"]["utc_1786_seasons"] = seasons

    event = fetch(API_BASE + "/event/9521037", pool)
    out["calls"] += event.get("attempt", 0)
    out["results"]["event_9521037"] = event

    OUT.write_text(json.dumps(out, indent=1, ensure_ascii=False))

    # Human-readable summary
    s = seasons.get("body") or {}
    season_list = s.get("seasons") or s.get("seasonTournaments") or []
    print("=== /unique-tournament/1786/seasons (status", seasons.get("status"), ") ===")
    for se in season_list:
        print("  sid=%s year=%s name=%s" % (se.get("id"), se.get("year"), se.get("name")))
    e = event.get("body") or {}
    ev = e.get("event") or {}
    print("=== /event/9521037 (status", event.get("status"), ") ===")
    print("  season:", (ev.get("season") or {}).get("id"), (ev.get("season") or {}).get("year"))
    print("  tournament:", (ev.get("tournament") or {}).get("id"),
          (ev.get("tournament") or {}).get("name"),
          "ut:", ((ev.get("tournament") or {}).get("uniqueTournament") or {}).get("id"),
          ((ev.get("tournament") or {}).get("uniqueTournament") or {}).get("name"))
    print("  home:", (ev.get("homeTeam") or {}).get("id"), (ev.get("homeTeam") or {}).get("name"))
    print("  away:", (ev.get("awayTeam") or {}).get("id"), (ev.get("awayTeam") or {}).get("name"))
    print("  startTimestamp:", ev.get("startTimestamp"))
    print("  status:", (ev.get("status") or {}).get("description"))
    print("calls used:", out["calls"])


if __name__ == "__main__":
    main()
