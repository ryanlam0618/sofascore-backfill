#!/usr/bin/env python3
"""
gen4_phaseB_cup_retry_backfill.py — cup-retry tranche backfill (5 seasons x 5 cups).

COPY of gen4_phaseB_2021-25_backfill.py (2026-09-25; base file is protected —
zero changes to it, mtime untouched). Diffs vs base: TARGET_COMPS narrowed to
the 5 retry cups, PHASE_TAG="cup-retry", new output paths (no clobbering of
2021-25 artifacts), UECL-20/21 skip removed (UECL not in scope).

Base: gen4_phaseB_17_18_rerun.py (latest fixed, mtime 2026-09-18 01:27), which carries:
  - idempotent upserts (ON DUPLICATE KEY UPDATE) for matches/incidents/lineups/statistics/shotmap/odds
  - health-aware round-robin pick_ip (per-IP burst guard, MAX_CALLS_PER_IP_BURST=2)
  - consec_403 early-stop (CONSEC_403_LIMIT=5, only real 403/5xx)
  - 407 terminal halt
  - synthetic incident_id fallback (inc.get("id") or match_id*1000+idx)
  - API shape-change handling: /event/{id} unwrap of {"event":{...}}; shotmap team_id
    derived from isHome + event home/away (shots no longer carry team/)
  - fixed field names match_date/match_time/round_name
  - correct ENDPOINT_PATHS (no double /api/v1 prefix)

Season ids resolved per-season in-run via /unique-tournament/{ut}/seasons
(as-found labels; see season_ids JSON). Australia Cup 20/21 is a known genuine
gap (not on SofaScore) — recorded in-run, script continues remaining cells.
seasons rows seeded in-run (additive) before each comp-season for the
matches.season_id FK (this morning's 24-row seed already covers them; upsert
is idempotent).

POOL: 20-IP pool data/proxy_pools/good_20260917_v2.txt (19/20-proven, zero 403)
Seasons rows seeded in-run (additive ON DUPLICATE KEY UPDATE) per comp-season.

Writes: report JSON + evidence JSONL + checkpoint (on-exit watcher friendly).
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Set

import mysql.connector
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    cffi_requests = None
    HAS_CURL_CFFI = False

from ip_health_score import pick_ip, record_outcome, is_available, snapshot, reset

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

POOL_FILE = ROOT / "data/proxy_pools/good_20260917_v2.txt"
SEASON_IDS_FILE = ROOT / "data/gen4_phaseB_cup_retry_season_ids.json"
OUTPUT_REPORT = ROOT / "data/gen4_phaseB_cup_retry_report.json"
EVIDENCE_JSONL = ROOT / "data/gen4_phaseB_cup_retry_evidence.jsonl"
CHECKPOINT_FILE = ROOT / "data/phaseB_cup_retry_checkpoint.json"

SEASON_LABELS = ["20/21", "21/22", "22/23", "23/24", "24/25"]
PHASE_TAG = "cup-retry"

TIMEOUT = 20
MAX_CALLS_PER_IP_BURST = 2
CONSEC_403_LIMIT = 5
MAX_EVENTS_PER_COMP = 2000

API_BASE = "https://api.sofascore.com/api/v1"

# TLS/JA3 signature. 2026-09-26 Main-Agent disambiguation proved the 2026-09-25
# 403 wall is Chrome-family JA3 blocking (chrome124/chrome131 -> 403 on all pool
# IPs + direct; safari17_2_ios/firefox133 -> 200 on the same good_20260917_v2 IPs).
# Primary = safari17_2_ios. Mid-run fallback: if consec_403 early-stop fires under
# safari, swap IMPERSONATE to "firefox133" and relaunch (resume is idempotent via
# checkpoint).
IMPERSONATE = "safari17_2_ios"

# 5 retry cups (this morning's season_resolve_retry_20260925.json: 24 of 25
# grid cells resolved). Australia Cup 20/21 = known genuine gap (recorded
# in-run as season-not-resolved, script continues with remaining cells).
# Reference season_ids (in-run resolve should match these):
#   DFB Pokal 217:  20/21=29194 21/22=37289 22/23=41962 23/24=52284 24/25=61132
#   J.League Cup 101: 20/21=27196 21/22=35588 22/23=40504 23/24=48588 24/25=58111
#   Emperor's Cup 323: 20/21=32693 21/22=36765 22/23=41555 23/24=51378 24/25=60629
#   Australia Cup 1786: 21/22=37495 22/23=40658 23/24=52291 24/25=61199
#   Chinese FA Cup 882: 20/21=32681 21/22=37722 22/23=45113 23/24=51336 24/25=58767
TARGET_COMPS = [
    {"name": "DFB Pokal", "ut_id": 217, "type": "cup"},
    {"name": "J.League Cup", "ut_id": 101, "type": "cup"},
    {"name": "Emperor's Cup", "ut_id": 323, "type": "cup"},
    {"name": "Australia Cup", "ut_id": 1786, "type": "cup"},
    {"name": "Chinese FA Cup", "ut_id": 882, "type": "cup"},
]

ENDPOINT_PATHS = {
    "event": "/event/{event_id}",
    "incidents": "/event/{event_id}/incidents",
    "lineups": "/event/{event_id}/lineups",
    "statistics": "/event/{event_id}/statistics",
    "shotmap": "/event/{event_id}/shotmap",
    "odds": "/event/{event_id}/odds/1/all",
}

CORE_ENDPOINTS = ("event", "incidents", "lineups")
MID_ENDPOINTS = ("statistics", "shotmap")
HIGH_ENDPOINTS = ("odds",)
ALL_ENDPOINTS = CORE_ENDPOINTS + MID_ENDPOINTS + HIGH_ENDPOINTS

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class CallEvidence:
    comp_name: str
    ut_id: int
    season_id: int
    endpoint: str
    ip: Optional[str]
    status: Optional[int]
    event_count: int = 0
    error: Optional[str] = None
    latency_ms: Optional[int] = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_jsonl(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


@dataclass
class CompResult:
    comp_name: str
    ut_id: int
    season_id: int
    season_label: str
    comp_type: str
    matched_year: Optional[str] = None
    minute_null_incidents: int = 0
    total_events_found: int = 0
    events_processed: int = 0
    events_failed: int = 0
    events_skipped_existing: int = 0
    row_deltas: Dict[str, int] = field(default_factory=dict)
    calls_used: int = 0
    ip_usage: Dict[str, int] = field(default_factory=dict)
    consec_403_hit: bool = False
    error: Optional[str] = None


@dataclass
class BackfillReport:
    timestamp: str
    total_comps: int
    total_events: int
    total_calls: int
    comps: List[CompResult]
    ip_summary: Dict[str, Dict[str, int]]
    row_deltas_total: Dict[str, int]
    known_gaps: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# MySQL connection
# ---------------------------------------------------------------------------

def get_mysql_conn():
    return mysql.connector.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "root"),
        password=os.getenv('MYSQL_PASSWORD', ''),
        database=os.getenv("MYSQL_DATABASE", "appdb"),
        charset="utf8mb4",
        connect_timeout=10,
    )


# ---------------------------------------------------------------------------
# Proxy pool with health-aware selection (round-robin + burst guard)
# ---------------------------------------------------------------------------

class HealthProxyPool:
    def __init__(self, pool_file: Path):
        self.pool: List[Dict[str, str]] = []
        self.ip_burst_counts: Dict[str, int] = {}
        self._cursor: int = 0
        self._load(pool_file)
        reset()

    def _load(self, pool_file: Path):
        for line in pool_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(":", 3)
            if len(parts) == 4:
                ip, port, user, pw = parts
                self.pool.append({"ip": ip, "port": port, "user": user, "pw": pw})
                self.ip_burst_counts[ip] = 0
        print(f"[pool] Loaded {len(self.pool)} proxies from {pool_file}")

    def get_next_available(self) -> Optional[Dict[str, str]]:
        if sum(self.ip_burst_counts.values()) > len(self.pool) * MAX_CALLS_PER_IP_BURST:
            for ip in self.ip_burst_counts:
                if self.ip_burst_counts[ip] >= MAX_CALLS_PER_IP_BURST:
                    self.ip_burst_counts[ip] = max(0, self.ip_burst_counts[ip] - 1)
        n = len(self.pool)
        selected = None
        for _ in range(n):
            candidate = self.pool[self._cursor % n]
            self._cursor += 1
            if is_available(candidate["ip"]):
                selected = candidate
                break
        if selected is None:
            selected = pick_ip(self.pool)
        if selected:
            self.ip_burst_counts[selected["ip"]] += 1
        return selected


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

async def http_get(path: str, proxy: Dict[str, str], timeout: int = TIMEOUT) -> Tuple[Optional[Any], int, Optional[str], float]:
    if not HAS_CURL_CFFI:
        return None, 0, "curl_cffi unavailable", 0.0
    proxy_url = "http://" + proxy["user"] + ":" + proxy["pw"] + "@" + proxy["ip"] + ":" + proxy["port"]
    url = "%s%s" % (API_BASE, path)
    t0 = time.monotonic()
    try:
        response = await asyncio.to_thread(
            cffi_requests.get, url, impersonate=IMPERSONATE,
            proxies={"http": proxy_url, "https": proxy_url}, timeout=timeout,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        status = response.status_code
        try:
            body = response.json()
            error = None
        except Exception as e:
            body = None
            error = "invalid_json: %s" % e
        return body, status, error, latency_ms
    except Exception as e:
        latency_ms = int((time.monotonic() - t0) * 1000)
        return None, 0, "%s: %s" % (type(e).__name__, str(e)[:120]), latency_ms


async def fetch_with_retry(path: str, pool: HealthProxyPool, max_retries: int = 3,
                           evidence_lines: Optional[List[CallEvidence]] = None,
                           comp_name: str = "", ut_id: int = 0, season_id: int = 0) -> Tuple[Optional[Any], int, Optional[str], Optional[str], int]:
    last_error = None
    last_status = 0
    ip_used = None

    for attempt in range(max_retries + 1):
        proxy = pool.get_next_available()
        if not proxy:
            await asyncio.sleep(5)
            continue

        ip_used = proxy["ip"]
        body, status, error, latency = await http_get(path, proxy)

        if status == 200 and body is not None:
            record_outcome(ip_used, "success", path=path)
        elif status == 403:
            record_outcome(ip_used, "403", path=path)
        elif status == 404:
            record_outcome(ip_used, "not_found", path=path)
        else:
            record_outcome(ip_used, "fail", path=path, extra={"http": status, "error": error})

        if evidence_lines is not None:
            ev_count = 0
            if body and isinstance(body, dict):
                events = body.get("events", [])
                if isinstance(events, list):
                    ev_count = len(events)
            evidence_lines.append(CallEvidence(
                comp_name=comp_name, ut_id=ut_id, season_id=season_id,
                endpoint=path, ip=ip_used, status=status, event_count=ev_count,
                error=error, latency_ms=latency,
            ))

        if status == 200 and body is not None:
            return body, status, None, ip_used, attempt + 1

        if status == 404:
            return None, 404, "http_404", ip_used, attempt + 1

        if status == 403 or status >= 500 or status == 0:
            last_error = error or "http_%s" % status
            last_status = status
            await asyncio.sleep(random.uniform(1.0, 2.0))
            continue

        last_error = error or "http_%s" % status
        last_status = status
        await asyncio.sleep(random.uniform(1.0, 2.0))

    return None, last_status, last_error, ip_used, max_retries + 1


# ---------------------------------------------------------------------------
# Enumeration (full pagination)
# ---------------------------------------------------------------------------

async def enumerate_all_events(pool: HealthProxyPool, ut_id: int, season_id: int,
                                evidence_lines: List[CallEvidence],
                                comp_name: str) -> List[int]:
    all_event_ids: List[int] = []
    seen: Set[int] = set()
    page = 0
    consec_403 = 0
    empty_pages = 0

    while len(all_event_ids) < MAX_EVENTS_PER_COMP:
        path = "/unique-tournament/%s/season/%s/events/last/%s" % (ut_id, season_id, page)
        body, status, error, ip, tries = await fetch_with_retry(
            path, pool, max_retries=2, evidence_lines=evidence_lines,
            comp_name=comp_name, ut_id=ut_id, season_id=season_id
        )

        if status == 403:
            consec_403 += 1
            if consec_403 >= CONSEC_403_LIMIT:
                print("  ⚠ %s: consec_403 limit (%s) reached, stopping enumeration" % (comp_name, CONSEC_403_LIMIT))
                break
        else:
            consec_403 = 0

        if status != 200 or body is None:
            print("  ⚠ %s page %s: HTTP %s %s" % (comp_name, page, status, error))
            break

        events = body.get("events", [])
        if not events:
            empty_pages += 1
            if empty_pages >= 3:
                break
        else:
            empty_pages = 0

        page_new = 0
        for e in events:
            eid = e.get("id")
            if eid and eid not in seen:
                seen.add(eid)
                all_event_ids.append(eid)
                page_new += 1

        print("  [enum] %s page=%s new=%s total=%s" % (comp_name, page, page_new, len(all_event_ids)))
        page += 1
        await asyncio.sleep(random.uniform(0.5, 1.5))

    print("  [enum] %s DONE: %s unique events" % (comp_name, len(all_event_ids)))
    return all_event_ids


# ---------------------------------------------------------------------------
# Event processing
# ---------------------------------------------------------------------------

async def fetch_event_bundle(pool: HealthProxyPool, event_id: int,
                              evidence_lines: List[CallEvidence],
                              comp_name: str, ut_id: int, season_id: int) -> Dict[str, Any]:
    bundle = {"event_id": event_id, "endpoints": {}}

    for ep_name in ALL_ENDPOINTS:
        path = ENDPOINT_PATHS[ep_name].format(event_id=event_id)
        body, status, error, ip, tries = await fetch_with_retry(
            path, pool, max_retries=2, evidence_lines=evidence_lines,
            comp_name=comp_name, ut_id=ut_id, season_id=season_id
        )
        bundle["endpoints"][ep_name] = {"status": status, "body": body, "error": error}

        await asyncio.sleep(random.uniform(0.3, 0.8))

    return bundle


def upsert_match(conn, event_data: dict, season_id: int, competition_id: int) -> int:
    cur = conn.cursor()
    home = event_data.get("homeTeam", {})
    away = event_data.get("awayTeam", {})
    home_id = home.get("id")
    away_id = away.get("id")
    status_raw = event_data.get("status", {}).get("type", "finished")
    status_map = {"finished": "finished", "inprogress": "live", "notstarted": "scheduled", "postponed": "postponed", "cancelled": "postponed"}
    status_norm = status_map.get(status_raw, "finished")

    start_ts = event_data.get("startTimestamp")
    match_date = None
    match_time = None
    if start_ts:
        dt = datetime.fromtimestamp(start_ts, tz=timezone.utc)
        match_date = dt.date()
        match_time = dt.time()

    cur.execute("""
        INSERT INTO matches (match_id, season_id, competition_id, home_team_id, away_team_id,
                             home_score, away_score, status, match_date, match_time, round_name)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            home_score=VALUES(home_score), away_score=VALUES(away_score),
            status=VALUES(status), match_date=VALUES(match_date),
            match_time=VALUES(match_time), round_name=VALUES(round_name),
            updated_at=NOW()
    """, (
        event_data.get("id"), season_id, competition_id, home_id, away_id,
        (event_data.get("homeScore") or {}).get("current", 0),
        (event_data.get("awayScore") or {}).get("current", 0),
        status_norm, match_date, match_time, (event_data.get("roundInfo") or {}).get("name")
    ))
    conn.commit()
    return event_data.get("id")


def upsert_match_with_fk_seed(conn, event_data: dict, season_id: int, competition_id: int) -> int:
    """Match INSERT with in-run FK-failure seeding (MySQL 1216/1452).
    On FK failure: INSERT IGNORE seed teams from the event payload, retry once.
    Seeding is additive only (no UPDATE/DELETE)."""
    try:
        return upsert_match(conn, event_data, season_id, competition_id)
    except mysql.connector.Error as e:
        if e.errno not in (1216, 1452):
            raise
        print("    [fk] match INSERT FK errno=%s — seeding teams, retrying" % e.errno)
        try:
            conn.rollback()
        except Exception:
            pass
        cur = conn.cursor()
        seeded = 0
        for t in ((event_data.get("homeTeam") or {}), (event_data.get("awayTeam") or {})):
            tid = t.get("id")
            if tid is None:
                continue
            cur.execute("""
                INSERT IGNORE INTO teams (team_id, name, short_name) VALUES (%s, %s, %s)
            """, (int(tid), t.get("name") or "Unknown", t.get("shortName") or ""))
            seeded += max(cur.rowcount, 0)
        conn.commit()
        cur.close()
        print("    [fk] seeded %s team row(s); retrying match INSERT" % seeded)
        return upsert_match(conn, event_data, season_id, competition_id)


def upsert_incidents(conn, match_id: int, data: dict, event_data: dict) -> int:
    fallback_team_id = (event_data.get("homeTeam") or {}).get("id") or (event_data.get("awayTeam") or {}).get("id")
    cur = conn.cursor()
    n = 0
    incidents = data.get("incidents", [])
    for idx, inc in enumerate(incidents):
        team_id = inc.get("team", {}).get("id")
        if team_id is None:
            team_id = fallback_team_id
        if team_id is None:
            continue
        itype = inc.get("incidentType", "")
        if itype not in ("goal", "card", "substitution", "period", "var"):
            itype = "period"
        goal_type = None
        card_type = None
        if itype == "goal":
            goal_type = inc.get("incidentClass", "regular")
            if goal_type not in ("regular", "penalty", "own_goal", "free_kick"):
                goal_type = "regular"
        elif itype == "card":
            card_type = inc.get("incidentClass", "yellow")
            if card_type not in ("yellow", "red", "yellow_red"):
                card_type = "yellow"

        try:
            cur.execute("""
                INSERT INTO match_incidents (match_id, incident_id, team_id, player_id,
                                             incident_type, minute, added_time, period,
                                             is_home, goal_type, card_type, incident_text, reason)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    team_id=VALUES(team_id), player_id=VALUES(player_id),
                    incident_type=VALUES(incident_type), minute=VALUES(minute),
                    added_time=VALUES(added_time), period=VALUES(period),
                    is_home=VALUES(is_home), goal_type=VALUES(goal_type),
                    card_type=VALUES(card_type), incident_text=VALUES(incident_text),
                    reason=VALUES(reason)
            """, (
                match_id, inc.get("id") or (match_id * 1000 + idx), team_id,
                inc.get("player", {}).get("id"),
                itype, inc.get("time"), inc.get("addedTime"),
                inc.get("period") or ("first" if (inc.get("time") or 0) <= 45 else "second"),
                1 if inc.get("isHome") else 0,
                goal_type, card_type, inc.get("text"), inc.get("reason")
            ))
            n += 1
        except Exception as e:
            print('    Error inserting incident %s: %s' % (inc.get("id"), e))
            conn.rollback()
    conn.commit()
    return n


def upsert_lineups(conn, match_id: int, data: dict, event_data: dict) -> int:
    cur = conn.cursor()
    n = 0
    data.setdefault("homeTeam", event_data.get("homeTeam", {}))
    data.setdefault("awayTeam", event_data.get("awayTeam", {}))

    for is_home, key in ((1, "home"), (0, "away")):
        side = data.get(key, {}) or {}
        team = side.get("team") or {}
        team_id = team.get("id")
        players = side.get("players", []) or []
        if team_id is None and players:
            team_id = players[0].get("teamId")
        if team_id is None:
            team_id = (data["homeTeam"] if is_home else data["awayTeam"]).get("id")
        fallback_team = data["homeTeam"] if is_home else data["awayTeam"]

        for p in players:
            pl = p.get("player", {})
            if not pl.get("id") or team_id is None:
                continue
            stat = p.get("statistics") or {}
            pos_map = {"G": "GK", "D": "DEF", "M": "MID", "F": "FWD"}
            pc = pos_map.get((pl.get("position") or "").upper()[:1], "MID")

            cur.execute("""
                INSERT IGNORE INTO teams (team_id, name, short_name) VALUES (%s, %s, %s)
            """, (team_id, (team or fallback_team).get("name", "Unknown"), (team or fallback_team).get("shortName", "")))

            cur.execute("""
                INSERT IGNORE INTO players (player_id, name, short_name, position, current_team_id)
                VALUES (%s, %s, %s, %s, %s)
            """, (pl["id"], pl.get("name", "Unknown"), pl.get("shortName", ""), pl.get("position", ""), team_id))

            try:
                cur.execute("""
                    INSERT INTO match_lineups
                    (match_id, team_id, player_id, is_home, is_starter,
                     jersey_number, position, position_category, is_captain,
                     minutes_played, rating)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        is_starter=VALUES(is_starter), jersey_number=VALUES(jersey_number),
                        position=VALUES(position), position_category=VALUES(position_category),
                        is_captain=VALUES(is_captain), minutes_played=VALUES(minutes_played),
                        rating=VALUES(rating)
                """, (
                    match_id, team_id, pl["id"], is_home,
                    1 if p.get("substitute") is False else 0,
                    p.get("jerseyNumber") if p.get("jerseyNumber") not in ("", None) else None,
                    pl.get("position") or p.get("position") or "",
                    pc, 1 if p.get("captain") else 0,
                    stat.get("minutesPlayed"), stat.get("rating")
                ))
                n += 1
            except Exception as e:
                print('    Error inserting lineup player %s: %s' % (pl.get("id"), e))
                conn.rollback()
    conn.commit()
    return n


def upsert_statistics(conn, match_id: int, data: dict, event_data: dict = None) -> int:
    cur = conn.cursor()
    n = 0
    stats = data.get("statistics", [])
    for group in stats:
        group_name = group.get("groupName", "")
        items = group.get("statisticsItems", [])
        for item in items:
            cur.execute("""
                INSERT INTO match_statistics (match_id, team_id, group_name, name, home_value, away_value)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    home_value=VALUES(home_value), away_value=VALUES(away_value)
            """, (
                match_id,
                item.get("teamId"),
                group_name,
                item.get("name"),
                item.get("home"),
                item.get("away")
            ))
            n += 1
    conn.commit()
    return n


def upsert_shotmap(conn, match_id: int, data: dict, event_data: dict = None) -> int:
    cur = conn.cursor()
    n = 0
    shotmap = data.get("shotmap", [])
    home_id = ((event_data or {}).get("homeTeam") or {}).get("id")
    away_id = ((event_data or {}).get("awayTeam") or {}).get("id")
    for shot in shotmap:
        try:
            shot_team_id = (shot.get("team") or {}).get("id")
            if shot_team_id is None:
                shot_team_id = home_id if shot.get("isHome") else away_id
            cur.execute("""
                INSERT INTO match_shotmap (match_id, shot_id, team_id, player_id, is_home,
                                           minute, incident_type, shot_type, situation,
                                           body_part, player_x, player_y, player_z, xg,
                                           is_goal, goal_mouth_location, goal_mouth_x,
                                           goal_mouth_y, goal_mouth_z, xgot,
                                           block_x, block_y, block_z,
                                           goalkeeper_id, goalkeeper_name, added_time,
                                           time_seconds, period_time_seconds)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    team_id=VALUES(team_id), player_id=VALUES(player_id),
                    is_home=VALUES(is_home), minute=VALUES(minute),
                    incident_type=VALUES(incident_type), shot_type=VALUES(shot_type),
                    situation=VALUES(situation), body_part=VALUES(body_part),
                    player_x=VALUES(player_x), player_y=VALUES(player_y),
                    player_z=VALUES(player_z), xg=VALUES(xg), is_goal=VALUES(is_goal),
                    goal_mouth_location=VALUES(goal_mouth_location),
                    goal_mouth_x=VALUES(goal_mouth_x), goal_mouth_y=VALUES(goal_mouth_y),
                    goal_mouth_z=VALUES(goal_mouth_z), xgot=VALUES(xgot),
                    block_x=VALUES(block_x), block_y=VALUES(block_y),
                    block_z=VALUES(block_z), goalkeeper_id=VALUES(goalkeeper_id),
                    goalkeeper_name=VALUES(goalkeeper_name), added_time=VALUES(added_time),
                    time_seconds=VALUES(time_seconds), period_time_seconds=VALUES(period_time_seconds)
            """, (
                match_id, shot.get("id"), shot_team_id,
                shot.get("player", {}).get("id"), 1 if shot.get("isHome") else 0,
                shot.get("time"), shot.get("incidentType"), shot.get("shotType"),
                shot.get("situation"), shot.get("bodyPart"),
                shot.get("x"), shot.get("y"), shot.get("z"), shot.get("xg"),
                1 if shot.get("isGoal") else 0,
                shot.get("goalMouthLocation"), shot.get("goalMouthX"),
                shot.get("goalMouthY"), shot.get("goalMouthZ"), shot.get("xgot"),
                shot.get("blockX"), shot.get("blockY"), shot.get("blockZ"),
                shot.get("goalkeeper", {}).get("id"), shot.get("goalkeeper", {}).get("name"),
                shot.get("addedTime"), shot.get("timeSeconds"), shot.get("periodTimeSeconds")
            ))
            n += 1
        except Exception as e:
            print('    Error inserting shot %s: %s' % (shot.get("id"), e))
            conn.rollback()
    conn.commit()
    return n


def upsert_odds(conn, match_id: int, data: dict, event_data: dict = None) -> int:
    cur = conn.cursor()
    n = 0
    markets = data.get("markets", [])
    for market in markets:
        market_id = market.get("marketId")
        market_name = market.get("marketName")
        market_group = market.get("marketGroup")
        market_period = market.get("marketPeriod")
        structure_type = market.get("structureType")
        suspended = 1 if market.get("suspended") else 0
        choices = market.get("choices", [])
        for choice in choices:
            try:
                cur.execute("""
                    INSERT INTO match_odds (match_id, market_id, market_name, market_group, market_period,
                                             structure_type, suspended, choice_name, initial_fractional_value,
                                             fractional_value, winning, bookmaker_id, bookmaker_name, odds_type,
                                             home_odds, draw_odds, away_odds, fetched_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        fractional_value=VALUES(fractional_value), winning=VALUES(winning),
                        home_odds=VALUES(home_odds), draw_odds=VALUES(draw_odds), away_odds=VALUES(away_odds),
                        fetched_at=VALUES(fetched_at)
                """, (
                    match_id,
                    market_id, market_name, market_group, market_period,
                    structure_type, suspended, choice.get("name"),
                    choice.get("initialFractionalValue"), choice.get("fractionalValue"),
                    1 if choice.get("winning") else 0,
                    None, None, None,
                    None, None, None,
                    None
                ))
                n += 1
            except Exception as e:
                print('    Error inserting odds choice: %s' % e)
                conn.rollback()
    conn.commit()
    return n


UPGRADE_FUNCS = {
    "incidents": upsert_incidents,
    "lineups": upsert_lineups,
    "statistics": upsert_statistics,
    "shotmap": upsert_shotmap,
    "odds": upsert_odds,
}


async def process_event(conn, pool: HealthProxyPool, event_id: int,
                         evidence_lines: List[CallEvidence],
                         comp_name: str, ut_id: int, season_id: int,
                         comp_id: int) -> Tuple[bool, Dict[str, int]]:
    bundle = await fetch_event_bundle(pool, event_id, evidence_lines, comp_name, ut_id, season_id)
    event_data = bundle["endpoints"].get("event", {}).get("body", {})
    if not event_data or bundle["endpoints"].get("event", {}).get("status") != 200:
        return False, {}
    if isinstance(event_data, dict) and "event" in event_data and "id" not in event_data:
        event_data = event_data["event"]

    match_id = upsert_match_with_fk_seed(conn, event_data, season_id, comp_id)
    deltas = {}

    for ep_name in ("incidents", "lineups", "statistics", "shotmap", "odds"):
        ep_data = bundle["endpoints"].get(ep_name, {})
        if ep_data.get("status") == 200 and ep_data.get("body"):
            try:
                rows = UPGRADE_FUNCS[ep_name](conn, match_id, ep_data["body"], event_data)
                deltas[ep_name] = rows
            except Exception as e:
                print("    ⚠ %s event %s %s upsert failed: %s" % (comp_name, event_id, ep_name, e))
                deltas[ep_name] = 0

    return True, deltas


# ---------------------------------------------------------------------------
# DB state check
# ---------------------------------------------------------------------------

def get_db_counts(conn, ut_id: int, year_start: int) -> Dict[str, int]:
    """Row counts for one comp+season (year_start), before/after processing (delta calc)."""
    counts = {}
    for table in ["matches", "match_incidents", "match_lineups", "match_statistics",
                  "match_shotmap", "match_odds"]:
        try:
            cur = conn.cursor()
            if table == "matches":
                cur.execute("""
                    SELECT COUNT(*) FROM matches m
                    JOIN seasons s ON m.season_id=s.season_id
                    WHERE s.competition_id=%s AND s.year_start=%s
                """, (ut_id, year_start))
            else:
                cur.execute("""
                    SELECT COUNT(*) FROM %s t
                    JOIN matches m ON t.match_id=m.match_id
                    JOIN seasons s ON m.season_id=s.season_id
                    WHERE s.competition_id=%%s AND s.year_start=%%s
                """ % table, (ut_id, year_start))
            counts[table] = cur.fetchone()[0] or 0
            cur.close()
        except Exception as e:
            print("    [count_rows] %s %s err: %s" % (table, ut_id, e))
            counts[table] = -1
    return counts


# ---------------------------------------------------------------------------
# Season resolution (per-season, as-found) + seasons row seeding + baseline
# ---------------------------------------------------------------------------

SEASONS_CACHE: Dict[int, List[Dict[str, Any]]] = {}


def label_year_start(label: str) -> int:
    """'20/21' -> 2020, '21/22' -> 2021, ..."""
    return int(label[:2]) + 2000


async def resolve_season_id(pool: HealthProxyPool, ut_id: int, label: str,
                            comp_name: str) -> Tuple[Optional[int], Optional[str]]:
    """Resolve one comp+label SofaScore season id via /unique-tournament/{ut}/seasons
    (as-found). Seasons list cached per ut_id. Returns (season_id, matched_year)."""
    if ut_id not in SEASONS_CACHE:
        path = "/unique-tournament/%s/seasons" % ut_id
        body, status, error, ip, tries = await fetch_with_retry(
            path, pool, max_retries=2, comp_name=comp_name, ut_id=ut_id, season_id=0
        )
        if status != 200 or body is None or not isinstance(body, dict):
            print("  x %s: seasons fetch HTTP %s %s" % (comp_name, status, error))
            SEASONS_CACHE[ut_id] = []
        else:
            SEASONS_CACHE[ut_id] = body.get("seasons", []) or []
    seasons = SEASONS_CACHE[ut_id]
    if not seasons:
        return None, None
    ystart = label_year_start(label)
    for cand in (label, str(ystart)):
        for s in seasons:
            if str(s.get("year")) == cand:
                return s.get("id"), str(s.get("year"))
    # as-found fallback: substring match (e.g. "2020/21" for label 20/21)
    for s in seasons:
        y = str(s.get("year") or "")
        if y.startswith(str(ystart)) or label in y:
            return s.get("id"), y
    return None, None


def ensure_season_row(conn, ut_id: int, season_id: int, label: str) -> str:
    """Ensure the seasons row exists (FK target of matches.season_id).
    Additive only (ON DUPLICATE KEY UPDATE; no DELETE). Returns 'ok'|'missing-competition'."""
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM competitions WHERE competition_id=%s", (ut_id,))
    if not cur.fetchall():
        cur.close()
        return "missing-competition"
    cur.execute("""
        INSERT INTO seasons (season_id, competition_id, year_label, year_start, year_end, start_date, end_date, is_current)
        VALUES (%s, %s, %s, %s, %s, NULL, NULL, 0)
        ON DUPLICATE KEY UPDATE year_label=VALUES(year_label),
            year_start=VALUES(year_start), year_end=VALUES(year_end)
    """, (season_id, ut_id, label, label_year_start(label), label_year_start(label) + 1))
    conn.commit()
    cur.close()
    return "ok"


def snapshot_baseline(conn) -> Dict[str, Any]:
    """Pre-run baseline: per comp x table x season_id rowcounts for ALL seasons of
    TARGET_COMPS (scope guard: no season_id count may drop post-run)."""
    cur = conn.cursor()
    result: Dict[str, Any] = {}
    for comp in TARGET_COMPS:
        ut = comp["ut_id"]
        result[str(ut)] = {}
        for table in ["matches", "match_incidents", "match_lineups", "match_statistics",
                      "match_shotmap", "match_odds"]:
            if table == "matches":
                sql = (f"SELECT m.season_id, COUNT(*) FROM {table} m "
                       f"JOIN seasons s ON m.season_id=s.season_id "
                       f"WHERE s.competition_id=%s GROUP BY m.season_id")
            else:
                sql = (f"SELECT m.season_id, COUNT(*) FROM {table} t "
                       f"JOIN matches m ON t.match_id=m.match_id "
                       f"JOIN seasons s ON m.season_id=s.season_id "
                       f"WHERE s.competition_id=%s GROUP BY m.season_id")
            try:
                cur.execute(sql, (ut,))
                result[str(ut)][table] = {int(r[0]): int(r[1]) for r in cur.fetchall()}
            except Exception as e:
                print("    [baseline] %s %s err: %s" % (table, ut, e))
                result[str(ut)][table] = {}
    conn.commit()
    cur.close()
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run_comp(comp: Dict[str, Any], pool: HealthProxyPool,
                   evidence_lines: List[CallEvidence], season_label: str,
                   season_id: int) -> CompResult:
    name = comp["name"]
    ut_id = comp["ut_id"]
    comp_type = comp["type"]

    print("\n%s" % ("="*70))
    print("  BACKFILL: %s (ut_id=%s) season_id=%s %s" % (name, ut_id, season_id, season_label))
    print("%s" % ("="*70))

    result = CompResult(
        comp_name=name, ut_id=ut_id, season_id=season_id,
        season_label=season_label, comp_type=comp_type
    )

    ystart = label_year_start(season_label)
    conn = get_mysql_conn()
    before_counts = get_db_counts(conn, ut_id, ystart)
    conn.close()
    print("  [DB before] %s" % before_counts)

    event_ids = await enumerate_all_events(pool, ut_id, season_id, evidence_lines, name)
    result.total_events_found = len(event_ids)

    if smoke_events := int(os.environ.get("SMOKE_EVENTS", "0") or 0):
        event_ids = event_ids[:smoke_events]
        print("  [smoke] limiting to %s events" % len(event_ids))

    if not event_ids:
        result.error = "No events found"
        return result

    conn = get_mysql_conn()
    consec_403 = 0

    for idx, eid in enumerate(event_ids):
        print("  [%s/%s] Processing event %s" % (idx+1, len(event_ids), eid))

        try:
            success, deltas = await process_event(
                conn, pool, eid, evidence_lines, name, ut_id, season_id, ut_id
            )
            if success:
                result.events_processed += 1
                for k, v in deltas.items():
                    result.row_deltas[k] = result.row_deltas.get(k, 0) + v
            else:
                result.events_failed += 1
                consec_403 += 1
        except Exception as e:
            print("    ❌ Event %s error: %s" % (eid, e))
            result.events_failed += 1

        if consec_403 >= CONSEC_403_LIMIT:
            print("  ⚠ consec_403 limit (%s) reached, stopping %s" % (CONSEC_403_LIMIT, name))
            result.consec_403_hit = True
            break

        await asyncio.sleep(random.uniform(1.0, 2.5))

    conn.close()

    conn = get_mysql_conn()
    after_counts = get_db_counts(conn, ut_id, ystart)
    # known gap: minute-null incident rows for this season (reported, non-blocking)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM match_incidents mi
            JOIN matches m ON mi.match_id=m.match_id
            JOIN seasons s ON m.season_id=s.season_id
            WHERE s.competition_id=%s AND s.year_start=%s AND mi.minute IS NULL
        """, (ut_id, ystart))
        result.minute_null_incidents = cur.fetchone()[0] or 0
        cur.close()
    except Exception as e:
        print("    [minute-null count] err: %s" % e)
    conn.close()
    print("  [DB after] %s (minute_null_incidents=%s)" % (after_counts, result.minute_null_incidents))

    for table in before_counts:
        if before_counts[table] >= 0 and after_counts[table] >= 0:
            delta = after_counts[table] - before_counts[table]
            if delta > 0:
                result.row_deltas["db_%s" % table] = delta

    for ev in evidence_lines:
        if ev.comp_name == name and ev.ip:
            result.ip_usage[ev.ip] = result.ip_usage.get(ev.ip, 0) + 1

    result.calls_used = sum(1 for ev in evidence_lines if ev.comp_name == name)

    print("  ✅ %s: events=%s/%s, failed=%s, calls=%s, deltas=%s" % (
        name, result.events_processed, result.total_events_found,
        result.events_failed, result.calls_used, result.row_deltas))

    return result


async def main():
    print("%s" % ("="*70))
    print("  Gen4 Phase B cup-retry Backfill (5 cups x %s seasons)" % len(SEASON_LABELS))
    print("%s" % ("="*70))

    # Smoke mode: SMOKE_EVENTS=<n>, SMOKE_COMPS=<k>, SMOKE_SEASONS=<s> limit the run.
    smoke_events = int(os.environ.get("SMOKE_EVENTS", "0") or 0)
    smoke_comps = int(os.environ.get("SMOKE_COMPS", "0") or 0)
    smoke_seasons = int(os.environ.get("SMOKE_SEASONS", "1" if smoke_events else "0") or 0)
    if smoke_events:
        print("⚠ SMOKE MODE: %s events/comp, %s comps, %s season(s)" % (smoke_events, smoke_comps or "all", smoke_seasons))

    if not POOL_FILE.exists():
        print("✗ Pool file not found: %s" % POOL_FILE)
        sys.exit(1)

    pool = HealthProxyPool(POOL_FILE)
    evidence_lines: List[CallEvidence] = []
    all_results: List[CompResult] = []
    total_calls = 0

    # Scope-guard baseline (pre-run): all seasons of TARGET_COMPS, written once.
    baseline_path = ROOT / "data/gen4_phaseB_cup_retry_baseline_counts.json"
    if not baseline_path.exists():
        conn = get_mysql_conn()
        baseline_path.write_text(json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "baseline": snapshot_baseline(conn),
        }, ensure_ascii=False, indent=2))
        conn.close()
        print("[baseline] pre-run baseline written to %s" % baseline_path)
    else:
        print("[baseline] baseline already exists, keeping it: %s" % baseline_path)

    # Idempotent resume: skip comp-seasons already completed (per checkpoint).
    completed_pairs: Set[Tuple[str, str]] = set()
    checkpoint_path = CHECKPOINT_FILE if not smoke_events else ROOT / "data/phaseB_cup_retry_smoke_checkpoint.json"
    if not smoke_events and checkpoint_path.exists():
        try:
            cp = json.loads(checkpoint_path.read_text())
            completed_pairs = {(x["comp_name"], x["season_label"]) for x in cp.get("completed_comps", [])}
            print("[resume] checkpoint: %s comp-seasons already completed" % len(completed_pairs))
        except Exception as e:
            print("[resume] checkpoint unreadable (%s), starting fresh" % e)

    comps = TARGET_COMPS
    if smoke_events:
        comps = comps[:smoke_comps] if smoke_comps else comps[:1]
    labels = SEASON_LABELS[:smoke_seasons] if (smoke_events and smoke_seasons) else SEASON_LABELS

    checkpoint = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "phase_tag": PHASE_TAG,
        "completed": len(all_results),
        "total": len(comps) * len(labels),
        "last_comp": None,
        "total_calls": total_calls,
        "total_events_processed": 0,
        "completed_comps": [],
    }

    for comp in comps:
        for label in labels:
            if (comp["name"], label) in completed_pairs:
                print("  ∼ %s %s already completed — skip (resume)" % (comp["name"], label))
                continue

            season_id, matched_year = await resolve_season_id(pool, comp["ut_id"], label, comp["name"])
            if season_id is None:
                print("  ✗ %s %s: season not resolved — skip (recorded)" % (comp["name"], label))
                all_results.append(CompResult(comp_name=comp["name"], ut_id=comp["ut_id"], season_id=0,
                                              season_label=label, comp_type=comp["type"],
                                              error="season-not-resolved"))
                continue

            # Prep: ensure seasons row exists (FK target), additive only.
            conn = get_mysql_conn()
            seed_status = ensure_season_row(conn, comp["ut_id"], season_id, label)
            conn.close()
            if seed_status != "ok":
                print("  ✗ %s %s: %s — skip (recorded)" % (comp["name"], label, seed_status))
                all_results.append(CompResult(comp_name=comp["name"], ut_id=comp["ut_id"], season_id=season_id,
                                              season_label=label, comp_type=comp["type"],
                                              error=seed_status))
                continue

            result = await run_comp(comp, pool, evidence_lines, label, season_id)
            result.matched_year = matched_year
            all_results.append(result)
            total_calls += result.calls_used

            checkpoint["completed_comps"].append({
                "comp_name": comp["name"], "season_label": label,
                "season_id": season_id, "matched_year": matched_year,
            })
            checkpoint["timestamp"] = datetime.now(timezone.utc).isoformat()
            checkpoint["completed"] = len(all_results)
            checkpoint["last_comp"] = "%s %s" % (comp["name"], label)
            checkpoint["total_calls"] = total_calls
            checkpoint["total_events_processed"] = sum(r.events_processed for r in all_results)
            checkpoint_path.write_text(json.dumps(checkpoint, indent=2))

    ip_summary = {}
    for ev in evidence_lines:
        if ev.ip:
            if ev.ip not in ip_summary:
                ip_summary[ev.ip] = {"calls": 0, "success": 0, "403": 0, "fail": 0}
            ip_summary[ev.ip]["calls"] += 1
            if ev.status == 200:
                ip_summary[ev.ip]["success"] += 1
            elif ev.status == 403:
                ip_summary[ev.ip]["403"] += 1
            else:
                ip_summary[ev.ip]["fail"] += 1

    total_deltas = {}
    for r in all_results:
        for k, v in r.row_deltas.items():
            total_deltas[k] = total_deltas.get(k, 0) + v

    known_gaps = []
    minute_null_total = sum(r.minute_null_incidents for r in all_results)
    if minute_null_total:
        known_gaps.append("minute-null incident rows: %s (recorded as known gap, non-blocking)" % minute_null_total)
    for r in all_results:
        if r.error:
            known_gaps.append("%s %s: %s" % (r.comp_name, r.season_label, r.error))

    report = BackfillReport(
        timestamp=datetime.now(timezone.utc).isoformat(),
        total_comps=len(all_results),
        total_events=sum(r.events_processed for r in all_results),
        total_calls=total_calls,
        comps=all_results,
        ip_summary=ip_summary,
        row_deltas_total=total_deltas,
        known_gaps=known_gaps,
    )

    OUTPUT_REPORT.parent.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_REPORT
    evid_path = EVIDENCE_JSONL
    if smoke_events:
        out_path = ROOT / "data/gen4_phaseB_cup_retry_smoke_report.json"
        evid_path = ROOT / "data/gen4_phaseB_cup_retry_smoke_evidence.jsonl"
    out_path.write_text(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    print("\n✓ Report written to %s" % out_path)

    evid_path.parent.mkdir(parents=True, exist_ok=True)
    with evid_path.open("w") as f:
        for ev in evidence_lines:
            f.write(ev.to_jsonl() + "\n")
    print("✓ Evidence written to %s (%s lines)" % (evid_path, len(evidence_lines)))

    SEASON_IDS_FILE.write_text(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "phase_tag": PHASE_TAG,
        "season_labels": SEASON_LABELS,
        "comps": [
            {"name": r.comp_name, "ut_id": r.ut_id, "season_label": r.season_label,
             "season_id": r.season_id, "matched_year": r.matched_year,
             "status": r.error or "ok"}
            for r in all_results
        ],
    }, ensure_ascii=False, indent=2))
    print("✓ Season ids written to %s" % SEASON_IDS_FILE)

    print("\n%s" % ("="*70))
    print("  BACKFILL SUMMARY")
    print("%s" % ("="*70))
    for r in all_results:
        print("  %s %s (sid=%s%s) | events=%s/%s | failed=%s | calls=%s | deltas=%s" % (
            r.comp_name, r.season_label, r.season_id,
            ", matched=%s" % r.matched_year if r.matched_year else "",
            r.events_processed, r.total_events_found,
            r.events_failed, r.calls_used, r.row_deltas))
    for g in known_gaps:
        print("  [known gap] %s" % g)
    print("\n  Total events processed: %s" % sum(r.events_processed for r in all_results))
    print("  Total calls: %s" % total_calls)
    print("  Total row deltas: %s" % total_deltas)


if __name__ == "__main__":
    asyncio.run(main())