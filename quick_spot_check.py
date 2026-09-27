#!/usr/bin/env python3
"""Quick spot-check - test all 7 endpoints with correct proxy format."""

import os
import sys
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Load pool - first 5 IPs
pool = []
for line in (ROOT / "data/proxy_pools/good_20260907.txt").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#"):
        parts = line.split(":", 3)
        if len(parts) == 4:
            ip, port, user, pw = parts
            pool.append({"ip": ip, "port": port, "user": user, "pw": pw})
            if len(pool) >= 5:
                break

import mysql.connector
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

conn = mysql.connector.connect(
    host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
    port=int(os.environ.get("MYSQL_PORT", "3306")),
    user=os.environ.get("MYSQL_USER", "appdb_rw"),
    password=os.environ.get("MYSQL_PASSWORD", ""),
    database=os.environ.get("MYSQL_DATABASE", "appdb"),
    autocommit=True
)
cur = conn.cursor()

cur.execute("""
    SELECT m.match_id, c.name
    FROM matches m
    JOIN seasons s ON m.season_id = s.season_id
    JOIN competitions c ON s.competition_id = c.competition_id
    WHERE s.year_label = '25/26'
      AND m.status = 'finished'
      AND m.home_score IS NOT NULL
    ORDER BY RAND()
    LIMIT 3
""")
events = cur.fetchall()
conn.close()

print("Sample events:")
for e in events:
    print("  {} ({})".format(e[0], e[1]))

from curl_cffi import requests as cffi_requests

ENDPOINTS = {
    "h2h": "/event/{}/h2h",
    "votes": "/event/{}/votes",
    "graph": "/event/{}/graph",
    "odds": "/event/{}/odds",
    "average-positions": "/event/{}/average-positions",
    "best-players": "/event/{}/best-players",
    "player-statistics": "/event/{}/player-statistics",
}

def make_proxy(meta):
    return "http://{}:{}@{}:{}".format(meta['user'], meta['pw'], meta['ip'], meta['port'])

print("\n" + "=" * 70)
print("SPOT-CHECK RESULTS")
print("=" * 70)

results = {}
for event_id, comp_name in events:
    print("\nEvent {} ({}):".format(event_id, comp_name))
    event_results = {}
    for ep_name, ep_path in ENDPOINTS.items():
        path = ep_path.format(event_id)
        success = False
        http_code = None
        size = 0
        keys = []
        used_ip = None
        
        # Try up to 3 IPs
        for attempt in range(3):
            meta = pool[attempt % len(pool)]
            proxy = make_proxy(meta)
            try:
                r = cffi_requests.get(
                    "https://api.sofascore.com/api/v1{}".format(path),
                    impersonate="chrome124",
                    proxies={"http": proxy, "https": proxy},
                    timeout=20,
                )
                used_ip = meta['ip']
                http_code = r.status_code
                size = len(r.text)
                if r.status_code == 200:
                    data = r.json()
                    keys = list(data.keys())[:8]
                    success = True
                elif r.status_code == 404:
                    break
                break  # got a response
            except Exception as e:
                print("  {:20s} attempt {} ERROR: {}: {}".format(
                    ep_name, attempt+1, type(e).__name__, str(e)[:80]))
                continue
        
        event_results[ep_name] = {
            "success": success, "http": http_code, "size": size, "ip": used_ip
        }
        status = "OK" if success else ("404" if http_code == 404 else "FAIL")
        print("  {:20s} -> {:4s} HTTP={} size={:6d} ip={}".format(
            ep_name, status, http_code or '-', size, used_ip or '-'))
        if keys:
            print("    Keys: {}".format(keys))
    results[event_id] = event_results

# Summary
print("\n" + "=" * 70)
print("SUMMARY (endpoints OK / 3events)")
print("=" * 70)
for ep_name in ENDPOINTS.keys():
    ok = sum(1 for e in results.values() if e[ep_name]["success"])
    n404 = sum(1 for e in results.values() if e[ep_name]["http"] == 404)
    fail = sum(1 for e in results.values() if e[ep_name]["http"] not in (200, 404) or e[ep_name]["http"] is None)
    print("  {:20s} -> OK={}  404={}  FAIL={}".format(ep_name, ok, n404, fail))

# Save results
output = {"events": events, "results": results}
out_path = ROOT / "data/gen4_stage4c_endpoint_spot_check.json"
out_path.write_text(json.dumps(output, indent=2, default=str))
print("\nResults saved to {}".format(out_path))
print("Done.")