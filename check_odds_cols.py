#!/usr/bin/env python3
"""Quick check for match_odds columns and fix."""

import mysql.connector
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
env = dict(l.split("=", 1) for l in (ROOT / ".env").read_text().splitlines() if "=" in l and not l.startswith("#"))
pwd = os.environ.get("MYSQL_PASSWORD", env.get("MYSQL_PASSWORD", "")).strip()

conn = mysql.connector.connect(
    host="127.0.0.1", port=3306, user="appdb_rw",
    password=pwd,
    database="appdb", autocommit=True
)
cur = conn.cursor()

print("=== Current match_odds columns ===")
cur.execute("SELECT * FROM match_odds LIMIT 0")
for d in cur.description:
    print(f"  {d[0]}")

conn.close()