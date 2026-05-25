#!/usr/bin/env python3
"""Retry failed incident metadata with aggressive backoff + longer sleeps.

Uses Webshare proxy (verified working 2026-05-13).
"""
import json, random, subprocess, sys, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

WORKDIR = "/root/.openclaw/workspace/sofascore_backfill"
API_BASE = "https://www.sofascore.com/api/v1"

# Webshare proxy (verified working 2026-05-13)
_PROXY = "http://aeptenjc:dztr57tcycoz@142.111.67.146:5611"
_PROXIES = {"http": _PROXY, "https": _PROXY}

def _build_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler(_PROXIES))

_opener = None
def _get_opener():
    global _opener
    if _opener is None:
        _opener = _build_opener()
    return _opener

def now_mysql():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def fetch_event(event_id: int) -> dict:
    """Fetch event details via SofaScore API through Webshare proxy."""

    headers = {
        "User-Agent": random.choice([
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
        ]),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.sofascore.com/",
    }

    url = f"{API_BASE}/event/{event_id}"
    req = urllib.request.Request(url, headers=headers)

    for attempt in range(5):
        try:
            with _get_opener().open(req, timeout=15) as resp:
                data = json.loads(resp.read())
                ev = data.get("event", {})
                return {
                    "event_id": event_id,
                    "match_date": ev.get("startTimestamp", 0),
                    "league": (ev.get("tournament") or {}).get("name", ""),
                    "home_team": (ev.get("homeTeam") or {}).get("name", ""),
                    "away_team": (ev.get("awayTeam") or {}).get("name", ""),
                    "status": 200,
                }
        except urllib.error.HTTPError as e:
            if e.code == 403:
                backoff = (2 ** attempt) * random.uniform(15, 45)
                print(f"[RATELIMIT] event={event_id} attempt={attempt+1} backing off {backoff:.0f}s", flush=True)
                time.sleep(backoff)
            else:
                return {"event_id": event_id, "status": e.code}
        except Exception as e:
            backoff = (2 ** attempt) * random.uniform(5, 15)
            print(f"[ERROR] event={event_id} attempt={attempt+1} {type(e).__name__}: {e}, backing off {backoff:.0f}s", flush=True)
            time.sleep(backoff)

    return {"event_id": event_id, "status": 0}

def main():
    sys.path.insert(0, WORKDIR)
    import fetch_incidents as fi

    conn = fi.mysql_connect()
    cur = conn.cursor()

    # Get events that still need metadata
    cur.execute('''
        SELECT DISTINCT event_id FROM sofascore_incident_events
        WHERE match_date IS NULL OR match_date = ""
        ORDER BY event_id
    ''')
    event_ids = [r[0] for r in cur.fetchall()]
    total = len(event_ids)
    cur.close()
    conn.close()

    print(f"[INFO] Retrying {total} events with failed metadata via proxy", flush=True)
    print(f"[INFO] Using 2 workers, 5-15s base sleep, exponential backoff on 403s", flush=True)

    if total == 0:
        print("[DONE] No events to retry!")
        return

    done = ok = errors = 0
    BASE_SLEEP_MIN = 5
    BASE_SLEEP_MAX = 15

    def fetch_one(event_id):
        # Random sleep before request (mimics human behavior)
        time.sleep(random.uniform(BASE_SLEEP_MIN, BASE_SLEEP_MAX))

        result = fetch_event(event_id)

        if result["status"] == 200:
            # Write to MySQL
            match_date_ts = result.get("match_date", 0)
            if match_date_ts:
                import datetime as dt
                match_date = dt.datetime.fromtimestamp(match_date_ts, tz=timezone.utc).strftime("%Y-%m-%d")
            else:
                match_date = ""

            sys.path.insert(0, WORKDIR)
            import fetch_incidents as fi2
            conn2 = fi2.mysql_connect()
            cur2 = conn2.cursor()

            cur2.execute("""
                UPDATE sofascore_incident_events
                SET match_date=%s, league=%s, home_team=%s, away_team=%s
                WHERE event_id=%s
            """, (match_date, result["league"], result["home_team"], result["away_team"], event_id))

            # Also update incidents table
            cur2.execute("""
                UPDATE sofascore_incidents
                SET match_date=%s, league=%s, home_team=%s, away_team=%s
                WHERE event_id=%s
            """, (match_date, result["league"], result["home_team"], result["away_team"], event_id))

            conn2.commit()
            cur2.close()
            conn2.close()

            return event_id, True, f'{result["league"]} {result["home_team"]} vs {result["away_team"]}'
        else:
            return event_id, False, f'status={result["status"]}'

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(fetch_one, eid): eid for eid in event_ids}
        for future in as_completed(futures):
            eid, ok_flag, label = future.result()
            done += 1
            if ok_flag:
                ok += 1
            else:
                errors += 1
            if done % 50 == 0 or done == total:
                print(f"[PROGRESS] {done}/{total} ok={ok} err={errors} last={label}", flush=True)

    print(f"[DONE] Processed {total} events: {ok} ok, {errors} errors", flush=True)

if __name__ == "__main__":
    main()