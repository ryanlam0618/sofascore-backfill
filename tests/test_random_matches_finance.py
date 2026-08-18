#!/usr/bin/env python3
"""
Test Random Matches - Batch 3 (Finance Agent)
==============================================
For 6 competitions, randomly pick 1 match per season, fetch ALL 22 endpoints,
store in SQLite.

Competitions: FA Cup, J.League Cup, J1 League, K League 1, La Liga, Ligue 1
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv('/root/.openclaw/workspace/.env')

# ── Config ───────────────────────────────────────────────────────
PROXY_SERVER = os.getenv('SOFA_PROXY_HOST', 'p.webshare.io')
PROXY_PORT = os.getenv('SOFA_PROXY_PORT', '80')
PROXY_USERNAME = os.getenv('SOFA_PROXY_USER', 'aeptenjc-rotate')
PROXY_PASSWORD = os.getenv('SOFA_PROXY_PASS', 'dztr57tcycoz')
PROXY_URL = f"http://{PROXY_SERVER}:{PROXY_PORT}"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)

DB_PATH = Path("/root/.openclaw/workspace/sofascore-backfill/data/test_random_matches_finance.sqlite")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

RESULT_PATH = Path("/root/.openclaw/workspace/subagent_results/finance_result.json")
RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)

# ── Competitions ─────────────────────────────────────────────────
# From discoveries.json
COMPETITIONS = {
    "FA Cup": {
        "ut_id": 19, "cat_id": 1, "mode": "cup",
        "ten_yr_seasons": [
            ("82557", "25/26"), ("67958", "24/25"), ("55978", "23/24"),
            ("41913", "22/23"), ("39384", "21/22"), ("34650", "20/21"),
            ("25944", "19/20"), ("19402", "18/19"), ("15100", "17/18"),
            ("12550", "16/17"), ("11016", "15/16"),
        ],
    },
    "J.League Cup": {
        "ut_id": 101, "cat_id": 52, "mode": "cup",
        "ten_yr_seasons": [
            ("70910", "2025"), ("58111", "2024"), ("48588", "2023"),
            ("40504", "2022"), ("35588", "2021"), ("27196", "2020"),
            ("22631", "2019"), ("16256", "2018"), ("13015", "2017"),
            ("11322", "2016"),
        ],
    },
    "J1 League": {
        "ut_id": 196, "cat_id": 52, "mode": "national",
        "ten_yr_seasons": [
            ("69871", "2025"), ("57353", "2024"), ("48055", "2023"),
            ("40230", "2022"), ("35273", "2021"), ("27042", "2020"),
            ("22479", "2019"), ("16004", "2018"), ("12879", "2017"),
            ("11307", "2016"),
        ],
    },
    "K League 1": {
        "ut_id": 410, "cat_id": 291, "mode": "national",
        "ten_yr_seasons": [
            ("70830", "2025"), ("57878", "2024"), ("48379", "2023"),
            ("40409", "2022"), ("35558", "2021"), ("27192", "2020"),
            ("22409", "2019"), ("16193", "2018"), ("12908", "2017"),
            ("11290", "2016"),
        ],
    },
    "La Liga": {
        "ut_id": 8, "cat_id": 32, "mode": "national",
        "ten_yr_seasons": [
            ("77559", "25/26"), ("61643", "24/25"), ("52376", "23/24"),
            ("42409", "22/23"), ("37223", "21/22"), ("32501", "20/21"),
            ("24127", "19/20"), ("18020", "18/19"), ("13662", "17/18"),
            ("11906", "16/17"),
        ],
    },
    "Ligue 1": {
        "ut_id": 34, "cat_id": 7, "mode": "national",
        "ten_yr_seasons": [
            ("77356", "25/26"), ("61736", "24/25"), ("52571", "23/24"),
            ("42273", "22/23"), ("37167", "21/22"), ("28222", "20/21"),
            ("23872", "19/20"), ("17279", "18/19"), ("13384", "17/18"),
            ("11648", "16/17"),
        ],
    },
}

# ── 22 Endpoints ────────────────────────────────────────────────
ENDPOINTS = [
    ("event", "/api/v1/event/{event_id}"),
    ("statistics", "/api/v1/event/{event_id}/statistics"),
    ("lineups", "/api/v1/event/{event_id}/lineups"),
    ("incidents", "/api/v1/event/{event_id}/incidents"),
    ("graph", "/api/v1/event/{event_id}/graph"),
    ("managers", "/api/v1/event/{event_id}/managers"),
    ("comments", "/api/v1/event/{event_id}/comments"),
    ("votes", "/api/v1/event/{event_id}/votes"),
    ("pregame_form", "/api/v1/event/{event_id}/pregame-form"),
    ("official_tweets", "/api/v1/event/{event_id}/official-tweets"),
    ("best_players_summary", "/api/v1/event/{event_id}/best-players/summary"),
    ("average_positions", "/api/v1/event/{event_id}/average-positions"),
    ("highlights", "/api/v1/event/{event_id}/highlights"),
    ("media_summary", "/api/v1/event/{event_id}/media/summary/country/JP"),
    ("odds_featured", "/api/v1/event/{event_id}/odds/1/featured"),
    ("odds_all", "/api/v1/event/{event_id}/odds/1/all"),
    ("provider_winning_odds", "/api/v1/event/{event_id}/provider/1/winning-odds"),
    ("shotmap", "/api/v1/event/{event_id}/shotmap"),
    ("player_statistics", "/api/v1/event/{event_id}/player-statistics"),
    ("momentum", "/api/v1/event/{event_id}/momentum"),
    ("h2h", "/api/v1/event/{event_id}/h2h"),
    ("tv", "/api/v1/event/{event_id}/tv"),
]


# ── SQLite ───────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS test_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        competition TEXT,
        season_id TEXT,
        season_year TEXT,
        match_id BIGINT,
        endpoint TEXT,
        status_code INTEGER,
        has_data BOOLEAN,
        fields TEXT,
        sample TEXT,
        fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS test_summary (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        competition TEXT,
        season_id TEXT,
        match_id BIGINT,
        match_name TEXT,
        endpoints_tested INTEGER,
        endpoints_success INTEGER,
        endpoints_empty INTEGER,
        endpoints_failed INTEGER,
        fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    conn.commit()
    return conn


def save_result(conn, competition, season_id, season_year, match_id, endpoint, status_code, has_data, fields, sample):
    conn.execute(
        "INSERT INTO test_results (competition, season_id, season_year, match_id, endpoint, status_code, has_data, fields, sample) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (competition, season_id, season_year, match_id, endpoint, status_code, int(has_data),
         json.dumps(fields, ensure_ascii=False) if fields else "[]",
         json.dumps(sample, ensure_ascii=False, default=str) if sample else "null"),
    )


def save_summary(conn, competition, season_id, match_id, match_name, tested, success, empty, failed):
    conn.execute(
        "INSERT INTO test_summary (competition, season_id, match_id, match_name, endpoints_tested, endpoints_success, endpoints_empty, endpoints_failed) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (competition, season_id, match_id, match_name, tested, success, empty, failed),
    )
    conn.commit()


# ── Helper: extract fields from JSON ─────────────────────────────
def extract_fields(data, prefix="", max_depth=3):
    """Extract field names from a JSON object."""
    fields = []
    if isinstance(data, dict):
        for k, v in data.items():
            full = f"{prefix}.{k}" if prefix else k
            fields.append(full)
            if max_depth > 0 and isinstance(v, (dict, list)):
                fields.extend(extract_fields(v, full, max_depth - 1))
    elif isinstance(data, list) and data:
        fields.append(f"{prefix}[]")
        if max_depth > 0 and isinstance(data[0], (dict, list)):
            fields.extend(extract_fields(data[0], f"{prefix}[0]", max_depth - 1))
    return fields


def extract_sample(data, max_items=3):
    """Extract a small sample of the data."""
    if isinstance(data, dict):
        sample = {}
        for i, (k, v) in enumerate(data.items()):
            if i >= max_items:
                break
            if isinstance(v, list):
                sample[k] = f"[array, len={len(v)}]"
            elif isinstance(v, dict):
                sample[k] = {kk: vv for j, (kk, vv) in enumerate(v.items()) if j < 3}
            else:
                sample[k] = v
        return sample
    elif isinstance(data, list):
        return f"[array, len={len(data)}]"
    return data


# ── Main logic ──────────────────────────────────────────────────
async def fetch_events_for_season(page, ut_id, season_id, mode, retries=2):
    """Fetch events list for a season via browser."""
    # Navigate to season page
    if mode == "cup":
        url = f"https://www.sofascore.com/football/unique-tournament/{ut_id}/season/{season_id}"
    else:
        url = f"https://www.sofascore.com/football/unique-tournament/{ut_id}/season/{season_id}"

    for attempt in range(retries):
        try:
            resp = await page.goto(url, timeout=30000, wait_until='domcontentloaded')
            if resp.status == 403:
                print(f"    ⚠️ 403 on season page, retry {attempt+1}")
                await asyncio.sleep(3)
                continue
            await asyncio.sleep(2)
            break
        except Exception as e:
            print(f"    ⚠️ Navigation error: {e}, retry {attempt+1}")
            await asyncio.sleep(3)

    # Fetch events via API
    api_path = f"/api/v1/unique-tournament/{ut_id}/season/{season_id}/events/last/0"
    try:
        result = await page.evaluate('''
            async (path) => {
                const resp = await fetch(path, { credentials: "include" });
                const text = await resp.text();
                let body;
                try { body = JSON.parse(text); } catch(e) { return { status: resp.status, events: [] }; }
                return { status: resp.status, events: body.events || [] };
            }
        ''', api_path)
        return result.get("events", []), result.get("status", 0)
    except Exception as e:
        print(f"    ⚠️ Fetch events error: {e}")
        return [], 0


async def fetch_endpoint(page, event_id, endpoint_name, path_template):
    """Fetch a single endpoint for an event."""
    path = path_template.format(event_id=event_id)
    try:
        result = await page.evaluate('''
            async (path) => {
                const resp = await fetch(path, { credentials: "include" });
                const text = await resp.text();
                let body;
                try { body = JSON.parse(text); } catch(e) { return { status: resp.status, body: null, raw: text.substring(0, 500) }; }
                return { status: resp.status, body: body };
            }
        ''', path)
        status = result.get("status", 0)
        body = result.get("body")
        has_data = body is not None and not (isinstance(body, dict) and "error" in body and len(body) == 1)

        if has_data and isinstance(body, dict):
            # Check if it's a SofaScore error response
            if body.get("error", {}).get("code") == "NOT_FOUND":
                has_data = False

        fields = extract_fields(body) if has_data else []
        sample = extract_sample(body) if has_data else None
        return status, has_data, fields, sample
    except Exception as e:
        return 0, False, [], {"error": str(e)}


async def main():
    print("=" * 60)
    print("Test Random Matches - Batch 3 (Finance Agent)")
    print(f"DB: {DB_PATH}")
    print(f"Competitions: {len(COMPETITIONS)}")
    total_seasons = sum(len(c['ten_yr_seasons']) for c in COMPETITIONS.values())
    print(f"Total seasons: {total_seasons}")
    print("=" * 60)

    conn = init_db()
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=True,
        args=['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu', '--window-size=1920,1080'],
    )
    context = await browser.new_context(
        viewport={'width': 1920, 'height': 1080},
        user_agent=USER_AGENT,
        proxy={
            'server': PROXY_URL,
            'username': PROXY_USERNAME,
            'password': PROXY_PASSWORD,
        },
    )
    page = await context.new_page()

    all_results = []
    endpoint_quality = {}  # endpoint_name -> {total, success, fields_seen}

    for comp_name, comp_info in COMPETITIONS.items():
        ut_id = comp_info["ut_id"]
        mode = comp_info["mode"]
        seasons = comp_info["ten_yr_seasons"]

        print(f"\n{'='*60}")
        print(f"🏟️  {comp_name} (ut_id={ut_id}, {len(seasons)} seasons)")
        print(f"{'='*60}")

        for season_id, season_year in seasons:
            print(f"\n  📅 Season {season_year} (id={season_id})")

            # Fetch events
            events, http_status = await fetch_events_for_season(page, ut_id, season_id, mode)

            if not events:
                print(f"    ⚠️ No events found (HTTP {http_status}), skipping")
                save_summary(conn, comp_name, season_id, 0, "NO_EVENTS", 0, 0, 0, 0)
                continue

            # Randomly pick 1 match
            match = random.choice(events)
            match_id = match.get("id")
            home_name = match.get("homeTeam", {}).get("name", "?")
            away_name = match.get("awayTeam", {}).get("name", "?")
            match_name = f"{home_name} vs {away_name}"
            print(f"    🎲 Picked: {match_name} (id={match_id})")

            # Navigate to match page to warm cookies
            try:
                await page.goto(f"https://www.sofascore.com/event/{match_id}", timeout=30000, wait_until='domcontentloaded')
                await asyncio.sleep(2)
            except Exception as e:
                print(f"    ⚠️ Failed to navigate to match page: {e}")
                save_summary(conn, comp_name, season_id, match_id, f"NAV_ERROR: {e}", 0, 0, 0, 0)
                continue

            # Fetch all 22 endpoints
            success_count = 0
            empty_count = 0
            fail_count = 0

            for ep_name, ep_template in ENDPOINTS:
                status, has_data, fields, sample = await fetch_endpoint(page, match_id, ep_name, ep_template)

                save_result(conn, comp_name, season_id, season_year, match_id, ep_name, status, has_data, fields, sample)

                # Track endpoint quality
                if ep_name not in endpoint_quality:
                    endpoint_quality[ep_name] = {"total": 0, "success": 0, "all_fields": set()}
                endpoint_quality[ep_name]["total"] += 1
                if has_data:
                    endpoint_quality[ep_name]["success"] += 1
                    endpoint_quality[ep_name]["all_fields"].update(fields[:50])  # limit

                if status == 200 and has_data:
                    success_count += 1
                    print(f"    ✅ [{status}] {ep_name} ({len(fields)} fields)")
                elif status == 200 and not has_data:
                    empty_count += 1
                    print(f"    ⬜ [{status}] {ep_name} (empty/error)")
                else:
                    fail_count += 1
                    print(f"    ❌ [{status}] {ep_name}")

                # Sleep between requests
                await asyncio.sleep(random.uniform(1.0, 2.0))

            save_summary(conn, comp_name, season_id, match_id, match_name,
                        len(ENDPOINTS), success_count, empty_count, fail_count)

            all_results.append({
                "competition": comp_name,
                "season_id": season_id,
                "season_year": season_year,
                "match_id": match_id,
                "match_name": match_name,
                "success": success_count,
                "empty": empty_count,
                "failed": fail_count,
            })

            print(f"    📊 {success_count}✅ {empty_count}⬜ {fail_count}❌ out of {len(ENDPOINTS)}")

    # Cleanup
    await page.close()
    await context.close()
    await browser.close()
    await pw.stop()
    conn.close()

    # ── Summary ──────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("📊 FINAL SUMMARY")
    print("=" * 60)

    total_matches = len(all_results)
    total_success = sum(r["success"] for r in all_results)
    total_empty = sum(r["empty"] for r in all_results)
    total_failed = sum(r["failed"] for r in all_results)
    total_endpoints = total_matches * len(ENDPOINTS)

    print(f"Matches tested: {total_matches}")
    print(f"Endpoints tested: {total_endpoints}")
    print(f"Success: {total_success} ({100*total_success/max(1,total_endpoints):.1f}%)")
    print(f"Empty/Error: {total_empty} ({100*total_empty/max(1,total_endpoints):.1f}%)")
    print(f"Failed (non-200): {total_failed} ({100*total_failed/max(1,total_endpoints):.1f}%)")

    print("\n📋 Per Competition:")
    for comp_name in COMPETITIONS:
        comp_results = [r for r in all_results if r["competition"] == comp_name]
        if comp_results:
            s = sum(r["success"] for r in comp_results)
            e = sum(r["empty"] for r in comp_results)
            f = sum(r["failed"] for r in comp_results)
            t = len(comp_results) * len(ENDPOINTS)
            print(f"  {comp_name}: {len(comp_results)} matches, {s}✅ {e}⬜ {f}❌ / {t}")

    print("\n📋 Endpoint Quality:")
    for ep_name in ENDPOINTS:
        ep_name_str = ep_name[0]
        if ep_name_str in endpoint_quality:
            eq = endpoint_quality[ep_name_str]
            rate = 100 * eq["success"] / max(1, eq["total"])
            print(f"  {ep_name_str}: {eq['success']}/{eq['total']} ({rate:.0f}%) - {len(eq['all_fields'])} unique fields")

    # Write result JSON
    result_data = {
        "batch": 3,
        "agent": "finance",
        "competitions": list(COMPETITIONS.keys()),
        "total_matches_tested": total_matches,
        "total_endpoints_tested": total_endpoints,
        "total_success": total_success,
        "total_empty": total_empty,
        "total_failed": total_failed,
        "success_rate": round(100 * total_success / max(1, total_endpoints), 1),
        "per_competition": {},
        "per_endpoint": {},
        "matches": all_results,
        "data_quality_issues": [],
    }

    for comp_name in COMPETITIONS:
        comp_results = [r for r in all_results if r["competition"] == comp_name]
        if comp_results:
            result_data["per_competition"][comp_name] = {
                "matches_tested": len(comp_results),
                "success": sum(r["success"] for r in comp_results),
                "empty": sum(r["empty"] for r in comp_results),
                "failed": sum(r["failed"] for r in comp_results),
            }

    for ep_name_str, eq in endpoint_quality.items():
        result_data["per_endpoint"][ep_name_str] = {
            "total": eq["total"],
            "success": eq["success"],
            "success_rate": round(100 * eq["success"] / max(1, eq["total"]), 1),
            "unique_fields": len(eq["all_fields"]),
            "sample_fields": sorted(list(eq["all_fields"]))[:20],
        }

    # Identify quality issues
    for ep_name_str, eq in endpoint_quality.items():
        rate = eq["success"] / max(1, eq["total"])
        if rate < 0.5:
            result_data["data_quality_issues"].append({
                "type": "low_success_rate",
                "endpoint": ep_name_str,
                "rate": round(rate * 100, 1),
                "detail": f"Only {eq['success']}/{eq['total']} successful",
            })

    with open(str(RESULT_PATH), "w") as f:
        json.dump(result_data, f, indent=2, ensure_ascii=False, default=str)

    print(f"\n✅ Results saved to {RESULT_PATH}")
    print(f"✅ SQLite saved to {DB_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
