#!/usr/bin/env python3
"""
gen4_fetcher.py — Gen4 fetch strategy prototype.

Per Kris 23:30 Gen4 implementation prompt §3 / §17:
  - Independent strategy: gen4_curl_then_cloak_then_ssr
  - Opt-in via FETCH_STRATEGY=gen4; default remains FETCH_STRATEGY=gen2
  - write_enabled=False invariant (no MySQL, no DataInserter, no insert_*)
  - 3-tier fallback: curl_cffi -> CloakBrowser -> SSR
  - Response contract per spec §7

This module is the prototype skeleton. Live curl_cffi / CloakBrowser / SSR
calls are deferred until offline tests in tests/test_gen4_fetch_strategy.py
all pass (Phase 3 static gate per spec §12.3).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# Phase 3: runtime import of the typed exception so `_fetch_via_ssr` can
# `except SSRHelperMissing` and surface it as `ssr_helper_missing` (a
# deterministic error class for the artifact) rather than bundling it
# into the generic `ssr failed: ...` branch.
#
# gen4_ssr is lazy about importing playwright (Playwright is only loaded
# when `load_event_ssr` is actually called), so this top-level import is
# safe at module load time.
from gen4_ssr import SSRHelperMissing
from gen4_components import (
    TIER1_MAX_RETRIES,
    TIER2_BROWSER_MAX_RETRIES,
    TOTAL_MAX_ATTEMPTS_PER_REQUEST,
)

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

# ---------------------------------------------------------------------------
# Strategy / feature flag (§10)
# ---------------------------------------------------------------------------

GEN2_DEFAULT = "gen2"
GEN4_STRATEGY = "gen4"


def resolve_fetch_strategy() -> str:
    """Resolve fetch strategy from FETCH_STRATEGY env var.

    Per spec §10: default MUST stay gen2. Gen4 is opt-in only.
    Unsupported values raise (do NOT silently fall back).
    """
    strategy = os.getenv("FETCH_STRATEGY", GEN2_DEFAULT).strip().lower()
    if strategy not in (GEN2_DEFAULT, GEN4_STRATEGY):
        raise ValueError(
            f"Unsupported FETCH_STRATEGY={strategy!r}. "
            f"Allowed: {GEN2_DEFAULT!r} or {GEN4_STRATEGY!r}."
        )
    return strategy


# ---------------------------------------------------------------------------
# Response contract (§7)
# ---------------------------------------------------------------------------


def _make_attempt(transport: str, status: int, latency_ms: int, error: Optional[str] = None) -> Dict[str, Any]:
    return {
        "transport": transport,
        "status": status,
        "latency_ms": latency_ms,
        "error": error,
    }


def _success_result(*, status: int, data: Any, transport: str, attempts: List[Dict[str, Any]],
                    retry_count: int, payload_complete: bool,
                    halt_reason: Optional[str] = None) -> Dict[str, Any]:
    return {
        "ok": True,
        "status": status,
        "data": data,
        "transport": transport,
        "attempts": attempts,
        "fallback_used": any(a["transport"] != attempts[0]["transport"] for a in attempts) if attempts else False,
        "retry_count": retry_count,
        "payload_complete": payload_complete,
        "error_class": None,
        "halt_reason": halt_reason,
    }


def _failure_result(*, status: int, transport: str, attempts: List[Dict[str, Any]],
                    retry_count: int, error_class: str,
                    halt_reason: Optional[str] = None) -> Dict[str, Any]:
    # Per §7: fallback_used reflects whether a fallback tier was actually
    # invoked. A terminal halt (e.g. 407) records only the curl_cffi attempt,
    # so fallback_used must be False — callers rely on this to distinguish
    # "halted at first tier" from "exhausted all tiers".
    fallback_used = len(attempts) > 1
    return {
        "ok": False,
        "status": status,
        "data": None,
        "transport": transport,
        "attempts": attempts,
        "fallback_used": fallback_used,
        "retry_count": retry_count,
        "payload_complete": False,
        "error_class": error_class,
        "halt_reason": halt_reason,
    }


# ---------------------------------------------------------------------------
# Payload validation (§8)
# ---------------------------------------------------------------------------

# Endpoint-specific minimum keys for payload_complete=True (§8).
ENDPOINT_MIN_KEYS: Dict[str, Tuple[str, ...]] = {
    # Per spec §8 + Phase 4 v1 fix (Kris 02:14 GMT+8): root /api/v1/event/{id}
    # payloads are real event objects keyed under "id" — Sofascore does NOT
    # return top-level "home"/"away" for a single-event fetch; teams sit
    # inside "homeTeam"/"awayTeam" or nested under "event". Requiring
    # ("id", "home", "away") rejects valid payloads, so event uses just
    # ("id",). Sub-endpoints keep their own keys.
    "event": ("id",),
    "incidents": ("incidents",),
    "lineups": ("home", "away"),
    "shotmap": ("shotmap",),
    "graph": ("graph",),
    "statistics": ("statistics",),
    "comments": ("comments",),
    "votes": ("votes",),
    "managers": ("home", "away"),
    "pregame-form": ("pregameForm",),
    "average-positions": ("averagePositions",),
    "web-odds": ("odds",),
    "highlights": ("highlights",),
    "featured-players": ("featuredPlayers",),
    "achievements": ("achievements",),
}


def classify_error(status: int, body: Any, error: Optional[str]) -> str:
    if error and "timeout" in error.lower():
        return "timeout"
    if error and "json" in error.lower():
        return "invalid_json"
    if error and ("browser" in error.lower() or "epipe" in error.lower() or "target" in error.lower()):
        return "browser_error"
    if status == 403:
        return "http_403"
    if status == 404:
        return "http_404"
    if status == 407:
        return "http_407"
    return "unknown"


def validate_payload(path: str, body: Any) -> Tuple[bool, str]:
    """Validate per §8. Returns (is_complete, error_class).

    Per Kris 02:14 GMT+8 Phase 4 v1 fix: root /api/v1/event/{id} payloads are
    real event objects keyed under "id" — Sofascore does NOT return top-level
    "home"/"away" for a single-event fetch; teams sit inside "homeTeam"/
    "awayTeam" or nested under "event". Requiring ("id", "home", "away")
    rejects valid payloads, so event uses just ("id",). Sub-endpoints keep
    their own keys.

    For root event payloads, the body may wrap the event in {"event": {...}}
    — the endpoint check descends into that wrapper when present.
    """
    if body is None:
        return (False, "invalid_json")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except (TypeError, json.JSONDecodeError):
            return (False, "invalid_json")
    if not isinstance(body, (dict, list)) or (isinstance(body, (dict, list)) and len(body) == 0):
        return (False, "success_empty")
    # Endpoint-specific minimum keys
    ep_match = re.search(r"/event/[^/]+/([^/?]+)", path or "")
    endpoint = ep_match.group(1) if ep_match else ("event" if "/event/" in (path or "") else None)
    if endpoint and endpoint in ENDPOINT_MIN_KEYS:
        needed = ENDPOINT_MIN_KEYS[endpoint]
        if isinstance(body, dict):
            # Phase 4 v1 fix: if the body wraps the event object under an
            # "event" key, descend into it so {"event": {"id": ...}} passes
            # the root /api/v1/event/{id} minimum-key check.
            check_body = body.get("event", body) if isinstance(body.get("event"), dict) else body
            if not any(k in check_body for k in needed):
                return (False, "success_empty")
        # If body is a list (e.g. incidents list), any non-empty counts as complete
    return (True, "success_complete")


# ---------------------------------------------------------------------------
# Credential redaction (§15)
# ---------------------------------------------------------------------------

_CREDENTIAL_PATTERNS = [
    re.compile(r"://[^/\s\"']+:[^@\s/\"']+@"),
    re.compile(r"SOFA_PROXY_PASS=[^\s]+"),
    re.compile(r"dztr57tcycoz"),
    re.compile(r"***REMOVED***-rotate:[^@\s/\"']+@"),
]


def redact_credentials(text: str) -> str:
    if not isinstance(text, str):
        return text
    out = text
    for pat in _CREDENTIAL_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out


# ---------------------------------------------------------------------------
# SSR mapping (§9) — _map_ssr_to_api_format
# ---------------------------------------------------------------------------
#
# Phase 2 offline: real implementation. Maps `__NEXT_DATA__` JSON to the
# equivalent /api/v1/event/{eid}/{endpoint} response payload so the SSR tier
# can serve event data without a browser fetch on the API endpoint itself.
#
# Contract:
#   - Input: raw_next_data (the parsed JSON from #__NEXT_DATA__) + api_path
#     (the same path that was requested, e.g. "/api/v1/event/{eid}/lineups")
#   - Output: a dict in the shape Sofascore's API would return, OR
#     None when the endpoint is structurally NOT available in SSR.
#
# Endpoints supported by SSR (return a real payload):
#   - "event" (root /api/v1/event/{eid}) — wraps pageProps.event under
#     {"event": ...} so it matches what the curl_cffi tier returns.
#
# Endpoints NOT supported by SSR (return None; resolver classifies as
# ssr_unsupported_endpoint):
#   - "incidents", "lineups", "statistics", "shotmap", "comments",
#     "graph", "votes", "managers", "pregame-form", "average-positions",
#     "web-odds", "highlights", "featured-players", "achievements"
#
# Rationale (verified against data/debug_ssr_raw.json):
#   pageProps keys: ['event', 'eventMeta', 'initialHasLineups', 'revalidate',
#   'initialStandingsProperties', '_sentryTraceData', '_sentryBaggage']
#   pageProps.event is a 26-key object without 'incidents' or 'lineups'.
#   'initialHasLineups' is a boolean — lineups live on a separate request.

# Endpoints that the SSR payload structurally CANNOT serve.
#
# POLICY (Phase 3, approved by Kris 2026-08-24 21:55 GMT+8):
#   `lineups` and `incidents` are PERMANENTLY in this set. The Sofascore
#   SSR pageProps contains only `initialHasLineups` (a boolean indicating
#   whether lineups exist) — NOT the actual lineup or incident data.
#   Verified against data/debug_ssr_raw.json.
#   Any change to this policy requires explicit Kris approval and a
#   re-verification against a fresh SSR fixture.
#
# Keep in sync with SSR_UNSUPPORTED_ENDPOINTS in gen4_canary_validate.py.
SSR_UNSUPPORTED_ENDPOINTS: frozenset = frozenset({
    # --- Permanently unsupported (SSR has no payload for these) ---
    "incidents", "lineups",
    # --- Unsubscribe-supported (SSR has no payload for these either) ---
    "statistics", "shotmap", "comments",
    "graph", "votes", "managers", "pregame-form", "average-positions",
    "web-odds", "highlights", "featured-players", "achievements",
})


def _parse_event_id_from_path(path: str) -> Optional[int]:
    """Extract event_id from /api/v1/event/{eid}[/endpoint]."""
    if not path:
        return None
    m = re.search(r"/event/(\d+)", path)
    if not m:
        return None
    try:
        return int(m.group(1))
    except (TypeError, ValueError):
        return None


def _parse_endpoint_from_path(path: str) -> Optional[str]:
    """Extract sub-endpoint name (e.g. 'lineups', 'incidents') from path.

    Returns 'event' for /api/v1/event/{eid}, the sub-endpoint name for
    /api/v1/event/{eid}/{sub}, and None if the path doesn't match the
    /event/{eid}... shape.
    """
    if not path:
        return None
    m = re.search(r"/event/(\d+)(?:/([^/?]+))?", path)
    if not m:
        return None
    sub = m.group(2)
    return sub if sub else "event"


def _extract_page_props(raw_next_data: Any) -> Optional[Dict[str, Any]]:
    """Walk the Next.js data envelope to find pageProps.

    Accepts:
      - the raw __NEXT_DATA__ JSON (top-level keys: props, page, ...)
      - already-extracted props.pageProps dict
      - already-extracted pageProps dict (legacy fallback)
    Returns pageProps dict or None if not found.
    """
    if raw_next_data is None:
        return None
    if isinstance(raw_next_data, str):
        try:
            raw_next_data = json.loads(raw_next_data)
        except (TypeError, json.JSONDecodeError):
            return None
    if not isinstance(raw_next_data, dict):
        return None
    # Standard Next.js envelope: {props: {pageProps: {...}}, ...}
    props = raw_next_data.get("props") if isinstance(raw_next_data.get("props"), dict) else None
    if props is not None:
        page_props = props.get("pageProps")
        if isinstance(page_props, dict):
            return page_props
    # Legacy / flat shape: {pageProps: {...}}
    page_props = raw_next_data.get("pageProps")
    if isinstance(page_props, dict):
        return page_props
    return None


def _map_ssr_to_api_format(raw_next_data: Any, path: str) -> Optional[Dict[str, Any]]:
    """Map a `__NEXT_DATA__` JSON payload to the equivalent API response.

    Phase 2 offline implementation. Returns:
      - dict payload matching /api/v1/event/{eid}[/endpoint] shape, OR
      - None if the requested endpoint is structurally unsupported by SSR.

    For the root /event/{eid} endpoint the payload wraps pageProps.event
    under {"event": ...} so downstream `validate_payload` accepts it.

    For sub-endpoints in SSR_UNSUPPORTED_ENDPOINTS the function returns
    None and the caller (resolver) MUST surface that as
    `ssr_unsupported_endpoint`, not as a silent missing field.
    """
    if raw_next_data is None or not path:
        return None
    endpoint = _parse_endpoint_from_path(path)
    if endpoint is None:
        return None
    if endpoint in SSR_UNSUPPORTED_ENDPOINTS:
        # Structural gap — caller is expected to map None -> ssr_unsupported_endpoint.
        return None
    # Only 'event' is supported at SSR level today.
    if endpoint != "event":
        # Defensive: unknown endpoint that wasn't in the unsupported set.
        return None
    page_props = _extract_page_props(raw_next_data)
    if page_props is None:
        return None
    event = page_props.get("event")
    if not isinstance(event, dict):
        return None
    # The Sofascore API returns {"event": {...}} for /api/v1/event/{eid}.
    # pageProps.event already has id, homeTeam, awayTeam, status, etc.
    return {"event": event}



# ---------------------------------------------------------------------------
# Gen4Fetcher (§3)
# ---------------------------------------------------------------------------


@dataclass
class Gen4Config:
    base_url: str = "https://www.sofascore.com"
    proxy_server: str = os.getenv("SOFA_PROXY_HOST", "p.webshare.io")
    proxy_port: str = os.getenv("SOFA_PROXY_PORT", "80")
    proxy_user: str = os.getenv("SOFA_PROXY_USER", "***REMOVED***-rotate")
    proxy_pass: str = os.getenv("SOFA_PROXY_PASS", "")
    request_timeout_s: int = 30
    browser_timeout_ms: int = 45000
    max_browser_rebuilds: int = 1
    write_enabled: bool = False


@dataclass
class Gen4Stats:
    attempts: List[Dict[str, Any]] = field(default_factory=list)
    browser_rebuild_count: int = 0
    epipe_count: int = 0
    quota_warning: bool = False


class Gen4Fetcher:
    """Three-tier fetch strategy: curl_cffi -> CloakBrowser -> SSR.

    Phase 8 (Kris 2026-08-25 09:49) ADDS optional DI for new components:
      - retry_policy: if provided, fetch_api runs Tier-1/Tier-2 with
        bounded retry + rotation via decide_retry state-machine
      - proxy_rotator: if provided, used to rotate proxies between attempts
      - warmup_sequence: if provided, called once before first request
      - cookie_store: if provided, cookies captured + injected per request
      - sticky_session: if provided, session key pinning (per-competition)
      - header_factory: if provided, request headers built from UA + Referer
      - budget: if provided, local retry/quota budget guard

    BACKWARD COMPAT: all new fields default None. Existing 4-arg signature
    (config, curl_cffi_get, cloakbrowser_launcher, ssr_fetcher) UNCHANGED.
    Behavior with no new components = identical to Phase 7 Gen4Fetcher.
    """

    def __init__(self, config: Optional[Gen4Config] = None,
                 curl_cffi_get: Optional[Callable[..., Any]] = None,
                 cloakbrowser_launcher: Optional[Callable[..., Any]] = None,
                 ssr_fetcher: Optional[Callable[[str], Optional[dict]]] = None,
                 # Phase 8 additive Optional DI (all default None for back-compat)
                 retry_policy: Optional[Any] = None,
                 proxy_rotator: Optional[Any] = None,
                 warmup_sequence: Optional[Any] = None,
                 cookie_store: Optional[Any] = None,
                 sticky_session: Optional[Any] = None,
                 header_factory: Optional[Any] = None,
                 budget: Optional[Any] = None):
        self.config = config or Gen4Config()
        self.stats = Gen4Stats()
        self._started = False
        # Injectable for tests; production wires real callables.
        self._curl_cffi_get = curl_cffi_get
        self._cloakbrowser_launcher = cloakbrowser_launcher
        self._ssr_fetcher = ssr_fetcher
        self._browser = None
        self._context = None
        # Phase 8 optional DI (all default None → behavior unchanged)
        self._retry_policy = retry_policy
        self._proxy_rotator = proxy_rotator
        self._warmup_sequence = warmup_sequence
        self._cookie_store = cookie_store
        self._sticky_session = sticky_session
        self._header_factory = header_factory
        self._budget = budget
        # Warm-up gate state (Q1)
        self._warmed_up = False
        # Current UA + Referer (used by HeaderFactory if injected)
        self._current_ua: Optional[str] = None
        self._current_referer: Optional[str] = None

    # -- write gate (invariant) --------------------------------------------
    def _assert_no_write(self) -> None:
        if self.config.write_enabled:
            raise RuntimeError(
                "Gen4Fetcher invariant violated: write_enabled=True. "
                "Prototype must remain read-only."
            )

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        self._assert_no_write()
        self._started = True

    async def close(self) -> None:
        """Always close / cleanup. Spec §5 lifecycle + §12.2 case 15."""
        try:
            if self._context is not None:
                try:
                    await self._context.close()
                except Exception:
                    pass
                self._context = None
            if self._browser is not None:
                try:
                    await self._browser.close()
                except Exception:
                    pass
                self._browser = None
        finally:
            self._started = False

    async def __aenter__(self) -> "Gen4Fetcher":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    # -- proxy config ------------------------------------------------------
    def proxy_url(self) -> Optional[str]:
        if not self.config.proxy_server or not self.config.proxy_pass:
            return None
        return (
            f"http://{self.config.proxy_user}:{self.config.proxy_pass}"
            f"@{self.config.proxy_server}:{self.config.proxy_port}"
        )

    # -- browser rebuild (spec §5) -----------------------------------------
    async def rebuild_browser(self) -> None:
        if self.stats.browser_rebuild_count >= self.config.max_browser_rebuilds:
            return  # limit reached; SSR will take over
        try:
            if self._context is not None:
                try:
                    await self._context.close()
                except Exception:
                    pass
                self._context = None
            if self._browser is not None:
                try:
                    await self._browser.close()
                except Exception:
                    pass
                self._browser = None
        except Exception:
            pass
        self.stats.browser_rebuild_count += 1

    def _is_target_closed_error(self, exc: Exception) -> bool:
        msg = str(exc)
        return (
            "Target page, context or browser has been closed" in msg
            or "Target closed" in msg
            or "Browser has been closed" in msg
            or "EPIPE" in msg
        )

    # -- Tier 1: curl_cffi -------------------------------------------------
    async def _fetch_via_curl_cffi(self, path: str, timeout_ms: int) -> Dict[str, Any]:
        t0 = time.monotonic()
        try:
            if self._curl_cffi_get is None:
                return {"attempt": _make_attempt("curl_cffi", 0, int((time.monotonic() - t0) * 1000), "curl_cffi_unavailable"),
                        "body": None}
            url = f"{self.config.base_url}{path}"
            proxy = self.proxy_url()
            # Phase 8: build request kwargs via HeaderFactory + CookieStore if injected.
            # If neither injected, request_kwargs is identical to Phase 7.
            request_kwargs = self._build_curl_cffi_kwargs(url, proxy, timeout_ms)
            response = await asyncio.to_thread(
                self._curl_cffi_get,
                url,
                **request_kwargs,
            )
            status = getattr(response, "status_code", 0)
            try:
                body = response.json()
                error = None
            except Exception as json_error:
                body = getattr(response, "text", None)
                error = f"json parse failed: {json_error}"
        except Exception as request_error:
            return {"attempt": _make_attempt("curl_cffi", 0, int((time.monotonic() - t0) * 1000),
                                              f"curl_cffi request failed: {request_error}"),
                    "body": None}
        return {"attempt": _make_attempt("curl_cffi", status, int((time.monotonic() - t0) * 1000), error),
                "body": body}

    def _build_curl_cffi_kwargs(
        self,
        url: str,
        proxy: Optional[str],
        timeout_ms: int,
    ) -> Dict[str, Any]:
        """Build kwargs for self._curl_cffi_get call.

        Phase 8 (additive): if HeaderFactory + CookieStore are injected,
        emit Gen2-baseline headers (Origin/Referer/Sec-Fetch-/UA) + cookies.
        Otherwise returns minimal kwargs (Phase 7 behavior preserved).
        """
        kwargs: Dict[str, Any] = {
            "proxies": {"http": proxy, "https": proxy} if proxy else None,
            "impersonate": "chrome",
            "timeout": max(1, timeout_ms / 1000),
        }
        # Phase 8 optional: HeaderFactory (Q5 backward-compat: no behavior change
        # if not injected).
        if self._header_factory is not None:
            ua = self._current_ua or "Mozilla/5.0"
            referer = self._current_referer or f"{self.config.base_url}/"
            headers = self._header_factory.headers_for(
                user_agent=ua,
                referer=referer,
                origin=self.config.base_url,
            )
            kwargs["headers"] = headers
        if self._cookie_store is not None:
            # Phase 8: capture cookies from cookie store snapshot if set.
            # Real Playwright context integration deferred to Phase 9+.
            snapshot = getattr(self._cookie_store, "_last_snapshot", None)
            if snapshot is not None and isinstance(snapshot, dict):
                cookies: Dict[str, str] = dict(snapshot)
                self._cookie_store.inject(kwargs, cookies)
                kwargs["cookies"] = cookies
        return kwargs

    async def _ensure_warmed(self, path: str) -> None:
        """Phase 8 (Q1): warm-up gate before first request.

        If warmup_sequence is injected, call warm_homepage. Capture UA +
        Referer for HeaderFactory use. Graceful degradation: if warmup fails,
        log + continue (no exception to caller).
        """
        if self._warmed_up or self._warmup_sequence is None:
            return
        try:
            await self._warmup_sequence.warm_homepage()
            try:
                from anti_block import get_random_ua
                self._current_ua = get_random_ua()
            except ImportError:
                self._current_ua = "Mozilla/5.0"
            self._current_referer = f"{self.config.base_url}/"
            self._warmed_up = True
        except Exception:
            # Graceful degradation: caller still gets a valid Referer
            self._current_referer = f"{self.config.base_url}/"
            self._warmed_up = True  # don't retry warm-up

    async def _rotate_proxy_with_cookie_eviction(self) -> None:
        """Phase 8 (Q1 + Q6): rotate proxy + evict cookies from previous proxy.

        Called between attempts. If proxy_rotator is None, no-op.
        If cookie_store is None, still rotate but skip eviction.
        """
        if self._proxy_rotator is None:
            return
        try:
            await self._proxy_rotator.rotate()
            if self._cookie_store is not None:
                new_proxy = self._proxy_rotator.current_proxy() or "unknown"
                self._cookie_store.evict_by_proxy(new_proxy)
                if self._budget is not None:
                    self._budget.cookies_evicted += 1
        except Exception:
            # graceful: rotation failure shouldn't crash request
            pass

    async def _try_rotate(self) -> None:
        """Phase 8.1 (Kris 2026-08-25 12:02): rotation helper. Calls
        _rotate_proxy_with_cookie_eviction. Kept as a thin alias for
        backward-compat with any test that may reference _try_rotate.
        """
        await self._rotate_proxy_with_cookie_eviction()

    async def _fetch_with_retry_tier(
        self,
        tier_name: str,
        path: str,
        timeout_ms: int,
        tier_max_retries: int,
    ) -> Dict[str, Any]:
        """Phase 8.1 retry-with-rotation loop for Tier-1 (curl_cffi) or
        Tier-2 (cloakbrowser).

        Returns dict with:
          - attempts: List of attempt records
          - final_attempt: last attempt dict
          - success: bool (True if any attempt returned 200 + valid payload)
          - body: response body on success, else None
          - transport: transport name on success, else tier_name
          - fallback_to_next_tier: bool (True if all attempts exhausted)

        Invariants (LOCKED Kris 2026-08-25 09:49):
          - 407 on first attempt → TERMINAL_HALT, return immediately with
            fallback_to_next_tier=False (caller must halt entirely)
          - 404 → ENDPOINT_REVIEW, no retry, fall to next tier
          - 403 → ROTATE_AND_RETRY, rotate proxy + retry up to tier_max_retries
          - timeout/conn-error → TRANSIENT_RETRY, max 2 retries
          - 200 + invalid → VALIDATE_THEN_RETRY, 1 retry then fall
          - Total attempts across ALL tiers capped at TOTAL_MAX_ATTEMPTS_PER_REQUEST=8
          - Per-tier attempt count = tier_max_retries (1-based contract)
          - Cookie eviction on proxy rotation (Q1 isolation)
          - Budget tracking (Q7) records every attempt
        """
        attempts: List[Dict[str, Any]] = []
        attempts_used = 0  # 1-based: count of COMPLETED attempts on this tier
        last_attempt: Dict[str, Any] = {}
        last_status = 0
        last_exception: Optional[str] = None
        last_body: Any = None
        last_error: Optional[str] = None
        last_transport: str = tier_name

        # Tier-1 first attempt is BEFORE the loop so we can check 407 terminal halt
        # immediately (per §17: 407 must NEVER trigger Tier-2 fallback).
        if tier_name == "curl_cffi":
            result = await self._fetch_via_curl_cffi(path, timeout_ms)
        elif tier_name == "cloakbrowser":
            result = await self._fetch_via_cloakbrowser(path, timeout_ms)
        else:
            raise ValueError(f"unknown tier_name: {tier_name}")
        last_attempt = result["attempt"]
        attempts.append(last_attempt)
        attempts_used += 1
        last_status = last_attempt.get("status", 0)
        last_exception = last_attempt.get("exception")
        last_body = result.get("body")
        last_error = last_attempt.get("error")
        last_transport = tier_name
        # Budget: record attempt
        if self._budget is not None:
            self._budget.record_attempt(
                status=last_status,
                is_timeout=last_status == 0 and last_exception is not None,
            )

        # Immediate terminal halt on 407 (Kris 2026-08-25 09:49 preserved)
        if last_status == 407:
            return {
                "attempts": attempts,
                "final_attempt": last_attempt,
                "success": False,
                "body": None,
                "transport": tier_name,
                "fallback_to_next_tier": False,
                "halt_reason": "http_407_safety_halt",
            }

        # Validate 200 payload
        if last_status == 200 and last_body is not None:
            try:
                complete, _ = validate_payload(path, last_body)
            except Exception:
                complete = False
            if complete:
                return {
                    "attempts": attempts,
                    "final_attempt": last_attempt,
                    "success": True,
                    "body": last_body,
                    "transport": tier_name,
                    "fallback_to_next_tier": False,
                    "halt_reason": None,
                }

        # Bounded retry loop with rotation between attempts
        while attempts_used < tier_max_retries:
            # Classify and decide whether to retry
            from gen4_components import (
                classify_attempt,
                decide_retry,
                RetryClassification,
            )
            body_valid: Optional[bool] = None
            if last_status == 200:
                try:
                    body_valid, _ = validate_payload(path, last_body) if last_body is not None else (False, "")
                except Exception:
                    body_valid = False
            classification = classify_attempt(
                status=last_status,
                body_valid=body_valid,
                exception=last_exception,
            )
            decision = decide_retry(
                classification,
                attempts_used=attempts_used,  # 1-based contract
                tier_max_retries=tier_max_retries,
                total_attempts_used=attempts_used,  # per-tier counter (caller handles cross-tier via separate fetch_api ceiling)
            )
            if not decision.should_retry:
                # 404, SUCCESS already handled above, or terminal halt
                break

            # Rotate proxy + evict cookies before next attempt
            await self._try_rotate()

            # Make next attempt
            if tier_name == "curl_cffi":
                result = await self._fetch_via_curl_cffi(path, timeout_ms)
            else:
                result = await self._fetch_via_cloakbrowser(path, timeout_ms)
            last_attempt = result["attempt"]
            attempts.append(last_attempt)
            attempts_used += 1
            last_status = last_attempt.get("status", 0)
            last_exception = last_attempt.get("exception")
            last_body = result.get("body")
            last_error = last_attempt.get("error")
            # Budget: record attempt
            if self._budget is not None:
                self._budget.record_attempt(
                    status=last_status,
                    is_timeout=last_status == 0 and last_exception is not None,
                )
            # Immediate 407 halt in retry loop
            if last_status == 407:
                return {
                    "attempts": attempts,
                    "final_attempt": last_attempt,
                    "success": False,
                    "body": None,
                    "transport": tier_name,
                    "fallback_to_next_tier": False,
                    "halt_reason": "http_407_safety_halt",
                }
            # Validate 200 on retry
            if last_status == 200 and last_body is not None:
                try:
                    complete, _ = validate_payload(path, last_body)
                except Exception:
                    complete = False
                if complete:
                    return {
                        "attempts": attempts,
                        "final_attempt": last_attempt,
                        "success": True,
                        "body": last_body,
                        "transport": tier_name,
                        "fallback_to_next_tier": False,
                        "halt_reason": None,
                    }

        # All attempts exhausted for this tier
        return {
            "attempts": attempts,
            "final_attempt": last_attempt,
            "success": False,
            "body": None,
            "transport": tier_name,
            "fallback_to_next_tier": True,
            "halt_reason": None,
        }

    # -- Tier 2: CloakBrowser ---------------------------------------------
    async def _fetch_via_cloakbrowser(self, path: str, timeout_ms: int) -> Dict[str, Any]:
        t0 = time.monotonic()
        if self._cloakbrowser_launcher is None:
            return {"attempt": _make_attempt("cloakbrowser", 0, int((time.monotonic() - t0) * 1000),
                                              "cloakbrowser_unavailable"),
                    "body": None}
        try:
            if self._browser is None:
                proxy = self.proxy_url()
                self._browser = await self._cloakbrowser_launcher(
                    headless=True,
                    proxy=proxy,
                    humanize=False,
                )
            # Active fetch via browser API context.request (Playwright-compatible)
            api_context = getattr(self._browser, "contexts", None)
            page = await self._browser.new_page()
            try:
                resp = await page.goto(f"{self.config.base_url}{path}",
                                       timeout=max(3000, timeout_ms))  # Playwright ms units
                status = resp.status if resp else 0
                try:
                    body = await resp.json()
                    error = None
                except Exception as json_error:
                    body = await resp.text()
                    error = f"json parse failed: {json_error}"
            finally:
                try:
                    await page.close()
                except Exception:
                    pass
            return {"attempt": _make_attempt("cloakbrowser", status, int((time.monotonic() - t0) * 1000), error),
                    "body": body}
        except Exception as e:
            if self._is_target_closed_error(e):
                self.stats.epipe_count += 1
                await self.rebuild_browser()
            return {"attempt": _make_attempt("cloakbrowser", 0, int((time.monotonic() - t0) * 1000),
                                              f"cloakbrowser failed: {e}"),
                    "body": None}

    # -- Tier 3: SSR -------------------------------------------------------
    async def _fetch_via_ssr(self, path: str, timeout_ms: int) -> Dict[str, Any]:
        t0 = time.monotonic()
        if self._ssr_fetcher is None:
            return {"attempt": _make_attempt("ssr", 0, int((time.monotonic() - t0) * 1000), "ssr_unavailable"),
                    "body": None}
        try:
            raw = await asyncio.to_thread(self._ssr_fetcher, path)
            if raw is None:
                return {"attempt": _make_attempt("ssr", 0, int((time.monotonic() - t0) * 1000), "ssr_returned_none"),
                        "body": None}
            return {"attempt": _make_attempt("ssr", 200, int((time.monotonic() - t0) * 1000)),
                    "body": raw}
        except SSRHelperMissing as e:
            # Phase 3: surface the typed error as a deterministic attempt
            # so the artifact distinguishes 'helper missing' from generic
            # failures. Refusing to bundle into the generic except branch.
            return {"attempt": _make_attempt("ssr", 0, int((time.monotonic() - t0) * 1000),
                                              f"ssr_helper_missing: {e}"),
                    "body": None}
        except Exception as e:
            return {"attempt": _make_attempt("ssr", 0, int((time.monotonic() - t0) * 1000),
                                              f"ssr failed: {e}"),
                    "body": None}

    # -- Main entry --------------------------------------------------------
    async def fetch_api(self, path: str, *, timeout_ms: int = 30000) -> Dict[str, Any]:
        """Three-tier fetch with retry-with-rotation.

        Returns contract per §7. Phase 8.1 (Kris 2026-08-25 12:02):
          - Tier-1: bounded retry (up to TIER1_MAX_RETRIES=5), rotate proxy
            between 403 attempts, evict cookies on rotation
          - Tier-2: bounded retry (up to TIER2_BROWSER_MAX_RETRIES=2),
            rotate proxy between attempts
          - Tier-3 SSR: 1 attempt (incidents returns None by design)
          - 407 TERMINAL HALT preserved at every attempt
          - 404 → fall to next tier, no retry (not generic-retry policy)
          - 200 + invalid → 1 retry then fall
          - Budget guard records every attempt
          - Cross-tier total attempts capped at TOTAL_MAX_ATTEMPTS_PER_REQUEST=8
        """
        self._assert_no_write()
        # Phase 8: warm-up gate (Q1)
        await self._ensure_warmed(path)
        all_attempts: List[Dict[str, Any]] = []
        total_attempts_used = 0

        # ---- Tier 1 (curl_cffi) ----
        from gen4_components import TIER1_MAX_RETRIES, TIER2_BROWSER_MAX_RETRIES
        t1 = await self._fetch_with_retry_tier(
            tier_name="curl_cffi",
            path=path,
            timeout_ms=timeout_ms,
            tier_max_retries=TIER1_MAX_RETRIES,
        )
        all_attempts.extend(t1["attempts"])
        total_attempts_used += len(t1["attempts"])
        if t1["halt_reason"] == "http_407_safety_halt":
            # TERMINAL HALT (LOCKED Phase 8 / §17) — no Tier-2 / Tier-3 fallback
            self.stats.attempts.extend(all_attempts)
            return _failure_result(
                status=407,
                transport="curl_cffi",
                attempts=all_attempts,
                retry_count=total_attempts_used - 1,
                error_class="http_407",
                halt_reason="http_407_safety_halt",
            )
        if t1["success"]:
            self.stats.attempts.extend(all_attempts)
            return _success_result(
                status=t1["final_attempt"]["status"],
                data=t1["body"],
                transport="curl_cffi",
                attempts=all_attempts,
                retry_count=total_attempts_used - 1,
                payload_complete=True,
            )

        # ---- Tier 2 (cloakbrowser) ----
        if total_attempts_used < TOTAL_MAX_ATTEMPTS_PER_REQUEST:
            t2 = await self._fetch_with_retry_tier(
                tier_name="cloakbrowser",
                path=path,
                timeout_ms=timeout_ms,
                tier_max_retries=TIER2_BROWSER_MAX_RETRIES,
            )
            all_attempts.extend(t2["attempts"])
            total_attempts_used += len(t2["attempts"])
            if t2["halt_reason"] == "http_407_safety_halt":
                self.stats.attempts.extend(all_attempts)
                return _failure_result(
                    status=407,
                    transport="cloakbrowser",
                    attempts=all_attempts,
                    retry_count=total_attempts_used - 1,
                    error_class="http_407",
                    halt_reason="http_407_safety_halt",
                )
            if t2["success"]:
                self.stats.attempts.extend(all_attempts)
                return _success_result(
                    status=t2["final_attempt"]["status"],
                    data=t2["body"],
                    transport="cloakbrowser",
                    attempts=all_attempts,
                    retry_count=total_attempts_used - 1,
                    payload_complete=True,
                )

        # ---- Tier 3 (SSR) ----
        t3 = await self._fetch_via_ssr(path, timeout_ms)
        all_attempts.append(t3["attempt"])
        total_attempts_used += 1
        if self._budget is not None:
            self._budget.record_attempt(
                status=t3["attempt"]["status"],
                is_timeout=False,
            )
        if t3["attempt"]["status"] == 200 and t3["body"] is not None:
            complete, err_class = validate_payload(path, t3["body"])
            self.stats.attempts.extend(all_attempts)
            return _success_result(
                status=t3["attempt"]["status"], data=t3["body"],
                transport="ssr", attempts=all_attempts,
                retry_count=total_attempts_used - 1, payload_complete=complete,
            )

        # ---- All tiers failed ----
        last_status = all_attempts[-1]["status"] if all_attempts else 0
        first_status = all_attempts[0]["status"] if all_attempts else 0
        error_class = classify_error(last_status, None, all_attempts[-1]["error"] if all_attempts else None)
        # Phase 8.1: any_success must reflect COMPLETED success (returned from
        # fetch_api as _success_result), not raw status==200 (which can be a
        # 200 + invalid payload attempt that fell through). We track via
        # transport + the _success_result was NOT called.
        # Simpler: if the request loop reached this point, no tier returned
        # _success_result — so any_success is False.
        any_success = False
        failure_status = 0 if not any_success else (last_status or first_status)
        self.stats.attempts.extend(all_attempts)
        return _failure_result(
            status=failure_status,
            transport="ssr",
            attempts=all_attempts,
            retry_count=total_attempts_used - 1,
            error_class=error_class,
        )
