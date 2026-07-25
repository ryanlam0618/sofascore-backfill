#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import csv
import json
import os
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from playwright.async_api import async_playwright
import yaml

WORKDIR = Path(__file__).parent
COMPETITIONS_FILE = WORKDIR / 'competitions_10y.yaml'
DISCOVERIES_FILE = WORKDIR / 'discoveries.json'
OUT_JSON = WORKDIR / 'audit_24x10_results.json'
OUT_CSV = WORKDIR / 'audit_24x10_results.csv'
BASE = 'https://www.sofascore.com'
API_BASE = 'https://www.sofascore.com/api/v1'

load_dotenv(WORKDIR / '.env')


def load_targets() -> list[dict[str, Any]]:
    comps = yaml.safe_load(COMPETITIONS_FILE.read_text())['competitions']
    discoveries = json.loads(DISCOVERIES_FILE.read_text()) if DISCOVERIES_FILE.exists() else {}
    targets = []
    for comp in comps:
        name = comp['name']
        disc = discoveries.get(name, {})
        seasons = disc.get('ten_yr_seasons') or []
        season_map = disc.get('seasons') or {}
        for sid in seasons:
            sid_str = str(sid)
            targets.append({
                'competition': name,
                'ut_id': comp['ut_id'],
                'category_id': comp['category_id'],
                'cat_country': comp.get('cat_country', ''),
                'season_id': sid_str,
                'season_label': season_map.get(sid_str, ''),
                'url': None,
                'http_status': None,
                'final_url': None,
                'ok': False,
                'source': 'discoveries',
                'note': '',
            })
    return targets


async def infer_afc_two_current() -> dict[str, Any]:
    proxy_pass = os.getenv('SOFA_PROXY_PASS', '')
    launch_kwargs: dict[str, Any] = {
        'headless': True,
        'args': ['--no-sandbox', '--disable-dev-shm-usage'],
    }
    if proxy_pass:
        launch_kwargs['proxy'] = {
            'server': f"http://{os.getenv('SOFA_PROXY_HOST', 'p.webshare.io')}:{os.getenv('SOFA_PROXY_PORT', '80')}",
            'username': os.getenv('SOFA_PROXY_USER', ''),
            'password': proxy_pass,
        }
    async with async_playwright() as p:
        browser = await p.chromium.launch(**launch_kwargs)
        page = await browser.new_page()
        season_ids: set[str] = set()
        try:
            resp = await page.goto('https://www.sofascore.com/football/tournament/asia/afc-cup/668', wait_until='domcontentloaded', timeout=60000)
            await page.wait_for_timeout(10000)
            final_url = page.url
            m = re.search(r'#id:(\d+)', final_url)
            if m:
                season_ids.add(m.group(1))
            text = await page.locator('body').inner_text()
            label = None
            m2 = re.search(r'Select season in unique tournament header\s*(\S+)', text)
            if m2:
                label = m2.group(1)
            return {
                'page_status': resp.status if resp else None,
                'final_url': final_url,
                'current_season_id': next(iter(season_ids), None),
                'current_label': label,
                'note': 'derived from live tournament page because /seasons endpoint returns 403',
            }
        finally:
            await browser.close()


async def audit_target(page, target: dict[str, Any]) -> dict[str, Any]:
    target['url'] = f"{API_BASE}/unique-tournament/{target['ut_id']}/season/{target['season_id']}/info"
    try:
        resp = await page.request.get(target['url'], timeout=30000)
        target['http_status'] = resp.status
        target['final_url'] = target['url']
        target['ok'] = resp.status == 200
        if not target['ok']:
            body = await resp.text()
            target['note'] = body[:200]
    except Exception as e:
        target['note'] = str(e)
        target['ok'] = False
    return target


async def main() -> None:
    results = load_targets()
    afc_two = await infer_afc_two_current()
    if afc_two.get('current_season_id'):
        results.append({
            'competition': 'AFC Champions League Two',
            'ut_id': 668,
            'category_id': 1467,
            'cat_country': 'asia',
            'season_id': str(afc_two['current_season_id']),
            'season_label': afc_two.get('current_label') or '',
            'url': None,
            'http_status': afc_two.get('page_status'),
            'final_url': afc_two.get('final_url'),
            'ok': afc_two.get('page_status') == 200,
            'source': 'live_page_probe',
            'note': afc_two.get('note', ''),
        })

    proxy_pass = os.getenv('SOFA_PROXY_PASS', '')
    launch_kwargs: dict[str, Any] = {
        'headless': True,
        'args': ['--no-sandbox', '--disable-dev-shm-usage'],
    }
    if proxy_pass:
        launch_kwargs['proxy'] = {
            'server': f"http://{os.getenv('SOFA_PROXY_HOST', 'p.webshare.io')}:{os.getenv('SOFA_PROXY_PORT', '80')}",
            'username': os.getenv('SOFA_PROXY_USER', ''),
            'password': proxy_pass,
        }

    async with async_playwright() as p:
        browser = await p.chromium.launch(**launch_kwargs)
        page = await browser.new_page()
        audited = []
        for idx, row in enumerate(results, start=1):
            if row['source'] == 'live_page_probe':
                audited.append(row)
                continue
            audited_row = await audit_target(page, row)
            audited.append(audited_row)
            if idx % 10 == 0:
                print(f"progress {idx}/{len(results)} ok={sum(1 for r in audited if r.get('ok'))}", flush=True)
        await browser.close()

    OUT_JSON.write_text(json.dumps(audited, ensure_ascii=False, indent=2) + '\n')
    with OUT_CSV.open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['competition','ut_id','category_id','cat_country','season_id','season_label','source','url','http_status','final_url','ok','note'])
        writer.writeheader()
        writer.writerows(audited)

    ok_count = sum(1 for r in audited if r['ok'])
    summary = {
        'rows': len(audited),
        'ok_count': ok_count,
        'non_ok_count': len(audited) - ok_count,
        'afc_two_probe': afc_two,
        'json': str(OUT_JSON),
        'csv': str(OUT_CSV),
    }
    (WORKDIR / 'audit_24x10_summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
