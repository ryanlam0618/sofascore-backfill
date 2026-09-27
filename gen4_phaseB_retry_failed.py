#!/usr/bin/env python3
"""Retry the 56 failed events from Phase B 17/18 (transient http_403).

Reuses gen4_phaseB_backfill machinery (health-aware IP pool, writers,
process_event). Scope: only event IDs listed in /tmp/gen4_phaseB/failed_56.json.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
EV_IN = Path("/tmp/gen4_phaseB/failed_56.json")
REPORT = ROOT / "data/gen4_phaseB_retry_failed_17-18.json"

sys.path.insert(0, str(ROOT))
import gen4_phaseB_backfill as pb  # noqa: E402


def main() -> int:
    import mysql.connector
    env = dict(l.split("=", 1) for l in (ROOT / ".env").read_text().splitlines()
               if "=" in l and not l.startswith("#"))
    conn = mysql.connector.connect(host="127.0.0.1", port=3306, user="appdb_rw",
                                   password=env["MYSQL_PASSWORD"].strip(), database="appdb",
                                   autocommit=False)
    from backfill_runner import DataInserter, ensure_competition, ensure_season
    inserter = DataInserter(conn)
    pb.HEALTH_ENABLED = True

    failed = json.loads(EV_IN.read_text())
    by_comp = {}
    for r in failed:
        by_comp.setdefault(r["competition"], []).append(r["event_id"])

    targets = {name: (ut, cat_id, slabel, sid)
               for name, ut, cat_id, slabel, sid, _cut in pb.resolve_targets()}

    per_ip, report = {}, {"events": len(failed), "ok": 0, "fail": 0, "details": []}
    t0 = time.monotonic()
    for cname, ids in by_comp.items():
        ut, cat_id, slabel, sid = targets[cname]
        comp_id = ensure_competition(conn, cname, cat_id or 0, ut,
                                     "uefa" if cat_id in (1465, 1467) else "national", "")
        ensure_season(conn, sid, comp_id, slabel)
        retries, _ = pb.tier(cname)
        print(f"\n[{cname}] season={sid} retry_events={len(ids)}", flush=True)
        for eid in ids:
            rec = pb.process_event(conn, inserter, cname, eid, comp_id, sid,
                                   retries, per_ip)
            rec["competition"] = cname
            report["details"].append(rec)
            report["ok" if rec["ok"] else "fail"] += 1
            print(f"  {eid}: {'OK' if rec['ok'] else 'FAIL'} "
                  f"{json.dumps(rec.get('steps', {}))[:160]}", flush=True)

    report["duration_s"] = round(time.monotonic() - t0, 1)
    report["per_ip"] = per_ip
    REPORT.write_text(json.dumps(report, indent=2))
    print(f"\nDONE ok={report['ok']} fail={report['fail']} -> {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
