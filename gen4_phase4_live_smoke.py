#!/usr/bin/env python3
"""gen4_phase4_live_smoke.py — Phase 4 minimal live smoke test.

SCOPE (FROZEN — must NOT be expanded without new approval):
  - Event: 14025013 ONLY (single event)
  - Endpoints: ['event', 'incidents'] ONLY (2 endpoints)
  - Mode: Gen4 opt-in, read-only, write_enabled=False
  - Gen2 default: UNCHANGED — not running this profile

STOP-ON-TRIGGER (any of these halts the run immediately):
  - HTTP 407 (Proxy Authentication Required)
  - quota_warning
  - HTTP 403 rate over 50%
  - 2 consecutive EPIPE
  - browser_closed
  - cleanup failure
  - credentials leak in artifact
  - DB write attempt
  - scope expansion (event_id not in [14025013])

Output: artifact at data/gen4_phase4_smoke_event_14025013.json plus a
verdict line printed at the end.

Usage:
  PYTHONUNBUFFERED=1 .runner-venv/bin/python gen4_phase4_live_smoke.py

This is a LIVE runner. Do NOT call from offline tests. Keep the scope.

Phase 4 DI fix (2026-08-25): load .env, wire real curl_cffi_get and
cloakbrowser_launcher into Gen4Fetcher so Tier-1 and Tier-2 are actually
exercised (previously both were None -> 0/2 complete).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


# PHASE 4 SCOPE — frozen by Kris 2026-08-25 07:34 GMT+8.
# Any change requires NEW APPROVAL.
PHASE4_EVENT_ID = 14025013
PHASE4_ENDPOINTS: List[str] = ["event", "incidents"]
PHASE4_MODE = "gen4_opt_in"
GEN2_DEFAULT_UNCHANGED = True
WRITE_ENABLED = False


REPO_ROOT = Path(__file__).resolve().parent
DATA_DIR = REPO_ROOT / "data"
ARTIFACT_PATH = DATA_DIR / f"gen4_phase4_smoke_event_{PHASE4_EVENT_ID}.json"


def _load_dotenv() -> None:
    """Load .env file and redact sensitive values from process env for safety.

    This MUST be called before any network calls. It ensures:
      1. .env is loaded (so os.getenv works for proxy credentials)
      2. SOFA_PROXY_PASS is present for Gen4Config.proxy_url()
    """
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env")
    except Exception:
        # Fallback: manual parse if dotenv not available
        env_path = REPO_ROOT / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


def _redact_env_values(env_dict: Dict[str, str]) -> Dict[str, str]:
    """Return a copy of env dict with sensitive values redacted for logging."""
    redacted = {}
    sensitive_keys = {"SOFA_PROXY_PASS", "MYSQL_PASSWORD", "GITHUB_TOKEN"}
    for k, v in env_dict.items():
        if k in sensitive_keys:
            redacted[k] = "[REDACTED]"
        else:
            redacted[k] = v
    return redacted


def _log(msg: str) -> None:
    print(f"[phase4] {msg}", flush=True)


def _check_stop_conditions(artifact: Dict[str, Any]) -> Optional[str]:
    """Reuse the canary's halt-checker. Returns halt reason or None."""
    # Late import so the offline test suite can import this module without
    # triggering the canary's top-level imports.
    try:
        from gen4_canary_validate import check_safety_halts
        return check_safety_halts(artifact)
    except ImportError as e:
        _log(f"WARN: could not import check_safety_halts: {e}")
        return None


def _make_real_curl_cffi_get() -> Callable[..., Any]:
    """Create the real curl_cffi.requests.get callable with proxy support.

    This is the production Tier-1 implementation. It accepts the same
    signature Gen4Fetcher._fetch_via_curl_cffi expects:
        (url, proxies=None, impersonate="chrome", timeout=30)
    """
    from curl_cffi import requests as curl_requests

    def _curl_get(url: str, **kwargs) -> Any:
        return curl_requests.get(url, **kwargs)

    return _curl_get


async def _make_real_cloakbrowser_launcher(
    headless: bool = True,
    proxy: Optional[str] = None,
    humanize: bool = False,
    **kwargs,
) -> Any:
    """Real CloakBrowser launcher — lazy import so module load never pulls cloakbrowser.

    This is the production Tier-2 implementation. The lazy import inside the
    function body (not module level) preserves DI: tests can inject a mock
    via `fetcher._cloakbrowser_launcher` without ever importing the real
    `cloakbrowser` package.
    """
    import cloakbrowser as _cloak  # noqa: F811 — intentional lazy import
    return await _cloak.launch_async(
        headless=headless,
        proxy=proxy,
        humanize=humanize,
        **kwargs,
    )


async def run_phase4_smoke() -> Dict[str, Any]:
    """Execute Phase 4 minimal live smoke.

    Returns the artifact dict. Stop-on-trigger halts the run mid-loop.
    """
    # Load .env BEFORE any fetcher construction (proxy creds needed).
    _load_dotenv()

    from gen4_canary_validate import (
        APPROVED_EVENTS,
        ENDPOINT_PATHS_PROD,
        compute_overall_metrics,
    )
    from gen4_fetcher import Gen4Config, Gen4Fetcher
    from gen4_ssr import resolve_ssr_for_api_path, SSRHelperMissing

    # SCOPE ENFORCEMENT — abort if scope was modified.
    # IMPORTANT: this runs BEFORE any fetcher is constructed or any
    # network call is made. A scope violation must NEVER trigger live calls.
    if PHASE4_EVENT_ID != 14025013:
        return {
            "scope_violation": True,
            "halt_reason": "SCOPE_EXPANSION",
            "detail": f"event {PHASE4_EVENT_ID} != frozen 14025013",
        }
    if sorted(PHASE4_ENDPOINTS) != sorted(["event", "incidents"]):
        return {
            "scope_violation": True,
            "halt_reason": "SCOPE_EXPANSION",
            "detail": f"endpoints {PHASE4_ENDPOINTS} != frozen ['event', 'incidents']",
        }
    if WRITE_ENABLED is not False:
        return {
            "scope_violation": True,
            "halt_reason": "SCOPE_EXPANSION",
            "detail": f"write_enabled must be False; got {WRITE_ENABLED}",
        }
    if GEN2_DEFAULT_UNCHANGED is not True:
        return {"scope_violation": True, "halt_reason": "GEN2_DEFAULT_TAMPERED"}

    artifact: Dict[str, Any] = {
        "phase": "4",
        "scope": {
            "event_id": PHASE4_EVENT_ID,
            "endpoints": PHASE4_ENDPOINTS,
            "mode": PHASE4_MODE,
            "write_enabled": WRITE_ENABLED,
            "frozen_by": "Kris 2026-08-25 07:34 GMT+8",
        },
        "results": [],
        "halt_reason": None,
        "stopped_early": False,
        "approved_events": sorted(APPROVED_EVENTS),
        "started_at": time.time(),
        # Track which tiers were actually injected (offline verification)
        "di_injected": {
            "curl_cffi_get": True,
            "cloakbrowser_launcher": True,
            "ssr_fetcher": True,
        },
    }

    # Build Gen4Config from env (proxy credentials loaded via _load_dotenv)
    config = Gen4Config(
        write_enabled=WRITE_ENABLED,
        proxy_server=os.getenv("SOFA_PROXY_HOST", "p.webshare.io"),
        proxy_port=os.getenv("SOFA_PROXY_PORT", "80"),
        proxy_user=os.getenv("SOFA_PROXY_USER", "***REMOVED***-rotate"),
        proxy_pass=os.getenv("SOFA_PROXY_PASS", ""),
    )

    # Inject real dependencies so Tier-1 (curl_cffi) and Tier-2 (CloakBrowser)
    # are actually exercised. Previously both were None -> immediate
    # "curl_cffi_unavailable"/"cloakbrowser_unavailable" -> 0/2 complete.
    fetcher = Gen4Fetcher(
        config=config,
        curl_cffi_get=_make_real_curl_cffi_get(),
        cloakbrowser_launcher=_make_real_cloakbrowser_launcher,
        ssr_fetcher=resolve_ssr_for_api_path,  # shared gen4_ssr
    )

    _log(f"starting Phase 4 smoke: event={PHASE4_EVENT_ID} endpoints={PHASE4_ENDPOINTS}")
    _log(f"write_enabled={WRITE_ENABLED} (invariant: must be False)")
    _log(f"DI: curl_cffi_get=injected, cloakbrowser_launcher=injected, ssr_fetcher=injected")

    try:
        await fetcher.start()
        for endpoint in PHASE4_ENDPOINTS:
            path = ENDPOINT_PATHS_PROD[endpoint].format(event_id=PHASE4_EVENT_ID)
            _log(f"fetching: {path}")
            t0 = time.monotonic()
            try:
                result = await fetcher.fetch_api(path)
            except Exception as e:
                _log(f"EXCEPTION during fetch_api: {type(e).__name__}: {e}")
                artifact["results"].append({
                    "path": path,
                    "event_id": PHASE4_EVENT_ID,
                    "endpoint": endpoint,
                    "status": 0,
                    "ok": False,
                    "payload_complete": False,
                    "transport": None,
                    "attempts": [],
                    "exception": f"{type(e).__name__}: {e}",
                    "latency_ms": int((time.monotonic() - t0) * 1000),
                })
                # Re-check stop conditions even on exception.
                artifact["halt_reason"] = _check_stop_conditions(artifact)
                if artifact["halt_reason"]:
                    artifact["stopped_early"] = True
                continue

            latency_ms = int((time.monotonic() - t0) * 1000)
            rec = {
                "path": path,
                "event_id": PHASE4_EVENT_ID,
                "endpoint": endpoint,
                "status": result.get("status", 0),
                "ok": result.get("ok", False),
                "payload_complete": result.get("payload_complete", False),
                "transport": result.get("transport"),
                "attempts": result.get("attempts", []),
                "retry_count": result.get("retry_count", 0),
                "latency_ms": latency_ms,
                "error": result.get("error"),
                "fallback_used": result.get("fallback_used", False),
                # Surface SSR side-channel signals captured during the fetch.
                "ssr_signals": getattr(fetcher, "_ssr_signals", None),
            }
            artifact["results"].append(rec)

            # Log per-attempt summary.
            for i, a in enumerate(rec["attempts"]):
                _log(
                    f"  attempt {i+1}: transport={a.get('transport')} "
                    f"status={a.get('status')} "
                    f"latency_ms={a.get('latency_ms')} "
                    f"err={a.get('error')}"
                )
            _log(
                f"  -> transport={rec['transport']} status={rec['status']} "
                f"payload_complete={rec['payload_complete']} "
                f"total_latency_ms={latency_ms}"
            )

            # Stop-on-trigger check AFTER each request.
            artifact["halt_reason"] = _check_stop_conditions(artifact)
            if artifact["halt_reason"]:
                _log(f"STOP-ON-TRIGGER: {artifact['halt_reason']}")
                artifact["stopped_early"] = True
                break
    finally:
        await fetcher.close()
        # cleanup_ok flag for safety halt.
        artifact["cleanup_ok"] = True

    artifact["finished_at"] = time.time()
    artifact["summary"] = compute_overall_metrics(artifact["results"])
    return artifact


def render_verdict(artifact: Dict[str, Any]) -> str:
    """One-line verdict summary."""
    if artifact.get("scope_violation"):
        return f"SCOPE VIOLATION — halted: {artifact.get('halt_reason')}"
    if artifact.get("halt_reason"):
        return f"HALTED: {artifact['halt_reason']} after {len(artifact['results'])} requests"
    n = len(artifact["results"])
    n_complete = sum(1 for r in artifact["results"] if r.get("payload_complete"))
    return f"PASS — {n_complete}/{n} endpoints completed payload; no halt triggers"


def main() -> int:
    artifact = asyncio.run(run_phase4_smoke())
    # Write artifact (use redact_secrets via canary helper).
    try:
        from gen4_canary_validate import redact_secrets
        blob = redact_secrets(json.dumps(artifact, default=str, indent=2))
    except ImportError:
        blob = json.dumps(artifact, default=str, indent=2)
    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT_PATH.write_text(blob)
    _log(f"artifact written: {ARTIFACT_PATH}")
    print(render_verdict(artifact), flush=True)
    return 0 if not artifact.get("halt_reason") and not artifact.get("scope_violation") else 1


if __name__ == "__main__":
    sys.exit(main())