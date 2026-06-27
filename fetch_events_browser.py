#!/usr/bin/env python3
"""Playwright-based SofaScore event fetcher.

Browser-first, SSR-aware, and proxy-enabled.

This script uses the shared browser client in `sofascore_browser.py`.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from sofascore_browser import EVENT_ENDPOINTS, SofaScoreBrowserClient, run_async

# ── MySQL helpers ──────────────────────────────────────────────
USE_MYSQL = False


def mysql_connect():
    import os
    from dotenv import load_dotenv

    load_dotenv('/root/.openclaw/workspace/.env')
    import mysql.connector

    return mysql.connector.connect(
        host=os.getenv('MYSQL_HOST'),
        port=int(os.getenv('MYSQL_PORT', 3306)),
        user=os.getenv('MYSQL_USER'),
        password=os.getenv('MYSQL_PASSWORD'),
        database=os.getenv('MYSQL_DATABASE'),
        autocommit=True,
    )


# ── Constants ───────────────────────────────────────────────────
API_BASE = "https://www.sofascore.com/api/v1"
DATA_DIR = Path(__file__).parent / "data" / "backfill_sofascore_10y"
DATA_DIR.mkdir(parents=True, exist_ok=True)


# ── Fallback: direct API (for when it works) ───────────────────
def direct_api_get(path: str) -> tuple[int, dict]:
    url = f"{API_BASE}/{path.lstrip('/')}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.sofascore.com/",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception as e:
        return 500, {'error': str(e)}


# ── Database helpers ─────────────────────────────────────────────
def get_db_conn():
    return sqlite3.connect(DATA_DIR / f"events_{int(time.time())}.sqlite", check_same_thread=False)


def ensure_tables(conn: sqlite3.Connection):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS event_apis (
        event_id INTEGER,
        endpoint TEXT,
        status INTEGER,
        data TEXT,
        fetched_at TEXT,
        PRIMARY KEY (event_id, endpoint)
    );
    """)
    conn.commit()


def save_api_result(conn: sqlite3.Connection, event_id: int, endpoint: str, status: int, data):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO event_apis VALUES (?, ?, ?, ?, ?)",
        (event_id, endpoint, status, json.dumps(data, ensure_ascii=False), now)
    )
    conn.commit()


# ── Main fetch logic ──────────────────────────────────────────────
def fetch_event_data(event_id: int, method='browser', dry_run=False) -> dict:
    """Fetch all API data for a given event.

    method: 'browser' | 'direct' | 'auto'
    browser: preferred path
    direct: legacy fallback only
    auto: browser first, then direct fallback
    """

    if method in ('browser', 'auto'):
        try:
            async def _fetch():
                async with SofaScoreBrowserClient(headless=True) as client:
                    return await client.fetch_event_bundle(event_id, EVENT_ENDPOINTS)

            result = run_async(_fetch())
        except Exception as e:
            print(f"  ⚠️ Browser fetch failed: {e}")
            result = None

        if result:
            if dry_run:
                print(f"\n  🔍 Event {event_id} browser result:")
                print(f"    SSR: {'yes' if result.get('ssr') else 'no'}")
                for path, data in list(result.get('apis', {}).items())[:8]:
                    print(f"    [{data.get('status')}] {path}")
                return result

            conn = get_db_conn()
            ensure_tables(conn)
            for path, data in result.get('apis', {}).items():
                body = data.get('body', data.get('body_text', data.get('error', '')))
                save_api_result(conn, event_id, path, data.get('status', 0), body)
            conn.close()
            return result

        if method == 'browser':
            return {'error': 'browser fetch failed', 'event_id': event_id}

    # Direct API fallback
    if method in ('direct', 'auto'):
        result = {'event_id': event_id, 'direct_apis': {}}
        endpoints = [
            f'event/{event_id}/incidents',
            f'event/{event_id}/lineups',
            f'event/{event_id}/statistics',
            f'event/{event_id}/managers',
            f'event/{event_id}/graph',
        ]
        conn = get_db_conn()
        ensure_tables(conn)

        for ep in endpoints:
            status, data = direct_api_get(ep)
            result['direct_apis'][ep] = {'status': status, 'data': data if status == 200 else {}}

            if status == 200:
                save_api_result(conn, event_id, ep, status, data)
            print(f"  [{status}] {ep}")

        conn.close()
        return result

    return {}


# ── Entry point ──────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description='Fetch SofaScore event data via browser')
    ap.add_argument('--event-id', type=int, help='Single event ID to fetch')
    ap.add_argument('--limit', type=int, help='Fetch N events from DB')
    ap.add_argument('--dry-run', action='store_true', help='Show what would be fetched without saving')
    ap.add_argument('--method', choices=['browser', 'direct', 'auto'], default='browser',
                    help='browser is preferred; auto keeps legacy fallback')
    args = ap.parse_args()

    if args.event_id:
        result = fetch_event_data(args.event_id, method=args.method, dry_run=args.dry_run)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print('Specify --event-id or --limit')
        sys.exit(1)


if __name__ == '__main__':
    main()
