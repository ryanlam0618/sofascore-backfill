#!/usr/bin/env python3
"""gen4_phaseB_19_20_prep.py — Seed 19/20 season rows + snapshot row-count baseline.

SAFE: only INSERT IGNORE into `seasons` (idempotent metadata) + read row counts.
No DROP / DELETE. Needed because matches.season_id has FK -> seasons.season_id.

Handles all 24 comps from competitions_10y.yaml. UECL (ut_id 17015, Europa Conference
League) has NO 19/20 season (founded 2021, earliest 21/22) -> season_id None -> skipped
for seeding; still included in the scope-guard baseline (its existing seasons must not drop).

Baseline scope guard: for every comp x table we record {season_id: rowcount} for ALL
seasons (15/16 .. 25/26). Post-run, each count must be >= baseline (19/20 may only grow).

Writes:
  - data/gen4_phaseB_19-20_baseline_counts.json  ({comp: {table: {season_id: count}}})
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import mysql.connector
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

SEASON_IDS = ROOT / "data/gen4_phaseB_19-20_season_ids.json"
BASELINE = ROOT / "data/gen4_phaseB_19-20_baseline_counts.json"

# All 24 comps from competitions_10y.yaml (source of truth)
COMPETITION_IDS = {
    17: "Premier League", 8: "La Liga", 23: "Serie A", 35: "Bundesliga", 34: "Ligue 1",
    196: "J1 League", 410: "K League 1", 136: "A-League Men", 649: "Chinese Super League",
    7: "UCL", 679: "UEL", 17015: "UECL", 463: "AFC Champions League", 668: "AFC Champions League Two",
    19: "FA Cup", 21: "EFL Cup", 329: "Copa del Rey", 328: "Coppa Italia", 335: "Coupe de France",
    217: "DFB Pokal", 101: "J.League Cup", 323: "Emperor's Cup", 1786: "Australia Cup",
    882: "Chinese FA Cup",
}

TABLES = ["matches", "match_incidents", "match_lineups", "match_statistics",
          "match_shotmap", "match_odds"]


def get_conn():
    return mysql.connector.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "root"),
        password=os.getenv("MYSQL_PASSWORD", ""),
        database=os.getenv("MYSQL_DATABASE", "appdb"),
        charset="utf8mb4", connect_timeout=10,
    )


def seed_seasons(conn, resolved_comp_ids: dict) -> dict:
    """Insert 19/20 season rows idempotently. Skips comps unresolved (UECL)."""
    cur = conn.cursor()
    outcomes = {}
    for comp in resolved_comp_ids:
        name = comp["name"]
        sid = comp["season_id"]
        ut = comp["ut_id"]
        if sid is None:
            outcomes[name] = "skipped-no-19/20-season"
            print("  ∼ %-24s no 19/20 season (skip seed)" % name)
            continue
        cur.execute("SELECT 1 FROM competitions WHERE competition_id=%s", (ut,))
        if not cur.fetchall():
            outcomes[name] = "missing-competition"
            print("  ! %-24s competition_id=%s NOT in competitions (skip)" % (name, ut))
            continue
        cur.execute("""
            INSERT INTO seasons (season_id, competition_id, year_label, year_start, year_end, start_date, end_date, is_current)
            VALUES (%s, %s, %s, %s, %s, NULL, NULL, 0)
            ON DUPLICATE KEY UPDATE year_label=VALUES(year_label),
                year_start=VALUES(year_start), year_end=VALUES(year_end)
        """, (sid, ut, "19/20", 2019, 2020))
        outcomes[name] = "ok"
        print("  ✓ %-24s season_id=%s" % (name, sid))
    conn.commit()
    cur.close()
    return outcomes


def snapshot_counts(conn) -> dict:
    """Per comp x table: {season_id: rowcount} for all seasons (scope-guard baseline).

    matches has season_id directly; child tables join through matches.
    Could be slow (~24 comps x 6 tables) but one-off.
    """
    cur = conn.cursor()
    result = {}
    for ut in COMPETITION_IDS:
        result[str(ut)] = {}
        for table in TABLES:
            if table == "matches":
                sql = (f"SELECT m.season_id, COUNT(*) FROM {table} m "
                       f"JOIN seasons s ON m.season_id=s.season_id "
                       f"WHERE s.competition_id=%s GROUP BY m.season_id")
            else:
                sql = (f"SELECT m.season_id, COUNT(*) FROM {table} t "
                       f"JOIN matches m ON t.match_id=m.match_id "
                       f"JOIN seasons s ON m.season_id=s.season_id "
                       f"WHERE s.competition_id=%s GROUP BY m.season_id")
            try:
                cur.execute(sql, (ut,))
                result[str(ut)][table] = {int(r[0]): int(r[1]) for r in cur.fetchall()}
            except Exception as e:
                print("    [snapshot] %s %s err: %s" % (table, ut, e))
                result[str(ut)][table] = {}
    cur.close()
    return result


def main():
    data = json.loads(SEASON_IDS.read_text())
    comps = data["comps"]

    conn = get_conn()
    print("Seeding 19/20 seasons table rows...")
    outcomes = seed_seasons(conn, comps)

    print("\nSnapshot baseline (all seasons, per comp x table)...")
    counts = snapshot_counts(conn)

    payload = {
        "timestamp": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "season_label": "19/20",
        "seed_outcomes": outcomes,
        "baseline": counts,
    }
    BASELINE.write_text(json.dumps(payload, ensure_ascii=False, indent=2))

    # Summary: per comp total matches across all seasons
    print("\n=== per-comp matches across all seasons (pre-run) ===")
    for ut in COMPETITION_IDS:
        tot = sum(counts[str(ut)]["matches"].values())
        print("  %-24s total_matches=%d seasons=%s" % (
            COMPETITION_IDS[ut], tot, sorted(counts[str(ut)]["matches"].keys())))
    conn.close()
    print("\n✓ Wrote %s" % BASELINE)


if __name__ == "__main__":
    main()