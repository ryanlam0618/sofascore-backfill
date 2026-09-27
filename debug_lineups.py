#!/usr/bin/env python3
"""Debug lineups payload - check for null names (using double quotes)."""

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

from curl_cffi import requests as cffi_requests
meta = pool[0]
proxy = "http://{}:{}@{}:{}".format(meta["user"], meta["pw"], meta["ip"], meta["port"])

event_id = 14015654
r = cffi_requests.get(
    "https://api.sofascore.com/api/v1/event/{}/lineups".format(event_id),
    impersonate="chrome124",
    proxies={"http": proxy, "https": proxy},
    timeout=20,
)
print("HTTP", r.status_code)
data = r.json()

home = data.get("home", {})
away = data.get("away", {})

# Check player structure
for side, team in [("home", home), ("away", away)]:
    players = team.get("players", [])
    print(f"\n{side} players: {len(players)}")
    for p in players:
        pid = p.get("player", {}).get("id") if isinstance(p.get("player"), dict) else p.get("id")
        pname = p.get("player", {}).get("name") if isinstance(p.get("player"), dict) else p.get("name")
        if pname is None or pname == "":
            print(f"  NULL/EMPTY name: player_id={pid}, keys={list(p.keys())}")

# Print first player full structure
if home.get("players"):
    print("\nFirst home player full structure:")
    print(json.dumps(home["players"][0], indent=2)[:800])