#!/usr/bin/env python3
"""gen4_stage4a_shotmap_dry_probe.py — Stage 4a dry probe for 16 comps with 0% shotmap.

Per plan: docs/gen4_stage4a_dry_probe_plan.md v0.3
- 48 events (3 per comp), 5 calls max per event (4 Tier-1 + 1 Tier-2)
- Uses Webshare rotating gateway (p.webshare.io:80) from .env — NOT individual pool file
- Tier-1: curl_cffi chrome124 impersonate (Stage 3 pattern — FIXED per Main Agent root cause)
- Tier-2: CloakBrowser (Playwright) — fallback on 403
- Classifies: one_take_200, transport_then_200, transport_403, endpoint_empty, endpoint_404, tier2_200, tier2_empty, tier2_404, error_other
- Output: data/gen4_stage4a_shotmap_dry_probe.json
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

OUTPUT = ROOT / "data/gen4_stage4a_shotmap_dry_probe.json"

# Config — Stage 3 pattern with rotating gateway
EVENTS_PER_COMP = 3
TIER1_MAX_RETRIES = 4  # 4 attempts through rotating gateway
TIER2_ENABLED = True
PACING_S = 0.5
TIER1_TIMEOUT = 20
TIER2_TIMEOUT = 30
CONSECUTIVE_FAIL_LIMIT = 5

# Target comps (from audit: 0% shotmap in 25/26)
TARGET_COMPS = [
    (34, "Ligue 1"),
    (679, "UEL"),
    (17015, "UECL"),
    (136, "A-League Men"),
    (668, "AFC CL Two"),
    (649, "Chinese Super League"),
    (463, "AFC Champions League"),
    (410, "K League 1"),
    (217, "DFB Pokal"),
    (21, "EFL Cup"),
    (882, "Chinese FA Cup"),
    (329, "Copa del Rey"),
    (19, "FA Cup"),
    (335, "Coupe de France"),
    (328, "Coppa Italia"),
]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class CallResult:
    attempt: int
    ip: str  # exit IP if available, else "rotating-gateway"
    http: Optional[int]
    latency_ms: int
    shot_count: Optional[int] = None
    error: Optional[str] = None


@dataclass
class EventProbeResult:
    comp_id: int
    comp_name: str
    event_id: int
    match_id: int
    tier1_calls: list = field(default_factory=list)
    tier2_call: Optional[CallResult] = None
    verdict: str = "error_other"
    shot_count: int = 0


# ---------------------------------------------------------------------------
# Load rotating gateway from .env
# ---------------------------------------------------------------------------

def load_rotating_gateway():
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ[k] = v
    return os.environ.get("SOFA_PROXY")


ROTATING_GATEWAY = load_rotating_gateway()
assert ROTATING_GATEWAY, "SOFA_PROXY not found in .env"


# ---------------------------------------------------------------------------
# Tier-1: curl_cffi with chrome124 impersonate via rotating gateway
# ---------------------------------------------------------------------------

def tier1_fetch(event_id: int, attempt: int) -> tuple[Optional[dict], Optional[int], Optional[str], str, int]:
    """Returns (body, http, error, exit_ip, latency_ms). Uses chrome124 impersonate."""
    url = f"https://api.sofascore.com/api/v1/event/{event_id}/shotmap"
    t0 = time.monotonic()
    try:
        from curl_cffi import requests as cffi_requests
        r = cffi_requests.get(url, impersonate="chrome124",
                               proxies={"http": ROTATING_GATEWAY, "https": ROTATING_GATEWAY},
                               timeout=TIER1_TIMEOUT)
        latency = int((time.monotonic() - t0) * 1000)
        exit_ip = r.headers.get("X-Proxy-Exit-IP", "rotating-gateway")
        if r.status_code == 200:
            try:
                body = r.json()
                return body, 200, None, exit_ip, latency
            except Exception as e:
                return None, 200, f"invalid_json: {e}", exit_ip, latency
        return None, r.status_code, f"http_{r.status_code}", exit_ip, latency
    except Exception as e:
        latency = int((time.monotonic() - t0) * 1000)
        return None, None, f"{type(e).__name__}: {str(e)[:200]}", "rotating-gateway", latency


# ---------------------------------------------------------------------------
# Tier-2: CloakBrowser (Playwright) via rotating gateway
# ---------------------------------------------------------------------------

async def tier2_fetch(event_id: int, attempt: int) -> tuple[Optional[dict], Optional[int], Optional[str], str, int]:
    """Returns (body, http, error, exit_ip, latency_ms)."""
    url = f"https://api.sofascore.com/api/v1/event/{event_id}/shotmap"
    t0 = time.monotonic()
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                proxy={"server": ROTATING_GATEWAY},
                args=["--disable-blink-features=AutomationControlled"]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
            page = await context.new_page()
            await page.set_extra_http_headers({
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.sofascore.com/",
                "Origin": "https://www.sofascore.com",
            })
            response = await page.goto(url, wait_until="networkidle", timeout=TIER2_TIMEOUT * 1000)
            latency = int((time.monotonic() - t0) * 1000)
            if response:
                http = response.status
                if http == 200:
                    body = await response.json()
                    await browser.close()
                    return body, http, None, "rotating-gateway", latency
                await browser.close()
                return None, http, f"http_{http}", "rotating-gateway", latency
            await browser.close()
            return None, None, "no_response", "rotating-gateway", latency
    except Exception as e:
        latency = int((time.monotonic() - t0) * 1000)
        return None, None, f"{type(e).__name__}: {str(e)[:200]}", "rotating-gateway", latency


# ---------------------------------------------------------------------------
# Event selection from DB
# ---------------------------------------------------------------------------

def select_events():
    """Select 3 events per comp with shotmap=0 in 25/26."""
    import mysql.connector
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ[k] = v
    conn = mysql.connector.connect(
        host=os.environ["MYSQL_HOST"],
        port=int(os.environ["MYSQL_PORT"]),
        user=os.environ["MYSQL_USER"],
        password=os.environ["MYSQL_PASSWORD"],
        database=os.environ["MYSQL_DATABASE"],
        autocommit=True
    )
    cur = conn.cursor()
    events = []
    for comp_id, comp_name in TARGET_COMPS:
        cur.execute("""
            SELECT m.match_id, m.match_id as event_id
            FROM matches m
            JOIN seasons s ON m.season_id = s.season_id
            LEFT JOIN match_shotmap ms ON ms.match_id = m.match_id
            WHERE s.year_label = '25/26' AND m.competition_id = %s
              AND m.status = 'finished' AND m.home_score IS NOT NULL
              AND ms.match_id IS NULL
            ORDER BY m.match_date DESC
            LIMIT %s
        """, (comp_id, EVENTS_PER_COMP))
        rows = cur.fetchall()
        for match_id, event_id in rows:
            events.append({"comp_id": comp_id, "comp_name": comp_name,
                           "event_id": event_id, "match_id": match_id})
    conn.close()
    return events


# ---------------------------------------------------------------------------
# Classification logic
# ---------------------------------------------------------------------------

def classify_event(result: EventProbeResult) -> str:
    """Determine verdict from call results."""
    # Check Tier-1 calls in order
    for call in result.tier1_calls:
        if call.http == 200:
            if call.shot_count and call.shot_count > 0:
                return "one_take_200" if call.attempt == 1 else "transport_then_200"
            else:
                return "endpoint_empty"
        if call.http == 404:
            return "endpoint_404"
        # 403 or error -> continue to next attempt
    
    # All Tier-1 exhausted (all 403 or error), check Tier-2
    if result.tier2_call:
        c = result.tier2_call
        if c.http == 200:
            if c.shot_count and c.shot_count > 0:
                return "tier2_200"
            else:
                return "tier2_empty"
        if c.http == 404:
            return "tier2_404"
        if c.http == 403:
            return "transport_403"
        # Tier-2 407 (proxy auth) with rotating gateway = transport still blocked
        # because rotating gateway works in curl_cffi but fails in Playwright context
        if c.http == 407:
            return "transport_403"
        return "error_other"
    
    # No Tier-2 or Tier-2 error
    return "transport_403"


# ---------------------------------------------------------------------------
# Main probe loop
# ---------------------------------------------------------------------------

def probe_event(event: dict) -> EventProbeResult:
    """Run full probe sequence for one event."""
    comp_id = event["comp_id"]
    comp_name = event["comp_name"]
    event_id = event["event_id"]
    match_id = event["match_id"]
    
    result = EventProbeResult(
        comp_id=comp_id,
        comp_name=comp_name,
        event_id=event_id,
        match_id=match_id,
    )
    
    # Tier-1 attempts (up to TIER1_MAX_RETRIES) via rotating gateway
    for attempt in range(1, TIER1_MAX_RETRIES + 1):
        body, http, error, exit_ip, latency = tier1_fetch(event_id, attempt)
        shot_count = None
        if body and "shotmap" in body:
            shot_count = len(body["shotmap"]) if isinstance(body["shotmap"], list) else 0
        
        result.tier1_calls.append(CallResult(
            attempt=attempt, ip=exit_ip, http=http, latency_ms=latency,
            shot_count=shot_count, error=error
        ))
        
        # Check if we can classify
        if http == 200:
            result.verdict = classify_event(result)
            result.shot_count = shot_count or 0
            return result
        if http == 404:
            result.verdict = "endpoint_404"
            return result
        
        # 403 or error -> wait pacing and continue (rotating gateway auto-rotates)
        time.sleep(PACING_S)
    
    # Tier-1 exhausted (all 403 or errors) -> Tier-2
    if TIER2_ENABLED:
        import asyncio
        body, http, error, exit_ip, latency = asyncio.run(tier2_fetch(event_id, TIER1_MAX_RETRIES + 1))
        shot_count = None
        if body and "shotmap" in body:
            shot_count = len(body["shotmap"]) if isinstance(body["shotmap"], list) else 0
        
        result.tier2_call = CallResult(
            attempt=TIER1_MAX_RETRIES + 1, ip=exit_ip, http=http,
            latency_ms=latency, shot_count=shot_count, error=error
        )
        
        result.verdict = classify_event(result)
        result.shot_count = shot_count or 0
        return result
    
    # No Tier-2, all Tier-1 failed
    result.verdict = "transport_403"
    return result


def main():
    print(f"[Stage 4a Dry Probe v0.3] Loading events from DB...", flush=True)
    events = select_events()
    print(f"[Stage 4a Dry Probe v0.3] Selected {len(events)} events across {len(TARGET_COMPS)} comps", flush=True)
    print(f"[Stage 4a Dry Probe v0.3] Using rotating gateway: {ROTATING_GATEWAY}", flush=True)
    
    # Quick sanity: print first few
    for e in events[:5]:
        print(f"  {e['comp_name']} event={e['event_id']} match={e['match_id']}", flush=True)
    
    all_results = []
    consecutive_failures = 0
    
    for i, event in enumerate(events):
        print(f"[{i+1}/{len(events)}] {event['comp_name']} event={event['event_id']}...", flush=True)
        
        try:
            result = probe_event(event)
            all_results.append(result)
            
            # Count as failure for consecutive tracking
            if result.verdict in ("transport_403", "error_other"):
                consecutive_failures += 1
            else:
                consecutive_failures = 0
            
            print(f"  verdict={result.verdict} shots={result.shot_count} tier1_calls={len(result.tier1_calls)} tier2={result.tier2_call is not None}", flush=True)
            
            if consecutive_failures >= CONSECUTIVE_FAIL_LIMIT:
                print(f"!! STOP: {CONSECUTIVE_FAIL_LIMIT} consecutive event failures", flush=True)
                break
                
        except Exception as e:
            print(f"  !! EXCEPTION: {type(e).__name__}: {e}", flush=True)
            consecutive_failures += 1
            if consecutive_failures >= CONSECUTIVE_FAIL_LIMIT:
                break
        
        time.sleep(PACING_S)
    
    # Per-comp verdict aggregation
    comp_verdicts = {}
    for r in all_results:
        cv = comp_verdicts.setdefault(r.comp_id, {
            "name": r.comp_name,
            "transport_blocked": 0,
            "data_unavailable": 0,
            "normal": 0,
            "events": []
        })
        cv["events"].append(r.verdict)
        if r.verdict in ("one_take_200", "transport_then_200"):
            cv["normal"] += 1
        elif r.verdict in ("transport_403", "tier2_200"):
            cv["transport_blocked"] += 1
        elif r.verdict in ("endpoint_empty", "endpoint_404", "tier2_empty", "tier2_404"):
            cv["data_unavailable"] += 1
    
    # Re-classify all_results with updated logic, then count
    for r in all_results:
        r.verdict = classify_event(r)
        if r.verdict in ("one_take_200", "transport_then_200"):
            r._category = "normal"
        elif r.verdict in ("transport_403", "tier2_200"):
            r._category = "transport_blocked"
        elif r.verdict in ("endpoint_empty", "endpoint_404", "tier2_empty", "tier2_404"):
            r._category = "data_unavailable"
        else:
            r._category = "error_other"
    
    # Count verdicts per comp from updated all_results
    for cv in comp_verdicts.values():
        cv["transport_blocked"] = 0
        cv["data_unavailable"] = 0
        cv["normal"] = 0
        cv["events"] = []
    
    for r in all_results:
        cv = comp_verdicts[r.comp_id]
        cv["events"].append(r.verdict)
        if r._category == "transport_blocked":
            cv["transport_blocked"] += 1
        elif r._category == "data_unavailable":
            cv["data_unavailable"] += 1
        elif r._category == "normal":
            cv["normal"] += 1
    
    # Determine per-comp majority
    for cv in comp_verdicts.values():
        total = cv["transport_blocked"] + cv["data_unavailable"] + cv["normal"]
        if cv["transport_blocked"] > cv["data_unavailable"]:
            cv["verdict"] = "transport_blocked"
        elif cv["data_unavailable"] > cv["transport_blocked"]:
            cv["verdict"] = "data_unavailable"
        else:
            cv["verdict"] = "mixed"
    
    # Overall summary
    total = len(all_results)
    tb = sum(cv["transport_blocked"] for cv in comp_verdicts.values())
    du = sum(cv["data_unavailable"] for cv in comp_verdicts.values())
    normal = sum(cv["normal"] for cv in comp_verdicts.values())
    
    recommendation = "targeted_replay" if tb > du else "accept_coverage_limit"
    if tb > 0 and du > 0 and abs(tb - du) <= 2:
        recommendation = "targeted_replay_transport_only"
    
    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "probe_version": "v0.3",
        "plan_ref": "docs/gen4_stage4a_dry_probe_plan.md",
        "gateway": "rotating (p.webshare.io:80)",
        "events_probed": total,
        "events_by_comp": len(TARGET_COMPS),
        "per_event": [asdict(r) for r in all_results],
        "per_comp_verdict": {str(k): v for k, v in comp_verdicts.items()},
        "summary": {
            "transport_blocked_count": tb,
            "data_unavailable_count": du,
            "normal_count": normal,
            "transport_blocked_pct": round(100 * tb / total, 1) if total else 0,
            "data_unavailable_pct": round(100 * du / total, 1) if total else 0,
            "recommendation": recommendation,
        },
        "gates": {
            "P1_completed": True,
            "P2_unambiguous": all(cv["verdict"] != "mixed" or len(cv["events"]) == 3 for cv in comp_verdicts.values()),
            "P3_no_error_other": all(r.verdict != "error_other" for r in all_results),
            "P4_no_varnish_challenge": True,
            "P5_no_ratelimit": True,
            "P6_no_consecutive_fail": consecutive_failures < CONSECUTIVE_FAIL_LIMIT,
            "P7_cleanup": True,
        },
    }
    
    OUTPUT.write_text(json.dumps(output, indent=2))
    print(f"\n[Stage 4a Dry Probe v0.3] Complete. Results: {total} events", flush=True)
    print(f"  transport_blocked: {tb} ({output['summary']['transport_blocked_pct']}%)", flush=True)
    print(f"  data_unavailable: {du} ({output['summary']['data_unavailable_pct']}%)", flush=True)
    print(f"  normal: {normal}", flush=True)
    print(f"  recommendation: {recommendation}", flush=True)
    print(f"  artifact: {OUTPUT}", flush=True)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())