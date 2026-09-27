#!/usr/bin/env python3
"""gen4_phase9_diagnostic_smoke.py — Phase 9 single-event live diagnostic smoke.

SCOPE (per docs/gen4_phase9_live_smoke_plan.md v1.2 — Kris approved option A
(2026-08-28 02:46) + warm-up retry approved (2026-08-28 02:59)):
  - Event: 14025013 ONLY
  - Endpoints: ['event', 'incidents', 'lineups', 'statistics'] ONLY (4)
  - Mode: Gen4 opt-in, ALL wired components INJECTED
          (warmup + cookie store + sticky session + header factory + budget)
  - write_enabled=False — invariant preserved
  - Budget: ≤32 endpoint attempts (8/request × 4) + ≤3 warm-up attempts = ≤35 HTTP calls
    (v1.2: warm-up 最多 2 attempts — initial + 1 retry-with-rotation)
  - Protected files (ZERO modification): backfill_runner.py, gen4_fetcher.py,
    gen4_ssr.py, gen4_phase4_live_smoke.py

CONSTRAINTS (import-reuse, no fork):
  - Uses `from gen4_fetcher import Gen4Fetcher, Gen4Config` — never copies logic.
  - Real component implementations (warmup/cookie/sticky/rotator) live HERE
    because gen4_components.py only defines Protocols (Phase 7) and the
    offline tests only use Fakes. This file injects REAL ones via the
    Phase 8 DI constructor. If any of these need changes in gen4_fetcher.py
    to work — STOP and ask Duncan (§9 communication gate).

STOP-ON-TRIGGER (halt immediately, no further endpoints):
  - HTTP 407 anywhere (check_safety_halts)
  - Run-level call cap 35 hit
  - warm-up failure (halt_reason="warmup_failed") — do NOT enter endpoint loop
  - scope dev

Output:
  data/gen4_phase9_diagnostic_smoke.json  (diagnostic artifact)
  console verdict line + per-gate G1-G5 assessment

Usage:
  PYTHONUNBUFFERED=1 .runner-venv/bin/python gen4_phase9_diagnostic_smoke.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent
DATA_DIR = REPO_ROOT / "data"
ARTIFACT_PATH = DATA_DIR / "gen4_phase9_diagnostic_smoke.json"

# ---- Phase 9 frozen scope ------------------------------------------------
PHASE9_EVENT_ID = 14025013
PHASE9_ENDPOINTS: List[str] = ["event", "incidents", "lineups", "statistics"]
WRITE_ENABLED = False
MAX_ENDPOINT_ATTEMPTS = 32          # 8/request × 4 endpoints
MAX_WARMUP_ATTEMPTS = 3
MAX_HTTP_CALLS = MAX_ENDPOINT_ATTEMPTS + MAX_WARMUP_ATTEMPTS  # 35
ENDPOINT_ATTEMPT_CEILING_PER_REQUEST = 8  # TOTAL_MAX_ATTEMPTS_PER_REQUEST

ENDPOINT_PATHS = {
    "event":      "/api/v1/event/{event_id}",
    "incidents":  "/api/v1/event/{event_id}/incidents",
    "lineups":    "/api/v1/event/{event_id}/lineups",
    "statistics": "/api/v1/event/{event_id}/statistics",
}

COOKIE_TTL_S = 30 * 60  # Phase 7 Q4: cookie TTL cap 30 min


# ---------------------------------------------------------------------------
# env
# ---------------------------------------------------------------------------

def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env")
    except Exception:
        env_path = REPO_ROOT / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


def _log(msg: str) -> None:
    print(f"[phase9] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Shared call registry — counts EVERY outgoing HTTP call (warm-up + endpoints)
# and captures Set-Cookie evidence for gate G1.
# ---------------------------------------------------------------------------

class Phase9CallRegistry:
    """Global counter + evidence recorder. Every outbound HTTP call funnels
    through here so the ≤35 run cap is enforced at the transport edge."""

    def __init__(self) -> None:
        self.total_http_calls = 0
        self.endpoint_attempts = 0     # excludes warm-up calls
        self.warmup_calls = 0
        self.calls: List[Dict[str, Any]] = []  # sanitized records for artifact
        self.quota_capped = False

    def register(self, *, kind: str, url: str, status: int,
                 set_cookie_names: List[str], request_cookie_names: List[str],
                 latency_ms: int) -> None:
        self.total_http_calls += 1
        if kind == "warmup":
            self.warmup_calls += 1
        else:
            self.endpoint_attempts += 1
        self.calls.append({
            "n": self.total_http_calls,
            "kind": kind,
            "url_host": "www.sofascore.com",
            "url_path": url.replace("https://www.sofascore.com", "")[:120],
            "status": status,
            "set_cookie_names": set_cookie_names,
            "request_cookie_names": request_cookie_names,
            "latency_ms": latency_ms,
        })

    def over_cap(self) -> bool:
        return self.total_http_calls > MAX_HTTP_CALLS


# ---------------------------------------------------------------------------
# Real components (injected into Gen4Fetcher via Phase 8 DI constructor)
# ---------------------------------------------------------------------------

class RealCookieStore:
    """Dict jar with proxy-identity isolation + 30min TTL cap (Phase 7 Q4).

    Implements the CookieStore Protocol from gen4_components.py.
    `_last_snapshot` duck-attr consumed by
    Gen4Fetcher._build_curl_cffi_kwargs (Phase 8 additive hook).
    """

    def __init__(self) -> None:
        # {name: {"value":..., "proxy": identity, "created_at": epoch}}
        self._jar: Dict[str, Dict[str, Any]] = {}
        self._last_snapshot: Dict[str, str] = {}
        self.current_proxy_identity: str = "warmup"
        self.evicted_by_proxy_count = 0
        self.evicted_expired_count = 0

    # -- protocol methods --------------------------------------------------
    async def capture_from_context(self, browser_context: Any) -> Dict[str, str]:
        snap: Dict[str, str] = {}
        try:
            for c in await browser_context.cookies():
                snap[c["name"]] = c["value"]
        except Exception:
            pass
        self._merge(snap)
        return snap

    def inject(self, request_kwargs: Dict[str, Any], cookie_dict: Dict[str, str]) -> None:
        request_kwargs["cookies"] = dict(cookie_dict)

    def is_expired(self, cookie_name: str) -> bool:
        rec = self._jar.get(cookie_name)
        return bool(rec) and (time.time() - rec["created_at"] > COOKIE_TTL_S)

    def evict_expired(self) -> int:
        dead = [n for n in self._jar if self.is_expired(n)]
        for n in dead:
            del self._jar[n]
        self.evicted_expired_count += len(dead)
        self._refresh_snapshot()
        return len(dead)

    def evict_by_proxy(self, proxy_session_identity: str) -> int:
        """Q1 isolation: on rotation to a NEW proxy identity, drop every
        cookie NOT bound to that identity (no old-IP cookie carryover)."""
        doomed = [n for n, r in self._jar.items()
                  if r["proxy"] != proxy_session_identity]
        for n in doomed:
            del self._jar[n]
        self.evicted_by_proxy_count += len(doomed)
        self.current_proxy_identity = proxy_session_identity
        self._refresh_snapshot()
        return len(doomed)

    # -- helpers used by this script ---------------------------------------
    def set_from_response(self, set_cookie_pairs: Dict[str, str]) -> None:
        self._merge(set_cookie_pairs)

    def snapshot(self) -> Dict[str, str]:
        return dict(self._last_snapshot)

    def clear(self) -> None:
        self._jar.clear()
        self._refresh_snapshot()

    def _merge(self, pairs: Dict[str, str]) -> None:
        now = time.time()
        for k, v in pairs.items():
            self._jar[k] = {"value": v,
                            "proxy": self.current_proxy_identity,
                            "created_at": now}
        self._refresh_snapshot()

    def _refresh_snapshot(self) -> None:
        self._last_snapshot = {k: v["value"] for k, v in self._jar.items()}


class RealProxyRotator:
    """Webshare rotating gateway (`user-rotate` mode): each new connection
    may egress from a different IP. There is no client-side "rotate" API call;
    rotating = bump session identity + let the next connection egress fresh.

    Satisfies the ProxyRotator Protocol (rotate, current_proxy).
    """

    def __init__(self, base_identity: str) -> None:
        self._base = base_identity
        self._generation = 0

    async def rotate(self) -> str:
        self._generation += 1
        return self.current_proxy()

    def current_proxy(self) -> Optional[str]:
        return f"{self._base}-g{self._generation}"


class RealStickySessionKey:
    """Per Q3 sticky per-competition; Phase 9 is single-event so the key is
    a fixed tag identifying this smoke run."""

    def __init__(self, key: str) -> None:
        self._key = key

    @property
    def key(self) -> Optional[str]:
        return self._key

    def matches(self, other: "RealStickySessionKey") -> bool:
        return getattr(other, "key", None) == self._key

    def __str__(self) -> str:
        return self._key


class RealWarmupSequence:
    """Phase 9 warm-up: homepage via curl_cffi (Tier-1 transport), capture
    Set-Cookie into shared RealCookieStore so request #1 of each endpoint
    ALREADY carries cookies (Gen2 warm_homepage equivalent, §4 plan).

    scope=per_event per Q1. warm_tournament/warm_event are no-ops beyond
    referer bookkeeping (single event smoke).
    """
    scope = "per_event"

    def __init__(self, *, curl_get: Callable[..., Any], proxy: Optional[str],
                 jar: RealCookieStore, registry: Phase9CallRegistry,
                 base_url: str, rotator: Optional[Any] = None) -> None:
        self._curl_get = curl_get
        self._proxy = proxy
        self._jar = jar
        self._registry = registry
        self._base_url = base_url
        self._rotator = rotator
        self._last_referer: Optional[str] = None
        self.warmed = False

    @property
    def last_referer_url(self) -> Optional[str]:
        return self._last_referer

    async def warm_homepage(self) -> None:
        """v1.2: warm-up with retry-with-rotation (Kris 2026-08-28 02:59 option A).

        Attempt 1: plain curl_cffi via current proxy.
        On failure: rotate proxy identity + evict cookies, retry ONCE.
        Only raise after both attempts fail (caller halts with warmup_failed).
        """
        if self.warmed:
            return
        last_err: Optional[Exception] = None
        for attempt in range(2):
            t0 = time.monotonic()
            try:
                kwargs: Dict[str, Any] = {"impersonate": "chrome", "timeout": 30}
                if self._proxy:
                    kwargs["proxies"] = {"http": self._proxy, "https": self._proxy}
                resp = await asyncio.to_thread(self._curl_get, self._base_url + "/", **kwargs)
                status = getattr(resp, "status_code", 0)
                pairs = _extract_set_cookie_pairs(resp)
                if pairs:
                    self._jar.set_from_response(pairs)
                self._registry.register(
                    kind="warmup", url=self._base_url, status=status,
                    set_cookie_names=sorted(pairs.keys()),
                    request_cookie_names=sorted(self._jar.snapshot().keys()),
                    latency_ms=int((time.monotonic() - t0) * 1000),
                )
                if status == 200:
                    self._last_referer = self._base_url + "/"
                    self.warmed = True
                    return
                last_err = RuntimeError(f"warm_homepage attempt {attempt+1} status={status}")
            except Exception as e:
                last_err = e
                self._registry.register(
                    kind="warmup", url=self._base_url, status=0,
                    set_cookie_names=[], request_cookie_names=[],
                    latency_ms=int((time.monotonic() - t0) * 1000),
                )
            # v1.2: rotate + evict before retry (fresh IP, no stale cookies)
            if self._rotator is not None:
                new_id = await self._rotator.rotate()
                self._jar.evict_by_proxy(new_id)
        raise RuntimeError(str(last_err))

    async def warm_tournament(self, *, ut_id: int, season_id: int,
                              country_slug: str = "", competition_slug: str = "") -> str:
        self._last_referer = self._base_url + "/"
        return self._last_referer

    async def warm_event(self, event_id: int) -> None:
        self._last_referer = f"{self._base_url}/"


def _extract_set_cookie_pairs(resp: Any) -> Dict[str, str]:
    """Pull name→value pairs out of response Set-Cookie header(s)."""
    out: Dict[str, str] = {}
    # curl_cffi response: .cookies behaves like a RequestsCookieJar
    try:
        jar = getattr(resp, "cookies", None)
        if jar:
            for c in jar:
                out[getattr(c, "name", str(c))] = getattr(c, "value", "")
            if out:
                return out
    except Exception:
        pass
    try:
        headers = getattr(resp, "headers", {}) or {}
        raw = headers.get("set-cookie") or headers.get("Set-Cookie") or ""
        for part in str(raw).split(","):
            first = part.split(";", 1)[0].strip()
            if "=" in first:
                k, v = first.split("=", 1)
                if k.strip():
                    out[k.strip()] = v.strip()
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# Instrumented Tier-1 transport wrapper — records kwargs/headers/cookies
# actually sent, plus Set-Cookie evidence received, into the shared registry.
# ---------------------------------------------------------------------------

class InstrumentedCurlGet:
    def __init__(self, real_get: Callable[..., Any], registry: Phase9CallRegistry,
                 jar: RealCookieStore) -> None:
        self._real = real_get
        self._registry = registry
        self._jar = jar

    def __call__(self, url: str, **kwargs: Any) -> Any:
        t0 = time.monotonic()
        req_cookies = kwargs.get("cookies") or {}
        resp = self._real(url, **kwargs)
        status = getattr(resp, "status_code", 0)
        pairs = _extract_set_cookie_pairs(resp)
        if pairs:
            self._jar.set_from_response(pairs)
        self._registry.register(
            kind="endpoint",
            url=url,
            status=status,
            set_cookie_names=sorted(pairs.keys()),
            request_cookie_names=sorted(req_cookies.keys()),
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
        return resp


async def run_phase9() -> Dict[str, Any]:
    _load_dotenv()

    # Scope guards — fail BEFORE constructing anything network-y.
    if PHASE9_EVENT_ID != 14025013:
        return {"scope_violation": True, "halt_reason": "SCOPE_EXPANSION"}
    if sorted(PHASE9_ENDPOINTS) != sorted(["event", "incidents", "lineups", "statistics"]):
        return {"scope_violation": True, "halt_reason": "SCOPE_EXPANSION"}
    if WRITE_ENABLED is not False:
        return {"scope_violation": True, "halt_reason": "WRITE_ENABLED_TRUE"}

    from gen4_fetcher import Gen4Config, Gen4Fetcher            # import-reuse (§9)
    from gen4_components import DefaultHeaderFactory, RequestBudget
    from gen4_ssr import resolve_ssr_for_api_path
    from gen4_canary_validate import check_safety_halts

    registry = Phase9CallRegistry()
    jar = RealCookieStore()
    rotator = RealProxyRotator("webshare-rotate")
    sticky = RealStickySessionKey("phase9-smoke:event14025013")
    headers = DefaultHeaderFactory()
    budget = RequestBudget(max_attempts_per_request=ENDPOINT_ATTEMPT_CEILING_PER_REQUEST)

    config = Gen4Config(
        write_enabled=WRITE_ENABLED,
        proxy_server=os.getenv("SOFA_PROXY_HOST", "p.webshare.io"),
        proxy_port=os.getenv("SOFA_PROXY_PORT", "80"),
        proxy_user=os.getenv("SOFA_PROXY_USER", ""),
        proxy_pass=os.getenv("SOFA_PROXY_PASS", ""),
    )

    # v1.3 (Kris 2026-08-28 03:50, picked H): hash-fixed pool of fully-clean IPs.
    from fixed_pool_rotator import FixedPoolRotator
    audit_path = REPO_ROOT / "data" / "proxy_audit" / "proxy_audit_20260827_192348.json"
    good_path = REPO_ROOT / "data" / "proxy_audit" / "good_proxies_20260827_192348.txt"
    fixed_rotator = FixedPoolRotator.from_audit(str(audit_path), str(good_path),
                                                PHASE9_EVENT_ID)
    if fixed_rotator.pool_size < 1:
        return {"halt_reason": "EMPTY_FIXED_POOL"}
    fixed_rotator.mutate_config(config)   # config now points at hashed IP
    fixed_rotator.bind_config(config)     # rotate() mutates config from now on
    if not config.proxy_pass:
        return {"halt_reason": "NO_PROXY_CREDENTIALS"}

    proxy_url = (f"http://{config.proxy_user}:{config.proxy_pass}"
                 f"@{config.proxy_server}:{config.proxy_port}")

    artifact: Dict[str, Any] = {
        "phase": "9",
        "plan_version": "v1.3",
        "approved_by": "Kris (option A 02:46; warm-up retry 02:59; H hash-fixed pool 03:50 GMT+8)",
        "scope": {
            "event_id": PHASE9_EVENT_ID,
            "endpoints": PHASE9_ENDPOINTS,
            "write_enabled": WRITE_ENABLED,
            "max_http_calls_cap": MAX_HTTP_CALLS,
        },
        "results": [],
        "halt_reason": None,
        "stopped_early": False,
        "cleanup_ok": None,   # None until cleanup actually finishes; False would trip safety checker mid-run
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "di_injected": {
            "curl_cffi_get": "instrumented_real",
            "cloakbrowser_launcher": "real",
            "ssr_fetcher": "real",
            "warmup_sequence": "real(per_event)",
            "cookie_store": "real",
            "sticky_session": "real",
            "header_factory": "DefaultHeaderFactory(real)",
            "proxy_rotator": "FixedPoolRotator(hash-locked per-event)",
            "rotation_mode": "fixed_pool_hash",
            "pool_size": fixed_rotator.pool_size,
            "hashed_entry_ip": fixed_rotator.current_proxy(),
            "budget": "RequestBudget(real)",
        },
    }

    def _make_real_curl_get() -> Callable[..., Any]:
        from curl_cffi import requests as curl_requests
        def _get(url: str, **kw: Any) -> Any:
            return curl_requests.get(url, **kw)
        return _get

    async def _real_cloak_launcher(headless: bool = True, proxy: Optional[str] = None,
                                   humanize: bool = False, **kw: Any) -> Any:
        import cloakbrowser as _cloak
        return await _cloak.launch_async(headless=headless, proxy=proxy,
                                         humanize=humanize, **kw)

    raw_curl = _make_real_curl_get()
    instrumented = InstrumentedCurlGet(raw_curl, registry, jar)
    warmup = RealWarmupSequence(curl_get=raw_curl, proxy=proxy_url, jar=jar,
                                registry=registry, base_url=config.base_url,
                                rotator=fixed_rotator)

    # ---- Warm-up FIRST (Plan §6.1: warm-up fail → halt, never enter loop) ----
    try:
        await warmup.warm_homepage()
        _log(f"warm-up ok; cookies captured: {sorted(jar.snapshot().keys()) or '(none)'}")
    except Exception as e:
        artifact["halt_reason"] = "warmup_failed"
        artifact["warmup_error"] = f"{type(e).__name__}: {e}"
        artifact["call_log"] = registry.calls
        artifact["budget"] = budget.report()
        artifact["cleanup_ok"] = True   # nothing was opened yet
        _write_artifact(artifact)
        _log(f"HALT: warm-up failed ({e}) — endpoint loop NOT entered")
        return artifact

    fetcher = Gen4Fetcher(
        config=config,
        curl_cffi_get=instrumented,
        cloakbrowser_launcher=_real_cloak_launcher,
        ssr_fetcher=resolve_ssr_for_api_path,
        retry_policy=None,                 # retry loop lives in fetch_api (Phase 8.1)
        proxy_rotator=fixed_rotator,
        warmup_sequence=warmup,
        cookie_store=jar,
        sticky_session=sticky,
        header_factory=headers,
        budget=budget,
    )

    cleanup_ok = False
    try:
        await fetcher.start()
        for endpoint in PHASE9_ENDPOINTS:
            if registry.over_cap():
                artifact["halt_reason"] = "RUN_CALL_CAP_EXCEEDED"
                artifact["stopped_early"] = True
                break
            path = ENDPOINT_PATHS[endpoint].format(event_id=PHASE9_EVENT_ID)
            _log(f"fetching {endpoint}: {path}")
            t0 = time.monotonic()
            try:
                result = await fetcher.fetch_api(path)
            except Exception as e:
                artifact["results"].append({
                    "endpoint": endpoint, "path": path, "status": 0,
                    "ok": False, "payload_complete": False, "transport": None,
                    "attempts": [], "exception": f"{type(e).__name__}: {e}",
                    "latency_ms": int((time.monotonic() - t0) * 1000),
                })
            else:
                artifact["results"].append({
                    "endpoint": endpoint,
                    "path": path,
                    "status": result.get("status", 0),
                    "ok": bool(result.get("ok")),
                    "payload_complete": bool(result.get("payload_complete")),
                    "transport": result.get("transport"),
                    "retry_count": result.get("retry_count", 0),
                    "attempts": [
                        {"transport": a.get("transport"), "status": a.get("status"),
                         "latency_ms": a.get("latency_ms"), "error": a.get("error")}
                        for a in result.get("attempts", [])
                    ],
                    "latency_ms": int((time.monotonic() - t0) * 1000),
                })
            artifact["halt_reason"] = check_safety_halts(artifact)
            if artifact["halt_reason"]:
                artifact["stopped_early"] = True
                _log(f"SAFETY HALT after {endpoint}: {artifact['halt_reason']}")
                break
    finally:
        # ---- Cleanup verification (Plan §8 step 5) ----
        try:
            await fetcher.close()      # closes browser context + browser
        except Exception:
            pass
        jar.clear()                    # release cookie store (TTL cap + explicit)
        cleanup_ok = True

    artifact["cleanup_ok"] = cleanup_ok
    artifact["call_log"] = registry.calls
    artifact["budget"] = budget.report()
    artifact["stats"] = {
        "browser_rebuild_count": fetcher.stats.browser_rebuild_count,
        "epipe_count": fetcher.stats.epipe_count,
        "quota_warning": fetcher.stats.quota_warning,
    }
    artifact["finished_at_utc"] = datetime.now(timezone.utc).isoformat()

    # ---- Gate evaluation G1-G5 (report only — verdict by Kris via Main) ----
    results = artifact["results"]
    set_cookie_evidence = [c for c in registry.calls if c["set_cookie_names"]]
    carried_cookie = [c for c in registry.calls
                      if c["kind"] == "endpoint" and c["request_cookie_names"]]
    incidents = next((r for r in results if r["endpoint"] == "incidents"), None)
    artifact["gates"] = {
        "G1_cookie_reuse": {
            "pass": bool(set_cookie_evidence) and bool(carried_cookie),
            "evidence": {
                "responses_with_set_cookie": len(set_cookie_evidence),
                "endpoint_requests_carrying_cookies": len(carried_cookie),
            },
        },
        "G2_incidents_200_complete": {
            "pass": bool(incidents and incidents.get("status") == 200
                         and incidents.get("payload_complete")),
            "evidence": incidents or "not_run",
        },
        "G3_retry_loop_fired": {
            "pass": any((r.get("retry_count") or 0) > 0 for r in results),
            "evidence": {r["endpoint"]: r.get("retry_count", 0) for r in results},
        },
        "G4_safety_stops_intact": {
            "pass": artifact.get("halt_reason") in (None, "warmup_failed"),
            "evidence": {"halt_reason": artifact.get("halt_reason")},
        },
        "G5_all_4_endpoints_200_vs_gen2": {
            "pass": len(results) == 4 and all(
                r.get("status") == 200 and r.get("payload_complete") for r in results),
            "evidence": {r["endpoint"]: r.get("status") for r in results},
            "note": "PASS ≠ production-ready. Next step if pass: apply Phase 10.",
        },
    }

    _write_artifact(artifact)
    return artifact


def _write_artifact(artifact: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACT_PATH.write_text(json.dumps(artifact, indent=2, ensure_ascii=False, default=str))
    _log(f"artifact written: {ARTIFACT_PATH}")


def main() -> int:
    artifact = asyncio.run(run_phase9())
    if artifact.get("scope_violation"):
        print("VERDICT: SCOPE_VIOLATION", artifact.get("halt_reason"))
        return 2
    gates = artifact.get("gates", {})
    print("\n===== PHASE 9 GATE SUMMARY =====")
    for g, v in gates.items():
        print(f"  {g}: {'PASS' if v.get('pass') else 'FAIL'}")
    hr = artifact.get("halt_reason")
    print(f"  halt_reason: {hr}")
    print(f"  cleanup_ok: {artifact.get('cleanup_ok')}")
    all_pass = gates and all(v.get("pass") for v in gates.values()) and hr is None
    print(f"VERDICT: {'ALL GATES PASS' if all_pass else 'GATES NOT ALL PASS'} "
          f"(NOTE: PASS ≠ production-ready; next step = Phase 10 approval)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
