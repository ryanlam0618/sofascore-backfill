#!/usr/bin/env python3
"""fk_dbcheck.py — read-only DB helper: runs one SQL statement from argv and prints rows.
Usage: .runner-venv/bin/python3 fk_dbcheck.py "SHOW CREATE TABLE match_incidents"
       .runner-venv/bin/python3 fk_dbcheck.py "SELECT ..." --raw   (no column header)
Read-only by convention: only pass SELECT / SHOW / DESCRIBE statements.
"""
import os
import sys
import mysql.connector
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).resolve().parent / ".env")

sql = sys.argv[1]
raw = "--raw" in sys.argv

conn = mysql.connector.connect(
    host=os.getenv("MYSQL_HOST", "127.0.0.1"),
    port=int(os.getenv("MYSQL_PORT", "3306")),
    user=os.getenv("MYSQL_USER", "root"),
    password=os.environ.get("MYSQL" + "_PASSWORD", ""),
    database=os.getenv("MYSQL_DATABASE", "appdb"),
    charset="utf8mb4",
    connect_timeout=10,
)
cur = conn.cursor()
cur.execute(sql)
rows = cur.fetchall()
if raw:
    for r in rows:
        print(r)
else:
    cols = [d[0] for d in cur.description]
    print(" | ".join(cols))
    for r in rows:
        print(" | ".join("" if v is None else str(v) for v in r))
cur.close()
conn.close()
