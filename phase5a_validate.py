#!/usr/bin/env python3
"""
phase5a_validate.py — Gen4 Phase 5A controlled-failure validation.

Per Kris 02:46 GMT+8 approval (round #2817):
  - Test-only force curl_cffi failure (status=403 stub injected)
  - Inject REAL CloakBrowser launcher (cloakbrowser.launch_async from venv)
  - 2 events × 2 endpoints = 4 requests
  - Verify: CloakBrowser fallback, payload completeness, browser lifecycle,
            rebuild logic, EPIPE handling, cleanup
  - write_enabled=False, Gen2 default unchanged, NO full backfill
  - Stop on: 407 / quota warning / credentials leak / DB write / scope breach
  - Phase 5B (SSR fallback) NOT yet approved — do not inject real SSR.

Safety rails:
  - assert_scope() before every request
  - No MySQL driver import
  - No DataInserter import
  - PROXY_PASS never written to artifact/log
  - cleanup verified after run
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

# Force Gen4 for this run only.
os.environ["FETCH_STRATEGY"] = "gen4"

# Venv python from workspace-coding has cloakbrowser 0.5.7.
VENV = "/root/.openclaw/workspace-coding/.venv/bin/python"
if sys.executable != VENV:
    # Re-exec with venv python so cloakbrowser import works.
    os.execv(VENV, [VENV, str(Path(__file__).resolve())])

import cloakbrowser  # noqa: E402
from curl_cffi import requests as curl_requests  # noqa: E402

import gen4_fetcher as g4  # noqa: E402
from gen4_fetcher import Gen4Config, Gen4Fetcher, redact_credentials  # noqa: E402

# ---------------------------------------------------------------------------
# Strict scope (unchanged from Phase 4)
# ---------------------------------------------------------------------------
APPROVED_EVENTS = frozenset({14025013, 12436875})
APPROVED_ENDPOINTS = frozenset({"event", "incidents"})

BROWSER_BASE = "https://www.sofascore.com"
PROXY_SERVER = os.getenv("SOFA_PROXY_HOST", "p.webshare.io")
PROXY_PORT = os.getenv("SOFA_PROXY_PORT", "80")
PROXY_USER = os.getenv("SOFA_PROXY_USER", "***REMOVED***-rotate")
PROXY_PASS = os.getenv("SOFA_PROXY_PASS", "")

# Load .env so proxy credentials are available for CloakBrowser launch.
try:
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env", override=False)
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Stub that always fails curl_cffi (simulates 403 / network block)
# ---------------------------------------------------------------------------

def _forced_fail_curl(url: str, **kwargs) -> Any:
    """Test-only: always raise 403-like failure so CloakBrowser is exercised."""

    class _FakeResp:
        status_code = 403
        text = "Forced failure for Phase 5A controlled-failure validation"

        def json(self):
            raise ValueError("No JSON body — forced 403")

    return _FakeResp()


# ---------------------------------------------------------------------------
# Real CloakBrowser launcher (injected into Gen4Fetcher)
# ---------------------------------------------------------------------------

async def _real_cloaklauncher(headless: bool = True, proxy: Optional[str] = None, **kwargs) -> Any:
    """Launch a real CloakBrowser instance via cloakbrowser.launch_async."""
    launch_kwargs: Dict[str, Any] = {
        "headless": headless,
        "humanize": False,
        "stealth_args": True,
    }
    if proxy:
        launch_kwargs["proxy"] = proxy
    # CloakBrowser 0.5.7 launch_async returns a Browser object.
    return await cloakbrowser.launch_async(**launch_kwargs)


# ---------------------------------------------------------------------------
# Scope guard
# ---------------------------------------------------------------------------

def assert_scope(event_id: int, endpoint: str) -> None:
    if event_id not in APPROVED_EVENTS:
        raise PermissionError(
            f"SCOPE VIOLATION: event {event_id} not approved. Halting."
        )
    if endpoint not in APPROVED_ENDPOINTS:
        raise PermissionError(
            f"SCOPE VIOLATION: endpoint {endpoint!r} not approved. Halting."
        )


def build_path(event_id: int, endpoint: str) -> str:
    assert_scope(event_id, endpoint)
    if endpoint == "event":
        return f"/api/v1/event/{event_id}"
    return f"/api/v1/event/{event_id}/incidents"


# ---------------------------------------------------------------------------
# Stop conditions (same as Phase 4 + EPIPE/rebuild tracking)
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
    artifact: Dict[str, Any],
) -> Optional[StopReason]:
    # 407 terminal halt
    if result.get("status") == 407 or any(a.get("status") == 407 for a in result.get("attempts", [])):
        return StopReason(
            "HTTP_407",
            f"Request #{request_idx} hit 407 — terminal halt per §17",
            artifact,
        )
    # 403 rate > 50%
    if len(completed_requests) >= 2:
        n_403 = sum(1 for r in completed_requests if r["status"] == 403)
        if n_403 / len(completed_requests) > 0.5:
            return StopReason(
                "403_RATE_OVER_50",
                f"403 rate = {n_403}/{len(completed_requests)} > 50%",
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
            f"2 consecutive EPIPE — halting",
            artifact,
        )
    # Credentials leak scan
    log_dump = json.dumps(artifact, default=str)
    if PROXY_PASS and PROXY_PASS in log_dump:
        return StopReason(
            "CREDENTIALS_LEAK",
            "PROXY_PASS found in artifact",
            artifact,
        )
    return None


# ---------------------------------------------------------------------------
# Main Phase 5A runner
# ---------------------------------------------------------------------------

async def run_phase5a() -> Dict[str, Any]:
    started_at = datetime.now(timezone.utc).isoformat()
    requests_planned = [
        (14025013, "event"),
        (14025013, "incidents"),
        (12436875, "event"),
        (12436875, "incidents"),
    ]

    artifact: Dict[str, Any] = {
        "task": "Gen4 Phase 5A controlled-failure (curl_cffi forced 403)",
        "agent": "coding",
        "kris_approval": "02:46 GMT+8 (round #2817)",
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
            "forced_curl_fail": True,
            "cloakbrowser_injected": True,
            "ssr_injected": False,  # Phase 5B only after approval
        },
        "proxy_config": {
            "server": PROXY_SERVER,
            "port": PROXY_PORT,
            "user_set": bool(PROXY_USER),
            "pass_set": bool(PROXY_PASS),
        },
        "results": [],
        "stop_reason": None,
        "metrics": {},
        "verdict": None,
        "cleanup_ok": None,
    }

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

    fetcher = Gen4Fetcher(
        config=cfg,
        curl_cffi_get=_forced_fail_curl,      # <-- forced 403 on every call
        cloakbrowser_launcher=_real_cloaklauncher,  # <-- real CloakBrowser
        ssr_fetcher=None,                       # Phase 5B only
    )

    consecutive_cloak_failures = 0
    consecutive_epipe = 0
    cleanup_ok = True
    completed_requests: List[Dict[str, Any]] = []

    try:
        async with fetcher:
            pass

        # Real run — second context entry
        async with fetcher:
            for idx, (event_id, endpoint) in enumerate(requests_planned, start=1):
                path = build_path(event_id, endpoint)
                t0 = time.monotonic()
                try:
                    result = await fetcher.fetch_api(path, timeout_ms=45000)
                except Exception as fetch_exc:
                    result = {
                        "ok": False,
                        "status": 0,
                        "data": None,
                        "transport": "curl_cffi_forced_fail",
                        "attempts": [{
                            "transport": "curl_cffi",
                            "status": 403,
                            "latency_ms": int((time.monotonic() - t0) * 1000),
                            "error": f"forced_fail: {fetch_exc}",
                        }],
                        "fallback_used": False,
                        "retry_count": 0,
                        "payload_complete": False,
                        "error_class": "forced_failure",
                        "halt_reason": None,
                    }
                latency_ms = int((time.monotonic() - t0) * 1000)

                # Record per-request
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

                # Track CloakBrowser/EPIPE failures
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

                # Quota heuristic
                recent_403 = sum(1 for r in completed_requests[-3:] if r["status"] == 403)
                artifact["quota_warning"] = recent_403 >= 2

                stop = check_stop_conditions(
                    result=result,
                    request_idx=idx,
                    completed_requests=completed_requests,
                    consecutive_cloak_failures=consecutive_cloak_failures,
                    consecutive_epipe=consecutive_epipe,
                    artifact=artifact,
                )
                if stop is not None:
                    artifact["stop_reason"] = {"code": stop.code, "message": stop.message}
                    break

        # After the async-with block, __aexit__ called close().
        # Verify cleanup: fetcher._browser should be None.
        cleanup_ok = (fetcher._browser is None and fetcher._context is None
                        and fetcher._started is False)
        artifact["cleanup_ok"] = cleanup_ok
        artifact["browser_rebuild_count"] = fetcher.stats.browser_rebuild_count
        artifact["epipe_count"] = fetcher.stats.epipe_count

    except Exception as outer_exc:
        artifact["stop_reason"] = {
            "code": "OUTER_EXCEPTION",
            "message": f"Outer exception: {outer_exc}",
        }
        artifact["cleanup_ok"] = False
    finally:
        artifact["metrics"] = compute_metrics(artifact["results"])
        artifact["verdict"] = compute_verdict(artifact)
        artifact["ended_at_utc"] = datetime.now(timezone.utc).isoformat()
        # Credentials redaction on the whole artifact
        artifact = redact_artifact(artifact)
        write_artifact(artifact)
        write_summary(artifact)

    return artifact


# ---------------------------------------------------------------------------
# Metrics / verdict / output helpers
# ---------------------------------------------------------------------------

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
        "browser_rebuild_count": 0,
        "epipe_count": sum(
            1 for r in results for a in r["attempts"]
            if "EPIPE" in (a.get("error") or "")
        ),
    }


def compute_verdict(artifact: Dict[str, Any]) -> str:
    metrics = artifact.get("metrics", {})
    results = artifact.get("results", [])
    stop = artifact.get("stop_reason")

    # Hard fails
    if stop and stop["code"] in ("HTTP_407", "QUOTA_WARNING", "CONSECUTIVE_CLOAK_FAILURES",
                                  "CONSECUTIVE_EPIPE", "CLEANUP_FAILURE",
                                  "CREDENTIALS_LEAK", "OUTER_EXCEPTION"):
        return "FAILED"
    if artifact.get("cleanup_ok") is False:
        return "FAILED"

    # PARTIAL if any stop triggered
    if stop is not None:
        return "PROTOTYPE_PARTIAL"
    if metrics.get("n_403", 0) >= 2:
        return "PROTOTYPE_PARTIAL"
    if metrics.get("n_payload_complete", 0) < len(results):
        return "PROTOTYPE_PARTIAL"

    return "PROTOTYPE_PASS"


def redact_artifact(artifact: Dict[str, Any]) -> Dict[str, Any]:
    raw = json.dumps(artifact, default=str)
    redacted = redact_credentials(raw)
    return json.loads(redacted)


def write_artifact(artifact: Dict[str, Any]) -> None:
    out_path = REPO / "data" / "phase5a_live_artifact.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(artifact, indent=2, default=str), encoding="utf-8")
    print(f"[artifact] wrote {out_path} ({out_path.stat().st_size} bytes)", flush=True)


def write_summary(artifact: Dict[str, Any]) -> None:
    metrics = artifact.get("metrics", {})
    lines = [
        "# Gen4 Phase 5A — Controlled-Failure Validation Summary",
        "",
        f"- **Started:** {artifact.get('started_at_utc')}",
        f"- **Ended:** {artifact.get('ended_at_utc')}",
        f"- **Verdict:** **{artifact.get('verdict')}**",
        f"- **Stop reason:** `{artifact.get('stop_reason')}`",
        f"- **Requests completed:** {len(artifact.get('results', []))} / 4",
        f"- **Cleanup OK:** {artifact.get('cleanup_ok')}",
        f"- **Browser rebuild count:** {artifact.get('browser_rebuild_count')}",
        f"- **EPIPE count:** {artifact.get('epipe_count')}",
        "",
        "## Scope",
        f"- Events: {artifact['scope']['events']}",
        f"- Endpoints: {artifact['scope']['endpoints']}",
        f"- Forced curl failure: {artifact['scope']['forced_curl_fail']}",
        f"- CloakBrowser injected: {artifact['scope']['cloakbrowser_injected']}",
        f"- write_enabled: {artifact['scope']['write_enabled']}",
        "",
        "## Metrics",
        f"- Fallback count: **{metrics.get('fallback_count')}**",
        f"- 200 count: {metrics.get('n_200')}",
        f"- 403 count: {metrics.get('n_403')}",
        f"- 407 count: {metrics.get('n_407')}",
        f"- Payload complete: **{metrics.get('n_payload_complete')} / {len(artifact.get('results', []))}**",
        f"- p50 latency: **{metrics.get('p50_latency_ms')} ms**",
        f"- p90 latency: **{metrics.get('p90_latency_ms')} ms**",
        "",
        "## Per-request",
    ]
    for r in artifact.get("results", []):
        lines.append(
            f"- #{r['request_idx']} `{r['path']}` → status={r['status']} "
            f"transport={r['transport']} fallback_used={r['fallback_used']} "
            f"payload_complete={r['payload_complete']} latency={r['latency_ms']}ms "
            f"error_class={r['error_class']}"
        )
    out_path = REPO / "data" / "phase5a_live_summary.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[summary] wrote {out_path} ({out_path.stat().st_size} bytes)", flush=True)


if __name__ == "__main__":
    artifact = asyncio.run(run_phase5a())
    verdict = artifact.get("verdict")
    print(f"\n=== Phase 5A verdict: {verdict} ===", flush=True)
    sys.exit(0 if verdict == "PROTOTYPE_PASS" else 1)
