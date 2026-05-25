#!/usr/bin/env python3
"""
Bypass SofaScore API rate-limits by scraping HTML pages instead.
Scrapes __NEXT_DATA__ JSON from https://www.sofascore.com/event/{event_id}
Extracts: tournament name, home team, away team, start timestamp.
Updates sofascore_incident_events and sofascore_incidents tables.
"""
from __future__ import annotations
import json, random, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone

WORKDIR = "/root/.openclaw/workspace/sofascore_backfill"
PROXY = "http://aeptenjc:dztr57tcycoz@142.111.67.146:5611"
_PROXIES = {"http": PROXY, "https": PROXY}

_opener = None
def _get_opener():
    global _opener
    if _opener is None:
        _opener = urllib.request.build_opener(urllib.request.ProxyHandler(_PROXIES))
    return _opener

def now_mysql():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
]

def fetch_event_html(event_id: int) -> dict:
    """Fetch event details from HTML page __NEXT_DATA__ JSON."""
    url = f"https://www.sofascore.com/event/{event_id}"
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml",
        "Referer": "https://www.sofascore.com/",
    }
    req = urllib.request.Request(url, headers=headers)
    
    try:
        with _get_opener().open(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
            
            nd_idx = html.find("__NEXT_DATA__")
            if nd_idx < 0:
                return {"event_id": event_id, "status": "no_next_data"}
            
            json_start = html.find("{", nd_idx)
            brace_count = 0
            for i, c in enumerate(html[json_start:]):
                if c == "{": brace_count += 1
                elif c == "}": brace_count -= 1
                if brace_count == 0:
                    data = json.loads(html[json_start:json_start + i + 1])
                    break
            else:
                return {"event_id": event_id, "status": "json_parse_error"}
            
            # Navigate to event data
            pp = data.get("props", {}).get("pageProps", {})
            ip = pp.get("initialProps", pp) or {}
            ev = ip.get("event", {})
            
            if not ev:
                return {"event_id": event_id, "status": "no_event_data"}
            
            # startTime: might be numeric timestamp or ISO string
            start_ts = ev.get("startTime") or ev.get("startTimestamp") or 0
            if isinstance(start_ts, str) and start_ts:
                # Try parse ISO string
                try:
                    ts_int = int(datetime.fromisoformat(start_ts.replace("Z", "+00:00")).timestamp())
                except:
                    ts_int = 0
            elif isinstance(start_ts, (int, float)) and start_ts > 0:
                ts_int = int(start_ts)
            else:
                ts_int = 0
            
            if ts_int:
                match_date = datetime.fromtimestamp(ts_int, tz=timezone.utc).strftime("%Y-%m-%d")
            else:
                match_date = ""
            
            return {
                "event_id": event_id,
                "status": 200,
                "match_date": match_date,
                "match_ts": ts_int,
                "league": (ev.get("tournament") or {}).get("name", ""),
                "home_team": (ev.get("homeTeam") or {}).get("name", ""),
                "away_team": (ev.get("awayTeam") or {}).get("name", ""),
            }
    except urllib.error.HTTPError as e:
        return {"event_id": event_id, "status": f"http_{e.code}"}
    except Exception as e:
        return {"event_id": event_id, "status": f"error_{type(e).__name__}"}


def main():
    sys.path.insert(0, WORKDIR)
    from fetch_incidents import mysql_connect
    
    conn = mysql_connect()
    cur = conn.cursor()
    
    # Get events with NULL metadata
    cur.execute("""
        SELECT DISTINCT event_id FROM sofascore_incident_events
        WHERE match_date IS NULL OR match_date = ''
        ORDER BY event_id
    """)
    event_ids = [r[0] for r in cur.fetchall()]
    total = len(event_ids)
    
    if total == 0:
        print("[INFO] No events with NULL metadata - all done!")
        cur.close()
        conn.close()
        return
    
    print(f"[INFO] HTML retry: {total} events with NULL metadata")
    
    ok = errors = skipped = 0
    status_counts = {}
    
    for i, event_id in enumerate(event_ids, 1):
        sleep_time = random.uniform(2, 5)
        time.sleep(sleep_time)
        
        result = fetch_event_html(event_id)
        status = result["status"]
        
        status_counts[status] = status_counts.get(status, 0) + 1
        
        if status == 200:
            conn2 = mysql_connect()
            cur2 = conn2.cursor()
            cur2.execute("""
                UPDATE sofascore_incident_events
                SET match_date=%s, league=%s, home_team=%s, away_team=%s
                WHERE event_id=%s
            """, (result["match_date"], result["league"], result["home_team"], result["away_team"], event_id))
            cur2.execute("""
                UPDATE sofascore_incidents
                SET match_date=%s, league=%s, home_team=%s, away_team=%s
                WHERE event_id=%s
            """, (result["match_date"], result["league"], result["home_team"], result["away_team"], event_id))
            conn2.commit()
            cur2.close()
            conn2.close()
            ok += 1
            label = f'{result["league"]} {result["home_team"]} vs {result["away_team"]}'
        else:
            errors += 1
            label = f'status={status}'
        
        if i % 20 == 0 or i == total:
            print(f"[PROGRESS] {i}/{total} ok={ok} err={errors} last={label}", flush=True)
    
    print(f"[DONE] HTML retry: {total} events, ok={ok}, errors={errors}")
    print(f"Status breakdown: {dict(sorted(status_counts.items()))}")
    
    cur.close()
    conn.close()

if __name__ == "__main__":
    main()