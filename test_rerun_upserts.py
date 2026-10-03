#!/usr/bin/env python3
"""Test all upsert functions from the rerun script."""

import asyncio
import sys
sys.path.insert(0, '/root/.openclaw/workspace/sofascore-backfill')

from gen4_phaseB_17_18_rerun import (
    upsert_match, upsert_incidents, upsert_lineups, 
    upsert_statistics, upsert_shotmap, upsert_odds
)
import mysql.connector
import os
from dotenv import load_dotenv
load_dotenv('.env')

from curl_cffi import requests as cffi_requests

async def test_all():
    # credentials come from the environment (never hardcoded):
    #   PROXY_USER / PROXY_PW  (export them, e.g. via .env)
    proxy = {'ip': '179.198.16.221', 'port': '6840',
             'user': __import__('os').environ.get('PROXY_USER', ''),
             'pw': __import__('os').environ.get('PROXY_PW', '')}
    proxy_url = 'http://%s:***@%s:%s' % (proxy["user"], proxy["pw"], proxy["ip"], proxy["port"])
    
    endpoints = {
        "event": "/api/v1/event/7551880",
        "incidents": "/api/v1/event/7551880/incidents",
        "lineups": "/api/v1/event/7551880/lineups",
        "statistics": "/api/v1/event/7551880/statistics",
        "shotmap": "/api/v1/event/7551880/shotmap",
        "odds": "/api/v1/event/7551880/odds/1/all",
    }
    
    bundle = {}
    for ep_name, path in endpoints.items():
        r = await asyncio.to_thread(
            cffi_requests.get,
            'https://api.sofascore.com' + path,
            impersonate='chrome124',
            proxies={'http': proxy_url, 'https': proxy_url},
            timeout=20
        )
        print('%s: %s' % (ep_name, r.status_code))
        if r.status_code == 200:
            try:
                bundle[ep_name] = r.json()
            except:
                bundle[ep_name] = None
        else:
            bundle[ep_name] = None
    
    # Now test inserts using the fixed functions from the module
    conn = mysql.connector.connect(
        host=os.getenv('MYSQL_HOST', '127.0.0.1'),
        port=int(os.getenv('MYSQL_PORT', '3306')),
        user=os.getenv('MYSQL_USER', 'root'),
        password=__import__('os').environ.get('MYSQL_' + 'PASSWORD', ''),
        database=os.getenv('MYSQL_DATABASE', 'appdb'),
        charset='utf8mb4',
    )
    
    event_data = bundle.get("event", {}).get("event", {})
    if not event_data:
        event_data = bundle.get("event", {})
    
    print("Event data keys: %s" % list(event_data.keys()) if event_data else "None")
    
    # Test upsert_match
    try:
        match_id = upsert_match(conn, event_data, 13415, 7)
        print("\u2705 matches: %s" % match_id)
    except Exception as e:
        print("\u274c matches: %s" % e)
        conn.rollback()
    
    # Test upsert_incidents
    if bundle.get("incidents"):
        try:
            n = upsert_incidents(conn, event_data.get("id"), bundle["incidents"])
            print("\u2705 incidents: %s rows" % n)
        except Exception as e:
            print("\u274c incidents: %s" % e)
            conn.rollback()
    else:
        print("No incidents data")
    
    # Test upsert_lineups
    if bundle.get("lineups"):
        try:
            n = upsert_lineups(conn, event_data.get("id"), bundle["lineups"], event_data)
            print("\u2705 lineups: %s rows" % n)
        except Exception as e:
            print("\u274c lineups: %s" % e)
            conn.rollback()
    else:
        print("No lineups data")
    
    # Test upsert_statistics
    if bundle.get("statistics"):
        try:
            n = upsert_statistics(conn, event_data.get("id"), bundle["statistics"])
            print("\u2705 statistics: %s rows" % n)
        except Exception as e:
            print("\u274c statistics: %s" % e)
            conn.rollback()
    else:
        print("No statistics data")
    
    # Test upsert_shotmap
    if bundle.get("shotmap"):
        try:
            n = upsert_shotmap(conn, event_data.get("id"), bundle["shotmap"])
            print("\u2705 shotmap: %s rows" % n)
        except Exception as e:
            print("\u274c shotmap: %s" % e)
            conn.rollback()
    else:
        print("No shotmap data")
    
    # Test upsert_odds
    if bundle.get("odds"):
        try:
            n = upsert_odds(conn, event_data.get("id"), bundle["odds"])
            print("\u2705 odds: %s rows" % n)
        except Exception as e:
            print("\u274c odds: %s" % e)
            conn.rollback()
    else:
        print("No odds data")
    
    conn.close()

asyncio.run(test_all())
