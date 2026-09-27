#!/usr/bin/env python3
"""gen4_phaseB_19-20_verify_scope.py — Verify scope guard + 19/20 growth.

Compares current DB counts against the baseline (per comp x table x season_id).
Any season_id count that DROPS below baseline is a regression (violation).
19/20 season ids may only grow (that is the backfill).

Reads baseline from data/gen4_phaseB_19-20_baseline_counts.json.
Writes data/gen4_phaseB_19-20_scope_verify.json.

Usage: .runner-venv/bin/python gen4_phaseB_19-20_verify_scope.py
"""

from __future__ import annotations

import json
from pathlib import Path

from dotenv import load_dotenv

import gen4_phaseB_19_20_prep as prep

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

BASELINE_FILE = ROOT / "data/gen4_phaseB_19-20_baseline_counts.json"
OUT = ROOT / "data/gen4_phaseB_19-20_scope_verify.json"


def main():
    base = json.loads(BASELINE_FILE.read_text())["baseline"]
    conn = prep.get_conn()
    cur = conn.cursor()

    regressions = []        # drops below baseline
    new_season_rows = {}    # 19/20 + others only grown

    for ut, name in prep.COMPETITION_IDS.items():
        for table in prep.TABLES:
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

    # Aggregate 19/20 growth: only season_ids that are year_label 19/20 per comp
    summary = {
        "timestamp": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "scope_guard_ok": len(regressions) == 0,
        "regressions": regressions,
        "grown_rows": new_season_rows,
    }
    OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2))

    print("scope_guard_ok:", summary["scope_guard_ok"])
    if regressions:
        print("REGRESSIONS:")
        for r in regressions:
            print("  ", r)
    else:
        print("  (no season_id cell dropped below baseline)")
    ncomp = len(new_season_rows)
    print("comps with growth:", ncomp)
    for name, tbls in new_season_rows.items():
        total = sum(sum(sidctx.values()) for sidctx in tbls.values())
        print("  %-24s grown recompute rows=%d tables=%s" % (name, total, sorted(tbls.keys())))
    print("Wrote", OUT)


if __name__ == "__main__":
    main()