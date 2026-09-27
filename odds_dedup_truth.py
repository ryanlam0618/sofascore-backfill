#!/usr/bin/env python3
"""Odds dedup ground-truth diagnostic (read-only) — reconcile 3,083 / 3,082 / 3,098 / 3,259.

Criteria D1: GROUP BY all 17 cols EXCEPT odds_id AND fetched_at
  -> rows identical except row-ID and fetch time (true redundant)
Criteria D2: GROUP BY all 18 cols EXCEPT odds_id (fetched_at included)
  -> rows identical except row-ID only (same fetch batch duplicates)
"""
import pymysql, os

conn = pymysql.connect(
    host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
    port=int(os.environ.get("MYSQL_PORT", "3306")),
    user=os.environ["MYSQL_USER"],
    password=os.environ["MYSQL_PASSWORD"],
    database="appdb",
    charset="utf8mb4",
)
cur = conn.cursor()

COLS_17 = ("`match_id`, `market_id`, `market_name`, `market_group`, `market_period`, "
           "`structure_type`, `suspended`, `choice_name`, `initial_fractional_value`, "
           "`fractional_value`, `winning`, `bookmaker_id`, `bookmaker_name`, `odds_type`, "
           "`home_odds`, `draw_odds`, `away_odds`")

# D2: all except odds_id (fetched_at included)
cur.execute(f"""
SELECT COUNT(*), COALESCE(SUM(c),0), COALESCE(SUM(c-1),0)
FROM (
  SELECT {COLS_17}, `fetched_at`, COUNT(*) AS c
  FROM match_odds
  GROUP BY {COLS_17}, `fetched_at`
  HAVING COUNT(*) > 1
) t
""")
d2_groups, d2_rows, d2_delete = cur.fetchone()
print(f"D2 (all-except-odds_id, fetched_at incl): groups={d2_groups} rows={d2_rows} rows_to_delete={d2_delete}")

# D1: all except odds_id AND fetched_at
cur.execute(f"""
SELECT COUNT(*), COALESCE(SUM(c),0), COALESCE(SUM(c-1),0)
FROM (
  SELECT {COLS_17}, COUNT(*) AS c
  FROM match_odds
  GROUP BY {COLS_17}
  HAVING COUNT(*) > 1
) t
""")
d1_groups, d1_rows, d1_delete = cur.fetchone()
print(f"D1 (all-except-odds_id-and-fetched_at):   groups={d1_groups} rows={d1_rows} rows_to_delete={d1_delete}")

# per-comp breakdown for D1
cur.execute(f"""
SELECT m.`competition_id`, COALESCE(SUM(t.c-1),0)
FROM (
  SELECT `match_id`, {COLS_17}, COUNT(*) AS c
  FROM match_odds
  GROUP BY `match_id`, {COLS_17}
  HAVING COUNT(*) > 1
) t
JOIN matches m ON m.match_id = t.match_id
GROUP BY m.`competition_id` ORDER BY 2 DESC
""")
print("D1 per-comp rows_to_delete:", cur.fetchall())

conn.close()
print("DIAG_DONE")
