#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Minimal test-only SofaScore browser scraping probe.
Purpose: verify whether the old aifootballbets browser scraping approach can still be revived
on this Linux host with DrissionPage + Chromium.

Tests:
1) Open direct API URL in browser tab and inspect `tab.json`
2) Open match page and inspect HTML / __NEXT_DATA__ tokens for shotmap/xg/xgot
3) Print sample shot object keys if available

Usage:
  python3 test_old_browser_scrape.py --event-id 7073008
  python3 test_old_browser_scrape.py --event-id 7073008 --keep-open
"""

from __future__ import annotations

import argparse
import json
import re
import time
from typing import Any

from DrissionPage import ChromiumPage, ChromiumOptions


MATCH_URLS = [
    "https://www.sofascore.com/event/{event_id}",
    "https://www.sofascore.com/football/match/_/_#id:{event_id}",
]

API_URLS = [
    "https://www.sofascore.com/api/v1/event/{event_id}/shotmap",
    "https://www.sofascore.com/api/v1/event/{event_id}/statistics",
    "https://www.sofascore.com/api/v1/event/{event_id}",
]


def make_page(headless: bool = True) -> ChromiumPage:
    co = ChromiumOptions()
    co.set_browser_path('/usr/bin/chromium')
    if headless:
        co.headless(True)
    for arg in [
        '--headless=new',
        '--no-sandbox',
        '--disable-dev-shm-usage',
        '--disable-gpu',
        '--disable-blink-features=AutomationControlled',
        '--window-size=1920,1080',
    ]:
        co.set_argument(arg)
    return ChromiumPage(co)


def try_api_json(page: ChromiumPage, url: str, timeout: int) -> dict[str, Any]:
    out: dict[str, Any] = {'url': url}
    try:
        page.get(url, timeout=timeout)
        time.sleep(1.5)
        out['final_url'] = page.url
        out['title'] = page.title
        data = page.json
        out['json_type'] = type(data).__name__
        if isinstance(data, dict):
            out['json_keys'] = sorted(list(data.keys()))[:30]
            if 'error' in data:
                out['error_payload'] = data['error']
            if 'shotmap' in data and isinstance(data['shotmap'], list):
                out['shot_count'] = len(data['shotmap'])
                if data['shotmap']:
                    first = data['shotmap'][0]
                    if isinstance(first, dict):
                        out['first_shot_keys'] = sorted(first.keys())
                        out['first_shot_sample'] = json.dumps(first, ensure_ascii=False)[:1200]
            return out
        out['json_preview'] = str(data)[:500]
        return out
    except Exception as e:
        out['error'] = repr(e)
        return out


def extract_next_data(html: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        'has_shotmap_token': '"shotmap":' in html,
        'has_xg_token': '"xg":' in html,
        'has_xgot_token': '"xgot":' in html,
        'has_expectedGoals_token': 'expectedGoals' in html,
    }
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html)
    result['has_next_data'] = bool(m)
    if not m:
        return result
    txt = m.group(1)
    result['next_has_shotmap'] = '"shotmap":' in txt
    result['next_has_xg'] = '"xg":' in txt
    result['next_has_xgot'] = '"xgot":' in txt
    result['next_has_expectedGoals'] = 'expectedGoals' in txt
    for token in ['"shotmap":', '"xg":', '"xgot":', 'expectedGoals']:
        idx = txt.find(token)
        result[f'idx_{token}'] = idx
        if idx >= 0:
            result[f'snip_{token}'] = txt[max(0, idx - 180):idx + 900]
    return result


def try_match_page(page: ChromiumPage, url: str, timeout: int) -> dict[str, Any]:
    out: dict[str, Any] = {'url': url}
    try:
        page.get(url, timeout=timeout)
        time.sleep(5)
        html = page.html or ''
        out['final_url'] = page.url
        out['title'] = page.title
        out['html_len'] = len(html)
        out.update(extract_next_data(html))
        return out
    except Exception as e:
        out['error'] = repr(e)
        return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--event-id', type=int, required=True)
    ap.add_argument('--timeout', type=int, default=25)
    ap.add_argument('--keep-open', action='store_true')
    ap.add_argument('--headed', action='store_true')
    args = ap.parse_args()

    page = make_page(headless=not args.headed)
    results: dict[str, Any] = {'event_id': args.event_id, 'api_tests': [], 'match_tests': []}

    try:
        for tmpl in API_URLS:
            results['api_tests'].append(try_api_json(page, tmpl.format(event_id=args.event_id), args.timeout))

        for tmpl in MATCH_URLS:
            results['match_tests'].append(try_match_page(page, tmpl.format(event_id=args.event_id), args.timeout))

        print(json.dumps(results, ensure_ascii=False, indent=2))

        if args.keep_open:
            print('\n[INFO] Browser kept open for manual inspection. Ctrl+C to exit.')
            while True:
                time.sleep(1)
    finally:
        if not args.keep_open:
            try:
                page.quit()
            except Exception:
                pass


if __name__ == '__main__':
    main()
