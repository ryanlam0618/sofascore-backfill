#!/usr/bin/env python3
"""gen4_lineups_rerun.py — Re-fetch 2 lineups fail events after patch line 203.

Main Agent self-run (Kris #24179 explicit "do it by yourself" after 5 forge aborts).
Patch: gen4_stage3_expansion_backfill.py line 203 — jersey_number sanitize to NULL.

Targets:
- UEL 14023705
- Coppa Italia 14054083
"""
import json, time, sys
from pathlib import Path

ROOT = Path("/root/.openclaw/workspace/sofascore-backfill")
sys.path.insert(0, str(ROOT))

from gen4_stage3_expansion_backfill import (
    fetch_json, insert_match_lineups, tier, ROOT as M_ROOT
)

TARGETS = [
    {"competition": "UEL", "event_id": 14023705, "comp_id": 679, "season_id": 76984, "retries": 1},
    {"competition": "Coppa Italia", "event_id": 14054083, "comp_id": 328, "season_id": 77308, "retries": 1},
]

def main():
    env = dict(l.split('=',1) for l in (ROOT/'.env').read_text().splitlines() if '=' in l and not l.startswith('#'))
    import mysql.connector
    conn = mysql.connector.connect(host='127.0.0.1', port=3306, user='appdb_rw',
                                   password=env.get('MYSQL_PASSWORD','').strip(),
                                   database='appdb', autocommit=False)
    
    results = []
    for t in TARGETS:
        eid = t['event_id']
        comp = t['competition']
        retries = t['retries']
        print(f"\n[{comp}] event_id={eid} — re-fetch /lineups", flush=True)
        
        # Fetch event root (need home/away ids for _ins_lineups)
        ev_root_r = fetch_json(f"/event/{eid}", retries)
        if ev_root_r.kind != "ok":
            print(f"  event root fetch failed: {ev_root_r}", flush=True)
            results.append({"event_id": eid, "comp": comp, "status": "fail", "reason": f"event_root_{ev_root_r.kind}", "err": ev_root_r.error})
            continue
        ev_root = ev_root_r.body.get("event") or ev_root_r.body
        home_team_id = ev_root.get("homeTeam", {}).get("id") or ev_root.get("home", {}).get("id")
        away_team_id = ev_root.get("awayTeam", {}).get("id") or ev_root.get("away", {}).get("id")
        
        # Fetch lineups
        lineups_r = fetch_json(f"/event/{eid}/lineups", retries)
        if lineups_r.kind != "ok":
            print(f"  lineups fetch failed: {lineups_r}", flush=True)
            results.append({"event_id": eid, "comp": comp, "status": "fail", "reason": f"lineups_{lineups_r.kind}", "err": lineups_r.error})
            continue
        
        # Count before
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM match_lineups WHERE match_id=%s", (eid,))
        before = cur.fetchone()[0]
        cur.close()
        
        # Insert (patched line 203)
        try:
            insert_match_lineups(conn, eid, lineups_r.body, {"home": {"id": home_team_id}, "away": {"id": away_team_id}})
            conn.commit()
        except Exception as e:
            print(f"  insert error: {e}", flush=True)
            conn.rollback()
            results.append({"event_id": eid, "comp": comp, "status": "fail", "reason": "insert_error", "err": str(e)[:200]})
            continue
        
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM match_lineups WHERE match_id=%s", (eid,))
        after = cur.fetchone()[0]
        cur.close()
        
        delta = after - before
        print(f"  OK lineups: {before} -> {after} (delta={delta})", flush=True)
        results.append({"event_id": eid, "comp": comp, "status": "ok", "before": before, "after": after, "delta": delta, "ip_used": lineups_r.ip})
    
    conn.close()
    
    out = ROOT / "data" / "gen4_lineups_rerun.json"
    out.write_text(json.dumps({"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"), "results": results}, indent=2))
    print(f"\nReport: {out}", flush=True)
    
    n_ok = sum(1 for r in results if r['status']=='ok')
    n_fail = sum(1 for r in results if r['status']=='fail')
    print(f"\n=== SUMMARY ===")
    print(f"OK: {n_ok}/{len(results)}, FAIL: {n_fail}/{len(results)}")
    return 0 if n_fail == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
