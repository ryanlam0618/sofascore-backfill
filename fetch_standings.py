#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fetch tournament standings from SofaScore.
Uses direct API: GET /tournament/{category_id}/season/{season_id}/standings/{type}
Types: total, home, away

Resume-safe with sqlite + state JSON.
Supports MySQL via USE_MYSQL=1 or --use-mysql flag.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from mysql_helpers import ensure_mysql_tables, mysql_connect, use_mysql

API_BASE = "https://www.sofascore.com/api/v1"

_proxy_opener = None


def _get_opener():
    global _proxy_opener
    if _proxy_opener is None:
        proxy = os.environ.get("SOFA_PROXY", "")
        if proxy:
            ph = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        else:
            ph = urllib.request.ProxyHandler({})
        _proxy_opener = urllib.request.build_opener(ph)
    return _proxy_opener


TOURNAMENT_IDS = {
    "premier-league": 1, "championship": 2, "laliga": 8, "la-liga": 8,
    "serie-a": 23, "bundesliga": 9, "ligue-1": 4,
    "uefa-champions-league": 7, "uefa-europa-league": 459,
    "j1-league": 94, "k-league-1": 245, "a-league": 314,
    "fa-cup": 164, "efl-cup": 17, "copa-del-rey": 205,
    "mls": 18,
}

UT_IDS = {
    "premier-league": 17, "laliga": 8, "serie-a": 23, "bundesliga": 9,
    "ligue-1": 34, "uefa-champions-league": 7, "uefa-europa-league": 459,
    "j1-league": 94, "k-league-1": 245, "a-league": 314,
}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def now_mysql():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def ensure_tables(conn, is_mysql=False):
    """Create SQLite tables (used when not using MySQL)."""
    if is_mysql:
        # Tables are created via ensure_mysql_tables() in main()
        return
    conn.execute("""
        CREATE TABLE IF NOT EXISTS standings_fetch_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          category_id INTEGER,
          season_id INTEGER,
          standing_type TEXT,
          tournament_name TEXT,
          fetched_at TEXT,
          status_code INTEGER,
          row_count INTEGER,
          error TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS standings (
          category_id INTEGER,
          tournament_name TEXT,
          season_id INTEGER,
          standing_type TEXT,
          position INTEGER,
          team_id INTEGER,
          team_name TEXT,
          team_short_name TEXT,
          played INTEGER DEFAULT 0,
          wins INTEGER DEFAULT 0,
          draws INTEGER DEFAULT 0,
          losses INTEGER DEFAULT 0,
          goals_for INTEGER DEFAULT 0,
          goals_against INTEGER DEFAULT 0,
          goal_diff INTEGER DEFAULT 0,
          points INTEGER DEFAULT 0,
          last_5 TEXT DEFAULT '',
          streak TEXT DEFAULT '',
          fetched_at TEXT,
          UNIQUE(category_id, season_id, standing_type, position, team_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_standings_key ON standings(category_id, season_id, standing_type)")
    conn.commit()


def load_state(path):
    if not path.exists():
        return {"started_at": now_iso(), "updated_at": now_iso(),
                "processed": 0, "ok": 0, "not_found": 0, "errors": 0,
                "standings_rows": 0, "last_category_id": None, "total_targets": 0}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path, state):
    state["updated_at"] = now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def to_int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


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
    try:
        with _get_opener().open(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise
    except Exception as e:
        raise


def get_season_id(category_id):
    try:
        j = api_get(f"tournament/{category_id}/seasons")
        seasons = j.get("seasons", [])
        if seasons and len(seasons) > 0:
            return seasons[0]["id"]
    except Exception:
        pass
    return 0


def parse_standings_row(row):
    team = row.get("team", {})
    return {
        "team_id": to_int(team.get("id")),
        "team_name": team.get("name", ""),
        "team_short_name": team.get("shortName", ""),
        "position": to_int(row.get("position")),
        "played": to_int(row.get("gamesPlayed")),
        "wins": to_int(row.get("wins")),
        "draws": to_int(row.get("draws")),
        "losses": to_int(row.get("losses")),
        "goals_for": to_int(row.get("goalsFor")),
        "goals_against": to_int(row.get("goalsAgainst")),
        "goal_diff": to_int(row.get("goalsDiff")),
        "points": to_int(row.get("points")),
        "last_5": json.dumps(row.get("form", [])),
        "streak": str(row.get("streak", "")),
    }


def fetch_standings(category_id, season_id, standing_type):
    """Returns (status_code, rows, tournament_name)."""
    rows = []
    tournament_name = ""
    try:
        try:
            j = api_get(f"tournament/{category_id}")
            tournament_name = j.get("tournament", {}).get("name", "")
        except Exception:
            pass

        if standing_type == "total":
            j = api_get(f"tournament/{category_id}/season/{season_id}/standings/total")
        else:
            j = api_get(f"tournament/{category_id}/season/{season_id}/standings/{standing_type}")

        standings_list = j.get("standings", [])
        for s in standings_list:
            stype = s.get("type", standing_type.upper())
            for row in s.get("rows", []):
                r = parse_standings_row(row)
                r["standing_type"] = stype
                r["tournament_name"] = tournament_name
                rows.append(r)

        return (200, rows, tournament_name)

    except urllib.error.HTTPError as e:
        return (e.code, [], tournament_name) if e.code == 404 else (404, [], tournament_name)
    except Exception:
        return (500, [], tournament_name)


# ── MySQL upsert helpers ────────────────────────────────────────────────────

def mysql_upsert_standings(conn, category_id, season_id, standing_type, tournament_name, r, fetched_ts):
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO sofascore_standings
        (category_id, tournament_name, season_id, standing_type, position,
         team_id, team_name, team_short_name, played, wins, draws, losses,
         goals_for, goals_against, goal_diff, points, last_5, streak, fetched_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
          tournament_name=VALUES(tournament_name), position=VALUES(position),
          team_name=VALUES(team_name), team_short_name=VALUES(team_short_name),
          played=VALUES(played), wins=VALUES(wins), draws=VALUES(draws),
          losses=VALUES(losses), goals_for=VALUES(goals_for), goals_against=VALUES(goals_against),
          goal_diff=VALUES(goal_diff), points=VALUES(points), last_5=VALUES(last_5),
          streak=VALUES(streak), fetched_at=VALUES(fetched_at)
    """, (
        category_id, r.get("tournament_name", tournament_name), season_id,
        r.get("standing_type", standing_type),
        to_int(r.get("position")),
        to_int(r.get("team_id")),
        r.get("team_name", ""),
        r.get("team_short_name", ""),
        to_int(r.get("played")),
        to_int(r.get("wins")),
        to_int(r.get("draws")),
        to_int(r.get("losses")),
        to_int(r.get("goals_for")),
        to_int(r.get("goals_against")),
        to_int(r.get("goal_diff")),
        to_int(r.get("points")),
        r.get("last_5", ""),
        r.get("streak", ""),
        fetched_ts,
    ))
    cur.close()


def mysql_log_standings(conn, category_id, season_id, standing_type, tournament_name, fetched_ts, status_code, local_rows, err):
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO sofascore_standings_fetch_log
        (category_id, season_id, standing_type, tournament_name, fetched_at, status_code, row_count, error)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE fetched_at=VALUES(fetched_at)
    """, (category_id, season_id, standing_type, tournament_name, fetched_ts, status_code, local_rows, err))
    cur.close()


def main():
    ap = argparse.ArgumentParser(description="Backfill tournament standings from SofaScore")
    ap.add_argument("--db", default="data/backfill_sofascore_10y/standings.sqlite")
    ap.add_argument("--state", default="data/backfill_sofascore_10y/standings_state.json")
    ap.add_argument("--sleep-min", type=float, default=1.0)
    ap.add_argument("--sleep-max", type=float, default=2.0)
    ap.add_argument("--category-id", type=int, nargs="+", default=[])
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--use-mysql", action="store_true",
                    help="Write to MySQL instead of SQLite")
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

    targets = []
    standing_types = ["total", "home", "away"]

    if args.category_id:
        cat_ids = args.category_id
    elif args.all:
        cat_ids = list(set(TOURNAMENT_IDS.values()))
    else:
        cat_ids = [1, 8, 9, 23]

    if args.limit and args.limit > 0:
        cat_ids = cat_ids[:args.limit]

    for cid in cat_ids:
        for stype in standing_types:
            targets.append((cid, stype))

    total = len(targets)
    state["total_targets"] = total
    save_state(state_path, state)
    print(f"[INFO] standings targets={total}")

    rows_upserted = 0

    for (category_id, standing_type) in targets:
        status_code = 0
        err = ""
        local_rows = 0
        season_id = 0
        tournament_name = ""

        try:
            season_id = get_season_id(category_id)
            if not season_id:
                state["not_found"] += 1
                status_code = 404
            else:
                status_code, rows, tournament_name = fetch_standings(category_id, season_id, standing_type)

                if status_code == 200 and rows:
                    fetched_ts = now_mysql() if is_mysql else now_iso()

                    if is_mysql:
                        for r in rows:
                            mysql_upsert_standings(conn, category_id, season_id, standing_type, tournament_name, r, fetched_ts)
                            local_rows += 1
                            rows_upserted += 1
                    else:
                        for r in rows:
                            conn.execute("""
                                INSERT OR REPLACE INTO standings
                                (category_id, tournament_name, season_id, standing_type, position,
                                 team_id, team_name, team_short_name, played, wins, draws, losses,
                                 goals_for, goals_against, goal_diff, points, last_5, streak, fetched_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, (
                                category_id, r.get("tournament_name", ""), season_id,
                                r.get("standing_type", standing_type),
                                to_int(r.get("position")),
                                to_int(r.get("team_id")),
                                r.get("team_name", ""),
                                r.get("team_short_name", ""),
                                to_int(r.get("played")),
                                to_int(r.get("wins")),
                                to_int(r.get("draws")),
                                to_int(r.get("losses")),
                                to_int(r.get("goals_for")),
                                to_int(r.get("goals_against")),
                                to_int(r.get("goal_diff")),
                                to_int(r.get("points")),
                                r.get("last_5", ""),
                                r.get("streak", ""),
                                fetched_ts,
                            ))
                            local_rows += 1
                            rows_upserted += 1
                    state["ok"] += 1
                    state["standings_rows"] += local_rows
                elif status_code == 404:
                    state["not_found"] += 1
                else:
                    state["errors"] += 1
                    err = f"HTTP {status_code}"

        except Exception as e:
            state["errors"] += 1
            err = str(e)

        if is_mysql:
            mysql_log_standings(conn, category_id, season_id, standing_type, tournament_name, now_mysql(), status_code, local_rows, err)
        else:
            conn.execute("""
                INSERT INTO standings_fetch_log
                (category_id, season_id, standing_type, tournament_name, fetched_at, status_code, row_count, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (category_id, season_id, standing_type, tournament_name, now_iso(), status_code, local_rows, err))

        state["processed"] += 1

        if state["processed"] % 20 == 0:
            if is_mysql:
                conn.commit()
            else:
                db.commit()
            save_state(state_path, state)
            pct = (state["processed"] / total) * 100
            print(f"[CHK] {state['processed']}/{total} ({pct:.0f}%) ok={state['ok']} 404={state['not_found']} err={state['errors']}")

        time.sleep(random.uniform(args.sleep_min, args.sleep_max))

    if is_mysql:
        conn.commit()
    else:
        conn.commit()
        conn.close()
    save_state(state_path, state)
    print(f"[DONE] standings: {rows_upserted} rows, ok={state['ok']}, 404={state['not_found']}, err={state['errors']}")


if __name__ == "__main__":
    main()