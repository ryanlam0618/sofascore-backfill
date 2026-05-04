#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
smoke_test_mysql.py — Smoke-test script to verify MySQL write capability.
1. Create all sofascore_* tables
2. Insert 1 row per table (real-ish SofaScore data pattern)
3. Verify rows exist with SELECT

Usage:
    python smoke_test_mysql.py            # no-op dry-run
    USE_MYSQL=1 python smoke_test_mysql.py # actual write
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

# Allow running from any directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mysql_helpers import mysql_connect, ensure_mysql_tables, use_mysql, SQL_SCHEMA


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def run():
    print("=" * 60)
    print("SofaScore Backfill — MySQL Smoke Test")
    print("=" * 60)

    if not use_mysql():
        print("\n[SKIP] USE_MYSQL=1 not set. Dry-run only (tables won't be created).\n")
        print("  To run actual test: USE_MYSQL=1 python smoke_test_mysql.py")
        print("  Schema that would be created:")
        for line in SQL_SCHEMA.strip().splitlines():
            print(f"    {line}")
        return

    conn = mysql_connect()
    cur = conn.cursor(dictionary=True)

    # ── Step 1: Create tables ─────────────────────────────────
    print("\n[STEP 1] Creating MySQL tables ...")
    groups = ensure_mysql_tables(conn)

    all_tables = []
    for tables in groups.values():
        all_tables.extend(tables)

    cur.execute("SHOW TABLES")
    existing = [r["Tables_in_appdb"] for r in cur.fetchall()]
    found = [t for t in all_tables if t in existing]
    print(f"  Tables created/verified: {found}")
    print(f"  Missing tables: {[t for t in all_tables if t not in existing]}")

    # ── Step 2: Insert smoke data ─────────────────────────────
    print("\n[STEP 2] Inserting smoke-test rows ...")

    ts = now_iso()
    results = []

    # Standings
    cur.execute("""
        INSERT INTO sofascore_standings
        (category_id, tournament_name, season_id, standing_type, position,
         team_id, team_name, team_short_name, played, wins, draws, losses,
         goals_for, goals_against, goal_diff, points, last_5, streak, fetched_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE fetched_at=VALUES(fetched_at)
    """, (
        1, "Premier League", 76986, "total", 1,
        1675, "Manchester City", "Man City", 38, 28, 5, 5,
        94, 33, 61, 89, '["W","W","D","W","W"]', "WWWWW", ts
    ))
    results.append(("sofascore_standings", cur.rowcount))

    cur.execute("""
        INSERT INTO sofascore_standings_fetch_log
        (category_id, season_id, standing_type, tournament_name, fetched_at, status_code, row_count, error)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE fetched_at=VALUES(fetched_at)
    """, (1, 76986, "total", "Premier League", ts, 200, 20, ""))

    # Player stats
    cur.execute("""
        INSERT INTO sofascore_player_season_stats
        (category_id, ut_id, season_id, stat_type, `rank`, player_id, player_name,
         player_position, team_id, team_name, goals, assists, appearances,
         minutes_played, xg, xa, yellow_cards, red_cards, fetched_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE fetched_at=VALUES(fetched_at)
    """, (
        1, 17, 76986, "goals", 1, 1234567, "Erling Haaland",
        "Forward", 1675, "Manchester City", 36, 8, 31,
        2700, 32.5, 6.2, 5, 0, ts
    ))

    # Related matches (H2H)
    cur.execute("""
        INSERT INTO sofascore_related_matches
        (source_event_id, home_team_id, home_team_name, away_team_id, away_team_name,
         league_category_id, league_name, match_timestamp, home_wins, draws, away_wins, total_h2h, fetched_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE fetched_at=VALUES(fetched_at)
    """, (
        99988877, 1675, "Manchester City", 1672, "Liverpool",
        1, "Premier League", 1704067200, 45, 32, 38, 115, ts
    ))

    # Incidents
    cur.execute("""
        INSERT INTO sofascore_incident_events
        (event_id, status_code, incident_count, fetched_at, error)
        VALUES (%s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE fetched_at=VALUES(fetched_at)
    """, (99988877, 200, 3, ts, ""))

    cur.execute("""
        INSERT INTO sofascore_incidents
        (event_id, incident_id, incident_type, minute, team_id, player_id, player_name,
         is_home_incident, fetched_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE fetched_at=VALUES(fetched_at)
    """, (99988877, 1, "goal", 23, 1675, 1234567, "Erling Haaland", 1, ts))

    # Lineups
    cur.execute("""
        INSERT INTO sofascore_lineup_events
        (event_id, status_code, home_count, away_count, confirmed, fetched_at, error)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE fetched_at=VALUES(fetched_at)
    """, (99988877, 200, 11, 11, 1, ts, ""))

    cur.execute("""
        INSERT INTO sofascore_lineups
        (event_id, is_home, team_id, team_name, player_id, player_name,
         position, position_type, jersey_number, captain, player_key, fetched_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE fetched_at=VALUES(fetched_at)
    """, (99988877, 1, 1675, "Manchester City", 1234567, "Erling Haaland",
          "Forward", "attacker", 9, 0, "1234567", ts))

    # Shotmap xG
    cur.execute("""
        INSERT INTO sofascore_shotmap_xg_backfill
        (event_id, status_code, has_shotmap, has_xg, shot_count,
         home_shotmap_xg, away_shotmap_xg, fetched_at, error)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE fetched_at=VALUES(fetched_at)
    """, (99988877, 200, 1, 1, 28, 2.34, 1.12, ts, ""))

    # Shotmap Details
    cur.execute("""
        INSERT INTO sofascore_shotmap_detail_events
        (event_id, status_code, shot_count, fetched_at, error)
        VALUES (%s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE fetched_at=VALUES(fetched_at)
    """, (99988877, 200, 28, ts, ""))

    cur.execute("""
        INSERT INTO sofascore_shotmap_details
        (event_id, shot_id, is_home_shot, team_id, player_id, player_name,
         minute, xg, player_x, player_y, fetched_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE fetched_at=VALUES(fetched_at)
    """, (99988877, 1, 1, 1675, 1234567, "Erling Haaland", 23, 0.87, 0.82, 0.45, ts))

    conn.commit()
    print("  Smoke rows inserted.")

    # ── Step 3: Verify rows exist ────────────────────────────
    print("\n[STEP 3] Verifying rows ...")
    verified = []
    failed = []

    checks = [
        ("sofascore_standings", "SELECT COUNT(*) AS cnt FROM sofascore_standings WHERE team_name='Manchester City'"),
        ("sofascore_player_season_stats", "SELECT COUNT(*) AS cnt FROM sofascore_player_season_stats WHERE player_name='Erling Haaland'"),
        ("sofascore_related_matches", "SELECT COUNT(*) AS cnt FROM sofascore_related_matches WHERE source_event_id=99988877"),
        ("sofascore_incident_events", "SELECT COUNT(*) AS cnt FROM sofascore_incident_events WHERE event_id=99988877"),
        ("sofascore_incidents", "SELECT COUNT(*) AS cnt FROM sofascore_incidents WHERE event_id=99988877 AND incident_type='goal'"),
        ("sofascore_lineup_events", "SELECT COUNT(*) AS cnt FROM sofascore_lineup_events WHERE event_id=99988877"),
        ("sofascore_lineups", "SELECT COUNT(*) AS cnt FROM sofascore_lineups WHERE event_id=99988877 AND player_name='Erling Haaland'"),
        ("sofascore_shotmap_xg_backfill", "SELECT COUNT(*) AS cnt FROM sofascore_shotmap_xg_backfill WHERE event_id=99988877"),
        ("sofascore_shotmap_detail_events", "SELECT COUNT(*) AS cnt FROM sofascore_shotmap_detail_events WHERE event_id=99988877"),
        ("sofascore_shotmap_details", "SELECT COUNT(*) AS cnt FROM sofascore_shotmap_details WHERE event_id=99988877 AND shot_id=1"),
    ]

    for table, sql in checks:
        try:
            cur.execute(sql)
            cnt = cur.fetchone()["cnt"]
            if cnt > 0:
                verified.append(table)
                print(f"  ✅ {table}: {cnt} row(s)")
            else:
                failed.append(table)
                print(f"  ❌ {table}: 0 rows (MISSING)")
        except Exception as e:
            failed.append(table)
            print(f"  ❌ {table}: {e}")

    cur.close()

    print("\n" + "=" * 60)
    print(f"RESULT: {len(verified)}/{len(checks)} tables verified")
    print("=" * 60)

    if failed:
        print(f"FAILED tables: {failed}")
        sys.exit(1)
    else:
        print("All smoke tests PASSED ✅")
        sys.exit(0)


if __name__ == "__main__":
    run()