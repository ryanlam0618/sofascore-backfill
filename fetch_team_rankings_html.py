#!/usr/bin/env python3
"""
Fetch team rankings via HTML page scraping (NOT the API).
Extracts rankings from __NEXT_DATA__ JSON embedded in team pages.

Works even when SofaScore API is rate-limited.
"""
from __future__ import annotations

import json
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────
WORKDIR = Path(__file__).parent
TEAM_PAGE_BASE = "https://www.sofascore.com/team/football"
SLEEP_MIN, SLEEP_MAX = 2.0, 5.0   # Be respectful — HTML pages are cheaper
REQUEST_TIMEOUT = 20
MAX_RETRIES = 3
RETRY_BACKOFF = 30                # seconds before retrying a failed team

# Headers matching a real browser
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "identity",
    "Referer": "https://www.sofascore.com/",
}


def now_mysql() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def get_html(team_id: int, slug: str = "", retries: int = MAX_RETRIES) -> tuple[int, str]:
    """Fetch team page HTML. Returns (http_status, html_or_empty)."""
    slug_path = f"{slug}/{team_id}" if slug else f"team/{team_id}"
    url = f"{TEAM_PAGE_BASE}/{slug_path}"
    req = urllib.request.Request(url, headers=BROWSER_HEADERS)

    last_status = 0
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            last_status = e.code
            if e.code in (403, 429) and attempt < retries - 1:
                sleep_time = RETRY_BACKOFF + random.uniform(0, 30)
                print(f"[WARN] team_id={team_id} got {e.code}, retrying in {sleep_time:.0f}s...")
                time.sleep(sleep_time)
            else:
                return e.code, ""
        except Exception as e:
            last_status = 0
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
            continue
    return last_status, ""


def parse_rankings(html: str) -> list[dict]:
    """Extract team rankings from __NEXT_DATA__ in HTML."""
    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html, re.DOTALL
    )
    if not match:
        return []
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []

    initial_props = (
        data.get("props", {})
        .get("pageProps", {})
        .get("initialProps", {})
    )
    team_details = initial_props.get("teamDetails", {})
    team_name = team_details.get("name", "")
    team_slug = team_details.get("slug", "")

    rankings_raw = initial_props.get("teamRankings", {}).get("rankings", [])
    records = []
    for entry in rankings_raw:
        country_dict = entry.get("country") or {}
        records.append({
            "team_id":       int(entry.get("team", {}).get("id", 0) or team_details.get("id", 0) or 0),
            "team_name":     team_name or entry.get("team", {}).get("name", ""),
            "team_slug":     team_slug,
            "year":          int(entry.get("year", 0) or 0),
            "ranking_type":  int(entry.get("type", 0) or 0),
            "row_name":      entry.get("rowName", ""),
            "ranking":       int(entry.get("ranking", 0) or 0),
            "points":        float(entry.get("points", 0) or 0),
            "country":       str(country_dict.get("name", "")) if isinstance(country_dict, dict) else str(country_dict or ""),
            "ranking_class": entry.get("rankingClass", ""),
        })
    return records


def ensure_mysql_table(conn) -> None:
    """Creates table matching what this HTML scraper actually populates."""
    cur = conn.cursor()
    # Drop old table if it exists (wrong schema from API approach)
    try:
        cur.execute("DROP TABLE IF EXISTS sofascore_team_rankings_html")
        print("[INFO] Dropped old sofascore_team_rankings_html table")
    except Exception:
        pass
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sofascore_team_rankings_html (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            team_id INT,
            team_name VARCHAR(255),
            team_slug VARCHAR(100),
            year INT,
            ranking_type INT,
            row_name VARCHAR(255),
            ranking INT,
            points DOUBLE,
            country VARCHAR(100),
            ranking_class VARCHAR(50),
            fetched_at DATETIME,
            UNIQUE KEY ux_tr (team_id, year, ranking_type)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    try:
        cur.execute("CREATE INDEX idx_tr_team ON sofascore_team_rankings_html(team_id)")
    except Exception:
        pass  # Index already exists
    cur.close()


def upsert_rankings_mysql(conn, records: list[dict], fetched_at: str) -> int:
    if not records:
        return 0
    cur = conn.cursor()
    inserted = 0
    for rec in records:
        try:
            cur.execute("""
                INSERT INTO sofascore_team_rankings_html
                (team_id, team_name, team_slug, year, ranking_type,
                 row_name, ranking, points, country, ranking_class, fetched_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                  team_name=VALUES(team_name), team_slug=VALUES(team_slug),
                  row_name=VALUES(row_name), ranking=VALUES(ranking),
                  points=VALUES(points), country=VALUES(country),
                  ranking_class=VALUES(ranking_class), fetched_at=VALUES(fetched_at)
            """, (
                rec["team_id"], rec["team_name"], rec["team_slug"],
                rec["year"], rec["ranking_type"], rec["row_name"],
                rec["ranking"], rec["points"], rec["country"],
                rec["ranking_class"], fetched_at
            ))
            inserted += cur.rowcount
        except Exception as e:
            print(f"[DB-ERR] team_id={rec['team_id']} year={rec['year']}: {e}")
    conn.commit()
    cur.close()
    return inserted


def upsert_rankings_sqlite(conn, records: list[dict], fetched_at: str) -> int:
    """Upsert rankings into SQLite fetch log (not the final table)."""
    if not records:
        return 0
    cur = conn.cursor()
    inserted = 0
    for rec in records:
        cur.execute("""
            INSERT OR REPLACE INTO team_ranking_fetch_log
            (team_id, team_name, year, ranking_type, ranking, points,
             status_code, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, 200, ?)
        """, (
            rec["team_id"], rec["team_name"], rec["year"],
            rec["ranking_type"], rec["ranking"], rec["points"], fetched_at
        ))
        inserted += 1
    conn.commit()
    cur.close()
    return inserted


# ── MySQL helpers (copied from mysql_helpers.py) ────────────────────────────
def mysql_connect():
    import mysql.connector
    env_path = Path.home() / ".openclaw" / "workspace" / ".env"
    opts = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("MYSQL_"):
                k, _, v = line.partition("=")
                opts[k] = v.strip()
    return mysql.connector.connect(
        host=opts.get("MYSQL_HOST", "127.0.0.1"),
        port=int(opts.get("MYSQL_PORT", "3306")),
        user=opts.get("MYSQL_USER", "A100"),
        password=opts.get("MYSQL_PASSWORD", ""),
        database=opts.get("MYSQL_DATABASE", "appdb"),
        charset="utf8mb4",
        collation="utf8mb4_unicode_ci",
    )


def use_mysql() -> bool:
    return Path.home() / ".openclaw" / "workspace" / ".env".exists()


def ensure_sqlite_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS team_ranking_fetch_log (
            team_id INTEGER,
            team_name TEXT,
            year INTEGER,
            ranking_type INTEGER,
            ranking INTEGER,
            points REAL,
            status_code INTEGER,
            fetched_at TEXT,
            UNIQUE(team_id, year, ranking_type)
        )
    """)


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    import argparse
    ap = argparse.ArgumentParser(description="Fetch team rankings via HTML scraping")
    ap.add_argument("--team-id", type=int, nargs="+", default=None,
                    help="Specific team IDs to fetch")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--use-mysql", action="store_true")
    ap.add_argument("--sleep-min", type=float, default=SLEEP_MIN)
    ap.add_argument("--sleep-max", type=float, default=SLEEP_MAX)
    ap.add_argument("--retry-errors", action="store_true")
    args = ap.parse_args()

    is_mysql = args.use_mysql or use_mysql()

    if is_mysql:
        conn = mysql_connect()
        ensure_mysql_table(conn)
        print("[INFO] Using MySQL")
    else:
        import sqlite3
        conn = sqlite3.connect(WORKDIR / "data" / "backfill_sofascore_10y" / "team_rankings.sqlite")
        conn.execute("PRAGMA busy_timeout=60000")
        ensure_sqlite_table(conn)
        print(f"[INFO] Using SQLite")

    fetched_at = now_mysql()

    # ── Determine targets ───────────────────────────────────────────────────
    if args.team_id:
        targets = args.team_id
    elif is_mysql and args.retry_errors:
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT team_id FROM sofascore_team_rankings "
            "WHERE ranking = 0 OR ranking IS NULL"
        )
        targets = [r[0] for r in cur.fetchall()]
        cur.close()
        print(f"[INFO] Retrying {len(targets)} teams from MySQL")
    elif is_mysql and not args.team_id:
        # Load team IDs from sofascore_standings
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT team_id, team_name FROM sofascore_standings WHERE team_id IS NOT NULL")
        rows = cur.fetchall()
        cur.close()
        targets = [(r[0], r[1]) for r in rows]
        print(f"[INFO] Found {len(targets)} teams from sofascore_standings")
    else:
        targets = []

    if args.limit > 0:
        targets = targets[:args.limit]

    total = len(targets)
    ok = errors = 0

    for i, item in enumerate(targets, 1):
        if isinstance(item, tuple):
            team_id, team_name = item
        else:
            team_id = item
            team_name = None

        print(f"[{i}/{total}] Fetching team_id={team_id} ({team_name or 'unknown'})", flush=True)

        status, html = get_html(team_id)
        if status == 200 and html:
            records = parse_rankings(html)
            if records:
                if is_mysql:
                    n = upsert_rankings_mysql(conn, records, fetched_at)
                else:
                    n = upsert_rankings_sqlite(conn, records, fetched_at)
                ok += 1
                years = [r["year"] for r in records]
                print(f"[OK] team_id={team_id} -> {len(records)} entries (years: {years})", flush=True)
            else:
                errors += 1
                print(f"[WARN] team_id={team_id} -> no rankings found in HTML", flush=True)
        else:
            errors += 1
            print(f"[ERR] team_id={team_id} -> HTTP {status}", flush=True)

        if i < total:
            time.sleep(random.uniform(args.sleep_min, args.sleep_max))

    if is_mysql:
        conn.commit()
    else:
        conn.close()

    print(f"\n[DONE] team_rankings_html: ok={ok}/{total} errors={errors}", flush=True)


if __name__ == "__main__":
    main()