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
        database=_env("MYSQL_DATABASE", "footballdata"),
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

def upsert_mysql(conn, table: str, row: dict, unique_keys: list[str] | None = None):
    cols = list(row.keys())
    vals = [row[c] for c in cols]
    if not cols:
        return
    updates = ", ".join([f"{c}=VALUES({c})" for c in cols if not unique_keys or c not in unique_keys])
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(vals))})"
        + (f" ON DUPLICATE KEY UPDATE {updates}" if updates else "")
    )
    cur = conn.cursor()
    cur.execute(sql, vals)
    conn.commit()


def upsert_sqlite(conn, table: str, row: dict):
    cols = list(row.keys())
    vals = [row[c] for c in cols]
    if not cols:
        return
    sql = f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['?'] * len(vals))})"
    conn.execute(sql, vals)
    conn.commit()


def ensure_mysql_tables(conn):
    cur = conn.cursor()
    for stmt in [
        "CREATE TABLE IF NOT EXISTS fetch_log (id BIGINT AUTO_INCREMENT PRIMARY KEY, table_name VARCHAR(64) NOT NULL, match_id BIGINT NULL, team_id INT NULL, player_id BIGINT NULL, status_code INT NOT NULL, rows_affected INT DEFAULT 0, error_message TEXT, fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP, UNIQUE KEY uniq_fetch (table_name, match_id, team_id, player_id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4",
        "CREATE TABLE IF NOT EXISTS match_incidents (incident_id BIGINT PRIMARY KEY, match_id BIGINT NOT NULL, team_id INT NOT NULL, player_id BIGINT, related_player_id BIGINT, assist_player_id BIGINT, incident_type VARCHAR(32) NOT NULL, minute INT NOT NULL, added_time INT DEFAULT 0, period VARCHAR(16) NOT NULL, is_home TINYINT(1) NOT NULL, goal_type VARCHAR(16), card_type VARCHAR(16), incident_text TEXT, reason VARCHAR(255), created_at DATETIME DEFAULT CURRENT_TIMESTAMP) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4",
        "CREATE TABLE IF NOT EXISTS match_lineups (lineup_id BIGINT AUTO_INCREMENT PRIMARY KEY, match_id BIGINT NOT NULL, team_id INT NOT NULL, player_id BIGINT NOT NULL, is_home TINYINT(1) NOT NULL, is_starter TINYINT(1) NOT NULL, jersey_number INT, position VARCHAR(50), position_category VARCHAR(8) NOT NULL, is_captain TINYINT(1) DEFAULT 0, minutes_played INT DEFAULT 0, rating DECIMAL(3,1), UNIQUE KEY uniq_lineup (match_id, team_id, player_id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4",
        "CREATE TABLE IF NOT EXISTS match_shotmap (shot_id BIGINT PRIMARY KEY, match_id BIGINT NOT NULL, team_id INT NOT NULL, player_id BIGINT NOT NULL, is_home TINYINT(1) NOT NULL, minute INT NOT NULL, incident_type VARCHAR(50), shot_type VARCHAR(50), situation VARCHAR(50), body_part VARCHAR(30), player_x DECIMAL(6,2), player_y DECIMAL(6,2), xg DECIMAL(6,4), is_goal TINYINT(1) DEFAULT 0, created_at DATETIME DEFAULT CURRENT_TIMESTAMP) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4",
        "CREATE TABLE IF NOT EXISTS match_h2h (h2h_id BIGINT AUTO_INCREMENT PRIMARY KEY, match_id BIGINT NOT NULL, home_team_id INT NOT NULL, away_team_id INT NOT NULL, home_wins INT DEFAULT 0, draws INT DEFAULT 0, away_wins INT DEFAULT 0, total_matches INT DEFAULT 0, home_goals INT DEFAULT 0, away_goals INT DEFAULT 0, UNIQUE KEY uniq_h2h (match_id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4",
        "CREATE TABLE IF NOT EXISTS referees (referee_id BIGINT PRIMARY KEY, name VARCHAR(100), country_code VARCHAR(3), created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4",
        "CREATE TABLE IF NOT EXISTS managers (manager_id BIGINT PRIMARY KEY, name VARCHAR(100), country_code VARCHAR(3), created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4",
        "CREATE TABLE IF NOT EXISTS standings (id BIGINT AUTO_INCREMENT PRIMARY KEY, category_id INT NOT NULL, tournament_name VARCHAR(120), season_id INT NOT NULL, standing_type VARCHAR(32) NOT NULL, position INT, team_id INT NOT NULL, team_name VARCHAR(100), team_short_name VARCHAR(50), played INT DEFAULT 0, wins INT DEFAULT 0, draws INT DEFAULT 0, losses INT DEFAULT 0, goals_for INT DEFAULT 0, goals_against INT DEFAULT 0, goal_diff INT DEFAULT 0, points INT DEFAULT 0, last_5 TEXT, streak VARCHAR(50), fetched_at DATETIME, UNIQUE KEY uniq_standing (category_id, season_id, standing_type, team_id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4",
        "CREATE TABLE IF NOT EXISTS player_season_stats (id BIGINT AUTO_INCREMENT PRIMARY KEY, player_id BIGINT NOT NULL, season_id INT NOT NULL, competition_id INT, team_id INT, stat_name VARCHAR(64) NOT NULL, stat_value DECIMAL(18,6), stat_text VARCHAR(100), fetched_at DATETIME, UNIQUE KEY uniq_player_stat (player_id, season_id, stat_name)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4",
        "CREATE TABLE IF NOT EXISTS team_rankings (id BIGINT AUTO_INCREMENT PRIMARY KEY, team_id INT NOT NULL, team_name VARCHAR(100), rank_type VARCHAR(64) NOT NULL, rank_value INT, points DECIMAL(10,2), fetched_at DATETIME, UNIQUE KEY uniq_team_rank (team_id, rank_type)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4",
        "CREATE TABLE IF NOT EXISTS matches (match_id BIGINT PRIMARY KEY, attendance INT DEFAULT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4",
        "CREATE TABLE IF NOT EXISTS match_statistics (stat_id BIGINT AUTO_INCREMENT PRIMARY KEY, match_id BIGINT NOT NULL, stat_group VARCHAR(50) NOT NULL, stat_name VARCHAR(50) NOT NULL, home_value VARCHAR(100), away_value VARCHAR(100), home_value_num DECIMAL(10,2), away_value_num DECIMAL(10,2), UNIQUE KEY uniq_stat (match_id, stat_group, stat_name)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4",
        "CREATE TABLE IF NOT EXISTS match_player_stats (player_stat_id BIGINT AUTO_INCREMENT PRIMARY KEY, match_id BIGINT NOT NULL, team_id INT NOT NULL, player_id BIGINT NOT NULL, is_home TINYINT(1) NOT NULL, minutes_played INT DEFAULT 0, goals INT DEFAULT 0, shots INT DEFAULT 0, shots_on_target INT DEFAULT 0, xg DECIMAL(5,3) DEFAULT 0, assists INT DEFAULT 0, xa DECIMAL(5,3) DEFAULT 0, key_passes INT DEFAULT 0, passes_completed INT DEFAULT 0, tackles INT DEFAULT 0, interceptions INT DEFAULT 0, duels_won INT DEFAULT 0, fouls_committed INT DEFAULT 0, yellow_cards INT DEFAULT 0, red_cards INT DEFAULT 0, rating DECIMAL(3,1), UNIQUE KEY uniq_player_match (match_id, player_id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    ]:
        cur.execute(stmt)
    conn.commit()
