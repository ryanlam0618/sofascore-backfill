#!/usr/bin/env python3
"""Driver for fetch_attendance.py — backfill attendance for all events in sofascore_incident_events."""
import random, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed

WORKDIR = "/root/.openclaw/workspace/sofascore_backfill"

def main():
    import sys; sys.path.insert(0, WORKDIR)
    import fetch_incidents as fi
    
    conn = fi.mysql_connect()
    cur = conn.cursor()
    
    # Get all event_ids that need attendance (from incident_events table)
    cur.execute('''
        SELECT DISTINCT ie.event_id, ie.match_date, ie.league, ie.home_team, ie.away_team
        FROM sofascore_incident_events ie
        LEFT JOIN sofascore_attendance sa ON ie.event_id = sa.event_id
        WHERE sa.event_id IS NULL
    ''')
    events = []
    for r in cur.fetchall():
        events.append({'event_id': r[0], 'match_date': r[1], 'league': r[2], 
                       'home_team': r[3], 'away_team': r[4]})
    cur.close()
    conn.close()
    
    total = len(events)
    print(f"[INFO] Backfilling attendance for {total} events")
    
    done = ok = errors = 0
    SLEEP_MIN, SLEEP_MAX = 0.5, 1.5
    
    def fetch_one(ev):
        time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))
        result = subprocess.run([
            sys.executable, f"{WORKDIR}/fetch_attendance.py",
            "--event-id", str(ev['event_id']),
            "--use-mysql",
            "--sleep-min", str(SLEEP_MIN),
            "--sleep-max", str(SLEEP_MAX),
        ], capture_output=True, text=True, cwd=WORKDIR)
        return ev['event_id'], result.returncode == 0, "ok" in result.stdout.lower()
    
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(fetch_one, ev): ev for ev in events}
        for future in as_completed(futures):
            eid, ok_flag, has_ok = future.result()
            done += 1
            if ok_flag or has_ok:
                ok += 1
            else:
                errors += 1
            if done % 500 == 0:
                print(f"[PROGRESS] {done}/{total} ok={ok} err={errors}", flush=True)
    
    print(f"[DONE] attendance backfill: {ok}/{total} ok, {errors} errors", flush=True)

if __name__ == "__main__":
    main()