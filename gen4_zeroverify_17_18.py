#!/usr/bin/env python3
"""
gen4_zeroverify_17_18.py — Verify 16 zero-event comp-seasons from 17/18 Phase B report.

Uses new 20-IP pool (good_20260917_v2.txt) + curl_cffi chrome124.
Budget: ≤6 calls per comp, ~150 total calls.

Tasks:
1. For each 0-events comp-season 17/18: resolve season_id + fetch events listing
2. Sample-probe known real 17/18 event IDs (UCL final, FA Cup final) via /event/{id}
3. Check DB for existing matches (READ-ONLY)
4. Produce verdict report + evidence JSONL
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
from typing import Any, Dict, List, Optional, Tuple

import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Load .env for MySQL creds
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    cffi_requests = None
    HAS_CURL_CFFI = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

POOL_FILE = ROOT / "data/proxy_pools/good_20260917_v2.txt"
REPORT_PATH = ROOT / "data/gen4_phaseB_report_17-18.json"
OUTPUT_REPORT = ROOT / "data/gen4_phaseB_17-18_zeroverify_report.json"
EVIDENCE_JSONL = ROOT / "data/gen4_phaseB_17-18_zeroverify_evidence.jsonl"

TIMEOUT = 20
MAX_CALLS_PER_COMP = 6
MAX_TOTAL_CALLS = 150

API_BASE = "https://api.sofascore.com/api/v1"
BROWSER_BASE = "https://www.sofascore.com"

# 16 comp-seasons with 0 events in Phase B 17/18 report
ZERO_EVENT_COMPS = [
    # Leagues / Continental (6)
    {"name": "A-League Men", "ut_id": 136, "season_label": "17/18", "year_candidates": ["17/18", "2017"], "type": "league"},
    {"name": "Chinese Super League", "ut_id": 649, "season_label": "17/18", "year_candidates": ["17/18", "2017"], "type": "league"},
    {"name": "UCL", "ut_id": 7, "season_label": "17/18", "year_candidates": ["17/18", "2017"], "type": "continental"},
    {"name": "UEL", "ut_id": 679, "season_label": "17/18", "year_candidates": ["17/18", "2017"], "type": "continental"},
    {"name": "AFC Champions League", "ut_id": 463, "season_label": "17/18", "year_candidates": ["17/18", "2017"], "type": "continental"},
    {"name": "AFC Champions League Two", "ut_id": 668, "season_label": "17/18", "year_candidates": ["17/18", "2017"], "type": "continental"},
    # Cups (10)
    {"name": "FA Cup", "ut_id": 19, "season_label": "17/18", "year_candidates": ["17/18", "2017"], "type": "cup"},
    {"name": "EFL Cup", "ut_id": 21, "season_label": "17/18", "year_candidates": ["17/18", "2017"], "type": "cup"},
    {"name": "Copa del Rey", "ut_id": 329, "season_label": "17/18", "year_candidates": ["17/18", "2017"], "type": "cup"},
    {"name": "Coppa Italia", "ut_id": 328, "season_label": "17/18", "year_candidates": ["17/18", "2017"], "type": "cup"},
    {"name": "Coupe de France", "ut_id": 335, "season_label": "17/18", "year_candidates": ["17/18", "2017"], "type": "cup"},
    {"name": "DFB Pokal", "ut_id": 217, "season_label": "17/18", "year_candidates": ["17/18", "2017"], "type": "cup"},
    {"name": "J.League Cup", "ut_id": 101, "season_label": "17/18", "year_candidates": ["17/18", "2017"], "type": "cup"},
    {"name": "Emperor's Cup", "ut_id": 323, "season_label": "17/18", "year_candidates": ["17/18", "2017"], "type": "cup"},
    {"name": "Australia Cup", "ut_id": 1786, "season_label": "17/18", "year_candidates": ["17/18", "2017"], "type": "cup"},
    {"name": "Chinese FA Cup", "ut_id": 882, "season_label": "17/18", "year_candidates": ["17/18", "2017"], "type": "cup"},
]

# Known real 17/18 event IDs for sample probing (to be discovered or from known sources)
# UCL 17/18 final: Real Madrid vs Liverpool (2018-05-26) - event ID from SofaScore
# FA Cup 17/18 final: Chelsea vs Man Utd (2018-05-19) - event ID from SofaScore
# We'll discover these via the API if season listing works
SAMPLE_EVENTS = {
    "UCL_17_18_final": None,      # Will try to discover
    "FA_Cup_17_18_final": None,   # Will try to discover
}

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class CallEvidence:
    comp_name: str
    ut_id: int
    season_label: str
    endpoint: str
    ip: Optional[str]
    status: Optional[int]
    event_count: int
    error: Optional[str] = None
    latency_ms: Optional[int] = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_jsonl(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


@dataclass
class CompVerdict:
    comp_name: str
    ut_id: int
    season_label: str
    comp_type: str
    resolved_season_id: Optional[int] = None
    season_resolve_status: Optional[int] = None
    season_resolve_error: Optional[str] = None
    listing_endpoint: Optional[str] = None
    listing_status: Optional[int] = None
    listing_event_count: int = 0
    listing_error: Optional[str] = None
    sample_event_1_id: Optional[int] = None
    sample_event_1_status: Optional[int] = None
    sample_event_2_id: Optional[int] = None
    sample_event_2_status: Optional[int] = None
    db_match_count: int = 0
    verdict: str = "unknown"  # "403-blocked" | "genuine-empty" | "season-not-found" | "unknown"
    calls_used: int = 0


@dataclass
class ZeroVerifyReport:
    timestamp: str
    total_comps: int
    total_calls: int
    comps: List[CompVerdict]
    summary: Dict[str, int]
    recommended_next_action: str


# ---------------------------------------------------------------------------
# Proxy pool
# ---------------------------------------------------------------------------

class ProxyPool:
    def __init__(self, pool_file: Path):
        self.pool: List[Dict[str, str]] = []
        self.idx = 0
        self._load(pool_file)

    def _load(self, pool_file: Path):
        for line in pool_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(":", 3)
            if len(parts) == 4:
                ip, port, user, pw = parts
                self.pool.append({"ip": ip, "port": port, "user": user, "pw": pw})
        print(f"[pool] Loaded {len(self.pool)} proxies from {pool_file}")

    def next(self) -> Dict[str, str]:
        m = self.pool[self.idx % len(self.pool)]
        self.idx += 1
        return m


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

async def http_get(path: str, proxy: Dict[str, str], timeout: int = TIMEOUT) -> Tuple[Optional[Any], int, Optional[str], float]:
    """Return (body, status, error, latency_ms)."""
    if not HAS_CURL_CFFI:
        return None, 0, "curl_cffi unavailable", 0.0

    proxy_url = f"http://{proxy['user']}:{proxy['pw']}@{proxy['ip']}:{proxy['port']}"
    url = f"{API_BASE}{path}"
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
            error = f"invalid_json: {e}"
        return body, status, error, latency_ms
    except Exception as e:
        latency_ms = int((time.monotonic() - t0) * 1000)
        return None, 0, f"{type(e).__name__}: {str(e)[:120]}", latency_ms


async def fetch_with_retry(path: str, pool: ProxyPool, max_retries: int = 3) -> Tuple[Optional[Any], int, Optional[str], Optional[str], int]:
    """Fetch with proxy rotation on 403/5xx. Returns (body, status, error, ip_used, tries)."""
    last_error = None
    last_status = 0
    ip_used = None

    for attempt in range(max_retries + 1):
        proxy = pool.next()
        ip_used = proxy["ip"]
        body, status, error, latency = await http_get(path, proxy)

        if status == 200 and body is not None:
            return body, status, None, ip_used, attempt + 1

        if status == 404:
            return None, 404, "http_404", ip_used, attempt + 1

        if status == 403:
            last_error = "http_403"
            last_status = 403
            await asyncio.sleep(random.uniform(1.0, 2.0))
            continue

        if status >= 500 or status == 0:
            last_error = error or f"http_{status}"
            last_status = status
            await asyncio.sleep(random.uniform(1.0, 2.0))
            continue

        last_error = error or f"http_{status}"
        last_status = status
        await asyncio.sleep(random.uniform(1.0, 2.0))

    return None, last_status, last_error, ip_used, max_retries + 1


# ---------------------------------------------------------------------------
# DB check (READ-ONLY)
# ---------------------------------------------------------------------------

def check_db_matches(comp_name: str, ut_id: int) -> int:
    """Check if matches table has any rows for this competition 17/18 season.
    Returns count (0 if connection fails or no rows)."""
    try:
        import mysql.connector
        conn = mysql.connector.connect(
            host=os.getenv("MYSQL_HOST", "127.0.0.1"),
            port=int(os.getenv("MYSQL_PORT", "3306")),
            user=os.getenv("MYSQL_USER", "root"),
            password=os.getenv("MYSQL_PASSWORD", ""),
            database=os.getenv("MYSQL_DATABASE", "appdb"),
            charset="utf8mb4",
            connect_timeout=5,
        )
        cur = conn.cursor()
        # Check via competition_id (ut_id) and season year range
        cur.execute("""
            SELECT COUNT(*) FROM matches m
            JOIN seasons s ON m.season_id = s.season_id
            WHERE s.competition_id = %s AND s.year_start = 2017
        """, (ut_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return row[0] if row else 0
    except Exception as e:
        print(f"  [db] {comp_name}: connection failed: {e}")
        return -1  # -1 indicates connection failure


# ---------------------------------------------------------------------------
# Main verification logic
# ---------------------------------------------------------------------------

async def verify_comp(comp: Dict[str, Any], pool: ProxyPool, evidence_lines: List[CallEvidence]) -> Tuple[CompVerdict, int]:
    """Verify a single comp-season. Returns (verdict, calls_used)."""
    name = comp["name"]
    ut_id = comp["ut_id"]
    season_label = comp["season_label"]
    year_candidates = comp["year_candidates"]
    comp_type = comp["type"]

    verdict = CompVerdict(
        comp_name=name,
        ut_id=ut_id,
        season_label=season_label,
        comp_type=comp_type,
    )
    calls = 0

    print(f"\n{'='*60}")
    print(f"  Verifying: {name} (ut_id={ut_id}) {season_label}")
    print(f"{'='*60}")

    # 1. Resolve season_id via /unique-tournament/{ut_id}/seasons
    print(f"  [1] Resolving season_id via /unique-tournament/{ut_id}/seasons")
    season_path = f"/unique-tournament/{ut_id}/seasons"
    body, status, error, ip, tries = await fetch_with_retry(season_path, pool, max_retries=2)
    calls += tries
    evidence_lines.append(CallEvidence(
        comp_name=name, ut_id=ut_id, season_label=season_label,
        endpoint=season_path, ip=ip, status=status, event_count=0, error=error,
    ))

    verdict.season_resolve_status = status
    verdict.season_resolve_error = error
    verdict.calls_used = calls

    if status != 200 or body is None:
        print(f"    ✗ Season resolve failed: HTTP {status} {error}")
        if status == 404:
            verdict.verdict = "season-not-found"
        elif status == 403 or status == 0:
            verdict.verdict = "403-blocked"
        else:
            verdict.verdict = "unknown"
        return verdict, calls

    # Find matching season
    seasons = body.get("seasons", [])
    matched_sid = None
    matched_year = None
    for cand in year_candidates:
        for s in seasons:
            if str(s.get("year")) == cand:
                matched_sid = s.get("id")
                matched_year = cand
                break
        if matched_sid:
            break

    if not matched_sid:
        print(f"    ✗ No matching season for candidates {year_candidates}")
        print(f"    Available seasons: {[str(s.get('year')) for s in seasons[:10]]}")
        verdict.verdict = "season-not-found"
        return verdict, calls

    verdict.resolved_season_id = matched_sid
    print(f"    ✓ Resolved season_id={matched_sid} (year={matched_year})")

    # 2. Fetch events listing - try /events/last/0 first (works for cups and leagues)
    print(f"  [2] Fetching events via /unique-tournament/{ut_id}/season/{matched_sid}/events/last/0")
    listing_path = f"/unique-tournament/{ut_id}/season/{matched_sid}/events/last/0"
    body, status, error, ip, tries = await fetch_with_retry(listing_path, pool, max_retries=2)
    calls += tries
    event_count = 0
    if body and isinstance(body, dict):
        events = body.get("events", [])
        if isinstance(events, list):
            event_count = len(events)

    evidence_lines.append(CallEvidence(
        comp_name=name, ut_id=ut_id, season_label=season_label,
        endpoint=listing_path, ip=ip, status=status, event_count=event_count, error=error,
    ))

    verdict.listing_endpoint = listing_path
    verdict.listing_status = status
    verdict.listing_event_count = event_count
    verdict.listing_error = error
    verdict.calls_used = calls

    if status == 200 and event_count > 0:
        print(f"    ✓ Found {event_count} events")
        verdict.verdict = "genuine-empty" if event_count == 0 else "events-found"
    elif status == 403:
        print(f"    ✗ 403 blocked on events listing")
        verdict.verdict = "403-blocked"
    elif status == 404:
        print(f"    ✗ 404 on events listing (season may not have events endpoint)")
        verdict.verdict = "season-not-found"
    else:
        print(f"    ✗ Events listing failed: HTTP {status} {error}")
        verdict.verdict = "unknown"

    # If events/last/0 returned 0 or 404, try /events/round/1 as fallback
    if (status == 200 and event_count == 0) or status == 404:
        if calls < MAX_CALLS_PER_COMP:
            print(f"  [3] Fallback: trying /events/round/1")
            round_path = f"/unique-tournament/{ut_id}/season/{matched_sid}/events/round/1"
            body, status, error, ip, tries = await fetch_with_retry(round_path, pool, max_retries=1)
            calls += tries
            event_count = 0
            if body and isinstance(body, dict):
                events = body.get("events", [])
                if isinstance(events, list):
                    event_count = len(events)

            evidence_lines.append(CallEvidence(
                comp_name=name, ut_id=ut_id, season_label=season_label,
                endpoint=round_path, ip=ip, status=status, event_count=event_count, error=error,
            ))

            verdict.listing_endpoint = f"{listing_path} + {round_path}"
            verdict.listing_status = status
            verdict.listing_event_count = event_count
            verdict.listing_error = error
            verdict.calls_used = calls

            if status == 200 and event_count > 0:
                print(f"    ✓ Found {event_count} events via round/1")
                verdict.verdict = "events-found"
            elif status == 403:
                print(f"    ✗ 403 blocked on round/1")
                verdict.verdict = "403-blocked"
            else:
                print(f"    ✗ Round/1: HTTP {status} {error}")

    # 3. Sample probe known event IDs if we found events
    if event_count > 0 and body and isinstance(body, dict):
        events = body.get("events", [])
        if events:
            # Try first event
            sample_eid = events[0].get("id")
            if sample_eid and calls < MAX_CALLS_PER_COMP:
                print(f"  [4] Sample probe event {sample_eid}")
                event_path = f"/event/{sample_eid}"
                body, status, error, ip, tries = await fetch_with_retry(event_path, pool, max_retries=1)
                calls += tries
                evidence_lines.append(CallEvidence(
                    comp_name=name, ut_id=ut_id, season_label=season_label,
                    endpoint=event_path, ip=ip, status=status, event_count=1 if status == 200 else 0, error=error,
                ))
                verdict.sample_event_1_id = sample_eid
                verdict.sample_event_1_status = status
                verdict.calls_used = calls
                print(f"    Event {sample_eid}: HTTP {status}")

            # Try last event
            if len(events) > 1 and calls < MAX_CALLS_PER_COMP:
                sample_eid = events[-1].get("id")
                if sample_eid:
                    print(f"  [5] Sample probe event {sample_eid}")
                    event_path = f"/event/{sample_eid}"
                    body, status, error, ip, tries = await fetch_with_retry(event_path, pool, max_retries=1)
                    calls += tries
                    evidence_lines.append(CallEvidence(
                        comp_name=name, ut_id=ut_id, season_label=season_label,
                        endpoint=event_path, ip=ip, status=status, event_count=1 if status == 200 else 0, error=error,
                    ))
                    verdict.sample_event_2_id = sample_eid
                    verdict.sample_event_2_status = status
                    verdict.calls_used = calls
                    print(f"    Event {sample_eid}: HTTP {status}")

    # 4. Check DB for existing matches (READ-ONLY)
    print(f"  [DB] Checking matches table for {name} 17/18...")
    db_count = check_db_matches(name, ut_id)
    verdict.db_match_count = db_count
    if db_count >= 0:
        print(f"    DB matches: {db_count}")
    else:
        print(f"    DB: connection failed")

    # Final verdict logic
    if verdict.verdict == "unknown":
        if verdict.listing_status == 403:
            verdict.verdict = "403-blocked"
        elif verdict.listing_status == 404:
            verdict.verdict = "season-not-found"
        elif verdict.listing_status == 200 and verdict.listing_event_count == 0:
            verdict.verdict = "genuine-empty"
        else:
            verdict.verdict = "unknown"

    print(f"  >>> VERDICT: {verdict.verdict} (calls={calls})")
    return verdict, calls


async def main():
    print("="*70)
    print("  Gen4 Zero-Verify 17/18 — 16 comp-seasons with 0 events")
    print("="*70)

    if not POOL_FILE.exists():
        print(f"✗ Pool file not found: {POOL_FILE}")
        sys.exit(1)

    pool = ProxyPool(POOL_FILE)
    evidence_lines: List[CallEvidence] = []
    all_verdicts: List[CompVerdict] = []
    total_calls = 0

    # Verify each comp
    for comp in ZERO_EVENT_COMPS:
        if total_calls >= MAX_TOTAL_CALLS:
            print(f"\n⚠ Total call budget ({MAX_TOTAL_CALLS}) reached, stopping.")
            break

        verdict, calls = await verify_comp(comp, pool, evidence_lines)
        all_verdicts.append(verdict)
        total_calls += calls

        # Small delay between comps
        await asyncio.sleep(random.uniform(0.5, 1.5))

    # Build summary
    summary = {
        "403-blocked": 0,
        "genuine-empty": 0,
        "season-not-found": 0,
        "events-found": 0,
        "unknown": 0,
    }
    for v in all_verdicts:
        summary[v.verdict] = summary.get(v.verdict, 0) + 1

    # Recommended next action
    blocked_comps = [v.comp_name for v in all_verdicts if v.verdict == "403-blocked"]
    empty_comps = [v.comp_name for v in all_verdicts if v.verdict == "genuine-empty"]
    notfound_comps = [v.comp_name for v in all_verdicts if v.verdict == "season-not-found"]
    found_comps = [v.comp_name for v in all_verdicts if v.verdict == "events-found"]

    rec_parts = []
    if blocked_comps:
        rec_parts.append(f"Targeted re-run for {len(blocked_comps)} 403-blocked comps: {', '.join(blocked_comps)}")
    if empty_comps:
        rec_parts.append(f"{len(empty_comps)} genuine-empty (200 + 0 events): {', '.join(empty_comps)} — verify with SofaScore website")
    if notfound_comps:
        rec_parts.append(f"{len(notfound_comps)} season-not-found: {', '.join(notfound_comps)} — check if 17/18 season exists")
    if found_comps:
        rec_parts.append(f"{len(found_comps)} actually have events: {', '.join(found_comps)} — Phase B missed them")

    recommended = "; ".join(rec_parts) if rec_parts else "All clear"

    # Write report
    report = ZeroVerifyReport(
        timestamp=datetime.now(timezone.utc).isoformat(),
        total_comps=len(all_verdicts),
        total_calls=total_calls,
        comps=all_verdicts,
        summary=summary,
        recommended_next_action=recommended,
    )

    OUTPUT_REPORT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_REPORT.write_text(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    print(f"\n✓ Report written to {OUTPUT_REPORT}")

    # Write evidence JSONL
    EVIDENCE_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with EVIDENCE_JSONL.open("w") as f:
        for ev in evidence_lines:
            f.write(ev.to_jsonl() + "\n")
    print(f"✓ Evidence written to {EVIDENCE_JSONL} ({len(evidence_lines)} lines)")

    # Print summary
    print("\n" + "="*70)
    print("  SUMMARY")
    print("="*70)
    for k, v in summary.items():
        print(f"  {k}: {v}")
    print(f"\n  Total calls: {total_calls} / {MAX_TOTAL_CALLS}")
    print(f"\n  Recommended next action:")
    print(f"  {recommended}")

    # Also print per-compact verdict table
    print("\n  Per-comp verdicts:")
    for v in all_verdicts:
        print(f"    {v.comp_name:25s} | {v.verdict:18s} | calls={v.calls_used} | season_id={v.resolved_season_id} | events={v.listing_event_count} | db={v.db_match_count}")


if __name__ == "__main__":
    asyncio.run(main())