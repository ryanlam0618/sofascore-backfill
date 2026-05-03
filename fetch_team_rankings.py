#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fetch team rankings from SofaScore.

NOTE: Team world/regional rankings do NOT have a public API endpoint.
The /rankings/team/{type} endpoint returns 404.
Team rankings shown on SofaScore web are rendered client-side via JavaScript.

Options:
1. Use DrissionPage browser automation (requires significant memory)
2. Use paid data providers (e.g., API-Football, Football-Data.org)
3. Scrape via headless Chrome with CDP

For now, this script attempts direct API patterns but gracefully falls back
when rankings endpoints return 404.

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

# Known ranking types
RANKING_TYPES = ["overall", "attack", "defense", "goalkeeping"]

# Team ranking endpoints we tested (all return 404)
# /rankings/team/{type}
# /tournament/{id}/season/{id}/rankings
# /unique-tournament/{id}/season/{id}/rankings
# /rankings/team/world

CAT_TO_UT = {
    1: 17, 2: 18, 3: 24, 8: 8, 23: 23, 9: 9, 4: 34,
    7: 7, 459: 459, 94: 94, 245: 245, 314: 314,
}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def ensure_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS team_ranking_fetch_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ranking_type TEXT,
          category_id INTEGER,
          fetched_at TEXT,
          status_code INTEGER,
          team_count INTEGER,
          error TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS team_rankings (
          category_id INTEGER,
          ranking_type TEXT,
          rank INTEGER,
          team_id INTEGER,
          team_name TEXT,
          team_short_name TEXT,
          value REAL,
          value_str TEXT,
          trend TEXT,
          tournament_name TEXT,
          fetched_at TEXT,
          UNIQUE(category_id, ranking_type, team_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rank ON team_rankings(category_id, ranking_type)")
    conn.commit()


def load_state(path):
    if not path.exists():
        return {"started_at": now_iso(), "updated_at": now_iso(),
                "processed": 0, "ok": 0, "not_found": 0, "errors": 0,
                "ranking_rows": 0, "last_category_id": None, "total_targets": 0}
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


def api_get(path):
    url = f"{API_BASE}/{path.lstrip('/')}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    resp = urllib.request.urlopen(req, timeout=15)
    return json.loads(resp.read())


def get_season_id(ut_id):
    try:
        j = api_get(f"unique-tournament/{ut_id}/seasons")
        seasons = j.get("seasons", [])
        if seasons:
            return seasons[0]["id"]
    except Exception:
        pass
    return 0


def fetch_rankings(category_id, ranking_type):
    """Try direct API patterns for team rankings.
    
    All tested patterns return 404. This function attempts common
    patterns and returns empty results when none work.
    
    Future: Use browser automation to extract from web page if needed.
    """
    rows = []
    tournament_name = ""
    
    try:
        # Get tournament name
        try:
            j = api_get(f"tournament/{category_id}")
            tournament_name = j.get("tournament", {}).get("name", "")
        except Exception:
            pass
        
        # Try various ranking endpoint patterns
        patterns = [
            f"rankings/team/{ranking_type}",
            f"tournament/{category_id}/season/current/rankings/{ranking_type}",
            f"tournament/{category_id}/rankings/{ranking_type}",
            f"unique-tournament/{CAT_TO_UT.get(category_id, category_id)}/rankings/{ranking_type}",
            f"rankings/{ranking_type}",
        ]
        
        for path in patterns:
            try:
                j = api_get(path)
                # If we get here, the endpoint exists
                if j and "error" not in j:
                    # Try to parse rankings from response
                    ranking_data = j if isinstance(j, list) else j.get("rankings") or j.get("standings") or j.get("data") or []
                    if isinstance(ranking_data, list):
                        for item in ranking_data:
                            if isinstance(item, dict):
                                team = item.get("team") or item.get("teamEntity") or item
                                if isinstance(team, dict):
                                    rows.append({
                                        "rank": to_int(item.get("rank") or item.get("position")),
                                        "team_id": to_int(team.get("id")),
                                        "team_name": team.get("name", ""),
                                        "team_short_name": team.get("shortName", ""),
                                        "value": to_float(item.get("value") or item.get("stat")),
                                        "value_str": str(item.get("value", "")),
                                        "trend": item.get("trend", ""),
                                        "ranking_type": ranking_type,
                                        "tournament_name": tournament_name,
                                    })
                        if rows:
                            return (200, rows, tournament_name)
            except urllib.error.HTTPError:
                continue
            except Exception:
                continue
        
        # All patterns failed - return 404
        return (404, [], tournament_name)
        
    except Exception as e:
        return (500, [], tournament_name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/backfill_sofascore_10y/team_rankings.sqlite")
    ap.add_argument("--state", default="data/backfill_sofascore_10y/team_rankings_state.json")
    ap.add_argument("--sleep-min", type=float, default=0.3)
    ap.add_argument("--sleep-max", type=float, default=0.6)
    ap.add_argument("--category-id", type=int, nargs="+", default=[])
    ap.add_argument("--ranking-type", default="overall")
    ap.add_argument("--all-types", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=60000")
    ensure_tables(db)

    state_path = Path(args.state)
    state = load_state(state_path)

    targets = []
    stat_types = RANKING_TYPES if args.all_types else [args.ranking_type]

    if args.category_id:
        cat_ids = args.category_id
    elif args.all:
        cat_ids = list(CAT_TO_UT.keys())
    else:
        cat_ids = [1, 8, 9, 23]

    if args.limit and args.limit > 0:
        cat_ids = cat_ids[:args.limit]

    for cid in cat_ids:
        for rt in stat_types:
            targets.append((cid, rt))

    state["total_targets"] = len(targets)
    save_state(state_path, state)
    print(f"[INFO] team_rankings targets={len(targets)}")
    print(f"[NOTE] Rankings API endpoints return 404 - see docstring for options")

    rows_upserted = 0

    for (category_id, ranking_type) in targets:
        status_code = 0
        err = ""
        local_rows = 0
        tournament_name = ""

        try:
            status_code, rows, tournament_name = fetch_rankings(category_id, ranking_type)
            
            if status_code == 200 and rows:
                for r in rows:
                    db.execute("""
                        INSERT OR REPLACE INTO team_rankings
                        (category_id, ranking_type, rank, team_id, team_name, team_short_name,
                         value, value_str, trend, tournament_name, fetched_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        category_id, r.get("ranking_type", ranking_type),
                        to_int(r.get("rank")), to_int(r.get("team_id")),
                        r.get("team_name", ""), r.get("team_short_name", ""),
                        to_float(r.get("value")), r.get("value_str", ""),
                        r.get("trend", ""), r.get("tournament_name", ""), now_iso(),
                    ))
                    local_rows += 1
                    rows_upserted += 1
                state["ok"] += 1
                state["ranking_rows"] += local_rows
            elif status_code == 404:
                state["not_found"] += 1
            else:
                state["errors"] += 1
                err = f"HTTP {status_code}"

        except Exception as e:
            state["errors"] += 1
            err = str(e)

        db.execute(
            "INSERT INTO team_ranking_fetch_log (ranking_type, category_id, fetched_at, status_code, team_count, error) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ranking_type, category_id, now_iso(), status_code, local_rows, err))
        state["processed"] += 1

        if state["processed"] % 20 == 0:
            db.commit()
            save_state(state_path, state)
            pct = (state["processed"] / len(targets)) * 100
            print(f"[CHK] {state['processed']}/{len(targets)} ({pct:.0f}%) ok={state['ok']} 404={state['not_found']}")

        time.sleep(random.uniform(args.sleep_min, args.sleep_max))

    db.commit()
    save_state(state_path, state)
    print(f"[DONE] team_rankings: {rows_upserted} rows, ok={state['ok']}, 404={state['not_found']}, err={state['errors']}")


if __name__ == "__main__":
    main()