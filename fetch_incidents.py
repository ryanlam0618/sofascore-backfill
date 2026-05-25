#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fetch match incidents (timeline/events feed) from SofaScore via direct API.
Covers: Goals, cards, substitutions, penalties, VAR, etc. with minute, added time, coordinates.

API: GET https://www.sofascore.com/api/v1/event/{event_id}/incidents
Status: ✅ Direct API confirmed working (2026-05-03)

Supports MySQL via USE_MYSQL=1 or --use-mysql flag.
Resume-safe with sqlite + state JSON.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import time
import urllib.request
import urllib.error

from anti_block import (
    build_headers,
    build_html_headers,
    shuffle_targets,
    random_sleep,
    RequestCounter,
    get_decoy_pages,
)

_proxy_opener = None
_proxy_list = None
_last_proxy_idx = -1

# Anti-blocking: daily request counter
_request_counter = None

def _init_counter(daily_limit=5000):
    global _request_counter
    if _request_counter is None:
        _request_counter = RequestCounter(daily_limit=daily_limit)
    return _request_counter

def _load_proxy_list():
    global _proxy_list
    if _proxy_list is None:
        proxy_file = os.path.join(os.path.dirname(__file__), "proxy_list.txt")
        if os.path.exists(proxy_file):
            with open(proxy_file) as f:
                _proxy_list = [line.strip() for line in f if line.strip()]
        else:
            _proxy_list = []
    return _proxy_list

def _get_opener():
    global _proxy_opener, _last_proxy_idx
    if _proxy_opener is None:
        proxies = _load_proxy_list()
        if proxies:
            # Round-robin through proxies
            idx = (_last_proxy_idx + 1) % len(proxies)
            _last_proxy_idx = idx
            proxy = proxies[idx]
            print(f"[PROXY] Using: {proxy.split('@')[1] if '@' in proxy else proxy}")
        else:
            proxy = os.environ.get("SOFA_PROXY", "")
        if proxy:
            ph = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        else:
            ph = urllib.request.ProxyHandler({})
        _proxy_opener = urllib.request.build_opener(ph)
    return _proxy_opener


from datetime import datetime, timezone
from pathlib import Path

from mysql_helpers import ensure_mysql_tables, mysql_connect, use_mysql

API_BASE = "https://www.sofascore.com/api/v1"


def _random_ua():
    """Return a random user agent to avoid pattern detection."""
    uas = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:125.0) Gecko/20100101 Firefox/125.0",
    ]
    return random.choice(uas)


def _default_headers():
    """Return browser-like headers for SofaScore API requests."""
    return build_headers()


def api_get(path: str, referer: str = "") -> tuple[int, dict]:
    global _request_counter
    url = f"{API_BASE}/{path.lstrip('/')}"
    headers = _default_headers()
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    
    # Track request and check daily limit
    counter = _init_counter()
    if counter.is_exhausted:
        raise RuntimeError(
            f"[BLOCK] Daily request limit ({counter.daily_limit}) reached. "
            f"Pausing until tomorrow."
        )
    counter.increment()
    
    try:
        with _get_opener().open(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception:
        # Proxy might be dead — try without proxy as fallback
        ph = urllib.request.ProxyHandler({})
        opener = urllib.request.build_opener(ph)
        try:
            with opener.open(req, timeout=15) as resp:
                return resp.status, json.loads(resp.read())
        except Exception:
            return 500, {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_mysql() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS incident_events (
          event_id INTEGER PRIMARY KEY,
          match_date TEXT,
          league TEXT,
          home_team TEXT,
          away_team TEXT,
          status_code INTEGER,
          incident_count INTEGER,
          fetched_at TEXT,
          error TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS incidents (
          event_id INTEGER,
          incident_id INTEGER,
          match_date TEXT,
          league TEXT,
          home_team TEXT,
          away_team TEXT,
          incident_type TEXT,
          minute INTEGER,
          added_time INTEGER,
          time_seconds INTEGER,
          period_time_seconds INTEGER,
          is_home_incident INTEGER,
          team_id INTEGER,
          team_name TEXT,
          player_id INTEGER,
          player_name TEXT,
          related_player_id INTEGER,
          related_player_name TEXT,
          assist_player_id INTEGER,
          assist_player_name TEXT,
          reason TEXT,
          text TEXT,
          coordinates_x REAL,
          coordinates_y REAL,
          in_stats INTEGER,
          fetched_at TEXT,
          PRIMARY KEY (event_id, incident_id)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_incidents_event ON incidents(event_id)"
    )
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.commit()


def load_state(path: Path) -> dict:
    if not path.exists():
        return {
            "started_at": now_iso(), "updated_at": now_iso(),
            "processed": 0, "ok": 0, "not_found": 0, "errors": 0,
            "rows_upserted": 0, "last_event_id": None, "last_date": None,
            "total_targets": 0,
        }
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict) -> None:
    state["updated_at"] = now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def to_int(v, default=None):
    try:
        return int(v)
    except Exception:
        return default


def to_float(v, default=None):
    try:
        return float(v)
    except Exception:
        return default


def fetch_event_details(event_id: int) -> dict:
    """Fetch event metadata: match_date, league, home_team, away_team."""
    status, data = api_get(f"event/{event_id}")
    if status == 200:
        # Event details API wraps data in {"event": {...}}
        ev = data.get("event", {})
        ts = ev.get("startTimestamp", 0)
        match_date = ""
        if ts:
            try:
                match_date = datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d")
            except Exception:
                pass
        return {
            "match_date": match_date,
            "league": (ev.get("tournament") or {}).get("name", ""),
            "home_team": (ev.get("homeTeam") or {}).get("name", ""),
            "away_team": (ev.get("awayTeam") or {}).get("name", ""),
        }
    return {"match_date": "", "league": "", "home_team": "", "away_team": ""}


def fetch_incidents(event_id: int) -> tuple[int, list]:
    """Fetch incidents for an event. Returns (status_code, incidents_list)."""
    status, data = api_get(f"event/{event_id}/incidents")
    if status == 200:
        return 200, data.get("incidents", [])
    return status, []


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill match incidents/timeline from SofaScore (direct API)")
    ap.add_argument("--db", default="data/backfill_sofascore_10y/incidents.sqlite")
    ap.add_argument("--state", default="data/backfill_sofascore_10y/incidents_state.json")
    ap.add_argument("--sleep-min", type=float, default=2.0)
    ap.add_argument("--sleep-max", type=float, default=5.0)
    ap.add_argument("--daily-limit", type=int, default=5000,
                    help="Maximum API requests per day (anti-block)")
    ap.add_argument("--no-shuffle", action="store_true",
                    help="Disable target shuffling (not recommended)")
    ap.add_argument("--browse-every", type=int, default=20,
                    help="Fetch decoy page every N requests (0=disabled)")
    ap.add_argument("--checkpoint-every", type=int, default=100)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--event-id", type=int, nargs="+", default=[],
                    help="Specific event IDs to fetch; omit to auto-discover from season events")
    ap.add_argument("--category-id", type=int, default=1, help="Tournament category ID for event discovery")
    ap.add_argument("--season-id", type=int, default=61627, help="Season ID for event discovery")
    ap.add_argument("--past-only", action="store_true", default=True,
                    help="Only fetch past (finished) matches")
    ap.add_argument("--use-mysql", action="store_true", help="Write to MySQL instead of SQLite")
    args = ap.parse_args()

    is_mysql = args.use_mysql or use_mysql()

    if is_mysql:
        conn = mysql_connect()
        ensure_mysql_tables(conn)
        print(f"[INFO] Using MySQL (database: appdb)")
        db = None  # type: ignore
    else:
        db = sqlite3.connect(args.db)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=60000")
        ensure_tables(db)
        print(f"[INFO] Using SQLite: {args.db}")
        conn = db

    state_path = Path(args.state)
    state = load_state(state_path)
    
    # Initialize anti-block counter
    counter = _init_counter(daily_limit=args.daily_limit)
    print(f"[INFO] Daily request limit: {args.daily_limit}")
    print(f"[INFO] Sleep range: {args.sleep_min}-{args.sleep_max}s")

    # Build target event IDs
    if args.event_id:
        targets = args.event_id
    else:
        status, ev_data = api_get(f"tournament/{args.category_id}/season/{args.season_id}/events")
        if status != 200:
            print(f"[ERROR] Could not fetch events: HTTP {status}")
            return
        all_evs = ev_data.get("events", [])
        now_ts = datetime.now().timestamp()
        if args.past_only:
            all_evs = [e for e in all_evs if e.get("startTimestamp", 9999999999) < now_ts]
        targets = [int(e["id"]) for e in all_evs if e.get("id")]
    
    # Shuffle targets to avoid sequential access pattern
    if not args.no_shuffle:
        targets = shuffle_targets(targets)
        print(f"[INFO] Targets shuffled to avoid sequential pattern")

    # Filter out already-processed (SQLite only; MySQL dedup handled by ON DUPLICATE KEY)
    if not is_mysql:
        done_ids = set(r[0] for r in db.execute("SELECT event_id FROM incident_events").fetchall())
        targets = [eid for eid in targets if eid not in done_ids]

    if args.limit and args.limit > 0:
        targets = targets[:args.limit]

    total = len(targets)
    state["total_targets"] = total
    save_state(state_path, state)
    print(f"[INFO] incident targets={total}")

    if total == 0:
        print("[INFO] Nothing to process")
        return

    processed_run = 0

    for event_id in targets:
        status_code = 0
        err = ""
        upserted = 0

        # Fetch event metadata FIRST (before incidents, so we have match info)
        fetched_ts_meta = now_mysql() if is_mysql else now_iso()
        event_meta = fetch_event_details(event_id)
        match_date = event_meta["match_date"]
        league = event_meta["league"]
        home_team = event_meta["home_team"]
        away_team = event_meta["away_team"]

        try:
            status_code, incidents = fetch_incidents(event_id)

            if status_code == 200:
                for inc in incidents:
                    fetched_ts = now_mysql() if is_mysql else now_iso()

                    if is_mysql:
                        cur = conn.cursor()
                        cur.execute("""
                            INSERT INTO sofascore_incidents
                            (event_id, incident_id, match_date, league, home_team, away_team,
                             incident_type, minute, added_time, time_seconds, period_time_seconds,
                             is_home_incident, team_id, team_name,
                             player_id, player_name,
                             related_player_id, related_player_name,
                             assist_player_id, assist_player_name,
                             reason, text, coordinates_x, coordinates_y, in_stats, fetched_at)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON DUPLICATE KEY UPDATE
                              match_date=VALUES(match_date), league=VALUES(league),
                              home_team=VALUES(home_team), away_team=VALUES(away_team),
                              incident_type=VALUES(incident_type), minute=VALUES(minute),
                              added_time=VALUES(added_time), time_seconds=VALUES(time_seconds),
                              period_time_seconds=VALUES(period_time_seconds),
                              is_home_incident=VALUES(is_home_incident), team_id=VALUES(team_id),
                              team_name=VALUES(team_name), player_id=VALUES(player_id),
                              player_name=VALUES(player_name), related_player_id=VALUES(related_player_id),
                              related_player_name=VALUES(related_player_name),
                              assist_player_id=VALUES(assist_player_id),
                              assist_player_name=VALUES(assist_player_name),
                              reason=VALUES(reason), text=VALUES(text),
                              coordinates_x=VALUES(coordinates_x), coordinates_y=VALUES(coordinates_y),
                              in_stats=VALUES(in_stats), fetched_at=VALUES(fetched_at)
                        """, (
                            event_id,
                            to_int(inc.get("id")),
                            match_date,
                            league,
                            home_team,
                            away_team,
                            inc.get("incidentType"),
                            to_int(inc.get("time")),
                            to_int(inc.get("addedTime")),
                            to_int(inc.get("timeSeconds")),
                            to_int(inc.get("periodTimeSeconds")),
                            1 if inc.get("isHome") is True else 0 if inc.get("isHome") is False else None,
                            to_int((inc.get("team") or {}).get("id") if isinstance(inc.get("team"), dict) else None),
                            (inc.get("team") or {}).get("name") if isinstance(inc.get("team"), dict) else None,
                            to_int((inc.get("player") or {}).get("id") if isinstance(inc.get("player"), dict) else None),
                            (inc.get("player") or {}).get("name") if isinstance(inc.get("player"), dict) else None,
                            to_int((inc.get("relatedPlayer") or {}).get("id") if isinstance(inc.get("relatedPlayer"), dict) else None),
                            (inc.get("relatedPlayer") or {}).get("name") if isinstance(inc.get("relatedPlayer"), dict) else None,
                            to_int((inc.get("assist") or {}).get("id") if isinstance(inc.get("assist"), dict) else None),
                            (inc.get("assist") or {}).get("name") if isinstance(inc.get("assist"), dict) else None,
                            inc.get("reason"),
                            inc.get("text"),
                            to_float((inc.get("coordinates") or {}).get("x") if isinstance(inc.get("coordinates"), dict) else None),
                            to_float((inc.get("coordinates") or {}).get("y") if isinstance(inc.get("coordinates"), dict) else None),
                            1 if inc.get("inStats") is True else 0 if inc.get("inStats") is False else None,
                            fetched_ts,
                        ))
                        cur.close()
                    else:
                        db.execute("""
                            INSERT OR REPLACE INTO incidents
                            (event_id, incident_id, match_date, league, home_team, away_team,
                             incident_type, minute, added_time, time_seconds, period_time_seconds,
                             is_home_incident, team_id, team_name,
                             player_id, player_name,
                             related_player_id, related_player_name,
                             assist_player_id, assist_player_name,
                             reason, text, coordinates_x, coordinates_y, in_stats, fetched_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            event_id,
                            to_int(inc.get("id")),
                            match_date,
                            league,
                            home_team,
                            away_team,
                            inc.get("incidentType"),
                            to_int(inc.get("time")),
                            to_int(inc.get("addedTime")),
                            to_int(inc.get("timeSeconds")),
                            to_int(inc.get("periodTimeSeconds")),
                            1 if inc.get("isHome") is True else 0 if inc.get("isHome") is False else None,
                            to_int((inc.get("team") or {}).get("id") if isinstance(inc.get("team"), dict) else None),
                            (inc.get("team") or {}).get("name") if isinstance(inc.get("team"), dict) else None,
                            to_int((inc.get("player") or {}).get("id") if isinstance(inc.get("player"), dict) else None),
                            (inc.get("player") or {}).get("name") if isinstance(inc.get("player"), dict) else None,
                            to_int((inc.get("relatedPlayer") or {}).get("id") if isinstance(inc.get("relatedPlayer"), dict) else None),
                            (inc.get("relatedPlayer") or {}).get("name") if isinstance(inc.get("relatedPlayer"), dict) else None,
                            to_int((inc.get("assist") or {}).get("id") if isinstance(inc.get("assist"), dict) else None),
                            (inc.get("assist") or {}).get("name") if isinstance(inc.get("assist"), dict) else None,
                            inc.get("reason"),
                            inc.get("text"),
                            to_float((inc.get("coordinates") or {}).get("x") if isinstance(inc.get("coordinates"), dict) else None),
                            to_float((inc.get("coordinates") or {}).get("y") if isinstance(inc.get("coordinates"), dict) else None),
                            1 if inc.get("inStats") is True else 0 if inc.get("inStats") is False else None,
                            fetched_ts,
                        ))
                        upserted += 1
                state["ok"] = int(state.get("ok", 0)) + 1
            elif status_code == 404:
                state["not_found"] = int(state.get("not_found", 0)) + 1
            else:
                state["errors"] = int(state.get("errors", 0)) + 1
                err = f"HTTP {status_code}"

            if is_mysql:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO sofascore_incident_events
                    (event_id, match_date, league, home_team, away_team, status_code, incident_count, fetched_at, error)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                      match_date=VALUES(match_date), league=VALUES(league),
                      home_team=VALUES(home_team), away_team=VALUES(away_team),
                      status_code=VALUES(status_code),
                      incident_count=VALUES(incident_count), fetched_at=VALUES(fetched_at), error=VALUES(error)
                """, (event_id, match_date, league, home_team, away_team, status_code, len(incidents), fetched_ts_meta, err))
                cur.close()
            else:
                db.execute("""
                    INSERT OR REPLACE INTO incident_events
                    (event_id, match_date, league, home_team, away_team, status_code, incident_count, fetched_at, error)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (event_id, match_date, league, home_team, away_team, status_code, len(incidents), fetched_ts_meta, err))

        except Exception as e:
            state["errors"] = int(state.get("errors", 0)) + 1
            err = str(e)
            if is_mysql:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO sofascore_incident_events
                    (event_id, match_date, league, home_team, away_team, status_code, incident_count, fetched_at, error)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                      match_date=VALUES(match_date), league=VALUES(league),
                      home_team=VALUES(home_team), away_team=VALUES(away_team),
                      status_code=VALUES(status_code),
                      incident_count=VALUES(incident_count), fetched_at=VALUES(fetched_at), error=VALUES(error)
                """, (event_id, match_date, league, home_team, away_team, status_code, 0, fetched_ts_meta, err))
                cur.close()
            else:
                db.execute("""
                    INSERT OR REPLACE INTO incident_events
                    (event_id, match_date, league, home_team, away_team, status_code, incident_count, fetched_at, error)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (event_id, match_date, league, home_team, away_team, status_code, 0, fetched_ts_meta, err))

        state["rows_upserted"] = int(state.get("rows_upserted", 0)) + upserted
        state["processed"] = int(state.get("processed", 0)) + 1
        state["last_event_id"] = event_id
        state["last_date"] = match_date
        processed_run += 1

        if processed_run % args.checkpoint_every == 0:
            if is_mysql:
                conn.commit()
            else:
                db.commit()
            save_state(state_path, state)
            pct = (processed_run / total) * 100 if total else 100
            print(f"[CHK] run={processed_run}/{total} ({pct:.2f}%) ok={state['ok']} 404={state['not_found']} err={state['errors']}")

        time.sleep(random.uniform(args.sleep_min, args.sleep_max))
        
        # Occasionally fetch decoy pages to look like human browsing
        if args.browse_every > 0 and processed_run % args.browse_every == 0:
            try:
                # Fetch a decoy page
                decoy_url = f"https://www.sofascore.com/"
                decoy_headers = build_html_headers()
                decoy_req = urllib.request.Request(decoy_url, headers=decoy_headers)
                with _get_opener().open(decoy_req, timeout=10) as resp:
                    # Just verify it works, don't store anything
                    _ = resp.read()
                counter.increment()  # Count decoy request too
                print(f"[DECOY] Browsed homepage")
                time.sleep(random.uniform(1.0, 2.0))
            except Exception:
                pass  # Ignore decoy failures
    
    # Check if approaching daily limit
    if counter.usage_pct >= 0.8 and processed_run % 100 == 0:
        print(f"[WARN] Daily usage: {counter.usage_pct*100:.1f}% ({counter.count}/{counter.daily_limit})")

    # Final check before exit
    print(f"[INFO] Requests today: {counter.count}/{counter.daily_limit} ({counter.usage_pct*100:.1f}%)")
    
    if is_mysql:
        conn.commit()
    else:
        db.commit()
        db.close()
    save_state(state_path, state)
    print("[DONE] incidents backfill completed")


if __name__ == "__main__":
    main()
