#!/usr/bin/env python3
"""Test the odds endpoint structure."""

import asyncio
from curl_cffi import requests as cffi_requests

async def test_odds():
    # credentials come from the environment (never hardcoded):
    #   PROXY_USER / PROXY_PW  (export them, e.g. via .env)
    proxy = {'ip': '179.198.16.221', 'port': '6840',
             'user': __import__('os').environ.get('PROXY_USER', ''),
             'pw': __import__('os').environ.get('PROXY_PW', '')}
    proxy_url = 'http://%s:%s@%s:%s' % (proxy["user"], proxy["pw"], proxy["ip"], proxy["port"])
    
    r = await asyncio.to_thread(
        cffi_requests.get,
        'https://api.sofascore.com/api/v1/event/7551880/odds/1/all',
        impersonate='chrome124',
        proxies={'http': proxy_url, 'https': proxy_url},
        timeout=20
    )
    print('Status: %s' % r.status_code)
    if r.status_code == 200:
        data = r.json()
        print('Keys: %s' % list(data.keys()))
        for k, v in data.items():
            if isinstance(v, list) and len(v) > 0:
                print('  %s: list of %s items, first: %s' % (k, len(v), v[0]))
            elif isinstance(v, dict):
                print('  %s: dict with keys: %s' % (k, list(v.keys())))
            else:
                print('  %s: %s' % (k, v))

asyncio.run(test_odds())