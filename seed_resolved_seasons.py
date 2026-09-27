#!/usr/bin/env python3
"""
seed_resolved_seasons.py — seed the 24 resolved cup-season rows (5 cups x 20/21-24/25)
from data/season_resolve_retry_20260925.json into appdb.seasons.

Kris 2026-09-25 09:36 GMT+8 "試多次" retry deliverable. Additive only
(ON DUPLICATE KEY UPDATE, no DELETE) — same logic as production
ensure_season_row in gen4_phaseB_2021-25_backfill.py.
Australia Cup 20/21 skipped (SofaScore has no 2020 season — genuine absence).
"""
import json
import os
from pathlib import Path

import pymysql

ROOT = Path(__file__).resolve().parent


def main() -> None:
    d = json.load(open(ROOT / "data" / "season_resolve_retry_20260925.json"))

    conn = pymysql.connect(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ["MYSQL_USER"],
        password=os.environ["MYSQL_PASSWORD"],
        database=os.environ.get("MYSQL_DATABASE", "appdb"),
        charset="utf8mb4",
    )
    cur = conn.cursor()

    seeded = 0
    skipped = []
    for t in d["targets"]:
        ut_id, name = t["ut_id"], t["name"]
        cur.execute("SELECT 1 FROM competitions WHERE competition_id=%s", (ut_id,))
        if not cur.fetchall():
            skipped.append("%s: missing-competition" % name)
            continue
        for label, v in sorted(t["resolved"].items()):
            sid = v["season_id"]
            if not sid:
                continue
            ystart = int(label[:2]) + 2000
            cur.execute(
                """
                INSERT INTO seasons
                    (season_id, competition_id, year_label, year_start, year_end,
                     start_date, end_date, is_current)
                VALUES (%s, %s, %s, %s, %s, NULL, NULL, 0)
                ON DUPLICATE KEY UPDATE year_label=VALUES(year_label),
                    year_start=VALUES(year_start), year_end=VALUES(year_end)
                """,
                (sid, ut_id, label, ystart, ystart + 1),
            )
            seeded += 1
    conn.commit()
    print("seeded rows: %d (idempotent upsert, additive only)" % seeded)
    for s in skipped:
        print("skipped: %s" % s)

    print("")
    print("== VERIFY: seasons rows for 5 cups ==")
    for ut, name in [(217, "DFB Pokal"), (101, "J.League Cup"), (323, "Emperors Cup"),
                     (1786, "Australia Cup"), (882, "Chinese FA Cup")]:
        cur.execute(
            "SELECT year_label, season_id FROM seasons WHERE competition_id=%s ORDER BY year_label",
            (ut,))
        rows = cur.fetchall()
        labels = [r[0] for r in rows]
        print("%s (%d rows): %s" % (name, len(rows), ", ".join("%s=%s" % r for r in rows)))
        missing = [l for l in ["20/21", "21/22", "22/23", "23/24", "24/25"] if l not in labels]
        if missing:
            print("  still missing: %s" % ", ".join(missing))

    conn.close()


if __name__ == "__main__":
    main()
