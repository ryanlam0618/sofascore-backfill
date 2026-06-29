-- SofaScore Backfill — MySQL Schema v2.0 (Normalized)
-- 2025-06-30: Upgraded with proper normalization

-- Core Reference Tables
CREATE TABLE IF NOT EXISTS countries (
    country_code VARCHAR(3) PRIMARY KEY,
    country_name VARCHAR(100) NOT NULL,
    continent VARCHAR(20),
    alpha2 VARCHAR(2),
    created_at DATETIME DEFAULT NOW()
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS competitions (
    competition_id INT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    short_name VARCHAR(50),
    category_id INT NOT NULL,
    ut_id INT,
    type ENUM('league', 'cup', 'international') DEFAULT 'league',
    country_code VARCHAR(3),
    season_mode VARCHAR(20),
    created_at DATETIME DEFAULT NOW(),
    updated_at DATETIME DEFAULT NOW(),
    FOREIGN KEY (country_code) REFERENCES countries(country_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS seasons (
    season_id INT PRIMARY KEY,
    competition_id INT NOT NULL,
    year_label VARCHAR(20) NOT NULL,
    year_start INT,
    year_end INT,
    start_date DATE,
    end_date DATE,
    is_current BOOLEAN DEFAULT FALSE,
    created_at DATETIME DEFAULT NOW(),
    FOREIGN KEY (competition_id) REFERENCES competitions(competition_id),
    UNIQUE KEY (competition_id, year_label)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS teams (
    team_id INT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    short_name VARCHAR(50),
    slug VARCHAR(100),
    country_code VARCHAR(3),
    city VARCHAR(100),
    stadium VARCHAR(100),
    founded_year INT,
    team_type ENUM('club', 'national') DEFAULT 'club',
    created_at DATETIME DEFAULT NOW(),
    updated_at DATETIME DEFAULT NOW(),
    FOREIGN KEY (country_code) REFERENCES countries(country_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS players (
    player_id BIGINT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    short_name VARCHAR(50),
    slug VARCHAR(100),
    country_code VARCHAR(3),
    birth_date DATE,
    age INT,
    height INT,
    weight INT,
    position VARCHAR(50),
    preferred_foot ENUM('left', 'right', 'both'),
    current_team_id INT,
    created_at DATETIME DEFAULT NOW(),
    updated_at DATETIME DEFAULT NOW(),
    FOREIGN KEY (country_code) REFERENCES countries(country_code),
    FOREIGN KEY (current_team_id) REFERENCES teams(team_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Core Match Table
CREATE TABLE IF NOT EXISTS matches (
    match_id BIGINT PRIMARY KEY,
    season_id INT NOT NULL,
    competition_id INT NOT NULL,
    round_name VARCHAR(50),
    match_date DATE NOT NULL,
    match_time TIME,
    home_team_id INT NOT NULL,
    away_team_id INT NOT NULL,
    home_score INT DEFAULT NULL,
    away_score INT DEFAULT NULL,
    status ENUM('scheduled', 'live', 'finished', 'postponed') DEFAULT 'scheduled',
    attendance INT DEFAULT NULL,
    referee_id INT DEFAULT NULL,
    created_at DATETIME DEFAULT NOW(),
    updated_at DATETIME DEFAULT NOW(),
    FOREIGN KEY (season_id) REFERENCES seasons(season_id),
    FOREIGN KEY (competition_id) REFERENCES competitions(competition_id),
    FOREIGN KEY (home_team_id) REFERENCES teams(team_id),
    FOREIGN KEY (away_team_id) REFERENCES teams(team_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Match Incidents
CREATE TABLE IF NOT EXISTS match_incidents (
    incident_id BIGINT PRIMARY KEY,
    match_id BIGINT NOT NULL,
    team_id INT NOT NULL,
    player_id BIGINT,
    related_player_id BIGINT,
    assist_player_id BIGINT,
    incident_type ENUM('goal', 'card', 'substitution', 'period', 'var') NOT NULL,
    minute INT NOT NULL,
    added_time INT DEFAULT 0,
    period ENUM('first', 'second', 'extra_first', 'extra_second') NOT NULL,
    is_home TINYINT(1) NOT NULL,
    goal_type ENUM('regular', 'penalty', 'own_goal', 'free_kick'),
    card_type ENUM('yellow', 'red', 'yellow_red'),
    incident_text TEXT,
    reason VARCHAR(255),
    created_at DATETIME DEFAULT NOW(),
    FOREIGN KEY (match_id) REFERENCES matches(match_id),
    FOREIGN KEY (team_id) REFERENCES teams(team_id),
    FOREIGN KEY (player_id) REFERENCES players(player_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Match Lineups
CREATE TABLE IF NOT EXISTS match_lineups (
    lineup_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    match_id BIGINT NOT NULL,
    team_id INT NOT NULL,
    player_id BIGINT NOT NULL,
    is_home TINYINT(1) NOT NULL,
    is_starter TINYINT(1) NOT NULL,
    jersey_number INT,
    position VARCHAR(50),
    position_category ENUM('GK', 'DEF', 'MID', 'FWD') NOT NULL,
    is_captain TINYINT(1) DEFAULT 0,
    minutes_played INT DEFAULT 0,
    rating DECIMAL(3,1),
    FOREIGN KEY (match_id) REFERENCES matches(match_id),
    FOREIGN KEY (team_id) REFERENCES teams(team_id),
    FOREIGN KEY (player_id) REFERENCES players(player_id),
    UNIQUE KEY (match_id, team_id, player_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Match Statistics (Key-Value Design)
CREATE TABLE IF NOT EXISTS match_statistics (
    stat_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    match_id BIGINT NOT NULL,
    stat_group VARCHAR(50) NOT NULL,
    stat_name VARCHAR(50) NOT NULL,
    home_value VARCHAR(100),
    away_value VARCHAR(100),
    home_value_num DECIMAL(10,2),
    away_value_num DECIMAL(10,2),
    FOREIGN KEY (match_id) REFERENCES matches(match_id),
    UNIQUE KEY (match_id, stat_group, stat_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Match Shotmap
CREATE TABLE IF NOT EXISTS match_shotmap (
    shot_id BIGINT PRIMARY KEY,
    match_id BIGINT NOT NULL,
    team_id INT NOT NULL,
    player_id BIGINT NOT NULL,
    is_home TINYINT(1) NOT NULL,
    minute INT NOT NULL,
    incident_type VARCHAR(50),
    shot_type VARCHAR(50),
    situation VARCHAR(50),
    body_part VARCHAR(30),
    player_x DECIMAL(6,2),
    player_y DECIMAL(6,2),
    xg DECIMAL(6,4),
    is_goal TINYINT(1) DEFAULT 0,
    created_at DATETIME DEFAULT NOW(),
    FOREIGN KEY (match_id) REFERENCES matches(match_id),
    FOREIGN KEY (team_id) REFERENCES teams(team_id),
    FOREIGN KEY (player_id) REFERENCES players(player_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Match Player Stats
CREATE TABLE IF NOT EXISTS match_player_stats (
    player_stat_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    match_id BIGINT NOT NULL,
    team_id INT NOT NULL,
    player_id BIGINT NOT NULL,
    is_home TINYINT(1) NOT NULL,
    minutes_played INT DEFAULT 0,
    goals INT DEFAULT 0,
    shots INT DEFAULT 0,
    shots_on_target INT DEFAULT 0,
    xg DECIMAL(5,3) DEFAULT 0,
    assists INT DEFAULT 0,
    xa DECIMAL(5,3) DEFAULT 0,
    key_passes INT DEFAULT 0,
    passes_completed INT DEFAULT 0,
    tackles INT DEFAULT 0,
    interceptions INT DEFAULT 0,
    duels_won INT DEFAULT 0,
    fouls_committed INT DEFAULT 0,
    yellow_cards INT DEFAULT 0,
    red_cards INT DEFAULT 0,
    rating DECIMAL(3,1),
    FOREIGN KEY (match_id) REFERENCES matches(match_id),
    FOREIGN KEY (team_id) REFERENCES teams(team_id),
    FOREIGN KEY (player_id) REFERENCES players(player_id),
    UNIQUE KEY (match_id, player_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Match Momentum
CREATE TABLE IF NOT EXISTS match_momentum (
    momentum_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    match_id BIGINT NOT NULL,
    minute INT NOT NULL,
    period ENUM('first', 'second') NOT NULL,
    home_value INT NOT NULL,
    away_value INT NOT NULL,
    FOREIGN KEY (match_id) REFERENCES matches(match_id),
    UNIQUE KEY (match_id, minute)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Match H2H
CREATE TABLE IF NOT EXISTS match_h2h (
    h2h_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    match_id BIGINT NOT NULL,
    home_team_id INT NOT NULL,
    away_team_id INT NOT NULL,
    home_wins INT DEFAULT 0,
    draws INT DEFAULT 0,
    away_wins INT DEFAULT 0,
    total_matches INT DEFAULT 0,
    home_goals INT DEFAULT 0,
    away_goals INT DEFAULT 0,
    FOREIGN KEY (match_id) REFERENCES matches(match_id),
    UNIQUE KEY (match_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Match Votes
CREATE TABLE IF NOT EXISTS match_votes (
    vote_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    match_id BIGINT NOT NULL,
    home_votes INT DEFAULT 0,
    draw_votes INT DEFAULT 0,
    away_votes INT DEFAULT 0,
    home_percentage DECIMAL(5,2),
    draw_percentage DECIMAL(5,2),
    away_percentage DECIMAL(5,2),
    fetched_at DATETIME,
    FOREIGN KEY (match_id) REFERENCES matches(match_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Match Best Players
CREATE TABLE IF NOT EXISTS match_best_players (
    best_player_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    match_id BIGINT NOT NULL,
    player_id BIGINT NOT NULL,
    team_id INT NOT NULL,
    is_home TINYINT(1) NOT NULL,
    rating DECIMAL(3,1) NOT NULL,
    rank INT,
    FOREIGN KEY (match_id) REFERENCES matches(match_id),
    FOREIGN KEY (player_id) REFERENCES players(player_id),
    FOREIGN KEY (team_id) REFERENCES teams(team_id),
    UNIQUE KEY (match_id, player_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Match Average Positions
CREATE TABLE IF NOT EXISTS match_average_positions (
    avg_pos_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    match_id BIGINT NOT NULL,
    player_id BIGINT NOT NULL,
    team_id INT NOT NULL,
    avg_x DECIMAL(6,2),
    avg_y DECIMAL(6,2),
    FOREIGN KEY (match_id) REFERENCES matches(match_id),
    FOREIGN KEY (player_id) REFERENCES players(player_id),
    UNIQUE KEY (match_id, player_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Match Odds
CREATE TABLE IF NOT EXISTS match_odds (
    odds_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    match_id BIGINT NOT NULL,
    bookmaker_id INT,
    bookmaker_name VARCHAR(50),
    odds_type ENUM('1x2', 'asian_handicap', 'over_under') DEFAULT '1x2',
    home_odds DECIMAL(10,3),
    draw_odds DECIMAL(10,3),
    away_odds DECIMAL(10,3),
    fetched_at DATETIME,
    FOREIGN KEY (match_id) REFERENCES matches(match_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Standings
CREATE TABLE IF NOT EXISTS standings (
    standing_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    season_id INT NOT NULL,
    competition_id INT NOT NULL,
    standing_type VARCHAR(20) DEFAULT 'overall',
    position INT NOT NULL,
    team_id INT NOT NULL,
    played INT DEFAULT 0,
    wins INT DEFAULT 0,
    draws INT DEFAULT 0,
    losses INT DEFAULT 0,
    goals_for INT DEFAULT 0,
    goals_against INT DEFAULT 0,
    goal_diff INT DEFAULT 0,
    points INT DEFAULT 0,
    form VARCHAR(10),
    FOREIGN KEY (season_id) REFERENCES seasons(season_id),
    FOREIGN KEY (competition_id) REFERENCES competitions(competition_id),
    FOREIGN KEY (team_id) REFERENCES teams(team_id),
    UNIQUE KEY (season_id, standing_type, team_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Player Season Stats
CREATE TABLE IF NOT EXISTS player_season_stats (
    stat_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    season_id INT NOT NULL,
    competition_id INT NOT NULL,
    stat_type VARCHAR(30) NOT NULL,
    rank INT,
    player_id BIGINT NOT NULL,
    team_id INT NOT NULL,
    goals INT DEFAULT 0,
    assists INT DEFAULT 0,
    appearances INT DEFAULT 0,
    minutes_played INT DEFAULT 0,
    xg DECIMAL(5,2) DEFAULT 0,
    xa DECIMAL(5,2) DEFAULT 0,
    yellow_cards INT DEFAULT 0,
    red_cards INT DEFAULT 0,
    rating DECIMAL(3,1),
    FOREIGN KEY (season_id) REFERENCES seasons(season_id),
    FOREIGN KEY (player_id) REFERENCES players(player_id),
    FOREIGN KEY (team_id) REFERENCES teams(team_id),
    UNIQUE KEY (season_id, stat_type, player_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Team Rankings
CREATE TABLE IF NOT EXISTS team_rankings (
    ranking_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    team_id INT NOT NULL,
    year INT NOT NULL,
    ranking_type INT NOT NULL,
    ranking_type_name VARCHAR(50),
    ranking INT NOT NULL,
    points INT DEFAULT 0,
    ranking_class VARCHAR(50),
    fetched_at DATETIME,
    FOREIGN KEY (team_id) REFERENCES teams(team_id),
    UNIQUE KEY (team_id, year, ranking_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Unified Fetch Log
CREATE TABLE IF NOT EXISTS fetch_log (
    log_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    table_name VARCHAR(50) NOT NULL,
    match_id BIGINT,
    season_id INT,
    competition_id INT,
    operation VARCHAR(50),
    status ENUM('success', 'error', 'retry', 'skip') NOT NULL,
    status_code INT,
    rows_affected INT DEFAULT 0,
    error_message TEXT,
    duration_ms INT,
    source VARCHAR(50),
    fetched_at DATETIME DEFAULT NOW(),
    INDEX idx_table (table_name),
    INDEX idx_match (match_id),
    INDEX idx_time (fetched_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Indexes
CREATE INDEX idx_matches_date ON matches(match_date);
CREATE INDEX idx_matches_season ON matches(season_id);
CREATE INDEX idx_incidents_match ON match_incidents(match_id);
CREATE INDEX idx_shotmap_match ON match_shotmap(match_id);
CREATE INDEX idx_lineups_match ON match_lineups(match_id);
