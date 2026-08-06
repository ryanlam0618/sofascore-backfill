#!/usr/bin/env python3
"""
SofaScore Backfill Runner — Full Pipeline
==========================================
Drives the complete backfill: enumerate events → fetch all data → insert to MySQL.

Usage:
  python3 backfill_runner.py --competition "Premier League" --limit-rounds 2
  python3 backfill_runner.py --competition "Premier League" --from-year 2024
  python3 backfill_runner.py --all --dry-run
  python3 backfill_runner.py --all

Pipeline per competition per season:
  1. Warm tournament page → get session
  2. GET /rounds → list of round numbers
  3. For each round: GET /events/round/{n} → list of event IDs
  4. For each event: warm event page, then fetch bundle (incidents, lineups, statistics, shotmap, etc.)
  5. Parse + INSERT into MySQL

Anti-block: random sleep 1.5-3s between requests, rotate proxy IP per browser context.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlparse


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _first_non_empty(*values):
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _normalize_country_code(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = str(value).strip().upper()
    return value[:3] if value else None


def _normalize_preferred_foot(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    value = str(value).strip().lower()
    mapping = {
        "left": "left",
        "right": "right",
        "both": "both",
        "either": "both",
    }
    return mapping.get(value)


def _normalize_match_status(value: Any) -> str:
    raw = str(value or "scheduled").strip().lower()
    mapping = {
        "notstarted": "scheduled",
        "scheduled": "scheduled",
        "fixture": "scheduled",
        "pending": "scheduled",
        "inprogress": "live",
        "live": "live",
        "1h": "live",
        "2h": "live",
        "ht": "live",
        "et": "live",
        "finished": "finished",
        "afteret": "finished",
        "afterpens": "finished",
        "ft": "finished",
        "postponed": "postponed",
        "delayed": "postponed",
        "canceled": "postponed",
        "cancelled": "postponed",
        "abandoned": "postponed",
        "suspended": "postponed",
        "interrupted": "postponed",
    }
    return mapping.get(raw, "scheduled")


def _parse_date_value(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        ts = int(value)
        if ts > 10_000_000_000:
            ts = ts / 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        if "T" in value:
            value = value.split("T", 1)[0]
        return value[:10]
    return None


def _camel_to_snake(value: str) -> str:
    value = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", value)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value).lower()


def _extract_season_years(year_label: Optional[str], season_payload: Optional[dict] = None) -> tuple[Optional[int], Optional[int]]:
    import re

    payload = season_payload or {}
    year_value = payload.get("year")
    if isinstance(year_value, str):
        match = re.match(r"^(\d{4})\s*/\s*(\d{2,4})$", year_value)
        if match:
            start = int(match.group(1))
            end_raw = match.group(2)
            end = int(end_raw) if len(end_raw) == 4 else (start // 100) * 100 + int(end_raw)
            return start, end
        match = re.match(r"^(\d{4})$", year_value)
        if match:
            year = int(match.group(1))
            return year, year
    elif isinstance(year_value, int):
        return year_value, year_value

    if year_label:
        match = re.match(r'^(\d{2})/(\d{2})$', year_label)
        if match:
            start = 2000 + int(match.group(1))
            end = 2000 + int(match.group(2))
            if end < start:
                end += 100
            return start, end
        match = re.match(r'^(\d{4})/(\d{2,4})$', year_label)
        if match:
            start = int(match.group(1))
            end_raw = match.group(2)
            end = int(end_raw) if len(end_raw) == 4 else (start // 100) * 100 + int(end_raw)
            if end < start:
                end += 100
            return start, end
        match = re.match(r'^(\d{4})$', year_label)
        if match:
            year = int(match.group(1))
            return year, year
    return None, None

import mysql.connector
from dotenv import load_dotenv
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from anti_block import PLATFORM_MAP, USER_AGENTS, get_platform, get_sec_ch_ua

try:
    from playwright_stealth import stealth_async
    _STEALTH = None
    HAS_STEALTH = True
except ImportError:
    try:
        from playwright_stealth import Stealth
        stealth_async = None
        _STEALTH = Stealth()
        HAS_STEALTH = True
    except ImportError:
        stealth_async = None
        _STEALTH = None
        HAS_STEALTH = False

try:
    from curl_cffi import requests as curl_requests
    HAS_CURL_CFFI = True
except ImportError:
    curl_requests = None
    HAS_CURL_CFFI = False


def ensure_country(mysql_conn, country_code: Optional[str], country_name: Optional[str] = None, alpha2: Optional[str] = None) -> Optional[str]:
    """Ensure a country reference exists before teams/players point at it."""
    norm_country_code = _normalize_country_code(country_code)
    if not norm_country_code:
        return None

    clean_alpha2 = str(alpha2).strip().upper()[:2] if alpha2 else None
    cur = mysql_conn.cursor()
    cur.execute(
        """INSERT INTO countries (country_code, country_name, alpha2)
           VALUES (%s, %s, %s)
           ON DUPLICATE KEY UPDATE
               country_name=COALESCE(NULLIF(VALUES(country_name), ''), country_name),
               alpha2=COALESCE(VALUES(alpha2), alpha2)
        """,
        (norm_country_code, country_name or norm_country_code, clean_alpha2),
    )
    mysql_conn.commit()
    return norm_country_code


# ── Config ────────────────────────────────────────────────────────────────────
WORKDIR = Path(__file__).parent
DISCOVERIES_FILE = WORKDIR / "discoveries.json"
COMPETITIONS_FILE = WORKDIR / "competitions_10y.yaml"
DATA_DIR = WORKDIR / "data" / "backfill_sofascore_10y"
SEASON_URLS_FILE = WORKDIR / "data" / "season_urls_24_competitions.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATUS_DIR = WORKDIR / "logs"
STATUS_DIR.mkdir(parents=True, exist_ok=True)

BROWSER_BASE = "https://www.sofascore.com"
API_BASE = "https://www.sofascore.com/api/v1"

PLAYER_STAT_FIELDS = [
    "total_pass", "accurate_pass", "total_long_balls", "accurate_long_balls",
    "total_own_half_passes", "accurate_own_half_passes", "total_opposition_half_passes", "accurate_opposition_half_passes",
    "total_cross", "accurate_cross", "key_pass", "goal_assist", "big_chance_created",
    "total_shots", "on_target_scoring_attempt", "shot_off_target", "blocked_scoring_attempt",
    "expected_goals", "expected_goals_on_target", "expected_assists", "big_chance_missed", "shot_value_normalized",
    "duel_lost", "aerial_won", "aerial_lost", "total_contest", "won_contest", "challenge_lost",
    "total_tackle", "won_tackle", "interception_won", "total_clearance", "ball_recovery", "outfielder_block",
    "last_man_tackle", "error_lead_to_a_goal", "error_lead_to_a_shot",
    "dribble_value_normalized", "dispossessed", "unsuccessful_touch",
    "fouls", "was_fouled", "total_offside", "own_goals",
    "saves", "saved_shots_from_inside_the_box", "good_high_claim", "total_keeper_sweeper", "accurate_keeper_sweeper",
    "goals_prevented", "keeper_save_value", "goalkeeper_value_normalized",
    "top_speed", "kilometers_covered", "number_of_sprints", "meters_covered_running_km",
    "meters_covered_high_speed_running_km", "meters_covered_sprinting_km",
    "total_ball_carries_distance", "ball_carries_count", "total_progression", "progressive_ball_carries_count",
    "total_progressive_ball_carries_distance", "best_ball_carry_progression",
    "touches", "possession_lost_ctrl", "pass_value_normalized", "defensive_value_normalized",
]

PLAYER_STAT_JSON_KEYS = {}
for _field in PLAYER_STAT_FIELDS:
    parts = _field.split("_")
    PLAYER_STAT_JSON_KEYS[_field] = parts[0] + "".join(part.title() for part in parts[1:])
PLAYER_STAT_JSON_KEYS["error_lead_to_a_goal"] = "errorLeadToAGoal"
PLAYER_STAT_JSON_KEYS["error_lead_to_a_shot"] = "errorLeadToAShot"

LEGACY_PLAYER_STAT_MAPPING = {
    "minutes_played": "minutesPlayed",
    "goals": "goals",
    "shots": "totalShots",
    "shots_on_target": "onTargetScoringAttempt",
    "xg": "expectedGoals",
    "assists": "goalAssist",
    "xa": "expectedAssists",
    "key_passes": "keyPass",
    "passes_completed": "accuratePass",
    "tackles": "totalTackle",
    "interceptions": "interceptionWon",
    "duels_won": "duelWon",
    "fouls_committed": "fouls",
    "yellow_cards": "yellowCards",
    "red_cards": "redCards",
    "rating": "rating",
}

PROXY_SERVER = os.getenv("SOFA_PROXY_HOST", "p.webshare.io")
PROXY_PORT = os.getenv("SOFA_PROXY_PORT", "80")
PROXY_USER = os.getenv("SOFA_PROXY_USER", "aeptenjc-rotate")
PROXY_PASS = os.getenv("SOFA_PROXY_PASS", "")
PROXY_SESSION_PREFIX = os.getenv("SOFA_PROXY_SESSION_PREFIX", "sofa")
PROXY_STICKY_MINUTES = int(os.getenv("SOFA_PROXY_STICKY_MINUTES", "0"))  # 0 = rotate per request
BROWSER_RESTART_INTERVAL = 1800

# Load .env
load_dotenv(WORKDIR / ".env")
if not PROXY_PASS:
    PROXY_PASS = os.getenv("SOFA_PROXY_PASS", "")

CORE_ENDPOINTS: Sequence[str] = ("event", "incidents", "lineups")
MID_RISK_ENDPOINTS: Sequence[str] = ("statistics", "shotmap")
HIGH_RISK_ENDPOINTS: Sequence[str] = ("graph", "odds", "comments")

BLOCKED_THIRD_PARTY_DOMAINS: Sequence[str] = (
    "smartadserver.com",
    "googleadservices.com",
    "googlesyndication.com",
    "doubleclick.net",
    "googletagmanager.com",
    "google-analytics.com",
    "adverge.ai",
    "liadm.com",
    "criteo.com",
    "id5-sync",
    "a-mx.com",
    "a-mo.net",
    "crwdcntrl.net",
    "challenges.cloudflare.com",
    "jsdelivr.net",
    "digitaloceanspaces.com",
    "clipro.tv",
    "mvp.fan",
    "sentry.io",
    "firebaseinstallations.googleapis.com",
)

BLOCKED_RESOURCE_TYPES = {"image", "stylesheet", "font", "media"}

ENDPOINT_PATHS = {
    "event": "/api/v1/event/{event_id}",
    "incidents": "/api/v1/event/{event_id}/incidents",
    "lineups": "/api/v1/event/{event_id}/lineups",
    "statistics": "/api/v1/event/{event_id}/statistics",
    "shotmap": "/api/v1/event/{event_id}/shotmap",
    "graph": "/api/v1/event/{event_id}/graph",
    "odds": "/api/v1/event/{event_id}/odds/1/all",
    "comments": "/api/v1/event/{event_id}/comments",
}

PHASE_SLEEP_RANGES = {
    "core": (5.0, 8.0),
    "mid": (8.0, 12.0),
    "high": (12.0, 18.0),
}

ENDPOINT_PHASE = {
    "event": "core",
    "incidents": "core",
    "lineups": "core",
    "statistics": "mid",
    "shotmap": "mid",
    "graph": "high",
    "odds": "high",
    "comments": "high",
}

ENDPOINT_FETCH_LOG_TABLE = {
    "incidents": "match_incidents",
    "lineups": "match_lineups",
    "statistics": "match_statistics",
    "shotmap": "match_shotmap",
    "graph": "match_graph_points",
    "odds": "match_odds",
    "comments": "match_comments",
}

ENDPOINT_COUNTER_KEY = {
    "incidents": "incidents",
    "lineups": "lineups",
    "statistics": "statistics",
    "shotmap": "shotmap",
    "graph": "graph",
    "odds": "odds",
    "comments": "comments",
}

# ── MySQL ─────────────────────────────────────────────────────────────────────

def get_mysql_conn():
    return mysql.connector.connect(
        host=os.getenv("MYSQL_HOST", "192.168.0.183"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "root"),
        password=os.getenv("MYSQL_PASSWORD", ""),
        database=os.getenv("MYSQL_DATABASE", "appdb"),
        charset="utf8mb4",
    )


def ensure_competition(mysql_conn, name: str, cat_id: int, ut_id: int, season_mode: str, country: str) -> int:
    """Insert competition if not exists, return competition_id."""
    cur = mysql_conn.cursor()
    # Use ut_id as competition_id (unique tournament ID)
    comp_id = ut_id
    cur.execute(
        """INSERT IGNORE INTO competitions (competition_id, name, category_id, ut_id, type, season_mode)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (comp_id, name, cat_id, ut_id, season_mode, season_mode),
    )
    mysql_conn.commit()
    return comp_id


def ensure_season(mysql_conn, season_id: int, competition_id: int, year_label: str, season_data: Optional[dict] = None) -> int:
    cur = mysql_conn.cursor()
    payload = season_data or {}
    year_start, year_end = _extract_season_years(year_label, payload)
    start_date = _parse_date_value(_first_non_empty(payload.get("startDateTimestamp"), payload.get("startDate")))
    end_date = _parse_date_value(_first_non_empty(payload.get("endDateTimestamp"), payload.get("endDate")))
    is_current = 1 if payload.get("current") or payload.get("isCurrent") else 0
    cur.execute(
        """INSERT INTO seasons (season_id, competition_id, year_label, year_start, year_end, start_date, end_date, is_current)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON DUPLICATE KEY UPDATE
               competition_id=VALUES(competition_id),
               year_label=COALESCE(VALUES(year_label), year_label),
               year_start=COALESCE(VALUES(year_start), year_start),
               year_end=COALESCE(VALUES(year_end), year_end),
               start_date=COALESCE(VALUES(start_date), start_date),
               end_date=COALESCE(VALUES(end_date), end_date),
               is_current=VALUES(is_current)
        """,
        (season_id, competition_id, year_label, year_start, year_end, start_date, end_date, is_current),
    )
    mysql_conn.commit()
    return season_id


def ensure_team(mysql_conn, team_id: int, name: str, short_name: str = None, country_code: str = None, team_data: Optional[dict] = None) -> int:
    cur = mysql_conn.cursor()
    payload = team_data or {}
    country_payload = payload.get("country", {}) if isinstance(payload.get("country"), dict) else {}
    norm_country_code = ensure_country(
        mysql_conn,
        _first_non_empty(country_code, payload.get("countryCode"), country_payload.get("alpha3"), country_payload.get("alpha2")),
        country_payload.get("name"),
        country_payload.get("alpha2"),
    )
    slug = _first_non_empty(payload.get("slug"))
    city = _first_non_empty(payload.get("venueCity"), payload.get("city"), payload.get("venue", {}).get("city", {}).get("name"), payload.get("venue", {}).get("cityName"))
    stadium = _first_non_empty(payload.get("venueName"), payload.get("stadium"), payload.get("venue", {}).get("name"), payload.get("ground", {}).get("name"))
    founded_year = _first_non_empty(payload.get("founded"), payload.get("foundedYear"), payload.get("foundationYear"))
    team_type = "national" if payload.get("national") is True else "club"
    cur.execute(
        """INSERT INTO teams (team_id, name, short_name, slug, country_code, city, stadium, founded_year, team_type)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON DUPLICATE KEY UPDATE
               name=COALESCE(VALUES(name), name),
               short_name=COALESCE(VALUES(short_name), short_name),
               slug=COALESCE(VALUES(slug), slug),
               country_code=COALESCE(VALUES(country_code), country_code),
               city=COALESCE(VALUES(city), city),
               stadium=COALESCE(VALUES(stadium), stadium),
               founded_year=COALESCE(VALUES(founded_year), founded_year),
               team_type=COALESCE(VALUES(team_type), team_type),
               updated_at=NOW()
        """,
        (team_id, name, short_name, slug, norm_country_code, city, stadium, founded_year, team_type),
    )
    mysql_conn.commit()
    return team_id


def ensure_player(
    mysql_conn,
    player_id: int,
    name: str,
    short_name: str = None,
    position: str = None,
    country_code: str = None,
    player_data: Optional[dict] = None,
    current_team_id: Optional[int] = None,
) -> int:
    cur = mysql_conn.cursor()
    payload = player_data or {}
    country_payload = payload.get("country", {}) if isinstance(payload.get("country"), dict) else {}
    norm_country_code = ensure_country(
        mysql_conn,
        _first_non_empty(country_code, payload.get("countryCode"), country_payload.get("alpha3"), country_payload.get("alpha2")),
        country_payload.get("name"),
        country_payload.get("alpha2"),
    )
    birth_date = _parse_date_value(_first_non_empty(payload.get("dateOfBirthTimestamp"), payload.get("birthDateTimestamp"), payload.get("dateOfBirth"), payload.get("birthDate")))
    preferred_foot = _normalize_preferred_foot(_first_non_empty(payload.get("preferredFoot"), payload.get("foot")))
    resolved_team_id = _first_non_empty(current_team_id, payload.get("team", {}).get("id"), payload.get("currentTeam", {}).get("id"), payload.get("teamId"))

    cur.execute(
        """INSERT INTO players
               (player_id, name, short_name, slug, country_code, birth_date, age, height, weight,
                position, preferred_foot, current_team_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON DUPLICATE KEY UPDATE
               name=COALESCE(VALUES(name), name),
               short_name=COALESCE(VALUES(short_name), short_name),
               slug=COALESCE(VALUES(slug), slug),
               country_code=COALESCE(VALUES(country_code), country_code),
               birth_date=COALESCE(VALUES(birth_date), birth_date),
               age=COALESCE(VALUES(age), age),
               height=COALESCE(VALUES(height), height),
               weight=COALESCE(VALUES(weight), weight),
               position=COALESCE(VALUES(position), position),
               preferred_foot=COALESCE(VALUES(preferred_foot), preferred_foot),
               current_team_id=COALESCE(VALUES(current_team_id), current_team_id),
               updated_at=NOW()
        """,
        (
            player_id,
            name,
            short_name,
            _first_non_empty(payload.get("slug")),
            norm_country_code,
            birth_date,
            payload.get("age"),
            payload.get("height"),
            payload.get("weight"),
            position,
            preferred_foot,
            resolved_team_id,
        ),
    )
    mysql_conn.commit()
    return player_id


# ── State / Progress ──────────────────────────────────────────────────────────

class ProgressTracker:
    """Track backfill progress in a JSON file for resume support."""

    def __init__(self, path: Path):
        self.path = path
        self._state = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text())
        return {"events_done": [], "events_failed": [], "started_at": None, "updated_at": None}

    def save(self):
        self._state["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.path.write_text(json.dumps(self._state, ensure_ascii=False, indent=2))

    def is_done(self, event_id: int) -> bool:
        return event_id in self._state["events_done"]

    def mark_done(self, event_id: int):
        if event_id not in self._state["events_done"]:
            self._state["events_done"].append(event_id)
        self._state["events_failed"] = [
            item for item in self._state.get("events_failed", [])
            if (item.get("id") if isinstance(item, dict) else item) != event_id
        ]
        self.save()

    def mark_failed(self, event_id: int, reason: str = ""):
        if event_id not in self._state["events_failed"]:
            self._state["events_failed"].append({"id": event_id, "reason": reason, "at": datetime.now(timezone.utc).isoformat()})
        self.save()

    @property
    def done_count(self) -> int:
        return len(self._state["events_done"])

    @property
    def failed_count(self) -> int:
        return len(self._state["events_failed"])


# ── Browser Client ────────────────────────────────────────────────────────────

class BackfillClient:
    """Playwright browser client with proxy, for backfill operations."""

    def __init__(self, headless: bool = True, sticky_session_key: Optional[str] = None):
        self.headless = headless
        self.sticky_session_key = sticky_session_key
        self._pw = None
        self.browser = None
        self.context = None
        self.page = None
        self.api_page = None
        self._request_count = 0
        self._heal_count = 0
        self._current_event_id = None
        self._current_event_attempt = None
        self._current_action = "idle"
        self._last_api_path = None
        self._consecutive_403 = 0
        self._response_cache = {}
        self._response_waiters = {}
        self._network_capture_enabled = True
        self._current_ua = random.choice(USER_AGENTS)
        self._current_headers = {}
        self._current_proxy_url = None
        self._current_tournament_url = f"{BROWSER_BASE}/"
        self._last_browser_restart = time.time()

    async def __aenter__(self):
        self._pw = await async_playwright().start()
        launch_args = [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-setuid-sandbox",
            "--disable-accelerated-2d-canvas",
            "--disable-extensions",
            "--disable-plugins",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--memory-pressure-off",
            "--no-zygote",
            "--no-first-run",
            "--disable-webgl",
            '--js-flags="--max-old-space-size=512"',
            "--disable-blink-features=AutomationControlled",
        ]
        if self.headless:
            launch_args.append("--headless=new")
        self.browser = await self._pw.chromium.launch(
            headless=self.headless,
            args=launch_args,
        )
        self._last_browser_restart = time.time()
        await self._create_context_pages()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.shutdown()

    def _build_proxy_config(self):
        if not PROXY_SERVER:
            self._current_proxy_url = None
            return None
        username = PROXY_USER
        password_part = f":{PROXY_PASS}" if PROXY_PASS else ""
        self._current_proxy_url = f"http://{username}{password_part}@{PROXY_SERVER}:{PROXY_PORT}"
        return {
            "server": f"http://{PROXY_SERVER}:{PROXY_PORT}",
            "username": username,
            "password": PROXY_PASS,
        }

    def _build_context_headers(self, ua: str) -> dict:
        platform = get_platform(ua)
        is_mobile = "Mobile" in ua or "Android" in ua or "iPhone" in ua
        return {
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-CH-UA": get_sec_ch_ua(ua),
            "Sec-CH-UA-Mobile": "?1" if is_mobile else "?0",
            "Sec-CH-UA-Platform": PLATFORM_MAP.get(platform, '"Windows"'),
        }

    async def _apply_stealth(self, page):
        if not HAS_STEALTH or not page:
            return
        if stealth_async is not None:
            await stealth_async(page)
        elif _STEALTH is not None:
            await _STEALTH.apply_stealth_async(page)

    async def _new_stealth_page(self):
        page = await self.context.new_page()
        await self._apply_stealth(page)
        await self._block_unneeded_resources(page)
        return page

    async def _block_unneeded_resources(self, page):
        async def handle_route(route):
            request = route.request
            host = (urlparse(request.url).hostname or "").lower()
            should_block_host = any(
                host == domain or host.endswith(f".{domain}")
                for domain in BLOCKED_THIRD_PARTY_DOMAINS
            )
            if request.resource_type in BLOCKED_RESOURCE_TYPES or should_block_host:
                await route.abort()
                return
            await route.continue_()

        await page.route("**/*", handle_route)

    async def _create_context_pages(self):
        proxy_config = self._build_proxy_config()
        self._current_ua = random.choice(USER_AGENTS)
        self._current_headers = self._build_context_headers(self._current_ua)
        self._current_tournament_url = f"{BROWSER_BASE}/"
        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=self._current_ua,
            proxy=proxy_config,
            extra_http_headers=self._current_headers,
        )
        self.page = await self._new_stealth_page()
        self.api_page = await self._new_stealth_page()
        self._wire_network_capture()

    def _wire_network_capture(self):
        self._response_cache = {}
        self._response_waiters = {}
        if not self.context:
            return

        async def handle_response(response):
            try:
                url = response.url
                if not url.startswith(BROWSER_BASE + "/api/v1/"):
                    return
                path = url[len(BROWSER_BASE):]
                payload = {"status": response.status, "body": None}
                try:
                    payload["body"] = await response.json()
                except Exception as json_error:
                    payload["error"] = str(json_error)
                    try:
                        payload["body"] = await response.text()
                    except Exception:
                        payload["body"] = None
                self._response_cache[path] = payload
                waiter = self._response_waiters.pop(path, None)
                if waiter and not waiter.done():
                    waiter.set_result(payload)
            except Exception:
                return

        self.context.on("response", handle_response)

    async def _humanize_page(self):
        if not self.page:
            return
        try:
            await self.page.mouse.move(random.randint(150, 600), random.randint(120, 420), steps=random.randint(8, 20))
            await asyncio.sleep(random.uniform(0.2, 0.7))
            for _ in range(random.randint(1, 2)):
                await self.page.mouse.wheel(0, random.randint(250, 700))
                await asyncio.sleep(random.uniform(0.4, 1.2))
            if random.random() < 0.35:
                await self.page.mouse.wheel(0, -random.randint(120, 300))
                await asyncio.sleep(random.uniform(0.2, 0.6))
        except Exception:
            return

    async def _wait_for_captured_response(self, path: str, timeout_ms: int = 10000):
        if path in self._response_cache:
            return self._response_cache[path]
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._response_waiters[path] = fut
        try:
            return await asyncio.wait_for(fut, timeout_ms / 1000)
        except asyncio.TimeoutError:
            self._response_waiters.pop(path, None)
            return None

    async def shutdown(self):
        if self.api_page:
            try:
                await self.api_page.close()
            except Exception:
                pass
            self.api_page = None
        if self.page:
            try:
                await self.page.close()
            except Exception:
                pass
            self.page = None
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
            self.context = None
        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
            self.browser = None
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None

    @staticmethod
    def is_target_closed_error(exc: Exception) -> bool:
        message = str(exc)
        return (
            "Target page, context or browser has been closed" in message
            or "Target closed" in message
            or "Browser has been closed" in message
        )

    @staticmethod
    def is_transport_closed_error(exc: Exception) -> bool:
        message = str(exc)
        lowered = message.lower()
        return (
            "write epipe" in lowered
            or "econnreset" in lowered
            or "connection closed" in lowered
            or "pipe closed" in lowered
            or "transport closed" in lowered
            or "browser closed" in lowered
            or ("driver" in lowered and "closed" in lowered)
        )

    @staticmethod
    def is_navigation_context_error(exc: Exception) -> bool:
        message = str(exc)
        lowered = message.lower()
        return (
            "execution context was destroyed" in lowered
            or "interrupted by another navigation" in lowered
            or "frame was detached" in lowered
            or "cannot find context with specified id" in lowered
            or ("navigation" in lowered and "interrupted" in lowered)
        )

    @staticmethod
    def is_timeout_error(exc: Exception) -> bool:
        """Check if error is a Playwright TimeoutError (e.g. Page.goto timeout)."""
        return isinstance(exc, PlaywrightError) and "Timeout" in type(exc).__name__

    def is_recoverable_browser_error(self, exc: Exception) -> bool:
        return (
            self.is_target_closed_error(exc)
            or self.is_transport_closed_error(exc)
            or self.is_navigation_context_error(exc)
            or self.is_timeout_error(exc)
        )

    def set_event_context(self, event_id: Optional[int], attempt: Optional[int], action: str = "event"):
        self._current_event_id = event_id
        self._current_event_attempt = attempt
        self._current_action = action

    def clear_event_context(self):
        self._current_event_id = None
        self._current_event_attempt = None
        self._current_action = "idle"
        self._last_api_path = None

    def telemetry_snapshot(self) -> str:
        return (
            f"event={self._current_event_id} "
            f"event_attempt={self._current_event_attempt} "
            f"action={self._current_action} "
            f"path={self._last_api_path} "
            f"requests={self._request_count} heals={self._heal_count}"
        )

    async def rebuild(self, *, reason: str, warm_homepage: bool = True):
        self._heal_count += 1
        print(
            f"    ↻ Rebuilding browser client ({reason}) [heal #{self._heal_count}] "
            f"[{self.telemetry_snapshot()}]"
        )
        await self.shutdown()
        await self.__aenter__()
        if warm_homepage:
            await self.warm_homepage()

    async def maybe_restart_browser(self):
        if time.time() - self._last_browser_restart > BROWSER_RESTART_INTERVAL:
            print("    ↻ Periodic browser restart (30min interval)")
            await self.rebuild(reason="periodic restart")
            self._last_browser_restart = time.time()

    async def warm_homepage(self):
        """Load homepage to establish session cookies."""
        self._current_action = "warm_homepage"
        for attempt in range(2):
            try:
                print(f"    ↳ warm_homepage attempt {attempt+1}/2 [{self.telemetry_snapshot()}]")
                await self.page.goto(f"{BROWSER_BASE}/", timeout=30000, wait_until="domcontentloaded")
                await asyncio.sleep(2)
                await self._humanize_page()
                print(f"    ↳ warm_homepage ok [{self.telemetry_snapshot()}]")
                return
            except PlaywrightError as e:
                if self.is_recoverable_browser_error(e) and attempt == 0:
                    print(f"    ⚠ Browser transport issue during warm_homepage: {e} [{self.telemetry_snapshot()}]")
                    await self.rebuild(reason="browser transport issue during warm_homepage", warm_homepage=False)
                    continue
                raise

    async def warm_tournament(self, ut_id: int, season_id: int, country_slug: str = "", competition_slug: str = ""):
        """Load tournament page to get API access."""
        if country_slug and competition_slug:
            url = f"{BROWSER_BASE}/football/tournament/{country_slug}/{competition_slug}/{ut_id}#id:{season_id}"
        else:
            # Backward-compatible fallback for callers/configs without slugs.
            url = f"{BROWSER_BASE}/football/unique-tournament/{ut_id}/season/{season_id}"
        self._current_action = f"warm_tournament:{url}"
        for attempt in range(2):
            try:
                self._response_cache = {}
                print(f"    ↳ warm_tournament {url} attempt {attempt+1}/2 [{self.telemetry_snapshot()}]")
                await self.page.goto(url, timeout=30000, wait_until="domcontentloaded")
                await asyncio.sleep(3)
                await self._humanize_page()
                self._current_tournament_url = url
                print(f"    ↳ warm_tournament {url} ok [{self.telemetry_snapshot()}]")
                return
            except PlaywrightError as e:
                if self.is_recoverable_browser_error(e) and attempt == 0:
                    print(f"    ⚠ Browser transport issue during warm_tournament: {e} [{self.telemetry_snapshot()}]")
                    await self.rebuild(reason=f"browser transport issue during warm_tournament {url}")
                    continue
                raise

    async def warm_event(self, event_id: int):
        """Load event page."""
        self._current_action = "warm_event"
        self._current_event_id = event_id
        url = f"{BROWSER_BASE}/event/{event_id}"
        for attempt in range(2):
            try:
                self._response_cache = {}
                print(f"    ↳ warm_event {event_id} attempt {attempt+1}/2 [{self.telemetry_snapshot()}]")
                await self.page.goto(url, timeout=30000, wait_until="domcontentloaded")
                await asyncio.sleep(1.5)
                await self._humanize_page()
                print(f"    ↳ warm_event {event_id} ok [{self.telemetry_snapshot()}]")
                return
            except PlaywrightError as e:
                if self.is_recoverable_browser_error(e) and attempt == 0:
                    print(f"    ⚠ Browser transport issue during warm_event {event_id}: {e} [{self.telemetry_snapshot()}]")
                    await self.rebuild(reason=f"browser transport issue during warm_event {event_id}")
                    continue
                raise

    async def get_event_ssr(self, path: Optional[str] = None) -> Optional[dict]:
        """Extract the loaded event page's ``__NEXT_DATA__`` payload."""
        if not self.page:
            return None
        raw = await self.page.evaluate(
            """
            () => {
              const el = document.querySelector('#__NEXT_DATA__');
              if (!el || !el.textContent) return null;
              try { return JSON.parse(el.textContent); }
              catch (error) { return null; }
            }
            """
        )
        return self._map_ssr_to_api_format(raw, path)

    @staticmethod
    def _map_ssr_to_api_format(raw: Any, path: Optional[str] = None) -> Optional[dict]:
        """Map one endpoint's value in a Next.js hydration tree to API shape."""
        if not isinstance(raw, (dict, list)):
            return None
        match = re.search(r"/event/[^/]+/([^/?]+)", path or "")
        endpoint = match.group(1) if match else ("event" if "/event/" in (path or "") else None)
        aliases = {
            "event": ("event",), "incidents": ("incidents",),
            "lineups": ("lineups", "lineup"), "statistics": ("statistics", "stats"),
            "shotmap": ("shotmap", "shotMap"), "graph": ("graph",),
            "odds": ("odds",), "comments": ("comments",),
        }
        wanted = aliases.get(endpoint, (endpoint,)) if endpoint else ()

        def walk(value: Any) -> Optional[Any]:
            if isinstance(value, dict):
                for key in wanted:
                    if key in value and value[key] is not None:
                        return value[key]
                for child in value.values():
                    found = walk(child)
                    if found is not None:
                        return found
            elif isinstance(value, list):
                for child in value:
                    found = walk(child)
                    if found is not None:
                        return found
            return None

        value = walk(raw) if wanted else None
        if value is None:
            return None
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                return None
        if not isinstance(value, (dict, list)) or not value:
            return None
        return {"event": value} if endpoint == "event" else {endpoint: value}

    async def rotate_context(self):
        """Create a fresh browser context; sticky sessions keep the same IP/session unless session key changes."""
        if self.api_page:
            try:
                await self.api_page.close()
            except Exception:
                pass
        if self.page:
            try:
                await self.page.close()
            except Exception:
                pass
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass

        await self._create_context_pages()
        await self.warm_homepage()

    async def _browser_fetch_api(self, path: str, timeout_ms: int) -> dict:
        headers = {
            "Referer": self._current_tournament_url or f"{BROWSER_BASE}/",
            "User-Agent": self._current_ua,
            **self._current_headers,
        }
        await self.api_page.set_extra_http_headers(headers)
        response = await self.api_page.goto(
            f"{BROWSER_BASE}{path}",
            timeout=timeout_ms,
            wait_until="commit",
        )
        if response is None:
            return {"status": 0, "error": "no response returned"}
        status = response.status
        body = None
        error = None
        try:
            body = await response.json()
        except Exception as json_error:
            error = str(json_error)
            try:
                body = await self.api_page.evaluate("() => document.body.innerText")
            except Exception:
                body = None
        result = {"status": status, "body": body}
        if error:
            result["error"] = error
        return result

    async def _fetch_via_curl_cffi(self, path: str, timeout_ms: int) -> dict:
        if not HAS_CURL_CFFI or curl_requests is None:
            return {"status": 0, "error": "curl_cffi unavailable"}
        cookies = await self.context.cookies(BROWSER_BASE) if self.context else []
        cookie_dict = {
            cookie["name"]: cookie["value"]
            for cookie in cookies
            if cookie.get("name") and cookie.get("value") is not None
        }
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": self._current_headers.get("Accept-Language", "en-US,en;q=0.9"),
            "Origin": BROWSER_BASE,
            "Referer": self._current_tournament_url or f"{BROWSER_BASE}/",
            "User-Agent": self._current_ua,
            "Sec-CH-UA": self._current_headers.get("Sec-CH-UA", get_sec_ch_ua(self._current_ua)),
            "Sec-CH-UA-Mobile": self._current_headers.get("Sec-CH-UA-Mobile", "?0"),
            "Sec-CH-UA-Platform": self._current_headers.get("Sec-CH-UA-Platform", '"Windows"'),
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        proxies = {"https": self._current_proxy_url, "http": self._current_proxy_url} if self._current_proxy_url else None
        try:
            response = await asyncio.to_thread(
                curl_requests.get,
                f"{BROWSER_BASE}{path}",
                headers=headers,
                cookies=cookie_dict,
                proxies=proxies,
                impersonate="chrome",
                timeout=max(1, timeout_ms / 1000),
            )
        except Exception as request_error:
            return {"status": 0, "error": f"curl_cffi request failed: {request_error}"}
        body = None
        error = None
        try:
            body = response.json()
        except Exception as json_error:
            error = str(json_error)
            body = response.text
        result = {"status": response.status_code, "body": body, "transport": "curl_cffi"}
        if error:
            result["error"] = error
        return result

    async def fetch_api(self, path: str, timeout_ms: int = 30000, max_retries: int = 5, prefer_capture: bool = False) -> dict:
        """Fetch a SofaScoreet endpoint, rotating proxy on 403, falling back to SSR as last resort."""
        self._last_api_path = path
        last_result = {"status": 0, "error": "uninitialized"}
        print(f"    ↳ fetch_api start {path} prefer_capture={prefer_capture} proxy_tries={max_retries} [{self.telemetry_snapshot()}]")

        # ── Phase 1: network capture (prefer_capture) ──────────────────────────────────
        if prefer_capture and self._network_capture_enabled:
            captured = await self._wait_for_captured_response(path, timeout_ms=timeout_ms)
            if captured is not None:
                print(f"    ↳ capture hit for {path}: HTTP {captured.get('status')}")
                if captured.get("status") != 403:
                    self._consecutive_403 = 0
                    return captured
                self._consecutive_403 += 1
                print(f"    ↳ capture returned 403 for {path}, rotating proxy and retrying")
            print(f"    ↳ capture miss for {path}, falling back to direct fetch")

        # ── Phase 2: proxy rotation loop ───────────────────────────────────────────────
        for attempt in range(max_retries):
            self._request_count += 1
            self._current_action = f"fetch_api[{attempt+1}/{max_retries}]"

            # Rotate proxy before every retry (first attempt uses current context)
            if attempt > 0:
                print(f"    🔄 Rotating proxy [{attempt}/{max_retries}] [{self.telemetry_snapshot()}]")
                await self.rotate_context()
                await asyncio.sleep(random.uniform(1.5, 3.0))

            try:
                result = await self._fetch_via_curl_cffi(path, timeout_ms) if HAS_CURL_CFFI else {"status": 0, "error": "curl_cffi unavailable"}
                if result.get("status") == 403:
                    print(f"    ↳ curl_cffi returned 403 for {path}, falling back to browser fetch")
                    result = await self._browser_fetch_api(path, timeout_ms)
                elif result.get("status") == 0:
                    result = await self._browser_fetch_api(path, timeout_ms)
            except PlaywrightError as e:
                if self.is_recoverable_browser_error(e) and attempt + 1 < max_retries:
                    print(f"    ⚠ Recoverable browser error during fetch_api: {e} [{self.telemetry_snapshot()}]")
                    await self.rebuild(reason=f"recoverable browser error during fetch_api {path}")
                    continue
                raise

            last_result = result
            status = result.get("status")
            print(f"    ↳ fetch_api result {path}: HTTP {status} (proxy #{attempt+1}/{max_retries}) [{self.telemetry_snapshot()}]")
            if status != 403:
                self._consecutive_403 = 0
                return result

            self._consecutive_403 += 1

        # ── Phase 3: SSR fallback (all proxy rotations exhausted) ───────────────────────
        if last_result.get("status") == 403:
            ssr_result = await self.get_event_ssr(path)
            if ssr_result is not None:
                print(f"    ↳ SSR fallback hit for {path} (after {max_retries} proxy rotations)")
                self._consecutive_403 = 0
                return {"status": 200, "body": ssr_result, "transport": "ssr"}
            print(f"    ⚠ All {max_retries} proxies + SSR exhausted for {path}, giving up")
        return last_result

    async def sleep_between(self, min_s: float = 5.0, max_s: float = 10.0):
        """Random sleep between requests."""
        await asyncio.sleep(random.uniform(min_s, max_s))


class SmokeStatusReporter:
    def __init__(self, path: Path):
        self.path = path
        self.state = {
            "updated_at": _utc_now_iso(),
            "phase": "init",
            "competition": None,
            "season_id": None,
            "season_label": None,
            "event_id": None,
            "event_attempt": None,
            "action": None,
            "api_path": None,
            "http_status": None,
            "message": None,
            "requests": 0,
            "heals": 0,
            "done_count": 0,
            "failed_count": 0,
        }
        self.flush()

    def update(self, **kwargs):
        self.state.update(kwargs)
        self.state["updated_at"] = _utc_now_iso()
        self.flush()

    def flush(self):
        self.path.write_text(json.dumps(self.state, indent=2, ensure_ascii=False) + "\n")

async def _fetch_endpoint_result(client: BackfillClient, endpoint_name: str, eid: int, *, prefer_capture: bool = False):
    path = ENDPOINT_PATHS[endpoint_name].format(event_id=eid)
    phase = ENDPOINT_PHASE[endpoint_name]
    retry_counts = {"core": 3, "mid": 2, "high": 1}
    return await client.fetch_api(
        path,
        max_retries=retry_counts[phase],
        prefer_capture=prefer_capture,
    )


async def _process_non_event_endpoint(inserter, endpoint_name: str, eid: int, event_data: dict, result: dict) -> int:
    if result.get("status") != 200:
        return 0
    body = result.get("body", {})
    # Safety net: browser fallback may return the API body as a JSON string.
    if isinstance(body, str):
        import json as _json
        try:
            body = _json.loads(body)
        except Exception:
            pass
    if not isinstance(body, dict):
        return 0
    if endpoint_name in {"incidents", "lineups", "shotmap"} and isinstance(body, dict) and event_data:
        body.setdefault("homeTeam", event_data.get("homeTeam", {}))
        body.setdefault("awayTeam", event_data.get("awayTeam", {}))
    if endpoint_name == "incidents":
        return inserter.insert_incidents(eid, body)
    if endpoint_name == "lineups":
        return inserter.insert_lineups(eid, body)
    if endpoint_name == "statistics":
        return inserter.insert_statistics(eid, body)
    if endpoint_name == "shotmap":
        return inserter.insert_shotmap(eid, body)
    if endpoint_name == "graph":
        return inserter.insert_graph_points(eid, body)
    if endpoint_name == "odds":
        return inserter.insert_odds(eid, body)
    if endpoint_name == "comments":
        return inserter.insert_comments(eid, body)
    return 0


async def _run_endpoint_phase(client: BackfillClient, inserter, season_stats: dict, eid: int, event_data: dict,
                              endpoint_names: Sequence[str], *, prefer_capture: bool = False,
                              reporter: Optional[SmokeStatusReporter] = None):
    for endpoint_name in endpoint_names:
        phase = ENDPOINT_PHASE[endpoint_name]
        sleep_min, sleep_max = PHASE_SLEEP_RANGES[phase]
        print(f"    ↳ endpoint {endpoint_name} phase={phase} pre-sleep {sleep_min}-{sleep_max}s [event={eid}]")
        if reporter:
            reporter.update(phase="endpoint_pre_sleep", event_id=eid, action=f"endpoint:{endpoint_name}", api_path=ENDPOINT_PATHS[endpoint_name].format(event_id=eid), message=f"pre-sleep before {endpoint_name}", requests=client._request_count, heals=client._heal_count)
        await client.sleep_between(sleep_min, sleep_max)
        result = await _fetch_endpoint_result(client, endpoint_name, eid, prefer_capture=prefer_capture)
        status = result.get("status")
        if status == 403 and phase != "core":
            # Optional endpoints must not prevent the remaining bundle from completing.
            print(f"    ⚠ endpoint {endpoint_name} HTTP 403 after phase retries; skipping [event={eid}]")
        count = await _process_non_event_endpoint(inserter, endpoint_name, eid, event_data, result)
        print(f"    ↳ endpoint {endpoint_name} done http={status} rows={count} [event={eid}]")
        if reporter:
            reporter.update(phase="endpoint_done", event_id=eid, action=f"endpoint:{endpoint_name}", api_path=ENDPOINT_PATHS[endpoint_name].format(event_id=eid), http_status=status, message=f"{endpoint_name} rows={count}", requests=client._request_count, heals=client._heal_count)
        table_name = ENDPOINT_FETCH_LOG_TABLE[endpoint_name]
        inserter.log_fetch(
            table_name,
            eid,
            "success" if status == 200 else "error",
            count,
            None if status == 200 else f"HTTP {status}",
        )
        season_stats[ENDPOINT_COUNTER_KEY[endpoint_name]] += count
        if status == 403 and phase == "core":
            raise RuntimeError(f"{endpoint_name} HTTP 403 after core retries")


async def _process_event_with_retries(client: BackfillClient, inserter, progress, season_stats: dict,
                                      eid: int, sid: int, competition_id: int,
                                      *, max_attempts: int = 2,
                                      reporter: Optional[SmokeStatusReporter] = None) -> bool:
    last_error = None
    await client.maybe_restart_browser()
    for attempt in range(1, max_attempts + 1):
        client.set_event_context(eid, attempt)
        try:
            print(f"    ↳ process_event {eid} attempt {attempt}/{max_attempts} start [{client.telemetry_snapshot()}]")
            if reporter:
                reporter.update(phase="process_event", event_id=eid, event_attempt=attempt, action="process_event", message=f"start event {eid} attempt {attempt}", requests=client._request_count, heals=client._heal_count)
            if attempt > 1:
                print(f"    ↻ Retrying event {eid} (attempt {attempt}/{max_attempts})")

            if reporter:
                reporter.update(phase="warm_event", event_id=eid, event_attempt=attempt, action="warm_event", message=f"warming event {eid}", requests=client._request_count, heals=client._heal_count)
            await client.warm_event(eid)
            await client.sleep_between(*PHASE_SLEEP_RANGES["core"])

            if reporter:
                reporter.update(phase="fetch_event", event_id=eid, event_attempt=attempt, action="fetch_event", api_path=ENDPOINT_PATHS['event'].format(event_id=eid), message="fetching core event payload", requests=client._request_count, heals=client._heal_count)
            event_result = await _fetch_endpoint_result(client, "event", eid, prefer_capture=True)
            print(f"    ↳ event core payload http={event_result.get('status')} [event={eid}]")
            if reporter:
                reporter.update(phase="fetch_event_done", event_id=eid, event_attempt=attempt, action="fetch_event", api_path=ENDPOINT_PATHS['event'].format(event_id=eid), http_status=event_result.get('status'), message="core event payload finished", requests=client._request_count, heals=client._heal_count)
            if event_result.get("status") != 200:
                print(f"    ⚠ Event {eid}: HTTP {event_result.get('status')}")
                progress.mark_failed(eid, f"event HTTP {event_result.get('status')}")
                season_stats["failed"] += 1
                return False

            _ev_body = event_result.get("body", {})
            if isinstance(_ev_body, str):
                import json as _json
                try:
                    _ev_body = _json.loads(_ev_body)
                except Exception:
                    _ev_body = {}
            event_data = _ev_body.get("event", {}) if isinstance(_ev_body, dict) else {}
            if event_data:
                inserter.insert_match(event_data, sid, competition_id)

            await _run_endpoint_phase(client, inserter, season_stats, eid, event_data, CORE_ENDPOINTS[1:], prefer_capture=True, reporter=reporter)
            await _run_endpoint_phase(client, inserter, season_stats, eid, event_data, ("statistics",), prefer_capture=False, reporter=reporter)
            await _run_endpoint_phase(client, inserter, season_stats, eid, event_data, ("shotmap",), prefer_capture=True, reporter=reporter)
            await _run_endpoint_phase(client, inserter, season_stats, eid, event_data, HIGH_RISK_ENDPOINTS, prefer_capture=False, reporter=reporter)

            progress.mark_done(eid)
            season_stats["processed"] += 1
            if reporter:
                reporter.update(phase="done", event_id=eid, event_attempt=attempt, action="done", message=f"event {eid} processed", requests=client._request_count, heals=client._heal_count, done_count=progress.done_count, failed_count=len(progress._state.get('events_failed', [])))
            client.clear_event_context()
            return True
        except PlaywrightError as e:
            last_error = e
            if client.is_recoverable_browser_error(e) and attempt < max_attempts:
                print(f"    ⚠ Recoverable browser error while processing event {eid}: {e} [{client.telemetry_snapshot()}]")
                await client.rebuild(reason=f"recoverable browser error while processing event {eid}")
                continue
            break
        except Exception as e:
            last_error = e
            break

    print(f"    ❌ Event {eid} error: {last_error} [{client.telemetry_snapshot()}]")
    progress.mark_failed(eid, str(last_error))
    season_stats["failed"] += 1
    if reporter:
        reporter.update(phase="failed", event_id=eid, action="failed", message=str(last_error), requests=client._request_count, heals=client._heal_count, done_count=progress.done_count, failed_count=len(progress._state.get('events_failed', [])))
    client.clear_event_context()
    return False


def _dedupe_event_ids(event_ids: List[int]) -> List[int]:
    """Return event IDs in source order without duplicates."""
    seen = set()
    deduped = []
    for event_id in event_ids:
        if event_id in seen:
            continue
        seen.add(event_id)
        deduped.append(event_id)
    return deduped


def _extract_event_ids_from_events_body(body: Any) -> List[int]:
    if not isinstance(body, dict):
        return []
    events = body.get("events")
    if not isinstance(events, list):
        return []
    event_ids = []
    for event in events:
        if isinstance(event, dict) and event.get("id"):
            event_ids.append(int(event["id"]))
    return event_ids


def _extract_event_ids_from_cuptrees_body(body: Any) -> List[int]:
    if not isinstance(body, dict):
        return []
    event_ids = []
    for cup_tree in body.get("cupTrees", []) or []:
        if not isinstance(cup_tree, dict):
            continue
        for round_data in cup_tree.get("rounds", []) or []:
            if not isinstance(round_data, dict):
                continue
            for block in round_data.get("blocks", []) or []:
                if not isinstance(block, dict):
                    continue
                for event in block.get("events", []) or []:
                    if isinstance(event, dict) and event.get("id"):
                        event_ids.append(int(event["id"]))
                    elif isinstance(event, int):
                        event_ids.append(event)
    return _dedupe_event_ids(event_ids)


async def discover_season_event_ids(
    client: BackfillClient,
    ut_id: int,
    season_id: int,
    round_numbers: List[int],
    limit_rounds: int = 0,
) -> tuple[List[int], str]:
    """Discover event IDs for a season, including cup endpoints where round events 404."""
    if limit_rounds > 0:
        round_numbers = round_numbers[:limit_rounds]

    all_event_ids = []
    failed_round_statuses = []
    for rn in round_numbers:
        await client.sleep_between(1.0, 2.0)
        events_result = await client.fetch_api(
            f"/api/v1/unique-tournament/{ut_id}/season/{season_id}/events/round/{rn}"
        )
        status = events_result.get("status")
        if status != 200:
            print(f"  ⚠ Round {rn}: HTTP {status}")
            failed_round_statuses.append(status)
            continue
        all_event_ids.extend(_extract_event_ids_from_events_body(events_result.get("body")))

    all_event_ids = _dedupe_event_ids(all_event_ids)
    if all_event_ids:
        return all_event_ids, "rounds_events_round"

    # Knockout cups can expose bracket events through cuptrees while
    # /events/round/{round} returns 404 for every round.
    cuptrees_result = await client.fetch_api(
        f"/api/v1/unique-tournament/{ut_id}/season/{season_id}/cuptrees"
    )
    if cuptrees_result.get("status") == 200:
        cup_event_ids = _extract_event_ids_from_cuptrees_body(cuptrees_result.get("body"))
        if cup_event_ids:
            print(f"  ↳ Fallback cuptrees found {len(cup_event_ids)} events")
            return cup_event_ids, "cuptrees"
    else:
        print(f"  ⚠ Fallback cuptrees: HTTP {cuptrees_result.get('status')}")

    last_result = await client.fetch_api(
        f"/api/v1/unique-tournament/{ut_id}/season/{season_id}/events/last/0"
    )
    if last_result.get("status") == 200:
        last_event_ids = _dedupe_event_ids(_extract_event_ids_from_events_body(last_result.get("body")))
        if last_event_ids:
            print(f"  ↳ Fallback events/last/0 found {len(last_event_ids)} events")
            return last_event_ids, "events_last"
    else:
        print(f"  ⚠ Fallback events/last/0: HTTP {last_result.get('status')}")

    if failed_round_statuses:
        return [], f"no_events_round_statuses={sorted(set(failed_round_statuses))}"
    return [], "no_events"


# ── Data Inserter ─────────────────────────────────────────────────────────────

class DataInserter:
    """Insert fetched data into MySQL."""

    def __init__(self, mysql_conn):
        self.conn = mysql_conn

    def insert_match(self, event: dict, season_id: int, competition_id: int) -> int:
        """Insert or update a match, return match_id."""
        cur = self.conn.cursor()
        match_id = event.get("id")
        home_team = event.get("homeTeam", {})
        away_team = event.get("awayTeam", {})
        home_score = event.get("homeScore", {}).get("current")
        away_score = event.get("awayScore", {}).get("current")
        status = _normalize_match_status(event.get("status", {}).get("type", "scheduled"))
        round_info = event.get("roundInfo", {}).get("round")
        start_ts = event.get("startTimestamp")

        match_date = None
        match_time = None
        if start_ts:
            from datetime import datetime as dt
            dt_obj = dt.fromtimestamp(start_ts, tz=timezone.utc)
            match_date = dt_obj.strftime("%Y-%m-%d")
            match_time = dt_obj.strftime("%H:%M:%S")

        # Ensure teams
        if home_team.get("id"):
            ensure_team(
                self.conn,
                home_team["id"],
                home_team.get("name", ""),
                home_team.get("shortName", ""),
                home_team.get("countryCode"),
                home_team,
            )
        if away_team.get("id"):
            ensure_team(
                self.conn,
                away_team["id"],
                away_team.get("name", ""),
                away_team.get("shortName", ""),
                away_team.get("countryCode"),
                away_team,
            )

        attendance = event.get("attendance")
        referee_id = event.get("referee", {}).get("id")

        cur.execute(
            """INSERT INTO matches (match_id, season_id, competition_id, round_name, match_date, match_time,
                   home_team_id, away_team_id, home_score, away_score, status, attendance, referee_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON DUPLICATE KEY UPDATE
                   home_score=VALUES(home_score), away_score=VALUES(away_score),
                   status=VALUES(status), attendance=COALESCE(VALUES(attendance), attendance),
                   referee_id=COALESCE(VALUES(referee_id), referee_id), updated_at=NOW()
            """,
            (match_id, season_id, competition_id, round_info, match_date, match_time,
             home_team.get("id"), away_team.get("id"), home_score, away_score, status, attendance, referee_id),
        )
        self.conn.commit()
        return match_id

    @staticmethod
    def _incident_minute(incident: dict) -> int:
        """Return a NOT NULL minute for incident rows."""
        for key in ("time", "minute", "addedTime", "timeSeconds"):
            value = incident.get(key)
            if value is not None and value != "":
                try:
                    return int(float(value))
                except (TypeError, ValueError):
                    continue
        return 0

    def insert_incidents(self, match_id: int, data: dict):
        """Insert incidents for a match."""
        if not data or not isinstance(data, dict):
            return 0
        incidents = data.get("incidents", [])
        if not incidents:
            return 0

        cur = self.conn.cursor()
        count = 0
        for inc in incidents:
            inc_id = inc.get("id")
            if not inc_id:
                continue
            team = inc.get("team", {})
            player = inc.get("player", {})
            inc_type = inc.get("incidentType") or inc.get("type", "")

            type_map = {
                "goal": "goal",
                "card": "card",
                "substitution": "substitution",
                "period": "period",
                "varDecision": "var",
            }
            mapped_type = type_map.get(inc_type, "goal")
            if mapped_type not in ("goal", "card", "substitution", "period", "var"):
                mapped_type = "goal"

            period_map = {"1ST": "first", "2ND": "second", "ET1": "extra_first", "ET2": "extra_second"}
            period = period_map.get(inc.get("period", "").upper(), "first")

            goal_type = None
            card_type = None
            inc_class = inc.get("incidentClass", "")
            if mapped_type == "goal":
                gt_map = {"regular": "regular", "penalty": "penalty", "ownGoal": "own_goal", "freeKick": "free_kick"}
                goal_type = gt_map.get(inc_class)
            elif mapped_type == "card":
                ct_map = {"yellow": "yellow", "red": "red", "yellowRed": "yellow_red"}
                card_type = ct_map.get(inc_class)

            home_team = data.get("homeTeam", {})
            away_team = data.get("awayTeam", {})
            team_id = team.get("id") if team else None

            # SofaScore incidents don't have a nested "team" object; they use
            # "isHome" to indicate which side the incident belongs to.
            # Derive team_id from the match's homeTeam/awayTeam + isHome.
            if team_id is None:
                if inc.get("isHome") is True:
                    team_id = home_team.get("id")
                elif inc.get("isHome") is False:
                    team_id = away_team.get("id")

            # Period markers, injuryTime, and other system rows may have no
            # team at all. The current schema requires team_id NOT NULL, so
            # skip those.
            if team_id is None:
                continue

            is_home = 1 if team_id == home_team.get("id") else 0

            if player.get("id"):
                ensure_player(
                    self.conn,
                    player["id"],
                    player.get("name", ""),
                    player.get("shortName", ""),
                    player.get("position"),
                    player.get("countryCode"),
                    player,
                    team_id,
                )

            cur.execute(
                """INSERT INTO match_incidents
                       (incident_id, match_id, team_id, player_id, incident_type, minute,
                        period, is_home, goal_type, card_type, incident_text, reason)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON DUPLICATE KEY UPDATE incident_text=VALUES(incident_text)
                """,
                (inc_id, match_id, team_id, player.get("id"), mapped_type,
                 self._incident_minute(inc), period, is_home, goal_type, card_type,
                 inc.get("text"), inc.get("reason")),
            )
            count += 1
        self.conn.commit()
        return count

    def insert_lineups(self, match_id: int, data: dict):
        """Insert lineups for a match."""
        if not data or not isinstance(data, dict):
            return 0
        home = data.get("home", {})
        away = data.get("away", {})
        count = 0
        cur = self.conn.cursor()

        for is_home, team_data in [(1, home), (0, away)]:
            # SofaScore lineups don't have a nested "team" object at the
            # home/away level.  Derive team_id from the first player's
            # teamId field, or from the injected homeTeam/awayTeam.
            team = team_data.get("team", {})
            team_id = team.get("id") if team else None

            if team_id is None:
                # Try player-level teamId (all players on same side share it)
                players = team_data.get("players", [])
                if players:
                    team_id = players[0].get("teamId")

            if team_id is None:
                # Fallback to injected homeTeam/awayTeam from event_data
                if is_home:
                    team_id = data.get("homeTeam", {}).get("id")
                else:
                    team_id = data.get("awayTeam", {}).get("id")

            team_payload = team if team else (data.get("homeTeam", {}) if is_home else data.get("awayTeam", {}))
            if team_id:
                ensure_team(
                    self.conn,
                    team_id,
                    _first_non_empty(team_payload.get("name"), team.get("name", ""), ""),
                    _first_non_empty(team_payload.get("shortName"), team.get("shortName", "")),
                    team_payload.get("countryCode"),
                    team_payload,
                )
            else:
                # Schema requires team_id NOT NULL; skip this side if absent
                continue

            for player_entry in team_data.get("players", []):
                p = player_entry.get("player", {})
                if not p.get("id"):
                    continue

                ensure_player(
                    self.conn,
                    p["id"],
                    p.get("name", ""),
                    p.get("shortName", ""),
                    p.get("position"),
                    p.get("countryCode"),
                    p,
                    team_id,
                )

                pos_cat = _first_non_empty(player_entry.get("position"), p.get("position"), "")
                pos_map = {"G": "GK", "D": "DEF", "M": "MID", "F": "FWD"}
                position_category = pos_map.get(pos_cat, "MID")
                jersey_number = _first_non_empty(player_entry.get("jerseyNumber"), p.get("jerseyNumber"))
                is_starter = 0 if player_entry.get("substitute") is True else 1
                is_captain = 1 if player_entry.get("captain") or p.get("captain") else 0
                stats = player_entry.get("statistics") or {}
                rating = stats.get("rating")
                minutes_played = _first_non_empty(stats.get("minutesPlayed"), player_entry.get("minutesPlayed"), player_entry.get("minutes"), 0)

                cur.execute(
                    """INSERT INTO match_lineups
                           (match_id, team_id, player_id, is_home, is_starter,
                            jersey_number, position, position_category, is_captain, minutes_played, rating)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON DUPLICATE KEY UPDATE
                           is_starter=VALUES(is_starter),
                           jersey_number=COALESCE(VALUES(jersey_number), jersey_number),
                           position=COALESCE(VALUES(position), position),
                           position_category=VALUES(position_category),
                           is_captain=VALUES(is_captain),
                           minutes_played=VALUES(minutes_played),
                           rating=COALESCE(VALUES(rating), rating)
                    """,
                    (match_id, team_id, p["id"], is_home,
                     is_starter, jersey_number, pos_cat, position_category,
                     is_captain, minutes_played, rating),
                )
                self.insert_player_stats(match_id, player_entry, team_id, is_home, commit=False)
                count += 1
        self.conn.commit()
        return count

    def insert_player_stats(self, match_id: int, player_entry: dict, team_id: int, is_home: int, commit: bool = True):
        """Insert per-player statistics from a lineups player entry."""
        if not player_entry or not isinstance(player_entry, dict):
            return 0
        player = player_entry.get("player", {})
        player_id = player.get("id")
        if not player_id:
            return 0
        stats = player_entry.get("statistics") or {}
        if not isinstance(stats, dict):
            stats = {}

        values = {
            "match_id": match_id,
            "team_id": team_id,
            "player_id": player_id,
            "is_home": is_home,
        }
        for column, json_key in LEGACY_PLAYER_STAT_MAPPING.items():
            values[column] = stats.get(json_key)
        values["minutes_played"] = _first_non_empty(values.get("minutes_played"), player_entry.get("minutesPlayed"), player_entry.get("minutes"), 0)
        for column, json_key in PLAYER_STAT_JSON_KEYS.items():
            values[column] = stats.get(json_key)

        columns = list(values.keys())
        placeholders = ", ".join(["%s"] * len(columns))
        update_cols = [c for c in columns if c not in ("match_id", "player_id")]
        update_sql = ", ".join(f"{c}=VALUES({c})" for c in update_cols)
        cur = self.conn.cursor()
        cur.execute(
            f"""INSERT INTO match_player_stats ({', '.join(columns)})
                VALUES ({placeholders})
                ON DUPLICATE KEY UPDATE {update_sql}
            """,
            [values[c] for c in columns],
        )
        if commit:
            self.conn.commit()
        return 1

    def insert_statistics(self, match_id: int, data: dict):
        """Insert match statistics (key-value pairs)."""
        if not data or not isinstance(data, dict):
            return 0
        count = 0
        cur = self.conn.cursor()

        # SofaScore statistics API returns {"statistics": [{"period": ..., "groups": [...]}]}
        # where each group has "groupName" and "statisticsItems" (not "statistics").
        stats_periods = data.get("statistics", []) or data.get("periods", [])
        for period in stats_periods:
            group_name = period.get("period", "ALL")
            for stat in period.get("groups", []):
                stat_group = stat.get("groupName", group_name)
                # API uses "statisticsItems"; older code expected "statistics"
                stat_items = stat.get("statisticsItems", []) or stat.get("statistics", [])
                for s in stat_items:
                    name = s.get("name")
                    if not name:
                        continue
                    home_val = s.get("home")
                    away_val = s.get("away")
                    home_num = None
                    away_num = None
                    try:
                        home_num = float(str(home_val).replace("%", "").replace(",", ""))
                    except (ValueError, TypeError):
                        pass
                    try:
                        away_num = float(str(away_val).replace("%", "").replace(",", ""))
                    except (ValueError, TypeError):
                        pass

                    cur.execute(
                        """INSERT INTO match_statistics
                               (match_id, stat_group, stat_name, home_value, away_value,
                                home_value_num, away_value_num)
                           VALUES (%s, %s, %s, %s, %s, %s, %s)
                           ON DUPLICATE KEY UPDATE
                               home_value=VALUES(home_value), away_value=VALUES(away_value)
                        """,
                        (match_id, stat_group, name, str(home_val) if home_val is not None else None,
                         str(away_val) if away_val is not None else None, home_num, away_num),
                    )
                    count += 1
        self.conn.commit()
        return count

    def insert_shotmap(self, match_id: int, data: dict):
        """Insert shotmap entries."""
        if not data or not isinstance(data, dict):
            return 0
        shots = data.get("shotmap", [])
        if not shots:
            return 0
        count = 0
        cur = self.conn.cursor()

        for shot in shots:
            shot_id = shot.get("id")
            if not shot_id:
                continue
            player = shot.get("player", {})
            home_team = data.get("homeTeam", {})
            away_team = data.get("awayTeam", {})
            team_id = shot.get("teamId")

            # Older/newer SofaScore payloads may expose only isHome without an
            # embedded team object. Fall back to match teams when possible.
            if team_id is None:
                if shot.get("isHome") is True:
                    team_id = home_team.get("id")
                elif shot.get("isHome") is False:
                    team_id = away_team.get("id")

            if team_id is None:
                continue

            if player.get("id"):
                ensure_player(
                    self.conn,
                    player["id"],
                    player.get("name", ""),
                    player.get("shortName", ""),
                    player.get("position"),
                    player.get("countryCode"),
                    player,
                    team_id,
                )

            is_home = 1 if shot.get("isHome") else 0

            player_coords = shot.get("playerCoordinates") or {}
            goal_coords = shot.get("goalMouthCoordinates") or {}
            block_coords = shot.get("blockCoordinates") or {}
            goalkeeper = shot.get("goalkeeper") or {}

            cur.execute(
                """INSERT INTO match_shotmap
                       (shot_id, match_id, team_id, player_id, is_home, minute,
                        incident_type, shot_type, situation, body_part,
                        player_x, player_y, player_z, xg, xgot, is_goal,
                        goal_mouth_location, goal_mouth_x, goal_mouth_y, goal_mouth_z,
                        block_x, block_y, block_z, goalkeeper_id, goalkeeper_name,
                        added_time, time_seconds, period_time_seconds)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON DUPLICATE KEY UPDATE
                       incident_type=VALUES(incident_type), shot_type=VALUES(shot_type), situation=VALUES(situation),
                       body_part=VALUES(body_part), player_x=VALUES(player_x), player_y=VALUES(player_y),
                       player_z=VALUES(player_z), xg=VALUES(xg), xgot=VALUES(xgot), is_goal=VALUES(is_goal),
                       goal_mouth_location=VALUES(goal_mouth_location), goal_mouth_x=VALUES(goal_mouth_x),
                       goal_mouth_y=VALUES(goal_mouth_y), goal_mouth_z=VALUES(goal_mouth_z),
                       block_x=VALUES(block_x), block_y=VALUES(block_y), block_z=VALUES(block_z),
                       goalkeeper_id=VALUES(goalkeeper_id), goalkeeper_name=VALUES(goalkeeper_name),
                       added_time=VALUES(added_time), time_seconds=VALUES(time_seconds), period_time_seconds=VALUES(period_time_seconds)
                """,
                (shot_id, match_id, team_id, player.get("id"), is_home,
                 shot.get("time"), shot.get("incidentType"), shot.get("shotType"),
                 shot.get("situation"), shot.get("bodyPart"),
                 player_coords.get("x"), player_coords.get("y"), player_coords.get("z"),
                 shot.get("xg"), shot.get("xgot"), 1 if shot.get("isGoal") else 0,
                 shot.get("goalMouthLocation"), goal_coords.get("x"), goal_coords.get("y"), goal_coords.get("z"),
                 block_coords.get("x"), block_coords.get("y"), block_coords.get("z"),
                 goalkeeper.get("id"), goalkeeper.get("name"),
                 shot.get("addedTime"), shot.get("timeSeconds"), shot.get("periodTimeSeconds")),
            )
            count += 1
        self.conn.commit()
        return count

    def insert_graph_points(self, match_id: int, data: dict):
        """Insert SofaScore momentum graph points."""
        if not data or not isinstance(data, dict):
            return 0
        points = data.get("graphPoints", [])
        if not points:
            return 0
        cur = self.conn.cursor()
        count = 0
        cur.execute("DELETE FROM match_graph_points WHERE match_id = %s", (match_id,))
        for point in points:
            minute = point.get("minute")
            value = point.get("value")
            period = 1 if minute is not None and float(minute) <= 45 else 2
            cur.execute(
                """INSERT INTO match_graph_points (match_id, period, minute, value)
                   VALUES (%s, %s, %s, %s)
                """,
                (match_id, period, int(float(minute)) if minute is not None else None, value),
            )
            count += 1
        self.conn.commit()
        return count

    def insert_odds(self, match_id: int, data: dict):
        """Insert odds markets, one row per market choice."""
        if not data or not isinstance(data, dict):
            return 0
        markets = data.get("markets", [])
        if not markets:
            return 0
        cur = self.conn.cursor()
        count = 0
        cur.execute("DELETE FROM match_odds WHERE match_id = %s", (match_id,))
        for market in markets:
            for choice in market.get("choices", []) or []:
                cur.execute(
                    """INSERT INTO match_odds
                           (match_id, market_id, market_name, market_group, market_period,
                            structure_type, suspended, choice_name, initial_fractional_value,
                            fractional_value, winning)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (match_id, market.get("marketId"), market.get("marketName"), market.get("marketGroup"),
                     market.get("marketPeriod"), market.get("structureType"), 1 if market.get("suspended") else 0,
                     choice.get("name"), choice.get("initialFractionalValue"), choice.get("fractionalValue"),
                     1 if choice.get("winning") else 0),
                )
                count += 1
        self.conn.commit()
        return count

    def insert_comments(self, match_id: int, data: dict):
        """Insert match comments."""
        if not data or not isinstance(data, dict):
            return 0
        comments = data.get("comments", [])
        if not comments:
            return 0
        cur = self.conn.cursor()
        count = 0
        cur.execute("DELETE FROM match_comments WHERE match_id = %s", (match_id,))
        for comment in comments:
            cur.execute(
                """INSERT INTO match_comments
                       (match_id, sequence, comment_type, comment_text, notable_actions)
                   VALUES (%s, %s, %s, %s, %s)
                """,
                (match_id, comment.get("sequence"), comment.get("type"), comment.get("text"),
                 json.dumps(comment.get("notableActions"), ensure_ascii=False) if comment.get("notableActions") is not None else None),
            )
            count += 1
        self.conn.commit()
        return count

    def log_fetch(self, table_name: str, match_id: int, status: str, rows: int = 0,
                  error: str = None, duration_ms: int = None, source: str = "browser"):
        cur = self.conn.cursor()
        cur.execute(
            """INSERT INTO fetch_log (table_name, match_id, operation, status, rows_affected,
                   error_message, duration_ms, source)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (table_name, match_id, "upsert", status, rows, error, duration_ms, source),
        )
        self.conn.commit()


# ── Main Backfill Logic ──────────────────────────────────────────────────────

async def backfill_competition(
    name: str,
    ut_id: int,
    cat_id: int,
    season_mode: str,
    seasons: dict,  # {sid_str: year_label} or {sid_str: season metadata}
    from_year: int = 2015,
    to_year: int = 0,
    season_id: Optional[int] = None,
    limit_rounds: int = 0,
    limit_events: int = 0,
    dry_run: bool = False,
    headless: bool = True,
    sticky_session_key: Optional[str] = None,
    country_slug: str = "",
    competition_slug: str = "",
) -> dict:
    """Backfill one competition."""
    import re

    mysql_conn = get_mysql_conn()
    inserter = DataInserter(mysql_conn)
    competition_id = ensure_competition(mysql_conn, name, cat_id, ut_id, season_mode, "")

    # Filter seasons to 10yr window
    def season_start_year(label: str) -> int:
        m = re.match(r'^(\d{2})/(\d{2})$', label)
        if m:
            yy = int(m.group(1))
            return 2000 + yy if yy < 50 else 1900 + yy
        m = re.match(r'^(\d{4})', label)
        if m:
            return int(m.group(1))
        return 0

    target_seasons = {}
    for sid, season_data in seasons.items():
        if isinstance(season_data, dict):
            label = str(season_data.get("year", season_data.get("name", sid)))
        else:
            label = str(season_data)
        if season_start_year(label) >= from_year and (to_year == 0 or season_start_year(label) <= to_year):
            target_seasons[int(sid)] = season_data
    if season_id is not None:
        target_seasons = {sid: label for sid, label in target_seasons.items() if sid == season_id}
        if not target_seasons:
            target_seasons = {season_id: str(season_id)}

    print(f"\n{'='*60}")
    print(f"🏆 {name} — {len(target_seasons)} seasons ({from_year}-{to_year if to_year else 'now'})")
    print(f"{'='*60}")

    results = {"competition": name, "seasons": {}}
    status_path = STATUS_DIR / f"smoke_status_{name.replace(' ','_')}.json"
    reporter = SmokeStatusReporter(status_path)
    reporter.update(phase="start", competition=name, message="starting backfill competition")

    async with BackfillClient(headless=headless, sticky_session_key=sticky_session_key) as client:
        # Warm homepage
        reporter.update(phase="warm_homepage", action="warm_homepage", message="warming homepage", requests=client._request_count, heals=client._heal_count)
        await client.warm_homepage()
        reporter.update(phase="warm_homepage_done", action="warm_homepage", message="homepage warmed", requests=client._request_count, heals=client._heal_count)
        print(f"✅ Session warmed")

        for sid, season_data in sorted(target_seasons.items()):
            label = str(season_data.get("year", season_data.get("name", sid))) if isinstance(season_data, dict) else str(season_data)
            print(f"\n📅 Season {label} (SID={sid})")
            reporter.update(phase="season_start", season_id=sid, season_label=label, action="season_start", message=f"season {label} start")

            if dry_run:
                print(f"  [DRY] Would process season {label}")
                results["seasons"][sid] = {"status": "dry_run"}
                continue

            # Ensure season in DB
            season_payload = None
            reporter.update(phase="season_meta", season_id=sid, season_label=label, api_path=f"/api/v1/unique-tournament/{ut_id}/season/{sid}", action="season_meta", message="fetching season metadata", requests=client._request_count, heals=client._heal_count)
            season_result = await client.fetch_api(f"/api/v1/unique-tournament/{ut_id}/season/{sid}")
            reporter.update(phase="season_meta_done", season_id=sid, season_label=label, api_path=f"/api/v1/unique-tournament/{ut_id}/season/{sid}", action="season_meta", http_status=season_result.get('status'), message="season metadata fetched", requests=client._request_count, heals=client._heal_count)
            if season_result.get("status") == 200:
                season_body = season_result.get("body", {})
                season_payload = season_body.get("season") if isinstance(season_body, dict) else None
            ensure_season(mysql_conn, sid, competition_id, label, season_payload)

            # State file for resume
            state_path = DATA_DIR / f"progress_{name.replace(' ','_')}_{sid}.json"
            progress = ProgressTracker(state_path)

            # Warm tournament page
            try:
                reporter.update(phase="warm_tournament", season_id=sid, season_label=label, action="warm_tournament", message=f"warming tournament {ut_id}/{sid}", requests=client._request_count, heals=client._heal_count)
                await client.warm_tournament(ut_id, sid, country_slug, competition_slug)
                reporter.update(phase="warm_tournament_done", season_id=sid, season_label=label, action="warm_tournament", message=f"tournament {country_slug}/{competition_slug}/{ut_id}#id:{sid} warmed", requests=client._request_count, heals=client._heal_count)
            except Exception as e:
                print(f"  ❌ Failed to warm tournament page: {e}")
                results["seasons"][sid] = {"status": "error", "reason": str(e)}
                continue

            # Get rounds
            reporter.update(phase="rounds", season_id=sid, season_label=label, api_path=f"/api/v1/unique-tournament/{ut_id}/season/{sid}/rounds", action="rounds", message="fetching rounds", requests=client._request_count, heals=client._heal_count)
            rounds_result = await client.fetch_api(f"/api/v1/unique-tournament/{ut_id}/season/{sid}/rounds")
            reporter.update(phase="rounds_done", season_id=sid, season_label=label, api_path=f"/api/v1/unique-tournament/{ut_id}/season/{sid}/rounds", action="rounds", http_status=rounds_result.get('status'), message="rounds fetched", requests=client._request_count, heals=client._heal_count)
            if rounds_result.get("status") != 200:
                print(f"  ❌ Failed to get rounds: HTTP {rounds_result.get('status')}")
                results["seasons"][sid] = {"status": "error", "reason": f"rounds HTTP {rounds_result.get('status')}"}
                continue

            rounds_data = rounds_result.get("body", {})
            round_numbers = [r.get("round") for r in rounds_data.get("rounds", []) if r.get("round")]
            total_rounds = len(round_numbers)
            print(f"  {total_rounds} rounds found")

            # Collect all event IDs
            if limit_rounds > 0:
                print(f"  (Limited to first {limit_rounds} rounds)")
            all_event_ids, discovery_method = await discover_season_event_ids(
                client, ut_id, sid, round_numbers, limit_rounds=limit_rounds
            )

            print(f"  {len(all_event_ids)} events to process via {discovery_method} (already done: {progress.done_count})")
            reporter.update(phase="event_discovery_done", season_id=sid, season_label=label, action="event_discovery", message=f"{len(all_event_ids)} events via {discovery_method}", done_count=progress.done_count, failed_count=len(progress._state.get('events_failed', [])), requests=client._request_count, heals=client._heal_count)

            if limit_events > 0:
                all_event_ids = all_event_ids[:limit_events]
                print(f"  (Limited to {limit_events} events)")

            # Process each event
            season_stats = {"processed": 0, "skipped": 0, "failed": 0, "incidents": 0, "lineups": 0, "statistics": 0, "shotmap": 0, "graph": 0, "odds": 0, "comments": 0}

            for i, eid in enumerate(all_event_ids):
                if progress.is_done(eid):
                    season_stats["skipped"] += 1
                    continue

                ok = await _process_event_with_retries(
                    client,
                    inserter,
                    progress,
                    season_stats,
                    eid,
                    sid,
                    competition_id,
                    reporter=reporter,
                )

                if ok and ((i + 1) % 10 == 0 or (i + 1) == len(all_event_ids)):
                    print(
                        f"    [{i+1}/{len(all_event_ids)}] done | "
                        f"processed:{season_stats['processed']} failed:{season_stats['failed']} "
                        f"heals:{client._heal_count} requests:{client._request_count}"
                    )

            print(f"  ✅ Season {label}: {season_stats['processed']} processed, {season_stats['skipped']} skipped, {season_stats['failed']} failed")
            reporter.update(phase="season_done", season_id=sid, season_label=label, action="season_done", message=f"processed={season_stats['processed']} skipped={season_stats['skipped']} failed={season_stats['failed']}", done_count=progress.done_count, failed_count=len(progress._state.get('events_failed', [])), requests=client._request_count, heals=client._heal_count)
            results["seasons"][sid] = season_stats

    reporter.update(phase="complete", action="complete", message="competition finished")
    mysql_conn.close()
    return results


def run_test_match_json(path: str) -> dict:
    """Load a captured full-match JSON bundle and run all parse inserts."""
    bundle = json.loads(Path(path).read_text())
    event = bundle.get("event", {}).get("event", bundle.get("event", {}))
    if not event or not event.get("id"):
        raise ValueError(f"No event payload found in {path}")

    conn = get_mysql_conn()
    inserter = DataInserter(conn)
    match_id = event["id"]
    tournament = event.get("tournament", {}) or {}
    unique_tournament = tournament.get("uniqueTournament", {}) or tournament
    category = unique_tournament.get("category", {}) or tournament.get("category", {}) or {}
    season = event.get("season", {}) or {}
    competition_id = unique_tournament.get("id") or tournament.get("id") or 0
    season_id = season.get("id") or 0

    ensure_competition(conn, unique_tournament.get("name", tournament.get("name", "Unknown")), category.get("id", 0), competition_id, "league", category.get("country", {}).get("alpha3", ""))
    ensure_season(conn, season_id, competition_id, season.get("year") or season.get("name") or "unknown", season)
    inserter.insert_match(event, season_id, competition_id)

    for key in ("lineups", "incidents", "shotmap"):
        if isinstance(bundle.get(key), dict):
            bundle[key].setdefault("homeTeam", event.get("homeTeam", {}))
            bundle[key].setdefault("awayTeam", event.get("awayTeam", {}))

    counts = {
        "match_lineups": inserter.insert_lineups(match_id, bundle.get("lineups", {})),
        "match_incidents": inserter.insert_incidents(match_id, bundle.get("incidents", {})),
        "match_statistics": inserter.insert_statistics(match_id, bundle.get("statistics", {})),
        "match_shotmap": inserter.insert_shotmap(match_id, bundle.get("shotmap", {})),
        "match_graph_points": inserter.insert_graph_points(match_id, bundle.get("graph", {})),
        "match_odds": inserter.insert_odds(match_id, bundle.get("odds", {})),
        "match_comments": inserter.insert_comments(match_id, bundle.get("comments", {})),
    }

    row_counts = {}
    cur = conn.cursor()
    for table in ["match_player_stats", "match_shotmap", "match_graph_points", "match_odds", "match_comments"]:
        cur.execute(f"SELECT COUNT(*) FROM {table} WHERE match_id = %s", (match_id,))
        row_counts[table] = cur.fetchone()[0]
    conn.close()
    return {"match_id": match_id, "insert_counts": counts, "row_counts": row_counts}


# ── CLI ───────────────────────────────────────────────────────────────────────

def load_competitions() -> dict:
    """Load competitions from YAML, discoveries.json, and verified season URLs."""
    try:
        import yaml
        with open(COMPETITIONS_FILE) as f:
            data = yaml.safe_load(f)
        yaml_comps = {c["name"]: c for c in data.get("competitions", [])}
    except Exception:
        yaml_comps = {}

    # Load discovered season IDs
    discoveries = {}
    if DISCOVERIES_FILE.exists():
        discoveries = json.loads(DISCOVERIES_FILE.read_text())

    verified = {}
    if SEASON_URLS_FILE.exists():
        try:
            verified = {
                item["name"]: item
                for item in json.loads(SEASON_URLS_FILE.read_text()).get("competitions", [])
                if item.get("name")
            }
        except (OSError, json.JSONDecodeError, TypeError):
            verified = {}

    # Merge
    result = {}
    for name, cfg in yaml_comps.items():
        disc = discoveries.get(name, {})
        verified_cfg = verified.get(name, {})
        seasons = {}
        if isinstance(verified_cfg.get("seasons"), list):
            for season in verified_cfg["seasons"]:
                if not isinstance(season, dict) or season.get("id") is None:
                    continue
                sid = str(season["id"])
                seasons[sid] = {
                    "year": season.get("year", sid),
                    "id": season["id"],
                    "url": season.get("url"),
                }
        if not seasons:
            seasons = disc.get("seasons", {})
        result[name] = {
            "ut_id": cfg.get("ut_id"),
            "cat_id": cfg.get("category_id"),
            "season_mode": cfg.get("season_mode", "national"),
            "country_slug": verified_cfg.get("country_slug", cfg.get("country_slug", "")),
            "competition_slug": verified_cfg.get("competition_slug", cfg.get("competition_slug", "")),
            "seasons": seasons,
        }
    return result


async def main():
    ap = argparse.ArgumentParser(description="SofaScore Backfill Runner")
    ap.add_argument("--all", action="store_true", help="Run all competitions")
    ap.add_argument("--competition", type=str, help="Single competition name")
    ap.add_argument("--from-year", type=int, default=2015, help="Start year (default 2015)")
    ap.add_argument("--to-year", type=int, default=0, help="End year (0=no limit, e.g. 2024)")
    ap.add_argument("--season-id", type=int, help="Restrict to one SofaScore season ID")
    ap.add_argument("--limit-rounds", type=int, default=0, help="Limit rounds per season (0=all)")
    ap.add_argument("--limit-events", type=int, default=0, help="Limit events (0=all)")
    ap.add_argument("--dry-run", action="store_true", help="Show what would run")
    ap.add_argument("--headful", action="store_true", help="Run browser with visible UI for testing")
    ap.add_argument("--sticky-session-key", type=str, help="Override sticky proxy session key")
    ap.add_argument("--test-match-json", type=str, help="Run parser against a captured full-match JSON bundle")
    args = ap.parse_args()

    if args.test_match_json:
        result = run_test_match_json(args.test_match_json)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    competitions = load_competitions()

    if args.competition:
        if args.competition not in competitions:
            print(f"❌ Unknown competition: {args.competition}")
            print(f"Available: {', '.join(sorted(competitions.keys()))}")
            return
        comp = competitions[args.competition]
        session_key = args.sticky_session_key or f"{PROXY_SESSION_PREFIX}-{args.competition.lower().replace(' ', '-') }"
        result = await backfill_competition(
            name=args.competition,
            ut_id=comp["ut_id"],
            cat_id=comp["cat_id"],
            season_mode=comp["season_mode"],
            seasons=comp["seasons"],
            country_slug=comp.get("country_slug", ""),
            competition_slug=comp.get("competition_slug", ""),
            from_year=args.from_year,
            to_year=args.to_year,
            season_id=args.season_id,
            limit_rounds=args.limit_rounds,
            limit_events=args.limit_events,
            dry_run=args.dry_run,
            headless=not args.headful,
            sticky_session_key=session_key,
        )
        print(f"\n{json.dumps(result, indent=2, ensure_ascii=False)}")

    elif args.all:
        total = len(competitions)
        for i, (name, comp) in enumerate(sorted(competitions.items()), 1):
            if not comp["seasons"]:
                print(f"\n⏭ [{i}/{total}] {name}: no seasons discovered, skipping")
                continue
            print(f"\n[{i}/{total}] {name}")
            session_key = args.sticky_session_key or f"{PROXY_SESSION_PREFIX}-{name.lower().replace(' ', '-') }"
            result = await backfill_competition(
                name=name,
                ut_id=comp["ut_id"],
                cat_id=comp["cat_id"],
                season_mode=comp["season_mode"],
                seasons=comp["seasons"],
                country_slug=comp.get("country_slug", ""),
                competition_slug=comp.get("competition_slug", ""),
                from_year=args.from_year,
                to_year=args.to_year,
                limit_rounds=args.limit_rounds,
                limit_events=args.limit_events,
                dry_run=args.dry_run,
                headless=not args.headful,
                sticky_session_key=session_key,
            )
            # Small delay between competitions
            await asyncio.sleep(5)
    else:
        print("Usage: backfill_runner.py --all | --competition NAME [--from-year 2015] [--to-year 2024] [--limit-rounds N] [--dry-run]")
        print("       backfill_runner.py --test-match-json match_14023966_full.json")
        print(f"\nAvailable competitions: {', '.join(sorted(competitions.keys()))}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⏹ Interrupted by user")
    except Exception as e:
        print(f"\n💥 Fatal error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
