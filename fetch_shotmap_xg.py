#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shotmap xG backfill (resume-safe) via direct API.
- Fetch /event/{event_id}/shotmap
- Store per-match shotmap xG aggregates
- Optionally patch matches table

API: GET https://www.sofascore.com/api/v1/event/{event_id}/shotmap
Status: ✅ Direct API confirmed working (2026-05-03)

Usage:
  python fetch_shotmap_xg.py \\
    --db data/backfill_sofascore_10y/shotmap_xg.sqlite \\
    --state data/backfill_sofascore_10y/shotmap_xg_state.json \\
    --category-id 1 --season-id 61627 --limit 10
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
    except Exception as e:
        return 500, {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS shotmap_xg_backfill (
          event_id INTEGER PRIMARY KEY,
          match_date TEXT,
          league TEXT,
          home_team TEXT,
          away_team TEXT,
          status_code INTEGER,
          has_shotmap INTEGER,
          has_xg INTEGER,
          shot_count INTEGER,
          home_shotmap_xg REAL,
          away_shotmap_xg REAL,
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
            "has_shotmap": 0, "has_xg": 0,
            "matches_patched": 0,
            "last_event_id": None, "total_targets": 0,
        }
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict) -> None:
    state["updated_at"] = now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_shotmap(event_id: int) -> tuple[int, dict]:
    """Fetch shotmap for an event. Returns (status_code, payload)."""
    return api_get(f"event/{event_id}/shotmap")


def main() -> None:
    ap = argparse.ArgumentParser(description="Shotmap xG backfill (direct API)")
    ap.add_argument("--db", default="data/backfill_sofascore_10y/shotmap_xg.sqlite")
    ap.add_argument("--state", default="data/backfill_sofascore_10y/shotmap_xg_state.json")
    ap.add_argument("--sleep-min", type=float, default=0.3)
    ap.add_argument("--sleep-max", type=float, default=0.6)
    ap.add_argument("--checkpoint-every", type=int, default=100)
    ap.add_argument("--limit", type=int, default=0,
                    help="Max events to process (0 = unlimited)")
    ap.add_argument("--event-id", type=int, nargs="+", default=[],
                    help="Specific event IDs")
    ap.add_argument("--category-id", type=int, default=1,
                    help="Tournament category ID for event discovery")
    ap.add_argument("--season-id", type=int, default=61627,
                    help="Season ID for event discovery")
    ap.add_argument("--past-only", action="store_true", default=True)
    args = ap.parse_args()

    db_path = Path(args.db)
    state_path = Path(args.state)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_tables(conn)

    state = load_state(state_path)

    # Build target event IDs
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

    # Filter out already-processed
    done_ids = set(r[0] for r in conn.execute("SELECT event_id FROM shotmap_xg_backfill").fetchall())
    targets = [eid for eid in targets if eid not in done_ids]

    if args.limit and args.limit > 0:
        targets = targets[:args.limit]

    total_targets = len(targets)
    state["total_targets"] = total_targets
    save_state(state_path, state)
    print(f"[INFO] targets={total_targets}")

    if total_targets == 0:
        print("[INFO] Nothing to process")
        return

    processed_in_run = 0

    for event_id in targets:
        status_code = 0
        has_shotmap = 0
        has_xg = 0
        shot_count = 0
        home_xg = 0.0
        away_xg = 0.0
        err = ""

        try:
            status_code, payload = fetch_shotmap(event_id)
            if status_code == 200:
                arr = (payload or {}).get("shotmap") or []
                shot_count = len(arr)
                has_shotmap = 1 if shot_count > 0 else 0
                for sh in arr:
                    xg = sh.get("xg")
                    try:
                        xv = float(xg) if xg is not None else 0.0
                    except Exception:
                        xv = 0.0
                    if sh.get("isHome") is True:
                        home_xg += xv
                    elif sh.get("isHome") is False:
                        away_xg += xv
                has_xg = 1 if (home_xg > 0 or away_xg > 0) else 0
            elif status_code == 404:
                pass
            else:
                err = f"HTTP {status_code}"
        except Exception as e:
            err = str(e)

        conn.execute("""
            INSERT OR REPLACE INTO shotmap_xg_backfill
            (event_id, status_code, has_shotmap, has_xg,
             shot_count, home_shotmap_xg, away_shotmap_xg, fetched_at, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            event_id, status_code, has_shotmap, has_xg,
            shot_count, round(home_xg, 6), round(away_xg, 6),
            now_iso(), err,
        ))

        state["processed"] = int(state.get("processed", 0)) + 1
        processed_in_run += 1
        state["last_event_id"] = event_id

        if status_code == 200:
            state["ok"] = int(state.get("ok", 0)) + 1
        elif status_code == 404:
            state["not_found"] = int(state.get("not_found", 0)) + 1
        else:
            state["errors"] = int(state.get("errors", 0)) + 1

        if has_shotmap:
            state["has_shotmap"] = int(state.get("has_shotmap", 0)) + 1
        if has_xg:
            state["has_xg"] = int(state.get("has_xg", 0)) + 1

        if processed_in_run % args.checkpoint_every == 0:
            conn.commit()
            save_state(state_path, state)
            pct = (processed_in_run / total_targets) * 100 if total_targets else 100
            print(f"[CHK] run={processed_in_run}/{total_targets} ({pct:.2f}%) "
                  f"ok={state['ok']} 404={state['not_found']} err={state['errors']} "
                  f"has_xg={state['has_xg']}")

        time.sleep(random.uniform(args.sleep_min, args.sleep_max))

    conn.commit()
    save_state(state_path, state)
    print(f"[DONE] shotmap_xg: processed={processed_in_run} ok={state['ok']} "
          f"404={state['not_found']} err={state['errors']} has_xg={state['has_xg']}")


if __name__ == "__main__":
    main()