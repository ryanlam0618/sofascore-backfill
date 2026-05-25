#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fetch match attendance from SofaScore event API.

Endpoint: GET https://www.sofascore.com/api/v1/event/{event_id}
Returns: event.attendance (string, e.g. "61,052")

Status: ✅ Uses Webshare Tokyo proxy to bypass Fastly challenge
Supports MySQL via USE_MYSQL=1 or --use-mysql flag.
Resume-safe with sqlite + state JSON.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from mysql_helpers import ensure_mysql_tables, mysql_connect, use_mysql

API_BASE = "https://www.sofascore.com/api/v1"

_proxy_opener: urllib.request.OpenerDirector | None = None


def _get_opener() -> urllib.request.OpenerDirector:
    global _proxy_opener
    if _proxy_opener is None:
        proxy_file = os.path.join(os.path.dirname(__file__), "proxy_list.txt")
        proxies = []
        if os.path.exists(proxy_file):
            with open(proxy_file) as f:
                proxies = [line.strip() for line in f if line.strip()]
        if proxies:
            proxy = proxies[len(proxies) % 5]  # rotate
        else:
            proxy = os.environ.get("SOFA_PROXY", "")
        if proxy:
            ph = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        else:
            ph = urllib.request.ProxyHandler({})
        _proxy_opener = urllib.request.build_opener(ph)
    return _proxy_opener


def api_get(path: str) -> tuple[int, dict]:
    url = f"{API_BASE}/{path.lstrip('/')}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    req = urllib.request.Request(url, headers=headers)
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


def parse_attendance(raw: str | None) -> int | None:
    """Convert attendance string like '61,052' to int 61052. Returns None if missing."""
    if not raw:
        return None
    cleaned = re.sub(r"[^\d]", "", str(raw))
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def ensure_tables_sqlite(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sofascore_attendance_fetch_log (
            event_id INTEGER PRIMARY KEY,
            fetched_at TEXT,
            status_code INTEGER,
            attendance_raw TEXT,
            attendance_int INTEGER,
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
            "processed": 0, "ok": 0, "not_found": 0,
            "no_attendance": 0, "errors": 0,
        }
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict) -> None:
    state["updated_at"] = now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def upsert_attendance_sqlite(db, event_id: int, raw: str, attendance: int | None,
                              fetched_at: str, status_code: int, error: str) -> None:
    db.execute("""
        INSERT OR REPLACE INTO sofascore_attendance_fetch_log
        (event_id, fetched_at, status_code, attendance_raw, attendance_int, error)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (event_id, fetched_at, status_code, raw, attendance, error))


def upsert_attendance_mysql(conn, event_id: int, raw: str, attendance: int | None,
                               fetched_at: str, status_code: int, error: str) -> None:
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO sofascore_attendance_fetch_log
        (event_id, fetched_at, status_code, attendance_raw, attendance_int, error)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
          fetched_at=VALUES(fetched_at), status_code=VALUES(status_code),
          attendance_raw=VALUES(attendance_raw), attendance_int=VALUES(attendance_int),
          error=VALUES(error)
    """, (event_id, fetched_at, status_code, raw, attendance, error))
    cur.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch match attendance from SofaScore")
    ap.add_argument("--db", default="data/backfill_sofascore_10y/attendance.sqlite")
    ap.add_argument("--state", default="data/backfill_sofascore_10y/attendance_state.json")
    ap.add_argument("--event-id", type=int, nargs="+", default=[],
                    help="Specific event IDs to fetch")
    ap.add_argument("--sleep-min", type=float, default=1.0)
    ap.add_argument("--sleep-max", type=float, default=2.0)
    ap.add_argument("--use-mysql", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--retry-errors", action="store_true")
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

    if args.event_id:
        targets = args.event_id
    else:
        # Smoke test with known events (PL 25/26)
        targets = [14025013, 14025023, 14025027, 14025029, 14025033]

    if not is_mysql and args.retry_errors:
        rows = conn.execute(
            "SELECT DISTINCT event_id FROM sofascore_attendance_fetch_log "
            "WHERE status_code NOT IN (200, 404) OR attendance_raw IS NULL"
        ).fetchall()
        targets = [r[0] for r in rows]

    if args.limit > 0:
        targets = targets[:args.limit]

    ok = 0; not_found = 0; no_attendance = 0; errors = 0

    for event_id in targets:
        try:
            status, data = api_get(f"event/{event_id}")
            if status == 200:
                ev = data.get("event", {})
                raw = ev.get("attendance", "")
                attendance = parse_attendance(raw)
                if attendance is not None:
                    ok += 1
                    label = f"event={event_id} attendance={attendance:,} (raw: '{raw}')"
                else:
                    no_attendance += 1
                    label = f"event={event_id} no_attendance (raw: '{raw}')"
                print(f"[ATT] {label}")
                err = ""
            elif status in (404, 410):
                not_found += 1
                raw = ""; attendance = None; err = "not_found"
                print(f"[ATT] event={event_id} 404")
            else:
                errors += 1
                raw = ""; attendance = None; err = f"HTTP {status}"
                print(f"[ERR] event={event_id} status={status}")
                continue

            if is_mysql:
                upsert_attendance_mysql(conn, event_id, raw, attendance,
                                        fetched_at_mysql, status, err)
            else:
                upsert_attendance_sqlite(conn, event_id, raw, attendance,
                                          fetched_at_iso, status, err)

        except Exception as e:
            print(f"[EXC] event={event_id} {e}")
            errors += 1

        state["processed"] = state.get("processed", 0) + 1
        time.sleep(random.uniform(args.sleep_min, args.sleep_max))

    if is_mysql:
        conn.commit()
    else:
        conn.commit()
        conn.close()

    state["ok"] = state.get("ok", 0) + ok
    state["not_found"] = state.get("not_found", 0) + not_found
    state["no_attendance"] = state.get("no_attendance", 0) + no_attendance
    state["errors"] = state.get("errors", 0) + errors
    save_state(state_path, state)
    print(f"[DONE] attendance: ok={ok} no_attendance={no_attendance} "
          f"not_found={not_found} errors={errors}")


if __name__ == "__main__":
    main()
