#!/usr/bin/env python3
"""Spot-check 7 new endpoints with 5 events each."""

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Load pool
pool = []
for line in (ROOT / "data/proxy_pools/good_20260907.txt").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#"):
        parts = line.split(":", 3)
        if len(parts) == 4:
            ip, port, user, pw = parts
            pool.append({"ip": ip, "port": port, "user": user, "pw": pw})

import mysql.connector
env = dict(l.split("=", 1) for l in (ROOT / ".env").read_text().splitlines() if "=" in l and not l.startswith("#"))
pwd = os.environ.get("MYSQL_PASSWORD", env.get("MYSQL_PASSWORD", "")).strip()

conn = mysql.connector.connect(
    host="127.0.0.1", port=3306, user="appdb_rw",
    password=pwd, database="appdb", autocommit=True
)
cur = conn.cursor()

# Get 5 sample events from 25/26 finished matches
cur.execute("""
    SELECT m.match_id, c.name
    FROM matches m
    JOIN seasons s ON m.season_id = s.season_id
    JOIN competitions c ON s.competition_id = c.competition_id
    WHERE s.year_label = '25/26'
      AND m.status = 'finished'
      AND m.home_score IS NOT NULL
    ORDER BY RAND()
    LIMIT 5
""")
events = cur.fetchall()
conn.close()

print("Sample events for spot-check:")
for e in events:
    print(f"  match_id={e[0]} comp={e[1]}")

from curl_cffi import requests as cffi_requests

# Endpoints to test
ENDPOINTS = {
    "h2h": "/event/{id}/h2h",
    "votes": "/event/{id}/votes",
    "graph": "/event/{id}/graph",
    "odds": "/event/{id}/odds",
    "average-positions": "/event/{id}/average-positions",
    "best-players": "/event/{id}/best-players",
    "player-statistics": "/event/{id}/player-statistics",
}

def test_endpoint(event_id, endpoint_name, endpoint_path):
    """Test one endpoint with round-robin pool."""
    path = endpoint_path.format(id=event_id)
    for i, meta in enumerate(pool[:5]):  # Try first 5 IPs
        proxy = f"http://{meta['user']}:{meta['pw']}@{meta['ip']}:{meta['port']}"
        try:
            r = cffi_requests.get(
                f"https://api.sofascore.com/api/v1{path}",
                impersonate="chrome124",
                proxies={"http": proxy, "https": proxy},
                timeout=20,
            )
            if r.status_code == 200:
                data = r.json()
                return True, len(json.dumps(data)), r.headers.get("X-Proxy-Exit-IP", meta["ip"])
            elif r.status_code == 404:
                return "404", 0, meta["ip"]
            else:
                print(f"    {endpoint_name}: HTTP {r.status_code} via {meta['ip']}")
        except Exception as e:
            print(f"    {endpoint_name}: {type(e).__name__} via {meta['ip']}")
    return False, 0, "all_failed"

print("\n" + "=" * 60)
print("SPOT-CHECK RESULTS")
print("=" * 60)

results = {}
for event_id, comp_name in events:
    print(f"\nEvent {event_id} ({comp_name}):")
    event_results = {}
    for ep_name, ep_path in ENDPOINTS.items():
        success, size, ip = test_endpoint(event_id, ep_name, ep_path)
        event_results[ep_name] = {"success": success, "size": size, "ip": ip}
        status = "OK" if success is True else ("404" if success == "404" else "FAIL")
        print(f"  {ep_name:20s} -> {status:4s}  size={size:6d}  ip={ip}")
    results[event_id] = event_results

# Summary
print("\n" + "=" * 60)
print("SUMMARY")
print("=" * 60)
for ep_name in ENDPOINTS.keys():
    ok = sum(1 for e in results.values() if e[ep_name]["success"] is True)
    n404 = sum(1 for e in results.values() if e[ep_name]["success"] == "404")
    fail = sum(1 for e in results.values() if e[ep_name]["success"] is False)
    print(f"  {ep_name:20s} -> OK={ok}  404={n404}  FAIL={fail}")

print("\nSpot-check complete.")