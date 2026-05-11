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


def api_get(path: str) -> tuple[int, dict]:
    url = f"{API_BASE}/{path.lstrip('/')}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with _get_opener().open(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, {}
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
    ap.add_argument("--sleep-min", type=float, default=0.3)
    ap.add_argument("--sleep-max", type=float, default=0.6)
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
                            "", "", "", "",
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
                            "", "", "", "",
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
                    (event_id, status_code, incident_count, fetched_at, error)
                    VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE status_code=VALUES(status_code),
                      incident_count=VALUES(incident_count), fetched_at=VALUES(fetched_at), error=VALUES(error)
                """, (event_id, status_code, len(incidents), now_mysql(), err))
                cur.close()
            else:
                db.execute("""
                    INSERT OR REPLACE INTO incident_events
                    (event_id, status_code, incident_count, fetched_at, error)
                    VALUES (?, ?, ?, ?, ?)
                """, (event_id, status_code, len(incidents), now_iso(), err))

        except Exception as e:
            state["errors"] = int(state.get("errors", 0)) + 1
            err = str(e)
            if is_mysql:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO sofascore_incident_events
                    (event_id, status_code, incident_count, fetched_at, error)
                    VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE status_code=VALUES(status_code),
                      incident_count=VALUES(incident_count), fetched_at=VALUES(fetched_at), error=VALUES(error)
                """, (event_id, status_code, 0, now_mysql(), err))
                cur.close()
            else:
                db.execute("""
                    INSERT OR REPLACE INTO incident_events
                    (event_id, status_code, incident_count, fetched_at, error)
                    VALUES (?, ?, ?, ?, ?)
                """, (event_id, status_code, 0, now_iso(), err))

        state["rows_upserted"] = int(state.get("rows_upserted", 0)) + upserted
        state["processed"] = int(state.get("processed", 0)) + 1
        state["last_event_id"] = event_id
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

    if is_mysql:
        conn.commit()
    else:
        db.commit()
        db.close()
    save_state(state_path, state)
    print("[DONE] incidents backfill completed")


if __name__ == "__main__":
    main()