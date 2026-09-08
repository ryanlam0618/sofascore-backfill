#!/usr/bin/env python3
"""test_gen4_stage4d_deadlock_retry.py — Stage 4d-1 deadlock retry tests.

Stage 4c Batch C surfaced 37 `InternalError 1213 (40001) Deadlock found`
insert_errors (33 odds + 3 average_positions + 1 momentum) under 8 concurrent
workers. Root cause: DELETE+INSERT / ON DUPLICATE writers over the shared
InnoDB tables contend on gap/next-key locks.

Fix: `DataInserter._retry_deadlock` retries a whole insert on MySQL errno
1213 (deadlock) / 1205 (lock-wait timeout) with exponential backoff, since
InnoDB fully rolls back the victim transaction. `backfill_runner.insert_odds`
and `insert_graph_points` now route through it.

Offline — fake cursor, no DB. Run:
  .runner-venv/bin/python -m pytest tests/test_gen4_stage4d_deadlock_retry.py -v
"""
from __future__ import annotations

import backfill_runner


class _MySQLError(Exception):
    def __init__(self, errno, msg="boom"):
        super().__init__(msg)
        self.errno = errno


class _FakeCursor:
    def __init__(self):
        self.last_sql = None
        self.last_params = None

    def execute(self, sql, params=None):
        self.last_sql = sql
        self.last_params = params


class _FakeConn:
    def __init__(self, fail_errnos=()):
        self.fail_errnos = list(fail_errnos)
        self.cur = _FakeCursor()
        self.rollbacks = 0

    def cursor(self):
        return self.cur

    def commit(self):
        pass

    def rollback(self):
        self.rollbacks += 1


class _Inserter(backfill_runner.DataInserter):
    def __init__(self, conn):
        self.conn = conn


# ── _retry_deadlock unit behaviour ──────────────────────────────────────────

def test_retry_deadlock_succeeds_after_transient_1213():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _MySQLError(1213, "Deadlock found when trying to get lock")
        return "done"

    ins = _Inserter(_FakeConn())
    got = ins._retry_deadlock(flaky, retries=3)
    assert got == "done"
    assert calls["n"] == 3  # 2 deadlocks + 1 success


def test_retry_deadlock_gives_up_after_retries():
    calls = {"n": 0}

    def always_deadlock():
        calls["n"] += 1
        raise _MySQLError(1213, "Deadlock found")

    ins = _Inserter(_FakeConn())
    import pytest
    with pytest.raises(_MySQLError):
        ins._retry_deadlock(always_deadlock, retries=2)
    assert calls["n"] == 3  # initial + 2 retries, then re-raise


def test_retry_deadlock_does_not_swallow_other_errors():
    def boom():
        raise _MySQLError(1062, "Duplicate entry")  # NOT a deadlock errno

    ins = _Inserter(_FakeConn())
    import pytest
    with pytest.raises(_MySQLError):
        ins._retry_deadlock(boom, retries=3)


def test_retry_deadlock_rolls_back_on_retry():
    conn = _FakeConn()
    ins = _Inserter(conn)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _MySQLError(1205, "Lock wait timeout")
        return "ok"

    ins._retry_deadlock(flaky, retries=2)
    assert conn.rollbacks >= 1  # rolled back before retrying


# ── insert_odds routes through deadlock retry ───────────────────────────────

def test_insert_odds_retries_on_deadlock():
    """insert_odds must survive a single deadlock via _retry_deadlock."""
    calls = {"n": 0}

    class FlakyInserter(_Inserter):
        def _insert_odds_once(self, match_id, data):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _MySQLError(1213, "Deadlock found when trying to get lock")
            return super()._insert_odds_once(match_id, data)

    conn = _FakeConn()
    ins = FlakyInserter(conn)
    body = {"markets": [{"marketId": 1, "marketName": "Match Winner",
                         "marketGroup": "3Way", "marketPeriod": "ALL",
                         "structureType": "Total", "suspended": False,
                         "choices": [{"name": "Home", "initialFractionalValue": "1/2",
                                      "fractionalValue": "1/2", "winning": True}]}]}
    n = ins.insert_odds(999, body)
    assert n == 1
    assert calls["n"] == 2  # first deadlock + retry success
    # SQL is the odds DELETE + INSERT path
    assert conn.cur.last_sql is not None
    assert "INSERT INTO match_odds" in conn.cur.last_sql


def test_insert_graph_points_retries_on_deadlock():
    calls = {"n": 0}

    class FlakyInserter(_Inserter):
        def _insert_graph_points_once(self, match_id, data):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _MySQLError(1213, "Deadlock found when trying to get lock")
            return super()._insert_graph_points_once(match_id, data)

    conn = _FakeConn()
    ins = FlakyInserter(conn)
    body = {"graphPoints": [{"minute": 10, "value": 12}, {"minute": 60, "value": -5}]}
    n = ins.insert_graph_points(999, body)
    assert n == 2
    assert calls["n"] == 2


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v", "-s"]))