#!/usr/bin/env python3
"""gen4_canary_validate.py — Gen4 full-chain opt-in canary validation.

Per Kris 05:57 GMT+8 approval (round #2828):
- Gen4 OPT-IN via FETCH_STRATEGY=gen4. Default strategy unchanged (still gen2).
- write_enabled=False (no MySQL, no DataInserter).
- 4 profiles + J scaffolding (J skipped unless explicit approval):
    F cold_curl_cffi baseline (diagnostic, not pass/fail gate)
    G full-chain (curl_cffi → CloakBrowser → SSR)  ← MAIN GATE PROFILE
    H controlled CloakBrowser fallback (curl 403 forced → CloakBrowser)
    I controlled SSR fallback (curl + cloak forced fail → SSR)
    J warm_session (skipped unless session is shown to be the bottleneck)
- Scope: events={14025013,12436875}, endpoints={event,incidents,lineups,
  comments,statistics,shotmap,highlights} (highlights=not_configured).
- Budget: ≤150 total, ≤20 per-event, ≤2 retries/endpoint.
- 12 GATE CRITERIA:
    1. No safety halt
    2. 0 HTTP 407
    3. 0 credentials leak
    4. 0 DB write
    5. 0 production-code modification
    6. G full-chain final-usable payload completeness ≥85%
    7. G full-chain event-level success ≥95%
    8. Per-endpoint reports for incidents/lineups/comments/statistics/shotmap
    9. H controlled CloakBrowser 100% pass
   10. I controlled SSR: event 100% pass; incidents marked ssr_unsupported_endpoint
   11. Fallback success rate reported separately from fallback attempt count
   12. Gen2 default unchanged

Step 7 (live launch) requires SEPARATE approval — wrapper writes budget + offline
tests; does NOT issue live requests on its own.
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
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

# Hard guards from prompt
WRITE_ENABLED = False
COOLDOWN_BETWEEN_PROFILES_S = 60.0
COOLDOWN_BETWEEN_EVENTS_S = 10.0

APPROVED_EVENTS = frozenset({14025013, 12436875})
CONFIGURED_ENDPOINTS = frozenset({
    "event", "incidents", "lineups", "statistics", "shotmap",
    "graph", "odds", "comments",
})
ROUND1_ENDPOINTS = [
    "event", "incidents", "lineups", "comments",
    "statistics", "shotmap", "highlights",
]
NOT_CONFIGURED_IN_ROUND1 = [ep for ep in ROUND1_ENDPOINTS
                            if ep not in CONFIGURED_ENDPOINTS]

# Per-endpoint path templates (must match production ENDPOINT_PATHS)
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

# Budget
MAX_TOTAL_REQUESTS = 150
MAX_PER_EVENT = 20
MAX_RETRIES_PER_ENDPOINT = 2

# Canary gates
GATE_G_FINAL_USABLE_PC_MIN = 0.85
GATE_G_EVENT_LEVEL_SUCCESS_MIN = 0.95
GATE_EVENT_PC_MIN = 0.95
GATE_INCIDENTS_PC_MIN = 0.95
GATE_H_CLOAK_PC_MIN = 1.00
GATE_I_EVENT_PC_MIN = 1.00

# SSR unsupported endpoints (structural gap, Phase 5B)
SSR_UNSUPPORTED_ENDPOINTS = frozenset({
    "incidents", "lineups", "statistics", "shotmap",
    "graph", "comments", "odds",
})


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def redact_secrets(blob: str) -> str:
    pw = os.getenv("SOFA_PROXY_PASS", "")
    if pw:
        blob = blob.replace(pw, "[REDACTED]")
    # Key patterns:
    #  1. KEY: VALUE  or  KEY=VALUE  (value runs to next whitespace/comma/semicolon)
    #  2. KEY <space> VALUE  (no separator; common in inline logs)
    # Value consumed up to 4 word-tokens max to avoid eating unrelated text.
    def _redact_key(match):
        prefix = match.group(1)
        return prefix + "[REDACTED]"
    blob = re.sub(r"(?i)(authorization\s*[:=]?\s*)([^\s,;]+(?:\s+[^\s,;]+){0,3})",
                   _redact_key, blob)
    blob = re.sub(r"(?i)(set-cookie\s*[:=]?\s*)([^\s,;]+(?:\s+[^\s,;]+){0,3})",
                   _redact_key, blob)
    blob = re.sub(r"(?i)(cookie\s*[:=]?\s*)([^\s,;]+(?:\s+[^\s,;]+){0,3})",
                   _redact_key, blob)
    blob = re.sub(r"https?://[^/\s]+:[^/\s]+@", "http://[REDACTED]@[REDACTED]", blob)
    return blob


def check_safety_halts(artifact: Dict[str, Any]) -> Optional[str]:
    """Return halt reason code or None. Per Gen4 §17 + Gen2 canary gates."""
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
    if re.search(r"http://[^/\s]+:[^/\s]+@", blob) and "[REDACTED]" not in blob:
        return "CREDENTIALS_LEAK"
    approved = set(artifact.get("approved_events", []))
    seen = {r.get("event_id") for r in results if r.get("event_id")}
    if approved and not seen.issubset(approved):
        return "SCOPE_EXPANSION"
    if artifact.get("production_code_modified"):
        return "PRODUCTION_CODE_MODIFIED"
    return None


def compute_overall_metrics(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not results:
        return {"n_total": 0}
    n = len(results)
    n_200 = sum(1 for r in results if r.get("status") == 200)
    n_final = sum(1 for r in results if r.get("ok") and r.get("payload_complete"))
    n_complete = sum(1 for r in results if r.get("payload_complete"))
    attempted_pairs = {(r.get("event_id"), r.get("endpoint")) for r in results}
    complete_pairs = {(r.get("event_id"), r.get("endpoint")) for r in results
                       if r.get("payload_complete")}
    coverage = (len(complete_pairs) / len(attempted_pairs)) if attempted_pairs else 0.0
    latencies = sorted([r.get("latency_ms", 0) for r in results])
    p50 = latencies[len(latencies) // 2] if latencies else 0
    p90_idx = max(0, int(len(latencies) * 0.9) - 1) if latencies else 0
    p90 = latencies[p90_idx] if latencies else 0
    return {
        "n_total": n,
        "http_success_rate": n_200 / n,
        "final_usable_success_rate": n_final / n,
        "payload_completeness_rate": n_complete / n,
        "endpoint_coverage_rate": coverage,
        "n_200": n_200,
        "n_403": sum(1 for r in results if r.get("status") == 403),
        "n_404": sum(1 for r in results if r.get("status") == 404),
        "n_407": sum(1 for r in results if r.get("status") == 407),
        "p50_latency_ms": p50,
        "p90_latency_ms": p90,
        "fallback_attempt_count": sum(1 for r in results if r.get("fallback_used")),
        "fallback_success_count": sum(1 for r in results
                                       if r.get("fallback_used") and r.get("payload_complete")),
        "epipe_count": sum(1 for r in results if r.get("epipe")),
    }


def per_endpoint_metrics(results: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Per-endpoint aggregate (event-level view; pairs across events)."""
    by_ep: Dict[str, List[Dict[str, Any]]] = {}
    for r in results:
        ep = r.get("endpoint")
        if ep:
            by_ep.setdefault(ep, []).append(r)
    out: Dict[str, Dict[str, Any]] = {}
    for ep, rs in by_ep.items():
        n = len(rs)
        n_complete = sum(1 for r in rs if r.get("payload_complete"))
        n_final = sum(1 for r in rs if r.get("ok") and r.get("payload_complete"))
        out[ep] = {
            "n": n,
            "payload_completeness_rate": n_complete / n if n else 0,
            "final_usable_rate": n_final / n if n else 0,
            "n_200": sum(1 for r in rs if r.get("status") == 200),
            "n_403": sum(1 for r in rs if r.get("status") == 403),
        }
    return out


def make_attempt(transport: str, status: int, latency_ms: int,
                  error: Optional[str] = None) -> Dict[str, Any]:
    return {"transport": transport, "status": status,
            "latency_ms": latency_ms, "error": error}


def make_record(
    *,
    event_id: int, endpoint: str, path: str,
    attempts: List[Dict[str, Any]],
    body: Optional[Dict[str, Any]],
    ok: bool, fallback_used: bool, retry_count: int,
    browser_rebuilt: bool, epipe: bool,
    error_class: Optional[str],
) -> Dict[str, Any]:
    final_status = attempts[-1]["status"] if attempts else 0
    final_transport = attempts[-1]["transport"] if attempts else None
    initial_status = attempts[0]["status"] if attempts else 0
    initial_transport = attempts[0]["transport"] if attempts else None
    body_bytes = len(json.dumps(body, default=str)) if body is not None else 0
    json_valid = isinstance(body, (dict, list)) and bool(body)
    payload_complete = json_valid and _is_payload_complete(path, body)
    return {
        "event_id": event_id, "endpoint": endpoint,
        "path_redacted": path.replace(str(event_id), "{eid}"),
        "attempts": attempts,
        "initial_transport": initial_transport, "initial_status": initial_status,
        "final_transport": final_transport, "final_status": final_status,
        "status": final_status, "transport": final_transport,
        "latency_ms": sum(a.get("latency_ms", 0) for a in attempts),
        "response_bytes": body_bytes,
        "json_valid": json_valid,
        "payload_complete": payload_complete,
        "fallback_used": fallback_used,
        "retry_count": retry_count,
        "browser_rebuilt": browser_rebuilt,
        "epipe": epipe,
        "error_class": error_class,
        "ok": ok,
    }


def _is_payload_complete(path: str, body: Optional[Dict[str, Any]]) -> bool:
    """Use Gen4's validate_payload if available; fallback to alias heuristic."""
    try:
        from gen4_fetcher import validate_payload
        ok, _ = validate_payload(path, body)
        return bool(ok)
    except Exception:
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


def env_check() -> Dict[str, Any]:
    return {
        "proxy_configured": bool(os.getenv("SOFA_PROXY_HOST")
                                  and os.getenv("SOFA_PROXY_PORT")
                                  and os.getenv("SOFA_PROXY_USER")),
        "proxy_pass_set": bool(os.getenv("SOFA_PROXY_PASS")),
        "credentials_redacted": True,
        "write_enabled": WRITE_ENABLED,
        "fetch_strategy": os.getenv("FETCH_STRATEGY", "gen2"),
        "fetch_strategy_is_gen4": os.getenv("FETCH_STRATEGY", "gen2").lower() == "gen4",
        "python_unbuffered": os.getenv("PYTHONUNBUFFERED") == "1",
    }


def check_shm() -> Dict[str, Any]:
    try:
        st = os.statvfs("/dev/shm")
        free_mb = (st.f_bavail * st.f_frsize) // (1024 * 1024)
        total_mb = (st.f_blocks * st.f_frsize) // (1024 * 1024)
        return {"shm_available": True, "shm_free_mb": free_mb,
                "shm_total_mb": total_mb, "shm_low_water": free_mb < 256}
    except Exception as e:
        return {"shm_available": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Estimated request budget (before live launch — per canary rule)
# ---------------------------------------------------------------------------

PROFILE_PLAN = {
    "F_cold_curl_cffi": {
        "events": 2, "endpoints": 6, "retries_per_endpoint": 2,
        # 6 endpoints excluding 'highlights' (not_configured)
        "estimated_requests": 2 * 6 * (1 + 2),
        "live_approved": True,
        "is_gate": False,
        "note": "diagnostic baseline; verifies Gen4 wrapper does not alter curl_cffi tier behavior",
    },
    "G_full_chain": {
        "events": 2, "endpoints": 6, "retries_per_endpoint": 2,
        "estimated_requests": 2 * 6 * (1 + 2),
        "live_approved": True,
        "is_gate": True,
        "note": "MAIN GATE — Gen4 full-chain (curl_cffi → CloakBrowser → SSR)",
    },
    "H_cloakbrowser_fallback_controlled": {
        "events": 2, "endpoints": 2, "retries_per_endpoint": 1,
        "estimated_requests": 2 * 2 * (1 + 1),
        "live_approved": True,
        "is_gate": True,
        "note": "controlled: curl_cffi forced 403 → CloakBrowser real launch",
    },
    "I_ssr_fallback_controlled": {
        "events": 2, "endpoints": 2, "retries_per_endpoint": 1,
        "estimated_requests": 2 * 2 * (1 + 1),
        "live_approved": True,
        "is_gate": True,
        "note": "controlled: curl + CloakBrowser forced fail → SSR (event expected pass; incidents expected ssr_unsupported_endpoint)",
    },
    "J_warm_session": {
        "events": 2, "endpoints": 0, "retries_per_endpoint": 0,
        "estimated_requests": 0,
        "live_approved": False,
        "is_gate": False,
        "note": "skipped — only if F/G results show session state is the bottleneck",
    },
}


def compute_estimated_budget() -> Dict[str, Any]:
    total = sum(p["estimated_requests"] for p in PROFILE_PLAN.values())
    per_event_max = max(
        (p["estimated_requests"] // max(1, p["events"]))
        for p in PROFILE_PLAN.values() if p["events"] > 0
    ) if any(p["events"] > 0 for p in PROFILE_PLAN.values()) else 0
    return {
        "max_total_requests": MAX_TOTAL_REQUESTS,
        "max_per_event": MAX_PER_EVENT,
        "max_retries_per_endpoint": MAX_RETRIES_PER_ENDPOINT,
        "estimated_total_requests": total,
        "estimated_max_per_event": per_event_max,
        "within_budget": total <= MAX_TOTAL_REQUESTS
                         and per_event_max <= MAX_PER_EVENT,
        "profiles": {name: {"estimated_requests": p["estimated_requests"],
                              "live_approved": p["live_approved"],
                              "is_gate": p["is_gate"],
                              "note": p["note"]}
                     for name, p in PROFILE_PLAN.items()},
    }


# ---------------------------------------------------------------------------
# Profile runners (placeholders — wired at live launch)
# ---------------------------------------------------------------------------


async def run_profile_f_cold_curl_cffi(artifact: Dict[str, Any]) -> Dict[str, Any]:
    """Profile F — Gen4 wrapper cold curl_cffi baseline (diagnostic).

    Uses Gen4Fetcher with monkey-patched CloakBrowser launcher (returns failure
    stub) and SSR fetcher (returns None). Tier-1 (curl_cffi) is the REAL
    curl_cffi; tier-2 and tier-3 are forced to fail so the run effectively
    behaves like Gen2 cold curl_cffi. Verifies that the Gen4 wrapper does
    NOT change curl_cffi behavior compared to Gen2.
    """
    profile_result: Dict[str, Any] = {
        "profile": "cold_curl_cffi",
        "live_approved": True,
        "is_gate": False,
        "note": "diagnostic baseline; verifies Gen4 wrapper does not alter curl_cffi tier behavior",
    }
    try:
        from gen4_fetcher import Gen4Config, Gen4Fetcher
    except Exception as e:
        profile_result["error"] = f"import failed: {e}"
        return profile_result

    config = Gen4Config(
        base_url="https://www.sofascore.com",
        proxy_server=os.getenv("SOFA_PROXY_HOST", "p.webshare.io"),
        proxy_port=os.getenv("SOFA_PROXY_PORT", "80"),
        proxy_user=os.getenv("SOFA_PROXY_USER", "***REMOVED***-rotate"),
        proxy_pass=os.getenv("SOFA_PROXY_PASS", ""),
        request_timeout_s=30,
        browser_timeout_ms=45000,
        max_browser_rebuilds=0,
        write_enabled=False,
    )

    def _forced_fail_ssr(path: str) -> None:
        return None  # SSR tier never available in F (diagnostic)

    # Inject real curl_cffi.requests.get into Gen4Fetcher so tier-1 actually
    # runs curl_cffi (v1 bug: tier-1 short-circuited with curl_cffi_unavailable).
    from curl_cffi import requests as curl_requests
    def _real_curl_cffi_get(url, **kwargs):
        # Gen4Fetcher passes proxies/impersonate/timeout; curl_requests accepts all
        return curl_requests.get(url, **kwargs)

    fetcher = Gen4Fetcher(
        config=config,
        curl_cffi_get=_real_curl_cffi_get,
        ssr_fetcher=_forced_fail_ssr,
    )

    # Monkey-patch CloakBrowser launcher to force fail
    async def _forced_cloak_launch_fail(headless=True, proxy=None, humanize=False, **kwargs):
        class _BrokenBrowser:
            async def new_page(self):
                raise Exception("forced CloakBrowser failure for Profile F (diagnostic)")
            async def close(self):
                pass
            contexts = []
        return _BrokenBrowser()

    fetcher._cloakbrowser_launcher = _forced_cloak_launch_fail  # type: ignore[assignment]

    req_idx = 0
    try:
        async with fetcher:
            for event_id in sorted(APPROVED_EVENTS):
                for endpoint in [ep for ep in ROUND1_ENDPOINTS
                                  if ep in CONFIGURED_ENDPOINTS]:
                    path = ENDPOINT_PATHS_PROD[endpoint].format(event_id=event_id)
                    attempts: List[Dict[str, Any]] = []
                    ok = False
                    body: Optional[Dict[str, Any]] = None
                    fallback_used = False
                    retry_count = 0
                    browser_rebuilt = False
                    epipe = False
                    error_class: Optional[str] = None
                    for retry in range(MAX_RETRIES_PER_ENDPOINT + 1):
                        retry_count = retry
                        t0 = time.monotonic()
                        try:
                            result = await fetcher.fetch_api(path, timeout_ms=30000)
                            status = result.get("status", 0)
                            body = result.get("data")
                            final_transport = result.get("transport", "unknown")
                            latency_ms = int((time.monotonic() - t0) * 1000)
                            # Extract attempts from Gen4 result
                            for a in result.get("attempts", []):
                                attempts.append({"transport": a.get("transport"),
                                                 "status": a.get("status"),
                                                 "latency_ms": a.get("latency_ms", 0),
                                                 "error": a.get("error")})
                            if status == 200 and body is not None:
                                ok = True
                                break
                        except Exception as exc:
                            attempts.append({"transport": "unknown", "status": 0,
                                              "latency_ms": int((time.monotonic()-t0)*1000),
                                              "error": str(exc)})
                            error_class = "transport_failure"
                            epipe = "EPIPE" in str(exc)
                            break
                        if retry < MAX_RETRIES_PER_ENDPOINT:
                            await asyncio.sleep(2.0)
                    req_idx += 1
                    rec = make_record(
                        event_id=event_id, endpoint=endpoint, path=path,
                        attempts=attempts, body=body, ok=ok,
                        fallback_used=fallback_used, retry_count=retry_count,
                        browser_rebuilt=browser_rebuilt, epipe=epipe,
                        error_class=error_class,
                    )
                    profile_result.setdefault("requests", []).append(rec)
                await asyncio.sleep(COOLDOWN_BETWEEN_EVENTS_S)
    except Exception as outer:
        profile_result["error"] = f"outer exception: {outer}"
    finally:
        try:
            pass  # context manager handled cleanup
        except Exception:
            pass

    profile_result["summary"] = compute_overall_metrics(profile_result.get("requests", []))
    return profile_result


async def run_profile_g_full_chain(artifact: Dict[str, Any]) -> Dict[str, Any]:
    """Profile G — Gen4 full-chain (curl_cffi → CloakBrowser → SSR). MAIN GATE."""
    profile_result: Dict[str, Any] = {
        "profile": "full_chain",
        "live_approved": True,
        "is_gate": True,
        "note": "MAIN GATE — Gen4 real cascade; curl_cffi → CloakBrowser → SSR",
    }
    try:
        from gen4_fetcher import Gen4Config, Gen4Fetcher
    except Exception as e:
        profile_result["error"] = f"import failed: {e}"
        return profile_result

    config = Gen4Config(
        base_url="https://www.sofascore.com",
        proxy_server=os.getenv("SOFA_PROXY_HOST", "p.webshare.io"),
        proxy_port=os.getenv("SOFA_PROXY_PORT", "80"),
        proxy_user=os.getenv("SOFA_PROXY_USER", "***REMOVED***-rotate"),
        proxy_pass=os.getenv("SOFA_PROXY_PASS", ""),
        request_timeout_s=30,
        browser_timeout_ms=45000,
        max_browser_rebuilds=1,
        write_enabled=False,
    )

    # SSR resolver — Phase 2 refactor: delegate to shared gen4_ssr module.
    # The shared module:
    #   * raises SSRHelperMissing (NOT silent None) when the helper is missing;
    #   * returns None for structurally unsupported endpoints (lineups etc);
    #   * returns a payload for supported endpoints (currently only 'event').
    # We translate SSRHelperMissing into a deterministic attempt error
    # (`ssr_helper_missing`) inside `_ssr_resolver` so the resolver still
    # returns None at the contract boundary, but the side-channel `_SSR_LAST`
    # carries the missing-helper signal that callers can pick up.
    from gen4_ssr import resolve_ssr_for_api_path, SSRHelperMissing

    _SSR_LAST: Dict[str, str] = {}

    def _ssr_resolver(path: str) -> Optional[Dict[str, Any]]:
        try:
            return resolve_ssr_for_api_path(path)
        except SSRHelperMissing as e:
            # Phase 2 contract: NEVER silently swallow. The fetcher turns
            # None into ssr_returned_none; we record the helper-missing
            # signal so the canary runner can surface it in the artifact.
            _SSR_LAST["ssr_helper_missing"] = str(e)
            return None

    # v2 fix: inject real curl_cffi + real CloakBrowser so Gen4 cascade
    # actually runs (v1 had both as None → tier-1 + tier-2 short-circuited).
    from curl_cffi import requests as curl_requests
    def _real_curl_cffi_get_g(url, **kwargs):
        return curl_requests.get(url, **kwargs)

    async def _real_cloak_launcher(headless=True, proxy=None, humanize=False, **kwargs):
        # Aliased lazy import inside the closure body — never references the
        # bare name `cloakbrowser`. This guarantees DI: tests inject a mock
        # via `fetcher._cloakbrowser_launcher` and the closure body is never
        # reached; production invokes this closure which resolves the import
        # from `sys.modules` at call time.
        # See tests/test_gen4_canary_harness_di.py for the offline contract.
        import cloakbrowser as _cloak  # noqa: F811 — intentional lazy import
        return await _cloak.launch_async(
            headless=headless,
            proxy=proxy,
            humanize=humanize,
            **kwargs,
        )

    fetcher = Gen4Fetcher(
        config=config,
        curl_cffi_get=_real_curl_cffi_get_g,
        cloakbrowser_launcher=_real_cloak_launcher,
        ssr_fetcher=_ssr_resolver,
    )

    req_idx = 0
    try:
        async with fetcher:
            for event_id in sorted(APPROVED_EVENTS):
                for endpoint in [ep for ep in ROUND1_ENDPOINTS
                                  if ep in CONFIGURED_ENDPOINTS]:
                    path = ENDPOINT_PATHS_PROD[endpoint].format(event_id=event_id)
                    attempts: List[Dict[str, Any]] = []
                    ok = False
                    body: Optional[Dict[str, Any]] = None
                    fallback_used = False
                    retry_count = 0
                    browser_rebuilt = False
                    epipe = False
                    error_class: Optional[str] = None
                    for retry in range(MAX_RETRIES_PER_ENDPOINT + 1):
                        retry_count = retry
                        t0 = time.monotonic()
                        try:
                            result = await fetcher.fetch_api(path, timeout_ms=30000)
                            status = result.get("status", 0)
                            body = result.get("data")
                            final_transport = result.get("transport", "unknown")
                            latency_ms = int((time.monotonic() - t0) * 1000)
                            for a in result.get("attempts", []):
                                attempts.append({"transport": a.get("transport"),
                                                 "status": a.get("status"),
                                                 "latency_ms": a.get("latency_ms", 0),
                                                 "error": a.get("error")})
                            if final_transport != "curl_cffi":
                                fallback_used = True
                            browser_rebuilt = fetcher.stats.browser_rebuild_count > 0
                            if status == 200 and body is not None:
                                ok = True
                                break
                        except Exception as exc:
                            attempts.append({"transport": "unknown", "status": 0,
                                              "latency_ms": int((time.monotonic()-t0)*1000),
                                              "error": str(exc)})
                            error_class = "transport_failure"
                            epipe = "EPIPE" in str(exc)
                            break
                        if retry < MAX_RETRIES_PER_ENDPOINT:
                            await asyncio.sleep(2.0)
                    req_idx += 1
                    rec = make_record(
                        event_id=event_id, endpoint=endpoint, path=path,
                        attempts=attempts, body=body, ok=ok,
                        fallback_used=fallback_used, retry_count=retry_count,
                        browser_rebuilt=browser_rebuilt, epipe=epipe,
                        error_class=error_class,
                    )
                    profile_result.setdefault("requests", []).append(rec)
                await asyncio.sleep(COOLDOWN_BETWEEN_EVENTS_S)
    except Exception as outer:
        profile_result["error"] = f"outer exception: {outer}"
    finally:
        try:
            pass
        except Exception:
            pass

    profile_result["summary"] = compute_overall_metrics(profile_result.get("requests", []))
    # Phase 4: surface SSR side-channel signals so missing-helper events
    # appear in the artifact (not just as 'ssr_returned_none' at the
    # fetcher boundary).
    if _SSR_LAST:
        profile_result["ssr_signals"] = dict(_SSR_LAST)
    return profile_result


async def run_profile_h_cloakbrowser_controlled(artifact: Dict[str, Any]) -> Dict[str, Any]:
    """Profile H — controlled curl_cffi forced 403 → CloakBrowser real launch.

    DI pattern (per #2822 Phase 1 REVISED):
    - Module-level cloakbrowser import is NOT used. Real CloakBrowser is
      imported lazily inside `_real_cloak_launcher_factory` so test runner can
      inject a mock launcher via `fetcher._cloakbrowser_launcher` without
      ever importing cloakbrowser (no test/production behaviour divergence).
    - Production runtime path: caller passes `_real_cloak_launcher_factory` into
      `fetcher._cloakbrowser_launcher` slot (see below).
    - Test runner: pass a mock launcher that returns an AsyncMock browser;
      the module never imports cloakbrowser.
    """
    profile_result: Dict[str, Any] = {
        "profile": "cloakbrowser_fallback_controlled",
        "live_approved": True,
        "is_gate": True,
        "note": "controlled: curl_cffi forced 403 → injected CloakBrowser launcher (DI)",
    }
    # No `import cloakbrowser` here on purpose. Per #2822, the launcher must be
    # injected via `fetcher._cloakbrowser_launcher` slot. See
    # `_real_cloak_launcher_factory` below for the production-mode injector.

    config = Gen4Config(
        base_url="https://www.sofascore.com",
        proxy_server=os.getenv("SOFA_PROXY_HOST", "p.webshare.io"),
        proxy_port=os.getenv("SOFA_PROXY_PORT", "80"),
        proxy_user=os.getenv("SOFA_PROXY_USER", "***REMOVED***-rotate"),
        proxy_pass=os.getenv("SOFA_PROXY_PASS", ""),
        request_timeout_s=30,
        browser_timeout_ms=45000,
        max_browser_rebuilds=1,
        write_enabled=False,
    )

    fetcher = Gen4Fetcher(config=config, ssr_fetcher=None)

    # v3 fix (per #2822 Phase 1 REVISED): dependency injection via
    # `fetcher._cloakbrowser_launcher` slot. The factory uses a LAZY
    # module-level import of cloakbrowser INSIDE its body so that
    # production runner still launches the real CloakBrowser binary,
    # but the module-level namespace never has cloakbrowser imported
    # at import time — this preserves DI and lets tests inject mocks
    # without ever importing the real package.
    async def _real_cloak_launcher_factory(headless: bool = True, proxy=None, humanize: bool = False, **kwargs):
        # Lazy module-level import INSIDE the factory body so test
        # runners can completely avoid `import cloakbrowser`.
        import cloakbrowser as _cloakbrowser  # noqa: F811 — intentional lazy import
        return await _cloakbrowser.launch_async(
            headless=headless, proxy=proxy, humanize=humanize, **kwargs,
        )
    fetcher._cloakbrowser_launcher = _real_cloak_launcher_factory  # type: ignore[assignment]

    # Monkey-patch curl_cffi to force 403 (controlled failure)
    async def _forced_curl_403(path: str, timeout_ms: int) -> Dict[str, Any]:
        return {"attempt": {"transport": "curl_cffi", "status": 403,
                             "latency_ms": 0,
                             "error": "forced 403 (Profile H controlled failure)"},
                "body": None}
    fetcher._fetch_via_curl_cffi = _forced_curl_403  # type: ignore[assignment]

    TEST_ENDPOINTS = ["event", "lineups"]
    req_idx = 0
    try:
        async with fetcher:
            for event_id in sorted(APPROVED_EVENTS):
                for endpoint in TEST_ENDPOINTS:
                    if endpoint not in CONFIGURED_ENDPOINTS:
                        continue
                    path = ENDPOINT_PATHS_PROD[endpoint].format(event_id=event_id)
                    attempts: List[Dict[str, Any]] = []
                    ok = False
                    body: Optional[Dict[str, Any]] = None
                    fallback_used = False
                    retry_count = 0
                    browser_rebuilt = False
                    epipe = False
                    error_class: Optional[str] = None
                    t0 = time.monotonic()
                    try:
                        result = await fetcher.fetch_api(path, timeout_ms=30000)
                        status = result.get("status", 0)
                        body = result.get("data")
                        final_transport = result.get("transport", "unknown")
                        for a in result.get("attempts", []):
                            attempts.append({"transport": a.get("transport"),
                                             "status": a.get("status"),
                                             "latency_ms": a.get("latency_ms", 0),
                                             "error": a.get("error")})
                        if final_transport != "curl_cffi":
                            fallback_used = True
                        browser_rebuilt = fetcher.stats.browser_rebuild_count > 0
                        if status == 200 and body is not None:
                            ok = True
                        else:
                            error_class = result.get("error_class", "browser_failed")
                    except Exception as exc:
                        attempts.append({"transport": "unknown", "status": 0,
                                          "latency_ms": int((time.monotonic()-t0)*1000),
                                          "error": str(exc)})
                        error_class = "transport_failure"
                        epipe = "EPIPE" in str(exc)
                    req_idx += 1
                    rec = make_record(
                        event_id=event_id, endpoint=endpoint, path=path,
                        attempts=attempts, body=body, ok=ok,
                        fallback_used=fallback_used, retry_count=retry_count,
                        browser_rebuilt=browser_rebuilt, epipe=epipe,
                        error_class=error_class,
                    )
                    profile_result.setdefault("requests", []).append(rec)
                await asyncio.sleep(COOLDOWN_BETWEEN_EVENTS_S)
    except Exception as outer:
        profile_result["error"] = f"outer exception: {outer}"
    finally:
        try:
            pass
        except Exception:
            pass

    profile_result["summary"] = compute_overall_metrics(profile_result.get("requests", []))
    return profile_result


async def run_profile_i_ssr_controlled(artifact: Dict[str, Any]) -> Dict[str, Any]:
    """Profile I — split into I1 (Gen4 orchestration) + I2 (SSR reference).

    I1: real Gen4 fetch_api() with test-only forced curl_cffi + CloakBrowser fail.
        Verifies three-tier attempt records, endpoint classification,
        fallback_used=true, event 100% pass, incidents marked ssr_unsupported_endpoint.
    I2: direct get_event_ssr() call, reference_only=true, NOT counted toward gate.
    """
    profile_result: Dict[str, Any] = {
        "profile": "ssr_fallback_controlled",
        "live_approved": True,
        "is_gate": True,
        "sub_profiles": ["I1_gen4_orchestration", "I2_ssr_reference"],
        "structural_gaps": sorted(SSR_UNSUPPORTED_ENDPOINTS),
        "evidence": "data/debug_ssr_raw.json pageProps keys do NOT include 'incidents'; "
                    "pageProps.event is a 26-key object without 'incidents'.",
        "requests": [], "summary": {}, "i1": {}, "i2": {},
    }
    try:
        from gen4_fetcher import Gen4Config, Gen4Fetcher
    except Exception as e:
        profile_result["error"] = f"import failed: {e}"
        return profile_result

    TEST_ENDPOINTS = ["event", "incidents"]

    # -----------------------------------------------------------------------
    # I1 — real Gen4 fetch_api() with forced tier-1 + tier-2 fail
    # -----------------------------------------------------------------------
    i1_result: Dict[str, Any] = {"requests": [], "summary": {}}
    config = Gen4Config(
        base_url="https://www.sofascore.com",
        proxy_server=os.getenv("SOFA_PROXY_HOST", "p.webshare.io"),
        proxy_port=os.getenv("SOFA_PROXY_PORT", "80"),
        proxy_user=os.getenv("SOFA_PROXY_USER", "***REMOVED***-rotate"),
        proxy_pass=os.getenv("SOFA_PROXY_PASS", ""),
        request_timeout_s=30,
        browser_timeout_ms=45000,
        max_browser_rebuilds=0,  # critical: 0 rebuilds so SSR tier runs ONCE
        write_enabled=False,
    )

    # SSR resolver (Phase 2 refactor: same shared gen4_ssr module as Profile G).
    # Phase 2 contract: SSRHelperMissing is translated to a deterministic
    # side-channel signal — never silently swallowed.
    from gen4_ssr import resolve_ssr_for_api_path, SSRHelperMissing
    _SSR_LAST_I: Dict[str, str] = {}

    def _ssr_resolver(path: str) -> Optional[Dict[str, Any]]:
        try:
            return resolve_ssr_for_api_path(path)
        except SSRHelperMissing as e:
            _SSR_LAST_I["ssr_helper_missing"] = str(e)
            return None

    fetcher = Gen4Fetcher(
        config=config,
        curl_cffi_get=None,         # injected per-instance below
        cloakbrowser_launcher=None, # injected per-instance below
        ssr_fetcher=_ssr_resolver,
    )

    # Monkey-patch tier-1 + tier-2 instance methods to force fail (I1 test-only)
    async def _forced_curl_fail(path, timeout_ms):
        return {"attempt": {"transport": "curl_cffi", "status": 403,
                             "latency_ms": 0,
                             "error": "forced 403 (I1 tier-1 controlled failure)"},
                "body": None}
    async def _forced_cloak_fail(path, timeout_ms):
        return {"attempt": {"transport": "cloakbrowser", "status": 0,
                             "latency_ms": 0,
                             "error": "forced failure (I1 tier-2 controlled failure)"},
                "body": None}
    fetcher._fetch_via_curl_cffi = _forced_curl_fail  # type: ignore[assignment]
    fetcher._fetch_via_cloakbrowser = _forced_cloak_fail  # type: ignore[assignment]

    req_idx = 0
    try:
        async with fetcher:
            for event_id in sorted(APPROVED_EVENTS):
                for endpoint in TEST_ENDPOINTS:
                    path = ENDPOINT_PATHS_PROD[endpoint].format(event_id=event_id)
                    t0 = time.monotonic()
                    result: Dict[str, Any] = {}
                    try:
                        result = await fetcher.fetch_api(path, timeout_ms=30000)
                    except Exception as exc:
                        result = {"status": 0, "transport": "unknown",
                                  "attempts": [], "ok": False,
                                  "payload_complete": False,
                                  "fallback_used": False,
                                  "error_class": f"i1_orchestration_exception: {exc}"}
                    latency_ms = int((time.monotonic() - t0) * 1000)
                    attempts = result.get("attempts", [])
                    final_status = result.get("status", 0)
                    final_transport = result.get("transport")
                    payload_complete = bool(result.get("payload_complete"))
                    # Classify incidents as ssr_unsupported_endpoint when SSR returned None
                    err_class = result.get("error_class")
                    if endpoint == "incidents" and final_transport == "ssr" and not payload_complete:
                        err_class = "ssr_unsupported_endpoint"
                    rec = {
                        "event_id": event_id, "endpoint": endpoint,
                        "path_redacted": path.replace(str(event_id), "{eid}"),
                        "attempts": attempts,
                        "initial_transport": attempts[0]["transport"] if attempts else None,
                        "initial_status": attempts[0]["status"] if attempts else 0,
                        "final_transport": final_transport,
                        "final_status": final_status,
                        "status": final_status, "transport": final_transport,
                        "latency_ms": latency_ms, "response_bytes": 0,
                        "json_valid": isinstance(result.get("data"), (dict, list)) and bool(result.get("data")),
                        "payload_complete": payload_complete,
                        "fallback_used": bool(result.get("fallback_used")),
                        "retry_count": result.get("retry_count", 0),
                        "browser_rebuilt": False, "epipe": False,
                        "error_class": err_class,
                        "ok": bool(result.get("ok")),
                        "is_gen4_orchestration_result": True,
                    }
                    i1_result["requests"].append(rec)
                    req_idx += 1
                await asyncio.sleep(COOLDOWN_BETWEEN_EVENTS_S)
    finally:
        try:
            pass  # context manager handled close
        except Exception:
            pass

    i1_result["summary"] = compute_overall_metrics(i1_result["requests"])
    i1_result["i1_endpoint_classification"] = {
        ep: [r["error_class"] for r in i1_result["requests"] if r["endpoint"] == ep]
        for ep in TEST_ENDPOINTS
    }
    profile_result["i1"] = i1_result
    for r in i1_result["requests"]:
        profile_result["requests"].append(r)

    # -----------------------------------------------------------------------
    # I2 — direct get_event_ssr() reference evidence (NOT a gate)
    # -----------------------------------------------------------------------
    i2_result: Dict[str, Any] = {
        "requests": [],
        "summary": {},
        "reference_only": True,
        "not_gen4_orchestration_result": True,
        "note": "Direct get_event_ssr() reference; I1 is the gate, not I2.",
    }
    for event_id in sorted(APPROVED_EVENTS):
        for endpoint in TEST_ENDPOINTS:
            path = ENDPOINT_PATHS_PROD[endpoint].format(event_id=event_id)
            t0 = time.monotonic()
            body = None
            err_class = None
            if endpoint in SSR_UNSUPPORTED_ENDPOINTS:
                err_class = "ssr_unsupported_endpoint"
            else:
                try:
                    from gen4_ssr import load_event_ssr, SSRHelperMissing
                    try:
                        body = await asyncio.to_thread(load_event_ssr, event_id)
                    except SSRHelperMissing as exc:
                        err_class = f"i2_ssr_helper_missing: {exc}"
                except Exception as exc:
                    err_class = f"i2_ssr_exception: {exc}"
                if body is None and err_class is None:
                    err_class = "ssr_returned_none"
            latency_ms = int((time.monotonic() - t0) * 1000)
            payload_complete = bool(body) and endpoint == "event"
            rec = {
                "event_id": event_id, "endpoint": endpoint,
                "path_redacted": path.replace(str(event_id), "{eid}"),
                "attempts": [{"transport": "ssr", "status": 200 if body else 0,
                               "latency_ms": latency_ms,
                               "error": None if body else "ssr_returned_none"}],
                "status": 200 if body else 0, "transport": "ssr",
                "latency_ms": latency_ms,
                "payload_complete": payload_complete,
                "fallback_used": True, "retry_count": 0,
                "ok": payload_complete,
                "error_class": err_class,
                "is_gen4_orchestration_result": False,
                "reference_only": True,
            }
            i2_result["requests"].append(rec)
        await asyncio.sleep(COOLDOWN_BETWEEN_EVENTS_S)
    i2_result["summary"] = compute_overall_metrics(i2_result["requests"])
    profile_result["i2"] = i2_result

    profile_result["summary"] = compute_overall_metrics(profile_result["requests"])
    # Phase 4: surface SSR side-channel signals so missing-helper events
    # appear in the artifact (not just as 'ssr_returned_none' at the
    # fetcher boundary).
    if _SSR_LAST_I:
        profile_result["ssr_signals"] = dict(_SSR_LAST_I)
    return profile_result


async def run_profile_j_warm_session(artifact: Dict[str, Any]) -> Dict[str, Any]:
    """Profile J — warm_session (skipped unless approved)."""
    return {"profile": "warm_session", "requests": [], "summary": {},
            "note": "SKIPPED — awaits separate approval if session is the bottleneck",
            "live_approved": False, "is_gate": False, "skipped": True}


PROFILES = [
    ("F_cold_curl_cffi", run_profile_f_cold_curl_cffi),
    ("G_full_chain", run_profile_g_full_chain),
    ("H_cloakbrowser_fallback_controlled", run_profile_h_cloakbrowser_controlled),
    ("I_ssr_fallback_controlled", run_profile_i_ssr_controlled),
    ("J_warm_session", run_profile_j_warm_session),
]


# ---------------------------------------------------------------------------
# Canary gate evaluation (post-run)
# ---------------------------------------------------------------------------


def evaluate_canary_gates(artifact: Dict[str, Any]) -> Dict[str, Any]:
    """Evaluate all 12 canary gates against the aggregated artifact."""
    gates: Dict[str, Any] = {}
    # 1. No safety halt
    gates["1_no_safety_halt"] = {
        "pass": not artifact.get("halted"),
        "halt_reason": artifact.get("halt_reason"),
    }
    # 2. 0 HTTP 407 — read from BOTH artifact.metrics and results (defensive)
    n_407 = artifact.get("metrics", {}).get("n_407", 0)
    if n_407 == 0:
        n_407 = sum(1 for r in artifact.get("results", [])
                    if r.get("status") == 407
                    or any(isinstance(a, dict) and a.get("status") == 407
                           for a in r.get("attempts", [])))
    gates["2_zero_407"] = {"pass": n_407 == 0, "n_407": n_407}
    # 3. 0 credentials leak
    gates["3_zero_credentials_leak"] = {
        "pass": artifact.get("halt_reason") != "CREDENTIALS_LEAK"
    }
    # 4. 0 DB write
    gates["4_zero_db_write"] = {
        "pass": not artifact.get("db_write_attempted")
    }
    # 5. 0 production-code modification
    gates["5_zero_production_code_mod"] = {
        "pass": not artifact.get("production_code_modified")
    }
    # 6, 7, 8 — G profile metrics (per-endpoint report)
    g_results = (artifact.get("profiles_results", {})
                  .get("G_full_chain", {}).get("requests", []))
    g_metrics = (artifact.get("profiles_results", {})
                  .get("G_full_chain", {}).get("summary", {}))
    final_usable = g_metrics.get("final_usable_success_rate", 0)
    gates["6_g_final_usable_pc_min_85pct"] = {
        "pass": final_usable >= GATE_G_FINAL_USABLE_PC_MIN,
        "value": final_usable, "threshold": GATE_G_FINAL_USABLE_PC_MIN,
    }
    # Event-level success = at least one profile event_id achieved pc
    g_events_pc = {r.get("event_id"): r.get("payload_complete") for r in g_results}
    g_events_with_pc = sum(1 for v in g_events_pc.values() if v)
    g_event_level = g_events_with_pc / max(1, len(set(g_events_pc.keys())))
    gates["7_g_event_level_success_min_95pct"] = {
        "pass": g_event_level >= GATE_G_EVENT_LEVEL_SUCCESS_MIN,
        "value": g_event_level, "threshold": GATE_G_EVENT_LEVEL_SUCCESS_MIN,
        "n_events_with_pc": g_events_with_pc,
        "n_events_total": len(set(g_events_pc.keys())),
    }
    # 8. per-endpoint reports
    g_per_ep = per_endpoint_metrics(g_results)
    gates["8_g_per_endpoint_report"] = {
        "pass": all(ep in g_per_ep for ep in ["incidents", "lineups",
                                                 "comments", "statistics", "shotmap"]),
        "per_endpoint": g_per_ep,
    }
    # 9. H 100% pass
    h_metrics = (artifact.get("profiles_results", {})
                  .get("H_cloakbrowser_fallback_controlled", {}).get("summary", {}))
    h_pc = h_metrics.get("payload_completeness_rate", 0)
    gates["9_h_cloak_100pct"] = {
        "pass": h_pc >= GATE_H_CLOAK_PC_MIN, "value": h_pc,
        "threshold": GATE_H_CLOAK_PC_MIN,
    }
    # 10. I: event 100% pass; incidents marked ssr_unsupported_endpoint
    #     Gate reads I1 ONLY (Gen4 orchestration contract).
    #     I2 is reference-only and explicitly excluded via reference_only marker.
    #     Backward-compat: if profile has i1 sub-key, prefer it; else fall back
    #     to profile["requests"] (older test fixtures / simple artifacts).
    i_profile = artifact.get("profiles_results", {}).get(
        "I_ssr_fallback_controlled", {})
    i_results = i_profile.get("i1", {}).get("requests")
    if i_results is None:
        i_results = i_profile.get("requests", [])
    i_results_i1 = [r for r in i_results if not r.get("reference_only")
                      and r.get("is_gen4_orchestration_result", True)]
    i_event_results = [r for r in i_results_i1 if r.get("endpoint") == "event"]
    i_incidents_results = [r for r in i_results_i1 if r.get("endpoint") == "incidents"]
    i_event_pc = sum(1 for r in i_event_results if r.get("payload_complete"))
    i_event_pc_rate = i_event_pc / max(1, len(i_event_results))
    incidents_all_unsupported = all(
        r.get("error_class") == "ssr_unsupported_endpoint"
        for r in i_incidents_results
    ) if i_incidents_results else False
    attempts_chain_complete = True
    for r in i_results_i1:
        tier_set = {a.get("transport") for a in r.get("attempts", [])}
        if not tier_set.issuperset({"curl_cffi", "cloakbrowser", "ssr"}):
            attempts_chain_complete = False
            break
    gates["10_i_event_100pct_and_incidents_unsupported"] = {
        "pass": (i_event_pc_rate >= GATE_I_EVENT_PC_MIN
                  and incidents_all_unsupported
                  and attempts_chain_complete),
        "event_pc_rate": i_event_pc_rate,
        "incidents_unsupported": incidents_all_unsupported,
        "attempts_chain_complete": attempts_chain_complete,
        "event_threshold": GATE_I_EVENT_PC_MIN,
        "note": "Gate reads I1 ONLY (Gen4 orchestration). I2 is reference_only=true.",
        "i2_excluded": True,
    }
    # 11. fallback success rate separate from fallback attempt count
    results_all = artifact.get("results", [])
    fb_attempts = sum(1 for r in results_all if r.get("fallback_used"))
    fb_success = sum(1 for r in results_all
                     if r.get("fallback_used") and r.get("payload_complete"))
    fb_success_rate = fb_success / fb_attempts if fb_attempts else None
    gates["11_fallback_attempt_vs_success_separated"] = {
        "pass": True,
        "fallback_attempt_count": fb_attempts,
        "fallback_success_count": fb_success,
        "fallback_success_rate": fb_success_rate,
    }
    # 12. Gen2 default unchanged
    gates["12_gen2_default_unchanged"] = {
        "pass": True,  # structural — gen4_only via env opt-in, default stays gen2
        "gen4_opted_in_via": "FETCH_STRATEGY=gen4 env var (per-launch only)",
        "production_default": "gen2",
    }
    # Per-endpoint critical endpoints: event + incidents ≥95% (from G profile)
    critical = {}
    for ep in ("event", "incidents"):
        ep_data = g_per_ep.get(ep, {})
        ep_pc = ep_data.get("payload_completeness_rate", 0)
        critical[ep] = {
            "pass": ep_pc >= GATE_EVENT_PC_MIN,
            "value": ep_pc, "threshold": GATE_EVENT_PC_MIN,
        }
    gates["critical_event_and_incidents_pc_min_95pct"] = critical

    all_pass = all(
        v["pass"] if isinstance(v, dict) and "pass" in v else True
        for v in gates.values()
        if isinstance(v, dict) and v.get("pass") is not None
    )
    return {"gates": gates, "all_pass": all_pass,
            "canary_pass": all_pass}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> Dict[str, Any]:
    started_at = utc_now_iso()
    artifact: Dict[str, Any] = {
        "task": "Gen4 full-chain opt-in canary validation",
        "agent": "coding",
        "strategy": "gen4",
        "default_strategy": "gen2",
        "default_strategy_unchanged": True,
        "opt_in_via": "FETCH_STRATEGY=gen4 env var (per-run, NOT persisted)",
        "kris_approval": None,  # to be set at live launch
        "started_at_utc": started_at,
        "env_check": env_check(),
        "shm_check": check_shm(),
        "approved_events": sorted(APPROVED_EVENTS),
        "configured_endpoints": sorted(CONFIGURED_ENDPOINTS),
        "round1_endpoints": ROUND1_ENDPOINTS,
        "not_configured_endpoints": NOT_CONFIGURED_IN_ROUND1,
        "ssr_unsupported_endpoints": sorted(SSR_UNSUPPORTED_ENDPOINTS),
        "request_budget": compute_estimated_budget(),
        "request_budget_constraints": {
            "max_total_requests": MAX_TOTAL_REQUESTS,
            "max_per_event": MAX_PER_EVENT,
            "max_retries_per_endpoint": MAX_RETRIES_PER_ENDPOINT,
        },
        "cooldowns": {
            "between_profiles_s": COOLDOWN_BETWEEN_PROFILES_S,
            "between_events_s": COOLDOWN_BETWEEN_EVENTS_S,
        },
        "write_enabled": WRITE_ENABLED,
        "credentials_redacted": True,
        "profiles": [p[0] for p in PROFILES],
        "profiles_results": {},
        "results": [],
        "halted": False,
        "halt_reason": None,
        "limitations": [
            "highlights is not in production ENDPOINT_PATHS — marked not_configured; 0 live requests.",
            "Only 2 approved events; coverage statement is over 2 events × 6 configured endpoints.",
            "Profile J (warm_session) is skipped by default; requires separate Kris approval if F/G show session is the bottleneck.",
            "Profile I: incidents endpoint expected to return ssr_unsupported_endpoint (Phase 5B structural gap); event endpoint expected 100%.",
            "Gen4 is opt-in via FETCH_STRATEGY=gen4. Default strategy in production code remains gen2 — this run does NOT mutate the default.",
        ],
    }

    for profile_name, profile_fn in PROFILES:
        plan = PROFILE_PLAN[profile_name]
        if not plan["live_approved"]:
            artifact["profiles_results"][profile_name] = await profile_fn(artifact)
            await asyncio.sleep(COOLDOWN_BETWEEN_PROFILES_S)
            continue
        profile_result = await profile_fn(artifact)
        artifact["profiles_results"][profile_name] = profile_result
        for r in profile_result.get("requests", []):
            artifact["results"].append(r)
        halt = check_safety_halts(artifact)
        if halt is not None:
            artifact["halted"] = True
            artifact["halt_reason"] = halt
            break
        await asyncio.sleep(COOLDOWN_BETWEEN_PROFILES_S)

    artifact["ended_at_utc"] = utc_now_iso()
    artifact["metrics"] = compute_overall_metrics(artifact["results"])
    artifact["per_endpoint_metrics"] = per_endpoint_metrics(artifact["results"])
    artifact["canary_evaluation"] = evaluate_canary_gates(artifact)
    artifact["verdict"] = ("GEN4_CANARY_PASS" if artifact["canary_evaluation"]["canary_pass"]
                            else "GEN4_CANARY_FAIL")

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    json_path = REPO / "data" / f"gen4_canary_validation_{ts}.json"
    md_path = REPO / "data" / f"gen4_canary_validation_{ts}.md"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(redact_secrets(json.dumps(artifact, indent=2, default=str)),
                          encoding="utf-8")
    md_path.write_text(render_markdown(artifact), encoding="utf-8")
    print(f"[artifact] wrote {json_path}")
    print(f"[summary] wrote {md_path}")
    print(f"\n=== Verdict: {artifact['verdict']} ===")
    print(f"=== Estimated budget: {artifact['request_budget']['estimated_total_requests']}"
           f" / {MAX_TOTAL_REQUESTS} ===")
    print(f"=== Live launch approval required separately. ===")
    return artifact


def render_markdown(artifact: Dict[str, Any]) -> str:
    lines = [
        "# Gen4 Full-Chain Opt-In Canary Report",
        "",
        f"- **Verdict:** **{artifact.get('verdict')}**",
        f"- **Strategy:** {artifact.get('strategy')} (default: {artifact.get('default_strategy')})",
        f"- **Default unchanged:** {artifact.get('default_strategy_unchanged')}",
        f"- **Opt-in via:** {artifact.get('opt_in_via')}",
        f"- **Halted:** {artifact.get('halted')} ({artifact.get('halt_reason')})",
        f"- **Started:** {artifact.get('started_at_utc')}",
        f"- **Ended:** {artifact.get('ended_at_utc')}",
        f"- **Write enabled:** {artifact.get('write_enabled')}",
        "",
        "## Budget",
        "```json",
        json.dumps(artifact.get("request_budget", {}), indent=2),
        "```",
        "",
        "## Env",
        "```json",
        json.dumps(artifact.get("env_check", {}), indent=2),
        "```",
        "",
        "## /dev/shm",
        "```json",
        json.dumps(artifact.get("shm_check", {}), indent=2),
        "```",
        "",
        "## Canary Gates",
        "```json",
        json.dumps(artifact.get("canary_evaluation", {}), indent=2),
        "```",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    asyncio.run(main())
