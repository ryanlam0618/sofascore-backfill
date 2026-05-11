#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mysql_helpers.py — Shared MySQL helpers for sofascore_backfill repo.
Loads credentials from /root/.openclaw/workspace/.env automatically.

Usage in any script:
    from mysql_helpers import get_mysql_conn, use_mysql, upsert_mysql, upsert_sqlite

    use_mysql()          # activate MySQL for this run (env: USE_MYSQL=1)
    conn = get_mysql_conn()  # returns MySQL or SQLite conn based on flag
    upsert_mysql(conn, table, row)   # MySQL: INSERT ... ON DUPLICATE KEY UPDATE
    upsert_sqlite(conn, table, row)   # SQLite: INSERT OR REPLACE

Also exports:
    mysql_connect()  → mysql.connector connection (raw)
    mysql_cursor()   → cursor with dict-like access
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# ── Credential loading ──────────────────────────────────────────────────────

def _load_env() -> dict:
    env_path = Path("/root/.openclaw/workspace/.env")
    env = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env

_ENV = _load_env()


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key) or _ENV.get(key, default)


# ── MySQL connection ─────────────────────────────────────────────────────────

_MYSQL_CONN: "mysql.connector.MySQLConnection" | None = None

def mysql_connect(autocommit: bool = False) -> "mysql.connector.MySQLConnection":
    """Return a raw mysql.connector connection (singleton per process)."""
    global _MYSQL_CONN
    if _MYSQL_CONN is not None:
        try:
            _MYSQL_CONN.ping(reconnect=True)
            return _MYSQL_CONN
        except Exception:
            _MYSQL_CONN = None

    import mysql.connector
    _MYSQL_CONN = mysql.connector.connect(
        host=_env("MYSQL_HOST", "127.0.0.1"),
        port=int(_env("MYSQL_PORT", "3306")),
        user=_env("MYSQL_USER"),
        password=_env("MYSQL_PASSWORD"),
        database=_env("MYSQL_DATABASE", "appdb"),
        charset="utf8mb4",
        collation="utf8mb4_unicode_ci",
        autocommit=autocommit,
        raise_on_warnings=False,
    )
    return _MYSQL_CONN


def mysql_cursor(autocommit: bool = False) -> "mysql.connector.MySQLCursor":
    """Return a MySQL cursor with dict-like access."""
    conn = mysql_connect(autocommit=autocommit)
    cur = conn.cursor(dictionary=True)
    return cur


# ── Feature flag ────────────────────────────────────────────────────────────

def use_mysql() -> bool:
    """Return True if MySQL should be used (USE_MYSQL=1 env var or --use-mysql arg)."""
    return _env("USE_MYSQL", "0") == "1"


def get_db_connection(args_db: str | None = None) -> tuple:
    """
    Return (conn, is_mysql, conn_close_fn).
    is_mysql=True  → conn is mysql.connector connection
    is_mysql=False → conn is sqlite3.Connection

    Usage:
        conn, is_mysql, close_fn = get_db_connection(args.db)
        try:
            if is_mysql:
                cur = conn.cursor(dictionary=True)
            else:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
            ... use cur ...
        finally:
            close_fn(conn)
    """
    import sqlite3

    if use_mysql():
        conn = mysql_connect()
        return conn, True, _close_mysql

    db_path = args_db or "data/default.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout = 60000")
    return conn, False, _close_sqlite


def _close_mysql(conn):
    try:
        conn.commit()
    except Exception:
        pass


def _close_sqlite(conn):
    conn.commit()
    conn.close()


# ── Upsert helpers ────────────────────────────────────────────────────────────

def upsert_row(conn, is_mysql: bool, table: str, row: dict, primary_key: tuple[str, ...] | None = None):
    """
    Upsert a row into table.

    MySQL:  INSERT INTO table (...) VALUES (...) ON DUPLICATE KEY UPDATE ...
    SQLite: INSERT OR REPLACE INTO table (...) VALUES (...)

    row = {"col": value, ...}
    primary_key (mysql only): column(s) that form the unique key (default: all non-autoinc cols)
    """
    if is_mysql:
        _upsert_mysql(conn, table, row, primary_key)
    else:
        _upsert_sqlite(conn, table, row)


def _upsert_mysql(conn, table: str, row: dict, primary_key: tuple[str, ...] | None):
    """MySQL-specific upsert using ON DUPLICATE KEY UPDATE."""
    if not row:
        return
    cols = list(row.keys())
    vals = list(row.values())

    if primary_key:
        update_parts = [f"{c}=VALUES({c})" for c in cols if c not in primary_key]
    else:
        # All non-id cols are update targets
        update_parts = [f"{c}=VALUES({c})" for c in cols if c.lower() not in ("id", "auto_increment")]

    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) "
        f"VALUES ({', '.join(['%s'] * len(vals))}) "
        f"ON DUPLICATE KEY UPDATE {', '.join(update_parts)}"
    )
    cur = conn.cursor()
    cur.execute(sql, vals)
    cur.close()


def _upsert_sqlite(conn, table: str, row: dict):
    """SQLite upsert using INSERT OR REPLACE."""
    if not row:
        return
    cols = list(row.keys())
    vals = list(row.values())
    sql = f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['?'] * len(vals))})"
    conn.execute(sql, vals)


# ── Table-existence check ─────────────────────────────────────────────────────

def ensure_mysql_tables(conn) -> dict[str, list[str]]:
    """
    Create all sofascore_* tables in MySQL `appdb`.
    Returns dict mapping group → list of created table names.
    """
    groups: dict[str, list[str]] = {}

    cur = conn.cursor()

    # ── Standings ────────────────────────────────────────────────────────────
    groups["standings"] = ["sofascore_standings", "sofascore_standings_fetch_log"]
    cur.execute("""
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
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    cur.execute("""
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
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)

    # ── Player Stats ──────────────────────────────────────────────────────────
    groups["player_stats"] = ["sofascore_player_stats_fetch_log", "sofascore_player_season_stats"]
    cur.execute("""
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
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    cur.execute("""
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
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)

    # ── Related Matches (H2H) ─────────────────────────────────────────────────
    groups["related_matches"] = ["sofascore_related_matches_fetch_log", "sofascore_related_matches"]
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sofascore_related_matches_fetch_log (
          id INT AUTO_INCREMENT PRIMARY KEY,
          source_event_id BIGINT,
          fetched_at DATETIME,
          status_code INT,
          error TEXT
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    cur.execute("""
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
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)

    # ── Incidents ─────────────────────────────────────────────────────────────
    groups["incidents"] = ["sofascore_incident_events", "sofascore_incidents"]
    cur.execute("""
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
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    cur.execute("""
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
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)

    # ── Lineups ───────────────────────────────────────────────────────────────
    groups["lineups"] = ["sofascore_lineup_events", "sofascore_lineups"]
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sofascore_lineup_events (
          event_id BIGINT PRIMARY KEY,
          status_code INT,
          home_count INT,
          away_count INT,
          confirmed TINYINT,
          fetched_at DATETIME,
          error TEXT
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    cur.execute("""
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
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)

    # ── Shotmap xG ────────────────────────────────────────────────────────────
    groups["shotmap_xg"] = ["sofascore_shotmap_xg_backfill"]
    cur.execute("""
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
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)

    # ── Shotmap Details ──────────────────────────────────────────────────────
    groups["shotmap_details"] = ["sofascore_shotmap_detail_events", "sofascore_shotmap_details"]
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sofascore_shotmap_detail_events (
          event_id BIGINT PRIMARY KEY,
          status_code INT,
          shot_count INT,
          fetched_at DATETIME,
          error TEXT
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    cur.execute("""
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
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)


    # ── Attendance ──────────────────────────────────────────────────────────
    groups["attendance"] = ["sofascore_attendance_fetch_log", "sofascore_attendance"]
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sofascore_attendance_fetch_log (
          id INT AUTO_INCREMENT PRIMARY KEY,
          event_id BIGINT,
          fetched_at DATETIME,
          status_code INT,
          attendance_raw VARCHAR(50),
          attendance_int INT,
          error TEXT,
          UNIQUE KEY uk_event (event_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sofascore_attendance (
          event_id BIGINT PRIMARY KEY,
          attendance_raw VARCHAR(50),
          attendance_int INT,
          fetched_at DATETIME,
          status_code INT,
          error TEXT
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)

    # ── Referees ────────────────────────────────────────────────────────────
    groups["referees"] = ["sofascore_referees_fetch_log", "sofascore_referees"]
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sofascore_referees_fetch_log (
          id INT AUTO_INCREMENT PRIMARY KEY,
          season_id INT DEFAULT NULL,
          fetched_at DATETIME DEFAULT NULL,
          status_code INT DEFAULT NULL,
          match_count INT DEFAULT NULL,
          error TEXT COLLATE utf8mb4_unicode_ci
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sofascore_referees (
          referee_id INT PRIMARY KEY,
          name VARCHAR(255) DEFAULT NULL,
          short_name VARCHAR(100) DEFAULT NULL,
          slug VARCHAR(100) DEFAULT NULL,
          country_alpha2 VARCHAR(2) DEFAULT NULL,
          country_name VARCHAR(100) DEFAULT NULL,
          matches_total INT DEFAULT 0,
          yellow_cards_total INT DEFAULT 0,
          red_cards_total INT DEFAULT 0,
          yellow_red_cards_total INT DEFAULT 0,
          fetched_at DATETIME DEFAULT NULL,
          updated_at DATETIME DEFAULT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)

    # ── Team Rankings ───────────────────────────────────────────────────────
    groups["team_rankings"] = ["sofascore_team_rankings_fetch_log", "sofascore_team_rankings"]
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sofascore_team_rankings_fetch_log (
          id INT AUTO_INCREMENT PRIMARY KEY,
          team_id INT,
          fetched_at DATETIME,
          status_code INT,
          error TEXT
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sofascore_team_rankings (
          team_id INT,
          team_name VARCHAR(255),
          team_slug VARCHAR(100),
          year INT,
          ranking_type VARCHAR(50),
          ranking_type_name VARCHAR(255),
          ranking INT,
          league_name VARCHAR(255),
          league_id INT,
          continent_name VARCHAR(100),
          country_name VARCHAR(100),
          played INT DEFAULT 0,
          wins INT DEFAULT 0,
          draws INT DEFAULT 0,
          losses INT DEFAULT 0,
          goals_for INT DEFAULT 0,
          goals_against INT DEFAULT 0,
          goal_diff INT DEFAULT 0,
          points INT DEFAULT 0,
          fetched_at DATETIME,
          UNIQUE KEY uk_team_year (team_id, year, ranking_type)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)

    conn.commit()
    cur.close()
    return groups


# ── SQL schema file (for reference / manual apply) ──────────────────────────

SQL_SCHEMA = """
-- ============================================================
-- SofaScore Backfill — MySQL Schema for `appdb`
-- Run:  mysql -u A100 -p appdb < mysql_schema.sql
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
  last_5 TEXT DEFAULT '',
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
"""