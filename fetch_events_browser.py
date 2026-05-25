#!/usr/bin/env python3
"""
Playwright-based SofaScore API fetcher.
Bypasses bot detection by running as a headless browser that intercepts API responses.

Strategy:
1. Load match page in headless Chromium
2. Intercept network API calls
3. Extract & save the data (incidents, lineups, statistics, etc.)
4. Fall back to direct API if browser fails

Usage:
  python3 fetch_events_browser.py --event-id 7073008
  python3 fetch_events_browser.py --limit 5 --dry-run
"""

import argparse
import asyncio
import json
import random
import sqlite3
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

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
        autocommit=True
    )

# ── Constants ───────────────────────────────────────────────────
API_BASE = "https://www.sofascore.com/api/v1"
BROWSER_BASE = "https://www.sofascore.com"
DATA_DIR = Path(__file__).parent / "data" / "backfill_sofascore_10y"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# API paths to intercept
TARGET_PATTERNS = [
    '/api/v1/event/',
    '/statistics',
    '/lineups',
    '/incidents',
    '/managers',
    '/average-positions',
    '/shotmap',
]


# ── Playwright browser session ──────────────────────────────────
from playwright.async_api import async_playwright

class BrowserFetcher:
    """Headless browser that intercepts SofaScore API responses."""
    
    def __init__(self, headless=True):
        self.headless = headless
        self.browser = None
        self.context = None
        self.page = None
        self._api_data = {}
        self._pending_requests = {}
    
    async def __aenter__(self):
        self.pw = async_playwright()
        await self.pw.start()
        self.browser = await self.pw.chromium.launch(
            headless=self.headless,
            args=[
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--window-size=1920,1080',
                '--disable-blink-features=AutomationControlled',
            ]
        )
        self.context = await self.browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36',
            extra_http_headers={
                'Accept-Language': 'en-US,en;q=0.9',
            }
        )
        self.page = await self.context.new_page()
        
        # Intercept responses
        self.page.on('response', self._on_response)
        
        return self
    
    async def __aexit__(self, *args):
        if self.page:
            await self.page.close()
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.pw:
            await self.pw.stop()
    
    async def _on_response(self, response):
        url = response.url
        if 'sofascore.com/api/v1/event/' not in url:
            return
        
        try:
            # Try to parse JSON
            body = await response.json()
            self._api_data[url] = {
                'status': response.status,
                'body': body
            }
        except Exception:
            try:
                text = await response.text()
                self._api_data[url] = {
                    'status': response.status,
                    'body_text': text[:500]
                }
            except Exception:
                pass
    
    async def fetch_event(self, event_id: int, timeout=15) -> dict:
        """Load the event page and intercept all API calls."""
        self._api_data = {}
        
        url = f"{BROWSER_BASE}/event/{event_id}"
        try:
            await self.page.goto(url, timeout=timeout * 1000, wait_until='domcontentloaded')
            # Wait for API calls to complete
            await asyncio.sleep(3)
        except Exception as e:
            return {'error': str(e), 'event_id': event_id}
        
        # Extract data from intercepted calls
        result = {'event_id': event_id, 'apis': {}}
        
        for url, data in self._api_data.items():
            # Parse URL to get endpoint path
            path = url.replace('https://www.sofascore.com', '')
            result['apis'][path] = data
        
        return result


async def browser_fetch_event(event_id: int, headless=True) -> dict:
    """Fetch event data via headless browser."""
    async with BrowserFetcher(headless=headless) as bf:
        return await bf.fetch_event(event_id)


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
def fetch_event_data(event_id: int, method='auto', dry_run=False) -> dict:
    """
    Fetch all API data for a given event.
    
    method: 'browser' | 'direct' | 'auto'
    auto: try direct first, fallback to browser if 403
    """
    
    if method == 'browser' or method == 'auto':
        try:
            result = asyncio.run(browser_fetch_event(event_id, headless=True))
            
            if dry_run:
                print(f"\n  🔍 Event {event_id} browser result:")
                for path, data in list(result.get('apis', {}).items())[:5]:
                    print(f"    [{data['status']}] {path}")
                return result
            
            # Save to database
            conn = get_db_conn()
            ensure_tables(conn)
            
            for path, data in result.get('apis', {}).items():
                save_api_result(conn, event_id, path, data['status'], data.get('body', data.get('body_text', '')))
            
            conn.close()
            return result
                
        except Exception as e:
            print(f"  ⚠️ Browser fetch failed: {e}")
            if method == 'browser':
                return {'error': str(e), 'event_id': event_id}
    
    # Direct API fallback
    if method == 'direct' or method == 'auto':
        result = {'event_id': event_id, 'direct_apis': {}}
        endpoints = [
            f'event/{event_id}/incidents',
            f'event/{event_id}/lineups',
            f'event/{event_id}/statistics',
            f'event/{event_id}/managers',
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
    ap.add_argument('--method', choices=['browser', 'direct', 'auto'], default='auto',
                    help="'auto' tries direct first, falls back to browser on 403")
    args = ap.parse_args()
    
    if args.event_id:
        result = fetch_event_data(args.event_id, method=args.method, dry_run=args.dry_run)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("Specify --event-id or --limit")
        sys.exit(1)


if __name__ == '__main__':
    main()