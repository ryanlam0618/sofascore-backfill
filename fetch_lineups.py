#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fetch match lineups from SofaScore via direct API.

API: GET https://www.sofascore.com/api/v1/event/{event_id}/lineups
Status: ✅ Direct API confirmed working (2026-05-03)
Returns: { confirmed, home { players, team }, away { players, team }, statisticalVersion }

Resume-safe with sqlite + state JSON.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_BASE = "https://www.sofascore.com/api/v1"


def api_get(path: str) -> tuple[int, dict]:
    url = f"{API_BASE}/{path.lstrip('/')}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception:
        return 500, {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lineups (
          event_id INTEGER,
          is_home INTEGER,
          team_id INTEGER,
          team_name TEXT,
          player_id INTEGER,
          player_name TEXT,
          position TEXT,
          position_type TEXT,
          jersey_number INTEGER,
          captain INTEGER,
          player_key TEXT,
          fetched_at TEXT,
          PRIMARY KEY (event_id, is_home, player_key)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lineup_events (
          event_id INTEGER PRIMARY KEY,
          status_code INTEGER,
          home_count INTEGER,
          away_count INTEGER,
          confirmed INTEGER,
          fetched_at TEXT,
          error TEXT
        )
    """)
    conn.commit()


def load_state(path: Path) -> dict:
    if not path.exists():
        return {
            "started_at": now_iso(), "updated_at": now_iso(),
            "processed": 0, "ok": 0, "not_found": 0, "errors": 0,
            "players_upserted": 0, "last_event_id": None, "total_targets": 0,
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


def fetch_lineups(event_id: int) -> tuple[int, dict]:
    """Fetch lineups for an event. Returns (status_code, lineup_payload)."""
    return api_get(f"event/{event_id}/lineups")


def upsert_lineup_player(conn, event_id, is_home, team_id, team_name, player):
    player_info = player.get("player", {}) if isinstance(player.get("player"), dict) else player
    player_id = to_int(player.get("playerId") or player_info.get("id") or player.get("id"))
    if player_id is None:
        return False
    player_name = player.get("playerName") or player_info.get("name") or player_info.get("shortName")
    position = player.get("position") or player.get("positionName") or player.get("name")
    position_type = player.get("positionType") or player.get("type")
    jersey_number = to_int(player.get("jerseyNumber"))
    captain = 1 if player.get("captain") is True else 0
    player_key = str(player_id)

    conn.execute("""
        INSERT OR REPLACE INTO lineups
        (event_id, is_home, team_id, team_name, player_id, player_name,
         position, position_type, jersey_number, captain, player_key, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        event_id, is_home, team_id, team_name, player_id, player_name,
        position, position_type, jersey_number, captain, player_key, now_iso(),
    ))
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill match lineups from SofaScore (direct API)")
    ap.add_argument("--db", default="data/backfill_sofascore_10y/lineups.sqlite")
    ap.add_argument("--state", default="data/backfill_sofascore_10y/lineups_state.json")
    ap.add_argument("--sleep-min", type=float, default=0.3)
    ap.add_argument("--sleep-max", type=float, default=0.6)
    ap.add_argument("--checkpoint-every", type=int, default=100)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--event-id", type=int, nargs="+", default=[],
                    help="Specific event IDs")
    ap.add_argument("--category-id", type=int, default=1)
    ap.add_argument("--season-id", type=int, default=61627)
    ap.add_argument("--past-only", action="store_true", default=True)
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=60000")
    ensure_tables(db)

    state_path = Path(args.state)
    state = load_state(state_path)

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

    done_ids = set(r[0] for r in db.execute("SELECT event_id FROM lineup_events").fetchall())
    targets = [eid for eid in targets if eid not in done_ids]

    if args.limit and args.limit > 0:
        targets = targets[:args.limit]

    total = len(targets)
    state["total_targets"] = total
    save_state(state_path, state)
    print(f"[INFO] lineup targets={total}")

    if total == 0:
        print("[INFO] Nothing to process")
        return

    processed_run = 0

    for event_id in targets:
        status_code = 0
        err = ""
        upserted = 0

        try:
            status_code, payload = fetch_lineups(event_id)

            if status_code == 200:
                confirmed = 1 if payload.get("confirmed") is True else 0
                for is_home, team_key in [(1, "home"), (0, "away")]:
                    team_block = payload.get(team_key, {})
                    team_id = to_int(team_block.get("id") or team_block.get("team", {}).get("id"))
                    team_name = team_block.get("name") or team_block.get("team", {}).get("name") or ""
                    players = team_block.get("players", []) or []
                    for player in players:
                        if upsert_lineup_player(db, event_id, is_home, team_id, team_name, player):
                            upserted += 1
                state["ok"] = int(state.get("ok", 0)) + 1
                home_count = len(payload.get("home", {}).get("players", []))
                away_count = len(payload.get("away", {}).get("players", []))
                db.execute("""
                    INSERT OR REPLACE INTO lineup_events
                    (event_id, status_code, home_count, away_count, confirmed, fetched_at, error)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (event_id, status_code, home_count, away_count, confirmed, now_iso(), ""))
            elif status_code == 404:
                state["not_found"] = int(state.get("not_found", 0)) + 1
                err = "not found"
                db.execute("""
                    INSERT OR REPLACE INTO lineup_events
                    (event_id, status_code, home_count, away_count, confirmed, fetched_at, error)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (event_id, status_code, 0, 0, 0, now_iso(), err))
            else:
                state["errors"] = int(state.get("errors", 0)) + 1
                err = f"HTTP {status_code}"
                db.execute("""
                    INSERT OR REPLACE INTO lineup_events
                    (event_id, status_code, home_count, away_count, confirmed, fetched_at, error)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (event_id, status_code, 0, 0, 0, now_iso(), err))

        except Exception as e:
            state["errors"] = int(state.get("errors", 0)) + 1
            err = str(e)
            db.execute("""
                INSERT OR REPLACE INTO lineup_events
                (event_id, status_code, home_count, away_count, confirmed, fetched_at, error)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (event_id, status_code, 0, 0, 0, now_iso(), err))

        state["players_upserted"] = int(state.get("players_upserted", 0)) + upserted
        state["processed"] = int(state.get("processed", 0)) + 1
        state["last_event_id"] = event_id
        processed_run += 1

        if processed_run % args.checkpoint_every == 0:
            db.commit()
            save_state(state_path, state)
            pct = (processed_run / total) * 100 if total else 100
            print(f"[CHK] run={processed_run}/{total} ({pct:.2f}%) "
                  f"ok={state['ok']} 404={state['not_found']} err={state['errors']} "
                  f"players={state['players_upserted']}")

        time.sleep(random.uniform(args.sleep_min, args.sleep_max))

    db.commit()
    save_state(state_path, state)
    print(f"[DONE] lineups completed: players={state['players_upserted']}")


if __name__ == "__main__":
    main()