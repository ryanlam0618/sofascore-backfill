#!/usr/bin/env python3
"""phase5b_validate.py — Gen4 Phase 5B controlled-failure SSR fallback validation.

Per Kris 03:16 GMT+8 approval (round #2825):
- Force curl_cffi fail (403 stub)
- Force CloakBrowser fail (browser unavailable stub)
- Inject REAL SSR fetcher (using stable_proxy_fetch / backfill_runner._map_ssr_to_api_format)
- 2 events × 2 endpoints = 4 requests
- Verify SSR fallback, 3-tier attempt records, payload completeness, result contract,
  cleanup, no-write invariant
- write_enabled=False, Gen2 default unchanged, NO full backfill, NO scope expansion
- Stop on: 407, quota warning, credentials leak, DB write, cleanup failure,
           consecutive 2 fallback crashes
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

os.environ["FETCH_STRATEGY"] = "gen4"

# Venv python from workspace-coding has cloakbrowser 0.5.7 + curl_cffi.
VENV = "/root/.openclaw/workspace-coding/.venv/bin/python"
if sys.executable != VENV:
    os.execv(VENV, [VENV, str(Path(__file__).resolve())])

import cloakbrowser  # noqa: E402
from curl_cffi import requests as curl_requests  # noqa: E402

import gen4_fetcher as g4  # noqa: E402
from gen4_fetcher import Gen4Config, Gen4Fetcher, redact_credentials  # noqa: E402
# NOTE: do NOT import backfill_runner (pulls mysql.connector). Inline the SSR mapper below.

# ---------------------------------------------------------------------------
# Strict scope (unchanged from Phase 4/5A)
# ---------------------------------------------------------------------------
APPROVED_EVENTS = frozenset({14025013, 12436875})
APPROVED_ENDPOINTS = frozenset({"event", "incidents"})

BROWSER_BASE = "https://www.sofascore.com"
PROXY_SERVER = os.getenv("SOFA_PROXY_HOST", "p.webshare.io")
PROXY_PORT = os.getenv("SOFA_PROXY_PORT", "80")
PROXY_USER = os.getenv("SOFA_PROXY_USER", "***REMOVED***-rotate")
PROXY_PASS = os.getenv("SOFA_PROXY_PASS", "")

# ---------------------------------------------------------------------------
# Stub curl_cffi (forced 403) and stub CloakBrowser launcher (forced fail)
# ---------------------------------------------------------------------------

def _forced_fail_curl(url: str, **kwargs) -> Any:
    class _FakeResp:
        status_code = 403
        text = "Forced failure for Phase 5B (curl)"
        def json(self):
            raise ValueError("No JSON body — forced 403")
    return _FakeResp()


async def _forced_fail_cloak(headless=True, proxy=None, **kwargs) -> Any:
    """Return a stub browser object that raises immediately when used."""
    class _BrokenBrowser:
        async def new_page(self):
            raise Exception("Forced CloakBrowser failure for Phase 5B test")
        async def close(self):
            pass
        contexts = []
    return _BrokenBrowser()


# ---------------------------------------------------------------------------
# Real SSR fetcher — reuse backfill_runner._map_ssr_to_api_format
# Fetches https://www.sofascore.com/event/{id} HTML and extracts __NEXT_DATA__.
# ---------------------------------------------------------------------------

def _ssr_incidents_from_event(event_id: int) -> Optional[Dict[str, Any]]:
    """Fetch SSR HTML for an event and attempt to extract incidents data."""
    url = f"{BROWSER_BASE}/event/{event_id}"
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return _ssr_via_curl(event_id, "incidents")
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, timeout=30000)
            # Wait for __NEXT_DATA__ to exist
            page.wait_for_selector("#__NEXT_DATA__", timeout=15000)
            raw = page.evaluate("() => { const el = document.querySelector('#__NEXT_DATA__'); return el ? el.textContent : null; }")
            browser.close()
            if not raw:
                return None
            data = json.loads(raw)
            # The structure may contain incidents nested in event; just return event
            return _map_ssr(data, f"/api/v1/event/{event_id}/incidents")
    except Exception:
        return _ssr_via_curl(event_id, "incidents")


def _ssr_event_from_event(event_id: int) -> Optional[Dict[str, Any]]:
    """Fetch SSR HTML for an event and return the 'event' node."""
    url = f"{BROWSER_BASE}/event/{event_id}"
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return _ssr_via_curl(event_id, "event")
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, timeout=30000)
            page.wait_for_selector("#__NEXT_DATA__", timeout=15000)
            raw = page.evaluate("() => { const el = document.querySelector('#__NEXT_DATA__'); return el ? el.textContent : null; }")
            browser.close()
            if not raw:
                return None
            data = json.loads(raw)
            return _map_ssr(data, f"/api/v1/event/{event_id}")
    except Exception:
        return _ssr_via_curl(event_id, "event")


def _ssr_via_curl(event_id: int, endpoint: str) -> Optional[Dict[str, Any]]:
    """Fallback: use curl_cffi to grab the HTML page and parse __NEXT_DATA__."""
    try:
        resp = curl_requests.get(f"{BROWSER_BASE}/event/{event_id}",
                                 impersonate="chrome", timeout=25,
                                 headers={"Accept": "text/html"})
        if resp.status_code != 200:
            return None
        m = __import__("re").search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            resp.text, __import__("re").S)
        if not m:
            return None
        data = json.loads(m.group(1))
        return _map_ssr(data, f"/api/v1/event/{event_id}/{endpoint}")
    except Exception:
        return None


def _map_ssr(raw: Any, path: str) -> Optional[Dict[str, Any]]:
    """Reuse the same mapping logic from backfill_runner._map_ssr_to_api_format."""
    if not isinstance(raw, (dict, list)):
        return None
    match = re.search(r"/event/[^/]+/([^/?]+)", path or "")
    endpoint = match.group(1) if match else ("event" if "/event/" in (path or "") else None)
    aliases = {
        "event": ("event",), "incidents": ("incidents",),
        "lineups": ("lineups", "lineup"), "statistics": ("statistics", "stats"),
        "shotmap": ("shotmap", "shotMap"), "graph": ("graph",),
        "comments": ("comments",), "votes": ("votes",),
        "managers": ("managers", "manager"),
        "pregame-form": ("pregameForm",), "average-positions": ("averagePositions",),
        "web-odds": ("odds",), "highlights": ("highlights",),
        "featured-players": ("featuredPlayers",), "achievements": ("achievements",),
    }
    targets = aliases.get(endpoint)
    if not targets:
        return None

    def _lookup(node: Any, key_path: List[str]) -> Any:
        current = node
        for key in key_path:
            if isinstance(current, dict):
                current = current.get(key)
            else:
                return None
            if current is None:
                return None
        return current

    # Direct hit
    if isinstance(raw, dict):
        for key in targets:
            if key in raw:
                return raw[key]

    # Walk known containers
    containers = raw.get("props", {}).get("pageProps", {}) if isinstance(raw, dict) else {}
    for key in targets:
        if isinstance(containers, dict) and key in containers:
            return containers[key]

    # Shallow walk of dict values looking for a match key
    def _walk(node: Any, depth: int = 0) -> Any:
        if depth > 4 or not isinstance(node, (dict, list)):
            return None
        if isinstance(node, dict):
            for key in targets:
                if key in node:
                    return node[key]
            for v in node.values():
                found = _walk(v, depth + 1)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = _walk(item, depth + 1)
                if found is not None:
                    return found
        return None

    return _walk(raw)


import re  # noqa: E402  # placed here to avoid top-level circularity during helpers


# ---------------------------------------------------------------------------
# Scope guard
# ---------------------------------------------------------------------------

def assert_scope(event_id: int, endpoint: str) -> None:
    if event_id not in APPROVED_EVENTS:
        raise PermissionError(
            f"SCOPE VIOLATION: event {event_id} not in approved set. Halting."
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
# Stop conditions
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
    consecutive_fallback_failures: int,
    artifact: Dict[str, Any],
) -> Optional[StopReason]:
    # HTTP 407 terminal halt
    if result.get("status") == 407 or any(a.get("status") == 407 for a in result.get("attempts", [])):
        return StopReason("HTTP_407", f"Request #{request_idx} returned 407 — terminal safety halt", artifact)
    # Quota warning heuristic
    if artifact.get("quota_warning"):
        return StopReason("QUOTA_WARNING", f"Quota warning surfaced at request #{request_idx}", artifact)
    # 403 rate > 50%
    if len(completed_requests) >= 2:
        n_403 = sum(1 for r in completed_requests if r["status"] == 403)
        if n_403 / len(completed_requests) > 0.5:
            return StopReason("403_RATE_OVER_50", f"403 rate = {n_403}/{len(completed_requests)} > 50%", artifact)
    # 2 consecutive fallback crashes
    if consecutive_fallback_failures >= 2:
        return StopReason("CONSECUTIVE_FALLBACK_CRASHES", f"2 consecutive fallback tier crashes", artifact)
    # Credentials leak scan
    raw = json.dumps(artifact, default=str)
    if PROXY_PASS and PROXY_PASS in raw:
        return StopReason("CREDENTIALS_LEAK", "PROXY_PASS detected in artifact", artifact)
    return None


# ---------------------------------------------------------------------------
# Phase 5B main
# ---------------------------------------------------------------------------

async def run_phase5b() -> Dict[str, Any]:
    started_at = datetime.now(timezone.utc).isoformat()
    requests_planned = [
        (14025013, "event"),
        (14025013, "incidents"),
        (12436875, "event"),
        (12436875, "incidents"),
    ]

    artifact: Dict[str, Any] = {
        "task": "Gen4 Phase 5B controlled-failure (curl+cloakbrowser forced fail, SSR active)",
        "agent": "coding",
        "kris_approval": "04:41 GMT+8 (round #2826)",
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
            "forced_cloakbrowser_fail": True,
            "ssr_injected": True,
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

    # SSR resolver keyed per endpoint — documented structural gap below.
    # SofaScore event SSR __NEXT_DATA__ embeds only the `event` hydration
    # node; incidents/lineups/etc. are client-side API calls and CANNOT be
    # served from SSR HTML. We cache the parsed page per event_id so the
    # 2 endpoints per event share one HTML fetch, and short-circuit
    # unsupported endpoints to return None (logged in result contract).
    _SSR_PAGE_CACHE: Dict[int, Optional[Dict[str, Any]]] = {}

    _SSR_UNSUPPORTED_ENDPOINTS = frozenset({
        "incidents", "lineups", "statistics", "shotmap",
        "graph", "comments",
    })

    def _load_ssr_event_page(event_id: int) -> Optional[Dict[str, Any]]:
        if event_id in _SSR_PAGE_CACHE:
            return _SSR_PAGE_CACHE[event_id]
        url = f"{BROWSER_BASE}/event/{event_id}"
        payload: Optional[Dict[str, Any]] = None
        try:
            from playwright.sync_api import sync_playwright
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
                        payload = _map_ssr(data, f"/api/v1/event/{event_id}")
                finally:
                    browser.close()
        except Exception:
            try:
                resp = curl_requests.get(url, impersonate="chrome", timeout=25,
                                         headers={"Accept": "text/html"})
                if resp.status_code == 200:
                    m = re.search(
                        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                        resp.text, re.S)
                    if m:
                        data = json.loads(m.group(1))
                        payload = _map_ssr(data, f"/api/v1/event/{event_id}")
            except Exception:
                payload = None
        _SSR_PAGE_CACHE[event_id] = payload
        return payload

    def ssr_resolver(path: str) -> Optional[Dict[str, Any]]:
        m = re.search(r"/event/(\d+)(?:/([^/?]+))?", path)
        if not m:
            return None
        eid = int(m.group(1))
        endpoint = m.group(2) or "event"
        if endpoint in _SSR_UNSUPPORTED_ENDPOINTS:
            # Documented structural gap — SSR HTML doesn't embed this.
            return None
        return _load_ssr_event_page(eid)

    fetcher = Gen4Fetcher(
        config=cfg,
        curl_cffi_get=_forced_fail_curl,
        cloakbrowser_launcher=_forced_fail_cloak,
        ssr_fetcher=ssr_resolver,
    )

    consecutive_fallback_failures = 0
    cleanup_ok = True
    completed_requests: List[Dict[str, Any]] = []

    try:
        async with fetcher:
            pass

        async with fetcher:
            for idx, (event_id, endpoint) in enumerate(requests_planned, start=1):
                path = build_path(event_id, endpoint)
                t0 = time.monotonic()
                try:
                    result = await fetcher.fetch_api(path, timeout_ms=45000)
                except Exception as fetch_exc:
                    result = {
                        "ok": False, "status": 0, "data": None,
                        "transport": "curl_cffi",
                        "attempts": [{"transport": "curl_cffi", "status": 403,
                                       "latency_ms": int((time.monotonic() - t0) * 1000),
                                       "error": f"forced curl fail: {fetch_exc}"}],
                        "fallback_used": False,
                        "retry_count": 0, "payload_complete": False,
                        "error_class": "forced_failure", "halt_reason": None,
                    }
                latency_ms = int((time.monotonic() - t0) * 1000)

                record = {
                    "request_idx": idx, "event_id": event_id, "endpoint": endpoint,
                    "path": path, "latency_ms": latency_ms,
                    "status": result.get("status"),
                    "transport": result.get("transport"),
                    "attempts": result.get("attempts", []),
                    "fallback_used": result.get("fallback_used", False),
                    "payload_complete": result.get("payload_complete", False),
                    "error_class": result.get("error_class"),
                    "halt_reason": result.get("halt_reason"),
                    "data_keys_top_level": sorted(result["data"].keys())
                                        if isinstance(result.get("data"), dict) else None,
                    "ok": result.get("ok"),
                }
                artifact["results"].append(record)
                completed_requests.append(record)

                # Track FINAL-tier (SSR) crashes only — curl/cloak failures are by design forced.
                if record["attempts"]:
                    last = record["attempts"][-1]
                    if last.get("transport") == "ssr" and last.get("error"):
                        consecutive_fallback_failures += 1
                    else:
                        consecutive_fallback_failures = 0
                else:
                    consecutive_fallback_failures = 0

                # Quota heuristic
                recent_403 = sum(1 for r in completed_requests[-3:] if r["status"] == 403)
                artifact["quota_warning"] = recent_403 >= 2

                stop = check_stop_conditions(
                    result=result, request_idx=idx,
                    completed_requests=completed_requests,
                    consecutive_fallback_failures=consecutive_fallback_failures,
                    artifact=artifact,
                )
                if stop is not None:
                    artifact["stop_reason"] = {"code": stop.code, "message": stop.message}
                    break

        # Cleanup check
        cleanup_ok = (fetcher._browser is None and fetcher._context is None
                        and fetcher._started is False)
        artifact["cleanup_ok"] = cleanup_ok
        artifact["browser_rebuild_count"] = fetcher.stats.browser_rebuild_count
        artifact["epipe_count"] = fetcher.stats.epipe_count

    except Exception as outer_exc:
        artifact["stop_reason"] = {"code": "OUTER_EXCEPTION", "message": str(outer_exc)}
        artifact["cleanup_ok"] = False
    finally:
        artifact["metrics"] = compute_metrics(artifact["results"])
        artifact["verdict"] = compute_verdict(artifact)
        artifact["ended_at_utc"] = datetime.now(timezone.utc).isoformat()
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
        "ssr_success_count": sum(1 for r in results if r["transport"] == "ssr" and r["ok"]),
        "p50_latency_ms": p50, "p90_latency_ms": p90,
        "min_latency_ms": min(latencies), "max_latency_ms": max(latencies),
        "n_403": sum(1 for r in results if r["status"] == 403),
        "n_407": sum(1 for r in results if r["status"] == 407),
        "n_200": sum(1 for r in results if r["status"] == 200),
        "n_payload_complete": sum(1 for r in results if r["payload_complete"]),
    }


def compute_verdict(artifact: Dict[str, Any]) -> str:
    metrics = artifact.get("metrics", {})
    results = artifact.get("results", [])
    stop = artifact.get("stop_reason")

    # Hard fails
    if stop and stop["code"] in ("HTTP_407", "QUOTA_WARNING", "CREDENTIALS_LEAK",
                                  "CONSECUTIVE_FALLBACK_CRASHES", "OUTER_EXCEPTION"):
        return "FAILED"
    if artifact.get("cleanup_ok") is False:
        return "FAILED"
    if metrics.get("n_payload_complete", 0) == 0:
        return "FAILED"

    # PARTIAL if any stop or payload completeness < 100%
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
    out_path = REPO / "data" / "phase5b_live_artifact.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(artifact, indent=2, default=str), encoding="utf-8")
    print(f"[artifact] wrote {out_path} ({out_path.stat().st_size} bytes)", flush=True)


def write_summary(artifact: Dict[str, Any]) -> None:
    metrics = artifact.get("metrics", {})
    lines = [
        "# Gen4 Phase 5B — Controlled-Failure SSR Validation Summary",
        "",
        f"- **Started:** {artifact.get('started_at_utc')}",
        f"- **Ended:** {artifact.get('ended_at_utc')}",
        f"- **Verdict:** **{artifact.get('verdict')}**",
        f"- **Stop reason:** `{artifact.get('stop_reason')}`",
        f"- **Requests completed:** {len(artifact.get('results', []))} / 4",
        f"- **Cleanup OK:** {artifact.get('cleanup_ok')}",
        f"- **Browser rebuilds:** {artifact.get('browser_rebuild_count')}",
        f"- **EPIPE count:** {artifact.get('epipe_count')}",
        "",
        "## Scope",
        f"- Events: {artifact['scope']['events']}",
        f"- Endpoints: {artifact['scope']['endpoints']}",
        f"- Forced curl fail: {artifact['scope']['forced_curl_fail']}",
        f"- Forced CloakBrowser fail: {artifact['scope']['forced_cloakbrowser_fail']}",
        f"- SSR injected: {artifact['scope']['ssr_injected']}",
        f"- write_enabled: {artifact['scope']['write_enabled']}",
        "",
        "## Metrics",
        f"- Fallback count: **{metrics.get('fallback_count')}**",
        f"- SSR success count: **{metrics.get('ssr_success_count')} / 4**",
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
            f"transport={r['transport']} ok={r['ok']} "
            f"fallback_used={r['fallback_used']} payload_complete={r['payload_complete']} "
            f"latency={r['latency_ms']}ms error_class={r['error_class']}"
        )
    out_path = REPO / "data" / "phase5b_live_summary.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[summary] wrote {out_path} ({out_path.stat().st_size} bytes)", flush=True)


if __name__ == "__main__":
    artifact = asyncio.run(run_phase5b())
    verdict = artifact.get("verdict")
    print(f"\n=== Phase 5B verdict: {verdict} ===", flush=True)
    sys.exit(0 if verdict == "PROTOTYPE_PASS" else 1)
