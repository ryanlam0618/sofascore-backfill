#!/usr/bin/env python3
"""gen4_phaseA_repair_sweep.py — partial-write repair for Phase A.

Context (Kris decision 2026-09-09 02:14 GMT+8, option (a)):
Container restarted 2026-09-09 01:47:44 mid-backfill. Events in-flight at
that moment may have match rows inserted but sub-endpoints never written, and
`--resume` on relaunch skipped them (they were in `matches`).

Detection logic:
  suspect = match_ids in matches for seasons 15/16/16/17
            minus event ids present as successfully processed in
            /tmp/gen4_phaseA/evidence_*.jsonl
Rationale: every processed event writes exactly one evidence line; any match
row without a line was killed mid-write.
  (Pre-Phase-A the matches table contained no rows for these two seasons —
   verified by resume counts (PL 15/16: 390 total -> 383 remaining => only the
   7 pre-crash rows existed).)

Action: re-run full process_event for each suspect through the live
gen4_phaseA_backfill machinery (same pool, same writers, same skip rules).

Output: data/gen4_phaseA_repair_report.json
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import gen4_phaseA_backfill as A  # reuse fetch/process machinery (no __main__ side effects)

EVIDENCE_GLOB = "/tmp/gen4_phaseA/evidence_*.jsonl"
REPORT = ROOT / "data/gen4_phaseA_repair_report.json"
TARGET_LABELS = ("15/16", "16/17")


def mysql_conn():
    import mysql.connector
    env = dict(l.split("=", 1) for l in (ROOT / ".env").read_text().splitlines()
               if "=" in l and not l.startswith("#"))
    return mysql.connector.connect(host="127.0.0.1", port=3306, user="appdb_rw",
                                   password=env["MYSQL_PASSWORD"].strip(),
                                   database="appdb", autocommit=False)


def main():
    import glob
    t0 = time.monotonic()

    # 1. evidence-processed event ids (both pre- and post-crash runs write here)
    done_ids = set()
    for f in glob.glob(EVIDENCE_GLOB):
        for line in open(f):
            try:
                done_ids.add(json.loads(line)["event_id"])
            except Exception:
                pass

    conn = mysql_conn()
    cur = conn.cursor()
    ph = ",".join(["%s"] * len(TARGET_LABELS))
    cur.execute(
        f"""SELECT m.match_id, m.season_id, s.year_label, c.competition_id, c.name
              FROM matches m
              JOIN seasons s ON m.season_id = s.season_id
              JOIN competitions c ON m.competition_id = c.competition_id
             WHERE s.year_label IN ({ph})""",
        TARGET_LABELS,
    )
    rows = cur.fetchall()
    suspects = [r for r in rows if r[0] not in done_ids]
    print(f"matches in scope: {len(rows)}, evidence-done: {len(done_ids)}, suspects: {len(suspects)}", flush=True)

    from backfill_runner import DataInserter
    inserter = DataInserter(conn)

    repaired, still_partial, per_endpoint = 0, 0, {}
    per_ip = {}
    repaired_ids = []
    for i, (mid, sid, _lbl, _cid, cname) in enumerate(suspects):
        retries, _ = A.tier(cname)
        rec = A.process_event(conn, inserter, cname, mid, _cid, sid, retries, per_ip)
        steps = rec.get("steps", {})
        new_ok = any(s.get("status") == "ok" and (s.get("rows") or 0) > 0 for s in steps.values())
        if rec.get("ok") and new_ok:
            repaired += 1
            repaired_ids.append(mid)
        else:
            # ok but all-no_data is fine if flags said no data; count only
            # genuine hard fails as still_partial
            hard_fail = (rec.get("event_status") not in (None, "ok")) or any(
                s.get("status") == "fail" for s in steps.values())
            if hard_fail:
                still_partial += 1
            else:
                repaired += 1
                repaired_ids.append(mid)
        for ep, st in steps.items():
            if st.get("status") == "ok" and (st.get("rows") or 0) > 0:
                per_endpoint[ep] = per_endpoint.get(ep, 0) + st["rows"]
        if (i + 1) % 10 == 0:
            print(f"  [{i+1}/{len(suspects)}] repaired={repaired} still_partial={still_partial}", flush=True)
    conn.commit()
    conn.close()

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "task": "phaseA partial-write repair sweep (Kris 02:14 decision (a))",
        "matches_in_scope": len(rows),
        "evidence_processed_unique": len(done_ids),
        "suspected_partial_events": len(suspects),
        "repaired_count": repaired,
        "still_partial_count": still_partial,
        "rows_written_per_endpoint": per_endpoint,
        "repaired_event_ids": repaired_ids,
        "duration_s": round(time.monotonic() - t0, 1),
    }
    REPORT.write_text(json.dumps(report, indent=2))
    print(json.dumps({k: report[k] for k in
                      ("suspected_partial_events", "repaired_count", "still_partial_count",
                       "rows_written_per_endpoint")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
