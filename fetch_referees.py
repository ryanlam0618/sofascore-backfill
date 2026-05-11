#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fetch referee info from SofaScore.

Source A: /api/v1/event/{event_id} → event.referee (summary)
Source B: /api/v1/referee/{referee_id} → full profile (career stats)

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
        CREATE TABLE IF NOT EXISTS sofascore_referee_fetch_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT,
            source_id TEXT,
            fetched_at TEXT,
            status_code INTEGER,
            error TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sofascore_referees (
            referee_id INTEGER PRIMARY KEY,
            name TEXT,
            short_name TEXT,
            slug TEXT,
            country_alpha2 TEXT,
            country_name TEXT,
            matches_total INTEGER,
            yellow_cards_total INTEGER,
            red_cards_total INTEGER,
            yellow_red_cards_total INTEGER,
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
            "processed_events": 0, "processed_referees": 0,
            "ok": 0, "not_found": 0, "errors": 0,
        }
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict) -> None:
    state["updated_at"] = now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def upsert_referee_sqlite(db, ref: dict, fetched_at: str) -> None:
    db.execute("""
        INSERT OR REPLACE INTO sofascore_referees
        (referee_id, name, short_name, slug, country_alpha2, country_name,
         matches_total, yellow_cards_total, red_cards_total, yellow_red_cards_total,
         fetched_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        ref["referee_id"], ref["name"], ref["short_name"], ref["slug"],
        ref.get("country_alpha2"), ref.get("country_name"),
        ref.get("matches_total", 0), ref.get("yellow_cards_total", 0),
        ref.get("red_cards_total", 0), ref.get("yellow_red_cards_total", 0),
        fetched_at, fetched_at,
    ))


def upsert_referee_mysql(conn, ref: dict, fetched_at: str) -> None:
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO sofascore_referees
        (referee_id, name, short_name, slug, country_alpha2, country_name,
         matches_total, yellow_cards_total, red_cards_total, yellow_red_cards_total,
         fetched_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
          name=VALUES(name), short_name=VALUES(short_name),
          matches_total=VALUES(matches_total), yellow_cards_total=VALUES(yellow_cards_total),
          red_cards_total=VALUES(red_cards_total), yellow_red_cards_total=VALUES(yellow_red_cards_total),
          fetched_at=VALUES(fetched_at), updated_at=VALUES(updated_at)
    """, (
        ref["referee_id"], ref["name"], ref["short_name"], ref["slug"],
        ref.get("country_alpha2"), ref.get("country_name"),
        ref.get("matches_total", 0), ref.get("yellow_cards_total", 0),
        ref.get("red_cards_total", 0), ref.get("yellow_red_cards_total", 0),
        fetched_at, fetched_at,
    ))
    cur.close()


def normalize_from_event(event_ref: dict) -> dict:
    """Extract referee data from an event's referee field."""
    if not event_ref:
        return {}
    ref = event_ref.get("referee", event_ref) if isinstance(event_ref, dict) else {}
    country = ref.get("country", {}) or {}
    return {
        "referee_id":   ref.get("id", 0),
        "name":         ref.get("name", ""),
        "short_name":   ref.get("shortName", ""),
        "slug":         ref.get("slug", ""),
        "country_alpha2": country.get("alpha2", ""),
        "country_name": country.get("name", ""),
        "matches_total": 0, "yellow_cards_total": 0,
        "red_cards_total": 0, "yellow_red_cards_total": 0,
    }


def normalize_from_profile(profile: dict) -> dict:
    """Extract referee data from /api/v1/referee/{id} profile."""
    ref = profile.get("referee", {})
    country = ref.get("country", {}) or {}
    stats = profile.get("statistics", {}) or {}
    return {
        "referee_id":   ref.get("id", 0),
        "name":         ref.get("name", ""),
        "short_name":   ref.get("shortName", ""),
        "slug":         ref.get("slug", ""),
        "country_alpha2": country.get("alpha2", ""),
        "country_name": country.get("name", ""),
        "matches_total": stats.get("matchesTotal", 0),
        "yellow_cards_total": stats.get("yellowCardsTotal", 0),
        "red_cards_total": stats.get("redCardsTotal", 0),
        "yellow_red_cards_total": stats.get("yellowRedCardsTotal", 0),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch referee data from SofaScore")
    ap.add_argument("--db", default="data/backfill_sofascore_10y/referees.sqlite")
    ap.add_argument("--state", default="data/backfill_sofascore_10y/referees_state.json")
    ap.add_argument("--event-id", type=int, nargs="+", default=[],
                    help="Specific event IDs to extract referee from")
    ap.add_argument("--referee-id", type=int, nargs="+", default=[],
                    help="Specific referee IDs to fetch profile for")
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

    # Build targets
    tasks: list[tuple[str, int]] = []  # (type, id)
    if args.event_id:
        for eid in args.event_id:
            tasks.append(("event", eid))
    if args.referee_id:
        for rid in args.referee_id:
            tasks.append(("referee", rid))

    if args.limit > 0:
        tasks = tasks[:args.limit]

    ok = 0; not_found = 0; errors = 0

    for typ, oid in tasks:
        try:
            if typ == "event":
                status, data = api_get(f"event/{oid}")
                ref = {}
                if status == 200:
                    ev = data.get("event", {})
                    ref = normalize_from_event(ev)
                    print(f"[REF] event={oid} referee={ref.get('referee_id')} "
                          f"name={ref.get('name', 'n/a')}")
                else:
                    print(f"[ERR] event={oid} status={status}")
                    errors += 1
                    continue
            else:
                status, data = api_get(f"referee/{oid}")
                if status == 200:
                    ref = normalize_from_profile(data)
                    print(f"[REF] profile id={oid} name={ref.get('name')} "
                          f"matches={ref.get('matches_total', 0)}")
                else:
                    print(f"[ERR] referee={oid} status={status}")
                    errors += 1
                    continue

            if ref and ref.get("referee_id"):
                if is_mysql:
                    upsert_referee_mysql(conn, ref, fetched_at_mysql)
                else:
                    upsert_referee_sqlite(conn, ref, fetched_at_iso)
                ok += 1
                state["processed_referees"] = state.get("processed_referees", 0) + 1
            else:
                not_found += 1

        except Exception as e:
            print(f"[EXC] {typ}={oid} {e}")
            errors += 1

        if typ == "event":
            state["processed_events"] = state.get("processed_events", 0) + 1

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
    print(f"[DONE] referees: ok={ok} not_found={not_found} errors={errors}")


if __name__ == "__main__":
    main()