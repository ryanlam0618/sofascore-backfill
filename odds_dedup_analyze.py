#!/usr/bin/env python3
"""Odds dedup refined analysis (read-only) — reconcile 3,083 / 12,221 / 86,270.

Criteria A: GROUP BY all columns EXCEPT fetched_at
  -> rows identical except fetch time
Criteria B: GROUP BY logical key (match, market, bookmaker, choice, odds_type, period, structure)
  -> split by value-variants: never-moved (nv=1, redundant) vs moved (nv>1, real movement)
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

VALUE_EXPR = ("CONCAT_WS('|', `initial_fractional_value`, `fractional_value`, `home_odds`, "
              "`draw_odds`, `away_odds`, `suspended`, `winning`, `market_name`, `market_group`)")
LOGICAL_KEY = "`match_id`, `market_id`, `bookmaker_id`, `choice_name`, `odds_type`, `market_period`, `structure_type`"

# ---- Criteria A: all columns except fetched_at ----
cur.execute(f"""
SELECT COUNT(*), COALESCE(SUM(c),0), COALESCE(SUM(c-1),0)
FROM (
  SELECT {LOGICAL_KEY}, {VALUE_EXPR} AS v, COUNT(*) AS c
  FROM match_odds
  GROUP BY {LOGICAL_KEY}, v
  HAVING COUNT(*) > 1
) t
""")
a_groups, a_rows, a_delete = cur.fetchone()
print(f"CRITERIA_A (all-except-fetched_at): groups={a_groups} rows_in_groups={a_rows} rows_to_delete={a_delete}")

# ---- Criteria B: logical key, split never-moved vs moved ----
cur.execute(f"""
SELECT {LOGICAL_KEY}, COUNT(*) AS c,
       COUNT(DISTINCT {VALUE_EXPR}) AS nv
FROM match_odds
GROUP BY {LOGICAL_KEY}
HAVING COUNT(*) > 1
""")
rows = cur.fetchall()
nm_groups = sum(1 for r in rows if r[8] == 1)
nm_rows = sum(r[7] for r in rows if r[8] == 1)
nm_delete = sum(r[7] - 1 for r in rows if r[8] == 1)
mv_groups = sum(1 for r in rows if r[8] > 1)
mv_rows = sum(r[7] for r in rows if r[8] > 1)
print(f"CRITERIA_B never-moved (nv=1): groups={nm_groups} rows={nm_rows} rows_to_delete={nm_delete}")
print(f"CRITERIA_B moved (nv>1):      groups={mv_groups} rows={mv_rows}")
print(f"CRITERIA_B total multi-fetch: groups={len(rows)} rows={nm_rows + mv_rows}")

# ---- Per-competition breakdown of criteria-A rows_to_delete ----
cur.execute(f"""
SELECT m.`competition_id`, COALESCE(SUM(t.c-1),0)
FROM (
  SELECT `match_id`, {LOGICAL_KEY}, {VALUE_EXPR} AS v, COUNT(*) AS c
  FROM match_odds
  GROUP BY `match_id`, {LOGICAL_KEY}, v
  HAVING COUNT(*) > 1
) t
JOIN matches m ON m.match_id = t.match_id
GROUP BY m.`competition_id` ORDER BY 2 DESC
""")
print("CRITERIA_A per-comp rows_to_delete:", cur.fetchall())

conn.close()
print("ANALYSIS_DONE")
