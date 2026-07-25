#!/usr/bin/env python3
"""Complete schema expansion (resume from coding agent partial work)."""
import mysql.connector, os
from dotenv import load_dotenv

load_dotenv()

conn = mysql.connector.connect(
    host=os.getenv('MYSQL_HOST'), port=int(os.getenv('MYSQL_PORT', 3306)),
    user=os.getenv('MYSQL_USER'), password=os.getenv('MYSQL_PASSWORD'),
    database=os.getenv('MYSQL_DATABASE'), connect_timeout=5
)
cur = conn.cursor()

# Get existing columns
def existing_cols(table):
    cur.execute(f"SHOW COLUMNS FROM {table}")
    return {c[0] for c in cur.fetchall()}

# === Task 1: Remaining match_player_stats columns ===
existing = existing_cols('match_player_stats')
print(f"match_player_stats: {len(existing)} columns exist")

remaining_stats = [
    ('expected_assists', 'DECIMAL(8,4)'),
    ('big_chance_missed', 'INT'),
    ('shot_value_normalized', 'DECIMAL(6,4)'),
    ('duel_lost', 'INT'),
    ('aerial_won', 'INT'),
    ('aerial_lost', 'INT'),
    ('total_contest', 'INT'),
    ('won_contest', 'INT'),
    ('challenge_lost', 'INT'),
    ('total_tackle', 'INT'),
    ('won_tackle', 'INT'),
    ('interception_won', 'INT'),
    ('total_clearance', 'INT'),
    ('ball_recovery', 'INT'),
    ('outfielder_block', 'INT'),
    ('last_man_tackle', 'INT'),
    ('error_lead_to_a_goal', 'INT'),
    ('error_lead_to_a_shot', 'INT'),
    ('dribble_value_normalized', 'DECIMAL(6,4)'),
    ('dispossessed', 'INT'),
    ('unsuccessful_touch', 'INT'),
    ('fouls', 'INT'),
    ('was_fouled', 'INT'),
    ('total_offside', 'INT'),
    ('own_goals', 'INT'),
    ('saves', 'INT'),
    ('saved_shots_from_inside_the_box', 'INT'),
    ('good_high_claim', 'INT'),
    ('total_keeper_sweeper', 'INT'),
    ('accurate_keeper_sweeper', 'INT'),
    ('goals_prevented', 'DECIMAL(6,4)'),
    ('keeper_save_value', 'DECIMAL(6,4)'),
    ('goalkeeper_value_normalized', 'DECIMAL(6,4)'),
    ('top_speed', 'DECIMAL(5,1)'),
    ('kilometers_covered', 'DECIMAL(5,2)'),
    ('number_of_sprints', 'INT'),
    ('meters_covered_running_km', 'DECIMAL(5,2)'),
    ('meters_covered_high_speed_running_km', 'DECIMAL(5,2)'),
    ('meters_covered_sprinting_km', 'DECIMAL(5,2)'),
    ('total_ball_carries_distance', 'DECIMAL(8,2)'),
    ('ball_carries_count', 'INT'),
    ('total_progression', 'DECIMAL(8,2)'),
    ('progressive_ball_carries_count', 'INT'),
    ('total_progressive_ball_carries_distance', 'DECIMAL(8,2)'),
    ('best_ball_carry_progression', 'DECIMAL(8,2)'),
    ('touches', 'INT'),
    ('possession_lost_ctrl', 'INT'),
    ('pass_value_normalized', 'DECIMAL(6,4)'),
    ('defensive_value_normalized', 'DECIMAL(6,4)'),
]

added = skipped = 0
for col_name, col_type in remaining_stats:
    if col_name in existing:
        skipped += 1
        continue
    try:
        cur.execute(f"ALTER TABLE match_player_stats ADD COLUMN {col_name} {col_type} DEFAULT NULL")
        added += 1
    except Exception as e:
        print(f"  ❌ {col_name}: {e}")
print(f"Task 1: player_stats added={added} skipped={skipped}")
print(f"  Total columns: {len(existing_cols('match_player_stats'))}")

# === Task 2: ALTER match_shotmap ===
existing_sm = existing_cols('match_shotmap')
print(f"\nmatch_shotmap: {len(existing_sm)} columns exist")

shotmap_cols = [
    ('player_z', 'DECIMAL(6,2)'),
    ('goal_mouth_location', 'VARCHAR(30)'),
    ('goal_mouth_x', 'DECIMAL(6,2)'),
    ('goal_mouth_y', 'DECIMAL(6,2)'),
    ('goal_mouth_z', 'DECIMAL(6,2)'),
    ('xgot', 'DECIMAL(8,4)'),
    ('block_x', 'DECIMAL(6,2)'),
    ('block_y', 'DECIMAL(6,2)'),
    ('block_z', 'DECIMAL(6,2)'),
    ('goalkeeper_id', 'BIGINT'),
    ('goalkeeper_name', 'VARCHAR(100)'),
    ('added_time', 'INT'),
    ('time_seconds', 'INT'),
    ('period_time_seconds', 'INT'),
]

added = skipped = 0
for col_name, col_type in shotmap_cols:
    if col_name in existing_sm:
        skipped += 1
        continue
    try:
        cur.execute(f"ALTER TABLE match_shotmap ADD COLUMN {col_name} {col_type} DEFAULT NULL")
        added += 1
    except Exception as e:
        print(f"  ❌ {col_name}: {e}")
print(f"Task 2: shotmap added={added} skipped={skipped}")
print(f"  Total columns: {len(existing_cols('match_shotmap'))}")

# === Task 3: Create missing tables ===
cur.execute('SHOW TABLES')
tables = {t[0] for t in cur.fetchall()}

if 'match_graph_points' not in tables:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS match_graph_points (
            graph_point_id BIGINT AUTO_INCREMENT PRIMARY KEY,
            match_id BIGINT NOT NULL,
            period INT,
            minute INT,
            value DECIMAL(10,4),
            FOREIGN KEY (match_id) REFERENCES matches(match_id)
        )
    """)
    print("\n  ✅ match_graph_points created")

if 'match_comments' not in tables:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS match_comments (
            comment_id BIGINT AUTO_INCREMENT PRIMARY KEY,
            match_id BIGINT NOT NULL,
            sequence INT,
            comment_type VARCHAR(50),
            comment_text TEXT,
            notable_actions JSON,
            FOREIGN KEY (match_id) REFERENCES matches(match_id)
        )
    """)
    print("  ✅ match_comments created")

# Check match_odds schema
cur.execute("SHOW COLUMNS FROM match_odds")
odds_cols = {c[0]: c[1] for c in cur.fetchall()}
print(f"\nmatch_odds: {len(odds_cols)} columns: {list(odds_cols.keys())}")

conn.commit()

# Final summary
print("\n=== FINAL SCHEMA SUMMARY ===")
for table in ['match_player_stats', 'match_shotmap', 'match_graph_points', 'match_odds', 'match_comments']:
    cur.execute(f"SHOW COLUMNS FROM {table}")
    cols = cur.fetchall()
    print(f"  {table}: {len(cols)} columns")

conn.close()
print("\n✅ Schema expansion complete!")
