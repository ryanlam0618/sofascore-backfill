#!/usr/bin/env python3
"""
gen4_phaseB_19_20_remediation.py — scoped post-run FK remediation for 19/20.

Approved scope:
  * wait for the active 19/20 backfill run to finish;
  * collect failed / partially-failed event IDs from the 19/20 run log only;
  * seed missing teams/players with INSERT IGNORE only (additive; no UPDATE/DELETE);
  * re-run only those event IDs;
  * classify non-200 SofaScore endpoint responses as no_data instead of blind retry loops;
  * leave protected season scripts untouched (this is a new remediation script).

Outputs:
  data/gen4_phaseB_19-20_remediation_report.json
  data/gen4_phaseB_19-20_remediation_evidence.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import gen4_phaseB_19_20_backfill as bf  # noqa: E402

RUN_LOG = ROOT / "data/gen4_phaseB_19-20_run.log"
REPORT_OUT = ROOT / "data/gen4_phaseB_19-20_remediation_report.json"
EVIDENCE_OUT = ROOT / "data/gen4_phaseB_19-20_remediation_evidence.jsonl"

COMP_RE = re.compile(r"BACKFILL:\s+(.*?)\s+\(ut_id=(\d+)\)\s+season_id=(\d+)")
EVENT_RE = re.compile(r"\[\s*\d+/\s*\d+\]\s+Processing event\s+(\d+)")
EXPLICIT_FAIL_RE = re.compile(r"❌\s+Event\s+(\d+)\s+error:\s+(.*)")
UPSERT_WARN_RE = re.compile(r"⚠\s+(.*?)\s+event\s+(\d+)\s+(\w+)\s+upsert failed:\s+(.*)")
CHILD_ERROR_RE = re.compile(r"Error inserting\s+(incident|shot)\s+([^:]+):\s+(.*)")

COMP_BY_NAME = {c["name"]: c for c in bf.TARGET_COMPS}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_text(value: Any, max_len: int) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:max_len]


def wait_for_pid(pid: int, timeout_seconds: int = 0, poll_seconds: int = 30) -> bool:
    """Wait until pid exits. timeout_seconds=0 means no timeout."""
    start = time.time()
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            # Process exists but is owned by another user; keep waiting.
            pass
        if timeout_seconds and (time.time() - start) > timeout_seconds:
            return False
        time.sleep(poll_seconds)


def parse_failed_events(log_path: Path) -> Dict[int, Dict[str, Any]]:
    """
    Collect failed / partially-failed event IDs from run log.

    Sources:
      1) explicit event failures: "❌ Event <id> error: ..."
      2) endpoint upsert warnings: "⚠ <comp> event <id> <endpoint> upsert failed: ..."
      3) child-row insert errors grouped under the most recent "Processing event <id>" line.
    """
    entries: Dict[int, Dict[str, Any]] = {}
    current_comp: Optional[Dict[str, Any]] = None
    current_event_id: Optional[int] = None

    def ensure(event_id: int, comp: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        entry = entries.setdefault(event_id, {
            "event_id": event_id,
            "comp_name": None,
            "ut_id": None,
            "season_id": None,
            "comp_type": None,
            "reasons": [],
            "source_lines": [],
        })
        if comp:
            entry["comp_name"] = comp.get("name")
            entry["ut_id"] = comp.get("ut_id")
            entry["season_id"] = comp.get("season_id")
            entry["comp_type"] = comp.get("type")
        return entry

    if not log_path.exists():
        return entries

    with log_path.open("r", encoding="utf-8", errors="replace") as fh:
        for line_no, raw in enumerate(fh, 1):
            line = raw.rstrip("\n")

            m = COMP_RE.search(line)
            if m:
                comp_name = m.group(1).strip()
                comp = COMP_BY_NAME.get(comp_name, {})
                current_comp = {
                    "name": comp_name,
                    "ut_id": int(m.group(2)),
                    "season_id": int(m.group(3)),
                    "type": comp.get("type"),
                }
                current_event_id = None
                continue

            m = EVENT_RE.search(line)
            if m:
                current_event_id = int(m.group(1))
                ensure(current_event_id, current_comp)
                continue

            m = EXPLICIT_FAIL_RE.search(line)
            if m:
                event_id = int(m.group(1))
                entry = ensure(event_id, current_comp)
                entry["reasons"].append("explicit_event_error: %s" % m.group(2).strip())
                entry["source_lines"].append(line_no)
                current_event_id = event_id
                continue

            m = UPSERT_WARN_RE.search(line)
            if m:
                event_id = int(m.group(2))
                entry = ensure(event_id, current_comp)
                entry["reasons"].append("endpoint_upsert_warning %s: %s" % (m.group(3), m.group(4).strip()))
                entry["source_lines"].append(line_no)
                current_event_id = event_id
                continue

            m = CHILD_ERROR_RE.search(line)
            if m and current_event_id is not None:
                entry = ensure(current_event_id, current_comp)
                entry["reasons"].append("child_insert_error %s %s: %s" % (
                    m.group(1), m.group(2).strip(), m.group(3).strip()
                ))
                entry["source_lines"].append(line_no)

    # Drop entries that only mark enumeration/processing with no actual failure reason.
    return {eid: e for eid, e in entries.items() if e.get("reasons")}


def seed_team(cur, team: Optional[Dict[str, Any]]) -> Tuple[int, Optional[int]]:
    if not isinstance(team, dict):
        return 0, None
    team_id = team.get("id")
    if team_id is None:
        return 0, None
    cur.execute("""
        INSERT IGNORE INTO teams (team_id, name, short_name, slug)
        VALUES (%s, %s, %s, %s)
    """, (
        int(team_id),
        clean_text(team.get("name"), 100) or "Unknown",
        clean_text(team.get("shortName"), 50),
        clean_text(team.get("slug"), 100),
    ))
    return max(cur.rowcount, 0), int(team_id)


def seed_player(cur, player: Optional[Dict[str, Any]], current_team_id: Optional[int] = None) -> int:
    if not isinstance(player, dict):
        return 0
    player_id = player.get("id")
    if player_id is None:
        return 0
    cur.execute("""
        INSERT IGNORE INTO players (player_id, name, short_name, slug, position, current_team_id)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (
        int(player_id),
        clean_text(player.get("name"), 100) or "Unknown",
        clean_text(player.get("shortName"), 50),
        clean_text(player.get("slug"), 100),
        clean_text(player.get("position"), 50),
        current_team_id,
    ))
    return max(cur.rowcount, 0)


def extract_event_data(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict) and "event" in raw and "id" not in raw:
        return raw.get("event") or {}
    return raw if isinstance(raw, dict) else {}


def seed_from_bundle(conn, bundle: Dict[str, Any]) -> Dict[str, int]:
    """Seed only teams/players referenced by fetched payloads. INSERT IGNORE only."""
    cur = conn.cursor()
    counts = {"teams": 0, "players": 0}

    endpoints = bundle.get("endpoints", {})
    event_data = extract_event_data((endpoints.get("event") or {}).get("body") or {})

    # Match teams.
    team_ids: Set[int] = set()
    for key in ("homeTeam", "awayTeam"):
        inserted, team_id = seed_team(cur, event_data.get(key) or {})
        counts["teams"] += inserted
        if team_id is not None:
            team_ids.add(team_id)

    # Incident refs: team + involved player.
    incidents_body = (endpoints.get("incidents") or {}).get("body") or {}
    for inc in incidents_body.get("incidents", []) or []:
        inserted, team_id = seed_team(cur, inc.get("team") or {})
        counts["teams"] += inserted
        if team_id is not None:
            team_ids.add(team_id)
        counts["players"] += seed_player(cur, inc.get("player") or {}, team_id)

    # Lineup refs: team + full player list.
    lineups_body = (endpoints.get("lineups") or {}).get("body") or {}
    for side_key, fallback_key in (("home", "homeTeam"), ("away", "awayTeam")):
        side = lineups_body.get(side_key) or {}
        side_team = side.get("team") or event_data.get(fallback_key) or {}
        inserted, team_id = seed_team(cur, side_team)
        counts["teams"] += inserted
        if team_id is None:
            players = side.get("players") or []
            if players:
                team_id = players[0].get("teamId")
                if team_id is not None:
                    inserted, team_id = seed_team(cur, {"id": team_id, "name": "Unknown"})
                    counts["teams"] += inserted
        if team_id is not None:
            team_ids.add(team_id)
        for item in side.get("players", []) or []:
            counts["players"] += seed_player(cur, item.get("player") or {}, team_id)

    # Shotmap refs: shooting team/player + goalkeeper (goalkeeper team is not assumed).
    shotmap_body = (endpoints.get("shotmap") or {}).get("body") or {}
    home_id = ((event_data.get("homeTeam") or {}).get("id"))
    away_id = ((event_data.get("awayTeam") or {}).get("id"))
    for shot in shotmap_body.get("shotmap", []) or []:
        shot_team_id = (shot.get("team") or {}).get("id")
        if shot_team_id is None:
            shot_team_id = home_id if shot.get("isHome") else away_id
        if shot_team_id is not None:
            inserted, shot_team_id = seed_team(cur, (shot.get("team") or {"id": shot_team_id, "name": "Unknown"}))
            counts["teams"] += inserted
            if shot_team_id is not None:
                team_ids.add(shot_team_id)
        counts["players"] += seed_player(cur, shot.get("player") or {}, shot_team_id)
        counts["players"] += seed_player(cur, shot.get("goalkeeper") or {}, None)

    conn.commit()
    cur.close()
    return counts


def event_table_counts(conn, event_id: int) -> Dict[str, int]:
    tables = [
        "matches",
        "match_incidents",
        "match_lineups",
        "match_statistics",
        "match_shotmap",
        "match_odds",
    ]
    out: Dict[str, int] = {}
    cur = conn.cursor()
    for table in tables:
        cur.execute("SELECT COUNT(*) FROM %s WHERE match_id=%%s" % table, (event_id,))
        out[table] = int(cur.fetchone()[0])
    cur.close()
    return out


async def remediate_event(conn_factory, pool: bf.HealthProxyPool,
                          entry: Dict[str, Any], evidence_lines: List[bf.CallEvidence]) -> Dict[str, Any]:
    event_id = int(entry["event_id"])
    comp_name = entry.get("comp_name") or "unknown"
    ut_id = int(entry.get("ut_id") or 0)
    season_id = int(entry.get("season_id") or 0)

    result: Dict[str, Any] = {
        "event_id": event_id,
        "comp_name": comp_name,
        "ut_id": ut_id,
        "season_id": season_id,
        "reasons": entry.get("reasons", []),
        "endpoint_status": {},
        "seeded": {"teams": 0, "players": 0},
        "before_counts": {},
        "after_counts": {},
        "deltas": {},
        "errors": [],
        "classification": "failed",
    }

    bundle = await bf.fetch_event_bundle(pool, event_id, evidence_lines, comp_name, ut_id, season_id)
    endpoints = bundle.get("endpoints", {})
    for ep_name in bf.ALL_ENDPOINTS:
        ep = endpoints.get(ep_name) or {}
        result["endpoint_status"][ep_name] = ep.get("status")
        if ep.get("error"):
            result["errors"].append("%s fetch_error: %s" % (ep_name, ep.get("error")))

    event_status = (endpoints.get("event") or {}).get("status")
    event_body = extract_event_data((endpoints.get("event") or {}).get("body") or {})
    if event_status != 200 or not event_body:
        result["classification"] = "no_data"
        result["errors"].append("event endpoint unavailable status=%s" % event_status)
        return result

    conn = conn_factory()
    try:
        result["before_counts"] = event_table_counts(conn, event_id)
        result["seeded"] = seed_from_bundle(conn, bundle)

        match_id = bf.upsert_match(conn, event_body, season_id, ut_id)
        if int(match_id) != event_id:
            result["errors"].append("unexpected match_id returned=%s expected=%s" % (match_id, event_id))

        for ep_name in ("incidents", "lineups", "statistics", "shotmap", "odds"):
            ep = endpoints.get(ep_name) or {}
            if ep.get("status") != 200:
                if ep.get("status") is not None:
                    result["errors"].append("%s no_data status=%s" % (ep_name, ep.get("status")))
                continue
            body = ep.get("body")
            if not body:
                result["errors"].append("%s empty_body" % ep_name)
                continue
            try:
                rows = bf.UPGRADE_FUNCS[ep_name](conn, event_id, body, event_body)
                result["deltas"][ep_name] = int(rows or 0)
            except Exception as exc:
                result["errors"].append("%s upsert_exception: %s" % (ep_name, exc))
                conn.rollback()

        result["after_counts"] = event_table_counts(conn, event_id)
    except Exception as exc:
        conn.rollback()
        result["errors"].append("event_exception: %s" % exc)
    finally:
        conn.close()

    after = result.get("after_counts") or {}
    if after.get("matches", 0) > 0 and not result["errors"]:
        result["classification"] = "recovered"
    elif after.get("matches", 0) > 0:
        result["classification"] = "partial"
    elif event_status != 200:
        result["classification"] = "no_data"
    else:
        result["classification"] = "failed"
    return result


async def run(args: argparse.Namespace) -> Dict[str, Any]:
    if args.wait_pid:
        print("[remediation] waiting for backfill pid=%s" % args.wait_pid)
        ok = wait_for_pid(args.wait_pid, timeout_seconds=args.wait_timeout_seconds, poll_seconds=args.poll_seconds)
        if not ok:
            raise RuntimeError("timed out waiting for pid=%s" % args.wait_pid)
        print("[remediation] backfill pid=%s exited" % args.wait_pid)

    entries = parse_failed_events(Path(args.run_log))
    event_entries = [entries[eid] for eid in sorted(entries)]
    print("[remediation] collected %s failed/partial events from %s" % (len(event_entries), args.run_log))

    missing_scope = [e for e in event_entries if not e.get("season_id") or not e.get("ut_id")]
    if missing_scope and not args.allow_unknown_scope:
        names = [str(e["event_id"]) for e in missing_scope[:20]]
        raise RuntimeError("failed to map %s events to comp/season from run log (first: %s)" % (len(missing_scope), ", ".join(names)))

    pool = bf.HealthProxyPool(bf.POOL_FILE)
    evidence_lines: List[bf.CallEvidence] = []
    results: List[Dict[str, Any]] = []

    for idx, entry in enumerate(event_entries, 1):
        print("[remediation] %s/%s event=%s comp=%s" % (
            idx, len(event_entries), entry["event_id"], entry.get("comp_name")
        ))
        res = await remediate_event(bf.get_mysql_conn, pool, entry, evidence_lines)
        results.append(res)
        print("[remediation]  -> %s statuses=%s seeded=%s errors=%s" % (
            res["classification"], res["endpoint_status"], res["seeded"], len(res["errors"])
        ))
        await asyncio.sleep(0.75)

    summary = defaultdict(int)
    for res in results:
        summary[res["classification"]] += 1

    report = {
        "timestamp": utc_now(),
        "run_log": str(args.run_log),
        "wait_pid": args.wait_pid,
        "events_collected": len(event_entries),
        "summary": dict(summary),
        "results": results,
        "constraints": {
            "insert_ignore_only_for_teams_players": True,
            "failed_event_ids_only": True,
            "non_200_classified_no_data": True,
            "protected_scripts_modified": False,
        },
    }

    REPORT_OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    with EVIDENCE_OUT.open("w", encoding="utf-8") as fh:
        for ev in evidence_lines:
            fh.write(ev.to_jsonl() + "\n")

    print("[remediation] report=%s" % REPORT_OUT)
    print("[remediation] evidence=%s lines=%s" % (EVIDENCE_OUT, len(evidence_lines)))
    print("[remediation] summary=%s" % dict(summary))
    return report


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Scoped 19/20 FK remediation after backfill run")
    p.add_argument("--run-log", default=str(RUN_LOG))
    p.add_argument("--wait-pid", type=int, default=0)
    p.add_argument("--wait-timeout-seconds", type=int, default=0, help="0 means wait forever")
    p.add_argument("--poll-seconds", type=int, default=30)
    p.add_argument("--allow-unknown-scope", action="store_true")
    p.add_argument("--parse-only", action="store_true", help="Only parse failed IDs, do not call network/DB")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if args.parse_only:
        entries = parse_failed_events(Path(args.run_log))
        payload = {
            "timestamp": utc_now(),
            "run_log": str(args.run_log),
            "count": len(entries),
            "events": [entries[eid] for eid in sorted(entries)],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
