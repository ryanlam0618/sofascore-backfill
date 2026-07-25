#!/usr/bin/env python3
"""Fetch SofaScore API bundle for match 14023959 via Playwright."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

MATCH_ID = 14023959
BASE = f"https://www.sofascore.com/api/v1/event/{MATCH_ID}"
OUT = Path(__file__).with_name("match_14023959_full.json")

endpoints = {
    "event": BASE,
    "lineups": f"{BASE}/lineups",
    "statistics": f"{BASE}/statistics",
    "incidents": f"{BASE}/incidents",
    "shotmap": f"{BASE}/shotmap",
    "graph": f"{BASE}/graph",
    "comments": f"{BASE}/comments",
    "odds": f"{BASE}/odds/1/all",
}

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(
        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
        extra_http_headers={"Accept": "application/json,text/plain,*/*"},
    )
    page = ctx.new_page()
    result = {}
    for name, url in endpoints.items():
        resp = page.goto(url, timeout=30000, wait_until="domcontentloaded")
        if not resp or not resp.ok:
            status = resp.status if resp else "NO_RESPONSE"
            raise RuntimeError(f"{name} failed: {status}")
        result[name] = resp.json()
        print(f"{name}: {resp.status} {len(json.dumps(result[name]))} bytes")
    browser.close()

OUT.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
print(f"wrote {OUT}")
