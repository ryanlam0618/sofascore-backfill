#!/usr/bin/env python3
"""test_team_attribution_fix.py — loader team_id attribution fix (L1/L2/L3).

Task     : loader team_id attribution fix (Duncan → Forge, 2026-10-03;
           Kris-approved 15:10 HKT). Code-only: no DB writes, no fetch.
Root cause: data/rootcause_teamid_20261002.md
Helper   : team_attribution.py (resolve_side_team_id / resolve_incident_team_id /
           validate_team_id) — imported by every loader, no inline copies.

Verifies, against the REAL fixture match_14023966_full.json
(Sunderland 41 vs Chelsea 38):

  1. L1 lineups      — every row of both sides resolves to the event
                       home/away id; the fixture's player-level teamId values
                       (home: 41/1658/36360/241802, away: 38/2829/36539) are
                       NEVER the source. Note 41/38 are also *legit* event ids
                       in this fixture (players[0] happens to collide), so the
                       source-independence is proven by planting every garbage
                       value AND swapping the event ids to synthetic values —
                       resolution must follow the event ids alone.
  2. L2 incidents    — team_id derives from inc["isHome"] → event home/away.
                       Side-less system rows (period / injuryTime) are skipped
                       (reference behaviour, backfill_runner.py:1655).
  3. L2 foreign rows — a synthetic nested team.id belonging to a THIRD team is
                       rejected + counted: the real gen4 loader loop
                       (gen4_phaseB_cdf_gapfill.upsert_incidents) skips it,
                       never writes it, and logs the rejected count + sample
                       ids.
  4. L3 player stats — backfill_runner.insert_lineups → insert_player_stats
                       chain writes team_id from the event payload side id
                       for both match_lineups and match_player_stats rows.
  5. Behaviour       — the real gen4 upsert_lineups loop writes only 41/38
                       team ids on the fixture (40 rows, none of the garbage
                       player-level values).
  6. Production guard — the REAL DataInserter.insert_incidents (the
                       production path, backfill_runner.py) routes through
                       team_attribution on the fixture: side-derived ids,
                       synthetic foreign team.id row skipped + counted +
                       logged, side-less period row skipped, non-system
                       side-less row skipped + counted.

Offline — no network, no MySQL. The insert paths are exercised against a
recording fake cursor (same pattern as test_gen4_stage4d_insert_player_stats).

Run:
  .runner-venv/bin/python -m pytest tests/test_team_attribution_fix.py -v
"""
from __future__ import annotations

import copy
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from team_attribution import (  # noqa: E402
    resolve_incident_team_id,
    resolve_side_team_id,
    validate_team_id,
)

FIXTURE = os.path.join(ROOT, "match_14023966_full.json")

# Fixture ground truth (verified against the raw payload):
HOME_ID = 41    # Sunderland   (event.homeTeam.id)
AWAY_ID = 38    # Chelsea      (event.awayTeam.id)
# Player-level teamId values found in this fixture's lineups (the garbage the
# old loaders trusted): home side carries 4 distinct values, away side 3.
GARBAGE_HOME = {41, 1658, 36360, 241802}
GARBAGE_AWAY = {38, 2829, 36539}


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def payload():
    with open(FIXTURE) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def ev(payload):
    return payload["event"]["event"]


# ── recording fakes (same pattern as test_gen4_stage4d_insert_player_stats) ──

class _RecordingCursor:
    def __init__(self, calls):
        self._calls = calls

    def execute(self, sql, params=None):
        self._calls.append((" ".join(sql.split()), params))

    def executemany(self, sql, seq):
        self._calls.append((" ".join(sql.split()), list(seq)))

    def close(self):
        pass


class _FakeConn:
    def __init__(self):
        self.calls = []

    def cursor(self):
        return _RecordingCursor(self.calls)

    def commit(self):
        pass

    def rollback(self):
        pass


def _inserts_like(calls, prefix):
    return [c for c in calls if c[0].startswith(prefix)]


# ── 1. L1: lineups resolve to event home/away ids ─────────────────────────────

def test_fixture_ground_truth(payload, ev):
    """The fixture really is Sunderland 41 vs Chelsea 38 (guards the test)."""
    assert resolve_side_team_id(ev, True) == HOME_ID
    assert resolve_side_team_id(ev, False) == AWAY_ID
    home_players = payload["lineups"]["home"]["players"]
    away_players = payload["lineups"]["away"]["players"]
    assert len(home_players) == 20 and len(away_players) == 20
    assert {p.get("teamId") for p in home_players} == GARBAGE_HOME
    assert {p.get("teamId") for p in away_players} == GARBAGE_AWAY
    # lineups side payloads have NO nested team object — the old code's
    # first probe (side.team.id) was already dead.
    assert payload["lineups"]["home"].get("team") is None
    assert payload["lineups"]["away"].get("team") is None


def test_every_lineup_row_resolves_to_event_team_ids(payload, ev):
    """Every player row of both sides resolves to the event home/away id."""
    resolved = []
    for is_home, side_key in ((1, "home"), (0, "away")):
        players = payload["lineups"][side_key]["players"]
        for _p in players:
            team_id = resolve_side_team_id(ev, is_home)
            resolved.append(team_id)
    assert len(resolved) == 40
    # every home row -> 41, every away row -> 38
    assert all(t == HOME_ID for t in resolved[:20])
    assert all(t == AWAY_ID for t in resolved[20:])
    # resolved values are exactly the event's two team ids — none of the
    # non-colliding garbage values can ever appear
    assert set(resolved) == {HOME_ID, AWAY_ID}
    assert set(resolved).isdisjoint(GARBAGE_HOME - {HOME_ID})
    assert set(resolved).isdisjoint(GARBAGE_AWAY - {AWAY_ID})


def test_player_level_teamid_is_never_the_source(payload):
    """Plant every garbage teamId AND synthetic event ids — resolution must
    follow the event ids alone (proves 41/1658/36360/241802 are never sourced
    from player level, even where the value coincides with the real home id).
    """
    for garbage in sorted(GARBAGE_HOME | GARBAGE_AWAY):
        mutated = copy.deepcopy(payload)
        for side_key in ("home", "away"):
            for p in mutated["lineups"][side_key]["players"]:
                p["teamId"] = garbage          # old code read players[0] only
        m_ev = mutated["event"]["event"]
        m_ev["homeTeam"]["id"] = 424242        # synthetic — no collision possible
        m_ev["awayTeam"]["id"] = 434343
        assert resolve_side_team_id(m_ev, True) == 424242
        assert resolve_side_team_id(m_ev, False) == 434343


def test_side_team_id_none_when_event_lacks_side(ev):
    """Malformed event → None → loader must skip the side (NOT NULL column)."""
    assert resolve_side_team_id({}, True) is None
    assert resolve_side_team_id({"homeTeam": {}}, True) is None
    assert resolve_side_team_id({"homeTeam": None}, True) is None


# ── 2. L2: incidents derive from isHome ───────────────────────────────────────

def test_incidents_derive_from_is_home(payload, ev):
    attributable = skipped = 0
    for inc in payload["incidents"]["incidents"]:
        team_id, reason = resolve_incident_team_id(inc, ev)
        if inc.get("isHome") is True:
            assert team_id == HOME_ID and reason is None
            attributable += 1
        elif inc.get("isHome") is False:
            assert team_id == AWAY_ID and reason is None
            attributable += 1
        else:
            # period / injuryTime system rows: no team semantics → skipped
            # (reference behaviour, backfill_runner.py:1655; the 2026-10-02
            # DB repair exposes them with team_id_fixed = NULL via view).
            assert team_id is None
            assert reason == "no_side_signal"
            assert inc.get("incidentType") in ("period", "injuryTime")
            skipped += 1
    assert attributable == 20 and skipped == 4


def test_incident_payload_team_id_used_only_as_crosscheck(ev):
    """A payload team.id matching the match's teams is accepted only as a
    cross-check; isHome remains authoritative."""
    inc = {"id": 1, "incidentType": "goal", "isHome": False,
           "team": {"id": AWAY_ID, "name": "Chelsea"}}
    assert resolve_incident_team_id(inc, ev) == (AWAY_ID, None)
    # isHome wins even when the payload team.id disagrees (known-garbage field)
    inc2 = {"id": 2, "incidentType": "goal", "isHome": True,
             "team": {"id": AWAY_ID}}
    assert resolve_incident_team_id(inc2, ev) == (HOME_ID, None)


# ── 3. L2: synthetic foreign team.id rows are rejected + counted ──────────────

FOREIGN_ROWS = [
    {"id": 900001, "incidentType": "goal", "isHome": True, "time": 30,
     "player": {"id": 111}, "team": {"id": 241802, "name": "RB Omiya Ardija"}},
    {"id": 900002, "incidentType": "card", "isHome": False, "time": 60,
     "player": {"id": 222}, "team": {"id": 999, "name": "Mystery FC"}},
]


def test_synthetic_foreign_team_row_rejected_by_helper(ev):
    for inc in FOREIGN_ROWS:
        team_id, reason = resolve_incident_team_id(inc, ev)
        assert team_id is None
        assert reason == "foreign_team_id"
        foreign_id = inc["team"]["id"]
        assert validate_team_id(foreign_id, HOME_ID, AWAY_ID) == (False, "foreign_team_id")


def test_synthetic_foreign_team_row_rejected_and_counted_by_real_loader(
        payload, ev, capsys):
    """Run the REAL gen4 upsert_incidents loop (offline fake conn) on the
    fixture + 2 synthetic foreign rows: foreign rows skipped + counted,
    never written; the rejected count + sample ids are logged."""
    import gen4_phaseB_cdf_gapfill as cdf

    data = copy.deepcopy(payload["incidents"])
    data["incidents"] = list(data["incidents"]) + copy.deepcopy(FOREIGN_ROWS)
    conn = _FakeConn()
    n = cdf.upsert_incidents(conn, match_id=14023966, data=data, event_data=ev)

    written = _inserts_like(conn.calls, "INSERT INTO match_incidents")
    # 20 attributable fixture rows only — 4 side-less rows skipped,
    # 2 foreign rows rejected.
    assert n == 20
    assert len(written) == 20
    for sql, params in written:
        # INSERT INTO match_incidents (match_id, incident_id, team_id, player_id,
        #                              incident_type, minute, added_time, period,
        #                              is_home, goal_type, card_type, ...)
        team_id = params[2]
        is_home = params[8]
        assert team_id in (HOME_ID, AWAY_ID)
        assert (team_id == HOME_ID) == (is_home == 1)
        assert team_id not in {241802, 999}
    out = capsys.readouterr().out
    assert "rejected 2 foreign-team incident rows" in out
    assert "900001" in out and "900002" in out


def test_real_loader_lineups_write_only_event_team_ids(payload, ev):
    """Run the REAL gen4 upsert_lineups loop (offline fake conn) on the
    fixture: all 40 match_lineups rows carry 41/38, none of the garbage
    player-level teamId values ever reach the DB."""
    import gen4_phaseB_cdf_gapfill as cdf

    data = copy.deepcopy(payload["lineups"])
    conn = _FakeConn()
    n = cdf.upsert_lineups(conn, match_id=14023966, data=data, event_data=ev)

    assert n == 40
    rows = _inserts_like(conn.calls, "INSERT INTO match_lineups")
    assert len(rows) == 40
    team_ids = []
    for sql, params in rows:
        team_id, is_home = params[1], params[3]
        assert (team_id == HOME_ID) == (is_home == 1)
        team_ids.append(team_id)
    assert sorted(team_ids) == [AWAY_ID] * 20 + [HOME_ID] * 20
    assert set(team_ids).isdisjoint(GARBAGE_HOME - {HOME_ID})
    assert set(team_ids).isdisjoint(GARBAGE_AWAY - {AWAY_ID})
    # teams/players FK seeding also uses only the event ids (both the
    # inline execute and _seed_parents executemany shapes)
    for sql, params in _inserts_like(conn.calls, "INSERT IGNORE INTO teams"):
        rows = params if isinstance(params, list) else [params]
        for row in rows:
            assert row[0] in (HOME_ID, AWAY_ID)


# ── 4. L3: match_player_stats inherits the correct side team id ───────────────

def test_backfill_runner_player_stats_get_event_team_ids(payload, ev):
    """backfill_runner.insert_lineups → insert_player_stats chain: both
    match_lineups and match_player_stats rows carry the event side id."""
    import backfill_runner

    data = copy.deepcopy(payload["lineups"])
    data.setdefault("homeTeam", ev.get("homeTeam", {}))
    data.setdefault("awayTeam", ev.get("awayTeam", {}))
    conn = _FakeConn()
    inserter = backfill_runner.DataInserter(conn)
    n = inserter.insert_lineups(14023966, data)

    assert n == 40
    lineup_rows = _inserts_like(conn.calls, "INSERT INTO match_lineups")
    stats_rows = _inserts_like(conn.calls, "INSERT INTO match_player_stats")
    assert len(lineup_rows) == 40
    assert len(stats_rows) == 40
    for sql, params in lineup_rows:
        assert (params[1] == HOME_ID) == (params[3] == 1)
        assert params[1] in (HOME_ID, AWAY_ID)
    for sql, params in stats_rows:
        # values dict order: match_id, team_id, player_id, is_home, ...
        assert (params[1] == HOME_ID) == (params[3] == 1)
        assert params[1] in (HOME_ID, AWAY_ID)


# ── 6. Production path: DataInserter.insert_incidents foreign-team guard ──────────────────────────────

FOREIGN_INCIDENT_ROW = {
    "id": 900001, "incidentType": "goal", "isHome": True, "time": 30,
    "player": {"id": 111}, "team": {"id": 241802, "name": "RB Omima Ardija"},
}
SIDELESS_PERIOD_ROW = {
    "id": 900004, "incidentType": "period", "text": "HT",
    "time": 45, "addedTime": 999,
}
SIDELESS_NON_SYSTEM_ROW = {
    "id": 900003, "incidentType": "varDecision", "time": 50,
}


def test_backfill_runner_insert_incidents_guard(payload, ev, capsys):
    """Production path: DataInserter.insert_incidents routes team resolution
    through team_attribution — (a) real fixture rows get side-derived ids,
    (b) a synthetic third-team team.id row is skipped + counted + logged,
    (c) a side-less period row is skipped (documented system-row decision),
    and a non-system side-less row is skipped + counted."""
    import backfill_runner

    data = copy.deepcopy(payload["incidents"])
    data["incidents"] = (list(data["incidents"])
                        + [copy.deepcopy(FOREIGN_INCIDENT_ROW),
                           copy.deepcopy(SIDELESS_PERIOD_ROW),
                           copy.deepcopy(SIDELESS_NON_SYSTEM_ROW)])
    # Mirror the real call path: _upsert_endpoint / replay_bundle inject the
    # event's homeTeam/awayTeam into the incidents body before insert.
    data["homeTeam"] = ev["homeTeam"]
    data["awayTeam"] = ev["awayTeam"]

    conn = _FakeConn()
    inserter = backfill_runner.DataInserter(conn)
    n = inserter.insert_incidents(14023966, data)

    written = _inserts_like(conn.calls, "INSERT INTO match_incidents")
    # (a) 20 attributable fixture rows — fixture period/injuryTime rows have
    # no incident id and are skipped by the early guard; the synthetic
    # period row (id present) is skipped at team resolution.
    assert n == 20
    assert len(written) == 20
    for sql, params in written:
        # (incident_id, match_id, team_id, player_id, related_player_id,
        #  assist_player_id, incident_type, minute, added_time, period,
        #  is_home, goal_type, card_type, incident_text, reason)
        team_id, is_home = params[2], params[10]
        assert team_id in (HOME_ID, AWAY_ID)
        assert (team_id == HOME_ID) == (is_home == 1)
        assert params[0] not in (900001, 900003, 900004)
    # (b) + (c): guard counters + sample-id logging
    out = capsys.readouterr().out
    assert "rejected 1 foreign-team incident rows" in out
    assert "900001" in out
    # the non-system side-less row is counted; the period row is not
    assert "skipped 1 rows without a side signal" in out


# ── 5. helper gate ────────────────────────────────────────────────────────────

def test_validate_team_id_gate():
    assert validate_team_id(HOME_ID, HOME_ID, AWAY_ID) == (True, None)
    assert validate_team_id(AWAY_ID, HOME_ID, AWAY_ID) == (True, None)
    assert validate_team_id(None, HOME_ID, AWAY_ID) == (False, "missing_team_id")
    assert validate_team_id(999, HOME_ID, AWAY_ID) == (False, "foreign_team_id")
    assert validate_team_id(241802, HOME_ID, AWAY_ID) == (False, "foreign_team_id")
