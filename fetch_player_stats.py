#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fetch player season statistics from SofaScore.

Uses two API calls per player:
  1. GET /unique-tournament/{ut_id}/season/{season_id}/players?order_by={stat}&limit=50
     → Returns ranked player list (playerId, name, team) — NO actual stat values
  2. GET /player/{player_id}/statistics
     → Returns full stats for that player (goals, assists, xG, apps, etc.)

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
import urllib.error

_proxy_opener = None
_proxy_list = None
_last_proxy_idx = -1

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
            idx = (_last_proxy_idx + 1) % len(proxies)
            _last_proxy_idx = idx
            proxy = proxies[idx]
            print(f"[PROXY] Using: {proxy.split('@')[1] if '@' in proxy else proxy}")
        else:
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

TOURNAMENT_IDS = {
    "premier-league": 1, "championship": 2, "laliga": 8, "la-liga": 8,
    "serie-a": 23, "bundesliga": 9, "ligue-1": 4,
    "uefa-champions-league": 7, "uefa-europa-league": 459,
    "j1-league": 94, "k-league-1": 245, "a-league": 314,
}

CAT_TO_UT = {
    1: 17,   2: 18,   3: 24,   8: 8,    23: 23,  9: 9,    4: 34,
    7: 7,    459: 459, 94: 94,  245: 245, 314: 314,
}

STAT_TYPES = [
    "goals", "assists", "xg", "xa", "minutes",
    "appearances", "yellowCards", "redCards",
    "goalsByPenalties", "shotsOnTarget",
]

# Short names map to actual API order_by values
STAT_TYPE_SHORT = {
    "minutes_played": "minutes",
    "yellow_cards": "yellowCards",
    "red_cards": "redCards",
    "goals_assists_total": "goals",  # placeholder, will fetch both
}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def now_mysql():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def ensure_tables(conn, is_mysql=False):
    if is_mysql:
        return  # Tables created via ensure_mysql_tables()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS player_stats_fetch_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          category_id INTEGER,
          ut_id INTEGER,
          season_id INTEGER,
          stat_type TEXT,
          fetched_at TEXT,
          status_code INTEGER,
          player_count INTEGER,
          error TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS player_season_stats (
          category_id INTEGER,
          ut_id INTEGER,
          season_id INTEGER,
          stat_type TEXT,
          rank INTEGER,
          player_id INTEGER,
          player_name TEXT,
          player_position TEXT,
          team_id INTEGER,
          team_name TEXT,
          goals INTEGER DEFAULT 0,
          assists INTEGER DEFAULT 0,
          appearances INTEGER DEFAULT 0,
          minutes_played INTEGER DEFAULT 0,
          xg REAL DEFAULT 0,
          xa REAL DEFAULT 0,
          yellow_cards INTEGER DEFAULT 0,
          red_cards INTEGER DEFAULT 0,
          fetched_at TEXT,
          UNIQUE(category_id, season_id, stat_type, player_id)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ps ON player_season_stats(category_id, season_id, stat_type)"
    )
    conn.commit()


def load_state(path):
    if not path.exists():
        return {
            "started_at": now_iso(), "updated_at": now_iso(),
            "processed": 0, "ok": 0, "not_found": 0, "errors": 0,
            "stat_rows": 0, "last_category_id": None, "total_targets": 0,
        }
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


def to_float(v, default=0.0):
    try:
        return float(v)
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
        # Note: No Accept-Encoding — server may return gzip but urlopen auto-decompresses
    }


def api_get(path):
    url = f"{API_BASE}/{path.lstrip('/')}"
    req = urllib.request.Request(url, headers=_default_headers())
    resp = _get_opener().open(req, timeout=15)
    return json.loads(resp.read())


def get_season_id(ut_id):
    try:
        j = api_get(f"unique-tournament/{ut_id}/seasons")
        seasons = j.get("seasons", [])
        if seasons and len(seasons) > 0:
            return seasons[0]["id"]
    except Exception:
        pass
    return 0


def fetch_player_stats_for_season(player_id, target_season_id, target_ut_id):
    """Fetch /player/{id}/statistics, return stats for the target season+UT."""
    try:
        j = api_get(f"player/{player_id}/statistics")
        seasons = j.get("seasons", [])
        for s in seasons:
            ut = s.get("uniqueTournament", {})
            stats = s.get("statistics", {})
            ut_id_val = to_int(ut.get("id") if isinstance(ut, dict) else ut)
            season_id_val = to_int(s.get("season", {}).get("id") if isinstance(s.get("season"), dict) else s.get("season", {}).get("id"))

            if ut_id_val == target_ut_id or ut_id_val == 0:
                if target_season_id and season_id_val and season_id_val != target_season_id:
                    continue
                return {
                    "goals": to_int(stats.get("goals")),
                    "assists": to_int(stats.get("assists")),
                    "appearances": to_int(stats.get("appearances")),
                    "minutes_played": to_int(stats.get("minutesPlayed")),
                    "xg": to_float(stats.get("expectedGoals")),
                    "xa": to_float(stats.get("expectedAssists")),
                    "yellow_cards": to_int(stats.get("yellowCards")),
                    "red_cards": to_int(stats.get("redCards")),
                }

        for s in seasons:
            stats = s.get("statistics", {})
            season_id_val = to_int(s.get("season", {}).get("id") if isinstance(s.get("season"), dict) else 0)
            if target_season_id and season_id_val == target_season_id:
                return {
                    "goals": to_int(stats.get("goals")),
                    "assists": to_int(stats.get("assists")),
                    "appearances": to_int(stats.get("appearances")),
                    "minutes_played": to_int(stats.get("minutesPlayed")),
                    "xg": to_float(stats.get("expectedGoals")),
                    "xa": to_float(stats.get("expectedAssists")),
                    "yellow_cards": to_int(stats.get("yellowCards")),
                    "red_cards": to_int(stats.get("redCards")),
                }

        if seasons:
            s = seasons[0]
            stats = s.get("statistics", {})
            return {
                "goals": to_int(stats.get("goals")),
                "assists": to_int(stats.get("assists")),
                "appearances": to_int(stats.get("appearances")),
                "minutes_played": to_int(stats.get("minutesPlayed")),
                "xg": to_float(stats.get("expectedGoals")),
                "xa": to_float(stats.get("expectedAssists")),
                "yellow_cards": to_int(stats.get("yellowCards")),
                "red_cards": to_int(stats.get("redCards")),
            }
    except Exception:
        pass
    return None


def fetch_top_players_with_stats(ut_id, season_id, stat_type, get_stats=False):
    """Fetch top players ranked by stat. If get_stats=True, also fetch per-player stats."""
    rows = []
    ut_name = ""

    try:
        try:
            j = api_get(f"unique-tournament/{ut_id}")
            ut_name = j.get("uniqueTournament", {}).get("name", "")
        except Exception:
            pass

        j = api_get(f"unique-tournament/{ut_id}/season/{season_id}/players?order_by={stat_type}&limit=50")
        players = j.get("players", [])

        for rank, p in enumerate(players, 1):
            player_id = to_int(p.get("playerId"))

            stats_data = None
            if get_stats and player_id:
                stats_data = fetch_player_stats_for_season(player_id, season_id, ut_id)

            row = {
                "player_id": player_id,
                "player_name": p.get("playerName", ""),
                "player_position": p.get("position", ""),
                "team_id": to_int(p.get("teamId")),
                "team_name": p.get("teamName", ""),
                "rank": rank,
                "stat_type": stat_type,
                "ut_name": ut_name,
                "goals": stats_data.get("goals") if stats_data else 0,
                "assists": stats_data.get("assists") if stats_data else 0,
                "appearances": stats_data.get("appearances") if stats_data else 0,
                "minutes_played": stats_data.get("minutes_played") if stats_data else 0,
                "xg": stats_data.get("xg") if stats_data else 0.0,
                "xa": stats_data.get("xa") if stats_data else 0.0,
                "yellow_cards": stats_data.get("yellow_cards") if stats_data else 0,
                "red_cards": stats_data.get("red_cards") if stats_data else 0,
            }
            rows.append(row)

        return 200, rows, ut_name

    except urllib.error.HTTPError as e:
        if e.code == 404:
            return 404, [], ut_name
        return e.code, [], ut_name
    except Exception as e:
        return 500, [], ut_name


def mysql_upsert_player_stat(conn, category_id, ut_id, season_id, stat_type, r, fetched_ts):
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO player_season_stats
        (category_id, ut_id, season_id, stat_type, `rank`, player_id, player_name,
         player_position, team_id, team_name, goals, assists, appearances,
         minutes_played, xg, xa, yellow_cards, red_cards, fetched_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
          player_name=VALUES(player_name), player_position=VALUES(player_position),
          team_id=VALUES(team_id), team_name=VALUES(team_name),
          goals=VALUES(goals), assists=VALUES(assists), appearances=VALUES(appearances),
          minutes_played=VALUES(minutes_played), xg=VALUES(xg), xa=VALUES(xa),
          yellow_cards=VALUES(yellow_cards), red_cards=VALUES(red_cards), fetched_at=VALUES(fetched_at)
    """, (
        category_id, ut_id, season_id, stat_type,
        to_int(r.get("rank")),
        to_int(r.get("player_id")),
        r.get("player_name", ""),
        r.get("player_position", ""),
        to_int(r.get("team_id")),
        r.get("team_name", ""),
        to_int(r.get("goals")),
        to_int(r.get("assists")),
        to_int(r.get("appearances")),
        to_int(r.get("minutes_played")),
        to_float(r.get("xg")),
        to_float(r.get("xa")),
        to_int(r.get("yellow_cards")),
        to_int(r.get("red_cards")),
        fetched_ts,
    ))
    cur.close()


def mysql_log_player_stats(conn, category_id, ut_id, season_id, stat_type, fetched_ts, status_code, local_rows, err):
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO fetch_log
        (category_id, ut_id, season_id, stat_type, fetched_at, status_code, player_count, error)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE fetched_at=VALUES(fetched_at)
    """, (category_id, ut_id, season_id, stat_type, fetched_ts, status_code, local_rows, err))
    cur.close()


def main():
    ap = argparse.ArgumentParser(description="Backfill player season stats from SofaScore")
    ap.add_argument("--db", default="data/backfill_sofascore_10y/player_stats.sqlite")
    ap.add_argument("--state", default="data/backfill_sofascore_10y/player_stats_state.json")
    ap.add_argument("--sleep-min", type=float, default=1.0)
    ap.add_argument("--sleep-max", type=float, default=2.0)
    ap.add_argument("--category-id", type=int, nargs="+", default=[])
    ap.add_argument("--stat-type", default="goals")
    ap.add_argument("--stat-types", default="",
                    help="Comma-separated stat types: goals,assists,xg,xa,appearances,minutes_played,yellow_cards,red_cards,goals_assists_total")
    ap.add_argument("--all-stats", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--run-all", action="store_true",
                    help="Iterate through all stat types for each category/season")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-player-stats", action="store_true",
                    help="Skip per-player /player/{id}/statistics calls (faster, less data)")
    ap.add_argument("--use-mysql", action="store_true", help="Write to MySQL instead of SQLite")
    args = ap.parse_args()

    is_mysql = args.use_mysql or use_mysql()

    if is_mysql:
        conn = mysql_connect()
        ensure_mysql_tables(conn)
        print(f"[INFO] Using MySQL (database: footballdata)")
    else:
        conn = sqlite3.connect(args.db)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=60000")
        ensure_tables(conn, is_mysql=False)
        print(f"[INFO] Using SQLite: {args.db}")

    state_path = Path(args.state)
    state = load_state(state_path)

    # Resolve stat types
    if args.run_all:
        # --run-all: iterate through all known stat types
        stat_types = list(STAT_TYPES)
    elif args.stat_types:
        # --stat-types: comma-separated list
        stat_types = [s.strip() for s in args.stat_types.split(",") if s.strip()]
    elif args.all_stats:
        stat_types = list(STAT_TYPES)
    else:
        stat_types = [args.stat_type]

    targets = []
    if args.category_id:
        cat_ids = args.category_id
    elif args.all:
        cat_ids = list(set(TOURNAMENT_IDS.values()))
    else:
        cat_ids = [1, 8, 9, 23]

    if args.limit and args.limit > 0:
        cat_ids = cat_ids[:args.limit]

    for cid in cat_ids:
        ut_id = CAT_TO_UT.get(cid, cid)
        for stat in stat_types:
            # Map short name to API order_by value
            api_stat = STAT_TYPE_SHORT.get(stat, stat)
            targets.append((cid, ut_id, stat, api_stat))

    total = len(targets)
    state["total_targets"] = total
    save_state(state_path, state)

    get_player_stats = not args.no_player_stats
    print(f"[INFO] player_stats targets={total}, get_player_stats={get_player_stats}")

    rows_upserted = 0

    for (category_id, ut_id, stat_type, api_stat) in targets:
        status_code = 0
        err = ""
        local_rows = 0
        season_id = 0

        try:
            season_id = get_season_id(ut_id)

            if not season_id:
                state["not_found"] += 1
                status_code = 404
            else:
                do_get_stats = get_player_stats and stat_type in ("goals", "assists", "xg", "xa", "goals_assists_total")
                status_code, rows, ut_name = fetch_top_players_with_stats(
                    ut_id, season_id, api_stat, get_stats=do_get_stats
                )

                if status_code == 200 and rows:
                    fetched_ts = now_mysql() if is_mysql else now_iso()

                    if is_mysql:
                        for r in rows:
                            mysql_upsert_player_stat(conn, category_id, ut_id, season_id, stat_type, r, fetched_ts)
                            local_rows += 1
                            rows_upserted += 1
                    else:
                        for r in rows:
                            conn.execute("""
                                INSERT OR REPLACE INTO player_season_stats
                                (category_id, ut_id, season_id, stat_type, rank, player_id, player_name,
                                 player_position, team_id, team_name, goals, assists, appearances,
                                 minutes_played, xg, xa, yellow_cards, red_cards, fetched_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, (
                                category_id, ut_id, season_id, stat_type,
                                to_int(r.get("rank")),
                                to_int(r.get("player_id")),
                                r.get("player_name", ""),
                                r.get("player_position", ""),
                                to_int(r.get("team_id")),
                                r.get("team_name", ""),
                                to_int(r.get("goals")),
                                to_int(r.get("assists")),
                                to_int(r.get("appearances")),
                                to_int(r.get("minutes_played")),
                                to_float(r.get("xg")),
                                to_float(r.get("xa")),
                                to_int(r.get("yellow_cards")),
                                to_int(r.get("red_cards")),
                                fetched_ts,
                            ))
                            local_rows += 1
                            rows_upserted += 1
                    state["ok"] += 1
                    state["stat_rows"] += local_rows
                elif status_code == 404:
                    state["not_found"] += 1
                else:
                    state["errors"] += 1
                    err = f"HTTP {status_code}"

        except Exception as e:
            state["errors"] += 1
            err = str(e)

        if is_mysql:
            mysql_log_player_stats(conn, category_id, ut_id, season_id, stat_type, now_mysql(), status_code, local_rows, err)
        else:
            conn.execute("""
                INSERT INTO player_stats_fetch_log
                (category_id, ut_id, season_id, stat_type, fetched_at, status_code, player_count, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (category_id, ut_id, season_id, stat_type, now_iso(), status_code, local_rows, err))

        state["processed"] += 1

        if state["processed"] % 20 == 0:
            if is_mysql:
                conn.commit()
            else:
                conn.commit()
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
    print(f"[DONE] player_stats: {rows_upserted} rows, ok={state['ok']}, 404={state['not_found']}, err={state['errors']}")


if __name__ == "__main__":
    main()