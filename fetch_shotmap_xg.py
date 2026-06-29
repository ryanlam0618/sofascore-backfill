#!/usr/bin/env python3
"""
Re-fetch shotmap/xG for events that have shotmap but zero xG.
These events were stored when the API returned xg=None/0 for all shots.
After SofaScore API unblocks, run this to backfill the missing xG values.

Usage:
  python fetch_shotmap_xg.py --db data/backfill_sofascore_10y/shotmap_xg_PL.sqlite \
    --state data/backfill_sofascore_10y/shotmap_xg_PL_state.json --refetch-zero

OR to re-fetch a specific event:
  python fetch_shotmap_xg.py --event-id 7073008 \
    --db data/backfill_sofascore_10y/shotmap_xg_PL.sqlite \
    --state data/backfill_sofascore_10y/shotmap_xg_PL_state.json
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

from anti_block import (
    build_headers,
    shuffle_targets,
    random_sleep,
    RequestCounter,
)

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

_proxy_opener = None
_proxy_list = None
_last_proxy_idx = -1
_request_counter = None

def _init_counter(daily_limit=5000):
    global _request_counter
    if _request_counter is None:
        _request_counter = RequestCounter(daily_limit=daily_limit)
    return _request_counter

API_BASE = "https://www.sofascore.com/api/v1"


def _random_ua():
    """Return a random user agent to avoid pattern detection."""
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


def api_get(path: str, referer: str = "") -> tuple[int, dict]:
    global _request_counter
    url = f"{API_BASE}/{path.lstrip('/')}"
    headers = _default_headers()
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    
    counter = _init_counter()
    if counter.is_exhausted:
        raise RuntimeError(f"[BLOCK] Daily request limit reached.")
    counter.increment()
    
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
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=60000")
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
    ap = argparse.ArgumentParser(
        description="Shotmap xG backfill — also supports re-fetching events "
                    "that have shotmap but zero xG (script bug workaround)"
    )
    ap.add_argument("--db", default="data/backfill_sofascore_10y/shotmap_xg.sqlite")
    ap.add_argument("--state", default="data/backfill_sofascore_10y/shotmap_xg_state.json")
    ap.add_argument("--sleep-min", type=float, default=2.0)
    ap.add_argument("--sleep-max", type=float, default=5.0)
    ap.add_argument("--daily-limit", type=int, default=5000)
    ap.add_argument("--no-shuffle", action="store_true")
    ap.add_argument("--checkpoint-every", type=int, default=100)
    ap.add_argument("--limit", type=int, default=0,
                    help="Max events to process (0 = unlimited)")
    ap.add_argument("--event-id", type=int, nargs="+", default=[],
                    help="Specific event IDs")
    ap.add_argument("--category-id", type=int, default=1)
    ap.add_argument("--season-id", type=int, default=61627)
    ap.add_argument("--past-only", action="store_true", default=True)
    ap.add_argument("--use-mysql", action="store_true")
    ap.add_argument(
        "--refetch-zero", action="store_true",
        help="Re-fetch all events with has_shotmap=1 but has_xg=0 "
             "(these have shotmap but xG was 0 due to script/payload bug)"
    )
    args = ap.parse_args()

    is_mysql = args.use_mysql or use_mysql()

    if is_mysql:
        conn = mysql_connect()
        ensure_mysql_tables(conn)
        print(f"[INFO] Using MySQL (database: footballdata)")
        db = None
    else:
        conn = sqlite3.connect(args.db)
        conn.row_factory = sqlite3.Row
        ensure_tables(conn)
        print(f"[INFO] Using SQLite: {args.db}")
        db = conn

    state_path = Path(args.state)
    state = load_state(state_path)

    # Build target event IDs
    if args.refetch_zero:
        # Re-fetch events with shotmap but zero xG
        if is_mysql:
            print("[ERROR] --refetch-zero only works with SQLite for now")
            return
        rows = conn.execute("""
            SELECT event_id FROM shotmap_xg_backfill
            WHERE has_shotmap = 1 AND has_xg = 0
        """).fetchall()
        targets = [r["event_id"] for r in rows]
        print(f"[INFO] Re-fetch targets: {len(targets)} events with shotmap but zero xG")
    elif args.event_id:
        targets = args.event_id
        print(f"[INFO] Specific event IDs: {targets}")
    else:
        status, ev_data = api_get(
            f"tournament/{args.category_id}/season/{args.season_id}/events"
        )
        if status != 200:
            print(f"[ERROR] Could not fetch events: HTTP {status}")
            return
        all_evs = ev_data.get("events", [])
        now_ts = datetime.now().timestamp()
        if args.past_only:
            all_evs = [e for e in all_evs
                       if e.get("startTimestamp", 9999999999) < now_ts]
        targets = [int(e["id"]) for e in all_evs if e.get("id")]
        
        # Shuffle targets to avoid sequential access
        if not args.no_shuffle:
            targets = shuffle_targets(targets)
            print(f"[INFO] Targets shuffled")

        # Filter out already-processed (only for non-refetch)
        if not is_mysql:
            done_ids = {
                r[0] for r in conn.execute(
                    "SELECT event_id FROM shotmap_xg_backfill"
                ).fetchall()
            }
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

                # Also check top-level xG fields (fallback)
                payload_xg = payload or {}
                alt_home_xg = payload_xg.get("homeXg") or payload_xg.get("home_xg") or 0.0
                alt_away_xg = payload_xg.get("awayXg") or payload_xg.get("away_xg") or 0.0

                for sh in arr:
                    # Try multiple possible key names
                    xg = (
                        sh.get("xg") or sh.get("xG") or sh.get("expected_goals")
                        or sh.get("expectedGoals") or sh.get("xgot")  # xgot includes OT
                    )
                    try:
                        xv = float(xg) if xg is not None else 0.0
                    except (ValueError, TypeError):
                        xv = 0.0

                    if sh.get("isHome") is True:
                        home_xg += xv
                    elif sh.get("isHome") is False:
                        away_xg += xv

                # Fallback to top-level xG if per-shot xG is all zeros
                if home_xg == 0.0 and away_xg == 0.0:
                    try:
                        home_xg = float(alt_home_xg) if alt_home_xg else 0.0
                        away_xg = float(alt_away_xg) if alt_away_xg else 0.0
                    except (ValueError, TypeError):
                        pass

                has_xg = 1 if (home_xg > 0 or away_xg > 0) else 0

                if status_code == 200 and shot_count > 0:
                    print(f"  event {event_id}: shots={shot_count}, "
                          f"home_xg={home_xg:.4f}, away_xg={away_xg:.4f}")
            elif status_code == 404:
                pass
            else:
                err = f"HTTP {status_code}"
        except Exception as e:
            err = str(e)

        fetched_ts = now_mysql() if is_mysql else now_iso()

        if is_mysql:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO match_shotmap
                (event_id, status_code, has_shotmap, has_xg,
                 shot_count, home_shotmap_xg, away_shotmap_xg, fetched_at, error)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                  status_code=VALUES(status_code), has_shotmap=VALUES(has_shotmap),
                  has_xg=VALUES(has_xg), shot_count=VALUES(shot_count),
                  home_shotmap_xg=VALUES(home_shotmap_xg),
                  away_shotmap_xg=VALUES(away_shotmap_xg),
                  fetched_at=VALUES(fetched_at), error=VALUES(error)
            """, (
                event_id, status_code, has_shotmap, has_xg,
                shot_count, round(home_xg, 6), round(away_xg, 6),
                fetched_ts, err,
            ))
            cur.close()
        else:
            conn.execute("""
                INSERT OR REPLACE INTO shotmap_xg_backfill
                (event_id, status_code, has_shotmap, has_xg,
                 shot_count, home_shotmap_xg, away_shotmap_xg, fetched_at, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                event_id, status_code, has_shotmap, has_xg,
                shot_count, round(home_xg, 6), round(away_xg, 6),
                fetched_ts, err,
            ))
            db.commit()

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
            if is_mysql:
                conn.commit()
            else:
                db.commit()
            save_state(state_path, state)
            pct = (processed_in_run / total_targets) if total_targets else 1
            print(f"[CHK] run={processed_in_run}/{total_targets} ({pct*100:.2f}%) "
                  f"ok={state['ok']} 404={state['not_found']} err={state['errors']} "
                  f"has_xg={state['has_xg']}")

        time.sleep(random.uniform(args.sleep_min, args.sleep_max))
        
        # Update usage info periodically
        counter = _init_counter()
        if processed_in_run % 200 == 0 and processed_in_run > 0:
            print(f"[INFO] Usage: {counter.count}/{counter.daily_limit} ({counter.usage_pct*100:.1f}%)")

    if is_mysql:
        conn.commit()
    else:
        db.commit()
        db.close()
    save_state(state_path, state)
    print(f"[DONE] shotmap_xg: processed={processed_in_run} ok={state['ok']} "
          f"404={state['not_found']} err={state['errors']} has_xg={state['has_xg']}")
    counter = _init_counter()
    print(f"[INFO] Requests: {counter.count}/{counter.daily_limit} ({counter.usage_pct*100:.1f}%)")


if __name__ == "__main__":
    main()
