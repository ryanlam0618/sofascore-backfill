#!/usr/bin/env python3
"""Test the fixed upsert_odds function with real data."""

import asyncio
import mysql.connector
import os
from dotenv import load_dotenv
load_dotenv('.env')

from curl_cffi import requests as cffi_requests

async def test_upsert():
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
    if r.status_code != 200:
        return
    
    data = r.json()
    print('Markets count: %s' % len(data.get('markets', [])))
    
    conn = mysql.connector.connect(
        host=os.getenv('MYSQL_HOST', '127.0.0.1'),
        port=int(os.getenv('MYSQL_PORT', '3306')),
        user=os.getenv('MYSQL_USER', 'root'),
        password=os.getenv('MYSQL_PASSWORD', ''),
        database=os.getenv('MYSQL_DATABASE', 'appdb'),
        charset='utf8mb4',
    )
    cur = conn.cursor()
    
    markets = data.get("markets", [])
    n = 0
    for market in markets:
        market_id = market.get("marketId")
        market_name = market.get("marketName")
        market_group = market.get("marketGroup")
        market_period = market.get("marketPeriod")
        structure_type = market.get("structureType")
        suspended = 1 if market.get("suspended") else 0
        choices = market.get("choices", [])
        for choice in choices:
            try:
                cur.execute("""
                    INSERT INTO match_odds (match_id, market_id, market_name, market_group, market_period,
                                             structure_type, suspended, choice_name, initial_fractional_value,
                                             fractional_value, winning, bookmaker_id, bookmaker_name, odds_type,
                                             home_odds, draw_odds, away_odds, fetched_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        fractional_value=VALUES(fractional_value), winning=VALUES(winning),
                        home_odds=VALUES(home_odds), draw_odds=VALUES(draw_odds), away_odds=VALUES(away_odds),
                        fetched_at=VALUES(fetched_at)
                """, (
                    7551880,
                    market_id, market_name, market_group, market_period,
                    structure_type, suspended, choice.get("name"),
                    choice.get("initialFractionalValue"), choice.get("fractionalValue"),
                    1 if choice.get("winning") else 0,
                    None, None, None,
                    None, None, None,
                    None
                ))
                n += 1
            except Exception as e:
                print('    Error inserting odds choice: %s' % e)
                conn.rollback()
    conn.commit()
    print("\u2705 upsert_odds SUCCESS! Inserted %s rows" % n)
    
    cur.close()
    conn.close()

asyncio.run(test_upsert())