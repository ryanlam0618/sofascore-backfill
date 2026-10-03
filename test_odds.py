#!/usr/bin/env python3
"""Test the fixed upsert_odds function with a real event."""

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
    
    # Fetch odds for a known UCL 17/18 event
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
    print('Odds keys: %s' % list(data.keys()))
    if 'odds' in data:
        odds = data['odds']
        print('First odds entry: %s' % odds[0] if odds else 'empty')
        for o in odds[:3]:
            print('  bookmakerId: %s, market: %s, 1: %s, X: %s, 2: %s' % (
                o.get('bookmakerId'), o.get('market'), o.get('1'), o.get('X'), o.get('2')))
    
    # Test the fixed upsert_odds logic
    conn = mysql.connector.connect(
        host=os.getenv('MYSQL_HOST', '127.0.0.1'),
        port=int(os.getenv('MYSQL_PORT', '3306')),
        user=os.getenv('MYSQL_USER', 'root'),
        password=os.getenv('MYSQL_PASSWORD', ''),
        database=os.getenv('MYSQL_DATABASE', 'appdb'),
        charset='utf8mb4',
    )
    cur = conn.cursor()
    
    if 'odds' in data and data['odds']:
        n = 0
        for o in data['odds']:
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
                    o.get("marketId"), o.get("marketName"), o.get("marketGroup"), o.get("marketPeriod"),
                    o.get("structureType"), o.get("suspended", 0), o.get("choiceName"),
                    o.get("initialFractionalValue"), o.get("fractionalValue"), o.get("winning", 0),
                    o.get("bookmakerId"), o.get("bookmakerName"), o.get("oddsType", "1x2"),
                    o.get("1"), o.get("X"), o.get("2"), o.get("timestamp")
                ))
                n += 1
            except Exception as e:
                print('  Error inserting odds: %s' % e)
                conn.rollback()
        conn.commit()
        print("\n\u2705 upsert_odds SUCCESS! Inserted %s rows" % n)
    else:
        print("No odds data")
    
    cur.close()
    conn.close()

asyncio.run(test_upsert())