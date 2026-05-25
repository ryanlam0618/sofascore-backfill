#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fetch match lineups from SofaScore via the widgets embed endpoint.

Endpoint: GET https://widgets.sofascore.com/embed/lineups?id={event_id}
Returns: HTML page with __NEXT_DATA__ containing initialLineups + initialEvent
         { home { players, supportStaff, formation }, away { players, ... }, confirmed }

Migration history:
- 2026-05-03: /api/v1/event/{id}/lineups blocked (all 403)
  → replaced with widgets.sofascore.com/embed/lineups SSR endpoint

Resume-safe with sqlite + state JSON.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import time
import urllib.request
import urllib.error

from anti_block import (
    build_headers,
    build_html_headers,
    shuffle_targets,
    RequestCounter,
)

_proxy_opener = None
_proxy_list = None
_last_proxy_idx = -1
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

WIDGET_BASE = "https://widgets.sofascore.com"


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
    return build_headers()


def fetch_widget_html(path: str, retries: int = 3, backoff_base: float = 1.5) -> tuple[int, str]:
    """Fetch a widget page as raw HTML. Returns (status_code, html_or_empty)."""
    url = f"{WIDGET_BASE}/{path.lstrip('/')}"
    headers = {
        "User-Agent": _random_ua(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6,zh-HK;q=0.5",
        "Accept-Encoding": "identity",
        "Referer": "https://www.sofascore.com/",
    }
    last_status = 500
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            last_status = e.code
            if e.code in (404, 410) and attempt < retries - 1:
                time.sleep(backoff_base * (attempt + 1) + random.uniform(0.2, 0.8))
                continue
            if e.code == 403 and attempt < retries - 1:
                time.sleep(backoff_base * (attempt + 1) + random.uniform(0.2, 0.8))
                continue
            return e.code, ""
        except Exception:
            last_status = 500
            if attempt < retries - 1:
                time.sleep(backoff_base * (attempt + 1) + random.uniform(0.2, 0.8))
                continue
            return 500, ""

    return last_status, ""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_mysql() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


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
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.commit()


def load_state(path: Path) -> dict:
    if not path.exists():
        return {
            "started_at": now_iso(), "updated_at": now_iso(),
            "processed": 0, "ok": 0, "not_found": 0, "errors": 0,
            "players_upserted": 0, "last_event_id": None, "total_targets": 0,
            "replay_403": 0,  # track 403 events that are now being retried
        }
    state = json.loads(path.read_text(encoding="utf-8"))
    # Ensure replay field exists for backward compat
    state.setdefault("replay_403", 0)
    return state


def save_state(path: Path, state: dict) -> None:
    state["updated_at"] = now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def to_int(v, default=None):
    try:
        return int(v)
    except Exception:
        return default


# ── Widget response parsing ────────────────────────────────────────────────────

def parse_widget_response(html: str) -> tuple[dict, dict]:
    """
    Parse the widget HTML to extract initialLineups and initialEvent.
    Returns (lineups_payload, event_payload) — either may be empty dicts.
    """
    lineups = {}
    event = {}
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not m:
        return {}, {}
    try:
        data = json.loads(m.group(1))
        pp = data.get("props", {}).get("pageProps", {})
        lineups = pp.get("initialLineups", {}) or {}
        event = pp.get("initialEvent", {}) or {}
    except Exception:
        pass
    return lineups, event


# ── Player extraction (widget format) ───────────────────────────────────────

def extract_player(player_block: dict) -> dict | None:
    """Convert a widget player block to normalized format."""
    # Widget format: { player: {...}, teamId, shirtNumber, jerseyNumber,
    #                  position, substitute, statistics: {...} }
    player_info = player_block.get("player", {})
    if isinstance(player_info, dict):
        player_id = to_int(player_info.get("id") or player_block.get("playerId"))
    else:
        player_id = None

    if player_id is None:
        return None

    return {
        "player_id": player_id,
        "player_name": player_info.get("name") if isinstance(player_info, dict) else None,
        "position": player_block.get("position"),
        "position_type": None,
        "jersey_number": to_int(player_block.get("jerseyNumber") or player_block.get("shirtNumber")),
        "captain": 1 if player_block.get("captain") is True else 0,
        "player_key": str(player_id),
        "team_id": to_int(player_block.get("teamId")),
        "is_substitute": player_block.get("substitute", False) is True,
    }


# ── Team info extraction ───────────────────────────────────────────────────────

def get_team_info(event: dict, is_home: bool) -> tuple[int | None, str]:
    """Get team_id + team_name from initialEvent."""
    team_key = "homeTeam" if is_home else "awayTeam"
    team = event.get(team_key, {})
    team_id = to_int(team.get("id"))
    team_name = team.get("name") or ""
    return team_id, team_name


# ── MySQL helpers ─────────────────────────────────────────────────────────────

def upsert_lineup_player_mysql(conn, event_id, is_home, team_id, team_name,
                                player_id, player_name, position, position_type,
                                jersey_number, captain, player_key, fetched_ts):
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO sofascore_lineups
        (event_id, is_home, team_id, team_name, player_id, player_name,
         position, position_type, jersey_number, captain, player_key, fetched_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
          team_name=VALUES(team_name), player_name=VALUES(player_name),
          position=VALUES(position), position_type=VALUES(position_type),
          jersey_number=VALUES(jersey_number), captain=VALUES(captain), fetched_at=VALUES(fetched_at)
    """, (
        event_id, is_home, team_id, team_name, player_id, player_name,
        position, position_type, jersey_number, captain, player_key, fetched_ts,
    ))
    cur.close()
    return True


def upsert_lineup_player_sqlite(db, event_id, is_home, team_id, team_name,
                                 player_id, player_name, position, position_type,
                                 jersey_number, captain, player_key, fetched_ts):
    db.execute("""
        INSERT OR REPLACE INTO lineups
        (event_id, is_home, team_id, team_name, player_id, player_name,
         position, position_type, jersey_number, captain, player_key, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        event_id, is_home, team_id, team_name, player_id, player_name,
        position, position_type, jersey_number, captain, player_key, fetched_ts,
    ))
    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill match lineups from SofaScore (widget embed endpoint)")
    ap.add_argument("--db", default="data/backfill_sofascore_10y/lineups_PL.sqlite")
    ap.add_argument("--state", default="data/backfill_sofascore_10y/lineups_PL_state.json")
    ap.add_argument("--sleep-min", type=float, default=2.0)
    ap.add_argument("--sleep-max", type=float, default=5.0)
    ap.add_argument("--daily-limit", type=int, default=5000)
    ap.add_argument("--no-shuffle", action="store_true")
    ap.add_argument("--checkpoint-every", type=int, default=50)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--event-id", type=int, nargs="+", default=[],
                    help="Specific event IDs")
    ap.add_argument("--category-id", type=int, default=1)
    ap.add_argument("--season-id", type=int, default=76986,
                    help="Season ID (default 76986 = PL 25/26)")
    ap.add_argument("--past-only", action="store_true", default=True)
    ap.add_argument("--use-mysql", action="store_true", help="Write to MySQL instead of SQLite")
    ap.add_argument("--retry-403", action="store_true",
                    help="Re-fetch events that previously got 403 (use with care)")
    ap.add_argument("--retry-errors", action="store_true",
                    help="Re-fetch events that previously got errors")
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

    print(f"[INFO] Sleep range: {args.sleep_min}-{args.sleep_max}s")
    counter = _init_counter(daily_limit=args.daily_limit)
    print(f"[INFO] Daily limit: {args.daily_limit}")

    state_path = Path(args.state)
    state = load_state(state_path)

    # Build target event IDs
    if args.event_id:
        targets = args.event_id
    else:
        # Discover events from season
        status, ev_data = api_get_season_events(args.category_id, args.season_id)
        if status != 200:
            print(f"[ERROR] Could not fetch events: HTTP {status}")
            return
        all_evs = ev_data.get("events", [])
        now_ts = datetime.now().timestamp()
        if args.past_only:
            all_evs = [e for e in all_evs if e.get("startTimestamp", 9999999999) < now_ts]
        targets = [int(e["id"]) for e in all_evs if e.get("id")]
    
    # Shuffle targets to avoid sequential access
    if not args.no_shuffle:
        targets = shuffle_targets(targets)
        print(f"[INFO] Targets shuffled")

    # Filter based on retry mode
    if not is_mysql:
        # Always exclude completed (200/404) unless retrying
        done_ids = set(r[0] for r in db.execute(
            "SELECT event_id FROM lineup_events WHERE status_code IN (200, 404)"
        ).fetchall())


        if args.retry_403:
            # Re-fetch events that got 403 — targets come from DB, not season API
            targets = [r[0] for r in db.execute(
                "SELECT event_id FROM lineup_events WHERE status_code = 403"
            ).fetchall()]
        elif args.retry_errors:
            # Re-fetch events that got any non-200/404 status — from DB
            targets = [r[0] for r in db.execute(
                "SELECT event_id FROM lineup_events WHERE status_code NOT IN (200, 404)"
            ).fetchall()]
        else:
            # Normal mode: skip already done
            targets = [eid for eid in targets if eid not in done_ids]
    else:
        # MySQL: let ON DUPLICATE KEY handle overwrites
        if args.retry_403:
            # For MySQL, need to use a direct query too
            cur = conn.cursor()
            cur.execute("SELECT event_id FROM sofascore_lineup_events WHERE status_code = 403")
            targets = [r[0] for r in cur.fetchall()]
            cur.close()
        elif args.retry_errors:
            cur = conn.cursor()
            cur.execute("SELECT event_id FROM sofascore_lineup_events WHERE status_code NOT IN (200, 404)")
            targets = [r[0] for r in cur.fetchall()]
            cur.close()
        # else: normal mode — use season targets as-is

    if args.limit and args.limit > 0:
        targets = targets[:args.limit]

    total = len(targets)
    state["total_targets"] = total
    save_state(state_path, state)
    print(f"[INFO] lineup targets={total}")
    if args.retry_403:
        print("[INFO] RETRY MODE: only events with 403 status")
    if args.retry_errors:
        print("[INFO] RETRY MODE: all error events")

    if total == 0:
        print("[INFO] Nothing to process")
        return

    processed_run = 0

    for event_id in targets:
        status_code = 0
        err = ""
        upserted = 0

        try:
            status, html = fetch_widget_html(f"embed/lineups?id={event_id}")

            if status == 200:
                lineups_payload, event_data = parse_widget_response(html)

                if not lineups_payload and not event_data:
                    raise ValueError("Widget returned no lineup or event data")

                confirmed = 1 if lineups_payload.get("confirmed") is True else 0
                home_count = 0
                away_count = 0
                fetched_ts_mysql = now_mysql()
                fetched_ts_iso = now_iso()

                for is_home, team_key in [(1, "home"), (0, "away")]:
                    team_block = lineups_payload.get(team_key, {})
                    team_id, team_name = get_team_info(event_data, is_home)

                    # Fallback: try to get team info from team_block itself
                    if team_id is None:
                        team_id = to_int(team_block.get("team", {}).get("id"))
                    if not team_name:
                        team_name = team_block.get("team", {}).get("name") or ""

                    players = team_block.get("players") or []
                    if is_home:
                        home_count = len(players)
                    else:
                        away_count = len(players)

                    for player_block in players:
                        p = extract_player(player_block)
                        if p is None:
                            continue

                        if is_mysql:
                            upsert_lineup_player_mysql(
                                conn, event_id, is_home, p["team_id"] or team_id, team_name,
                                p["player_id"], p["player_name"], p["position"],
                                p["position_type"], p["jersey_number"], p["captain"],
                                p["player_key"], fetched_ts_mysql,
                            )
                        else:
                            upsert_lineup_player_sqlite(
                                db, event_id, is_home, p["team_id"] or team_id, team_name,
                                p["player_id"], p["player_name"], p["position"],
                                p["position_type"], p["jersey_number"], p["captain"],
                                p["player_key"], fetched_ts_iso,
                            )
                        upserted += 1

                state["ok"] = int(state.get("ok", 0)) + 1
                if is_mysql:
                    cur = conn.cursor()
                    cur.execute("""
                        INSERT INTO sofascore_lineup_events
                        (event_id, status_code, home_count, away_count, confirmed, fetched_at, error)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE status_code=VALUES(status_code),
                          home_count=VALUES(home_count), away_count=VALUES(away_count),
                          confirmed=VALUES(confirmed), fetched_at=VALUES(fetched_at), error=VALUES(error)
                    """, (event_id, status, home_count, away_count, confirmed, now_mysql(), ""))
                    cur.close()
                else:
                    db.execute("""
                        INSERT OR REPLACE INTO lineup_events
                        (event_id, status_code, home_count, away_count, confirmed, fetched_at, error)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (event_id, status, home_count, away_count, confirmed, now_iso(), ""))

            elif status in (404, 410):
                state["not_found"] = int(state.get("not_found", 0)) + 1
                err = "not found"
                if is_mysql:
                    cur = conn.cursor()
                    cur.execute("""
                        INSERT INTO sofascore_lineup_events
                        (event_id, status_code, home_count, away_count, confirmed, fetched_at, error)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE status_code=VALUES(status_code),
                          fetched_at=VALUES(fetched_at), error=VALUES(error)
                    """, (event_id, status, 0, 0, 0, now_mysql(), err))
                    cur.close()
                else:
                    db.execute("""
                        INSERT OR REPLACE INTO lineup_events
                        (event_id, status_code, home_count, away_count, confirmed, fetched_at, error)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (event_id, status, 0, 0, 0, now_iso(), err))

            else:
                state["errors"] = int(state.get("errors", 0)) + 1
                err = f"HTTP {status}"
                if is_mysql:
                    cur = conn.cursor()
                    cur.execute("""
                        INSERT INTO sofascore_lineup_events
                        (event_id, status_code, home_count, away_count, confirmed, fetched_at, error)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE status_code=VALUES(status_code),
                          fetched_at=VALUES(fetched_at), error=VALUES(error)
                    """, (event_id, status, 0, 0, 0, now_mysql(), err))
                    cur.close()
                else:
                    db.execute("""
                        INSERT OR REPLACE INTO lineup_events
                        (event_id, status_code, home_count, away_count, confirmed, fetched_at, error)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (event_id, status, 0, 0, 0, now_iso(), err))

        except sqlite3.OperationalError as e:
            state["errors"] = int(state.get("errors", 0)) + 1
            err = f"sqlite error: {e}"
            if not is_mysql:
                try:
                    db.rollback()
                except Exception:
                    pass
                db.execute("""
                    INSERT OR REPLACE INTO lineup_events
                    (event_id, status_code, home_count, away_count, confirmed, fetched_at, error)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (event_id, status_code, 0, 0, 0, now_iso(), err))
        except Exception as e:
            state["errors"] = int(state.get("errors", 0)) + 1
            err = str(e)
            if is_mysql:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO sofascore_lineup_events
                    (event_id, status_code, home_count, away_count, confirmed, fetched_at, error)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE status_code=VALUES(status_code),
                      fetched_at=VALUES(fetched_at), error=VALUES(error)
                """, (event_id, status_code, 0, 0, 0, now_mysql(), err))
                cur.close()
            else:
                try:
                    db.rollback()
                except Exception:
                    pass
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
            if is_mysql:
                conn.commit()
            else:
                db.commit()
            save_state(state_path, state)
            pct = (processed_run / total) * 100 if total else 100
            print(f"[CHK] run={processed_run}/{total} ({pct:.1f}%) "
                  f"ok={state['ok']} 404={state['not_found']} err={state['errors']} "
                  f"players={state['players_upserted']}")

        time.sleep(random.uniform(args.sleep_min, args.sleep_max))
        
        # Update usage periodically
        if processed_run % 100 == 0 and processed_run > 0:
            counter = _init_counter()
            print(f"[INFO] Usage: {counter.count}/{counter.daily_limit} ({counter.usage_pct*100:.1f}%)")
    
    if is_mysql:
        conn.commit()
    else:
        db.commit()
        db.close()

    save_state(state_path, state)
    counter = _init_counter()
    print(f"[INFO] Requests: {counter.count}/{counter.daily_limit} ({counter.usage_pct*100:.1f}%)")
    print(f"[DONE] lineups completed: players={state['players_upserted']} "
          f"ok={state['ok']} 404={state['not_found']} err={state['errors']}")


# ── Season discovery ─────────────────────────────────────────────────────────

def api_get_season_events(category_id: int, season_id: int, retries: int = 3) -> tuple[int, dict]:
    """Fetch season events via the public API (used for target discovery only)."""
    import urllib.error
    url = f"https://www.sofascore.com/api/v1/tournament/{category_id}/season/{season_id}/events"
    headers = build_headers()
    last_status = 500
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            last_status = e.code
            if attempt < retries - 1:
                time.sleep(2 ** attempt + random.uniform(0.5, 1.5))
                continue
            return e.code, {}
        except Exception:
            if attempt < retries - 1:
                time.sleep(2 ** attempt + random.uniform(0.5, 1.5))
                continue
            return 500, {}
    return last_status, {}


if __name__ == "__main__":
    main()