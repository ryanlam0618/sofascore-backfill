-- ============================================================
-- SofaScore Backfill — MySQL Schema for `appdb`
-- Run:  mysql -u A100 -p appdb < mysql_schema.sql
-- Or call ensure_mysql_tables() from mysql_helpers.py
-- ============================================================

-- ── Standings ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sofascore_standings_fetch_log (
  id INT AUTO_INCREMENT PRIMARY KEY,
  category_id INT,
  season_id INT,
  standing_type VARCHAR(20),
  tournament_name VARCHAR(255),
  fetched_at DATETIME,
  status_code INT,
  row_count INT,
  error TEXT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS sofascore_standings (
  category_id INT,
  tournament_name VARCHAR(255),
  season_id INT,
  standing_type VARCHAR(20),
  position INT,
  team_id INT,
  team_name VARCHAR(255),
  team_short_name VARCHAR(50),
  played INT DEFAULT 0,
  wins INT DEFAULT 0,
  draws INT DEFAULT 0,
  losses INT DEFAULT 0,
  goals_for INT DEFAULT 0,
  goals_against INT DEFAULT 0,
  goal_diff INT DEFAULT 0,
  points INT DEFAULT 0,
  last_5 VARCHAR(500) DEFAULT '',
  streak VARCHAR(50) DEFAULT '',
  fetched_at DATETIME,
  UNIQUE KEY uk_standings (category_id, season_id, standing_type, position, team_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ── Player Stats ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sofascore_player_stats_fetch_log (
  id INT AUTO_INCREMENT PRIMARY KEY,
  category_id INT,
  ut_id INT,
  season_id INT,
  stat_type VARCHAR(30),
  fetched_at DATETIME,
  status_code INT,
  player_count INT,
  error TEXT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS sofascore_player_season_stats (
  category_id INT,
  ut_id INT,
  season_id INT,
  stat_type VARCHAR(30),
  `rank` INT,
  player_id INT,
  player_name VARCHAR(255),
  player_position VARCHAR(50),
  team_id INT,
  team_name VARCHAR(255),
  goals INT DEFAULT 0,
  assists INT DEFAULT 0,
  appearances INT DEFAULT 0,
  minutes_played INT DEFAULT 0,
  xg DOUBLE DEFAULT 0,
  xa DOUBLE DEFAULT 0,
  yellow_cards INT DEFAULT 0,
  red_cards INT DEFAULT 0,
  fetched_at DATETIME,
  UNIQUE KEY uk_player_stats (category_id, season_id, stat_type, player_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ── Related Matches (H2H) ────────────────────────────────────
CREATE TABLE IF NOT EXISTS sofascore_related_matches_fetch_log (
  id INT AUTO_INCREMENT PRIMARY KEY,
  source_event_id BIGINT,
  fetched_at DATETIME,
  status_code INT,
  error TEXT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS sofascore_related_matches (
  source_event_id BIGINT PRIMARY KEY,
  home_team_id INT,
  home_team_name VARCHAR(255),
  away_team_id INT,
  away_team_name VARCHAR(255),
  league_category_id INT,
  league_name VARCHAR(255),
  match_timestamp BIGINT,
  home_wins INT DEFAULT 0,
  draws INT DEFAULT 0,
  away_wins INT DEFAULT 0,
  total_h2h INT DEFAULT 0,
  fetched_at DATETIME
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ── Incidents ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sofascore_incident_events (
  event_id BIGINT PRIMARY KEY,
  match_date VARCHAR(50),
  league VARCHAR(255),
  home_team VARCHAR(255),
  away_team VARCHAR(255),
  status_code INT,
  incident_count INT,
  fetched_at DATETIME,
  error TEXT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS sofascore_incidents (
  event_id BIGINT,
  incident_id BIGINT,
  match_date VARCHAR(50),
  league VARCHAR(255),
  home_team VARCHAR(255),
  away_team VARCHAR(255),
  incident_type VARCHAR(50),
  minute INT,
  added_time INT,
  time_seconds INT,
  period_time_seconds INT,
  is_home_incident TINYINT,
  team_id INT,
  team_name VARCHAR(255),
  player_id INT,
  player_name VARCHAR(255),
  related_player_id INT,
  related_player_name VARCHAR(255),
  assist_player_id INT,
  assist_player_name VARCHAR(255),
  reason VARCHAR(255),
  text TEXT,
  coordinates_x DOUBLE,
  coordinates_y DOUBLE,
  in_stats TINYINT,
  fetched_at DATETIME,
  UNIQUE KEY uk_incidents (event_id, incident_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ── Lineups ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sofascore_lineup_events (
  event_id BIGINT PRIMARY KEY,
  status_code INT,
  home_count INT,
  away_count INT,
  confirmed TINYINT,
  fetched_at DATETIME,
  error TEXT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS sofascore_lineups (
  event_id BIGINT,
  is_home TINYINT,
  team_id INT,
  team_name VARCHAR(255),
  player_id BIGINT,
  player_name VARCHAR(255),
  position VARCHAR(50),
  position_type VARCHAR(30),
  jersey_number INT,
  captain TINYINT,
  player_key VARCHAR(50),
  fetched_at DATETIME,
  UNIQUE KEY uk_lineups (event_id, is_home, player_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ── Shotmap xG ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sofascore_shotmap_xg_backfill (
  event_id BIGINT PRIMARY KEY,
  match_date VARCHAR(50),
  league VARCHAR(255),
  home_team VARCHAR(255),
  away_team VARCHAR(255),
  status_code INT,
  has_shotmap TINYINT,
  has_xg TINYINT,
  shot_count INT,
  home_shotmap_xg DOUBLE,
  away_shotmap_xg DOUBLE,
  fetched_at DATETIME,
  error TEXT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ── Shotmap Details ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sofascore_shotmap_detail_events (
  event_id BIGINT PRIMARY KEY,
  status_code INT,
  shot_count INT,
  fetched_at DATETIME,
  error TEXT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS sofascore_shotmap_details (
  event_id BIGINT,
  shot_id BIGINT,
  is_home_shot TINYINT,
  team_id INT,
  team_name VARCHAR(255),
  player_id BIGINT,
  player_name VARCHAR(255),
  player_position VARCHAR(50),
  minute INT,
  added_time INT,
  time_seconds INT,
  incident_type VARCHAR(50),
  shot_type VARCHAR(50),
  situation VARCHAR(50),
  body_part VARCHAR(30),
  goal_mouth_location VARCHAR(50),
  player_x DOUBLE,
  player_y DOUBLE,
  xg DOUBLE,
  home_team_goal_prob DOUBLE,
  away_team_goal_prob DOUBLE,
  fetched_at DATETIME,
  error TEXT,
  UNIQUE KEY uk_shotmap_details (event_id, shot_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;