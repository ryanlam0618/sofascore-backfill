#!/usr/bin/env python3
"""Idempotent schema expansion for SofaScore match detail backfill."""
import os

import mysql.connector
from dotenv import load_dotenv

load_dotenv()
load_dotenv('/root/.openclaw/workspace/.env')

conn = mysql.connector.connect(
    host=os.getenv('MYSQL_HOST', '192.168.0.183'),
    port=int(os.getenv('MYSQL_PORT', 3306)),
    user=os.getenv('MYSQL_USER', 'root'),
    password=os.getenv('MYSQL_PASSWORD', ''),
    database=os.getenv('MYSQL_DATABASE', 'appdb'),
    charset='utf8mb4',
)
cur = conn.cursor()
executed = []


def add_column(table, column, definition):
    sql = f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition} DEFAULT NULL"
    try:
        cur.execute(sql)
    except mysql.connector.errors.ProgrammingError as exc:
        # Older MySQL/MariaDB variants may not support IF NOT EXISTS.
        if 'Duplicate column' in str(exc):
            return False
        fallback = f"ALTER TABLE {table} ADD COLUMN {column} {definition} DEFAULT NULL"
        try:
            cur.execute(fallback)
            sql = fallback
        except mysql.connector.errors.ProgrammingError as fallback_exc:
            if 'Duplicate column' in str(fallback_exc):
                return False
            raise
    executed.append(sql)
    return True


player_stat_columns = [
    ('total_pass', 'INT'), ('accurate_pass', 'INT'), ('total_long_balls', 'INT'), ('accurate_long_balls', 'INT'),
    ('total_own_half_passes', 'INT'), ('accurate_own_half_passes', 'INT'), ('total_opposition_half_passes', 'INT'), ('accurate_opposition_half_passes', 'INT'),
    ('total_cross', 'INT'), ('accurate_cross', 'INT'), ('key_pass', 'INT'), ('goal_assist', 'INT'), ('big_chance_created', 'INT'),
    ('total_shots', 'INT'), ('on_target_scoring_attempt', 'INT'), ('shot_off_target', 'INT'), ('blocked_scoring_attempt', 'INT'),
    ('expected_goals', 'DECIMAL(8,4)'), ('expected_goals_on_target', 'DECIMAL(8,4)'), ('expected_assists', 'DECIMAL(8,4)'),
    ('big_chance_missed', 'INT'), ('shot_value_normalized', 'DECIMAL(6,4)'),
    ('duel_lost', 'INT'), ('aerial_won', 'INT'), ('aerial_lost', 'INT'), ('total_contest', 'INT'), ('won_contest', 'INT'), ('challenge_lost', 'INT'),
    ('total_tackle', 'INT'), ('won_tackle', 'INT'), ('interception_won', 'INT'), ('total_clearance', 'INT'),
    ('ball_recovery', 'INT'), ('outfielder_block', 'INT'), ('last_man_tackle', 'INT'), ('error_lead_to_a_goal', 'INT'), ('error_lead_to_a_shot', 'INT'),
    ('dribble_value_normalized', 'DECIMAL(6,4)'), ('dispossessed', 'INT'), ('unsuccessful_touch', 'INT'),
    ('fouls', 'INT'), ('was_fouled', 'INT'), ('total_offside', 'INT'), ('own_goals', 'INT'),
    ('saves', 'INT'), ('saved_shots_from_inside_the_box', 'INT'), ('good_high_claim', 'INT'),
    ('total_keeper_sweeper', 'INT'), ('accurate_keeper_sweeper', 'INT'),
    ('goals_prevented', 'DECIMAL(6,4)'), ('keeper_save_value', 'DECIMAL(6,4)'), ('goalkeeper_value_normalized', 'DECIMAL(6,4)'),
    ('top_speed', 'DECIMAL(5,1)'), ('kilometers_covered', 'DECIMAL(5,2)'), ('number_of_sprints', 'INT'),
    ('meters_covered_running_km', 'DECIMAL(5,2)'), ('meters_covered_high_speed_running_km', 'DECIMAL(5,2)'), ('meters_covered_sprinting_km', 'DECIMAL(5,2)'),
    ('total_ball_carries_distance', 'DECIMAL(8,2)'), ('ball_carries_count', 'INT'),
    ('total_progression', 'DECIMAL(8,2)'), ('progressive_ball_carries_count', 'INT'),
    ('total_progressive_ball_carries_distance', 'DECIMAL(8,2)'), ('best_ball_carry_progression', 'DECIMAL(8,2)'),
    ('touches', 'INT'), ('possession_lost_ctrl', 'INT'),
    ('pass_value_normalized', 'DECIMAL(6,4)'), ('defensive_value_normalized', 'DECIMAL(6,4)'),
]

shotmap_columns = [
    ('player_z', 'DECIMAL(6,2)'),
    ('goal_mouth_location', 'VARCHAR(30)'),
    ('goal_mouth_x', 'DECIMAL(6,2)'), ('goal_mouth_y', 'DECIMAL(6,2)'), ('goal_mouth_z', 'DECIMAL(6,2)'),
    ('xgot', 'DECIMAL(8,4)'),
    ('block_x', 'DECIMAL(6,2)'), ('block_y', 'DECIMAL(6,2)'), ('block_z', 'DECIMAL(6,2)'),
    ('goalkeeper_id', 'BIGINT'), ('goalkeeper_name', 'VARCHAR(100)'),
    ('added_time', 'INT'), ('time_seconds', 'INT'), ('period_time_seconds', 'INT'),
]

odds_columns = [
    ('market_id', 'INT'), ('market_name', 'VARCHAR(100)'), ('market_group', 'VARCHAR(50)'), ('market_period', 'VARCHAR(30)'),
    ('structure_type', 'INT'), ('suspended', 'TINYINT(1)'), ('choice_name', 'VARCHAR(100)'),
    ('initial_fractional_value', 'VARCHAR(20)'), ('fractional_value', 'VARCHAR(20)'), ('winning', 'TINYINT(1)'),
]

print('=== ALTER TABLE match_player_stats ===')
for name, definition in player_stat_columns:
    if add_column('match_player_stats', name, definition):
        print(executed[-1] + ';')

print('\n=== ALTER TABLE match_shotmap ===')
for name, definition in shotmap_columns:
    if add_column('match_shotmap', name, definition):
        print(executed[-1] + ';')

create_graph = """
CREATE TABLE IF NOT EXISTS match_graph_points (
    graph_point_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    match_id BIGINT NOT NULL,
    period INT,
    minute INT,
    value DECIMAL(10,4),
    FOREIGN KEY (match_id) REFERENCES matches(match_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

create_odds = """
CREATE TABLE IF NOT EXISTS match_odds (
    odds_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    match_id BIGINT NOT NULL,
    market_id INT,
    market_name VARCHAR(100),
    market_group VARCHAR(50),
    market_period VARCHAR(30),
    structure_type INT,
    suspended TINYINT(1),
    choice_name VARCHAR(100),
    initial_fractional_value VARCHAR(20),
    fractional_value VARCHAR(20),
    winning TINYINT(1),
    FOREIGN KEY (match_id) REFERENCES matches(match_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

create_comments = """
CREATE TABLE IF NOT EXISTS match_comments (
    comment_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    match_id BIGINT NOT NULL,
    sequence INT,
    comment_type VARCHAR(50),
    comment_text TEXT,
    notable_actions JSON,
    FOREIGN KEY (match_id) REFERENCES matches(match_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

print('\n=== UNIQUE KEYS ===')
cur.execute("""
    DELETE mps FROM match_player_stats mps
    JOIN match_player_stats newer
      ON newer.match_id = mps.match_id
     AND newer.player_id = mps.player_id
     AND newer.player_stat_id > mps.player_stat_id
""")
print('DELETE duplicate match_player_stats rows keeping latest player_stat_id;')
try:
    cur.execute('ALTER TABLE match_player_stats ADD UNIQUE KEY uniq_player_match (match_id, player_id)')
    print('ALTER TABLE match_player_stats ADD UNIQUE KEY uniq_player_match (match_id, player_id);')
except mysql.connector.errors.ProgrammingError as exc:
    if 'Duplicate key name' not in str(exc):
        raise

print('\n=== CREATE TABLE ===')
for ddl in [create_graph, create_odds, create_comments]:
    cur.execute(ddl)
    print(ddl.strip() + ';')

print('\n=== ALTER TABLE match_odds ===')
for name, definition in odds_columns:
    if add_column('match_odds', name, definition):
        print(executed[-1] + ';')

conn.commit()
print('\n=== VERIFICATION ===')
for table in ['match_player_stats', 'match_shotmap', 'match_graph_points', 'match_odds', 'match_comments']:
    cur.execute(f'SHOW COLUMNS FROM {table}')
    print(f'{table}: {len(cur.fetchall())} columns')

conn.close()
