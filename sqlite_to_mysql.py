#!/usr/bin/env python3
"""
Migrate SofaScore SQLite → MySQL appdb.
Fixed: name-based mapping, filters, proper encoding.
"""
import sqlite3, mysql.connector, os, fnmatch
from datetime import datetime

MYSQL = {
    'host': '127.0.0.1', 'port': 3306,
    'user': 'A100', 'password': 'hksfl1512',
    'database': 'appdb', 'charset': 'utf8mb4',
}

# League → (category_id, ut_id, season_id)
LEAGUE_MAP = {
    'PL':    (32, 8, 61627),
    'Premier_League': (1, 17, 61627),
    'Bundesliga':     (30, 9, 61627),
    'La_Liga':        (32, 8, 61627),
    'Serie_A':        (31, 23, 61627),
    'Ligue_1':        (7, 34, 61627),
    'UCL':            (1465, 7, 61627),
    'UEL':            (459, 459, 61627),
    'FA_Cup':         (1, 17, 61627),
    'EFL_Cup':        (1, 17, 61627),
    'DFB_Pokal':      (30, 9, 61627),
    'Copa_del_Rey':   (32, 8, 61627),
    'Coppa_Italia':   (31, 23, 61627),
    'Coupe_de_France': (7, 34, 61627),
    'AFC_Champions_League': (1467, 463, 61627),
    'Emperor_Cup':    (32, 8, 61627),
    "Emperor's_Cup":  (32, 8, 61627),
    'J.League_Cup':   (32, 8, 61627),
    'A-League_Men':   (1, 17, 61627),
    'K_League_1':      (32, 8, 61627),
    'Chinese_Super_League': (32, 8, 61627),
}

def infer_league(fname):
    base = fname.replace('.sqlite', '')
    for sep in ['_lineups','_incidents','_statistics','_shotmap','_standings','_player','_related','_managers']:
        base = base.replace(sep, '')
    for key in sorted(LEAGUE_MAP, key=len, reverse=True):
        if key in base:
            return key
    return None

def get_rows(sqlite_path, sqlite_table):
    """Read rows from SQLite using name-based mapping."""
    conn = sqlite3.connect(sqlite_path)
    conn.text_factory = str  # ensure strings, not bytes
    cur = conn.cursor()
    cur.execute(f'SELECT * FROM "{sqlite_table}"')
    rows = cur.fetchall()
    col_names = [d[0] for d in cur.description]
    conn.close()
    return col_names, rows

def migrate_lineups(conn_mysql, sqlite_path, league_key):
    """Migrate lineups. Filter: only rows where team_name or team_id is meaningful."""
    cat_id = LEAGUE_MAP.get(league_key, (None, None, None))[0]
    col_names, rows = get_rows(sqlite_path, 'lineups')
    rd_idx = {c: i for i, c in enumerate(col_names)}
    
    insert_sql = (
        "INSERT INTO sofascore_lineups "
        "(event_id,is_home,team_id,team_name,player_id,player_name,"
        "position,position_type,jersey_number,captain,player_key,fetched_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE fetched_at=VALUES(fetched_at)"
    )
    
    cur = conn_mysql.cursor()
    migrated, skipped = 0, 0
    for row in rows:
        rd = dict(zip(col_names, row))
        # Filter: skip rows where both team_name AND team_id are empty
        team_name = rd.get('team_name', '')
        team_id = rd.get('team_id')
        if not team_name and team_id is None:
            skipped += 1
            continue
        
        vals = (
            rd.get('event_id'),
            rd.get('is_home'),
            team_id,
            team_name or None,
            rd.get('player_id'),
            rd.get('player_name'),
            rd.get('position'),
            rd.get('position_type'),
            rd.get('jersey_number'),
            rd.get('captain'),
            str(rd.get('player_id', '')) if rd.get('player_id') else None,
            rd.get('fetched_at') or datetime.now().isoformat(),
        )
        try:
            cur.execute(insert_sql, vals)
            migrated += 1
        except Exception as e:
            if migrated < 3:
                print(f"    ⚠ {str(e)[:80]}")
    
    conn_mysql.commit()
    cur.close()
    return migrated, skipped

def migrate_lineup_events(conn_mysql, sqlite_path, league_key):
    """Migrate lineup_events."""
    cat_id = LEAGUE_MAP.get(league_key, (None, None, None))[0]
    col_names, rows = get_rows(sqlite_path, 'lineup_events')
    
    insert_sql = (
        "INSERT INTO sofascore_lineup_events "
        "(event_id,status_code,home_count,away_count,confirmed,fetched_at,error) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE fetched_at=VALUES(fetched_at)"
    )
    
    cur = conn_mysql.cursor()
    migrated = 0
    for row in rows:
        rd = dict(zip(col_names, row))
        vals = (
            rd.get('event_id'),
            rd.get('status_code'),
            rd.get('home_count'),
            rd.get('away_count'),
            rd.get('confirmed'),
            rd.get('fetched_at') or datetime.now().isoformat(),
            rd.get('error'),
        )
        try:
            cur.execute(insert_sql, vals)
            migrated += 1
        except:
            pass
    
    conn_mysql.commit()
    cur.close()
    return migrated

def migrate_incidents(conn_mysql, sqlite_path, league_key):
    """Migrate incidents. Filter: only real incidents (goals, cards, substitutions, penalties, var, own goals)."""
    cat_id = LEAGUE_MAP.get(league_key, (None, None, None))[0]
    col_names, rows = get_rows(sqlite_path, 'incidents')
    
    # Real incident types to keep (skip period markers, injuryTime, var_waiting)
    REAL_TYPES = {
        'goal', 'card', 'substitution', 'penalty', 'ownGoal',
        'varDecision', 'penaltyMissed', 'injury', 'penaltySaved',
        'substitution-in', 'substitution-out',
    }
    
    insert_sql = (
        "INSERT INTO sofascore_incidents "
        "(event_id,incident_id,match_date,league,home_team,away_team,"
        "incident_type,minute,added_time,time_seconds,period_time_seconds,"
        "is_home_incident,team_id,team_name,player_id,player_name,"
        "related_player_id,related_player_name,assist_player_id,assist_player_name,"
        "reason,text,coordinates_x,coordinates_y,in_stats,fetched_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE fetched_at=VALUES(fetched_at)"
    )
    
    cur = conn_mysql.cursor()
    migrated, skipped = 0, 0
    for row in rows:
        rd = dict(zip(col_names, row))
        inc_type = rd.get('incident_type', '')
        
        # Filter: skip period/injuryTime/etc without real data
        if inc_type not in REAL_TYPES:
            skipped += 1
            continue
        
        vals = (
            rd.get('event_id'),
            rd.get('incident_id'),
            rd.get('match_date') or None,
            rd.get('league') or None,
            rd.get('home_team') or None,
            rd.get('away_team') or None,
            inc_type,
            rd.get('minute'),
            rd.get('added_time'),
            rd.get('time_seconds'),
            rd.get('period_time_seconds'),
            rd.get('is_home_incident'),
            rd.get('team_id'),
            rd.get('team_name'),
            rd.get('player_id'),
            rd.get('player_name'),
            rd.get('related_player_id'),
            rd.get('related_player_name'),
            rd.get('assist_player_id'),
            rd.get('assist_player_name'),
            rd.get('reason'),
            rd.get('text'),
            rd.get('coordinates_x'),
            rd.get('coordinates_y'),
            rd.get('in_stats'),
            rd.get('fetched_at') or datetime.now().isoformat(),
        )
        try:
            cur.execute(insert_sql, vals)
            migrated += 1
        except Exception as e:
            if migrated < 3:
                print(f"    ⚠ {str(e)[:80]}")
    
    conn_mysql.commit()
    cur.close()
    return migrated, skipped

def migrate_incident_events(conn_mysql, sqlite_path, league_key):
    """Migrate incident_events."""
    col_names, rows = get_rows(sqlite_path, 'incident_events')
    
    insert_sql = (
        "INSERT INTO sofascore_incident_events "
        "(event_id,match_date,league,home_team,away_team,status_code,incident_count,fetched_at,error) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE fetched_at=VALUES(fetched_at)"
    )
    
    cur = conn_mysql.cursor()
    migrated = 0
    for row in rows:
        rd = dict(zip(col_names, row))
        vals = (
            rd.get('event_id'),
            rd.get('match_date') or None,
            rd.get('league') or None,
            rd.get('home_team') or None,
            rd.get('away_team') or None,
            rd.get('status_code'),
            rd.get('incident_count'),
            rd.get('fetched_at') or datetime.now().isoformat(),
            rd.get('error'),
        )
        try:
            cur.execute(insert_sql, vals)
            migrated += 1
        except:
            pass
    
    conn_mysql.commit()
    cur.close()
    return migrated

def migrate_statistics(conn_mysql, sqlite_path, league_key):
    """Migrate statistics with category_id/ut_id/season_id from league map."""
    cat_id, ut_id, season_id = LEAGUE_MAP.get(league_key, (None, None, None))
    col_names, rows = get_rows(sqlite_path, 'statistics')
    
    insert_sql = (
        "INSERT INTO sofascore_statistics "
        "(category_id,ut_id,season_id,event_id,period,group_name,stat_name,"
        "home_value,away_value,home_numeric,away_numeric,render_type,statistics_type,fetched_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE fetched_at=VALUES(fetched_at)"
    )
    
    cur = conn_mysql.cursor()
    migrated = 0
    for row in rows:
        rd = dict(zip(col_names, row))
        vals = (
            cat_id, ut_id, season_id,
            rd.get('event_id'),
            rd.get('period'),
            rd.get('group_name'),
            rd.get('stat_name'),
            rd.get('home_value'),
            rd.get('away_value'),
            rd.get('home_numeric'),
            rd.get('away_numeric'),
            rd.get('render_type'),
            rd.get('statistics_type') or 'overall',
            rd.get('fetched_at') or datetime.now().isoformat(),
        )
        try:
            cur.execute(insert_sql, vals)
            migrated += 1
        except:
            pass
    
    conn_mysql.commit()
    cur.close()
    return migrated

def migrate_statistics_events(conn_mysql, sqlite_path, league_key):
    """Migrate statistics_events."""
    col_names, rows = get_rows(sqlite_path, 'statistics_events')
    
    insert_sql = (
        "INSERT INTO sofascore_statistics_events "
        "(event_id,status_code,period_count,group_count,stat_count,fetched_at,error) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE fetched_at=VALUES(fetched_at)"
    )
    
    cur = conn_mysql.cursor()
    migrated = 0
    for row in rows:
        rd = dict(zip(col_names, row))
        vals = (
            rd.get('event_id'),
            rd.get('status_code'),
            rd.get('period_count'),
            rd.get('group_count'),
            rd.get('stat_count'),
            rd.get('fetched_at') or datetime.now().isoformat(),
            rd.get('error'),
        )
        try:
            cur.execute(insert_sql, vals)
            migrated += 1
        except:
            pass
    
    conn_mysql.commit()
    cur.close()
    return migrated

def migrate_shotmap_xg(conn_mysql, sqlite_path, league_key):
    """Migrate shotmap_xg."""
    cat_id = LEAGUE_MAP.get(league_key, (None, None, None))[0]
    col_names, rows = get_rows(sqlite_path, 'shotmap_xg_backfill')
    
    insert_sql = (
        "INSERT INTO sofascore_shotmap_xg_backfill "
        "(event_id,match_date,league,home_team,away_team,status_code,"
        "has_shotmap,has_xg,shot_count,home_shotmap_xg,away_shotmap_xg,fetched_at,error) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE fetched_at=VALUES(fetched_at)"
    )
    
    cur = conn_mysql.cursor()
    migrated = 0
    for row in rows:
        rd = dict(zip(col_names, row))
        vals = (
            rd.get('event_id'),
            rd.get('match_date') or None,
            rd.get('league') or None,
            rd.get('home_team') or None,
            rd.get('away_team') or None,
            rd.get('status_code'),
            rd.get('has_shotmap'),
            rd.get('has_xg'),
            rd.get('shot_count'),
            rd.get('home_shotmap_xg'),
            rd.get('away_shotmap_xg'),
            rd.get('fetched_at') or datetime.now().isoformat(),
            rd.get('error'),
        )
        try:
            cur.execute(insert_sql, vals)
            migrated += 1
        except:
            pass
    
    conn_mysql.commit()
    cur.close()
    return migrated

def migrate_shotmap_details(conn_mysql, sqlite_path, league_key):
    """Migrate shotmap_details."""
    col_names, rows = get_rows(sqlite_path, 'shotmap_details')
    
    insert_sql = (
        "INSERT INTO sofascore_shotmap_details "
        "(event_id,shot_id,is_home_shot,team_id,team_name,player_id,player_name,"
        "player_position,minute,added_time,time_seconds,incident_type,"
        "shot_type,situation,body_part,goal_mouth_location,"
        "player_x,player_y,xg,home_team_goal_prob,away_team_goal_prob,fetched_at,error) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE fetched_at=VALUES(fetched_at)"
    )
    
    cur = conn_mysql.cursor()
    migrated = 0
    for row in rows:
        rd = dict(zip(col_names, row))
        vals = (
            rd.get('event_id'),
            rd.get('shot_id'),
            rd.get('is_home_shot'),
            rd.get('team_id'),
            rd.get('team_name'),
            rd.get('player_id'),
            rd.get('player_name'),
            rd.get('player_position'),
            rd.get('minute'),
            rd.get('added_time'),
            rd.get('time_seconds'),
            rd.get('incident_type'),
            rd.get('shot_type'),
            rd.get('situation'),
            rd.get('body_part'),
            rd.get('goal_mouth_location'),
            rd.get('player_x'),
            rd.get('player_y'),
            rd.get('xg'),
            rd.get('home_team_goal_prob'),
            rd.get('away_team_goal_prob'),
            rd.get('fetched_at') or datetime.now().isoformat(),
            rd.get('error'),
        )
        try:
            cur.execute(insert_sql, vals)
            migrated += 1
        except:
            pass
    
    conn_mysql.commit()
    cur.close()
    return migrated

def migrate_player_stats(conn_mysql, sqlite_path, league_key):
    """Migrate player_season_stats."""
    cat_id, ut_id, season_id = LEAGUE_MAP.get(league_key, (1, 17, 61627))
    col_names, rows = get_rows(sqlite_path, 'player_season_stats')
    
    insert_sql = (
        "INSERT INTO sofascore_player_season_stats "
        "(category_id,ut_id,season_id,stat_type,`rank`,player_id,player_name,"
        "player_position,team_id,team_name,goals,assists,appearances,"
        "minutes_played,xg,xa,yellow_cards,red_cards,fetched_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE fetched_at=VALUES(fetched_at)"
    )
    
    cur = conn_mysql.cursor()
    migrated = 0
    for row in rows:
        rd = dict(zip(col_names, row))
        vals = (
            rd.get('category_id') or cat_id,
            rd.get('ut_id') or ut_id,
            rd.get('season_id') or season_id,
            rd.get('stat_type') or 'overall',
            rd.get('rank'),
            rd.get('player_id'),
            rd.get('player_name'),
            rd.get('player_position'),
            rd.get('team_id'),
            rd.get('team_name'),
            rd.get('goals', 0),
            rd.get('assists', 0),
            rd.get('appearances', 0),
            rd.get('minutes_played', 0),
            rd.get('xg', 0),
            rd.get('xa', 0),
            rd.get('yellow_cards', 0),
            rd.get('red_cards', 0),
            rd.get('fetched_at') or datetime.now().isoformat(),
        )
        try:
            cur.execute(insert_sql, vals)
            migrated += 1
        except Exception as e:
            if migrated < 3:
                print(f"    ⚠ {str(e)[:80]}")
    
    conn_mysql.commit()
    cur.close()
    return migrated

def migrate_related(conn_mysql, sqlite_path, league_key):
    """Migrate related_matches."""
    col_names, rows = get_rows(sqlite_path, 'related_matches')
    
    insert_sql = (
        "INSERT INTO sofascore_related_matches "
        "(source_event_id,home_team_id,home_team_name,away_team_id,away_team_name,"
        "league_category_id,league_name,match_timestamp,home_wins,draws,away_wins,total_h2h,fetched_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE fetched_at=VALUES(fetched_at)"
    )
    
    cur = conn_mysql.cursor()
    migrated = 0
    for row in rows:
        rd = dict(zip(col_names, row))
        vals = (
            rd.get('source_event_id'),
            rd.get('home_team_id'),
            rd.get('home_team_name'),
            rd.get('away_team_id'),
            rd.get('away_team_name'),
            rd.get('league_category_id'),
            rd.get('league_name'),
            rd.get('match_timestamp'),
            rd.get('home_wins'),
            rd.get('draws'),
            rd.get('away_wins'),
            rd.get('total_h2h'),
            rd.get('fetched_at') or datetime.now().isoformat(),
        )
        try:
            cur.execute(insert_sql, vals)
            migrated += 1
        except:
            pass
    
    conn_mysql.commit()
    cur.close()
    return migrated

def migrate_standings(conn_mysql, sqlite_path, league_key):
    """Migrate standings."""
    cat_id = LEAGUE_MAP.get(league_key, (None, None, None))[0]
    col_names, rows = get_rows(sqlite_path, 'standings')
    
    insert_sql = (
        "INSERT INTO sofascore_standings "
        "(category_id,tournament_name,season_id,standing_type,position,team_id,team_name,team_short_name,"
        "played,wins,draws,losses,goals_for,goals_against,goal_diff,points,last_5,streak,fetched_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE fetched_at=VALUES(fetched_at)"
    )
    
    cur = conn_mysql.cursor()
    migrated = 0
    for row in rows:
        rd = dict(zip(col_names, row))
        vals = (
            cat_id or rd.get('category_id'),
            rd.get('tournament_name'),
            rd.get('season_id'),
            rd.get('standing_type'),
            rd.get('position'),
            rd.get('team_id'),
            rd.get('team_name'),
            rd.get('team_short_name'),
            rd.get('played'),
            rd.get('wins'),
            rd.get('draws'),
            rd.get('losses'),
            rd.get('goals_for'),
            rd.get('goals_against'),
            rd.get('goal_diff'),
            rd.get('points'),
            rd.get('last_5'),
            rd.get('streak'),
            rd.get('fetched_at') or datetime.now().isoformat(),
        )
        try:
            cur.execute(insert_sql, vals)
            migrated += 1
        except:
            pass
    
    conn_mysql.commit()
    cur.close()
    return migrated

def main():
    base = '/root/.openclaw/workspace/sofascore_backfill/data/backfill_sofascore_10y'
    
    print("🔗 Connecting to MySQL...")
    conn = mysql.connector.connect(**MYSQL)
    
    # Track processed files
    processed = set()
    total_migrated = 0
    
    # ── LINEUPS ──────────────────────────────────────────────
    print("\n=== LINEUPS ===")
    matches = sorted([f for f in os.listdir(base)
                      if f.startswith('lineups_') and f.endswith('.sqlite') and '_state' not in f])
    for fname in matches:
        if fname in processed:
            continue
        processed.add(fname)
        league = infer_league(fname)
        path = os.path.join(base, fname)
        cat = LEAGUE_MAP.get(league, ('?',))[0]
        
        conn_sql = sqlite3.connect(path)
        cur = conn_sql.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall() if r[0] != 'sqlite_sequence']
        if not tables:
            conn_sql.close()
            print(f"  ⏭ empty file, skip")
            continue
        main_tbl = max(tables, key=lambda t: cur.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0])
        total_rows = cur.execute(f'SELECT COUNT(*) FROM "{main_tbl}"').fetchone()[0]
        conn_sql.close()
        
        print(f"▶ {fname:<45} → lineups (cat={cat}) [{total_rows:,} rows]")
        m, s = migrate_lineups(conn, path, league)
        me = migrate_lineup_events(conn, path, league)
        print(f"  ✅ {m:,} lineups (skipped {s:,} no-team), {me:,} lineup_events")
        total_migrated += m + me
    
    # ── INCIDENTS ────────────────────────────────────────────
    print("\n=== INCIDENTS ===")
    matches = sorted([f for f in os.listdir(base)
                      if f.startswith('incidents_') and f.endswith('.sqlite') and '_state' not in f])
    for fname in matches:
        if fname in processed:
            continue
        processed.add(fname)
        league = infer_league(fname)
        path = os.path.join(base, fname)
        cat = LEAGUE_MAP.get(league, ('?',))[0]
        
        conn_sql = sqlite3.connect(path)
        cur = conn_sql.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall() if r[0] != 'sqlite_sequence']
        if not tables:
            conn_sql.close()
            print(f"  ⏭ empty file, skip")
            continue
        main_tbl = max(tables, key=lambda t: cur.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0])
        total_rows = cur.execute(f'SELECT COUNT(*) FROM "{main_tbl}"').fetchone()[0]
        conn_sql.close()
        
        print(f"▶ {fname:<45} → incidents (cat={cat}) [{total_rows:,} rows]")
        m, s = migrate_incidents(conn, path, league)
        me = migrate_incident_events(conn, path, league)
        print(f"  ✅ {m:,} incidents (skipped {s:,} noise), {me:,} incident_events")
        total_migrated += m + me
    
    # ── STATISTICS ───────────────────────────────────────────
    print("\n=== STATISTICS ===")
    matches = sorted([f for f in os.listdir(base)
                      if f.startswith('statistics_') and f.endswith('.sqlite') and '_state' not in f])
    for fname in matches:
        if fname in processed:
            continue
        processed.add(fname)
        league = infer_league(fname)
        path = os.path.join(base, fname)
        cat = LEAGUE_MAP.get(league, ('?',))[0]
        
        conn_sql = sqlite3.connect(path)
        cur = conn_sql.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall() if r[0] != 'sqlite_sequence']
        if not tables:
            conn_sql.close()
            print(f"  ⏭ empty file, skip")
            continue
        main_tbl = max(tables, key=lambda t: cur.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0])
        total_rows = cur.execute(f'SELECT COUNT(*) FROM "{main_tbl}"').fetchone()[0]
        conn_sql.close()
        
        print(f"▶ {fname:<45} → statistics (cat={cat}) [{total_rows:,} rows]")
        m = migrate_statistics(conn, path, league)
        me = migrate_statistics_events(conn, path, league)
        print(f"  ✅ {m:,} stats, {me:,} stat_events")
        total_migrated += m + me
    
    # ── SHOTMAP XG ───────────────────────────────────────────
    print("\n=== SHOTMAP XG ===")
    matches = sorted([f for f in os.listdir(base)
                      if f.startswith('shotmap_xg') and f.endswith('.sqlite') and '_state' not in f])
    for fname in matches:
        if fname in processed:
            continue
        processed.add(fname)
        league = infer_league(fname)
        path = os.path.join(base, fname)
        cat = LEAGUE_MAP.get(league, ('?',))[0]
        
        conn_sql = sqlite3.connect(path)
        cur = conn_sql.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall() if r[0] != 'sqlite_sequence']
        if not tables:
            conn_sql.close()
            print(f"  ⏭ empty file, skip")
            continue
        main_tbl = max(tables, key=lambda t: cur.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0])
        total_rows = cur.execute(f'SELECT COUNT(*) FROM "{main_tbl}"').fetchone()[0]
        conn_sql.close()
        
        print(f"▶ {fname:<45} → shotmap_xg (cat={cat}) [{total_rows:,} rows]")
        m = migrate_shotmap_xg(conn, path, league)
        print(f"  ✅ {m:,} rows")
        total_migrated += m
    
    # ── SHOTMAP DETAILS ─────────────────────────────────────
    print("\n=== SHOTMAP DETAILS ===")
    for fname in ['shotmap_details.sqlite']:
        path = os.path.join(base, fname)
        if not os.path.exists(path):
            continue
        conn_sql = sqlite3.connect(path)
        cur = conn_sql.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall() if r[0] != 'sqlite_sequence']
        if not tables:
            conn_sql.close()
            print(f"  ⏭ empty file, skip")
            continue
        main_tbl = max(tables, key=lambda t: cur.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0])
        total_rows = cur.execute(f'SELECT COUNT(*) FROM "{main_tbl}"').fetchone()[0]
        conn_sql.close()
        
        print(f"▶ {fname:<45} → shotmap_details [{total_rows:,} rows]")
        m = migrate_shotmap_details(conn, path, None)
        print(f"  ✅ {m:,} rows")
        total_migrated += m
    
    # ── PLAYER STATS ────────────────────────────────────────
    print("\n=== PLAYER STATS ===")
    for fname in ['player_stats.sqlite']:
        path = os.path.join(base, fname)
        if not os.path.exists(path):
            continue
        conn_sql = sqlite3.connect(path)
        cur = conn_sql.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall() if r[0] != 'sqlite_sequence']
        if not tables:
            conn_sql.close()
            print(f"  ⏭ empty file, skip")
            continue
        main_tbl = max(tables, key=lambda t: cur.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0])
        total_rows = cur.execute(f'SELECT COUNT(*) FROM "{main_tbl}"').fetchone()[0]
        conn_sql.close()
        
        print(f"▶ {fname:<45} → player_season_stats [{total_rows:,} rows]")
        m = migrate_player_stats(conn, path, None)
        print(f"  ✅ {m:,} rows")
        total_migrated += m
    
    # ── RELATED MATCHES ─────────────────────────────────────
    print("\n=== RELATED MATCHES ===")
    for fname in ['related_matches.sqlite']:
        path = os.path.join(base, fname)
        if not os.path.exists(path):
            continue
        conn_sql = sqlite3.connect(path)
        cur = conn_sql.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall() if r[0] != 'sqlite_sequence']
        if not tables:
            conn_sql.close()
            print(f"  ⏭ empty file, skip")
            continue
        main_tbl = max(tables, key=lambda t: cur.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0])
        total_rows = cur.execute(f'SELECT COUNT(*) FROM "{main_tbl}"').fetchone()[0]
        conn_sql.close()
        
        print(f"▶ {fname:<45} → related_matches [{total_rows:,} rows]")
        m = migrate_related(conn, path, None)
        print(f"  ✅ {m:,} rows")
        total_migrated += m
    
    # ── STANDINGS ───────────────────────────────────────────
    print("\n=== STANDINGS ===")
    for fname in ['standings.sqlite']:
        path = os.path.join(base, fname)
        if not os.path.exists(path):
            continue
        conn_sql = sqlite3.connect(path)
        cur = conn_sql.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall() if r[0] != 'sqlite_sequence']
        if not tables:
            conn_sql.close()
            print(f"  ⏭ empty file, skip")
            continue
        main_tbl = max(tables, key=lambda t: cur.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0])
        total_rows = cur.execute(f'SELECT COUNT(*) FROM "{main_tbl}"').fetchone()[0]
        conn_sql.close()
        
        print(f"▶ {fname:<45} → standings [{total_rows:,} rows]")
        m = migrate_standings(conn, path, None)
        print(f"  ✅ {m:,} rows")
        total_migrated += m
    
    conn.close()
    print(f"\n🎉 Done: {total_migrated:,} rows migrated")

if __name__ == '__main__':
    main()
