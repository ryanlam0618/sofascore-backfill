#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Backfill detailed shotmap rows (resume-safe) via direct API.
- Reads event list from shotmap_xg_backfill where has_shotmap=1
- Fetches /api/v1/event/{event_id}/shotmap
- Stores per-shot rows in shotmap_details table

API: GET https://www.sofascore.com/api/v1/event/{event_id}/shotmap
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


def load_state(path: Path) -> dict:
    if not path.exists():
        return {
            "started_at": now_iso(), "updated_at": now_iso(),
            "processed": 0, "ok": 0, "not_found": 0, "errors": 0,
            "rows_upserted": 0, "last_event_id": None, "total_targets": 0,
        }
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict) -> None:
    state["updated_at"] = now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_tables(conn: sqlite3.Connection | None = None, mysql_conn=None) -> None:
    """Create SQLite tables. MySQL tables created via ensure_mysql_tables()."""
    if conn is None:
        return
    conn.execute("""
        CREATE TABLE IF NOT EXISTS shotmap_detail_events (
          event_id INTEGER PRIMARY KEY,
          status_code INTEGER,
          shot_count INTEGER,
          fetched_at TEXT,
          error TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS shotmap_details (
          event_id INTEGER,
          shot_id INTEGER,
          is_home_shot INTEGER,
          team_id INTEGER,
          team_name TEXT,
          player_id INTEGER,
          player_name TEXT,
          player_position TEXT,
          minute INTEGER,
          added_time INTEGER,
          time_seconds INTEGER,
          incident_type TEXT,
          shot_type TEXT,
          situation TEXT,
          body_part TEXT,
          goal_mouth_location TEXT,
          player_x REAL,
          player_y REAL,
          xg REAL,
          home_team_goal_prob REAL,
          away_team_goal_prob REAL,
          fetched_at TEXT,
          error TEXT,
          PRIMARY KEY (event_id, shot_id)
        )
    """)
    conn.commit()


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


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill detailed shotmap rows (direct API)")
    ap.add_argument("--db", default="data/backfill_sofascore_10y/shotmap_details.sqlite")
    ap.add_argument("--state", default="data/backfill_sofascore_10y/shotmap_details_state.json")
    ap.add_argument("--sleep-min", type=float, default=0.3)
    ap.add_argument("--sleep-max", type=float, default=0.6)
    ap.add_argument("--checkpoint-every", type=int, default=100)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--source-db", default="data/backfill_sofascore_10y/shotmap_xg.sqlite",
                    help="DB with shotmap_xg_backfill table to read event_ids from")
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
        ensure_tables(conn=db)
        conn = db
        print(f"[INFO] Using SQLite: {args.db}")

    state_path = Path(args.state)
    state = load_state(state_path)

    # Get event IDs from source shotmap DB
    try:
        if is_mysql:
            # Read from MySQL source table
            cur = conn.cursor(dictionary=True)
            cur.execute("SELECT event_id FROM sofascore_shotmap_xg_backfill WHERE has_shotmap=1")
            all_rows = cur.fetchall()
            cur.close()
            # MySQL dedup by ON DUPLICATE KEY
            targets = [(int(r["event_id"]),) for r in all_rows]
        else:
            src_db = sqlite3.connect(args.source_db)
            src_db.row_factory = sqlite3.Row
            done_ids = set(r[0] for r in db.execute("SELECT event_id FROM shotmap_detail_events").fetchall())
            rows = src_db.execute(
                "SELECT event_id FROM shotmap_xg_backfill WHERE has_shotmap=1"
            ).fetchall()
            targets = [r for r in rows if int(r["event_id"]) not in done_ids]
            src_db.close()
    except Exception as e:
        print(f"[ERROR] Could not read source DB: {e}")
        targets = []

    if args.limit and args.limit > 0:
        targets = targets[:args.limit]

    total = len(targets)
    state["total_targets"] = total
    save_state(state_path, state)
    print(f"[INFO] targets={total}")

    if total == 0:
        print("[INFO] Nothing to process")
        return

    processed_run = 0

    for row_data in targets:
        event_id = int(row_data[0]) if isinstance(row_data, tuple) else int(row_data["event_id"])
        status_code = 0
        err = ""
        upserted = 0

        try:
            status_code, payload = api_get(f"event/{event_id}/shotmap")
            shots = []
            if status_code == 200:
                shots = (payload or {}).get("shotmap") or []
                for sh in shots:
                    player_info = sh.get("player", {})
                    team_info = sh.get("team", {})
                    coords = sh.get("playerCoordinates", {}) or sh.get("coordinates", {})
                    fetched_ts = now_mysql() if is_mysql else now_iso()

                    if is_mysql:
                        cur = conn.cursor()
                        cur.execute("""
                            INSERT INTO sofascore_shotmap_details
                            (event_id, shot_id, is_home_shot,
                             team_id, team_name,
                             player_id, player_name, player_position,
                             minute, added_time, time_seconds,
                             incident_type, shot_type, situation, body_part, goal_mouth_location,
                             player_x, player_y, xg,
                             home_team_goal_prob, away_team_goal_prob,
                             fetched_at, error)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON DUPLICATE KEY UPDATE
                              is_home_shot=VALUES(is_home_shot), team_id=VALUES(team_id),
                              team_name=VALUES(team_name), player_id=VALUES(player_id),
                              player_name=VALUES(player_name), player_position=VALUES(player_position),
                              minute=VALUES(minute), added_time=VALUES(added_time),
                              time_seconds=VALUES(time_seconds), incident_type=VALUES(incident_type),
                              shot_type=VALUES(shot_type), situation=VALUES(situation),
                              body_part=VALUES(body_part), goal_mouth_location=VALUES(goal_mouth_location),
                              player_x=VALUES(player_x), player_y=VALUES(player_y),
                              xg=VALUES(xg), home_team_goal_prob=VALUES(home_team_goal_prob),
                              away_team_goal_prob=VALUES(away_team_goal_prob),
                              fetched_at=VALUES(fetched_at), error=VALUES(error)
                        """, (
                            event_id,
                            to_int(sh.get("id")),
                            1 if sh.get("isHome") is True else 0 if sh.get("isHome") is False else None,
                            to_int(team_info.get("id") if isinstance(team_info, dict) else None),
                            team_info.get("name") if isinstance(team_info, dict) else None,
                            to_int(player_info.get("id") if isinstance(player_info, dict) else None),
                            player_info.get("name") if isinstance(player_info, dict) else None,
                            player_info.get("position") if isinstance(player_info, dict) else None,
                            to_int(sh.get("time")),
                            to_int(sh.get("addedTime")),
                            to_int(sh.get("timeSeconds")),
                            sh.get("incidentType"),
                            sh.get("shotType"),
                            sh.get("situation"),
                            sh.get("bodyPart"),
                            sh.get("goalMouthLocation"),
                            to_float(coords.get("x") if isinstance(coords, dict) else None),
                            to_float(coords.get("y") if isinstance(coords, dict) else None),
                            to_float(sh.get("xg")),
                            to_float(sh.get("homeTeamGoalProbability")),
                            to_float(sh.get("awayTeamGoalProbability")),
                            fetched_ts,
                            "",
                        ))
                        cur.close()
                    else:
                        conn.execute("""
                            INSERT OR REPLACE INTO shotmap_details
                            (event_id, shot_id, is_home_shot,
                             team_id, team_name,
                             player_id, player_name, player_position,
                             minute, added_time, time_seconds,
                             incident_type, shot_type, situation, body_part, goal_mouth_location,
                             player_x, player_y, xg,
                             home_team_goal_prob, away_team_goal_prob,
                             fetched_at, error)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            event_id,
                            to_int(sh.get("id")),
                            1 if sh.get("isHome") is True else 0 if sh.get("isHome") is False else None,
                            to_int(team_info.get("id") if isinstance(team_info, dict) else None),
                            team_info.get("name") if isinstance(team_info, dict) else None,
                            to_int(player_info.get("id") if isinstance(player_info, dict) else None),
                            player_info.get("name") if isinstance(player_info, dict) else None,
                            player_info.get("position") if isinstance(player_info, dict) else None,
                            to_int(sh.get("time")),
                            to_int(sh.get("addedTime")),
                            to_int(sh.get("timeSeconds")),
                            sh.get("incidentType"),
                            sh.get("shotType"),
                            sh.get("situation"),
                            sh.get("bodyPart"),
                            sh.get("goalMouthLocation"),
                            to_float(coords.get("x") if isinstance(coords, dict) else None),
                            to_float(coords.get("y") if isinstance(coords, dict) else None),
                            to_float(sh.get("xg")),
                            to_float(sh.get("homeTeamGoalProbability")),
                            to_float(sh.get("awayTeamGoalProbability")),
                            fetched_ts,
                            "",
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
                    INSERT INTO sofascore_shotmap_detail_events
                    (event_id, status_code, shot_count, fetched_at, error)
                    VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE status_code=VALUES(status_code),
                      shot_count=VALUES(shot_count), fetched_at=VALUES(fetched_at), error=VALUES(error)
                """, (event_id, status_code, len(shots), now_mysql(), err))
                cur.close()
            else:
                conn.execute("""
                    INSERT OR REPLACE INTO shotmap_detail_events
                    (event_id, status_code, shot_count, fetched_at, error)
                    VALUES (?, ?, ?, ?, ?)
                """, (event_id, status_code, len(shots), now_iso(), err))

        except Exception as e:
            state["errors"] = int(state.get("errors", 0)) + 1
            err = str(e)
            if is_mysql:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO sofascore_shotmap_detail_events
                    (event_id, status_code, shot_count, fetched_at, error)
                    VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE status_code=VALUES(status_code),
                      fetched_at=VALUES(fetched_at), error=VALUES(error)
                """, (event_id, status_code, 0, now_mysql(), err))
                cur.close()
            else:
                conn.execute("""
                    INSERT OR REPLACE INTO shotmap_detail_events
                    (event_id, status_code, shot_count, fetched_at, error)
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
                conn.commit()
            save_state(state_path, state)
            pct = (processed_run / total) * 100 if total else 100
            print(f"[CHK] run={processed_run}/{total} ({pct:.2f}%) "
                  f"ok={state['ok']} 404={state['not_found']} err={state['errors']} "
                  f"upserted={state['rows_upserted']}")

        time.sleep(random.uniform(args.sleep_min, args.sleep_max))

    if is_mysql:
        conn.commit()
    else:
        conn.commit()
        conn.close()
    save_state(state_path, state)
    print("[DONE] shotmap_details completed")


if __name__ == "__main__":
    main()