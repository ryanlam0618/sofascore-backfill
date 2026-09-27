#!/usr/bin/env python3
"""gen2_full_validate.py — Gen2 Full Validation Benchmark (live runner).

Per Gen2 Full Validation Benchmark prompt:
- Read-only validation of production Gen2 fetch path
- write_enabled=False, FETCH_STRATEGY=gen2, no MySQL writes
- 5 profiles: A cold_curl_cffi, B production_hybrid, C warm_session_hybrid,
  D browser_fallback_controlled, E ssr_fallback_controlled
- Scope: events=[14025013, 12436875], endpoints=[event,incidents,lineups,
  comments,statistics,shotmap,highlights]
- Budget: ≤150 total, ≤20/event, ≤2 retries/endpoint
- Safety halts: 407, quota, 403>50%, 2×EPIPE, 2×browser-closed, /dev/shm,
  cleanup failure, DB write, credentials leak, scope expansion, production code mod
- Artifact: data/gen2_full_validation_YYYYMMDD_HHMMSS.{json,md}

Status: SCAFFOLD ONLY. Kris approval required before live launch (Step 7 gate).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

# Hard guards from prompt §"重要約束"
os.environ["FETCH_STRATEGY"] = "gen2"
os.environ.setdefault("PYTHONUNBUFFERED", "1")
WRITE_ENABLED = False

APPROVED_EVENTS = frozenset({14025013, 12436875})
CONFIGURED_ENDPOINTS = frozenset({
    "event", "incidents", "lineups", "statistics", "shotmap",
    "graph", "odds", "comments",
})
# Round-1 endpoints per prompt "第一輪最少必須包括"
ROUND1_ENDPOINTS = [
    "event", "incidents", "lineups", "comments",
    "statistics", "shotmap", "highlights",
]
# highlights is NOT_CONFIGURED in production; mark accordingly
NOT_CONFIGURED_IN_ROUND1 = [ep for ep in ROUND1_ENDPOINTS if ep not in CONFIGURED_ENDPOINTS]

# Endpoint → path template (matches backfill_runner.ENDPOINT_PATHS)
ENDPOINT_PATHS_PROD = {
    "event":      "/api/v1/event/{event_id}",
    "incidents":  "/api/v1/event/{event_id}/incidents",
    "lineups":    "/api/v1/event/{event_id}/lineups",
    "statistics": "/api/v1/event/{event_id}/statistics",
    "shotmap":    "/api/v1/event/{event_id}/shotmap",
    "graph":      "/api/v1/event/{event_id}/graph",
    "odds":       "/api/v1/event/{event_id}/odds/1/all",
    "comments":   "/api/v1/event/{event_id}/comments",
}

MAX_TOTAL_REQUESTS = 150
MAX_PER_EVENT = 20
MAX_RETRIES_PER_ENDPOINT = 2
COOLDOWN_BETWEEN_EVENTS_S = 10.0
COOLDOWN_BETWEEN_PROFILES_S = 60.0


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def redact_secrets(blob: str) -> str:
    """Strip proxy password, cookies, authorization headers from a string."""
    pw = os.getenv("SOFA_PROXY_PASS", "")
    if pw:
        blob = blob.replace(pw, "[REDACTED]")
    # Strip common auth patterns
    blob = re.sub(r"(?i)(authorization\s*[:=]\s*)\S+", r"\1[REDACTED]", blob)
    blob = re.sub(r"(?i)(set-cookie\s*[:=]\s*)\S+", r"\1[REDACTED]", blob)
    blob = re.sub(r"(?i)(cookie\s*[:=]\s*)\S+", r"\1[REDACTED]", blob)
    blob = re.sub(r"https?://[^/\s]+:[^/\s]+@", "http://[REDACTED]@[REDACTED]", blob)
    return blob


def is_payload_complete(path: str, body: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(body, dict) or not body:
        return False
    aliases = {
        "event": ("event", "id"),
        "incidents": ("incidents",),
        "lineups": ("home", "away", "lineups"),
        "statistics": ("statistics", "groups", "stats"),
        "shotmap": ("shotmap",),
        "graph": ("graph", "graphPoints"),
        "odds": ("odds",),
        "comments": ("comments",),
    }
    parts = path.split("/")
    endpoint = "event"
    if "event" in parts:
        i = parts.index("event")
        if i + 2 < len(parts):
            endpoint = parts[i + 2]
    return any(k in body for k in aliases.get(endpoint, ()))


def check_safety_halts(artifact: Dict[str, Any]) -> Optional[str]:
    """Return halt reason code or None. Pure-Python, no I/O."""
    results = artifact.get("results", [])
    for r in results:
        if r.get("status") == 407:
            return "HTTP_407"
        for a in r.get("attempts", []):
            if isinstance(a, dict) and a.get("status") == 407:
                return "HTTP_407"
    if artifact.get("quota_warning"):
        return "QUOTA_WARNING"
    if len(results) >= 2:
        n_403 = sum(1 for r in results if r.get("status") == 403)
        if n_403 / len(results) > 0.5:
            return "403_RATE_OVER_50"
    consec_epipe = 0
    for r in results:
        if r.get("epipe"):
            consec_epipe += 1
            if consec_epipe >= 2:
                return "CONSECUTIVE_EPIPE"
        else:
            consec_epipe = 0
    consec_closed = 0
    for r in results:
        if r.get("browser_closed"):
            consec_closed += 1
            if consec_closed >= 2:
                return "CONSECUTIVE_BROWSER_CLOSED"
        else:
            consec_closed = 0
    if artifact.get("shm_exhausted"):
        return "RESOURCE_EXHAUSTION"
    if artifact.get("cleanup_ok") is False:
        return "CLEANUP_FAILURE"
    if artifact.get("db_write_attempted"):
        return "DB_WRITE_DETECTED"
    blob = json.dumps(artifact, default=str)
    pw = os.getenv("SOFA_PROXY_PASS", "")
    if pw and pw in blob:
        return "CREDENTIALS_LEAK"
    if "http://" in blob and "@" in blob and "[REDACTED]" not in blob:
        # crude proxy URL leak check
        if re.search(r"http://[^/\s]+:[^/\s]+@", blob):
            return "CREDENTIALS_LEAK"
    approved = set(artifact.get("approved_events", []))
    seen = {r.get("event_id") for r in results if r.get("event_id")}
    if approved and not seen.issubset(approved):
        return "SCOPE_EXPANSION"
    if artifact.get("production_code_modified"):
        return "PRODUCTION_CODE_MODIFIED"
    return None


def make_attempt_record(transport: str, status: int, latency_ms: int,
                          error: Optional[str] = None) -> Dict[str, Any]:
    return {
        "transport": transport,
        "status": status,
        "latency_ms": latency_ms,
        "error": error,
    }


def make_result_record(
    *,
    request_idx: int,
    event_id: int,
    endpoint: str,
    path: str,
    attempts: List[Dict[str, Any]],
    body: Optional[Dict[str, Any]],
    ok: bool,
    fallback_used: bool,
    retry_count: int,
    browser_rebuilt: bool,
    epipe: bool,
    error_class: Optional[str],
    browser_closed: bool = False,
) -> Dict[str, Any]:
    final_status = attempts[-1]["status"] if attempts else 0
    body_bytes = len(json.dumps(body, default=str)) if body is not None else 0
    json_valid = isinstance(body, (dict, list)) and bool(body)
    payload_complete = is_payload_complete(path, body) if json_valid else False
    return {
        "event_id": event_id,
        "endpoint": endpoint,
        "path_redacted": path.replace(str(event_id), "{eid}"),
        "attempts": attempts,
        "initial_transport": attempts[0]["transport"] if attempts else None,
        "initial_status": attempts[0]["status"] if attempts else 0,
        "final_transport": attempts[-1]["transport"] if attempts else None,
        "final_status": final_status,
        "status": final_status,
        "transport": attempts[-1]["transport"] if attempts else None,
        "latency_ms": sum(a.get("latency_ms", 0) for a in attempts),
        "response_bytes": body_bytes,
        "json_valid": json_valid,
        "payload_complete": payload_complete,
        "fallback_used": fallback_used,
        "retry_count": retry_count,
        "browser_rebuilt": browser_rebuilt,
        "epipe": epipe,
        "browser_closed": browser_closed,
        "error_class": error_class,
        "ok": ok,
    }


def env_check() -> Dict[str, Any]:
    """Snapshot env before any request. No secrets recorded."""
    return {
        "proxy_configured": bool(os.getenv("SOFA_PROXY_HOST")
                                  and os.getenv("SOFA_PROXY_PORT")
                                  and os.getenv("SOFA_PROXY_USER")),
        "proxy_host_set": bool(os.getenv("SOFA_PROXY_HOST")),
        "proxy_user_set": bool(os.getenv("SOFA_PROXY_USER")),
        "proxy_pass_set": bool(os.getenv("SOFA_PROXY_PASS")),
        "credentials_redacted": True,
        "write_enabled": WRITE_ENABLED,
        "fetch_strategy": os.getenv("FETCH_STRATEGY", "gen2"),
        "python_unbuffered": os.getenv("PYTHONUNBUFFERED") == "1",
    }


def check_shm() -> Dict[str, Any]:
    """Record /dev/shm capacity without modifying anything."""
    try:
        st = os.statvfs("/dev/shm")
        free_mb = (st.f_bavail * st.f_frsize) // (1024 * 1024)
        total_mb = (st.f_blocks * st.f_frsize) // (1024 * 1024)
        return {"shm_available": True, "shm_free_mb": free_mb,
                "shm_total_mb": total_mb,
                "shm_low_water": free_mb < 256}
    except Exception as e:
        return {"shm_available": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Profile runners (each returns a profile_results dict; artifact aggregator
# assembles them and runs check_safety_halts after each profile).
# ---------------------------------------------------------------------------

async def run_profile_a_cold_curl_cffi(artifact: Dict[str, Any]) -> Dict[str, Any]:
    """Profile A — cold curl_cffi, no browser warmup, no production client.

    Uses curl_cffi.requests with the configured proxy, fresh session per request.
    Per-endpoint retries ≤ MAX_RETRIES_PER_ENDPOINT (2). Per-event cooldown 10s.
    """
    from curl_cffi import requests as curl_requests

    profile_result: Dict[str, Any] = {
        "profile": "cold_curl_cffi",
        "requests": [],
        "summary": {},
        "note": "diagnostic only, not production baseline",
    }
    proxy_url = None
    if os.getenv("SOFA_PROXY_HOST") and os.getenv("SOFA_PROXY_PORT"):
        u = os.getenv("SOFA_PROXY_USER", "")
        p = os.getenv("SOFA_PROXY_PASS", "")
        auth = f"{u}:{p}@" if u else ""
        proxy_url = f"http://{auth}{os.getenv('SOFA_PROXY_HOST')}:{os.getenv('SOFA_PROXY_PORT')}"
    base = "https://www.sofascore.com"

    req_idx = 0
    for event_id in sorted(APPROVED_EVENTS):
        for endpoint in ROUND1_ENDPOINTS:
            if endpoint not in CONFIGURED_ENDPOINTS:
                # mark as not_configured — no live request
                profile_result["requests"].append({
                    "event_id": event_id, "endpoint": endpoint,
                    "path_redacted": ENDPOINT_PATHS_PROD.get(endpoint, f"/api/v1/event/{event_id}/{endpoint}"),
                    "attempts": [], "status": 0, "transport": None,
                    "latency_ms": 0, "payload_complete": False,
                    "fallback_used": False, "retry_count": 0,
                    "browser_rebuilt": False, "epipe": False,
                    "error_class": "not_configured",
                    "ok": False, "skipped": True,
                    "skip_reason": "endpoint not in production ENDPOINT_PATHS",
                })
                continue
            path = ENDPOINT_PATHS_PROD[endpoint].format(event_id=event_id)
            url = f"{base}{path}"
            attempts = []
            ok = False
            body = None
            fallback_used = False
            retry_count = 0
            browser_rebuilt = False
            epipe = False
            error_class = None
            for retry in range(MAX_RETRIES_PER_ENDPOINT + 1):
                retry_count = retry
                t0 = time.monotonic()
                status = 0
                err = None
                try:
                    resp = curl_requests.get(
                        url,
                        impersonate="chrome",
                        timeout=30,
                        proxies={"http": proxy_url, "https": proxy_url} if proxy_url else None,
                        headers={"Accept": "application/json"},
                    )
                    status = resp.status_code
                    if status == 200:
                        try:
                            body = resp.json()
                        except Exception as json_exc:
                            err = f"json parse failed: {json_exc}"
                            error_class = "json_error"
                    attempts.append(make_attempt_record("curl_cffi", status, int((time.monotonic()-t0)*1000), err))
                except Exception as exc:
                    err = f"transport failure: {exc}"
                    attempts.append(make_attempt_record("curl_cffi", 0, int((time.monotonic()-t0)*1000), err))
                    error_class = "transport_failure"
                    epipe = "EPIPE" in str(exc)
                if status == 200:
                    ok = True
                    break
                if retry < MAX_RETRIES_PER_ENDPOINT:
                    # rotate proxy if available
                    await asyncio.sleep(1.5)
            req_idx += 1
            rec = make_result_record(
                request_idx=req_idx,
                event_id=event_id, endpoint=endpoint, path=path,
                attempts=attempts, body=body, ok=ok,
                fallback_used=fallback_used, retry_count=retry_count,
                browser_rebuilt=browser_rebuilt, epipe=epipe,
                error_class=error_class,
            )
            profile_result["requests"].append(rec)
        await asyncio.sleep(COOLDOWN_BETWEEN_EVENTS_S)

    profile_result["summary"] = compute_overall_metrics(profile_result["requests"])
    return profile_result


async def run_profile_b_production_hybrid(artifact: Dict[str, Any]) -> Dict[str, Any]:
    """Profile B — production BackfillClient.fetch_api() with prefer_capture=False.

    Uses the exact production code path. Limited retries (≤2) per endpoint
    to honor the prompt's budget rule. Stops at safety halt.
    """
    profile_result: Dict[str, Any] = {
        "profile": "production_hybrid",
        "requests": [],
        "summary": {},
        "note": "uses real BackfillClient.fetch_api(..., max_retries=2, prefer_capture=False)",
    }
    try:
        from backfill_runner import BackfillClient
    except Exception as e:
        profile_result["error"] = f"import failed: {e}"
        return profile_result

    req_idx = 0
    client = BackfillClient(headless=True)
    try:
        await client.__aenter__()
        for event_id in sorted(APPROVED_EVENTS):
            for endpoint in ROUND1_ENDPOINTS:
                if endpoint not in CONFIGURED_ENDPOINTS:
                    profile_result["requests"].append({
                        "event_id": event_id, "endpoint": endpoint,
                        "path_redacted": f"/api/v1/event/{event_id}/{endpoint}",
                        "attempts": [], "status": 0, "transport": None,
                        "latency_ms": 0, "payload_complete": False,
                        "fallback_used": False, "retry_count": 0,
                        "browser_rebuilt": False, "epipe": False,
                        "error_class": "not_configured",
                        "ok": False, "skipped": True,
                        "skip_reason": "endpoint not in production ENDPOINT_PATHS",
                    })
                    continue
                path = ENDPOINT_PATHS_PROD[endpoint].format(event_id=event_id)
                attempts = []
                ok = False
                body = None
                fallback_used = False
                retry_count = 0
                browser_rebuilt_before = getattr(client, "_rebuild_count", 0)
                epipe = False
                error_class = None
                for retry in range(MAX_RETRIES_PER_ENDPOINT + 1):
                    retry_count = retry
                    t0 = time.monotonic()
                    status = 0
                    err = None
                    try:
                        result = await client.fetch_api(
                            path, timeout_ms=30000,
                            max_retries=1,  # internal retries capped at 1 per our retry
                            prefer_capture=False,
                        )
                        status = result.get("status", 0)
                        body = result.get("body")
                        transport = result.get("transport", "unknown")
                        if status == 200 and body is None:
                            err = "empty body"
                            error_class = "success_empty"
                        attempts.append(make_attempt_record(transport, status, int((time.monotonic()-t0)*1000), err))
                        if transport and transport != "curl_cffi":
                            fallback_used = True
                        if status == 200 and body is not None:
                            ok = True
                            break
                    except Exception as exc:
                        err = f"transport failure: {exc}"
                        epipe = "EPIPE" in str(exc)
                        attempts.append(make_attempt_record("unknown", 0, int((time.monotonic()-t0)*1000), err))
                        error_class = "transport_failure"
                        break  # don't retry on transport exception
                    if retry < MAX_RETRIES_PER_ENDPOINT:
                        await asyncio.sleep(2.0)
                browser_rebuilt = (getattr(client, "_rebuild_count", 0) - browser_rebuilt_before) > 0
                req_idx += 1
                rec = make_result_record(
                    request_idx=req_idx,
                    event_id=event_id, endpoint=endpoint, path=path,
                    attempts=attempts, body=body, ok=ok,
                    fallback_used=fallback_used, retry_count=retry_count,
                    browser_rebuilt=browser_rebuilt, epipe=epipe,
                    error_class=error_class,
                )
                profile_result["requests"].append(rec)
                # Per-prompt: stop on 407 terminal
                if any(a.get("status") == 407 for a in attempts):
                    profile_result["halt_reason"] = "HTTP_407"
                    break
            if profile_result.get("halt_reason") == "HTTP_407":
                break
            await asyncio.sleep(COOLDOWN_BETWEEN_EVENTS_S)
    except Exception as outer:
        profile_result["error"] = f"outer exception: {outer}"
    finally:
        try:
            await client.__aexit__(None, None, None)
        except Exception:
            pass

    profile_result["summary"] = compute_overall_metrics(profile_result["requests"])
    return profile_result


async def run_profile_c_warm_session_hybrid(artifact: Dict[str, Any]) -> Dict[str, Any]:
    """Profile C — warm session: warm homepage + each event page explicitly,
    then fetch via curl_cffi (cookies exported from warmed context).
    """
    profile_result: Dict[str, Any] = {
        "profile": "warm_session_hybrid",
        "requests": [],
        "summary": {},
        "note": "explicitly warm homepage + event page before curl_cffi fetch",
        "warm_session_supported": True,
    }
    try:
        from backfill_runner import BackfillClient
    except Exception as e:
        profile_result["error"] = f"import failed: {e}"
        profile_result["warm_session_supported"] = False
        return profile_result

    req_idx = 0
    client = BackfillClient(headless=True)
    try:
        await client.__aenter__()
        # Explicit warm homepage (already done in __aenter__ but be explicit)
        try:
            await client.warm_homepage()
        except Exception as warm_exc:
            profile_result["warm_homepage_error"] = str(warm_exc)

        for event_id in sorted(APPROVED_EVENTS):
            # Explicit warm event page for each event
            try:
                await client.warm_event(event_id)
            except Exception as warm_ev:
                profile_result.setdefault("warm_event_errors", []).append({event_id: str(warm_ev)})

            for endpoint in ROUND1_ENDPOINTS:
                if endpoint not in CONFIGURED_ENDPOINTS:
                    profile_result["requests"].append({
                        "event_id": event_id, "endpoint": endpoint,
                        "path_redacted": f"/api/v1/event/{event_id}/{endpoint}",
                        "attempts": [], "status": 0, "transport": None,
                        "latency_ms": 0, "payload_complete": False,
                        "fallback_used": False, "retry_count": 0,
                        "browser_rebuilt": False, "epipe": False,
                        "error_class": "not_configured",
                        "ok": False, "skipped": True,
                        "skip_reason": "endpoint not in production ENDPOINT_PATHS",
                    })
                    continue
                path = ENDPOINT_PATHS_PROD[endpoint].format(event_id=event_id)
                attempts = []
                ok = False
                body = None
                fallback_used = False
                retry_count = 0
                browser_rebuilt_before = getattr(client, "_rebuild_count", 0)
                epipe = False
                error_class = None
                for retry in range(MAX_RETRIES_PER_ENDPOINT + 1):
                    retry_count = retry
                    t0 = time.monotonic()
                    status = 0
                    err = None
                    transport = "unknown"
                    try:
                        # Use _fetch_via_curl_cffi directly to isolate the warm-cookie effect
                        result = await client._fetch_via_curl_cffi(path, 30000)
                        status = result.get("status", 0)
                        body = result.get("body")
                        transport = result.get("transport", "curl_cffi")
                        if status == 200 and body is None:
                            err = "empty body"
                            error_class = "success_empty"
                        attempts.append(make_attempt_record(transport, status, int((time.monotonic()-t0)*1000), err))
                        if status == 200 and body is not None:
                            ok = True
                            break
                    except Exception as exc:
                        err = f"transport failure: {exc}"
                        epipe = "EPIPE" in str(exc)
                        attempts.append(make_attempt_record(transport, 0, int((time.monotonic()-t0)*1000), err))
                        error_class = "transport_failure"
                        break
                    if retry < MAX_RETRIES_PER_ENDPOINT:
                        await asyncio.sleep(2.0)
                browser_rebuilt = (getattr(client, "_rebuild_count", 0) - browser_rebuilt_before) > 0
                req_idx += 1
                rec = make_result_record(
                    request_idx=req_idx,
                    event_id=event_id, endpoint=endpoint, path=path,
                    attempts=attempts, body=body, ok=ok,
                    fallback_used=fallback_used, retry_count=retry_count,
                    browser_rebuilt=browser_rebuilt, epipe=epipe,
                    error_class=error_class,
                )
                profile_result["requests"].append(rec)
            await asyncio.sleep(COOLDOWN_BETWEEN_EVENTS_S)
    except Exception as outer:
        profile_result["error"] = f"outer exception: {outer}"
    finally:
        try:
            await client.__aexit__(None, None, None)
        except Exception:
            pass

    profile_result["summary"] = compute_overall_metrics(profile_result["requests"])
    return profile_result


async def run_profile_d_browser_fallback_controlled(artifact: Dict[str, Any]) -> Dict[str, Any]:
    """Profile D — controlled curl_cffi forced fail → Playwright browser tier."""
    profile_result: Dict[str, Any] = {
        "profile": "browser_fallback_controlled",
        "requests": [],
        "summary": {},
        "note": "controlled: curl_cffi forced 403 → Playwright browser",
    }
    try:
        from backfill_runner import BackfillClient
    except Exception as e:
        profile_result["error"] = f"import failed: {e}"
        return profile_result

    def _forced_fail_curl(url, **kwargs):
        class _FakeResp:
            status_code = 403
            text = "Forced failure for Profile D (curl_cffi)"
            def json(self): raise ValueError("No JSON body")
        return _FakeResp()

    # Endpoints: event + lineups (different shapes; excludes not_configured highlights)
    TEST_ENDPOINTS = ["event", "lineups"]
    req_idx = 0
    client = BackfillClient(headless=True)
    try:
        await client.__aenter__()
        # Monkey-patch _fetch_via_curl_cffi to force 403
        original_fetch = client._fetch_via_curl_cffi
        async def _forced_403(path, timeout_ms):
            return {"status": 403, "body": None, "transport": "curl_cffi",
                    "error": "forced 403 for Profile D"}
        client._fetch_via_curl_cffi = _forced_403
        for event_id in sorted(APPROVED_EVENTS):
            for endpoint in TEST_ENDPOINTS:
                if endpoint not in CONFIGURED_ENDPOINTS:
                    continue
                path = ENDPOINT_PATHS_PROD[endpoint].format(event_id=event_id)
                attempts = []
                ok = False
                body = None
                fallback_used = False
                retry_count = 0
                browser_rebuilt_before = getattr(client, "_rebuild_count", 0)
                epipe = False
                error_class = None
                t0 = time.monotonic()
                try:
                    # Use production fetch_api with monkey-patched tier 1
                    result = await client.fetch_api(path, timeout_ms=30000,
                                                     max_retries=1, prefer_capture=False)
                    status = result.get("status", 0)
                    body = result.get("body")
                    transport = result.get("transport", "unknown")
                    attempts.append(make_attempt_record(transport, status, int((time.monotonic()-t0)*1000)))
                    if status == 200 and body is not None:
                        ok = True
                        fallback_used = transport != "curl_cffi"
                    else:
                        error_class = "browser_failed"
                except Exception as exc:
                    attempts.append(make_attempt_record("unknown", 0, int((time.monotonic()-t0)*1000), str(exc)))
                    error_class = "transport_failure"
                    epipe = "EPIPE" in str(exc)
                browser_rebuilt = (getattr(client, "_rebuild_count", 0) - browser_rebuilt_before) > 0
                req_idx += 1
                rec = make_result_record(
                    request_idx=req_idx,
                    event_id=event_id, endpoint=endpoint, path=path,
                    attempts=attempts, body=body, ok=ok,
                    fallback_used=fallback_used, retry_count=retry_count,
                    browser_rebuilt=browser_rebuilt, epipe=epipe,
                    error_class=error_class,
                )
                profile_result["requests"].append(rec)
            await asyncio.sleep(COOLDOWN_BETWEEN_EVENTS_S)
    except Exception as outer:
        profile_result["error"] = f"outer exception: {outer}"
    finally:
        try:
            await client.__aexit__(None, None, None)
        except Exception:
            pass

    profile_result["summary"] = compute_overall_metrics(profile_result["requests"])
    return profile_result


async def run_profile_e_ssr_fallback_controlled(artifact: Dict[str, Any]) -> Dict[str, Any]:
    """Profile E — controlled: curl_cffi + Playwright forced fail → SSR tier.

    We bypass fetch_api and directly call BackfillClient.get_event_ssr to
    measure SSR tier isolation. fetch_api's internal loop would short-circuit
    on 403+0 status without retrying into SSR unless max_retries >= 5.

    NOTE: Per Phase 5B discovery (2026-08-24), SofaScore event SSR HTML
    __NEXT_DATA__ embeds ONLY the `event` hydration node; incidents and other
    sub-endpoints are client-side API calls and CANNOT be served from SSR.
    This is a documented structural gap, not a bug.
    """
    profile_result: Dict[str, Any] = {
        "profile": "ssr_fallback_controlled",
        "requests": [],
        "summary": {},
        "note": "controlled: curl_cffi + Playwright forced fail → SSR (direct get_event_ssr)",
        "structural_gaps": ["incidents", "lineups", "statistics", "shotmap",
                              "graph", "comments", "odds"],
        "evidence": "data/debug_ssr_raw.json pageProps keys do NOT include 'incidents'; "
                    "pageProps.event is a 26-key object without 'incidents'.",
    }
    try:
        from backfill_runner import BackfillClient
    except Exception as e:
        profile_result["error"] = f"import failed: {e}"
        return profile_result

    TEST_ENDPOINTS = ["event", "incidents"]
    req_idx = 0
    client = BackfillClient(headless=True)
    try:
        await client.__aenter__()
        # Mark tier-1 and tier-2 as 'forced failed' for transparency.
        tier1_fail = {"transport": "curl_cffi", "status": 403, "latency_ms": 0,
                      "error": "forced 403 (Profile E controlled failure)"}
        tier2_fail = {"transport": "browser", "status": 0, "latency_ms": 0,
                      "error": "forced browser failure (Profile E controlled failure)"}

        # SSR via independent Playwright (same pattern as Phase 5B).
        from playwright.sync_api import sync_playwright
        ssr_pages: Dict[int, Optional[dict]] = {}

        def _load_ssr_page(event_id: int) -> Optional[dict]:
            """Sync helper: load event page and extract __NEXT_DATA__."""
            if event_id in ssr_pages:
                return ssr_pages[event_id]
            url = f"https://www.sofascore.com/event/{event_id}"
            payload = None
            try:
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    try:
                        page = browser.new_page()
                        page.goto(url, timeout=30000)
                        page.wait_for_selector("#__NEXT_DATA__", timeout=15000)
                        raw = page.evaluate(
                            "() => { const el = document.querySelector('#__NEXT_DATA__');"
                            " return el ? el.textContent : null; }"
                        )
                        if raw:
                            data = json.loads(raw)
                            from backfill_runner import _map_ssr_to_api_format
                            payload = _map_ssr_to_api_format(data, f"/api/v1/event/{event_id}")
                    finally:
                        browser.close()
            except Exception:
                payload = None
            ssr_pages[event_id] = payload
            return payload

        for event_id in sorted(APPROVED_EVENTS):
            for endpoint in TEST_ENDPOINTS:
                if endpoint not in CONFIGURED_ENDPOINTS:
                    continue
                path = ENDPOINT_PATHS_PROD[endpoint].format(event_id=event_id)
                attempts = [dict(tier1_fail), dict(tier2_fail)]
                ok = False
                body = None
                fallback_used = True
                retry_count = 0
                browser_rebuilt = False
                epipe = False
                error_class = None
                t0 = time.monotonic()
                try:
                    ssr_body = await asyncio.to_thread(_load_ssr_page, event_id)
                    latency = int((time.monotonic() - t0) * 1000)
                    if ssr_body is not None:
                        status = 200
                        body = ssr_body
                        ok = True
                        attempts.append({"transport": "ssr", "status": 200,
                                          "latency_ms": latency, "error": None})
                    else:
                        status = 0
                        error_class = "ssr_returned_none"
                        attempts.append({"transport": "ssr", "status": 0,
                                          "latency_ms": latency,
                                          "error": "ssr_returned_none (structural gap)"})
                except Exception as exc:
                    latency = int((time.monotonic() - t0) * 1000)
                    attempts.append({"transport": "ssr", "status": 0,
                                      "latency_ms": latency,
                                      "error": f"ssr exception: {exc}"})
                    error_class = "ssr_exception"
                    epipe = "EPIPE" in str(exc)
                req_idx += 1
                rec = make_result_record(
                    request_idx=req_idx,
                    event_id=event_id, endpoint=endpoint, path=path,
                    attempts=attempts, body=body, ok=ok,
                    fallback_used=fallback_used, retry_count=retry_count,
                    browser_rebuilt=browser_rebuilt, epipe=epipe,
                    error_class=error_class,
                )
                profile_result["requests"].append(rec)
            await asyncio.sleep(COOLDOWN_BETWEEN_EVENTS_S)
    except Exception as outer:
        profile_result["error"] = f"outer exception: {outer}"
    finally:
        try:
            await client.__aexit__(None, None, None)
        except Exception:
            pass

    profile_result["summary"] = compute_overall_metrics(profile_result["requests"])
    return profile_result


PROFILES = [
    ("cold_curl_cffi", run_profile_a_cold_curl_cffi),
    ("production_hybrid", run_profile_b_production_hybrid),
    ("warm_session_hybrid", run_profile_c_warm_session_hybrid),
    ("browser_fallback_controlled", run_profile_d_browser_fallback_controlled),
    ("ssr_fallback_controlled", run_profile_e_ssr_fallback_controlled),
]


async def main() -> Dict[str, Any]:
    started_at = utc_now_iso()
    artifact: Dict[str, Any] = {
        "strategy": "gen2",
        "default_strategy": "gen2",
        "kris_approval": None,  # to be set on live launch
        "started_at_utc": started_at,
        "env_check": env_check(),
        "shm_check": check_shm(),
        "approved_events": sorted(APPROVED_EVENTS),
        "endpoints": [ep for ep in ROUND1_ENDPOINTS
                       if ep in CONFIGURED_ENDPOINTS],
        "endpoints_not_configured": NOT_CONFIGURED_IN_ROUND1,
        "request_budget": {
            "max_total": MAX_TOTAL_REQUESTS,
            "max_per_event": MAX_PER_EVENT,
            "max_retries_per_endpoint": MAX_RETRIES_PER_ENDPOINT,
            "cooldown_between_events_s": COOLDOWN_BETWEEN_EVENTS_S,
            "cooldown_between_profiles_s": COOLDOWN_BETWEEN_PROFILES_S,
        },
        "profiles": [p[0] for p in PROFILES],
        "profiles_results": {},
        "results": [],
        "limitations": [
            "highlights is not in production ENDPOINT_PATHS — marked not_configured; no live request.",
            "Only 2 approved events exist (14025013, 12436875); no scope expansion.",
            "Cold curl_cffi profile is diagnostic only, not production baseline.",
            "Profile C (warm session) may be marked warm_session_not_supported if production code does not export cookies to curl_cffi.",
        ],
        "write_enabled": WRITE_ENABLED,
        "credentials_redacted": True,
        "partial_run": False,
        "halted": False,
        "halt_reason": None,
        "verdict": None,
    }

    for profile_name, profile_fn in PROFILES:
        profile_result = await profile_fn(artifact)
        artifact["profiles_results"][profile_name] = profile_result
        # Aggregate results across profiles for halt checks
        for r in profile_result.get("requests", []):
            artifact["results"].append(r)
        halt = check_safety_halts(artifact)
        if halt is not None:
            artifact["halted"] = True
            artifact["halt_reason"] = halt
            artifact["partial_run"] = True
            break
        await asyncio.sleep(COOLDOWN_BETWEEN_PROFILES_S)

    artifact["ended_at_utc"] = utc_now_iso()
    artifact["metrics"] = compute_overall_metrics(artifact["results"])
    artifact["verdict"] = compute_verdict(artifact)

    # Persist
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    json_path = REPO / "data" / f"gen2_full_validation_{ts}.json"
    md_path = REPO / "data" / f"gen2_full_validation_{ts}.md"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(redact_secrets(json.dumps(artifact, indent=2, default=str)),
                          encoding="utf-8")
    md_path.write_text(render_markdown(artifact), encoding="utf-8")
    print(f"[artifact] wrote {json_path}")
    print(f"[summary] wrote {md_path}")
    print(f"\n=== Verdict: {artifact['verdict']} ===")
    return artifact


def compute_overall_metrics(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not results:
        return {"http_success_rate": 0.0, "final_usable_success_rate": 0.0,
                "payload_completeness_rate": 0.0, "endpoint_coverage_rate": 0.0,
                "p50_latency_ms": None, "p90_latency_ms": None,
                "n_403": 0, "n_407": 0, "n_404": 0, "n_total": 0,
                "fallback_count": 0, "epipe_count": 0}
    n = len(results)
    n_200 = sum(1 for r in results if r.get("final_status") == 200)
    n_final = sum(1 for r in results if r.get("ok") and r.get("payload_complete"))
    n_complete = sum(1 for r in results if r.get("payload_complete"))
    attempted_pairs = {(r.get("event_id"), r.get("endpoint")) for r in results}
    complete_pairs = {(r.get("event_id"), r.get("endpoint")) for r in results
                       if r.get("payload_complete")}
    coverage = (len(complete_pairs) / len(attempted_pairs)) if attempted_pairs else 0.0
    latencies = sorted([r.get("latency_ms", 0) for r in results])
    p50 = latencies[len(latencies) // 2]
    p90_idx = max(0, int(len(latencies) * 0.9) - 1)
    p90 = latencies[p90_idx]
    return {
        "n_total": n,
        "http_success_rate": n_200 / n,
        "final_usable_success_rate": n_final / n,
        "payload_completeness_rate": n_complete / n,
        "endpoint_coverage_rate": coverage,
        "n_200": n_200,
        "n_403": sum(1 for r in results if r.get("final_status") == 403),
        "n_404": sum(1 for r in results if r.get("final_status") == 404),
        "n_407": sum(1 for r in results if r.get("final_status") == 407),
        "p50_latency_ms": p50,
        "p90_latency_ms": p90,
        "fallback_count": sum(1 for r in results if r.get("fallback_used")),
        "epipe_count": sum(1 for r in results if r.get("epipe")),
    }


def compute_verdict(artifact: Dict[str, Any]) -> str:
    if artifact.get("halted"):
        return "VALIDATION_FAILED"
    if artifact.get("partial_run"):
        return "VALIDATION_PARTIAL"
    metrics = artifact.get("metrics", {})
    if metrics.get("n_407", 0) > 0:
        return "VALIDATION_FAILED"
    if metrics.get("n_total", 0) == 0:
        return "VALIDATION_PARTIAL"
    coverage = metrics.get("endpoint_coverage_rate", 0.0)
    if coverage >= 0.99:
        return "VALIDATION_PASS"
    return "VALIDATION_PARTIAL"


def render_markdown(artifact: Dict[str, Any]) -> str:
    metrics = artifact.get("metrics", {})
    lines = [
        "# Gen2 Full Validation Report",
        "",
        f"- **Verdict:** **{artifact.get('verdict')}**",
        f"- **Halted:** {artifact.get('halted')} ({artifact.get('halt_reason')})",
        f"- **Partial run:** {artifact.get('partial_run')}",
        f"- **Started:** {artifact.get('started_at_utc')}",
        f"- **Ended:** {artifact.get('ended_at_utc')}",
        f"- **Write enabled:** {artifact.get('write_enabled')}",
        f"- **Strategy:** {artifact.get('strategy')} (default: {artifact.get('default_strategy')})",
        "",
        "## Env Check",
        "```json",
        json.dumps(artifact.get("env_check", {}), indent=2),
        "```",
        "",
        "## /dev/shm",
        "```json",
        json.dumps(artifact.get("shm_check", {}), indent=2),
        "```",
        "",
        "## Scope",
        f"- Events: {artifact.get('approved_events')}",
        f"- Configured endpoints: {artifact.get('endpoints')}",
        f"- Not configured (marked): {artifact.get('endpoints_not_configured')}",
        f"- Budget: {artifact.get('request_budget')}",
        "",
        "## Metrics",
        f"- HTTP 200 rate: {metrics.get('http_success_rate'):.1%}",
        f"- Final usable rate: {metrics.get('final_usable_success_rate'):.1%}",
        f"- Payload completeness: {metrics.get('payload_completeness_rate'):.1%}",
        f"- Endpoint coverage: {metrics.get('endpoint_coverage_rate'):.1%}",
        f"- 403 count: {metrics.get('n_403')}",
        f"- 404 count: {metrics.get('n_404')}",
        f"- 407 count: {metrics.get('n_407')}",
        f"- p50 / p90 latency (ms): {metrics.get('p50_latency_ms')} / {metrics.get('p90_latency_ms')}",
        "",
        "## Limitations",
    ]
    for lim in artifact.get("limitations", []):
        lines.append(f"- {lim}")
    return "\n".join(lines)


if __name__ == "__main__":
    asyncio.run(main())
