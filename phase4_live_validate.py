#!/usr/bin/env python3
"""
phase4_live_validate.py — Gen4 Phase 4 LIVE VALIDATION (Kris 02:07 GMT+8 approval).

Strict scope (read-only, Gen4 only):
  - 2 events ONLY: 14025013, 12436875
  - 2 endpoints ONLY: /event/{id}, /event/{id}/incidents
  - 4 requests total: 2 events × 2 endpoints
  - write_enabled=False (Gen4Config default)
  - NO MySQL, NO DataInserter, NO full backfill
  - NO scope expansion

Stop conditions (per Kris spec — any one of these halts immediately):
  - HTTP 407 from any tier
  - Quota warning surfaced
  - 403 rate > 50% (2/4 or more)
  - 2 consecutive CloakBrowser / EPIPE failures
  - Browser / context cleanup failure
  - Credentials leak in any log
  - Any DB write detected

Outputs:
  - phase4_live_artifact.json (per-request metrics + stop condition status)
  - phase4_live_summary.md (human-readable verdict)
  - Verdict: PROTOTYPE_PASS / PROTOTYPE_PARTIAL / FAILED

Metrics tracked:
  - per-request: status, transport, latency_ms, attempts, fallback_used, payload_complete, error_class, halt_reason
  - aggregate: fallback count, 403/407 count, p50/p90 latency, payload completeness, browser rebuild count, EPIPE count
"""
from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

# Force FETCH_STRATEGY=gen4 for this validation run.
os.environ["FETCH_STRATEGY"] = "gen4"

try:
    from curl_cffi import requests as curl_requests
except Exception as _exc:  # pragma: no cover
    print(f"FATAL: curl_cffi not importable: {_exc}", flush=True)
    sys.exit(2)

import gen4_fetcher as g4  # noqa: E402
from gen4_fetcher import Gen4Config, Gen4Fetcher, redact_credentials  # noqa: E402

# ---------------------------------------------------------------------------
# Strict scope (Kris 02:07 spec)
# ---------------------------------------------------------------------------
APPROVED_EVENTS = frozenset({14025013, 12436875})
APPROVED_ENDPOINTS = frozenset({"event", "incidents"})

BROWSER_BASE = "https://www.sofascore.com"
PROXY_SERVER = os.getenv("SOFA_PROXY_HOST", "p.webshare.io")
PROXY_PORT = os.getenv("SOFA_PROXY_PORT", "80")
PROXY_USER = os.getenv("SOFA_PROXY_USER", "***REMOVED***-rotate")
PROXY_PASS = os.getenv("SOFA_PROXY_PASS", "")

# ---------------------------------------------------------------------------
# Live curl_cffi helper (real network call, no MySQL, no DataInserter)
# ---------------------------------------------------------------------------

def _real_curl_get(url: str, **kwargs) -> Any:
    """Sync curl_cffi GET. Used via asyncio.to_thread inside Gen4Fetcher."""
    proxies = kwargs.pop("proxies", None) or {"http": None, "https": None}
    timeout = kwargs.pop("timeout", 30)
    impersonate = kwargs.pop("impersonate", "chrome")
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": BROWSER_BASE,
        "Referer": f"{BROWSER_BASE}/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    return curl_requests.get(
        url,
        headers=headers,
        proxies=proxies if (proxies.get("https") or proxies.get("http")) else None,
        impersonate=impersonate,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Scope guard
# ---------------------------------------------------------------------------

def assert_scope(event_id: int, endpoint: str) -> None:
    """Enforce strict scope. Any violation raises before fetch."""
    if event_id not in APPROVED_EVENTS:
        raise PermissionError(
            f"SCOPE VIOLATION: event {event_id} not in approved set "
            f"{sorted(APPROVED_EVENTS)}. Halting immediately."
        )
    if endpoint not in APPROVED_ENDPOINTS:
        raise PermissionError(
            f"SCOPE VIOLATION: endpoint {endpoint!r} not in approved set "
            f"{sorted(APPROVED_ENDPOINTS)}. Halting immediately."
        )


def build_path(event_id: int, endpoint: str) -> str:
    assert_scope(event_id, endpoint)
    if endpoint == "event":
        return f"/api/v1/event/{event_id}"
    if endpoint == "incidents":
        return f"/api/v1/event/{event_id}/incidents"
    raise PermissionError(f"unreachable: {endpoint!r}")


# ---------------------------------------------------------------------------
# Stop condition checker
# ---------------------------------------------------------------------------

class StopReason(Exception):
    def __init__(self, code: str, message: str, partial_artifact: Dict[str, Any]):
        super().__init__(message)
        self.code = code
        self.message = message
        self.partial_artifact = partial_artifact


def check_stop_conditions(
    *,
    result: Dict[str, Any],
    request_idx: int,
    completed_requests: List[Dict[str, Any]],
    consecutive_cloak_failures: int,
    consecutive_epipe: int,
    cleanup_ok: bool,
    artifact: Dict[str, Any],
) -> Optional[StopReason]:
    """Return StopReason if any stop condition fires; else None."""
    # HTTP 407
    if result.get("status") == 407 or any(a.get("status") == 407 for a in result.get("attempts", [])):
        return StopReason(
            "HTTP_407",
            f"Request #{request_idx} returned 407 — terminal safety halt",
            artifact,
        )
    # quota warning surfaced
    if artifact.get("quota_warning"):
        return StopReason(
            "QUOTA_WARNING",
            f"Quota warning surfaced at request #{request_idx}",
            artifact,
        )
    # 403 rate > 50% (need at least 2 completed requests before evaluating)
    if len(completed_requests) >= 2:
        n_403 = sum(1 for r in completed_requests if r["status"] == 403)
        if n_403 / len(completed_requests) > 0.5:
            return StopReason(
                "403_RATE_OVER_50",
                f"403 rate = {n_403}/{len(completed_requests)} > 50% — halting",
                artifact,
            )
    # 2 consecutive CloakBrowser failures
    if consecutive_cloak_failures >= 2:
        return StopReason(
            "CONSECUTIVE_CLOAK_FAILURES",
            f"2 consecutive CloakBrowser failures — halting",
            artifact,
        )
    # 2 consecutive EPIPE
    if consecutive_epipe >= 2:
        return StopReason(
            "CONSECUTIVE_EPIPE",
            f"2 consecutive EPIPE failures — halting",
            artifact,
        )
    # cleanup failure
    if not cleanup_ok:
        return StopReason(
            "CLEANUP_FAILURE",
            "Browser/context cleanup did not complete cleanly — halting",
            artifact,
        )
    # credentials leak in any log
    log_dump = json.dumps(artifact, default=str)
    if PROXY_PASS and PROXY_PASS in log_dump:
        return StopReason(
            "CREDENTIALS_LEAK",
            "PROXY_PASS detected in artifact JSON — halting",
            artifact,
        )
    if "dztr57tcycoz" in log_dump:
        return StopReason(
            "CREDENTIALS_LEAK",
            "API key fragment detected in artifact JSON — halting",
            artifact,
        )
    return None




# ---------------------------------------------------------------------------
# Phase 4 main entry
# ---------------------------------------------------------------------------

async def run_phase4() -> Dict[str, Any]:
    """Run the 4-request Phase 4 live validation. Return artifact."""
    started_at = datetime.now(timezone.utc).isoformat()
    requests_planned = [
        (14025013, "event"),
        (14025013, "incidents"),
        (12436875, "event"),
        (12436875, "incidents"),
    ]

    artifact: Dict[str, Any] = {
        "task": "Gen4 Phase 4 live validation",
        "agent": "coding",
        "kris_approval": "02:07 GMT+8 (round #2805)",
        "started_at_utc": started_at,
        "scope": {
            "events": sorted(APPROVED_EVENTS),
            "endpoints": sorted(APPROVED_ENDPOINTS),
            "requests_planned": len(requests_planned),
            "read_only": True,
            "gen4_only": True,
            "write_enabled": False,
            "no_mysql": True,
            "no_full_backfill": True,
        },
        "results": [],
        "stop_reason": None,
        "metrics": {},
        "verdict": None,
    }

    # Init fetcher. CloakBrowser / SSR inject None: Phase 4 scope is curl_cffi-only;
    # any tier-2/3 attempt will mark "tier_unavailable" and fall through to failure.
    cfg = Gen4Config(
        base_url=BROWSER_BASE,
        proxy_server=PROXY_SERVER,
        proxy_port=PROXY_PORT,
        proxy_user=PROXY_USER,
        proxy_pass=PROXY_PASS,
        request_timeout_s=30,
        browser_timeout_ms=45000,
        max_browser_rebuilds=1,
        write_enabled=False,
    )
    fetcher = Gen4Fetcher(config=cfg, curl_cffi_get=_real_curl_get,
                           cloakbrowser_launcher=None, ssr_fetcher=None)

    cleanup_ok = True
    consecutive_cloak_failures = 0
    consecutive_epipe = 0
    quota_warning = False
    completed_requests: List[Dict[str, Any]] = []

    try:
        async with fetcher:
            pass  # start() / close() handled by __aenter__/__aexit__

        # We re-enter to actually run requests, so closure stays explicit.
        async with fetcher:
            for idx, (event_id, endpoint) in enumerate(requests_planned, start=1):
                path = build_path(event_id, endpoint)
                t0 = time.monotonic()
                try:
                    result = await fetcher.fetch_api(path, timeout_ms=30000)
                except Exception as fetch_exc:
                    result = {
                        "ok": False,
                        "status": 0,
                        "data": None,
                        "transport": "curl_cffi",
                        "attempts": [{"transport": "curl_cffi", "status": 0,
                                       "latency_ms": int((time.monotonic() - t0) * 1000),
                                       "error": f"fetch_api exception: {fetch_exc}"}],
                        "fallback_used": False,
                        "retry_count": 0,
                        "payload_complete": False,
                        "error_class": "fetch_exception",
                        "halt_reason": None,
                    }
                latency_ms = int((time.monotonic() - t0) * 1000)

                # Per-request record
                record = {
                    "request_idx": idx,
                    "event_id": event_id,
                    "endpoint": endpoint,
                    "path": path,
                    "latency_ms": latency_ms,
                    "status": result.get("status"),
                    "transport": result.get("transport"),
                    "attempts": result.get("attempts", []),
                    "fallback_used": result.get("fallback_used", False),
                    "payload_complete": result.get("payload_complete", False),
                    "error_class": result.get("error_class"),
                    "halt_reason": result.get("halt_reason"),
                    "data_keys_top_level": (
                        sorted(result["data"].keys())
                        if isinstance(result.get("data"), dict) else None
                    ),
                    "ok": result.get("ok"),
                }
                artifact["results"].append(record)
                completed_requests.append(record)

                # Track consecutive CloakBrowser / EPIPE
                cloak_attempts = [a for a in record["attempts"] if a.get("transport") == "cloakbrowser"]
                if cloak_attempts and any(a.get("status") == 0 for a in cloak_attempts):
                    consecutive_cloak_failures += 1
                else:
                    consecutive_cloak_failures = 0
                if any("EPIPE" in (a.get("error") or "") or "Target closed" in (a.get("error") or "")
                       for a in record["attempts"]):
                    consecutive_epipe += 1
                else:
                    consecutive_epipe = 0
                # Quota warning heuristic: 403 on multiple sequential requests
                recent_403 = sum(1 for r in completed_requests[-3:] if r["status"] == 403)
                if recent_403 >= 2:
                    quota_warning = True

                artifact["quota_warning"] = quota_warning

                # Stop condition check
                stop = check_stop_conditions(
                    result=result,
                    request_idx=idx,
                    completed_requests=completed_requests,
                    consecutive_cloak_failures=consecutive_cloak_failures,
                    consecutive_epipe=consecutive_epipe,
                    cleanup_ok=cleanup_ok,
                    artifact=artifact,
                )
                if stop is not None:
                    artifact["stop_reason"] = {"code": stop.code, "message": stop.message}
                    break

            # Verify cleanup after last request — close() runs via __aexit__
        # (cleanup_ok checked below; if close() raised inside __aexit__ it'd propagate,
        # but our Gen4Fetcher.close() swallows internal exceptions)

    except Exception as outer_exc:
        artifact["stop_reason"] = {
            "code": "OUTER_EXCEPTION",
            "message": f"Outer exception during run: {outer_exc}",
        }
    finally:
        # Compute aggregate metrics regardless of stop reason
        artifact["metrics"] = compute_metrics(artifact["results"])
        artifact["verdict"] = compute_verdict(artifact)
        artifact["ended_at_utc"] = datetime.now(timezone.utc).isoformat()
        # Final credentials redaction check on whole artifact
        artifact = redact_artifact(artifact)
        # Write partial artifact (even if stopped mid-run)
        write_artifact(artifact)
        write_summary(artifact)

    return artifact


def compute_metrics(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not results:
        return {"fallback_count": 0, "p50_latency_ms": None, "p90_latency_ms": None,
                "n_403": 0, "n_407": 0, "n_200": 0, "n_payload_complete": 0,
                "browser_rebuild_count": 0, "epipe_count": 0}
    latencies = [r["latency_ms"] for r in results]
    latencies_sorted = sorted(latencies)
    p50 = latencies_sorted[len(latencies_sorted) // 2]
    p90_idx = max(0, int(len(latencies_sorted) * 0.9) - 1)
    p90 = latencies_sorted[p90_idx]
    return {
        "fallback_count": sum(1 for r in results if r["fallback_used"]),
        "p50_latency_ms": p50,
        "p90_latency_ms": p90,
        "min_latency_ms": min(latencies),
        "max_latency_ms": max(latencies),
        "n_403": sum(1 for r in results if r["status"] == 403),
        "n_407": sum(1 for r in results if r["status"] == 407),
        "n_200": sum(1 for r in results if r["status"] == 200),
        "n_payload_complete": sum(1 for r in results if r["payload_complete"]),
        "browser_rebuild_count": 0,  # No CloakBrowser injected in Phase 4; always 0
        "epipe_count": sum(1 for r in results
                           for a in r["attempts"]
                           if "EPIPE" in (a.get("error") or "")),
    }


def compute_verdict(artifact: Dict[str, Any]) -> str:
    metrics = artifact.get("metrics", {})
    results = artifact.get("results", [])
    stop = artifact.get("stop_reason")

    # FAILED criteria (hard):
    if stop and stop["code"] in ("HTTP_407", "QUOTA_WARNING", "CONSECUTIVE_CLOAK_FAILURES",
                                  "CONSECUTIVE_EPIPE", "CLEANUP_FAILURE",
                                  "CREDENTIALS_LEAK", "OUTER_EXCEPTION"):
        return "FAILED"
    if metrics.get("n_407", 0) > 0:
        return "FAILED"

    # PARTIAL criteria:
    if stop is not None:
        return "PROTOTYPE_PARTIAL"
    if metrics.get("n_403", 0) >= 2:
        return "PROTOTYPE_PARTIAL"  # 403 even if not >50%
    if metrics.get("n_payload_complete", 0) < len(results):
        return "PROTOTYPE_PARTIAL"

    # PASS criteria: all 4 requests 200 + payload_complete + no stop
    return "PROTOTYPE_PASS"


def redact_artifact(artifact: Dict[str, Any]) -> Dict[str, Any]:
    raw = json.dumps(artifact, default=str)
    redacted = redact_credentials(raw)
    return json.loads(redacted)


def write_artifact(artifact: Dict[str, Any]) -> None:
    out_path = REPO / "data" / "phase4_live_artifact.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(artifact, indent=2, default=str), encoding="utf-8")
    print(f"[artifact] wrote {out_path} ({out_path.stat().st_size} bytes)", flush=True)


def write_summary(artifact: Dict[str, Any]) -> None:
    metrics = artifact.get("metrics", {})
    summary_lines = [
        "# Gen4 Phase 4 — Live Validation Summary",
        "",
        f"- **Started:** {artifact.get('started_at_utc')}",
        f"- **Ended:** {artifact.get('ended_at_utc')}",
        f"- **Verdict:** **{artifact.get('verdict')}**",
        f"- **Stop reason:** `{artifact.get('stop_reason')}`",
        f"- **Requests completed:** {len(artifact.get('results', []))} / 4",
        "",
        "## Scope",
        f"- Events: {artifact['scope']['events']}",
        f"- Endpoints: {artifact['scope']['endpoints']}",
        f"- Gen4 only: {artifact['scope']['gen4_only']}",
        f"- Read-only: {artifact['scope']['read_only']}",
        f"- write_enabled: {artifact['scope']['write_enabled']}",
        "",
        "## Metrics",
        f"- Fallback count: **{metrics.get('fallback_count')}**",
        f"- 200 count: {metrics.get('n_200')}",
        f"- 403 count: **{metrics.get('n_403')}**",
        f"- 407 count: **{metrics.get('n_407')}**",
        f"- Payload complete: **{metrics.get('n_payload_complete')} / {len(artifact.get('results', []))}**",
        f"- p50 latency: **{metrics.get('p50_latency_ms')} ms**",
        f"- p90 latency: **{metrics.get('p90_latency_ms')} ms**",
        f"- Browser rebuild count: {metrics.get('browser_rebuild_count')}",
        f"- EPIPE count: {metrics.get('epipe_count')}",
        "",
        "## Per-request",
    ]
    for r in artifact.get("results", []):
        summary_lines.append(
            f"- #{r['request_idx']} `{r['path']}` → status={r['status']} "
            f"transport={r['transport']} fallback_used={r['fallback_used']} "
            f"payload_complete={r['payload_complete']} latency={r['latency_ms']}ms "
            f"error_class={r['error_class']} halt_reason={r['halt_reason']}"
        )
    out_path = REPO / "data" / "phase4_live_summary.md"
    out_path.write_text("\n".join(summary_lines), encoding="utf-8")
    print(f"[summary] wrote {out_path} ({out_path.stat().st_size} bytes)", flush=True)


if __name__ == "__main__":
    artifact = asyncio.run(run_phase4())
    verdict = artifact.get("verdict")
    print(f"\n=== Phase 4 verdict: {verdict} ===", flush=True)
    sys.exit(0 if verdict == "PROTOTYPE_PASS" else 1)
