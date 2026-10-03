"""team_attribution.py — authoritative team_id resolution for every loader.

Root cause (data/rootcause_teamid_20261002.md, Duncan 2026-10-02):
  * L1 lineups:            team_id came from player-level ``players[0]["teamId"]``
                           — a garbage value pointing at unrelated clubs
                           (fixture match_14023966_full.json: one side's 20
                           players carry 4 different teamId values, one of them
                           a J2 club on an EPL player; 73.8% of DB rows wrong).
  * L2 incidents:          team_id fell back blindly to the HOME team id,
                           ignoring the trustworthy ``inc["isHome"]`` side
                           flag (≈100% of 18/19–24/25 rows wrong), and rows
                           carrying a nested third-team ``team.id`` were
                           written anyway (10,486 cross-match contamination
                           rows, deleted DB-side 2026-10-02).
  * L3 match_player_stats: inherited the L1 bug through the same side
                           context passed down from the lineups loop
                           (43% wrong historically).

RULE (Kris-approved 2026-10-03 — applies to ALL loaders):
  team_id MUST be derived from the EVENT payload's homeTeam/awayTeam id plus
  the trusted side signal (``is_home`` / ``inc["isHome"]``) — never from
  player-level ``teamId``, never from a blind home-team fallback — and a
  loader MUST refuse to write a team_id that is not one of the match's two
  teams.

Canonical lineups pattern:    gen4_lineups_replay.py:85-100
Reference incidents impl:     backfill_runner.py:1655 (``insert_incidents`` —
                              isHome-derived; side-less system rows skipped)
Per the 2026-10-02 DB repair decision, period/injuryTime marker rows have NO
team semantics; the reference implementation skips them, and this loader-side
fix keeps that behavior (they are inert noise in the DB and are exposed with
team_id_fixed = NULL by data/views_db_fix_20261002.sql).

All loaders import these helpers — no inline copies left behind.

Offline module: no network, no DB, no side effects — safe to unit-test.
"""
from __future__ import annotations

# Reject reasons returned by resolve_* / validate_team_id (None means OK).
REASON_MISSING_TEAM_ID = "missing_team_id"   # no team id could be resolved
REASON_FOREIGN_TEAM_ID = "foreign_team_id"   # id belongs to neither team
REASON_NO_SIDE_SIGNAL = "no_side_signal"     # no isHome flag / usable team.id


def validate_team_id(team_id, home_id, away_id):
    """Gate: is ``team_id`` one of this match's two teams?

    Returns ``(ok, reason)``:
      ``(True,  None)``               — team_id is home_id or away_id
      ``(False, "missing_team_id")``  — team_id is None
      ``(False, "foreign_team_id")``  — team_id belongs to neither team

    Loaders MUST NOT write a row when ok is False.
    """
    if team_id is None:
        return False, REASON_MISSING_TEAM_ID
    if team_id not in (home_id, away_id):
        return False, REASON_FOREIGN_TEAM_ID
    return True, None


def resolve_side_team_id(event, is_home):
    """Authoritative team id for a lineup / player-stats side (L1, L3).

    Reads ONLY the event payload's homeTeam/awayTeam id — the same ids the
    ``matches`` table stores. Never the player-level ``teamId`` (garbage),
    never ``side["team"]`` (None in real payloads), never a blind fallback.

    Args:
      event:   the event payload dict (``event_data``); must carry
               ``homeTeam``/``awayTeam`` objects with ``id``.
      is_home: side flag — truthy → home side, falsy → away side.

    Returns the side's team id, or None when the event payload lacks it
    (caller must skip the side/row — schema requires team_id NOT NULL).
    """
    side = (event.get("homeTeam") if is_home else event.get("awayTeam")) or {}
    if not isinstance(side, dict):
        return None
    return side.get("id")


def resolve_incident_team_id(inc, event):
    """Authoritative team id for an incident row (L2).

    The side is derived from the trustworthy ``inc["isHome"]`` flag → the
    event payload's home/away id. A payload-supplied nested
    ``inc["team"]["id"]`` is treated ONLY as a cross-check (never as the
    primary source): if it names a THIRD team the row is rejected and must
    be SKIPPED + counted in the caller's ``rejected_foreign_team`` counter
    (log sample ids).

    Returns ``(team_id, reject_reason)``:
      ``(team_id, None)``             — write the row with this team_id
      ``(None, "foreign_team_id")``   — SKIP + count: payload team.id is a
                                        third team (cross-match contamination)
      ``(None, "no_side_signal")``    — SKIP: no isHome flag and no payload
                                        team.id (period / injuryTime system
                                        rows land here; the reference
                                        implementation skips them too)
      ``(None, "missing_team_id")``    — SKIP: side resolved but the event
                                        payload lacks that side's team id
    """
    home_id = resolve_side_team_id(event, True)
    away_id = resolve_side_team_id(event, False)

    raw_team = inc.get("team")
    payload_team_id = raw_team.get("id") if isinstance(raw_team, dict) else None
    if payload_team_id is not None and payload_team_id not in (home_id, away_id):
        return None, REASON_FOREIGN_TEAM_ID

    is_home = inc.get("isHome")
    if is_home:
        team_id = home_id
    elif is_home is not None:
        team_id = away_id
    else:
        # No side flag: fall back to the payload team id — reachable only
        # when it already passed the cross-check above, i.e. it names one
        # of the match's two teams.
        if payload_team_id is None:
            return None, REASON_NO_SIDE_SIGNAL
        team_id = payload_team_id

    ok, reason = validate_team_id(team_id, home_id, away_id)
    if not ok:
        return None, reason
    return team_id, None
