#!/usr/bin/env python3
"""Test the fixed upsert_match function with a real event."""

import asyncio
import mysql.connector
import os
from datetime import datetime, timezone
from dotenv import load_dotenv
load_dotenv('.env')

from curl_cffi import requests as cffi_requests

async def test_upsert():
    # Use one of the IPs from the pool
    # credentials come from the environment (never hardcoded):
    #   PROXY_USER / PROXY_PW  (export them, e.g. via .env)
    proxy = {'ip': '179.198.16.221', 'port': '6840',
             'user': __import__('os').environ.get('PROXY_USER', ''),
             'pw': __import__('os').environ.get('PROXY_PW', '')}
    proxy_url = 'http://%s:%s@%s:%s' % (proxy["user"], proxy["pw"], proxy["ip"], proxy["port"])
    
    # Fetch a known UCL 17/18 event
    r = await asyncio.to_thread(
        cffi_requests.get,
        'https://api.sofascore.com/api/v1/event/7551880',
        impersonate='chrome124',
        proxies={'http': proxy_url, 'https': proxy_url},
        timeout=20
    )
    print('Status: %s' % r.status_code)
    if r.status_code != 200:
        return
    
    data = r.json()
    event = data.get('event', {})
    print('Event ID: %s' % event.get('id'))
    print('Home: %s' % event.get('homeTeam', {}).get('name'))
    print('Away: %s' % event.get('awayTeam', {}).get('name'))
    print('StartTimestamp: %s' % event.get('startTimestamp'))
    print('RoundInfo: %s' % event.get('roundInfo'))
    print('Status: %s' % event.get('status'))
    
    # Test the fixed upsert_match logic
    conn = mysql.connector.connect(
        host=os.getenv('MYSQL_HOST', '127.0.0.1'),
        port=int(os.getenv('MYSQL_PORT', '3306')),
        user=os.getenv('MYSQL_USER', 'root'),
        password=os.getenv('MYSQL_PASSWORD', ''),
        database=os.getenv('MYSQL_DATABASE', 'appdb'),
        charset='utf8mb4',
    )
    cur = conn.cursor()
    
    home = event.get("homeTeam", {})
    away = event.get("awayTeam", {})
    home_id = home.get("id")
    away_id = away.get("id")
    status_raw = event.get("status", {}).get("type", "finished")
    status_map = {"finished": "finished", "inprogress": "live", "notstarted": "scheduled", "postponed": "postponed", "cancelled": "postponed"}
    status_norm = status_map.get(status_raw, "finished")

    start_ts = event.get("startTimestamp")
    match_date = None
    match_time = None
    if start_ts:
        dt = datetime.fromtimestamp(start_ts, tz=timezone.utc)
        match_date = dt.date()
        match_time = dt.time()

    print("\nInserting:")
    print("  match_id: %s" % event.get('id'))
    print("  season_id: 13415")
    print("  competition_id: 7")
    print("  home_team_id: %s" % home_id)
    print("  away_team_id: %s" % away_id)
    print("  home_score: %s" % event.get('homeScore', {}).get('current', 0))
    print("  away_score: %s" % event.get('awayScore', {}).get('current', 0))
    print("  status: %s" % status_norm)
    print("  match_date: %s" % match_date)
    print("  match_time: %s" % match_time)
    print("  round_name: %s" % event.get('roundInfo', {}).get('name'))

    try:
        cur.execute("""
            INSERT INTO matches (match_id, season_id, competition_id, home_team_id, away_team_id,
                                 home_score, away_score, status, match_date, match_time, round_name)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                home_score=VALUES(home_score), away_score=VALUES(away_score),
                status=VALUES(status), match_date=VALUES(match_date),
                match_time=VALUES(match_time), round_name=VALUES(round_name),
                updated_at=NOW()
        """, (
            event.get("id"), 13415, 7, home_id, away_id,
            event.get("homeScore", {}).get("current", 0),
            event.get("awayScore", {}).get("current", 0),
            status_norm, match_date, match_time, event.get("roundInfo", {}).get("name")
        ))
        conn.commit()
        print("\n\u2705 upsert_match SUCCESS!")
    except Exception as e:
        print("\n\u274c upsert_match FAILED: %s" % e)
        conn.rollback()
    
    cur.close()
    conn.close()

asyncio.run(test_upsert())