#!/usr/bin/env python3
"""gen4_lineups_replay.py — Lineups deep-stats replay (2026-09-01, Kris A1 approval).

Re-fetch /lineups for the 1000 Stage-2 events (25/26 seasons), parse the full
player `statistics` object, and persist ~30 core deep-stat columns into the
newly-ALTERed `match_lineups` table.

Method: gen4 fp v2 + 21-IP good pool + chrome124 curl_cffi (same as Stage 2).
Idempotent: ON DUPLICATE KEY UPDATE on the (match_id, team_id, player_id) unique key.

Safety:
 - appdb_rw only; no DROP/TRUNCATE.
 - Bounded retry (2 tries, next IP) per endpoint.
 - Early-stop: 10 consecutive failures -> abort.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

POOL_FILE = ROOT / "data/proxy_pools/good_21.txt"
EV = Path("/tmp/gen4_stage2/evidence.jsonl")
OUT = ROOT / "data/gen4_lineups_replay.json"

TIMEOUT = 20

# ── 30 core deep-stat columns (column -> JSON key in /lineups statistics object) ──
DEEP_STAT_MAP = {
    "goals": "goals",
    "total_shots": "totalShots",
    "on_target_scoring_attempt": "onTargetScoringAttempt",
    "expected_goals": "expectedGoals",
    "expected_goals_on_target": "expectedGoalsOnTarget",
    "shot_value_normalized": "shotValueNormalized",
    "total_pass": "totalPass",
    "accurate_pass": "accuratePass",
    "key_pass": "keyPass",
    "total_cross": "totalCross",
    "accurate_cross": "accurateCross",
    "goal_assist": "goalAssist",
    "expected_assists": "expectedAssists",
    "big_chance_created": "bigChanceCreated",
    "pass_value_normalized": "passValueNormalized",
    "total_tackle": "totalTackle",
    "won_tackle": "wonTackle",
    "interception_won": "interceptionWon",
    "total_clearance": "totalClearance",
    "ball_recovery": "ballRecovery",
    "defensive_value_normalized": "defensiveValueNormalized",
    "duel_won": "duelWon",
    "aerial_won": "aerialWon",
    "dribble_value_normalized": "dribbleValueNormalized",
    "top_speed": "topSpeed",
    "number_of_sprints": "numberOfSprints",
    "total_ball_carries_distance": "totalBallCarriesDistance",
    "progressive_ball_carries_count": "progressiveBallCarriesCount",
    "rating_versions": "ratingVersions",   # JSON column, serialized below
    "statistics_type": "statisticsType",
}

pool = []
for l in POOL_FILE.read_text().splitlines():
    if l.strip() and not l.startswith("#"):
        ip, port, user, pw = l.strip().split(":", 3)
        pool.append({"ip": ip, "port": port, "user": user, "pw": pw})
assert len(pool) == 21, f"expected 21 IPs, got {len(pool)}"

_ip_counter = {"n": 0}


def next_ip():
    m = pool[_ip_counter["n"] % len(pool)]
    _ip_counter["n"] += 1
    return m


from curl_cffi import requests as cffi_requests


def insert_match_lineups_deep(conn, match_id, data, event):
    """Persist core lineups + 30 deep-stat columns (idempotent UPSERT)."""
    data.setdefault("homeTeam", event.get("homeTeam", {}))
    data.setdefault("awayTeam", event.get("awayTeam", {}))
    # Authoritative team IDs come from the EVENT payload (matches table uses these),
    # NOT from lineups: lineups `side.team.id` is None and player `teamId` is garbage
    # (points to unrelated clubs). BUGFIX vs original Stage 2 which mis-resolved team_id.
    home_team_id = (event.get("homeTeam", {}) or {}).get("id")
    away_team_id = (event.get("awayTeam", {}) or {}).get("id")
    cur = conn.cursor()
    n = 0
    for is_home, key in ((1, "home"), (0, "away")):
        side = data.get(key, {}) or {}
        team_id = home_team_id if is_home else away_team_id
        players = side.get("players", []) or []
        if team_id is None:
            continue
        for p in players:
            pl = p.get("player", {})
            if not pl.get("id") or team_id is None:
                continue
            stat = p.get("statistics") or {}
            _posmap = {"G": "GK", "D": "DEF", "M": "MID", "F": "FWD"}
            _pc = _posmap.get((pl.get("position") or "").upper()[:1], "MID")
            # NOTE: skip ensure_team/ensure_player — they already exist from Stage 2,
            # and each does an individual commit() (~40+ per event => massive bottleneck).
            # The FK constraints (team_id, player_id) are satisfied by pre-existing rows.

            # Build value dict: core + deep stats
            values = {
                "match_id": match_id,
                "team_id": team_id,
                "player_id": pl["id"],
                "is_home": is_home,
                "is_starter": 1 if p.get("substitute") is False else 0,
                "jersey_number": p.get("jerseyNumber"),
                "position": pl.get("position") or p.get("position") or "",
                "position_category": _pc,
                "is_captain": 1 if p.get("captain") else 0,
                "minutes_played": stat.get("minutesPlayed"),
                "rating": stat.get("rating"),
            }
            for col, jk in DEEP_STAT_MAP.items():
                values[col] = stat.get(jk)
            # statisticsType is a dict {'sportSlug':..., 'statisticsType':...} -> extract inner string
            st = values.get("statistics_type")
            if isinstance(st, dict):
                values["statistics_type"] = st.get("statisticsType")
            # rating_versions is JSON -> serialize
            rv = values.get("rating_versions")
            if rv is not None:
                values["rating_versions"] = json.dumps(rv, separators=(",", ":"))

            cols = list(values.keys())
            ph = ", ".join(["%s"] * len(cols))
            upd = ", ".join(f"{c}=VALUES({c})" for c in cols
                            if c not in ("match_id", "player_id", "team_id"))
            cur.execute(
                f"INSERT INTO match_lineups ({', '.join(cols)}) VALUES ({ph}) "
                f"ON DUPLICATE KEY UPDATE {upd}",
                [values[c] for c in cols],
            )
            n += 1
    conn.commit()
    return n


def api_get(path: str, max_tries: int = 2):
    last = None
    for _ in range(max_tries):
        m = next_ip()
        proxy = "http://" + m["user"] + ":" + m["pw"] + "@" + m["ip"] + ":" + m["port"]
        try:
            r = cffi_requests.get("https://api.sofascore.com/api/v1" + path,
                                  impersonate="chrome124",
                                  proxies={"http": proxy, "https": proxy},
                                  timeout=TIMEOUT)
            if r.status_code == 200:
                return r.json(), m["ip"], None
            last = f"http_{r.status_code}"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"[:150]
    return None, None, last


def load_event_ids():
    """Extract event_id list from evidence.jsonl (1000 rows)."""
    ids = []
    for line in EV.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        eid = rec.get("event_id")
        if eid:
            ids.append(eid)
    return ids


def main() -> int:
    env = dict(line.split("=", 1) for line in Path(ROOT / ".env").read_text().splitlines()
               if "=" in line and not line.strip().startswith("#"))
    import mysql.connector
    conn = mysql.connector.connect(host="127.0.0.1", port=3306, user="appdb_rw",
                                   password=env["MYSQL_PASSWORD"], database="appdb",
                                   autocommit=False)

    event_ids = load_event_ids()
    print(f"[replay] loaded {len(event_ids)} event_ids from evidence.jsonl", flush=True)

    # Preload team context from DB (home/away team id + name) so we DON'T re-fetch /event.
    # matches.match_id == SofaScore event id.
    cur = conn.cursor()
    cur.execute("""
        SELECT m.match_id, m.home_team_id, m.away_team_id,
               th.name AS home_name, th.short_name AS home_short, th.country_code AS home_cc,
               ta.name AS away_name, ta.short_name AS away_short, ta.country_code AS away_cc
        FROM matches m
        LEFT JOIN teams th ON th.team_id = m.home_team_id
        LEFT JOIN teams ta ON ta.team_id = m.away_team_id
    """)
    team_ctx = {}
    for row in cur.fetchall():
        (mid, hid, aid, hn, hs, hcc, an, asn, acc) = row
        team_ctx[mid] = {
            "homeTeam": {"id": hid, "name": hn or "", "shortName": hs or "", "countryCode": hcc},
            "awayTeam": {"id": aid, "name": an or "", "shortName": asn or "", "countryCode": acc},
        }
    print(f"[replay] preloaded team ctx for {len(team_ctx)} matches", flush=True)

    total_ok = total_fail = 0
    fail_streak = 0
    players_upserted = 0
    per_ip = {}
    errors = []

    for eid in event_ids:
        if fail_streak >= 10:
            print("[replay] ABORT: 10 consecutive failures", flush=True)
            break
        # fetch ONLY lineups (team context from DB, not /event)
        data, ipx, err_lineups = api_get(f"/event/{eid}/lineups")
        if data is None:
            total_fail += 1
            fail_streak += 1
            errors.append({"event_id": eid, "err": f"lineups: {err_lineups}"})
            continue

        event = team_ctx.get(eid)
        if not event or not event["homeTeam"]["id"] or not event["awayTeam"]["id"]:
            total_fail += 1
            fail_streak += 1
            errors.append({"event_id": eid, "err": "no team ctx in matches"})
            continue

        match_id = eid

        try:
            n = insert_match_lineups_deep(conn, match_id, data, event)
            players_upserted += n
            total_ok += 1
            fail_streak = 0
            d = per_ip.setdefault(ipx, {"ok": 0, "fail": 0})
            d["ok"] += 1
        except Exception as e:
            conn.rollback()
            total_fail += 1
            fail_streak += 1
            errors.append({"event_id": eid, "err": f"insert: {type(e).__name__}: {e}"[:200]})

        if total_ok % 50 == 0:
            print(f"[replay] progress: ok={total_ok} fail={total_fail} players={players_upserted}", flush=True)

    result = {
        "schema_version": "1.0",
        "task_id": "lineups-deep-stats-persist-20260901",
        "total_events": len(event_ids),
        "ok": total_ok,
        "fail": total_fail,
        "players_upserted": players_upserted,
        "aborted": fail_streak >= 10,
        "per_ip": {k: v for k, v in per_ip.items()},
        "errors": errors[:50],
    }
    OUT.write_text(json.dumps(result, indent=2))
    print(f"\n[replay] DONE ok={total_ok} fail={total_fail} players={players_upserted}", flush=True)
    print(f"[replay] artifact: {OUT}", flush=True)
    conn.close()
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())