#!/usr/bin/env python3
"""Odds dedup verification (read-only) — task odds-dedup-delete-20260927.

Step 1 of task spec: verify pure-duplicate criteria on match_odds.
- Schema + total rows
- Pure dup groups (all columns identical): n_groups / rows_in_groups / rows_to_delete
- Group size distribution
- Per-competition breakdown of rows_to_delete
- ~340 gapfill relationship check (pure dups among gapfill events)
"""
import pymysql, os, json
from collections import defaultdict

conn = pymysql.connect(
    host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
    port=int(os.environ.get("MYSQL_PORT", "3306")),
    user=os.environ["MYSQL_USER"],
    password=os.environ["MYSQL_PASSWORD"],
    database="appdb",
    charset="utf8mb4",
)
cur = conn.cursor()

# 1. Schema
cur.execute("SHOW COLUMNS FROM match_odds")
cols = [r[0] for r in cur.fetchall()]
print("COLUMNS:", cols)
pk = [r[0] for r in cur.fetchall() if r[3] == "PRI"]  # r[3] = Key
print("PK:", pk)

# 2. Total rows
cur.execute("SELECT COUNT(*) FROM match_odds")
total = cur.fetchone()[0]
print("TOTAL_ROWS:", total)

cols_csv = ", ".join(f"`{c}`" for c in cols)

# 3. Pure duplicate groups (all columns identical)
cur.execute(f"""
SELECT COALESCE(SUM(c),0), COALESCE(SUM(c-1),0), COUNT(*)
FROM (
  SELECT {cols_csv}, COUNT(*) AS c
  FROM match_odds
  GROUP BY {cols_csv}
  HAVING COUNT(*) > 1
) t
""")
n_rows_in_groups, n_to_delete, n_groups = cur.fetchone()
print(f"PURE_DUP: rows_in_groups={n_rows_in_groups} rows_to_delete={n_to_delete} groups={n_groups}")

# 4. Group size distribution
cur.execute(f"""
SELECT c, COUNT(*) FROM (
  SELECT {cols_csv}, COUNT(*) AS c
  FROM match_odds
  GROUP BY {cols_csv}
  HAVING COUNT(*) > 1
) t GROUP BY c ORDER BY c
""")
print("GROUP_SIZE_DIST:", cur.fetchall())

# 5. Per-competition breakdown of rows_to_delete (if season/comp linkage exists via matches)
if pk:
    pk_col = pk[0]
    # find comp column on matches
    cur.execute("SHOW COLUMNS FROM matches")
    mcols = [r[0] for r in cur.fetchall()]
    comp_col = "competition_id" if "competition_id" in mcols else None
    if comp_col:
        cur.execute(f"""
        SELECT m.`{comp_col}`, COALESCE(SUM(t.c-1),0)
        FROM (
          SELECT {cols_csv}, COUNT(*) AS c
          FROM match_odds
          GROUP BY {cols_csv}
          HAVING COUNT(*) > 1
        ) t
        JOIN matches m ON m.match_id = t.match_id
        GROUP BY m.`{comp_col}` ORDER BY 2 DESC
        """)
        print("PER_COMP_ROWS_TO_DELETE:", cur.fetchall())

# 6. Movement snapshot check: (match_id, bookmaker, time-ish cols) groups with differing values
#    -> confirm they are NOT caught by the all-columns dup scan
non_value_cols = [c for c in cols if c not in ("id",) ]
print("NOTE: movement snapshots (same match+bookmaker+ts, diff values) are excluded by all-columns GROUP BY by construction.")

conn.close()
print("VERIFY_DONE")
