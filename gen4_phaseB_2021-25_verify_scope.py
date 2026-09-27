#!/usr/bin/env python3
"""gen4_phaseB_2021-25_verify_scope.py — Verify scope guard + 2021-25 growth + match-row coverage.

Compares current DB counts against the pre-run baseline (per comp x table x season_id).
Any season_id count that DROPS below baseline is a regression (violation).
2021-25 target seasons may only grow (that is the backfill).

Also reads the backfill report and checks per comp-season:
  * events_failed == 0 AND events_processed + events_failed == total_events_found
    -> all attempted events have match rows
  * skipped (season-not-resolved / missing-competition) listed as known gaps

Reads baseline from data/gen4_phaseB_2021-25_baseline_counts.json.
Writes data/gen4_phaseB_2021-25_scope_verify.json.

Usage: .runner-venv/bin/python gen4_phaseB_2021-25_verify_scope.py
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import mysql.connector
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

BASELINE_FILE = ROOT / "data/gen4_phaseB_2021-25_baseline_counts.json"
REPORT_FILE = ROOT / "data/gen4_phaseB_2021-25_backfill_report.json"
OUT = ROOT / "data/gen4_phaseB_2021-25_scope_verify.json"

TABLES = ["matches", "match_incidents", "match_lineups", "match_statistics",
          "match_shotmap", "match_odds"]

COMPETITION_IDS = {
    17: "Premier League", 8: "La Liga", 23: "Serie A", 35: "Bundesliga",
    34: "Ligue 1", 196: "J1 League", 410: "K League 1", 136: "A-League Men",
    649: "Chinese Super League", 7: "UCL", 679: "UEL", 17015: "UECL",
    463: "AFC Champions League", 668: "AFC Champions League Two",
    19: "FA Cup", 21: "EFL Cup", 329: "Copa del Rey", 328: "Coppa Italia",
    335: "Coupe de France", 217: "DFB Pokal", 101: "J.League Cup",
    323: "Emperor's Cup", 1786: "Australia Cup", 882: "Chinese FA Cup",
}


def get_conn():
    return mysql.connector.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "root"),
        password=os.getenv('MYSQL_PASSWORD', ''),
        database=os.getenv("MYSQL_DATABASE", "appdb"),
        charset="utf8mb4",
        connect_timeout=10,
    )


def main():
    base = json.loads(BASELINE_FILE.read_text())["baseline"]
    conn = get_conn()
    cur = conn.cursor()

    regressions = []        # drops below baseline (violation)
    new_season_rows = {}    # grown rows only

    for ut, name in COMPETITION_IDS.items():
        for table in TABLES:
            # current counts per season_id for this comp/table
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
                now = {int(r[0]): int(r[1]) for r in cur.fetchall()}
            except Exception:
                now = {}
            # Normalize: baseline JSON stores season_id as str keys; live query returns ints.
            bl = {int(k): int(v) for k, v in base.get(str(ut), {}).get(table, {}).items()}

            for sid, cnt in bl.items():
                n = now.get(sid, 0)
                if n < cnt:
                    regressions.append({"comp": name, "table": table,
                                        "season_id": sid, "baseline": cnt, "now": n})
            for sid, cnt in now.items():
                if sid not in bl or cnt > bl[sid]:
                    new_season_rows.setdefault(name, {}).setdefault(table, {})[sid] = cnt

    conn.close()

    # Match-row coverage from the backfill report
    all_match_rows_ok = None
    comp_season_gaps = []
    try:
        report = json.loads(REPORT_FILE.read_text())
        all_match_rows_ok = True
        for comp in report["comps"]:
            if comp.get("error"):
                comp_season_gaps.append("%s %s: %s" % (comp["comp_name"], comp["season_label"], comp["error"]))
                continue
            attempted = comp.get("events_processed", 0) + comp.get("events_failed", 0)
            if comp.get("events_failed", 0) > 0 or attempted < comp.get("total_events_found", 0):
                all_match_rows_ok = False
                comp_season_gaps.append(
                    "%s %s: failed=%s attempted=%s found=%s consec_403=%s" % (
                        comp["comp_name"], comp["season_label"], comp.get("events_failed"),
                        attempted, comp.get("total_events_found"), comp.get("consec_403_hit")))
    except Exception as e:
        comp_season_gaps.append("report read failed: %s" % e)

    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "scope_guard_ok": len(regressions) == 0,
        "regressions": regressions,
        "grown_rows": new_season_rows,
        "all_match_rows_ok": all_match_rows_ok,
        "comp_season_gaps": comp_season_gaps,
    }
    OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2))

    print("scope_guard_ok:", summary["scope_guard_ok"])
    if regressions:
        print("REGRESSIONS:")
        for r in regressions:
            print("  ", r)
    else:
        print("  (no season_id cell dropped below baseline)")
    print("all_match_rows_ok:", summary["all_match_rows_ok"])
    for g in comp_season_gaps:
        print("  [gap] %s" % g)
    ncomp = len(new_season_rows)
    print("comps with growth:", ncomp)
    for name, tbls in new_season_rows.items():
        total = sum(sum(sidctx.values()) for sidctx in tbls.values())
        print("  %-24s grown rows=%d tables=%s" % (name, total, sorted(tbls.keys())))
    print("Wrote", OUT)


if __name__ == "__main__":
    main()
