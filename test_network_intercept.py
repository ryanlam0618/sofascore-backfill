#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Browser network interception probe for SofaScore match pages.
Goal: verify whether loading a match page triggers hidden XHR/fetch requests
for event / statistics / shotmap / incidents and recover their response bodies.

Usage:
  python3 test_network_intercept.py --event-id 7073008
"""

from __future__ import annotations

import argparse
import json
import time
from DrissionPage import ChromiumPage, ChromiumOptions


TARGET_PATTERNS = [
    '/api/v1/event/',
    '/statistics',
    '/incidents',
    '/lineups',
    '/shotmap',
]


def make_page() -> ChromiumPage:
    co = ChromiumOptions()
    co.set_browser_path('/usr/bin/chromium')
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


def packet_to_dict(packet):
    try:
        body = packet.response.body
    except Exception as e:
        body = {'_body_error': repr(e)}

    preview = None
    if isinstance(body, dict):
        preview = {
            'keys': sorted(list(body.keys()))[:30],
        }
        if 'error' in body:
            preview['error'] = body['error']
        if 'shotmap' in body and isinstance(body['shotmap'], list):
            preview['shot_count'] = len(body['shotmap'])
            if body['shotmap']:
                first = body['shotmap'][0]
                if isinstance(first, dict):
                    preview['first_shot_keys'] = sorted(first.keys())
                    preview['first_shot_sample'] = json.dumps(first, ensure_ascii=False)[:800]
        if 'home' in body and isinstance(body.get('home'), dict):
            home_players = (body.get('home') or {}).get('players') or []
            away_players = (body.get('away') or {}).get('players') or []
            preview['home_players'] = len(home_players)
            preview['away_players'] = len(away_players)
        if 'statistics' in body and isinstance(body.get('statistics'), list):
            preview['statistics_count'] = len(body['statistics'])
    elif isinstance(body, str):
        preview = body[:800]
    else:
        preview = str(body)[:800]

    return {
        'url': packet.url,
        'method': packet.method,
        'resourceType': packet.resourceType,
        'status': getattr(packet.response, 'status', None),
        'mimeType': getattr(packet.response, 'mimeType', None),
        'body_preview': preview,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--event-id', type=int, required=True)
    ap.add_argument('--timeout', type=int, default=25)
    ap.add_argument('--listen-seconds', type=int, default=10)
    args = ap.parse_args()

    page = make_page()
    result = {'event_id': args.event_id, 'captured': []}
    try:
        page.listen.start(TARGET_PATTERNS, is_regex=False, method=True, res_type=True)
        url = f'https://www.sofascore.com/event/{args.event_id}'
        page.get(url, timeout=args.timeout)
        end = time.time() + args.listen_seconds
        while time.time() < end:
            try:
                pkt = page.listen.wait(timeout=1, fit_count=False)
            except Exception:
                pkt = None
            if not pkt:
                continue
            if isinstance(pkt, list):
                for p in pkt:
                    result['captured'].append(packet_to_dict(p))
            else:
                result['captured'].append(packet_to_dict(pkt))
        result['final_url'] = page.url
        result['title'] = page.title
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        try:
            page.listen.stop()
        except Exception:
            pass
        try:
            page.quit()
        except Exception:
            pass


if __name__ == '__main__':
    main()
