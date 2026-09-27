"""
Phase 8.1 offline integration tests for Gen4Fetcher.

Scope (Kris 2026-08-25 12:02 GMT+8):
  - Tier-1 403 → rotate → retry → success.
  - Tier-1 exhaust → Tier-2.
  - Tier-2 403 retry twice → SSR.
  - 407 terminal halt (no Tier-2 / Tier-3 fallback).
  - 404 no generic retry → next tier.
  - timeout / connection error bounded retry.
  - budget exhaustion guard.
  - cookie eviction after rotation no cross-proxy reuse.
  - no duplicate warm-up (Q1).
  - uses Phase 4 artifact (event 14025013) as deterministic fixture input.

Forbidden: live HTTP, real proxy, browser launch, canary, full backfill.

These tests exercise the wired retry loop in `fetch_api` end-to-end via
fake components injected through DI. Phase 4 artifact is loaded once and
replayed as canned responses — no real network, no real browser.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

# Repo root on sys.path
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Use the project's venv pytest if available
_PROJECT_VENV = _REPO_ROOT / ".runner-venv" / "lib" / "python3.11" / "site-packages"
if _PROJECT_VENV.exists() and str(_PROJECT_VENV) not in sys.path:
    sys.path.insert(0, str(_PROJECT_VENV))

from gen4_fetcher import Gen4Fetcher, Gen4Config, SSR_UNSUPPORTED_ENDPOINTS  # noqa: E402

# -- Phase 4 artifact (deterministic fixture) -----------------------------

PHASE4_ARTIFACT = _REPO_ROOT / "data" / "gen4_phase4_smoke_event_14025013.json"
PHASE4_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "phase4_event_14025013.json"


def _load_phase4() -> Dict[str, Any]:
    assert PHASE4_FIXTURE.exists(), f"Phase 4 fixture missing: {PHASE4_FIXTURE}"
    with open(PHASE4_FIXTURE) as f:
        return json.load(f)


def _make_valid_event_body() -> Dict[str, Any]:
    """Minimal valid SofaScore event payload — mimic what /event returns."""
    return {
        "event": {
            "id": 14025013,
            "home": {"id": 10431, "name": "Team A"},
            "away": {"id": 10432, "name": "Team B"},
            "status": {"type": "finished"},
        },
        "incidents": [],
        "lineups": {"home": [], "away": []},
        "statistics": [],
    }


# -- Fake components (re-use phase 8 DI patterns) -------------------------

class _ScriptedCurlCffi:
    """Returns canned responses in order. Records every call.

    Each canned response is a dict with keys:
      - status: int (HTTP status to set on .status_code attribute)
      - body: dict | None (JSON-serialisable body; .json() returns this)
      - error: Optional[str] (sets .json() failure message)
      - exception: Optional[str] (sets attempt exception field)
      - latency_ms: int (recorded latency)
    The returned object mimics a curl-cffi response: .status_code + .json()
    + .text attributes.
    """

    def __init__(self, responses: List[Dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.call_count = 0
        self.last_kwargs: Optional[Dict[str, Any]] = None

    def __call__(self, path: str, *, timeout_ms: int = 30000, **kwargs: Any) -> Any:
        self.call_count += 1
        self.last_kwargs = {"path": path, "timeout_ms": timeout_ms, **kwargs}
        if not self._responses:
            r = {"status": 200, "body": {}}
        else:
            r = self._responses.pop(0)
        # Build a mock response object that mimics curl_cffi Response.
        return _MockResponse(
            status_code=r.get("status", 200),
            body=r.get("body"),
            error=r.get("error"),
        )


class _MockResponse:
    """Minimal mock of curl_cffi Response with .status_code, .json(), .text."""

    def __init__(self, *, status_code: int, body: Any = None, error: Optional[str] = None) -> None:
        self.status_code = status_code
        self._body = body
        self._error = error
        self.text = json.dumps(body) if body is not None else ""

    def json(self) -> Any:
        if self._body is None:
            raise ValueError(self._error or "no body")
        return self._body


class _ScriptedCloakBrowser:
    """Mimics CloakBrowser launch + new_page + goto + close for Tier-2.

    CloakBrowser contract (production):
      launcher = cloakbrowser_launcher(**kwargs) → browser object
      browser.new_page() → page
      page.goto(url, timeout=ms) → response with .status, .json() async
      page.close()
    """

    def __init__(self, responses: List[Dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.call_count = 0

    async def launch(self, **kwargs: Any) -> "_FakeBrowser":
        return _FakeBrowser(self)

    async def __call__(self, path: str, *, timeout_ms: int = 30000, **kwargs: Any) -> Dict[str, Any]:
        self.call_count += 1
        if not self._responses:
            return _canned(status=200, body={})
        return self._responses.pop(0)


class _FakeBrowser:
    """Mimics CloakBrowser browser object with new_page()."""

    def __init__(self, controller: _ScriptedCloakBrowser) -> None:
        self._controller = controller

    async def new_page(self) -> "_FakePage":
        return _FakePage(self._controller)


class _FakePage:
    """Mimics Playwright Page with goto() returning a Response-like object."""

    def __init__(self, controller: _ScriptedCloakBrowser) -> None:
        self._controller = controller

    async def goto(self, url: str, timeout: int = 30000) -> "_FakeResponse":
        self._controller.call_count += 1
        if not self._controller._responses:
            r = _canned(status=200, body={})
        else:
            r = self._controller._responses.pop(0)
        return _FakeResponse(status=r["status"], body=r.get("body"), error=r.get("error"))

    async def close(self) -> None:
        pass


class _FakeResponse:
    """Mimics Playwright Response with .status, .json(), .text."""

    def __init__(self, *, status: int, body: Any = None, error: Optional[str] = None) -> None:
        self.status = status
        self._body = body
        self._error = error

    async def json(self) -> Any:
        if self._body is None:
            raise ValueError(self._error or "no body")
        return self._body

    async def text(self) -> str:
        return json.dumps(self._body) if self._body is not None else ""


class _ScriptedSSR:
    def __init__(self, response_body: Optional[Dict[str, Any]] = None) -> None:
        self._body = response_body
        self.call_count = 0

    def __call__(self, path: str) -> Optional[Dict[str, Any]]:
        self.call_count += 1
        return self._body


class _CountingProxyRotator:
    """Tracks rotation calls + current proxy identity."""

    def __init__(self) -> None:
        self.proxies = ["proxy-a", "proxy-b", "proxy-c", "proxy-d"]
        self.index = 0
        self.rotate_count = 0

    async def rotate(self) -> None:
        self.index = (self.index + 1) % len(self.proxies)
        self.rotate_count += 1

    def current_proxy(self) -> str:
        return self.proxies[self.index]


class _CountingCookieStore:
    """Tracks evictions + cross-proxy reuse attempts."""

    def __init__(self) -> None:
        self.captured: List[Tuple[str, Dict[str, Any]]] = []
        self.evict_count = 0
        self.proxy_to_cookies: Dict[str, Dict[str, Any]] = {}

    def capture(self, proxy_id: str, cookies: Dict[str, Any]) -> None:
        self.captured.append((proxy_id, cookies))
        self.proxy_to_cookies[proxy_id] = cookies

    def evict_by_proxy(self, current_proxy: str) -> int:
        """Evict cookies NOT matching current_proxy. Returns eviction count."""
        evicted = 0
        for k in list(self.proxy_to_cookies.keys()):
            if k != current_proxy:
                del self.proxy_to_cookies[k]
                evicted += 1
        self.evict_count += evicted
        return evicted


class _CountingBudget:
    """Tracks attempts + cookies_evicted + quota_warning."""

    def __init__(self, max_total: int = 100) -> None:
        self.total_attempts = 0
        self.cookies_evicted = 0
        self.max_total = max_total
        self.records: List[Dict[str, Any]] = []

    def record_attempt(self, *, status: int, is_timeout: bool = False) -> None:
        self.total_attempts += 1
        # status=0 is the production convention for transport timeout/conn-error.
        # Reflect this in records so tests can count timeouts.
        detected_timeout = is_timeout or status == 0
        self.records.append({"status": status, "is_timeout": detected_timeout})

    def quota_warning(self, *, max_total: Optional[int] = None) -> bool:
        ceiling = max_total if max_total is not None else self.max_total
        return self.total_attempts >= ceiling * 0.8


class _OnceWarmup:
    """Fires exactly once per fetcher instance."""

    def __init__(self) -> None:
        self.call_count = 0
        self.scope = "per_event"

    async def warm_homepage(self) -> None:
        self.call_count += 1


# -- Helpers --------------------------------------------------------------

def _ok_attempt(transport: str, *, body: Any = None, status: int = 200, latency_ms: int = 100) -> Dict[str, Any]:
    return {
        "attempt": {
            "transport": transport,
            "status": status,
            "latency_ms": latency_ms,
            "exception": None,
            "error": None,
        },
        "body": body,
    }


def _err_attempt(transport: str, *, status: int, error: Optional[str] = None,
                 exception: Optional[str] = None, latency_ms: int = 100) -> Dict[str, Any]:
    return {
        "attempt": {
            "transport": transport,
            "status": status,
            "latency_ms": latency_ms,
            "exception": exception,
            "error": error,
        },
        "body": None,
    }


def _canned(*, status: int, body: Any = None, error: Optional[str] = None,
             exception: Optional[str] = None) -> Dict[str, Any]:
    """Build a canned response for _ScriptedCurlCffi / _ScriptedCloakBrowser.

    These fakes expect a flat dict with status / body / error / exception keys.
    NOT the wrapped {_ok_attempt / _err_attempt} format used by tier fetchers.
    """
    return {
        "status": status,
        "body": body,
        "error": error,
        "exception": exception,
    }


def _make_fetcher(
    *,
    curl_cffi: Any,
    cloakbrowser: _ScriptedCloakBrowser,
    ssr: Any,
    proxy_rotator: Optional[_CountingProxyRotator] = None,
    cookie_store: Optional[_CountingCookieStore] = None,
    budget: Optional[_CountingBudget] = None,
    warmup: Optional[_OnceWarmup] = None,
) -> Gen4Fetcher:
    """Build Gen4Fetcher with all DI fields populated (Phase 8 11-arg ctor)."""
    config = Gen4Config(
        base_url="https://www.sofascore.com",
        max_browser_rebuilds=2,
    )
    return Gen4Fetcher(
        config=config,
        curl_cffi_get=curl_cffi,
        cloakbrowser_launcher=cloakbrowser.launch,
        ssr_fetcher=ssr,
        proxy_rotator=proxy_rotator,
        cookie_store=cookie_store,
        budget=budget,
        warmup_sequence=warmup,
    )


# -- 1. Tier-1 403 → rotate → retry → success -----------------------------

class TestTier1RetryRotationSuccess:
    """Tier-1 403, rotate proxy, retry, eventually succeed."""

    def test_t1_403_then_200_after_rotation(self):
        async def run():
            valid_body = _make_valid_event_body()
            curl = _ScriptedCurlCffi([
                _canned(status=403, error="forbidden"),
                _canned(status=200, body=valid_body),  # success after rotation
            ])
            proxy_rotator = _CountingProxyRotator()
            cookie_store = _CountingCookieStore()
            budget = _CountingBudget()
            f = _make_fetcher(
                curl_cffi=curl, cloakbrowser=_ScriptedCloakBrowser([]),
                ssr=_ScriptedSSR(),
                proxy_rotator=proxy_rotator, cookie_store=cookie_store,
                budget=budget,
            )
            result = await f.fetch_api("/api/v1/event/14025013")
            # Assertions:
            assert result["transport"] == "curl_cffi"
            assert result["status"] == 200
            # Tier-1 made 2 attempts (1 initial + 1 retry after rotation)
            assert curl.call_count == 2
            # 1 rotation happened
            assert proxy_rotator.rotate_count == 1
            # budget recorded both attempts
            assert budget.total_attempts == 2
            return result

        result = asyncio.run(run())
        assert result["status"] == 200


# -- 2. Tier-1 exhaust → Tier-2 -------------------------------------------

class TestTier1ExhaustTier2Success:
    """Tier-1 all 403s → fall back to Tier-2 which succeeds."""

    def test_t1_exhaust_falls_to_t2(self):
        async def run():
            valid_body = _make_valid_event_body()
            # Tier-1: 5 × 403 (exhausts retries)
            curl = _ScriptedCurlCffi([
                _canned(status=403),
                _canned(status=403),
                _canned(status=403),
                _canned(status=403),
                _canned(status=403),
            ])
            # Tier-2: 1 × 200 success
            cloak = _ScriptedCloakBrowser([_canned(status=200, body=valid_body)])
            proxy_rotator = _CountingProxyRotator()
            budget = _CountingBudget()
            f = _make_fetcher(
                curl_cffi=curl, cloakbrowser=cloak,
                ssr=_ScriptedSSR(),
                proxy_rotator=proxy_rotator,
                budget=budget,
            )
            result = await f.fetch_api("/api/v1/event/14025013")
            assert result["transport"] == "cloakbrowser"
            assert result["status"] == 200
            # Tier-1 made 5 attempts (max)
            assert curl.call_count == 5
            # Tier-2 made 1 attempt (succeeded first try)
            assert cloak.call_count == 1
            # 4 rotations during Tier-1 retry loop
            assert proxy_rotator.rotate_count == 4
            # Budget = 5 + 1 = 6
            assert budget.total_attempts == 6
            return result

        result = asyncio.run(run())
        assert result["transport"] == "cloakbrowser"


# -- 3. Tier-2 403 × 2 → SSR ----------------------------------------------

class TestTier2RetryExhaustSSR:
    """Tier-1 fails, Tier-2 403 × 2 → fall to SSR."""

    def test_t2_exhaust_falls_to_ssr(self):
        async def run():
            valid_body = _make_valid_event_body()
            # Tier-1: 5 × 403
            curl = _ScriptedCurlCffi([
                _canned(status=403),
                _canned(status=403),
                _canned(status=403),
                _canned(status=403),
                _canned(status=403),
            ])
            # Tier-2: 2 × 403 (exhausts)
            cloak = _ScriptedCloakBrowser([
                _canned(status=403),
                _canned(status=403),
            ])
            # SSR: 1 × 200 success
            ssr = _ScriptedSSR(valid_body)
            proxy_rotator = _CountingProxyRotator()
            budget = _CountingBudget()
            f = _make_fetcher(
                curl_cffi=curl, cloakbrowser=cloak, ssr=ssr,
                proxy_rotator=proxy_rotator,
                budget=budget,
            )
            result = await f.fetch_api("/api/v1/event/14025013")
            assert result["transport"] == "ssr"
            assert result["status"] == 200
            # Tier-1 5 + Tier-2 2 + SSR 1 = 8 (at total ceiling)
            assert curl.call_count == 5
            assert cloak.call_count == 2
            assert ssr.call_count == 1
            # 4 Tier-1 rotations + 1 Tier-2 rotation = 5
            assert proxy_rotator.rotate_count == 5
            # Budget = 8
            assert budget.total_attempts == 8
            return result

        result = asyncio.run(run())
        assert result["transport"] == "ssr"


# -- 4. 407 terminal halt -------------------------------------------------

class Test407TerminalHalt:
    """407 on first attempt → halt immediately, no Tier-2/3 fallback."""

    def test_t1_407_halts_no_t2(self):
        async def run():
            curl = _ScriptedCurlCffi([_canned(status=407)])
            cloak = _ScriptedCloakBrowser([])  # must NOT be called
            ssr = _ScriptedSSR()  # must NOT be called
            proxy_rotator = _CountingProxyRotator()  # must NOT be called
            budget = _CountingBudget()
            f = _make_fetcher(
                curl_cffi=curl, cloakbrowser=cloak, ssr=ssr,
                proxy_rotator=proxy_rotator, budget=budget,
            )
            result = await f.fetch_api("/api/v1/event/14025013")
            assert result["halt_reason"] == "http_407_safety_halt"
            assert result["status"] == 407
            assert result["transport"] == "curl_cffi"
            # NO Tier-2 / Tier-3 calls
            assert curl.call_count == 1
            assert cloak.call_count == 0
            assert ssr.call_count == 0
            # NO rotation
            assert proxy_rotator.rotate_count == 0
            # Only 1 budget record
            assert budget.total_attempts == 1
            return result

        result = asyncio.run(run())
        assert result["halt_reason"] == "http_407_safety_halt"

    def test_t1_407_in_retry_loop_still_halts(self):
        """Even if 407 arrives on retry (not first), still halt."""
        async def run():
            curl = _ScriptedCurlCffi([
                _canned(status=403),  # first attempt: rotate
                _canned(status=407),  # retry: HALT
            ])
            cloak = _ScriptedCloakBrowser([])
            ssr = _ScriptedSSR()
            proxy_rotator = _CountingProxyRotator()
            budget = _CountingBudget()
            f = _make_fetcher(
                curl_cffi=curl, cloakbrowser=cloak, ssr=ssr,
                proxy_rotator=proxy_rotator, budget=budget,
            )
            result = await f.fetch_api("/api/v1/event/14025013")
            assert result["halt_reason"] == "http_407_safety_halt"
            assert result["status"] == 407
            # 2 attempts total
            assert curl.call_count == 2
            assert cloak.call_count == 0
            return result

        result = asyncio.run(run())
        assert result["halt_reason"] == "http_407_safety_halt"


# -- 5. 404 no generic retry ---------------------------------------------

class Test404EndpointReview:
    """404 must NOT enter retry loop — fall to next tier immediately."""

    def test_t1_404_falls_to_t2_immediately(self):
        async def run():
            valid_body = _make_valid_event_body()
            curl = _ScriptedCurlCffi([_canned(status=404)])
            cloak = _ScriptedCloakBrowser([_canned(status=200, body=valid_body)])
            ssr = _ScriptedSSR()
            proxy_rotator = _CountingProxyRotator()
            budget = _CountingBudget()
            f = _make_fetcher(
                curl_cffi=curl, cloakbrowser=cloak, ssr=ssr,
                proxy_rotator=proxy_rotator, budget=budget,
            )
            result = await f.fetch_api("/api/v1/event/14025013")
            # Tier-1 made ONLY 1 attempt (404 is endpoint-review, not retry)
            assert curl.call_count == 1
            # Tier-2 succeeded
            assert cloak.call_count == 1
            assert result["transport"] == "cloakbrowser"
            assert result["status"] == 200
            # NO rotation (404 doesn't trigger rotate)
            assert proxy_rotator.rotate_count == 0
            # Budget = 1 + 1 = 2
            assert budget.total_attempts == 2
            return result

        result = asyncio.run(run())
        assert result["transport"] == "cloakbrowser"


# -- 6. Timeout / connection error bounded retry ---------------------------

class TestTimeoutBoundedRetry:
    """Status 0 + exception → TRANSIENT_RETRY, max 2 retries."""

    def test_timeout_retries_then_falls(self):
        async def run():
            valid_body = _make_valid_event_body()
            # Tier-1: timeout × 2 (initial + 1 transient retry)
            # Per spec: TRANSIENT_RETRY ceiling check is `attempts_used >= TIER3_TIMEOUT_MAX_RETRIES`
            # which equals "max 2 attempts total" (initial + 1 retry), NOT
            # "max 2 retries".  Documented invariant in decide_retry docstring.
            curl = _ScriptedCurlCffi([
                _canned(status=0, exception="TimeoutError"),
                _canned(status=0, exception="TimeoutError"),
                _canned(status=0, exception="TimeoutError"),
                _canned(status=0, exception="TimeoutError"),
                _canned(status=0, exception="TimeoutError"),
            ])
            # Tier-2: 1 success
            cloak = _ScriptedCloakBrowser([_canned(status=200, body=valid_body)])
            ssr = _ScriptedSSR()
            proxy_rotator = _CountingProxyRotator()
            budget = _CountingBudget()
            f = _make_fetcher(
                curl_cffi=curl, cloakbrowser=cloak, ssr=ssr,
                proxy_rotator=proxy_rotator, budget=budget,
            )
            result = await f.fetch_api("/api/v1/event/14025013")
            # Tier-1 made 2 attempts (initial + 1 retry, ceiling at TIER3_TIMEOUT_MAX_RETRIES=2)
            assert curl.call_count == 2
            # Tier-2 succeeded
            assert result["transport"] == "cloakbrowser"
            # Budget records show 2 timeouts (Tier-1) + 1 success (Tier-2) = 3
            assert budget.total_attempts == 3
            timeout_count = sum(1 for r in budget.records if r["is_timeout"])
            assert timeout_count == 2
            return result

        result = asyncio.run(run())
        assert result["transport"] == "cloakbrowser"


# -- 7. Budget exhaustion guard -------------------------------------------

class TestBudgetExhaustionGuard:
    """Budget tracker accumulates correctly across tiers."""

    def test_budget_records_all_attempts(self):
        async def run():
            curl = _ScriptedCurlCffi([
                _canned(status=403),
                _canned(status=403),
                _canned(status=403),
                _canned(status=403),
                _canned(status=403),
            ])
            cloak = _ScriptedCloakBrowser([
                _canned(status=403),
                _canned(status=403),
            ])
            ssr = _ScriptedSSR(None)  # SSR returns None → final fail
            proxy_rotator = _CountingProxyRotator()
            budget = _CountingBudget()
            f = _make_fetcher(
                curl_cffi=curl, cloakbrowser=cloak, ssr=ssr,
                proxy_rotator=proxy_rotator, budget=budget,
            )
            result = await f.fetch_api("/api/v1/event/14025013")
            assert result["status"] == 0  # all tiers failed
            # Budget = 5 + 2 + 1 = 8 (at ceiling)
            assert budget.total_attempts == 8
            # quota_warning should trigger at 80% of 100 = 80
            assert budget.quota_warning(max_total=10)  # 8 >= 8 (80% of 10)
            return result

        result = asyncio.run(run())
        assert result["status"] == 0


# -- 8. Cookie eviction no cross-proxy reuse -------------------------------

class TestCookieEvictionCrossProxyIsolation:
    """After proxy rotation, cookies from previous proxy are evicted."""

    def test_cookies_evicted_on_rotation_no_cross_proxy_reuse(self):
        async def run():
            valid_body = _make_valid_event_body()
            curl = _ScriptedCurlCffi([
                _canned(status=403),  # initial: 403 → rotate proxy
                _canned(status=200, body=valid_body),  # retry: success under new proxy
            ])
            proxy_rotator = _CountingProxyRotator()
            cookie_store = _CountingCookieStore()
            # Pre-populate cookies under proxy-a (the starting proxy) to verify
            # eviction removes them when proxy rotates to proxy-b.
            cookie_store.capture("proxy-a", {"session": "abc123"})
            budget = _CountingBudget()
            f = _make_fetcher(
                curl_cffi=curl, cloakbrowser=_ScriptedCloakBrowser([]),
                ssr=_ScriptedSSR(),
                proxy_rotator=proxy_rotator, cookie_store=cookie_store,
                budget=budget,
            )

            result = await f.fetch_api("/api/v1/event/14025013")
            assert result["status"] == 200
            # 1 rotation happened (proxy-a → proxy-b)
            assert proxy_rotator.rotate_count == 1
            # Cookies from previous proxy evicted on rotation (Q6 isolation)
            assert cookie_store.evict_count == 1
            # Budget records cookies_evicted increment
            assert budget.cookies_evicted == 1
            # After eviction, only new proxy's cookies would remain (none in this test
            # since capture() is Phase 9+ behavior). proxy-a's cookie was evicted.
            assert "proxy-a" not in cookie_store.proxy_to_cookies
            return result

        result = asyncio.run(run())
        assert result["status"] == 200


# -- 9. No duplicate warm-up ----------------------------------------------

class TestNoDuplicateWarmup:
    """Warm-up fires ONCE per fetcher, regardless of fetch_api call count."""

    def test_warmup_fires_once_per_instance(self):
        async def run():
            warmup = _OnceWarmup()
            curl = _ScriptedCurlCffi([
                _ok_attempt("curl_cffi", body=_make_valid_event_body()),
            ])
            f = _make_fetcher(
                curl_cffi=curl, cloakbrowser=_ScriptedCloakBrowser([]),
                ssr=_ScriptedSSR(),
                warmup=warmup,
            )
            # Call fetch_api 3 times
            await f.fetch_api("/api/v1/event/14025013")
            await f.fetch_api("/api/v1/event/14025013")
            await f.fetch_api("/api/v1/event/14025013")
            # Warm-up fires exactly once
            assert warmup.call_count == 1
            return warmup.call_count

        count = asyncio.run(run())
        assert count == 1


# -- 10. Phase 4 artifact as fixture input (no re-request) ---------------

class TestPhase4ArtifactFixtureInput:
    """Use Phase 4 artifact's event endpoint response as canned input.

    Verifies: Phase 4 fixture loads, returns 200 status from curl_cffi Tier-1,
    body matches artifact payload, no live HTTP needed.
    """

    def test_phase4_event_artifact_serves_as_deterministic_input(self):
        phase4 = _load_phase4()
        # Fixture must have event_id 14025013
        assert phase4["event_id"] == 14025013
        # Event endpoint artifact has transport="curl_cffi" + 200
        ep = phase4["endpoint_event"]
        assert ep["transport"] == "curl_cffi"
        assert ep["status"] == 200
        # Path must match what we'd send
        assert ep["path"] == "/api/v1/event/14025013"
        # Latency recorded for reproducibility
        assert ep["latency_ms"] > 0

    def test_replay_phase4_event_response_as_canned(self):
        """Replay Phase 4's /event response in offline test → success."""
        async def run():
            phase4 = _load_phase4()
            ep = phase4["endpoint_event"]
            # Use the recorded body as canned response
            body = ep.get("body") or _make_valid_event_body()
            curl = _ScriptedCurlCffi([_canned(status=200, body=body)])
            f = _make_fetcher(
                curl_cffi=curl, cloakbrowser=_ScriptedCloakBrowser([]),
                ssr=_ScriptedSSR(),
            )
            result = await f.fetch_api("/api/v1/event/14025013")
            assert result["status"] == 200
            assert result["transport"] == "curl_cffi"
            assert curl.call_count == 1  # succeeded first try (no retry needed)
            return result

        result = asyncio.run(run())
        assert result["status"] == 200

    def test_replay_phase4_incidents_response_as_canned(self):
        """Replay Phase 4's /incidents response: 3 × 403 + ssr_none."""
        async def run():
            phase4 = _load_phase4()
            ep = phase4["endpoint_incidents"]
            # Build canned responses from artifact
            tier1_responses = []
            for attempt in ep["attempts"]:
                if attempt.get("tier") == "curl_cffi":
                    tier1_responses.append(
                        _err_attempt("curl_cffi", status=attempt.get("status", 403),
                                      error=attempt.get("error", "forbidden"))
                    )
                elif attempt.get("tier") == "cloakbrowser":
                    # Tier-2 — would be tested separately; here we stop at Tier-1
                    pass
            # Should have at least 1 Tier-1 response from Phase 4
            curl = _ScriptedCurlCffi(tier1_responses or [_canned(status=403)])
            proxy_rotator = _CountingProxyRotator()
            budget = _CountingBudget()
            f = _make_fetcher(
                curl_cffi=curl, cloakbrowser=_ScriptedCloakBrowser([]),
                ssr=_ScriptedSSR(None),  # SSR returns None (incidents unsupported)
                proxy_rotator=proxy_rotator,
                budget=budget,
            )
            result = await f.fetch_api("/api/v1/event/14025013/incidents")
            # /incidents is in SSR unsupported set, so SSR returns None → overall fail
            assert result["status"] == 0
            # Tier-1 attempted at least once
            assert curl.call_count >= 1
            # Budget tracked attempts
            assert budget.total_attempts >= 1
            return result

        result = asyncio.run(run())
        assert result["status"] == 0


# -- 11. SSR /incidents returns None (preserved) --------------------------

class TestSSRIncidentsPreserved:
    """/incidents still returns None from SSR — backward compat from Phase 7."""

    def test_incidents_ssr_returns_none(self):
        async def run():
            # Tier-1 403, Tier-2 403, SSR = None for /incidents
            curl = _ScriptedCurlCffi([
                _canned(status=403),
            ])
            cloak = _ScriptedCloakBrowser([_canned(status=403)])
            ssr = _ScriptedSSR(None)  # /incidents unsupported
            f = _make_fetcher(
                curl_cffi=curl, cloakbrowser=cloak, ssr=ssr,
                proxy_rotator=_CountingProxyRotator(),
            )
            result = await f.fetch_api("/api/v1/event/14025013/incidents")
            # Final failure, transport ssr (tried last), status 0
            assert result["status"] == 0
            assert result["transport"] == "ssr"
            return result

        result = asyncio.run(run())
        assert result["status"] == 0


# -- 12. Cross-tier ceiling enforcement ----------------------------------

class TestCrossTierCeiling:
    """Total attempts across Tier-1 + Tier-2 + Tier-3 capped at 8."""

    def test_ceiling_blocks_after_8_attempts(self):
        async def run():
            # All 5 Tier-1 fail, all 2 Tier-2 fail, Tier-3 attempted once
            # 5 + 2 + 1 = 8 (exactly at ceiling)
            curl = _ScriptedCurlCffi([
                _canned(status=403),
                _canned(status=403),
                _canned(status=403),
                _canned(status=403),
                _canned(status=403),
            ])
            cloak = _ScriptedCloakBrowser([
                _canned(status=403),
                _canned(status=403),
            ])
            ssr = _ScriptedSSR(None)  # final fail
            proxy_rotator = _CountingProxyRotator()
            budget = _CountingBudget()
            f = _make_fetcher(
                curl_cffi=curl, cloakbrowser=cloak, ssr=ssr,
                proxy_rotator=proxy_rotator,
                budget=budget,
            )
            result = await f.fetch_api("/api/v1/event/14025013")
            assert result["status"] == 0
            assert curl.call_count == 5
            assert cloak.call_count == 2
            assert ssr.call_count == 1
            # Total attempts = 8 = TOTAL_MAX_ATTEMPTS_PER_REQUEST
            assert budget.total_attempts == 8
            return result

        result = asyncio.run(run())
        assert result["status"] == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
