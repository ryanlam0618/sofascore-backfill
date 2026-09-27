#!/usr/bin/env python3
"""test_gen4_stage4d_insert_player_stats.py — Stage 4d-1 nested-dict fix tests.

Verifies `backfill_runner.insert_player_stats` no longer raises
`MySQLInterfaceError: Python type dict cannot be converted` when a player's
lineups `statistics` payload embeds nested dict fields:
  - ``statisticsType`` -> ``{"sportSlug":"football","statisticsType":"player"}``
  - ``ratingVersions`` -> ``{"original":6.5,"alternative":6.6}``

Offline — no network, no MySQL. The insert path is exercised against a fake
cursor that records the bound params (the exact thing MySQL would reject).

Run:
  .runner-venv/bin/python -m pytest tests/test_gen4_stage4d_insert_player_stats.py -v
  # or from the repo root:
  .runner-venv/bin/python -m pytest tests/ -k stage4d -v
"""
from __future__ import annotations

import json

import backfill_runner


# ── fixture: a fake connection whose cursor() captures execute() params ──────

class _FakeCursor:
    def __init__(self):
        self.last_sql = None
        self.last_params = None
        self.calls = []

    def execute(self, sql, params=None):
        self.last_sql = sql
        self.last_params = params
        self.calls.append((sql, params))


class _FakeConn:
    def __init__(self):
        self.cur = _FakeCursor()

    def cursor(self):
        return self.cur

    def commit(self):
        pass


class _Inserter(backfill_runner.DataInserter):
    """DataInserter subclass wired to a fake connection (no DB)."""

    def __init__(self, conn):
        self.conn = conn


# ── _stat_value coercion unit tests ──────────────────────────────────────────

def test_stat_value_scalar_passthrough():
    stats = {"goals": 2, "rating": 8.3, "minutesPlayed": 90, "totalPass": None}
    assert backfill_runner._stat_value(stats, "goals") == 2
    assert backfill_runner._stat_value(stats, "rating") == 8.3
    assert backfill_runner._stat_value(stats, "totalPass") is None  # missing ok


def test_stat_value_statistics_type_dict_unwraps_inner_string():
    """The core bug: statisticsType is a dict; we must store the inner string."""
    stats = {"statisticsType": {"sportSlug": "football", "statisticsType": "player"}}
    assert backfill_runner._stat_value(stats, "statisticsType") == "player"


def test_stat_value_dict_without_inner_string_becomes_json():
    stats = {"weirdField": {"a": 1, "b": 2}}
    got = backfill_runner._stat_value(stats, "weirdField")
    parsed = json.loads(got)
    assert parsed == {"a": 1, "b": 2}
    assert isinstance(got, str)


def test_stat_value_list_becomes_json():
    stats = {"someList": [1, 2, 3]}
    got = backfill_runner._stat_value(stats, "someList")
    assert json.loads(got) == [1, 2, 3]
    assert isinstance(got, str)


def test_stat_value_non_dict_stats_returns_none():
    assert backfill_runner._stat_value(None, "x") is None
    assert backfill_runner._stat_value([], "x") is None


# ── insert_player_stats end-to-end (fake cursor, no crash) ───────────────────

def _make_player(**overrides):
    """A realistic lineups player entry carrying BOTH dict fields."""
    stats = {
        "minutesPlayed": 90,
        "rating": 7.8,
        "goals": 1,
        "totalShots": 3,
        "totalPass": 42,
        "accuratePass": 38,
        "statisticsType": {"sportSlug": "football", "statisticsType": "player"},
        "ratingVersions": {"original": 7.8, "alternative": 7.9},
    }
    stats.update(overrides.get("statistics", {}))
    return {
        "player": {"id": 1001, "name": "Test Player"},
        "statistics": stats,
    }


def test_insert_player_stats_handles_nested_dict_without_error():
    conn = _FakeConn()
    ins = _Inserter(conn)
    entry = _make_player()

    # Must NOT raise MySQLInterfaceError
    n = ins.insert_player_stats(match_id=88888, player_entry=entry,
                                team_id=7, is_home=1, commit=True)

    assert n == 1
    assert conn.cur.last_sql is not None
    params = conn.cur.last_params
    d = dict(zip(_param_keys(conn.cur.last_sql), params))

    # statistics_type must be the unwrapped inner string, NOT a dict
    assert d["statistics_type"] == "player"

    # rating_versions must be a serialised JSON string
    assert isinstance(d["rating_versions"], str)
    assert json.loads(d["rating_versions"]) == {"original": 7.8, "alternative": 7.9}

    # raw_statistics preserves the full payload
    assert isinstance(d["raw_statistics"], str)
    assert json.loads(d["raw_statistics"])["goals"] == 1

    # scalar columns unchanged
    assert d["goals"] == 1
    assert d["rating"] == 7.8
    assert d["total_pass"] == 42


def test_insert_player_stats_all_scalar_still_works(backward_compat=True):
    """Regression guard: a scalar-only payload (no dict fields) still inserts."""
    conn = _FakeConn()
    ins = _Inserter(conn)
    entry = _make_player(statistics={"ratingVersions": None, "statisticsType": "player"})

    n = ins.insert_player_stats(88888, entry, 7, 1, commit=False)
    assert n == 1
    params = conn.cur.last_params
    d = dict(zip(_param_keys(conn.cur.last_sql), params))
    assert d["statistics_type"] == "player"
    assert d["rating_versions"] is None
    assert d["goals"] == 1


def test_insert_player_stats_idempotent_sql_uses_upsert():
    """The generated SQL must be an ON DUPLICATE KEY UPDATE on the unique key."""
    conn = _FakeConn()
    ins = _Inserter(conn)
    ins.insert_player_stats(88888, _make_player(), 7, 1, commit=False)
    sql = conn.cur.last_sql
    assert "INSERT INTO match_player_stats" in sql
    assert "ON DUPLICATE KEY UPDATE" in sql
    # update_cols excludes the composite key (match_id, player_id)
    assert "match_id=VALUES(match_id)" not in sql
    assert "player_id=VALUES(player_id)" not in sql


# ── helper: parse column order from the generated INSERT ────────────────────

def _param_keys(sql):
    """Extract column names from `INSERT INTO t (c1, c2, ...) VALUES ...`."""
    # Grab the parenthesised column list right after the table name.
    start = sql.index("(")
    end = sql.index(")", start)
    cols = sql[start + 1:end]
    return [c.strip().strip("`") for c in cols.split(",") if c.strip()]


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v", "-s"]))