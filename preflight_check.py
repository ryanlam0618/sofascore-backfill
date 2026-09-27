#!/usr/bin/env python3
"""Stage 4c Pre-flight checks - schema verification and endpoint spot-check."""

import mysql.connector
from pathlib import Path

ROOT = Path(__file__).resolve().parent
env = dict(l.split("=", 1) for l in (ROOT / ".env").read_text().splitlines() if "=" in l and not l.startswith("#"))

conn = mysql.connector.connect(
    host="127.0.0.1", port=3306, user="appdb_rw",
    password=env["MYSQL_PASSWORD"].strip(),
    database="appdb", autocommit=True
)
cur = conn.cursor()

print("=" * 60)
print("STAGE 4C PRE-FLIGHT CHECKS")
print("=" * 60)

# 1. match_odds schema - check for market_id column
print("\n1. match_odds schema:")
cur.execute("SHOW CREATE TABLE match_odds")
for row in cur.fetchall():
    print(row[1])

cur.execute("SHOW COLUMNS FROM match_odds LIKE 'market_id'")
result = cur.fetchone()
print(f"market_id column exists: {result is not None}")
if result is None:
    print("  -> MISSING: market_id column (schema bug confirmed)")

# 2. match_player_stats column count
cur.execute("SELECT COUNT(*) FROM information_schema.columns WHERE table_name = 'match_player_stats'")
print(f"\n2. match_player_stats columns: {cur.fetchone()[0]}")

# 3. Check existing tables row counts
print("\n3. Current table row counts:")
tables = ['match_h2h', 'match_votes', 'match_momentum', 'match_odds', 
          'match_average_positions', 'match_best_players', 'match_player_stats',
          'match_shotmap', 'match_lineups', 'match_statistics', 'match_incidents']
for t in tables:
    cur.execute(f"SELECT COUNT(*) FROM {t}")
    count = cur.fetchone()[0]
    print(f"  {t}: {count} rows")

# 4. Check target events for Batch A (low coverage comps)
print("\n4. Batch A target comps - events missing endpoints:")
batch_a_comps = [335, 17015, 19, 329, 882]  # 5 worst
# Also include other 10 from Stage 4b with small gaps
batch_a_other = [34, 679, 668, 649, 463, 410, 217, 21, 136, 328]
all_batch_a = batch_a_comps + batch_a_other

placeholders = ",".join(["%s"] * len(all_batch_a))
cur.execute(f"""
    SELECT c.competition_id, c.name,
           COUNT(*) as total_matches,
           SUM(ms.match_id IS NULL) as shotmap_missing,
           SUM(ml.match_id IS NULL) as lineups_missing,
           SUM(mst.match_id IS NULL) as stats_missing,
           SUM(mi.match_id IS NULL) as incidents_missing
    FROM matches m
    JOIN seasons s ON m.season_id = s.season_id
    JOIN competitions c ON s.competition_id = c.competition_id
    LEFT JOIN match_shotmap ms ON ms.match_id = m.match_id
    LEFT JOIN match_lineups ml ON ml.match_id = m.match_id
    LEFT JOIN match_statistics mst ON mst.match_id = m.match_id
    LEFT JOIN match_incidents mi ON mi.match_id = m.match_id
    WHERE s.year_label = '25/26'
      AND c.competition_id IN ({placeholders})
      AND m.status = 'finished'
      AND m.home_score IS NOT NULL
    GROUP BY c.competition_id, c.name
    ORDER BY c.competition_id
""", tuple(all_batch_a))
print(f"  {'comp_id':>7} {'name':<25} {'total':>5} {'shotmap':>7} {'lineups':>7} {'stats':>7} {'incidents':>9}")
for row in cur.fetchall():
    print(f"  {row[0]:>7} {row[1]:<25} {row[2]:>5} {row[3]:>7} {row[4]:>7} {row[5]:>7} {row[6]:>9}")

# 5. Batch B zero comps shotmap missing
print("\n5. Batch B zero comps shotmap missing:")
zero_comps = [7, 8, 17, 23, 35, 101, 196, 323, 1786]
placeholders = ",".join(["%s"] * len(zero_comps))
cur.execute(f"""
    SELECT c.competition_id, c.name, COUNT(*) as cnt
    FROM matches m
    JOIN seasons s ON m.season_id = s.season_id
    JOIN competitions c ON s.competition_id = c.competition_id
    LEFT JOIN match_shotmap ms ON ms.match_id = m.match_id
    WHERE s.year_label = '25/26'
      AND c.competition_id IN ({placeholders})
      AND m.status = 'finished'
      AND m.home_score IS NOT NULL
      AND ms.match_id IS NULL
    GROUP BY c.competition_id, c.name
    ORDER BY c.competition_id
""", tuple(zero_comps))
total_b = 0
for row in cur.fetchall():
    print(f"  comp_id={row[0]} name={row[1]:<20} events={row[2]}")
    total_b += row[2]
print(f"  Total Batch B events: {total_b}")

# 6. Total events for Batch C (all 25/26 finished matches)
print("\n6. Total 25/26 finished events (Batch C scope):")
cur.execute("""
    SELECT COUNT(*) FROM matches m
    JOIN seasons s ON m.season_id = s.season_id
    WHERE s.year_label = '25/26'
      AND m.status = 'finished'
      AND m.home_score IS NOT NULL
""")
total_c = cur.fetchone()[0]
print(f"  Total: {total_c} events x 7 endpoints = {total_c * 7} calls")

conn.close()
print("\n" + "=" * 60)
print("PRE-FLIGHT COMPLETE")
print("=" * 60)