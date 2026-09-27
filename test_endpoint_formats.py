#!/usr/bin/env python3
"""Test correct URL formats for odds and player-statistics endpoints."""

import os
import sys
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

pool = []
for line in (ROOT / "data/proxy_pools/good_20260907.txt").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#"):
        parts = line.split(":", 3)
        if len(parts) == 4:
            ip, port, user, pw = parts
            pool.append({"ip": ip, "port": port, "user": user, "pw": pw})

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
    LIMIT 1
""")
event = cur.fetchone()
conn.close()

event_id = event[0]
print("Testing event {} ({})".format(event_id, event[1]))

from curl_cffi import requests as cffi_requests

meta = pool[0]
proxy = "http://{}:{}@{}:{}".format(meta['user'], meta['pw'], meta['ip'], meta['port'])

# Test different URL formats
test_urls = {
    "odds_format1": "/event/{}/odds",                    # already 404
    "odds_format2": "/odds/1/all",                        # suggested in task
    "odds_format3": "/event/{}/odds/1/all".format(event_id),
    "player_stats_1": "/event/{}/player-statistics".format(event_id),  # already 404
    "player_stats_2": "/event/{}/lineups".format(event_id),           # lineups has stats
    "player_stats_3": "/event/{}/statistics".format(event_id),        # statistics endpoint
}

for name, path in test_urls.items():
    try:
        r = cffi_requests.get(
            "https://api.sofascore.com/api/v1{}".format(path),
            impersonate="chrome124",
            proxies={"http": proxy, "https": proxy},
            timeout=20,
        )
        print("  {:20s} -> HTTP {}  size={}".format(name, r.status_code, len(r.text)))
        if r.status_code == 200:
            data = r.json()
            print("    Keys: {}".format(list(data.keys())[:10]))
    except Exception as e:
        print("  {:20s} -> ERROR: {}: {}".format(name, type(e).__name__, str(e)[:80]))

print("Done.")