#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fetch manager info from SofaScore.

Source A: /api/v1/team/{team_id} → team.manager (summary)
Source B: /api/v1/manager/{manager_id} → full profile

Status: ✅ Both direct API confirmed working (2026-05-07)

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


def ensure_tables_sqlite(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sofascore_manager_fetch_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT,
            source_id TEXT,
            fetched_at TEXT,
            status_code INTEGER,
            error TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sofascore_managers (
            manager_id INTEGER PRIMARY KEY,
            name TEXT,
            short_name TEXT,
            slug TEXT,
            country_alpha2 TEXT,
            country_name TEXT,
            birth_date TEXT,
            age INTEGER,
            active_team_id INTEGER,
            active_team_name TEXT,
            fetched_at TEXT,
            updated_at TEXT
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
            "processed_teams": 0, "processed_managers": 0,
            "ok": 0, "not_found": 0, "errors": 0,
        }
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict) -> None:
    state["updated_at"] = now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def upsert_manager_sqlite(db, mgr: dict, fetched_at: str) -> None:
    db.execute("""
        INSERT OR REPLACE INTO sofascore_managers
        (manager_id, name, short_name, slug, country_alpha2, country_name,
         birth_date, age, active_team_id, active_team_name, fetched_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        mgr["manager_id"], mgr["name"], mgr["short_name"], mgr["slug"],
        mgr.get("country_alpha2"), mgr.get("country_name"),
        mgr.get("birth_date"), mgr.get("age"),
        mgr.get("active_team_id", 0), mgr.get("active_team_name", ""),
        fetched_at, fetched_at,
    ))


def upsert_manager_mysql(conn, mgr: dict, fetched_at: str) -> None:
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO sofascore_managers
        (manager_id, name, short_name, slug, country_alpha2, country_name,
         birth_date, age, active_team_id, active_team_name, fetched_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
          name=VALUES(name), short_name=VALUES(short_name),
          active_team_id=VALUES(active_team_id), active_team_name=VALUES(active_team_name),
          fetched_at=VALUES(fetched_at), updated_at=VALUES(updated_at)
    """, (
        mgr["manager_id"], mgr["name"], mgr["short_name"], mgr["slug"],
        mgr.get("country_alpha2"), mgr.get("country_name"),
        mgr.get("birth_date"), mgr.get("age"),
        mgr.get("active_team_id", 0), mgr.get("active_team_name", ""),
        fetched_at, fetched_at,
    ))
    cur.close()


def normalize_from_team(team_data: dict) -> dict:
    """Extract manager data from a team response."""
    mgr = team_data.get("team", {}).get("manager", {}) or {}
    country = mgr.get("country", {}) or {}
    return {
        "manager_id":      mgr.get("id", 0),
        "name":            mgr.get("name", ""),
        "short_name":      mgr.get("shortName", ""),
        "slug":            mgr.get("slug", ""),
        "country_alpha2":  country.get("alpha2", ""),
        "country_name":    country.get("name", ""),
        "birth_date":      mgr.get("birthDate", ""),
        "age":             mgr.get("age", 0),
        "active_team_id":  team_data.get("team", {}).get("id", 0),
        "active_team_name": team_data.get("team", {}).get("name", ""),
    }


def normalize_from_profile(profile: dict) -> dict:
    """Extract manager data from /api/v1/manager/{id} profile."""
    mgr = profile.get("manager", {})
    country = mgr.get("country", {}) or {}
    return {
        "manager_id":      mgr.get("id", 0),
        "name":            mgr.get("name", ""),
        "short_name":      mgr.get("shortName", ""),
        "slug":            mgr.get("slug", ""),
        "country_alpha2":  country.get("alpha2", ""),
        "country_name":    country.get("name", ""),
        "birth_date":      mgr.get("birthDate", ""),
        "age":             mgr.get("age", 0),
        "active_team_id":  0,
        "active_team_name": "",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch manager data from SofaScore")
    ap.add_argument("--db", default="data/backfill_sofascore_10y/managers.sqlite")
    ap.add_argument("--state", default="data/backfill_sofascore_10y/managers_state.json")
    ap.add_argument("--team-id", type=int, nargs="+", default=[],
                    help="Specific team IDs to extract manager from")
    ap.add_argument("--manager-id", type=int, nargs="+", default=[],
                    help="Specific manager IDs to fetch profile for")
    ap.add_argument("--sleep-min", type=float, default=0.3)
    ap.add_argument("--sleep-max", type=float, default=0.7)
    ap.add_argument("--use-mysql", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    is_mysql = args.use_mysql or use_mysql()

    if is_mysql:
        conn = mysql_connect()
        ensure_mysql_tables(conn)
        print("[INFO] Using MySQL")
    else:
        conn = sqlite3.connect(args.db)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=60000")
        ensure_tables_sqlite(conn)
        print(f"[INFO] Using SQLite: {args.db}")

    state_path = Path(args.state)
    state = load_state(state_path)
    fetched_at_iso = now_iso()
    fetched_at_mysql = now_mysql()

    tasks: list[tuple[str, int]] = []
    if args.team_id:
        for tid in args.team_id:
            tasks.append(("team", tid))
    if args.manager_id:
        for mid in args.manager_id:
            tasks.append(("manager", mid))

    if args.limit > 0:
        tasks = tasks[:args.limit]

    ok = 0; not_found = 0; errors = 0

    for typ, oid in tasks:
        try:
            if typ == "team":
                status, data = api_get(f"team/{oid}")
                mgr_data = {}
                if status == 200:
                    mgr_data = normalize_from_team(data)
                    print(f"[MGR] team={oid} manager_id={mgr_data.get('manager_id')} "
                          f"name={mgr_data.get('name', 'n/a')}")
                else:
                    print(f"[ERR] team={oid} status={status}")
                    errors += 1
                    continue
            else:
                status, data = api_get(f"manager/{oid}")
                if status == 200:
                    mgr_data = normalize_from_profile(data)
                    print(f"[MGR] profile id={oid} name={mgr_data.get('name')} "
                          f"team={mgr_data.get('active_team_name', 'n/a')}")
                else:
                    print(f"[ERR] manager={oid} status={status}")
                    errors += 1
                    continue

            if mgr_data and mgr_data.get("manager_id"):
                if is_mysql:
                    upsert_manager_mysql(conn, mgr_data, fetched_at_mysql)
                else:
                    upsert_manager_sqlite(conn, mgr_data, fetched_at_iso)
                ok += 1
                state["processed_managers"] = state.get("processed_managers", 0) + 1
            else:
                not_found += 1

        except Exception as e:
            print(f"[EXC] {typ}={oid} {e}")
            errors += 1

        if typ == "team":
            state["processed_teams"] = state.get("processed_teams", 0) + 1

        time.sleep(random.uniform(args.sleep_min, args.sleep_max))

    if is_mysql:
        conn.commit()
    else:
        conn.commit()
        conn.close()

    state["ok"] = state.get("ok", 0) + ok
    state["not_found"] = state.get("not_found", 0) + not_found
    state["errors"] = state.get("errors", 0) + errors
    save_state(state_path, state)
    print(f"[DONE] managers: ok={ok} not_found={not_found} errors={errors}")


if __name__ == "__main__":
    main()