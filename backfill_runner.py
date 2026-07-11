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
from typing import Any, Dict, List, Optional


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
DATA_DIR.mkdir(parents=True, exist_ok=True)

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

# Load .env
load_dotenv(WORKDIR / ".env")
if not PROXY_PASS:
    PROXY_PASS = os.getenv("SOFA_PROXY_PASS", "")

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)

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

    def __init__(self, headless: bool = True):
        self.headless = headless
        self._pw = None
        self.browser = None
        self.context = None
        self.page = None
        self._request_count = 0
        self._heal_count = 0
        self._current_event_id = None
        self._current_event_attempt = None
        self._current_action = "idle"
        self._last_api_path = None

    async def __aenter__(self):
        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(
            headless=self.headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-size=1920,1080",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        proxy_config = None
        if PROXY_SERVER:
            proxy_config = {
                "server": f"http://{PROXY_SERVER}:{PROXY_PORT}",
                "username": PROXY_USER,
                "password": PROXY_PASS,
            }
        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=DEFAULT_UA,
            proxy=proxy_config,
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        self.page = await self.context.new_page()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.shutdown()

    async def shutdown(self):
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

    async def warm_homepage(self):
        """Load homepage to establish session cookies."""
        self._current_action = "warm_homepage"
        await self.page.goto(f"{BROWSER_BASE}/", timeout=30000, wait_until="domcontentloaded")
        await asyncio.sleep(2)

    async def warm_tournament(self, ut_id: int, season_id: int):
        """Load tournament page to get API access."""
        self._current_action = f"warm_tournament:{ut_id}/{season_id}"
        url = f"{BROWSER_BASE}/football/unique-tournament/{ut_id}/season/{season_id}"
        await self.page.goto(url, timeout=30000, wait_until="domcontentloaded")
        await asyncio.sleep(3)

    async def warm_event(self, event_id: int):
        """Load event page."""
        self._current_action = "warm_event"
        self._current_event_id = event_id
        url = f"{BROWSER_BASE}/event/{event_id}"
        await self.page.goto(url, timeout=30000, wait_until="domcontentloaded")
        await asyncio.sleep(1.5)

    async def rotate_context(self):
        """Create a fresh browser context to get a new rotating-proxy IP."""
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

        proxy_config = None
        if PROXY_SERVER:
            proxy_config = {
                "server": f"http://{PROXY_SERVER}:{PROXY_PORT}",
                "username": PROXY_USER,
                "password": PROXY_PASS,
            }
        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=DEFAULT_UA,
            proxy=proxy_config,
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        self.page = await self.context.new_page()
        # Re-warm homepage on the new context so cookies are set
        await self.warm_homepage()

    async def fetch_api(self, path: str, timeout_ms: int = 15000, max_retries: int = 3) -> dict:
        """Fetch a SofaScore API endpoint via page.evaluate, with retry on 403."""
        self._last_api_path = path
        for attempt in range(max_retries):
            self._request_count += 1
            self._current_action = f"fetch_api[{attempt+1}/{max_retries}]"
            try:
                result = await self.page.evaluate(
                    """
                    async ({ path, timeoutMs }) => {
                        const controller = new AbortController();
                        const timer = setTimeout(() => controller.abort(), timeoutMs);
                        try {
                            const resp = await fetch(path, {
                                credentials: 'include',
                                signal: controller.signal,
                            });
                            const text = await resp.text();
                            let body = text;
                            try { body = JSON.parse(text); } catch (e) {}
                            return { status: resp.status, body };
                        } catch (e) {
                            return { status: 0, error: e.message };
                        } finally {
                            clearTimeout(timer);
                        }
                    }
                    """,
                    {"path": path, "timeoutMs": timeout_ms},
                )
            except PlaywrightError as e:
                if self.is_target_closed_error(e) and attempt + 1 < max_retries:
                    print(f"    ⚠ Target closed during fetch_api: {e} [{self.telemetry_snapshot()}]")
                    await self.rebuild(reason=f"target closed during fetch_api {path}")
                    continue
                raise
            if result.get("status") != 403:
                return result
            # 403 — rotate proxy IP by creating a new browser context
            wait = 2 ** attempt + random.uniform(0.5, 1.5)
            print(f"    ⚠ 403 on {path} (attempt {attempt+1}/{max_retries}), rotating proxy IP in {wait:.1f}s...")
            await asyncio.sleep(wait)
            try:
                await self.rotate_context()
            except Exception as e:
                print(f"    ⚠ Context rotation failed: {e}")
        return result  # return last result (403) after all retries exhausted

    async def sleep_between(self, min_s: float = 1.5, max_s: float = 3.0):
        """Random sleep between requests."""
        await asyncio.sleep(random.uniform(min_s, max_s))


async def _process_event_with_retries(client: BackfillClient, inserter, progress, season_stats: dict,
                                      eid: int, sid: int, competition_id: int,
                                      *, max_attempts: int = 2) -> bool:
    last_error = None
    for attempt in range(1, max_attempts + 1):
        client.set_event_context(eid, attempt)
        try:
            if attempt > 1:
                print(f"    ↻ Retrying event {eid} (attempt {attempt}/{max_attempts})")

            await client.warm_event(eid)
            await client.sleep_between(0.5, 1.0)

            event_result = await client.fetch_api(f"/api/v1/event/{eid}")
            if event_result.get("status") != 200:
                print(f"    ⚠ Event {eid}: HTTP {event_result.get('status')}")
                progress.mark_failed(eid, f"event HTTP {event_result.get('status')}")
                season_stats["failed"] += 1
                return False

            event_data = event_result.get("body", {}).get("event", {})
            if event_data:
                inserter.insert_match(event_data, sid, competition_id)

            await client.sleep_between()
            inc_result = await client.fetch_api(f"/api/v1/event/{eid}/incidents")
            inc_count = 0
            if inc_result.get("status") == 200:
                inc_body = inc_result.get("body", {})
                if event_data:
                    inc_body.setdefault("homeTeam", event_data.get("homeTeam", {}))
                    inc_body.setdefault("awayTeam", event_data.get("awayTeam", {}))
                inc_count = inserter.insert_incidents(eid, inc_body)
            inserter.log_fetch("match_incidents", eid, "success" if inc_result.get("status") == 200 else "error",
                               inc_count, None if inc_result.get("status") == 200 else f"HTTP {inc_result.get('status')}")

            await client.sleep_between()
            lineup_result = await client.fetch_api(f"/api/v1/event/{eid}/lineups")
            lineup_count = 0
            if lineup_result.get("status") == 200:
                lineup_body = lineup_result.get("body", {})
                if event_data:
                    lineup_body.setdefault("homeTeam", event_data.get("homeTeam", {}))
                    lineup_body.setdefault("awayTeam", event_data.get("awayTeam", {}))
                lineup_count = inserter.insert_lineups(eid, lineup_body)
            inserter.log_fetch("match_lineups", eid, "success" if lineup_result.get("status") == 200 else "error",
                               lineup_count, None if lineup_result.get("status") == 200 else f"HTTP {lineup_result.get('status')}")

            await client.sleep_between()
            stats_result = await client.fetch_api(f"/api/v1/event/{eid}/statistics")
            stats_count = 0
            if stats_result.get("status") == 200:
                stats_count = inserter.insert_statistics(eid, stats_result.get("body", {}))
            inserter.log_fetch("match_statistics", eid, "success" if stats_result.get("status") == 200 else "error",
                               stats_count, None if stats_result.get("status") == 200 else f"HTTP {stats_result.get('status')}")

            await client.sleep_between()
            shot_result = await client.fetch_api(f"/api/v1/event/{eid}/shotmap")
            shot_count = 0
            if shot_result.get("status") == 200:
                shot_body = shot_result.get("body", {})
                if event_data:
                    shot_body.setdefault("homeTeam", event_data.get("homeTeam", {}))
                    shot_body.setdefault("awayTeam", event_data.get("awayTeam", {}))
                shot_count = inserter.insert_shotmap(eid, shot_body)
            inserter.log_fetch("match_shotmap", eid, "success" if shot_result.get("status") == 200 else "error",
                               shot_count, None if shot_result.get("status") == 200 else f"HTTP {shot_result.get('status')}")

            await client.sleep_between()
            graph_result = await client.fetch_api(f"/api/v1/event/{eid}/graph")
            graph_count = 0
            if graph_result.get("status") == 200:
                graph_count = inserter.insert_graph_points(eid, graph_result.get("body", {}))
            inserter.log_fetch("match_graph_points", eid, "success" if graph_result.get("status") == 200 else "error",
                               graph_count, None if graph_result.get("status") == 200 else f"HTTP {graph_result.get('status')}")

            await client.sleep_between()
            odds_result = await client.fetch_api(f"/api/v1/event/{eid}/odds/1/all")
            odds_count = 0
            if odds_result.get("status") == 200:
                odds_count = inserter.insert_odds(eid, odds_result.get("body", {}))
            inserter.log_fetch("match_odds", eid, "success" if odds_result.get("status") == 200 else "error",
                               odds_count, None if odds_result.get("status") == 200 else f"HTTP {odds_result.get('status')}")

            await client.sleep_between()
            comments_result = await client.fetch_api(f"/api/v1/event/{eid}/comments")
            comments_count = 0
            if comments_result.get("status") == 200:
                comments_count = inserter.insert_comments(eid, comments_result.get("body", {}))
            inserter.log_fetch("match_comments", eid, "success" if comments_result.get("status") == 200 else "error",
                               comments_count, None if comments_result.get("status") == 200 else f"HTTP {comments_result.get('status')}")

            progress.mark_done(eid)
            season_stats["processed"] += 1
            season_stats["incidents"] += inc_count
            season_stats["lineups"] += lineup_count
            season_stats["statistics"] += stats_count
            season_stats["shotmap"] += shot_count
            season_stats["graph"] += graph_count
            season_stats["odds"] += odds_count
            season_stats["comments"] += comments_count
            client.clear_event_context()
            return True
        except PlaywrightError as e:
            last_error = e
            if client.is_target_closed_error(e) and attempt < max_attempts:
                print(f"    ⚠ Target closed while processing event {eid}: {e} [{client.telemetry_snapshot()}]")
                await client.rebuild(reason=f"target closed while processing event {eid}")
                continue
            break
        except Exception as e:
            last_error = e
            break

    print(f"    ❌ Event {eid} error: {last_error} [{client.telemetry_snapshot()}]")
    progress.mark_failed(eid, str(last_error))
    season_stats["failed"] += 1
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
        status = event.get("status", {}).get("type", "scheduled")
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
    seasons: dict,  # {sid_str: year_label}
    from_year: int = 2015,
    season_id: Optional[int] = None,
    limit_rounds: int = 0,
    limit_events: int = 0,
    dry_run: bool = False,
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

    target_seasons = {
        int(sid): label for sid, label in seasons.items()
        if season_start_year(label) >= from_year
    }
    if season_id is not None:
        target_seasons = {sid: label for sid, label in target_seasons.items() if sid == season_id}
        if not target_seasons:
            target_seasons = {season_id: str(season_id)}

    print(f"\n{'='*60}")
    print(f"🏆 {name} — {len(target_seasons)} seasons (from {from_year})")
    print(f"{'='*60}")

    results = {"competition": name, "seasons": {}}

    async with BackfillClient(headless=True) as client:
        # Warm homepage
        await client.warm_homepage()
        print(f"✅ Session warmed")

        for sid, label in sorted(target_seasons.items()):
            print(f"\n📅 Season {label} (SID={sid})")

            if dry_run:
                print(f"  [DRY] Would process season {label}")
                results["seasons"][sid] = {"status": "dry_run"}
                continue

            # Ensure season in DB
            season_payload = None
            season_result = await client.fetch_api(f"/api/v1/unique-tournament/{ut_id}/season/{sid}")
            if season_result.get("status") == 200:
                season_body = season_result.get("body", {})
                season_payload = season_body.get("season") if isinstance(season_body, dict) else None
            ensure_season(mysql_conn, sid, competition_id, label, season_payload)

            # State file for resume
            state_path = DATA_DIR / f"progress_{name.replace(' ','_')}_{sid}.json"
            progress = ProgressTracker(state_path)

            # Warm tournament page
            try:
                await client.warm_tournament(ut_id, sid)
            except Exception as e:
                print(f"  ❌ Failed to warm tournament page: {e}")
                results["seasons"][sid] = {"status": "error", "reason": str(e)}
                continue

            # Get rounds
            rounds_result = await client.fetch_api(f"/api/v1/unique-tournament/{ut_id}/season/{sid}/rounds")
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
                )

                if ok and ((i + 1) % 10 == 0 or (i + 1) == len(all_event_ids)):
                    print(
                        f"    [{i+1}/{len(all_event_ids)}] done | "
                        f"processed:{season_stats['processed']} failed:{season_stats['failed']} "
                        f"heals:{client._heal_count} requests:{client._request_count}"
                    )

            print(f"  ✅ Season {label}: {season_stats['processed']} processed, {season_stats['skipped']} skipped, {season_stats['failed']} failed")
            results["seasons"][sid] = season_stats

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
    """Load competitions from YAML + discoveries.json."""
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

    # Merge
    result = {}
    for name, cfg in yaml_comps.items():
        disc = discoveries.get(name, {})
        seasons = disc.get("seasons", {})
        result[name] = {
            "ut_id": cfg.get("ut_id"),
            "cat_id": cfg.get("category_id"),
            "season_mode": cfg.get("season_mode", "national"),
            "seasons": seasons,
        }
    return result


async def main():
    ap = argparse.ArgumentParser(description="SofaScore Backfill Runner")
    ap.add_argument("--all", action="store_true", help="Run all competitions")
    ap.add_argument("--competition", type=str, help="Single competition name")
    ap.add_argument("--from-year", type=int, default=2015, help="Start year (default 2015)")
    ap.add_argument("--season-id", type=int, help="Restrict to one SofaScore season ID")
    ap.add_argument("--limit-rounds", type=int, default=0, help="Limit rounds per season (0=all)")
    ap.add_argument("--limit-events", type=int, default=0, help="Limit events (0=all)")
    ap.add_argument("--dry-run", action="store_true", help="Show what would run")
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
        result = await backfill_competition(
            name=args.competition,
            ut_id=comp["ut_id"],
            cat_id=comp["cat_id"],
            season_mode=comp["season_mode"],
            seasons=comp["seasons"],
            from_year=args.from_year,
            season_id=args.season_id,
            limit_rounds=args.limit_rounds,
            limit_events=args.limit_events,
            dry_run=args.dry_run,
        )
        print(f"\n{json.dumps(result, indent=2, ensure_ascii=False)}")

    elif args.all:
        total = len(competitions)
        for i, (name, comp) in enumerate(sorted(competitions.items()), 1):
            if not comp["seasons"]:
                print(f"\n⏭ [{i}/{total}] {name}: no seasons discovered, skipping")
                continue
            print(f"\n[{i}/{total}] {name}")
            result = await backfill_competition(
                name=name,
                ut_id=comp["ut_id"],
                cat_id=comp["cat_id"],
                season_mode=comp["season_mode"],
                seasons=comp["seasons"],
                from_year=args.from_year,
                limit_rounds=args.limit_rounds,
                limit_events=args.limit_events,
                dry_run=args.dry_run,
            )
            # Small delay between competitions
            await asyncio.sleep(5)
    else:
        print("Usage: backfill_runner.py --all | --competition NAME [--from-year 2015] [--limit-rounds N] [--dry-run]")
        print("       backfill_runner.py --test-match-json match_14023966_full.json")
        print(f"\nAvailable competitions: {', '.join(sorted(competitions.keys()))}")


if __name__ == "__main__":
    asyncio.run(main())
