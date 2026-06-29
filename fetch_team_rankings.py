#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fetch team world/regional rankings from SofaScore team page HTML __NEXT_DATA__.

Approach: SofaScore team web pages embed a `__NEXT_DATA__` hydration script
containing all server-side rendered data, including historical team rankings
under `props.pageProps.initialProps.teamRankings.rankings`.

Direct API ranking endpoints (/rankings/team/*) all return 404 — the data
is only available via HTML page hydration.

Endpoint: GET https://www.sofascore.com/team/football/{slug}/{team_id}
Returns: HTML page with __NEXT_DATA__ → props.pageProps.initialProps.teamRankings

Ranking types embedded in the page (integer `type` values):
  4  = European / UEFA ranking
  9  = World ranking (default shown on page)
  10 = Home ranking (if applicable)
  11 = Away ranking (if applicable)

Data fields per row: year, type, ranking, points, team, rowName, id, rankingClass

Resume-safe with sqlite + state JSON. Also supports MySQL via --use-mysql.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from mysql_helpers import ensure_mysql_tables, mysql_connect, use_mysql

TEAM_PAGE_BASE = "https://www.sofascore.com"


def fetch_team_page_html(team_id: int, slug: str = "", retries: int = 3,
                          backoff_base: float = 1.5) -> tuple[int, str]:
    """Fetch the team web page as HTML. Returns (status_code, html_or_empty)."""
    # Build URL — slug is optional for the endpoint
    if slug:
        url = f"{TEAM_PAGE_BASE}/team/football/{slug}/{team_id}"
    else:
        url = f"{TEAM_PAGE_BASE}/team/football/team/{team_id}"
    headers = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
        "Accept-Language": "en-GB,en;q=0.9,en;q=0.8",
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
                time.sleep(backoff_base * (attempt + 1) + random.uniform(0.3, 1.0))
                continue
            if e.code == 403 and attempt < retries - 1:
                time.sleep(backoff_base * (attempt + 1) + random.uniform(0.3, 1.0))
                continue
            return e.code, ""
        except Exception:
            last_status = 500
            if attempt < retries - 1:
                time.sleep(backoff_base * (attempt + 1) + random.uniform(0.3, 1.0))
                continue
            return 500, ""
    return last_status, ""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_mysql() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ── SQLite schema ─────────────────────────────────────────────────────────────

def ensure_tables_sqlite(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS team_ranking_fetch_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team_id INTEGER,
            team_slug TEXT,
            fetched_at TEXT,
            status_code INTEGER,
            row_count INTEGER,
            ranking_types TEXT,
            error TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS team_rankings (
            team_id INTEGER,
            team_name TEXT,
            team_slug TEXT,
            year TEXT,
            ranking_type INTEGER,
            ranking_type_name TEXT,
            ranking INTEGER,
            points INTEGER,
            ranking_class TEXT,
            fetched_at TEXT,
            UNIQUE(team_id, year, ranking_type)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tr_team ON team_rankings(team_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tr_year ON team_rankings(year)")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.commit()


# ── Ranking type mapping ───────────────────────────────────────────────────────

RANKING_TYPE_NAMES = {
    4:  "uefa",
    9:  "world",
    10: "home",
    11: "away",
}


def parse_team_page(html: str) -> tuple[list[dict], dict]:
    """
    Extract team rankings from the __NEXT_DATA__ hydration script.
    Returns (rankings_rows, team_meta_dict).
    """
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not m:
        return [], {}
    try:
        data = json.loads(m.group(1))
        pp = data.get("props", {}).get("pageProps", {})
        ip = pp.get("initialProps", {})
        rankings_raw = ip.get("teamRankings", {})
        rankings_rows = rankings_raw.get("rankings", []) if isinstance(rankings_raw, dict) else rankings_raw or []
        team_meta = ip.get("team", {})
        return rankings_rows, team_meta
    except Exception:
        return [], {}


def normalize_ranking_row(row: dict) -> dict:
    """Normalize a single ranking row to a flat dict."""
    team_block = row.get("team", {}) or {}
    ranking_type = int(row.get("type", 0))
    return {
        "team_id":     int(team_block.get("id", 0) or row.get("teamId", 0) or 0),
        "team_name":   team_block.get("name", "") or row.get("rowName", ""),
        "team_slug":   team_block.get("slug", ""),
        "year":        str(row.get("year", "")),
        "ranking_type": ranking_type,
        "ranking_type_name": RANKING_TYPE_NAMES.get(ranking_type, str(ranking_type)),
        "ranking":     int(row.get("ranking", 0) or 0),
        "points":      int(row.get("points", 0) or 0),
        "ranking_class": str(row.get("rankingClass", "")),
    }


def upsert_ranking_sqlite(db, row: dict, fetched_at: str) -> None:
    db.execute("""
        INSERT OR REPLACE INTO team_rankings
        (team_id, team_name, team_slug, year, ranking_type, ranking_type_name,
         ranking, points, ranking_class, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        row["team_id"], row["team_name"], row["team_slug"],
        row["year"], row["ranking_type"], row["ranking_type_name"],
        row["ranking"], row["points"], row["ranking_class"],
        fetched_at,
    ))


def upsert_ranking_mysql(conn, row: dict, fetched_at: str) -> None:
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO team_rankings
        (team_id, team_name, team_slug, year, ranking_type, ranking_type_name,
         ranking, points, ranking_class, fetched_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
          team_name=VALUES(team_name), team_slug=VALUES(team_slug),
          ranking=VALUES(ranking), points=VALUES(points),
          ranking_class=VALUES(ranking_class), fetched_at=VALUES(fetched_at)
    """, (
        row["team_id"], row["team_name"], row["team_slug"],
        row["year"], row["ranking_type"], row["ranking_type_name"],
        row["ranking"], row["points"], row["ranking_class"],
        fetched_at,
    ))
    cur.close()


# ── State management ──────────────────────────────────────────────────────────

def load_state(path: Path) -> dict:
    if not path.exists():
        return {
            "started_at": now_iso(), "updated_at": now_iso(),
            "processed": 0, "ok": 0, "not_found": 0, "errors": 0,
            "rows_upserted": 0, "last_team_id": None, "total_targets": 0,
        }
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict) -> None:
    state["updated_at"] = now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Fetch team world/regional rankings from SofaScore team page HTML")
    ap.add_argument("--db", default="data/backfill_sofascore_10y/team_rankings.sqlite")
    ap.add_argument("--state", default="data/backfill_sofascore_10y/team_rankings_state.json")
    ap.add_argument("--sleep-min", type=float, default=1.0)
    ap.add_argument("--sleep-max", type=float, default=2.0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--team-id", type=int, nargs="+", default=[],
                    help="Specific team IDs to fetch")
    ap.add_argument("--use-mysql", action="store_true", help="Write to MySQL instead of SQLite")
    ap.add_argument("--retry-errors", action="store_true",
                    help="Re-fetch teams that previously got errors")
    args = ap.parse_args()

    is_mysql = args.use_mysql or use_mysql()

    if is_mysql:
        conn = mysql_connect()
        ensure_mysql_tables(conn)
        print("[INFO] Using MySQL")
        db = None  # type: ignore
    else:
        db = sqlite3.connect(args.db)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=60000")
        ensure_tables_sqlite(db)
        print(f"[INFO] Using SQLite: {args.db}")
        conn = db

    state_path = Path(args.state)
    state = load_state(state_path)

    # Build target list
    if args.team_id:
        targets = args.team_id
    else:
        # Default known teams for smoke test
        targets = [42, 44, 17, 985, 20]  # Arsenal, Liverpool, Man City, Man Utd, Gillingham

    if not is_mysql and args.retry_errors:
        # Pick only teams that had errors previously
        rows = db.execute(
            "SELECT DISTINCT team_id FROM team_ranking_fetch_log WHERE status_code NOT IN (200, 404)"
        ).fetchall()
        targets = [r[0] for r in rows]

    if not is_mysql and not args.team_id and not args.retry_errors:
        # Exclude already-done teams in normal mode
        done = set(r[0] for r in db.execute(
            "SELECT DISTINCT team_id FROM team_ranking_fetch_log WHERE status_code = 200"
        ).fetchall())
        targets = [t for t in targets if t not in done]

    if args.limit and args.limit > 0:
        targets = targets[:args.limit]

    total = len(targets)
    state["total_targets"] = total
    save_state(state_path, state)
    print(f"[INFO] team_rankings targets={total}")

    if total == 0:
        print("[INFO] Nothing to process")
        return

    run_ok = 0
    run_not_found = 0
    run_errors = 0
    run_rows = 0
    processed_run = 0

    for team_id in targets:
        status_code = 0
        err = ""
        local_rows = 0
        ranking_types_found = ""

        fetched_at_iso = now_iso()
        fetched_at_mysql = now_mysql()

        try:
            status, html = fetch_team_page_html(team_id, retries=3)

            if status == 200:
                rows_raw, team_meta = parse_team_page(html)

                if not rows_raw:
                    # Check if the page rendered but has no rankings section
                    raise ValueError("No rankings data found in __NEXT_DATA__")

                ranking_types_found = ",".join(
                    str(r.get("type")) for r in rows_raw
                )

                for raw_row in rows_raw:
                    row = normalize_ranking_row(raw_row)
                    row["team_name"] = row["team_name"] or team_meta.get("name", "")
                    row["team_slug"] = row["team_slug"] or team_meta.get("slug", "")

                    if is_mysql:
                        upsert_ranking_mysql(conn, row, fetched_at_mysql)
                    else:
                        upsert_ranking_sqlite(db, row, fetched_at_iso)
                    local_rows += 1
                    run_rows += 1

                run_ok += 1

            elif status in (404, 410):
                run_not_found += 1
                err = "not_found"

            else:
                run_errors += 1
                err = f"HTTP {status}"

        except Exception as e:
            run_errors += 1
            err = str(e)

        # Log
        if is_mysql:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO fetch_log
                (team_id, team_slug, fetched_at, status_code, row_count, ranking_types, error)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                  fetched_at=VALUES(fetched_at), status_code=VALUES(status_code),
                  row_count=VALUES(row_count), ranking_types=VALUES(ranking_types),
                  error=VALUES(error)
            """, (team_id, "", fetched_at_mysql, status, local_rows, ranking_types_found, err))
            cur.close()
        else:
            db.execute("""
                INSERT INTO team_ranking_fetch_log
                (team_id, team_slug, fetched_at, status_code, row_count, ranking_types, error)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (team_id, "", fetched_at_iso, status, local_rows, ranking_types_found, err))

        processed_run += 1
        state["processed"] = int(state.get("processed", 0)) + 1
        state["last_team_id"] = team_id
        state["ok"] = int(state.get("ok", 0)) + run_ok
        state["not_found"] = int(state.get("not_found", 0)) + run_not_found
        state["errors"] = int(state.get("errors", 0)) + run_errors
        state["rows_upserted"] = int(state.get("rows_upserted", 0)) + local_rows

        if processed_run % 5 == 0:
            if is_mysql:
                conn.commit()
            else:
                db.commit()
            save_state(state_path, state)
            pct = (processed_run / total) * 100 if total else 100
            print(f"[CHK] run={processed_run}/{total} ({pct:.0f}%) "
                  f"ok={run_ok} 404={run_not_found} err={run_errors} rows={run_rows}")

        time.sleep(random.uniform(args.sleep_min, args.sleep_max))

    if is_mysql:
        conn.commit()
    else:
        db.commit()
        db.close()

    save_state(state_path, state)
    print(f"[DONE] team_rankings: rows={run_rows} ok={run_ok} 404={run_not_found} err={run_errors}")


if __name__ == "__main__":
    main()