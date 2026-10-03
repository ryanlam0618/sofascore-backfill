#!/usr/bin/env python3
"""
gen4_phaseB_17_18_rerun.py — Targeted re-run for 16 zero-event comps + K League 1 17/18.

Uses new 20-IP pool (good_20260917_v2.txt) + curl_cffi chrome124 + health-aware pick_ip.
Full pagination, idempotent upserts, consec_403 early-stop, per-IP burst guard.
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
from team_attribution import resolve_incident_team_id, resolve_side_team_id

import yaml
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

# Import health-aware IP selection
from ip_health_score import pick_ip, record_outcome, is_available, snapshot, reset

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

POOL_FILE = ROOT / "data/proxy_pools/good_20260917_v2.txt"
YAML_FILE = ROOT / "competitions_10y.yaml"
ZEROVERIFY_REPORT = ROOT / "data/gen4_phaseB_17-18_zeroverify_report.json"
OUTPUT_REPORT = ROOT / "data/gen4_phaseB_17-18_zeroverify_rerun_report.json"
EVIDENCE_JSONL = ROOT / "data/gen4_phaseB_17-18_zeroverify_rerun_evidence.jsonl"

TIMEOUT = 20
MAX_CALLS_PER_IP_BURST = 2  # Per-IP burst guard
CONSEC_403_LIMIT = 5  # Early-stop after N consecutive 403s
MAX_EVENTS_PER_COMP = 2000  # Safety ceiling

API_BASE = "https://api.sofascore.com/api/v1"

# 16 comps from zero-verify + K League 1 (partial from Phase B)
TARGET_COMPS = [
    {"name": "A-League Men", "ut_id": 136, "season_id": 14404, "season_label": "17/18", "type": "league"},
    {"name": "Chinese Super League", "ut_id": 649, "season_id": 12938, "season_label": "17/18", "type": "league"},
    {"name": "UCL", "ut_id": 7, "season_id": 13415, "season_label": "17/18", "type": "continental"},
    {"name": "UEL", "ut_id": 679, "season_id": 13416, "season_label": "17/18", "type": "continental"},
    {"name": "AFC Champions League", "ut_id": 463, "season_id": 12773, "season_label": "17/18", "type": "continental"},
    {"name": "AFC Champions League Two", "ut_id": 668, "season_id": 12835, "season_label": "17/18", "type": "continental"},
    {"name": "FA Cup", "ut_id": 19, "season_id": 15100, "season_label": "17/18", "type": "cup"},
    {"name": "EFL Cup", "ut_id": 21, "season_id": 13389, "season_label": "17/18", "type": "cup"},
    {"name": "Copa del Rey", "ut_id": 329, "season_id": 14023, "season_label": "17/18", "type": "cup"},
    {"name": "Coppa Italia", "ut_id": 328, "season_id": 13673, "season_label": "17/18", "type": "cup"},
    {"name": "Coupe de France", "ut_id": 335, "season_id": 15371, "season_label": "17/18", "type": "cup"},
    {"name": "DFB Pokal", "ut_id": 217, "season_id": 13373, "season_label": "17/18", "type": "cup"},
    {"name": "J.League Cup", "ut_id": 101, "season_id": 13015, "season_label": "17/18", "type": "cup"},
    {"name": "Emperor's Cup", "ut_id": 323, "season_id": 13400, "season_label": "17/18", "type": "cup"},
    {"name": "Australia Cup", "ut_id": 1786, "season_id": 13584, "season_label": "17/18", "type": "cup"},
    {"name": "Chinese FA Cup", "ut_id": 882, "season_id": 13137, "season_label": "17/18", "type": "cup"},
    {"name": "K League 1", "ut_id": 410, "season_id": 12908, "season_label": "17/18", "type": "league"},
]

# NOTE: http_get() prepends API_BASE (= https://api.sofascore.com/api/v1).
# Paths here must NOT include the /api/v1 prefix (double prefix -> 404,
# found in 2026-09-17 22:32 failed rerun: all 510 event-detail calls hit
# https://api.sofascore.com/api/v1/api/v1/event/... -> 404).
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
class RerunReport:
    timestamp: str
    total_comps: int
    total_events: int
    total_calls: int
    comps: List[CompResult]
    ip_summary: Dict[str, Dict[str, int]]
    row_deltas_total: Dict[str, int]


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
# Proxy pool with health-aware selection
# ---------------------------------------------------------------------------

class HealthProxyPool:
    def __init__(self, pool_file: Path):
        self.pool: List[Dict[str, str]] = []
        self.ip_burst_counts: Dict[str, int] = {}
        self._cursor: int = 0  # round-robin cursor so load spreads across pool
        self._load(pool_file)
        reset()  # Clear any previous health state

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
        """Get next available IP: round-robin rotation + burst guard.

        Fixed 2026-09-17: previous pick_ip-always-first behavior hammered the
        first healthy IP for every call (615 calls on one IP in the failed
        rerun) because 404s never trigger quarantine. Round-robin over
        available IPs preserves health-awareness while spreading load.
        """
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
            # All quarantined: fall back to soonest-release (never block pipeline)
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
            cffi_requests.get,
            url,
            impersonate="chrome124",
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=timeout,
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
    """Fetch with proxy rotation on 403/5xx, recording health outcomes."""
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

        # Record health outcome
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
    """Full pagination via /events/last/{page} until empty."""
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
    """Fetch all endpoints for one event."""
    bundle = {"event_id": event_id, "endpoints": {}}

    for ep_name in ALL_ENDPOINTS:
        path = ENDPOINT_PATHS[ep_name].format(event_id=event_id)
        body, status, error, ip, tries = await fetch_with_retry(
            path, pool, max_retries=2, evidence_lines=evidence_lines,
            comp_name=comp_name, ut_id=ut_id, season_id=season_id
        )
        bundle["endpoints"][ep_name] = {"status": status, "body": body, "error": error}

        # Rate limiting between endpoints
        await asyncio.sleep(random.uniform(0.3, 0.8))

    return bundle


def upsert_match(conn, event_data: dict, season_id: int, competition_id: int) -> int:
    """Insert or update match, return match_id."""
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


def upsert_incidents(conn, match_id: int, data: dict, event_data: dict) -> int:
    """Insert incidents matching actual schema."""
    # Schema: incident_id, match_id, team_id, player_id, related_player_id, assist_player_id,
    # incident_type (enum), minute, added_time, period (enum), is_home,
    # goal_type (enum), card_type (enum), incident_text, reason
    # team_id attribution fix (2026-10-03): isHome-derived side ids only;
    # foreign-team rows are skipped + counted (rootcause_teamid_20261002.md).
    # Kris 2026-10-03 18:07: period/injuryTime system marker rows are written
    # with the home team id as inert filler (is_home=0) and counted separately
    # in system_marker_rows — they are NOT attributions (team_attribution.py).
    rejected_foreign_team = 0
    rejected_foreign_sample = []
    system_marker_rows = 0
    cur = conn.cursor()
    n = 0
    incidents = data.get("incidents", [])
    for idx, inc in enumerate(incidents):
        team_id, reject_reason = resolve_incident_team_id(inc, event_data)
        if reject_reason == "foreign_team_id":
            rejected_foreign_team += 1
            if len(rejected_foreign_sample) < 5:
                rejected_foreign_sample.append(inc.get("id"))
        if team_id is None:
            continue
        if reject_reason == "system_marker":
            # Inert filler: the home team id only satisfies the NOT NULL FK;
            # NOT an attribution — consumers filter via
            # v_match_incidents_fixed.attribution_status='period_marker'.
            system_marker_rows += 1
        # Map incidentType to enum
        itype = inc.get("incidentType", "")
        if itype not in ("goal", "card", "substitution", "period", "var"):
            itype = "period"
        # Map incidentClass for goal_type/card_type
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
    if rejected_foreign_team or system_marker_rows:
        print('    [team_attribution] rejected %d foreign-team incident rows (sample ids: %s); '
              '%d system marker rows written (inert home-team filler)'
              % (rejected_foreign_team, rejected_foreign_sample, system_marker_rows))
    conn.commit()
    return n



def upsert_lineups(conn, match_id: int, data: dict, event_data: dict) -> int:
    """Insert lineups - minimal columns matching schema."""
    cur = conn.cursor()
    n = 0
    data.setdefault("homeTeam", event_data.get("homeTeam", {}))
    data.setdefault("awayTeam", event_data.get("awayTeam", {}))

    for is_home, key in ((1, "home"), (0, "away")):
        side = data.get(key, {}) or {}
        team = side.get("team") or {}
        players = side.get("players", []) or []
        # team_id attribution fix (2026-10-03, Kris-approved): derive from the
        # EVENT payload (same ids the matches table stores) + the trusted
        # is_home side flag — never from the garbage player-level `teamId`
        # (rootcause_teamid_20261002.md, team_attribution.py).
        team_id = resolve_side_team_id(event_data, is_home)
        fallback_team = data["homeTeam"] if is_home else data["awayTeam"]

        for p in players:
            pl = p.get("player", {})
            if not pl.get("id") or team_id is None:
                continue
            stat = p.get("statistics") or {}
            pos_map = {"G": "GK", "D": "DEF", "M": "MID", "F": "FWD"}
            pc = pos_map.get((pl.get("position") or "").upper()[:1], "MID")

            # Upsert team (minimal)
            cur.execute("""
                INSERT IGNORE INTO teams (team_id, name, short_name) VALUES (%s, %s, %s)
            """, (team_id, (team or fallback_team).get("name", "Unknown"), (team or fallback_team).get("shortName", "")))

            # Upsert player (minimal) - players table uses current_team_id
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
    """Insert shotmap matching actual schema."""
    # Schema: shot_id, match_id, team_id, player_id, is_home, minute, incident_type,
    # shot_type, situation, body_part, player_x, player_y, player_z, xg, is_goal,
    # goal_mouth_location, goal_mouth_x, goal_mouth_y, goal_mouth_z, xgot,
    # block_x, block_y, block_z, goalkeeper_id, goalkeeper_name, added_time,
    # time_seconds, period_time_seconds
    cur = conn.cursor()
    n = 0
    shotmap = data.get("shotmap", [])
    # Fixed 2026-09-18: shots no longer carry team/teamId (SofaScore shape change,
    # both current + old events) — derive team_id from isHome + event home/away.
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
    """Insert odds matching actual schema."""
    # Actual API structure: data['markets'] -> list of markets, each with 'choices'
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
                    None, None, None,  # bookmaker_id, bookmaker_name, odds_type - not in choice
                    None, None, None,  # home_odds, draw_odds, away_odds - use fractional
                    None  # fetched_at
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
    """Process one event. Returns (success, row_deltas)."""
    bundle = await fetch_event_bundle(pool, event_id, evidence_lines, comp_name, ut_id, season_id)
    event_data = bundle["endpoints"].get("event", {}).get("body", {})
    if not event_data or bundle["endpoints"].get("event", {}).get("status") != 200:
        return False, {}
    # Unwrap top-level {"event": {...}} wrapper (fixed 2026-09-18: /event/{id}
    # response nests the event object under "event"; without unwrap,
    # event_data.get("id") -> None -> INSERT 1048 match_id cannot be null).
    if isinstance(event_data, dict) and "event" in event_data and "id" not in event_data:
        event_data = event_data["event"]

    match_id = upsert_match(conn, event_data, season_id, comp_id)
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

def get_db_row_counts(conn, comp_name: str, ut_id: int) -> Dict[str, int]:
    """Get current row counts for this competition."""
    cur = conn.cursor()
    counts = {}
    tables = ["matches", "match_incidents", "match_lineups", "match_statistics",
              "match_shotmap", "match_odds"]
    for table in tables:
        try:
            cur.execute("""
                SELECT COUNT(*) FROM %s m
                JOIN seasons s ON m.season_id = s.season_id
                WHERE s.competition_id = %%s AND s.year_start = 2017
            """ % table, (ut_id,))
            row = cur.fetchone()
            counts[table] = row[0] if row else 0
        except Exception:
            counts[table] = -1
    cur.close()
    return counts


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run_comp(comp: Dict[str, Any], pool: HealthProxyPool,
                   evidence_lines: List[CallEvidence]) -> CompResult:
    name = comp["name"]
    ut_id = comp["ut_id"]
    season_id = comp["season_id"]
    season_label = comp["season_label"]
    comp_type = comp["type"]

    print("\n%s" % ("="*70))
    print("  RE-RUN: %s (ut_id=%s) season_id=%s %s" % (name, ut_id, season_id, season_label))
    print("%s" % ("="*70))

    result = CompResult(
        comp_name=name, ut_id=ut_id, season_id=season_id,
        season_label=season_label, comp_type=comp_type
    )

    # Get DB row counts before
    conn = get_mysql_conn()
    before_counts = get_db_row_counts(conn, name, ut_id)
    conn.close()
    print("  [DB before] %s" % before_counts)

    # Enumerate all events
    event_ids = await enumerate_all_events(pool, ut_id, season_id, evidence_lines, name)
    result.total_events_found = len(event_ids)

    if not event_ids:
        result.error = "No events found"
        return result

    # Process each event
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
            # NOTE: DB/INSERT errors must NOT count toward consec_403 early-stop
            # (fixed 2026-09-18: match_id-null INSERT errors were early-stopping
            # entire comps after 5 events even though HTTP was healthy).

        if consec_403 >= CONSEC_403_LIMIT:
            print("  ⚠ consec_403 limit (%s) reached, stopping %s" % (CONSEC_403_LIMIT, name))
            result.consec_403_hit = True
            break

        # Small delay between events
        await asyncio.sleep(random.uniform(1.0, 2.5))

    conn.close()

    # Get DB row counts after
    conn = get_mysql_conn()
    after_counts = get_db_row_counts(conn, name, ut_id)
    conn.close()
    print("  [DB after] %s" % after_counts)

    # Calculate deltas from DB (more accurate)
    for table in before_counts:
        if before_counts[table] >= 0 and after_counts[table] >= 0:
            delta = after_counts[table] - before_counts[table]
            if delta > 0:
                result.row_deltas["db_%s" % table] = delta

    # Collect IP usage
    for ev in evidence_lines:
        if ev.comp_name == name and ev.ip:
            result.ip_usage[ev.ip] = result.ip_usage.get(ev.ip, 0) + 1

    # Count calls for this comp
    result.calls_used = sum(1 for ev in evidence_lines if ev.comp_name == name)

    print("  ✅ %s: events=%s/%s, failed=%s, calls=%s, deltas=%s" % (
        name, result.events_processed, result.total_events_found,
        result.events_failed, result.calls_used, result.row_deltas))

    return result


async def main():
    print("%s" % ("="*70))
    print("  Gen4 Phase B 17/18 Targeted Re-Run")
    print("%s" % ("="*70))

    if not POOL_FILE.exists():
        print("✗ Pool file not found: %s" % POOL_FILE)
        sys.exit(1)

    # Verify season IDs from zero-verify report match
    zreport = json.loads(ZEROVERIFY_REPORT.read_text())
    zmap = {c["comp_name"]: c["resolved_season_id"] for c in zreport["comps"]}

    for comp in TARGET_COMPS:
        if comp["name"] in zmap:
            zid = zmap[comp["name"]]
            if comp["season_id"] != zid:
                print("  ⚠ Season ID mismatch for %s: script=%s, zero-verify=%s" % (comp['name'], comp['season_id'], zid))
                comp["season_id"] = zid
            else:
                print("  ✓ Season ID verified for %s: %s" % (comp['name'], zid))

    pool = HealthProxyPool(POOL_FILE)
    evidence_lines: List[CallEvidence] = []
    all_results: List[CompResult] = []
    total_calls = 0

    for comp in TARGET_COMPS:
        result = await run_comp(comp, pool, evidence_lines)
        all_results.append(result)
        total_calls += result.calls_used

        # Write progress checkpoint
        checkpoint = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "completed": len(all_results),
            "total": len(TARGET_COMPS),
            "last_comp": comp["name"],
            "total_calls": total_calls,
        }
        (ROOT / "data" / "phaseB_17-18_rerun_checkpoint.json").write_text(
            json.dumps(checkpoint, indent=2)
        )

    # IP summary
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

    # Total row deltas
    total_deltas = {}
    for r in all_results:
        for k, v in r.row_deltas.items():
            total_deltas[k] = total_deltas.get(k, 0) + v

    report = RerunReport(
        timestamp=datetime.now(timezone.utc).isoformat(),
        total_comps=len(all_results),
        total_events=sum(r.events_processed for r in all_results),
        total_calls=total_calls,
        comps=all_results,
        ip_summary=ip_summary,
        row_deltas_total=total_deltas,
    )

    OUTPUT_REPORT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_REPORT.write_text(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    print("\n✓ Report written to %s" % OUTPUT_REPORT)

    EVIDENCE_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with EVIDENCE_JSONL.open("w") as f:
        for ev in evidence_lines:
            f.write(ev.to_jsonl() + "\n")
    print("✓ Evidence written to %s (%s lines)" % (EVIDENCE_JSONL, len(evidence_lines)))

    # Print summary
    print("\n%s" % ("="*70))
    print("  RE-RUN SUMMARY")
    print("%s" % ("="*70))
    for r in all_results:
        print("  %s | events=%s/%s | failed=%s | calls=%s | deltas=%s" % (
            r.comp_name, r.events_processed, r.total_events_found,
            r.events_failed, r.calls_used, r.row_deltas))
    print("\n  Total events processed: %s" % sum(r.events_processed for r in all_results))
    print("  Total calls: %s" % total_calls)
    print("  Total row deltas: %s" % total_deltas)


if __name__ == "__main__":
    asyncio.run(main())