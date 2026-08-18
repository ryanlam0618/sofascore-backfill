#!/usr/bin/env python3
"""
Stable SofaScore API fetcher using CloakBrowser + WebShare proxy.

STRATEGY (based on empirical testing):
1. Use network capture: visit event PAGE (not API URL), intercept API responses.
   - page.goto(API_URL) = 403 "challenge" (anti-bot detects non-browser navigation)
   - page.goto(event_page) + context.on("response") = 200 (page JS makes legit API calls)
2. Retry with new browser on bad IP: ~40% of WebShare IPs are flagged by SofaScore.
   - "bad" IP: <10% API 200, >90% API 403
   - "good" IP: >95% API 200, <5% API 403
   - Relaunch browser to get new rotating IP. Expected ~2-3 retries.
3. Process multiple events per "good" browser session before IP gets stale.
4. Run each event in a subprocess to avoid CloakBrowser EPIPE crashes.

USAGE:
    from stable_proxy_fetch import StableProxyFetcher
    fetcher = StableProxyFetcher()
    data = fetcher.fetch_event(12436870)
    # data = {"event": {...}, "incidents": [...], "lineups": [...], "shotmap": [...], ...}
"""

import json
import time
import sys
import os
import subprocess
import tempfile
import logging
from typing import Optional, Dict, Any, List

# --- Configuration ---

PROXY = {
    "server": "http://p.webshare.io:80",
    "username": "aeptenjc-rotate",
    "password": "dztr57tcycoz",
}

# Which API endpoints to capture (by URL pattern match)
# These are the key data endpoints we need for backfill
TARGET_ENDPOINTS = {
    "event":          "/api/v1/event/{eid}",
    "incidents":      "/api/v1/event/{eid}/incidents",
    "lineups":        "/api/v1/event/{eid}/lineups",
    "shotmap":        "/api/v1/event/{eid}/shotmap",
    "graph":          "/api/v1/event/{eid}/graph",
    "statistics":     "/api/v1/event/{eid}/statistics",
    "comments":       "/api/v1/event/{eid}/comments",
    "votes":          "/api/v1/event/{eid}/votes",
    "managers":       "/api/v1/event/{eid}/managers",
    "pregame-form":   "/api/v1/event/{eid}/pregame-form",
    "average-positions": "/api/v1/event/{eid}/average-positions",
    "web-odds":       "/api/v1/event/{eid}/odds/1/web-odds",
    "highlights":     "/api/v1/event/{eid}/highlights",
    "featured-players": "/api/v1/event/{eid}/featured-players",
    "achievements":   "/api/v1/event/{eid}/achievements",
}

# Retry config
MAX_IP_RETRIES = 5          # Max browser relaunches per event (each gets new IP)
PAGE_TIMEOUT_MS = 45000     # Event page load timeout (proxy is slow)
API_WAIT_SEC = 8            # Wait after page load for API calls to complete
INTER_EVENT_DELAY = 2       # Delay between events in same browser session
MAX_EVENTS_PER_BROWSER = 5  # Rotate browser after N events (avoid stale IP/session)
COOLDOWN_ON_FAIL = 3        # Seconds to wait between retry attempts

# Threshold for "good" IP: at least this many API 200s
# Empirically: good IP → 100-130 API 200s; bad IP → 0-4 API 200s
MIN_OK_FOR_GOOD_IP = 10

# --- Logging ---
logger = logging.getLogger("stable_proxy_fetch")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)

# --- Subprocess worker script ---
# Each event runs in a subprocess to avoid EPIPE crashes in CloakBrowser's
# Node.js Playwright wrapper.
_WORKER_SCRIPT = r'''
import json, time, sys, os
sys.path.insert(0, '/root/.openclaw/workspace/sofascore-backfill/.runner-venv/lib/python3.11/site-packages')

from cloakbrowser import launch

PROXY = {
    "server": "http://p.webshare.io:80",
    "username": "aeptenjc-rotate",
    "password": "dztr57tcycoz",
}

EVENT_ID = int(sys.argv[1])
OUTPUT_FILE = sys.argv[2]
PAGE_TIMEOUT_MS = int(sys.argv[3]) if len(sys.argv) > 3 else 45000
API_WAIT_SEC = int(sys.argv[4]) if len(sys.argv) > 4 else 8

EVENT_PAGE = f"https://www.sofascore.com/event/{EVENT_ID}"

browser = launch(headless=True, humanize=True, proxy=PROXY)
context = browser.new_context()
page = context.new_page()

# Network capture: intercept ALL Sofascore API responses
api_data = {}
api_status = {}

def capture(resp):
    url = resp.url
    if "/api/v1/" in url and "sofascore.com" in url:
        try:
            status = resp.status
            parts = url.rstrip("/").split("/api/v1/")
            ep = parts[1] if len(parts) > 1 else "unknown"

            if status == 200:
                try:
                    body = resp.json()
                    api_data[ep] = body
                except:
                    try:
                        body = resp.text()
                        api_data[ep] = body
                    except:
                        pass
            api_status[ep] = status
        except:
            pass

page.on("response", capture)

# Visit event page (triggers page's own JS to make API calls)
try:
    resp = page.goto(EVENT_PAGE, timeout=PAGE_TIMEOUT_MS)
    page_status = resp.status if resp else 0
except Exception:
    page_status = 0

# Wait for API calls to complete
time.sleep(API_WAIT_SEC)

ok_count = sum(1 for s in api_status.values() if s == 200)
fail_count = sum(1 for s in api_status.values() if s == 403)

result = {
    "event_id": EVENT_ID,
    "page_status": page_status,
    "api_ok": ok_count,
    "api_403": fail_count,
    "api_status": api_status,
    "data": api_data,
    "endpoints_captured": list(api_data.keys()),
}

with open(OUTPUT_FILE, "w") as f:
    json.dump(result, f)

try:
    page.close()
    context.close()
    browser.close()
except:
    pass
'''


class StableProxyFetcher:
    """Stable SofaScore API fetcher with proxy + retry logic."""

    def __init__(self, proxy: Optional[Dict] = None):
        self.proxy = proxy or PROXY
        self._worker_path = None
        self._write_worker_script()

    def _write_worker_script(self):
        """Write the subprocess worker script to a temp file."""
        fd, path = tempfile.mkstemp(suffix=".py", prefix="cloak_worker_")
        with os.fdopen(fd, "w") as f:
            f.write(_WORKER_SCRIPT)
        self._worker_path = path

    def _run_worker(self, event_id: int, timeout: int = 90) -> Optional[Dict]:
        """Run the CloakBrowser worker in a subprocess. Returns result dict or None."""
        output_file = tempfile.mktemp(suffix=".json", prefix="cloak_result_")
        try:
            proc = subprocess.run(
                [
                    "/root/.openclaw/workspace/sofascore-backfill/.runner-venv/bin/python3",
                    self._worker_path,
                    str(event_id),
                    output_file,
                    str(PAGE_TIMEOUT_MS),
                    str(API_WAIT_SEC),
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            if proc.returncode != 0:
                logger.debug(f"Worker exit code {proc.returncode}: {proc.stderr[:200]}")
                # EPIPE or other crash — still try to read output file

            if os.path.exists(output_file):
                with open(output_file) as f:
                    return json.load(f)
            return None
        except subprocess.TimeoutExpired:
            logger.debug(f"Worker timed out for event {event_id}")
            return None
        except Exception as e:
            logger.debug(f"Worker error: {e}")
            return None
        finally:
            if os.path.exists(output_file):
                os.unlink(output_file)

    def _is_good_ip(self, result: Dict) -> bool:
        """Check if the result indicates a clean (unflagged) proxy IP."""
        if result is None:
            return False
        ok = result.get("api_ok", 0)
        fail = result.get("api_403", 0)
        total = ok + fail
        if total == 0:
            # Page itself failed (403 or timeout) — bad IP
            return result.get("page_status", 0) == 200
        return ok >= MIN_OK_FOR_GOOD_IP and fail < ok

    def fetch_event(self, event_id: int) -> Optional[Dict[str, Any]]:
        """
        Fetch all API data for a single event.
        
        Returns dict with:
            - "data": {endpoint: response_body} for successful endpoints
            - "page_status": HTTP status of event page
            - "api_ok": count of successful API calls
            - "api_403": count of blocked API calls
            - "endpoints_captured": list of endpoint names captured
            - "retries": number of IP retries needed
        
        Returns None if all retries failed.
        """
        for attempt in range(1, MAX_IP_RETRIES + 1):
            logger.info(f"Event {event_id} — attempt {attempt}/{MAX_IP_RETRIES}")
            
            result = self._run_worker(event_id)
            
            if result is None:
                logger.warning(f"Event {event_id} — worker crashed, retrying...")
                time.sleep(COOLDOWN_ON_FAIL)
                continue

            if self._is_good_ip(result):
                retries = attempt - 1
                logger.info(
                    f"Event {event_id} — ✅ OK "
                    f"(API: {result['api_ok']} ok, {result['api_403']} blocked, "
                    f"{retries} retries)"
                )
                result["retries"] = retries
                return result
            else:
                logger.warning(
                    f"Event {event_id} — ❌ bad IP "
                    f"(page: {result.get('page_status', '?')}, "
                    f"API: {result.get('api_ok', 0)} ok, "
                    f"{result.get('api_403', 0)} blocked)"
                )
                time.sleep(COOLDOWN_ON_FAIL)

        logger.error(f"Event {event_id} — FAILED after {MAX_IP_RETRIES} retries")
        return None

    def fetch_events_batch(self, event_ids: List[int]) -> Dict[int, Optional[Dict]]:
        """Fetch multiple events. Returns {event_id: result_or_None}."""
        results = {}
        for i, eid in enumerate(event_ids):
            logger.info(f"--- Event {eid} ({i+1}/{len(event_ids)}) ---")
            result = self.fetch_event(eid)
            results[eid] = result
            if i < len(event_ids) - 1:
                time.sleep(INTER_EVENT_DELAY)
        return results

    def close(self):
        """Cleanup temp files."""
        if self._worker_path and os.path.exists(self._worker_path):
            os.unlink(self._worker_path)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# --- CLI for testing ---
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Stable SofaScore proxy fetcher")
    parser.add_argument("event_ids", nargs="+", type=int, help="Event IDs to fetch")
    parser.add_argument("--json-out", default=None, help="Save results to JSON file")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    with StableProxyFetcher() as fetcher:
        results = fetcher.fetch_events_batch(args.event_ids)

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    ok = sum(1 for r in results.values() if r is not None)
    fail = sum(1 for r in results.values() if r is None)
    print(f"Total: {len(results)} | OK: {ok} | Failed: {fail}")

    for eid, r in results.items():
        if r:
            print(f"  Event {eid}: ✅ {r['api_ok']} API ok, {r['api_403']} blocked, {r.get('retries',0)} retries")
            print(f"    Endpoints: {', '.join(r.get('endpoints_captured', [])[:8])}")
        else:
            print(f"  Event {eid}: ❌ FAILED")

    if args.json_out:
        # Save full data
        serializable = {}
        for eid, r in results.items():
            if r:
                serializable[eid] = {
                    "event_id": eid,
                    "page_status": r["page_status"],
                    "api_ok": r["api_ok"],
                    "api_403": r["api_403"],
                    "retries": r.get("retries", 0),
                    "endpoints": r.get("endpoints_captured", []),
                    "data_keys": list(r.get("data", {}).keys()),
                }
            else:
                serializable[eid] = None

        with open(args.json_out, "w") as f:
            json.dump(serializable, f, indent=2, default=str)
        print(f"\nResults saved to {args.json_out}")
