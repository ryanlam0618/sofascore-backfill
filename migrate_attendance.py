#!/usr/bin/env python3
"""
Migrate attendance data from sofascore_attendance_fetch_log to sofascore_attendance.
Reads from fetch_log + incident table metadata, writes to attendance table.
"""
from __future__ import annotations
import sys, time
sys.path.insert(0, "/root/.openclaw/workspace/sofascore_backfill")
from fetch_incidents import mysql_connect

def now_mysql():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def main():
    conn = mysql_connect()
    cur = conn.cursor()

    # Count before
    cur.execute("SELECT COUNT(*) FROM sofascore_attendance")
    before = cur.fetchone()[0]
    print(f"Before migration: {before} rows")

    # Get all fetch_log entries with valid attendance that aren't yet in attendance table
    cur.execute("""
        SELECT fl.event_id, fl.status_code, fl.attendance_int, fl.fetched_at,
               COALESCE(ie.match_date, '') as match_date,
               COALESCE(ie.league, '') as league,
               COALESCE(ie.home_team, '') as home_team,
               COALESCE(ie.away_team, '') as away_team
        FROM sofascore_attendance_fetch_log fl
        LEFT JOIN sofascore_incident_events ie ON fl.event_id = ie.event_id
        WHERE fl.status_code = 200
          AND fl.attendance_int IS NOT NULL
          AND fl.attendance_int > 0
          AND NOT EXISTS (
              SELECT 1 FROM sofascore_attendance a
              WHERE a.event_id = fl.event_id
          )
        ORDER BY fl.event_id
    """)
    rows = cur.fetchall()
    total = len(rows)
    print(f"Migration target: {total} rows")

    if total == 0:
        print("[INFO] Nothing to migrate")
        conn.close()
        return

    inserted = 0
    skipped = 0
    fetched_at = now_mysql()

    for row in rows:
        event_id, status_code, attendance_int, log_fetched_at, match_date, league, home_team, away_team = row

        try:
            cur.execute("""
                INSERT INTO sofascore_attendance
                (event_id, match_date, league, home_team, away_team,
                 status_code, attendance, fetched_at, error)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (event_id, match_date, league, home_team, away_team,
                  status_code, attendance_int, fetched_at, ""))
            inserted += 1
        except Exception as e:
            skipped += 1
            print(f"  [WARN] event_id={event_id}: {e}")

        if inserted % 500 == 0:
            conn.commit()
            print(f"  [PROGRESS] inserted={inserted}/{total}")

    conn.commit()

    # Count after
    cur.execute("SELECT COUNT(*) FROM sofascore_attendance")
    after = cur.fetchone()[0]
    print(f"\n[DONE] inserted={inserted}, skipped={skipped}")
    print(f"Attendance table: {before} → {after} rows")

    conn.close()

if __name__ == "__main__":
    main()