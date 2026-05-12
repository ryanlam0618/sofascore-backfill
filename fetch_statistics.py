#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fetch match statistics from SofaScore via /api/v1/event/{id}/statistics.

Endpoint: GET https://www.sofascore.com/api/v1/event/{event_id}/statistics
Returns: JSON with statistics array — periods (ALL/1ST/2ND), groups, items with home/away values.

Key headers needed:
  - Referer: https://www.sofascore.com/event/{event_id}
  - Accept-Encoding: gzip, deflate (no br — server uses brotli but we decompress manually)
  - Accept: application/json, text/plain, */*

Migration history:
  - 2026-05-06: initial implementation based on browser network inspection (Kris shared headers)
  - Endpoint /api/v1/event/{id}/statistics confirmed working (HTTP 200 with full stats)

Resume-safe with sqlite + state JSON.
"""

from __future__ import annotations

import argparse
import gzip
import json
import random
import sqlite3
import time
import urllib.error
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

BASE_URL = "https://www.sofascore.com"
EVENT_PATH = "/api/v1/event/{event_id}/statistics"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_mysql() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def fetch_statistics(event_id: int, retries: int = 3, backoff_base: float = 1.5) -> tuple[int, dict]:
    """Fetch statistics JSON for a given event. Returns (status_code, data_dict)."""
    path = EVENT_PATH.format(event_id=event_id)
    url = f"{BASE_URL}{path}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/136.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6,zh-HK;q=0.5",
        "Accept-Encoding": "gzip, deflate",
        "Referer": f"{BASE_URL}/event/{event_id}",
        "Origin": BASE_URL,
    }
    last_status = 500
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
                # Decompress if needed
                try:
                    data = json.loads(gzip.decompress(raw))
                except Exception:
                    # Already plain JSON or single compressed layer failed
                    try:
                        data = json.loads(raw.decode("utf-8"))
                    except Exception:
                        # Try raw bytes
                        data = json.loads(raw)
                return resp.status, data
        except urllib.error.HTTPError as e:
            last_status = e.code
            if e.code in (404, 410) and attempt < retries - 1:
                time.sleep(backoff_base * (attempt + 1) + random.uniform(0.2, 0.8))
                continue
            if e.code == 403 and attempt < retries - 1:
                time.sleep(backoff_base * (attempt + 1) + random.uniform(0.2, 0.8))
                continue
            return e.code, {}
        except Exception:
            last_status = 500
            if attempt < retries - 1:
                time.sleep(backoff_base * (attempt + 1) + random.uniform(0.2, 0.8))
                continue
            return 500, {}
    return last_status, {}


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS statistics (
          event_id INTEGER,
          period TEXT,
          group_name TEXT,
          stat_name TEXT,
          home_value TEXT,
          away_value TEXT,
          home_numeric REAL,
          away_numeric REAL,
          render_type INTEGER,
          statistics_type TEXT,
          fetched_at TEXT,
          PRIMARY KEY (event_id, period, group_name, stat_name)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS statistics_events (
          event_id INTEGER PRIMARY KEY,
          status_code INTEGER,
          period_count INTEGER,
          group_count INTEGER,
          stat_count INTEGER,
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
            "started_at": now_iso(),
            "updated_at": now_iso(),
            "processed": 0,
            "ok": 0,
            "not_found": 0,
            "errors": 0,
            "stats_upserted": 0,
            "last_event_id": None,
            "total_targets": 0,
        }
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict) -> None:
    state["updated_at"] = now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_stat_value(val) -> tuple[str, float | None]:
    """Extract display string and numeric value from a stat value."""
    if val is None:
        return "", None
    s = str(val)
    # Try to parse numeric from string like "53%" or "1.56" or "24/59 (41%)"
    numeric = None
    import re
    m = re.search(r"[-+]?\d*\.\d+|\d+", s)
    if m:
        try:
            numeric = float(m.group())
        except Exception:
            pass
    return s, numeric


def upsert_stat(conn: sqlite3.Connection, event_id: int, period: str,
                group_name: str, item: dict, fetched_ts: str) -> bool:
    home_str, home_num = parse_stat_value(item.get("home"))
    away_str, away_num = parse_stat_value(item.get("away"))
    conn.execute("""
        INSERT OR REPLACE INTO statistics
        (event_id, period, group_name, stat_name, home_value, away_value,
         home_numeric, away_numeric, render_type, statistics_type, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        event_id, period, group_name,
        item.get("name", ""),
        home_str, away_str,
        home_num, away_num,
        item.get("renderType"),
        item.get("statisticsType"),
        fetched_ts,
    ))
    return True


def api_get_season_events(category_id: int, season_id: int, retries: int = 3) -> tuple[int, dict]:
    """Fetch season events via the public API."""
    url = f"{BASE_URL}/api/v1/tournament/{category_id}/season/{season_id}/events"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    }
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


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Backfill match statistics from SofaScore /api/v1/event/{id}/statistics"
    )
    ap.add_argument("--db", default="data/backfill_sofascore_10y/statistics_PL.sqlite")
    ap.add_argument("--state", default="data/backfill_sofascore_10y/statistics_PL_state.json")
    ap.add_argument("--sleep-min", type=float, default=1.0)
    ap.add_argument("--sleep-max", type=float, default=2.0)
    ap.add_argument("--checkpoint-every", type=int, default=50)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--event-id", type=int, nargs="+", default=[],
                    help="Specific event IDs")
    ap.add_argument("--category-id", type=int, default=1)
    ap.add_argument("--season-id", type=int, default=76986,
                    help="Season ID (default 76986 = PL 25/26)")
    ap.add_argument("--past-only", action="store_true", default=True)
    ap.add_argument("--retry-errors", action="store_true",
                    help="Re-fetch events that previously got errors")
    args = ap.parse_args()

    db_path = Path(args.db)
    state_path = Path(args.state)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    ensure_tables(conn)

    # Build target event IDs
    if args.event_id:
        targets = args.event_id
    else:
        status, ev_data = api_get_season_events(args.category_id, args.season_id)
        if status != 200:
            print(f"[ERROR] Could not fetch events: HTTP {status}")
            return
        all_evs = ev_data.get("events", [])
        now_ts = datetime.now().timestamp()
        if args.past_only:
            all_evs = [e for e in all_evs if e.get("startTimestamp", 9999999999) < now_ts]
        targets = [int(e["id"]) for e in all_evs if e.get("id")]

    # Filter out already-done events unless retrying
    if not args.retry_errors:
        done_ids = set(r[0] for r in conn.execute(
            "SELECT event_id FROM statistics_events WHERE status_code = 200"
        ).fetchall())
        targets = [eid for eid in targets if eid not in done_ids]

    total = len(targets)
    print(f"[INFO] Using SQLite: {args.db}")
    print(f"[INFO] Statistics targets={total}")

    if args.limit > 0:
        targets = targets[: args.limit]

    if not targets:
        print("[INFO] Nothing to process")
        conn.close()
        return

    state = load_state(state_path)
    state["total_targets"] = total

    processed_run = 0
    for event_id in targets:
        fetched_ts = now_iso()
        status_code, data = fetch_statistics(event_id)

        period_count = 0
        group_count = 0
        stat_count = 0
        upserted = 0

        if status_code == 200:
            periods = data.get("statistics", [])
            period_count = len(periods)
            for period in periods:
                period_name = period.get("period", "ALL")
                for group in period.get("groups", []):
                    group_name = group.get("groupName", "")
                    group_count += 1
                    for item in group.get("statisticsItems", []):
                        stat_count += 1
                        if upsert_stat(conn, event_id, period_name, group_name, item, fetched_ts):
                            upserted += 1

            conn.execute("""
                INSERT OR REPLACE INTO statistics_events
                (event_id, status_code, period_count, group_count, stat_count, fetched_at, error)
                VALUES (?, ?, ?, ?, ?, ?, NULL)
            """, (event_id, status_code, period_count, group_count, stat_count, fetched_ts))

            state["ok"] = int(state.get("ok", 0)) + 1
            print(f"[OK] {event_id}: {period_count} periods, {stat_count} stats")
        elif status_code == 404:
            conn.execute("""
                INSERT OR REPLACE INTO statistics_events
                (event_id, status_code, period_count, group_count, stat_count, fetched_at, error)
                VALUES (?, ?, 0, 0, 0, ?, 'not found')
            """, (event_id, status_code, fetched_ts))
            state["not_found"] = int(state.get("not_found", 0)) + 1
            print(f"[404] {event_id}: archived or not found")
        else:
            conn.execute("""
                INSERT OR REPLACE INTO statistics_events
                (event_id, status_code, period_count, group_count, stat_count, fetched_at, error)
                VALUES (?, ?, 0, 0, 0, ?, ?)
            """, (event_id, status_code, fetched_ts, f"HTTP {status_code}"))
            state["errors"] = int(state.get("errors", 0)) + 1
            print(f"[ERR] {event_id}: HTTP {status_code}")

        state["stats_upserted"] = int(state.get("stats_upserted", 0)) + upserted
        state["processed"] = int(state.get("processed", 0)) + 1
        state["last_event_id"] = event_id
        processed_run += 1

        if processed_run % args.checkpoint_every == 0:
            conn.commit()
            save_state(state_path, state)
            pct = (processed_run / total) * 100 if total else 100
            print(f"[CHK] run={processed_run}/{total} ({pct:.1f}%) "
                  f"ok={state['ok']} 404={state['not_found']} err={state['errors']} "
                  f"stats={state['stats_upserted']}")

        time.sleep(random.uniform(args.sleep_min, args.sleep_max))

    conn.commit()
    conn.close()

    save_state(state_path, state)
    print(f"[DONE] statistics completed: stats={state['stats_upserted']} "
          f"ok={state['ok']} 404={state['not_found']} err={state['errors']}")


if __name__ == "__main__":
    main()
