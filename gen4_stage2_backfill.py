#!/usr/bin/env python3
"""gen4_stage2_backfill.py — Stage 2 (Kris Option C 2026-08-31 03:53 GMT+8).

Real backfill: 1000 events (200 x 5 competitions, 25/26 seasons), fetch
event/lineups/statistics/incidents via curl_cffi chrome124 + 21-IP good pool,
INSERT into MySQL appdb via backfill_runner.DataInserter (protected file NOT
modified — imported read-only).

Safety:
 - appdb_rw only (no DROP anywhere in this file).
 - Batch boundary: progress logged every 50 events; bounded retry (1 retry,
   next IP) per endpoint.
 - Early-stop: 10 consecutive event failures -> abort + report.
 - Per-IP drift log in artifact.
"""
from __future__ import annotations
from team_attribution import resolve_side_team_id

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
POOL_FILE = ROOT / "data/proxy_pools/good_21.txt"
EV = Path("/tmp/gen4_stage2/evidence.jsonl")
EV.parent.mkdir(exist_ok=True)
OUT = ROOT / "data/gen4_stage2_canary.json"

COMPS = [
    # name, ut_id, season_id, cat_id, year_label, target_events
    ("Premier League", 17, 76986, 1, "25/26", 200),
    ("La Liga", 8, 77559, 32, "25/26", 200),
    ("Bundesliga", 35, 77333, 30, "25/26", 200),
    ("Serie A", 23, 76457, 31, "25/26", 200),
    ("UCL", 7, 76953, 1465, "25/26", 200),
]
ENDPOINTS = [("lineups", "/lineups"), ("statistics", "/statistics"), ("incidents", "/incidents")]
TIMEOUT = 20
PER_EVENT_LIMIT = 1000

pool = []
for l in POOL_FILE.read_text().splitlines():
    if l.strip() and not l.startswith("#"):
        ip, port, user, pw = l.strip().split(":", 3)
        pool.append({"ip": ip, "port": port, "user": user, "pw": pw})
assert len(pool) == 21
_ip_counter = {"n": 0}


def next_ip():
    m = pool[_ip_counter["n"] % len(pool)]
    _ip_counter["n"] += 1
    return m


from curl_cffi import requests as cffi_requests


def insert_match_lineups(conn, match_id, data, event):
    """Minimal match_lineups writer for the rebuilt appdb schema.

    NOTE: DataInserter.insert_lineups can't be used as-is because the rebuilt
    schema dropped the ~90 legacy match_player_stats columns and scope lock
    forbids ALTER. Player-level deep stats (match_player_stats) are therefore
    out of Stage-2 scope; core lineup columns are inserted here.
    """
    data.setdefault("homeTeam", event.get("homeTeam", {}))
    data.setdefault("awayTeam", event.get("awayTeam", {}))
    cur = conn.cursor()
    n = 0
    for is_home, key in ((1, "home"), (0, "away")):
        side = data.get(key, {}) or {}
        players = side.get("players", []) or []
        # team_id attribution fix (2026-10-03, Kris-approved): derive from the
        # EVENT payload (same ids the matches table stores) + the trusted
        # is_home side flag — never from the garbage player-level `teamId`
        # (rootcause_teamid_20261002.md, team_attribution.py).
        team_id = resolve_side_team_id(event, is_home)
        for p in players:
            pl = p.get("player", {})
            if not pl.get("id") or team_id is None:
                continue
            stat = p.get("statistics") or {}
            _posmap = {"G": "GK", "D": "DEF", "M": "MID", "F": "FWD"}
            _pc = _posmap.get((pl.get("position") or "").upper()[:1], "MID")
            from backfill_runner import ensure_player as _ep, ensure_team as _et
            _et(conn, team_id, (side.get("team") or data["homeTeam"] if is_home else side.get("team") or data["awayTeam"]).get("name", ""), "", None,
                (side.get("team") or data["homeTeam"] if is_home else side.get("team") or data["awayTeam"]))
            _ep(conn, pl["id"], pl.get("name", ""), pl.get("shortName", ""),
                pl.get("position") or "", None, pl, team_id)
            cur.execute(
                """INSERT INTO match_lineups
                   (match_id, team_id, player_id, is_home, is_starter,
                    jersey_number, position, position_category, is_captain,
                    minutes_played, rating)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE
                     is_starter=VALUES(is_starter), jersey_number=VALUES(jersey_number),
                     position=VALUES(position), position_category=VALUES(position_category),
                     is_captain=VALUES(is_captain), minutes_played=VALUES(minutes_played),
                     rating=VALUES(rating)""",
                (match_id, team_id, pl["id"], is_home,
                 1 if p.get("substitute") is False else 0,
                 p.get("jerseyNumber"), pl.get("position") or p.get("position") or "",
                 _pc,
                 1 if p.get("captain") else 0,
                 stat.get("minutesPlayed"), stat.get("rating")),
            )
            n += 1
    conn.commit()
    return n


def api_get(path: str, max_tries: int = 2):
    """GET via rotating good IP; returns (json_dict, ip) or (None, ip, error)."""
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


def main() -> int:
    env = dict(line.split("=", 1) for line in Path(ROOT / ".env").read_text().splitlines()
               if "=" in line and not line.strip().startswith("#"))
    import mysql.connector
    conn = mysql.connector.connect(host="127.0.0.1", port=3306, user="appdb_rw",
                                   password=env["MYSQL_PASSWORD"], database="appdb",
                                   autocommit=False)
    from backfill_runner import DataInserter, ensure_competition, ensure_season
    inserter = DataInserter(conn)

    def rowcount(t):
        cur = conn.cursor(); cur.execute(f"SELECT COUNT(*) FROM {t}"); return cur.fetchone()[0]
    counts_before = {t: rowcount(t) for t in
                     ("matches", "match_incidents", "match_lineups", "match_statistics")}

    t_run = time.monotonic()
    total_ok = total_fail = 0
    fail_streak = 0
    aborted = None
    per_ip = {}
    event_log = []

    for name, ut, sid, cat, yr, target in COMPS:
        comp_id = ensure_competition(conn, name, cat, ut, "national" if cat != 1465 else "uefa", "")
        ensure_season(conn, sid, comp_id, yr)

        # sample event ids via events/last pages
        ids, page, seen = [], 0, set()
        while len(ids) < target and page < 40:
            data, ip, err = api_get(f"/unique-tournament/{ut}/season/{sid}/events/last/{page}")
            if not data:
                page += 1
                continue
            for e in data.get("events", []):
                eid = e.get("id")
                if eid and eid not in seen:
                    seen.add(eid); ids.append(eid)
            page += 1
        ids = ids[:target]
        print(f"[{name}] sampled {len(ids)} events (pages={page})", flush=True)

        for eid in ids:
            if total_ok + total_fail >= PER_EVENT_LIMIT:
                aborted = "cap_1000"; break
            if fail_streak >= 10:
                aborted = "early_stop_10xfail"; break
            rec = {"competition": name, "event_id": eid, "steps": {}, "ok": False}
            try:
                ev, ip0, err = api_get(f"/event/{eid}")
                if not ev:
                    raise RuntimeError(f"event fetch: {err}")
                event = ev.get("event", ev)
                match_id = inserter.insert_match(event, sid, comp_id)
                rec["steps"]["event"] = {"ok": True, "ip": ip0}
                for label, tail in ENDPOINTS:
                    data, ipx, err = api_get(f"/event/{eid}{tail}")
                    if data is None:
                        rec["steps"][label] = {"ok": False, "err": err}
                        dip = per_ip.setdefault(ip0, {"ok": 0, "fail": 0})
                        dip["fail"] += 1
                        continue
                    if label == "lineups":
                        insert_match_lineups(conn, match_id, data, event)
                    elif label == "statistics":
                        inserter.insert_statistics(match_id, data)
                    else:
                        inserter.insert_incidents(match_id, data)
                    rec["steps"][label] = {"ok": True, "ip": ipx}
                    dip = per_ip.setdefault(ipx or ip0, {"ok": 0, "fail": 0})
                    dip["ok"] += 1
                rec["ok"] = all(s.get("ok") for s in rec["steps"].values())
            except Exception as e:
                rec["error"] = f"{type(e).__name__}: {e}"[:200]
                try:
                    conn.rollback()
                except Exception:
                    pass
            if rec["ok"]:
                total_ok += 1; fail_streak = 0
            else:
                total_fail += 1; fail_streak += 1
            event_log.append(rec)
            with EV.open("a") as f:
                f.write(json.dumps(rec) + "\n")
            if (total_ok + total_fail) % 50 == 0:
                print(f"progress events ok={total_ok} fail={total_fail}", flush=True)
        if aborted:
            break

    conn.commit()
    counts_after = {t: rowcount(t) for t in counts_before}
    conn.close()

    total = total_ok + total_fail
    pct = round(100.0 * total_ok / total, 2) if total else 0
    verdict = ("ABORTED" if aborted else
               "PASS" if total and total_ok / total >= 0.98 else "FAIL")
    out = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stage": 2, "mode": "real_backfill_mysql_write",
        "events_ok": total_ok, "events_failed": total_fail,
        "success_rate_pct": pct, "aborted": aborted,
        "duration_s": round(time.monotonic() - t_run, 1),
        "row_counts_before": counts_before, "row_counts_after": counts_after,
        "per_ip": per_ip,
        "failed_events": [r["event_id"] for r in event_log if not r["ok"]][:50],
        "verdict": verdict, "evidence_jsonl": str(EV),
    }
    OUT.write_text(json.dumps(out, indent=2))
    print(json.dumps({"verdict": verdict, "ok": total_ok, "fail": total_fail,
                      "pct": pct, "aborted": aborted}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
