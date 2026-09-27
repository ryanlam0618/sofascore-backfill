#!/usr/bin/env python3
"""Fix match_odds schema to match backfill_runner.insert_odds expectations."""

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

# ALTER match_odds to add columns needed by backfill_runner.insert_odds
alter_sql = """
ALTER TABLE match_odds
ADD COLUMN market_id BIGINT AFTER match_id,
ADD COLUMN market_name VARCHAR(100) AFTER market_id,
ADD COLUMN market_group VARCHAR(50) AFTER market_name,
ADD COLUMN market_period VARCHAR(20) AFTER market_group,
ADD COLUMN structure_type VARCHAR(50) AFTER market_period,
ADD COLUMN suspended TINYINT(1) DEFAULT 0 AFTER structure_type,
ADD COLUMN choice_name VARCHAR(100) AFTER suspended,
ADD COLUMN initial_fractional_value VARCHAR(50) AFTER choice_name,
ADD COLUMN fractional_value VARCHAR(50) AFTER initial_fractional_value,
ADD COLUMN winning TINYINT(1) DEFAULT 0 AFTER fractional_value
"""

try:
    cur.execute(alter_sql)
    print("ALTER TABLE match_odds succeeded")
except Exception as e:
    print(f"ALTER failed: {e}")

# Verify new columns
cur.execute("SELECT * FROM match_odds LIMIT 0")
print("=== match_odds columns after ALTER ===")
for d in cur.description:
    print(f"  {d[0]}")

conn.close()