#!/usr/bin/env python3
"""gen4_phaseB_cup_retry_verify_scope.py — Verify scope guard for the cup-retry tranche.

Compares current DB counts against the cup-retry baseline (per comp x table x
season_id, 5 cups only). Any season_id count that DROPS below baseline is a
regression (violation). Cup-retry season ids may only grow (that is the backfill).

Reads baseline from data/gen4_phaseB_cup_retry_baseline_counts.json.
Writes data/gen4_phaseB_cup_retry_scope_verify.json.

Usage: .runner-venv/bin/python3 gen4_phaseB_cup_retry_verify_scope.py
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
import mysql.connector

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

BASELINE_FILE = ROOT / "data/gen4_phaseB_cup_retry_baseline_counts.json"
OUT = ROOT / "data/gen4_phaseB_cup_retry_scope_verify.json"

COMPETITION_IDS = {
    217: "DFB Pokal",
    101: "J.League Cup",
    323: "Emperor's Cup",
    1786: "Australia Cup",
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
        charset="utf8mb4",
        connect_timeout=10,
    )


def main():
    base = json.loads(BASELINE_FILE.read_text())["baseline"]
    conn = get_conn()
    cur = conn.cursor()

    regressions = []        # drops below baseline
    new_season_rows = {}    # rows only grown

    for ut, name in COMPETITION_IDS.items():
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
                now = {int(r[0]): int(r[1]) for r in cur.fetchall()}
            except Exception:
                now = {}
            bl = base.get(str(ut), {}).get(table, {})
            # Normalize: baseline JSON stores season_id as str keys; live query returns ints.
            bl = {int(k): int(v) for k, v in bl.items()}

            for sid, cnt in bl.items():
                n = now.get(sid, 0)
                if n < cnt:
                    regressions.append({"comp": name, "table": table,
                                        "season_id": sid, "baseline": cnt, "now": n})
            for sid, cnt in now.items():
                if sid not in bl or cnt > bl[sid]:
                    new_season_rows.setdefault(name, {}).setdefault(table, {})[sid] = cnt

    conn.close()

    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "scope_guard_ok": len(regressions) == 0,
        "regressions": regressions,
        "grown_rows": new_season_rows,
    }
    OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2))

    print("scope_guard_ok:", summary["scope_guard_ok"])
    if regressions:
        print("REGRESSIONS:")
        for r in regressions:
            print("  %s %s sid=%s baseline=%s now=%s" % (
                r["comp"], r["table"], r["season_id"], r["baseline"], r["now"]))
        raise SystemExit(1)
    print("PASS: no season_id count dropped below baseline (5-cup scope)")


if __name__ == "__main__":
    main()
