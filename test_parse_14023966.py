#!/usr/bin/env python3
"""Test parse match 14023966 with new schema — standalone, reads from saved JSON."""
import json, sys, os
from dotenv import load_dotenv

load_dotenv()

# Add the workdir to path so we can import backfill_runner helpers
sys.path.insert(0, os.path.dirname(__file__))

import mysql.connector
from backfill_runner import (
    ensure_team, ensure_player, _first_non_empty, _normalize_country_code,
    LEGACY_PLAYER_STAT_MAPPING, PLAYER_STAT_JSON_KEYS,
)

conn = mysql.connector.connect(
    host=os.getenv('MYSQL_HOST'), port=int(os.getenv('MYSQL_PORT', 3306)),
    user=os.getenv('MYSQL_USER'), password=os.getenv('MYSQL_PASSWORD'),
    database=os.getenv('MYSQL_DATABASE'), connect_timeout=5
)

with open('match_14023966_full.json') as f:
    data = json.load(f)

match_id = 14023966
event_data = data.get('event', {}).get('event', {})
lineups = data['lineups']
stats_data = data['statistics']
incidents_data = data['incidents']
shotmap_data = data['shotmap']
graph_data = data['graph']

print(f'Match: {event_data.get("homeTeam",{}).get("name")} vs {event_data.get("awayTeam",{}).get("name")}')
print(f'Score: {event_data.get("homeScore",{}).get("current")} - {event_data.get("awayScore",{}).get("current")}')

# ── Insert match ──
cur = conn.cursor()
cur.execute('DELETE FROM match_player_stats WHERE match_id = %s', (match_id,))
cur.execute('DELETE FROM match_shotmap WHERE match_id = %s', (match_id,))
cur.execute('DELETE FROM match_graph_points WHERE match_id = %s', (match_id,))
cur.execute('DELETE FROM match_odds WHERE match_id = %s', (match_id,))
cur.execute('DELETE FROM match_comments WHERE match_id = %s', (match_id,))
conn.commit()

# ── Match ──
from backfill_runner import DataInserter
inserter = DataInserter(conn)

# Actually let's do everything manually to control the process

# 1. Insert match
ht = event_data.get('homeTeam', {})
at = event_data.get('awayTeam', {})
home_score = event_data.get('homeScore', {}).get('current')
away_score = event_data.get('awayScore', {}).get('current')
status = event_data.get('status', {}).get('type', 'scheduled')
round_info = event_data.get('roundInfo', {}).get('round')
start_ts = event_data.get('startTimestamp')

from datetime import datetime, timezone
if start_ts:
    dt_obj = datetime.fromtimestamp(start_ts, tz=timezone.utc)
    match_date = dt_obj.strftime('%Y-%m-%d')
    match_time = dt_obj.strftime('%H:%M:%S')
else:
    match_date = match_time = None

if ht.get('id'):
    ensure_team(conn, ht['id'], ht.get('name',''), ht.get('shortName',''), None, ht)
if at.get('id'):
    ensure_team(conn, at['id'], at.get('name',''), at.get('shortName',''), None, at)

attendance = event_data.get('attendance')
referee_id = event_data.get('referee', {}).get('id')

cur.execute("""INSERT INTO matches (match_id, season_id, competition_id, round_name, match_date, match_time,
    home_team_id, away_team_id, home_score, away_score, status, attendance, referee_id)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    ON DUPLICATE KEY UPDATE home_score=VALUES(home_score), away_score=VALUES(away_score),
    status=VALUES(status), updated_at=NOW()""",
    (match_id, event_data.get('season',{}).get('id',1), event_data.get('tournament',{}).get('id',1),
     round_info, match_date, match_time, ht.get('id'), at.get('id'),
     home_score, away_score, status, attendance, referee_id))
conn.commit()
print(f'✅ Match inserted')

# 2. Insert player stats (from lineups)
for is_home, side in [(1, 'home'), (0, 'away')]:
    team_data = lineups.get(side, {})
    players = team_data.get('players', [])
    team_id = None
    if players:
        team_id = players[0].get('teamId')
    if team_id is None:
        team_id = (ht if is_home else at).get('id')

    for entry in players:
        player = entry.get('player', {})
        pid = player.get('id')
        if not pid:
            continue

        # Ensure player and team
        if team_id:
            team_payload = ht if is_home else at
            ensure_team(conn, team_id,
                team_payload.get('name',''), team_payload.get('shortName',''),
                None, team_payload)
        ensure_player(conn, pid, player.get('name',''), player.get('shortName',''),
            player.get('position'), None, player, team_id)

        stats = entry.get('statistics') or {}

        # Build values dict
        values = {'match_id': match_id, 'team_id': team_id, 'player_id': pid, 'is_home': is_home}
        for col, jk in LEGACY_PLAYER_STAT_MAPPING.items():
            values[col] = stats.get(jk)
        values['minutes_played'] = _first_non_empty(values.get('minutes_played'), entry.get('minutesPlayed'), 0)
        for col, jk in PLAYER_STAT_JSON_KEYS.items():
            values[col] = stats.get(jk)

        columns = list(values.keys())
        placeholders = ','.join(['%s'] * len(columns))
        update_cols = [c for c in columns if c not in ('match_id', 'player_id')]
        update_sql = ','.join(f'{c}=VALUES({c})' for c in update_cols)

        cur.execute(f"""INSERT INTO match_player_stats ({','.join(columns)})
            VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {update_sql}""",
            [values[c] for c in columns])

conn.commit()
cur.execute('SELECT COUNT(*) FROM match_player_stats WHERE match_id = %s', (match_id,))
print(f'✅ Player stats: {cur.fetchone()[0]} rows')

# 3. Insert shotmap
shots = shotmap_data.get('shotmap', [])
for shot in shots:
    sid = shot.get('id')
    if not sid: continue
    player = shot.get('player', {})
    goalkeeper = shot.get('goalkeeper', {})

    team_id = shot.get('teamId')
    if team_id is None:
        team_id = (ht if shot.get('isHome') else at).get('id')
    if team_id is None: continue

    if player.get('id'):
        ensure_player(conn, player['id'], player.get('name',''), player.get('shortName',''),
            player.get('position'), None, player, team_id)
    if goalkeeper.get('id'):
        ensure_player(conn, goalkeeper['id'], goalkeeper.get('name',''), goalkeeper.get('shortName',''),
            goalkeeper.get('position'), None, goalkeeper, None)

    ish = 1 if shot.get('isHome') else 0
    pc = shot.get('playerCoordinates') or {}
    gmc = shot.get('goalMouthCoordinates') or {}
    bc = shot.get('blockCoordinates') or {}

    cur.execute("""INSERT INTO match_shotmap (shot_id, match_id, team_id, player_id, is_home, minute,
        incident_type, shot_type, situation, body_part,
        player_x, player_y, player_z,
        goal_mouth_location, goal_mouth_x, goal_mouth_y, goal_mouth_z,
        xg, xgot, is_goal, block_x, block_y, block_z,
        goalkeeper_id, goalkeeper_name, added_time, time_seconds, period_time_seconds)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE xg=VALUES(xg), xgot=COALESCE(VALUES(xgot),xgot), is_goal=VALUES(is_goal)""",
        (sid, match_id, team_id, player.get('id'), ish,
         shot.get('time'), shot.get('incidentType'), shot.get('shotType'),
         shot.get('situation'), shot.get('bodyPart'),
         pc.get('x'), pc.get('y'), pc.get('z'),
         shot.get('goalMouthLocation'), gmc.get('x'), gmc.get('y'), gmc.get('z'),
         shot.get('xg'), shot.get('xgot'), 1 if shot.get('isGoal') else 0,
         bc.get('x'), bc.get('y'), bc.get('z'),
         goalkeeper.get('id'), goalkeeper.get('name'),
         shot.get('addedTime'), shot.get('timeSeconds'), shot.get('periodTimeSeconds')))

conn.commit()
cur.execute('SELECT COUNT(*) FROM match_shotmap WHERE match_id = %s', (match_id,))
print(f'✅ Shotmap: {cur.fetchone()[0]} rows')

# 4. Insert graph points
pts = graph_data.get('graphPoints', [])
period_length = graph_data.get('periodLength') or 45
for pt in pts:
    minute = pt.get('minute', 0)
    period = 1 if minute <= period_length else 2
    cur.execute("INSERT INTO match_graph_points (match_id, period, minute, value) VALUES (%s,%s,%s,%s)",
        (match_id, period, minute, pt.get('value')))
conn.commit()
cur.execute('SELECT COUNT(*) FROM match_graph_points WHERE match_id = %s', (match_id,))
print(f'✅ Graph points: {cur.fetchone()[0]} rows')

# 5. Insert odds
odds_data = data.get('odds', {})
markets = odds_data.get('markets', [])
odds_count = 0
for market in markets:
    for choice in market.get('choices', []):
        cur.execute("""INSERT INTO match_odds (match_id, market_id, market_name, market_group, market_period,
            structure_type, suspended, choice_name, initial_fractional_value, fractional_value, winning)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (match_id, market.get('marketId'), market.get('marketName'),
             market.get('marketGroup'), market.get('marketPeriod'),
             market.get('structureType'), 1 if market.get('suspended') else 0,
             choice.get('name'), choice.get('initialFractionalValue'),
             choice.get('fractionalValue'), 1 if choice.get('winning') else 0))
        odds_count += 1
conn.commit()
cur.execute('SELECT COUNT(*) FROM match_odds WHERE match_id = %s', (match_id,))
print(f'✅ Odds: {cur.fetchone()[0]} rows ({odds_count} inserted)')

# 6. Insert comments
comments_data = data.get('comments', {})
comments = comments_data.get('comments', [])
for c in comments:
    notable = c.get('notableActions')
    notable_json = json.dumps(notable) if notable else None
    cur.execute("INSERT INTO match_comments (match_id, sequence, comment_type, comment_text, notable_actions) VALUES (%s,%s,%s,%s,%s)",
        (match_id, c.get('sequence'), c.get('type'), c.get('text'), notable_json))
conn.commit()
cur.execute('SELECT COUNT(*) FROM match_comments WHERE match_id = %s', (match_id,))
print(f'✅ Comments: {cur.fetchone()[0]} rows')

# ── Verify ──
print(f'\n{"="*50}')
print('VERIFICATION')
print(f'{"="*50}')

# Show a sample player stats row with new fields
cur.execute("""SELECT player_id, is_home, minutes_played, rating,
    total_pass, accurate_pass, touches, possession_lost_ctrl,
    top_speed, kilometers_covered, number_of_sprints,
    total_progression, ball_carries_count,
    pass_value_normalized, shot_value_normalized, defensive_value_normalized,
    saves, goals_prevented, keeper_save_value
    FROM match_player_stats WHERE match_id = %s LIMIT 3""", (match_id,))
cols = [d[0] for d in cur.description]
print(f'\nSample player stats (3 rows):')
for row in cur.fetchall():
    shown = {cols[i]: row[i] for i in range(len(cols)) if row[i] is not None}
    pid = shown.pop('player_id', '?')
    name = ''
    # Get player name
    cur2 = conn.cursor()
    cur2.execute('SELECT name FROM players WHERE player_id = %s', (pid,))
    nr = cur2.fetchone()
    if nr: name = nr[0]
    print(f'  {name}: {json.dumps(shown, default=str)}')

# Show a sample shotmap row
cur.execute("""SELECT player_id, minute, shot_type, xg, xgot, goal_mouth_location,
    player_x, player_y, player_z, goalkeeper_name, block_x, block_y, block_z
    FROM match_shotmap WHERE match_id = %s AND xgot > 0 LIMIT 3""", (match_id,))
print(f'\nShots with xGOT > 0:')
for row in cur.fetchall():
    print(f'  min={row[1]} type={row[2]} xg={row[3]:.4f} xgot={row[4]:.4f} gk={row[9]} gml={row[5]}')

# Counts
for table in ['match_player_stats', 'match_shotmap', 'match_graph_points', 'match_odds', 'match_comments']:
    cur.execute(f'SELECT COUNT(*) FROM {table} WHERE match_id = %s', (match_id,))
    print(f'{table}: {cur.fetchone()[0]} rows')

conn.close()
print(f'\n✅ Test complete!')
