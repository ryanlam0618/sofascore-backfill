#!/usr/bin/env python3
"""Simple sequential retry of failed event metadata — no threading to avoid crashes."""
import json, random, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone

WORKDIR = "/root/.openclaw/workspace/sofascore_backfill"
API_BASE = "https://www.sofascore.com/api/v1"
_PROXY = "http://aeptenjc:dztr57tcycoz@142.111.67.146:5611"
_PROXIES = {"http": _PROXY, "https": _PROXY}

_opener = None
def _get_opener():
    global _opener
    if _opener is None:
        _opener = urllib.request.build_opener(urllib.request.ProxyHandler(_PROXIES))
    return _opener

def now_mysql():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def fetch_event(event_id: int) -> dict:
    """Fetch event details via SofaScore API through Webshare proxy."""
    headers = {
        "User-Agent": random.choice([
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36",
        ]),
        "Accept": "application/json",
        "Referer": "https://www.sofascore.com/",
    }
    url = f"{API_BASE}/event/{event_id}"
    req = urllib.request.Request(url, headers=headers)

    for attempt in range(4):
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
                sleep_time = (2 ** attempt) * random.uniform(15, 45)
                print(f"[RATELIMIT] event={event_id} 403, retry in {sleep_time:.0f}s")
                time.sleep(sleep_time)
            else:
                return {"event_id": event_id, "status": e.code}
        except Exception as e:
            if attempt < 3:
                time.sleep((attempt + 1) * 5)
    return {"event_id": event_id, "status": 0}

def main():
    sys.path.insert(0, WORKDIR)
    import fetch_incidents as fi

    conn = fi.mysql_connect()
    cur = conn.cursor()

    cur.execute('''
        SELECT DISTINCT event_id FROM sofascore_incident_events
        WHERE match_date IS NULL OR match_date = ""
        ORDER BY event_id
    ''')
    event_ids = [r[0] for r in cur.fetchall()]
    total = len(event_ids)
    cur.close()
    conn.close()

    print(f"[INFO] Sequential retry: {total} events")
    ok = errors = 0
    BATCH_SIZE = 100

    for i, event_id in enumerate(event_ids, 1):
        sleep_time = random.uniform(4, 10)
        time.sleep(sleep_time)

        result = fetch_event(event_id)

        if result["status"] == 200:
            match_date_ts = result.get("match_date", 0)
            if match_date_ts:
                import datetime as dt
                match_date = dt.datetime.fromtimestamp(match_date_ts, tz=timezone.utc).strftime("%Y-%m-%d")
            else:
                match_date = ""

            conn2 = fi.mysql_connect()
            cur2 = conn2.cursor()
            cur2.execute("""
                UPDATE sofascore_incident_events
                SET match_date=%s, league=%s, home_team=%s, away_team=%s
                WHERE event_id=%s
            """, (match_date, result["league"], result["home_team"], result["away_team"], event_id))
            cur2.execute("""
                UPDATE sofascore_incidents
                SET match_date=%s, league=%s, home_team=%s, away_team=%s
                WHERE event_id=%s
            """, (match_date, result["league"], result["home_team"], result["away_team"], event_id))
            conn2.commit()
            cur2.close()
            conn2.close()
            ok += 1
            label = f'{result["league"]} {result["home_team"]} vs {result["away_team"]}'
        else:
            errors += 1
            label = f'status={result["status"]}'

        if i % 20 == 0 or i == total:
            print(f"[PROGRESS] {i}/{total} ok={ok} err={errors} last={label}", flush=True)

        # Periodic commit of progress
        if i % BATCH_SIZE == 0:
            print(f"[BATCH] Completed {i}/{total}", flush=True)

    print(f"[DONE] {total} events: ok={ok}, errors={errors}", flush=True)

if __name__ == "__main__":
    main()