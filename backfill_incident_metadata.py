#!/usr/bin/env python3
"""Backfill event metadata (match_date, league, home_team, away_team) for existing incidents.
Uses the already-collected incident data; only fetches event details for each event_id.
Much faster than re-running the full incident backfill."""
import json, random, sqlite3, subprocess, sys, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

API_BASE = "https://www.sofascore.com/api/v1"
PROXY = ""  # Direct connection works
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}

def _random_ua():
    uas = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    ]
    return random.choice(uas)

def _default_headers():
    return {
        "User-Agent": _random_ua(),
        "Accept": "application/json",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    }

def api_get(path: str) -> tuple[int, dict]:
    url = f"{API_BASE}/{path.lstrip('/')}"
    req = urllib.request.Request(url, headers=_default_headers())
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception:
        return 500, {}

def fetch_event_details(event_id: int) -> dict:
    status, data = api_get(f"event/{event_id}")
    if status == 200:
        ev = data.get("event", {})
        ts = ev.get("startTimestamp", 0)
        match_date = ""
        if ts:
            try:
                match_date = datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d")
            except Exception:
                pass
        return {
            "event_id": event_id,
            "match_date": match_date,
            "league": (ev.get("tournament") or {}).get("name", ""),
            "home_team": (ev.get("homeTeam") or {}).get("name", ""),
            "away_team": (ev.get("awayTeam") or {}).get("name", ""),
        }
    return None

def now_mysql():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def mysql_connect():
    import os
    env = {}
    ep = "/root/.openclaw/workspace/.env"
    if os.path.exists(ep):
        for line in open(ep):
            line = line.strip()
            if "=" in line:
                k, v = line.split("=", 1)
                env[k] = v
    import mysql.connector as mysql
    return mysql.connect(
        host=env.get("MYSQL_HOST", "127.0.0.1"),
        port=int(env.get("MYSQL_PORT", "3306")),
        user=env.get("MYSQL_USER", "root"),
        password=env.get("MYSQL_PASSWORD", ""),
        database=env.get("MYSQL_DATABASE", "appdb"),
    )

def main():
    use_mysql = "--use-mysql" in sys.argv
    
    if use_mysql:
        conn = mysql_connect()
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT event_id FROM sofascore_incident_events WHERE match_date IS NULL OR match_date = ''")
        event_ids = [r[0] for r in cur.fetchall()]
        cur.close()
    else:
        conn = sqlite3.connect("data/backfill_sofascore_10y/incidents.sqlite")
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT event_id FROM incident_events WHERE match_date IS NULL OR match_date = ''")
        event_ids = [r[0] for r in cur.fetchall()]
        cur.close()
    
    total = len(event_ids)
    
    print(f"[INFO] Backfilling metadata for {total} events", flush=True)
    print(f"[INFO] Running with 5 workers, ~0.5-1.5s sleep per request", flush=True)
    print(f"[INFO] Estimated time: ~{total // 5 * 2 // 60} minutes", flush=True)
    
    done = 0
    errors = 0
    
    def process_event(eid):
        meta = fetch_event_details(eid)
        time.sleep(random.uniform(0.5, 1.5))
        return eid, meta
    
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(process_event, eid): eid for eid in event_ids}
        
        for future in as_completed(futures):
            eid, meta = future.result()
            done += 1
            
            if meta is None:
                errors += 1
                if done % 500 == 0:
                    print(f"[PROGRESS] {done}/{total} - errors: {errors}", flush=True)
                continue
            
            # Update MySQL
            if use_mysql:
                try:
                    conn2 = mysql_connect()
                    cur2 = conn2.cursor()
                    cur2.execute('''
                        UPDATE sofascore_incident_events SET
                            match_date = %s, league = %s, home_team = %s, away_team = %s
                        WHERE event_id = %s AND (match_date IS NULL OR match_date = '')
                    ''', (meta['match_date'], meta['league'], meta['home_team'], meta['away_team'], eid))
                    cur2.execute('''
                        UPDATE sofascore_incidents SET
                            match_date = %s, league = %s, home_team = %s, away_team = %s,
                            fetched_at = %s
                        WHERE event_id = %s AND (match_date IS NULL OR match_date = '')
                    ''', (meta['match_date'], meta['league'], meta['home_team'], meta['away_team'], now_mysql(), eid))
                    conn2.commit()
                    cur2.close()
                    conn2.close()
                except Exception as e:
                    errors += 1
            
            if done % 500 == 0:
                print(f"[PROGRESS] {done}/{total} - errors: {errors}, last: {meta['league']}", flush=True)
    
    print(f"[DONE] Processed {done} events, errors: {errors}", flush=True)

if __name__ == "__main__":
    main()
