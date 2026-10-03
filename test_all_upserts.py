#!/usr/bin/env python3
"""Test all upsert functions with real data."""

import asyncio
import mysql.connector
import os
from dotenv import load_dotenv
load_dotenv('.env')

from curl_cffi import requests as cffi_requests
from team_attribution import resolve_incident_team_id, resolve_side_team_id

async def test_all():
    proxy = {'ip': '179.198.16.221', 'port': '6840', 'user': '***REMOVED***', 'pw': '***REMOVED***'}
    proxy_url = 'http://%s:%s@%s:%s' % (proxy["user"], proxy["pw"], proxy["ip"], proxy["port"])
    
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
    
    # Now test inserts
    conn = mysql.connector.connect(
        host=os.getenv('MYSQL_HOST', '127.0.0.1'),
        port=int(os.getenv('MYSQL_PORT', '3306')),
        user=os.getenv('MYSQL_USER', 'root'),
        password=os.getenv('MYSQL_PASSWORD', ''),
        database=os.getenv('MYSQL_DATABASE', 'appdb'),
        charset='utf8mb4',
    )
    cur = conn.cursor()
    
    event_data = bundle.get("event", {}).get("event", {})
    if not event_data:
        event_data = bundle.get("event", {})
    print("Event data keys: %s" % list(event_data.keys()) if event_data else "None")
    
    # Test upsert_match
    home = event_data.get("homeTeam", {})
    away = event_data.get("awayTeam", {})
    home_id = home.get("id")
    away_id = away.get("id")
    status_raw = event_data.get("status", {}).get("type", "finished")
    status_map = {"finished": "finished", "inprogress": "live", "notstarted": "scheduled", "postponed": "postponed", "cancelled": "postponed"}
    status_norm = status_map.get(status_raw, "finished")
    start_ts = event_data.get("startTimestamp")
    from datetime import datetime, timezone
    match_date = None
    match_time = None
    if start_ts:
        dt = datetime.fromtimestamp(start_ts, tz=timezone.utc)
        match_date = dt.date()
        match_time = dt.time()
    
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
            event_data.get("id"), 13415, 7, home_id, away_id,
            event_data.get("homeScore", {}).get("current", 0),
            event_data.get("awayScore", {}).get("current", 0),
            status_norm, match_date, match_time, event_data.get("roundInfo", {}).get("name")
        ))
        conn.commit()
        print("\u2705 matches")
    except Exception as e:
        print("\u274c matches: %s" % e)
        conn.rollback()
    
    # Test upsert_incidents
    if bundle.get("incidents", {}).get("incidents"):
        try:
            n = 0
            rejected_foreign_team = 0
            for inc in bundle["incidents"]["incidents"]:
                # team_id attribution fix (2026-10-03, Kris-approved): derive
                # from the EVENT payload + isHome; skip + count foreign team
                # ids (rootcause_teamid_20261002.md, team_attribution.py).
                team_id, reject_reason = resolve_incident_team_id(inc, event_data)
                if reject_reason == "foreign_team_id":
                    rejected_foreign_team += 1
                if team_id is None:
                    continue
                cur.execute("""
                    INSERT INTO match_incidents (match_id, incident_id, time, type, class, added_time,
                                                 home_score, away_score, player_id, player_name, team_id,
                                                 is_home, text, in_game_minute)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        time=VALUES(time), type=VALUES(type), class=VALUES(class),
                        added_time=VALUES(added_time), home_score=VALUES(home_score),
                        away_score=VALUES(away_score), player_name=VALUES(player_name),
                        team_id=VALUES(team_id), is_home=VALUES(is_home),
                        text=VALUES(text), in_game_minute=VALUES(in_game_minute)
                """, (
                    event_data.get("id"), inc.get("id"), inc.get("time"), inc.get("incidentType"),
                    inc.get("incidentClass"), inc.get("addedTime"),
                    inc.get("homeScore"), inc.get("awayScore"),
                    inc.get("player", {}).get("id"),
                    inc.get("player", {}).get("name"),
                    team_id,
                    1 if inc.get("isHome") else 0,
                    inc.get("text"), inc.get("inGameMinute")
                ))
                n += 1
            conn.commit()
            print("\u2705 incidents: %s rows" % n)
            if rejected_foreign_team:
                print("    [team_attribution] rejected %d foreign-team incident rows" % rejected_foreign_team)
        except Exception as e:
            print("\u274c incidents: %s" % e)
            conn.rollback()
    else:
        print("No incidents data")
    
    # Test upsert_lineups
    if bundle.get("lineups", {}).get("home") or bundle.get("lineups", {}).get("away"):
        try:
            n = 0
            data = bundle["lineups"]
            data.setdefault("homeTeam", event_data.get("homeTeam", {}))
            data.setdefault("awayTeam", event_data.get("awayTeam", {}))
            for is_home, key in ((1, "home"), (0, "away")):
                side = data.get(key, {}) or {}
                team = side.get("team") or {}
                players = side.get("players", []) or []
                # team_id attribution fix (2026-10-03, Kris-approved): EVENT
                # payload side id only — never the garbage player-level
                # `teamId` (rootcause_teamid_20261002.md, team_attribution.py).
                team_id = resolve_side_team_id(event_data, is_home)
                fallback_team = data["homeTeam"] if is_home else data["awayTeam"]
                for p in players:
                    pl = p.get("player", {})
                    if not pl.get("id") or team_id is None:
                        continue
                    stat = p.get("statistics") or {}
                    pos_map = {"G": "GK", "D": "DEF", "M": "MID", "F": "FWD"}
                    pc = pos_map.get((pl.get("position") or "").upper()[:1], "MID")
                    cur.execute("""
                        INSERT IGNORE INTO teams (team_id, name, short_name) VALUES (%s, %s, %s)
                    """, (team_id, (team or fallback_team).get("name", ""), (team or fallback_team).get("shortName", "")))
                    cur.execute("""
                        INSERT IGNORE INTO players (player_id, name, short_name, position, team_id)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (pl["id"], pl.get("name", ""), pl.get("shortName", ""), pl.get("position", ""), team_id))
                    cur.execute("""
                        INSERT INTO match_lineups
                        (match_id, team_id, player_id, is_home, is_starter,
                         jersey_number, position, position_category, is_captain,
                         minutes_played, rating)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON DUPLICATE KEY UPDATE
                            is_starter=VALUES(is_starter), jersey_number=VALUES(jersey_number),
                            position=VALUES(position), position_category=VALUES(position_category),
                            is_captain=VALUES(is_captain), minutes_played=VALUES(minutes_played),
                            rating=VALUES(rating)
                    """, (
                        event_data.get("id"), team_id, pl["id"], is_home,
                        1 if p.get("substitute") is False else 0,
                        p.get("jerseyNumber") if p.get("jerseyNumber") not in ("", None) else None,
                        pl.get("position") or p.get("position") or "",
                        pc, 1 if p.get("captain") else 0,
                        stat.get("minutesPlayed"), stat.get("rating")
                    ))
                    n += 1
            conn.commit()
            print("\u2705 lineups: %s rows" % n)
        except Exception as e:
            print("\u274c lineups: %s" % e)
            conn.rollback()
    else:
        print("No lineups data")
    
    # Test upsert_statistics
    if bundle.get("statistics", {}).get("statistics"):
        try:
            n = 0
            stats = bundle["statistics"]["statistics"]
            for group in stats:
                group_name = group.get("groupName", "")
                items = group.get("statisticsItems", [])
                for item in items:
                    cur.execute("""
                        INSERT INTO match_statistics (match_id, team_id, group_name, name, home_value, away_value)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            home_value=VALUES(home_value), away_value=VALUES(away_value)
                    """, (
                        event_data.get("id"),
                        item.get("teamId"),
                        group_name,
                        item.get("name"),
                        item.get("home"),
                        item.get("away")
                    ))
                    n += 1
            conn.commit()
            print("\u2705 statistics: %s rows" % n)
        except Exception as e:
            print("\u274c statistics: %s" % e)
            conn.rollback()
    else:
        print("No statistics data")
    
    # Test upsert_shotmap
    if bundle.get("shotmap", {}).get("shotmap"):
        try:
            n = 0
            for shot in bundle["shotmap"]["shotmap"]:
                cur.execute("""
                    INSERT INTO match_shotmap (match_id, shot_id, player_id, team_id, is_home,
                                               x, y, xg, situation, shot_type, body_part,
                                               time, added_time)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        x=VALUES(x), y=VALUES(y), xg=VALUES(xg), situation=VALUES(situation),
                        shot_type=VALUES(shot_type), body_part=VALUES(body_part),
                        time=VALUES(time), added_time=VALUES(added_time)
                """, (
                    event_data.get("id"), shot.get("id"), shot.get("player", {}).get("id"),
                    shot.get("team", {}).get("id"), 1 if shot.get("isHome") else 0,
                    shot.get("x"), shot.get("y"), shot.get("xg"),
                    shot.get("situation"), shot.get("shotType"), shot.get("bodyPart"),
                    shot.get("time"), shot.get("addedTime")
                ))
                n += 1
            conn.commit()
            print("\u2705 shotmap: %s rows" % n)
        except Exception as e:
            print("\u274c shotmap: %s" % e)
            conn.rollback()
    else:
        print("No shotmap data")
    
    # Test upsert_odds
    if bundle.get("odds", {}).get("markets"):
        try:
            n = 0
            markets = bundle["odds"]["markets"]
            for market in markets:
                market_id = market.get("marketId")
                market_name = market.get("marketName")
                market_group = market.get("marketGroup")
                market_period = market.get("marketPeriod")
                structure_type = market.get("structureType")
                suspended = 1 if market.get("suspended") else 0
                choices = market.get("choices", [])
                for choice in choices:
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
                        event_data.get("id"),
                        market_id, market_name, market_group, market_period,
                        structure_type, suspended, choice.get("name"),
                        choice.get("initialFractionalValue"), choice.get("fractionalValue"),
                        1 if choice.get("winning") else 0,
                        None, None, None,
                        None, None, None,
                        None
                    ))
                    n += 1
            conn.commit()
            print("\u2705 odds: %s rows" % n)
        except Exception as e:
            print("\u274c odds: %s" % e)
            conn.rollback()
    else:
        print("No odds data")
    
    cur.close()
    conn.close()

asyncio.run(test_all())