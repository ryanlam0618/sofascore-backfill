#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fetch head-to-head aggregated stats from SofaScore.

API: GET /event/{event_id}/h2h
Returns teamDuel with { homeWins, draws, awayWins } — aggregate stats, NOT individual events.
The allEvents array is empty in current SofaScore API.

Supports MySQL via USE_MYSQL=1 or --use-mysql flag.
Resume-safe with sqlite + state JSON.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import time
import urllib.request
import urllib.error
import urllib.error

_proxy_opener = None

def _get_opener():
    global _proxy_opener
    if _proxy_opener is None:
        # Load from .env if available
        env_path = "/root/.openclaw/workspace/.env"
        env = {}
        if os.path.exists(env_path):
            for line in open(env_path):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
        proxy = os.environ.get("SOFA_PROXY") or env.get("SOFA_PROXY", "")
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
    uas = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:125.0) Gecko/20100101 Firefox/125.0",
    ]
    return random.choice(uas)


def _default_headers():
    return {
        "User-Agent": _random_ua(),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6,zh-HK;q=0.5",
        "Accept-Encoding": "gzip, deflate",
    }


def api_get(path):
    url = f"{API_BASE}/{path.lstrip('/')}"
    req = urllib.request.Request(url, headers=_default_headers())
    resp = urllib.request.urlopen(req, timeout=15)
    return json.loads(resp.read())


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def now_mysql():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def to_int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def ensure_tables(conn, is_mysql=False):
    if is_mysql:
        return
    conn.execute("""
        CREATE TABLE IF NOT EXISTS related_matches_fetch_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source_event_id INTEGER,
          fetched_at TEXT,
          status_code INTEGER,
          error TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS related_matches (
          source_event_id INTEGER,
          home_team_id INTEGER,
          home_team_name TEXT,
          away_team_id INTEGER,
          away_team_name TEXT,
          league_category_id INTEGER,
          league_name TEXT,
          match_timestamp INTEGER,
          home_wins INTEGER DEFAULT 0,
          draws INTEGER DEFAULT 0,
          away_wins INTEGER DEFAULT 0,
          total_h2h INTEGER DEFAULT 0,
          fetched_at TEXT,
          UNIQUE(source_event_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rel_src ON related_matches(source_event_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rel_teams ON related_matches(home_team_id, away_team_id)")
    conn.commit()


def load_state(path):
    if not path.exists():
        return {"started_at": now_iso(), "updated_at": now_iso(),
                "processed": 0, "ok": 0, "not_found": 0, "errors": 0,
                "h2h_rows": 0, "last_event_id": None, "total_targets": 0}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path, state):
    state["updated_at"] = now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def get_past_event_ids(limit=100):
    """Fetch past event IDs from PL season (source of event IDs for H2H)."""
    events = []
    try:
        j = api_get("tournament/1/season/76986/events")
        all_evs = j.get("events", []) if isinstance(j, dict) else (j if isinstance(j, list) else [])
        now = datetime.now().timestamp()
        past = [e for e in all_evs if e.get("startTimestamp", 9999999999) < now]
        all_events = past + [e for e in all_evs if e not in past]
        for e in all_events[:limit]:
            events.append({
                "event_id": to_int(e.get("id")),
                "home_team_id": to_int(e.get("homeTeam", {}).get("id")),
                "home_team_name": e.get("homeTeam", {}).get("name", ""),
                "away_team_id": to_int(e.get("awayTeam", {}).get("id")),
                "away_team_name": e.get("awayTeam", {}).get("name", ""),
                "league_category_id": to_int(e.get("tournament", {}).get("id") if isinstance(e.get("tournament"), dict) else 1),
                "league_name": e.get("tournament", {}).get("name", "Premier League") if isinstance(e.get("tournament"), dict) else "Premier League",
                "match_timestamp": to_int(e.get("startTimestamp")),
            })
    except Exception:
        pass
    return events


def fetch_h2h(event_id):
    """Fetch H2H aggregated stats via /event/{id}/h2h."""
    try:
        j = api_get(f"event/{event_id}/h2h")
        td = j.get("teamDuel", {})

        home_wins = to_int(td.get("homeWins"))
        draws = to_int(td.get("draws"))
        away_wins = to_int(td.get("awayWins"))
        total = home_wins + draws + away_wins

        return (200, {
            "home_wins": home_wins,
            "draws": draws,
            "away_wins": away_wins,
            "total_h2h": total,
        })

    except urllib.error.HTTPError as e:
        return (e.code if e.code != 404 else 404, None)
    except Exception:
        return (500, None)


# ── MySQL upsert helpers ─────────────────────────────────────────────────────

def mysql_upsert_h2h(conn, event_id, home_id, home_name, away_id, away_name,
                     cat_id, league_name, ts, h2h_data, fetched_ts):
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO sofascore_related_matches
        (source_event_id, home_team_id, home_team_name, away_team_id, away_team_name,
         league_category_id, league_name, match_timestamp, home_wins, draws, away_wins,
         total_h2h, fetched_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
          home_team_name=VALUES(home_team_name), away_team_name=VALUES(away_team_name),
          league_name=VALUES(league_name), match_timestamp=VALUES(match_timestamp),
          home_wins=VALUES(home_wins), draws=VALUES(draws), away_wins=VALUES(away_wins),
          total_h2h=VALUES(total_h2h), fetched_at=VALUES(fetched_at)
    """, (
        event_id, home_id, home_name, away_id, away_name,
        cat_id, league_name, ts,
        h2h_data.get("home_wins", 0), h2h_data.get("draws", 0),
        h2h_data.get("away_wins", 0), h2h_data.get("total_h2h", 0),
        fetched_ts,
    ))
    cur.close()


def mysql_log_h2h(conn, event_id, fetched_ts, status_code, err):
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO sofascore_related_matches_fetch_log
        (source_event_id, fetched_at, status_code, error)
        VALUES (%s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE fetched_at=VALUES(fetched_at)
    """, (event_id, fetched_ts, status_code, err))
    cur.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/backfill_sofascore_10y/related_matches.sqlite")
    ap.add_argument("--state", default="data/backfill_sofascore_10y/related_matches_state.json")
    ap.add_argument("--sleep-min", type=float, default=1.0)
    ap.add_argument("--sleep-max", type=float, default=2.0)
    ap.add_argument("--event-id", type=int, nargs="+", default=[])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--source-league", default="pl",
                    help="Source league for event IDs (pl=PL, laliga=La Liga, etc.)")
    ap.add_argument("--use-mysql", action="store_true", help="Write to MySQL instead of SQLite")
    args = ap.parse_args()

    is_mysql = args.use_mysql or use_mysql()

    if is_mysql:
        conn = mysql_connect()
        ensure_mysql_tables(conn)
        print(f"[INFO] Using MySQL (database: appdb)")
    else:
        conn = sqlite3.connect(args.db)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=60000")
        ensure_tables(conn, is_mysql=False)
        print(f"[INFO] Using SQLite: {args.db}")

    state_path = Path(args.state)
    state = load_state(state_path)

    targets = args.event_id

    if not targets:
        events = get_past_event_ids(limit=500)
        targets = [e["event_id"] for e in events]

    if args.limit and args.limit > 0:
        targets = targets[:args.limit]

    if not targets:
        print("[INFO] No event IDs. Use --event-id to specify.")
        return

    state["total_targets"] = len(targets)
    save_state(state_path, state)
    print(f"[INFO] related_matches (H2H) targets={len(targets)}")

    rows_upserted = 0

    for event_id in targets:
        status_code = 0
        err = ""
        local_rows = 0
        h2h_data = None
        try:
            status_code, h2h_data = fetch_h2h(event_id)

            if status_code == 200 and h2h_data and h2h_data.get("total_h2h", 0) > 0:
                try:
                    ev_j = api_get(f"event/{event_id}")
                    ev = ev_j.get("event", ev_j)
                    home_id = to_int(ev.get("homeTeam", {}).get("id"))
                    home_name = ev.get("homeTeam", {}).get("name", "")
                    away_id = to_int(ev.get("awayTeam", {}).get("id"))
                    away_name = ev.get("awayTeam", {}).get("name", "")
                    ts = to_int(ev.get("startTimestamp"))
                    tournament = ev.get("tournament", {})
                    if isinstance(tournament, dict):
                        cat_id = to_int(tournament.get("id"))
                        league_name = tournament.get("name", "")
                    else:
                        cat_id = 1
                        league_name = "Premier League"
                except Exception:
                    home_id = 0; home_name = ""; away_id = 0; away_name = ""
                    ts = 0; cat_id = 1; league_name = "Unknown"

                fetched_ts = now_mysql() if is_mysql else now_iso()

                if is_mysql:
                    mysql_upsert_h2h(conn, event_id, home_id, home_name, away_id, away_name,
                                     cat_id, league_name, ts, h2h_data, fetched_ts)
                else:
                    conn.execute("""
                        INSERT OR REPLACE INTO related_matches
                        (source_event_id, home_team_id, home_team_name, away_team_id, away_team_name,
                         league_category_id, league_name, match_timestamp, home_wins, draws, away_wins,
                         total_h2h, fetched_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        event_id, home_id, home_name, away_id, away_name,
                        cat_id, league_name, ts,
                        h2h_data.get("home_wins", 0), h2h_data.get("draws", 0),
                        h2h_data.get("away_wins", 0), h2h_data.get("total_h2h", 0),
                        fetched_ts,
                    ))
                local_rows = 1
                rows_upserted += 1
                state["ok"] += 1
                state["h2h_rows"] += local_rows
            elif status_code == 404:
                state["not_found"] += 1
            else:
                state["errors"] += 1
                err = f"HTTP {status_code}"

        except Exception as e:
            state["errors"] += 1
            err = str(e)

        if is_mysql:
            mysql_log_h2h(conn, event_id, now_mysql(), status_code, err)
        else:
            conn.execute(
                "INSERT INTO related_matches_fetch_log (source_event_id, fetched_at, status_code, error) "
                "VALUES (?, ?, ?, ?)",
                (event_id, now_iso(), status_code, err))
        state["processed"] += 1

        if state["processed"] % 50 == 0:
            if is_mysql:
                conn.commit()
            else:
                conn.commit()
            save_state(state_path, state)
            pct = (state["processed"] / len(targets)) * 100
            print(f"[CHK] {state['processed']}/{len(targets)} ({pct:.0f}%) ok={state['ok']} 404={state['not_found']} err={state['errors']}")

        time.sleep(random.uniform(args.sleep_min, args.sleep_max))

    if is_mysql:
        conn.commit()
    else:
        conn.commit()
        conn.close()
    save_state(state_path, state)
    print(f"[DONE] related_matches: {rows_upserted} rows with H2H data, ok={state['ok']}, 404={state['not_found']}, err={state['errors']}")


if __name__ == "__main__":
    main()