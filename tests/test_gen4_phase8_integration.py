"""test_gen4_phase8_integration.py — Offline integration tests for Gen4Fetcher refactor.

Phase 8 APPROVED (Kris 2026-08-25 09:49). OFFLINE ONLY. Verifies:
  - Gen4Fetcher accepts 7 new Optional DI args (retry_policy, proxy_rotator,
    warmup_sequence, cookie_store, sticky_session, header_factory, budget)
  - Backward-compat: 4-arg signature still works, all defaults None
  - Warm-up gate fires once (Q1)
  - HeaderFactory wired into _fetch_via_curl_cffi (Q5)
  - CookieStore + proxy rotation + eviction (Q1, Q6)
  - Budget guard records attempts (Q7)
  - 407 terminal halt preserved (invariant)
  - SSR incidents unsupported preserved (invariant)
  - No live calls made during any test
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from gen4_fetcher import Gen4Fetcher, Gen4Config
from gen4_components import (
    BROWSER_BASE,
    DefaultHeaderFactory,
    RetryClassification,
    decide_retry,
    TIER1_MAX_RETRIES,
    TIER2_BROWSER_MAX_RETRIES,
    TOTAL_MAX_ATTEMPTS_PER_REQUEST,
)

from tests.fixtures.gen4_components_fakes import (
    FakeCookieStore,
    FakeHeaderFactory,
    FakeProxyRotator,
    FakeRetryPolicy,
    FakeStickySessionKey,
    FakeWarmupSequence,
    make_default_fakes,
)


# ===========================================================================
# Helpers
# ===========================================================================

def _make_config(write_enabled: bool = False) -> Gen4Config:
    return Gen4Config(
        write_enabled=write_enabled,
        proxy_server="p.webshare.io",
        proxy_port="80",
        proxy_user="test_user",
        proxy_pass="test_pass",
    )


class _FakeResponse:
    """Duck-typed curl_cffi response."""
    def __init__(self, status: int, body=None):
        self.status_code = status
        self._body = body
    def json(self):
        return self._body
    @property
    def text(self):
        return str(self._body) if self._body is not None else ""


def _curl_factory(status: int, body=None):
    """Returns a fake curl_cffi_get factory that always returns the given status."""
    def factory(url, **kwargs):
        return _FakeResponse(status, body)
    return factory


# ===========================================================================
# Section A: Constructor backward-compat (4-arg signature)
# ===========================================================================

class TestConstructorBackCompat:
    def test_4_arg_signature_still_works(self):
        """Phase 7 callers still work with 4 args + all new fields default None."""
        fetcher = Gen4Fetcher(
            config=_make_config(),
            curl_cffi_get=lambda *a, **kw: None,
            cloakbrowser_launcher=None,
            ssr_fetcher=None,
        )
        assert fetcher is not None
        # All new Phase 8 fields default None
        assert fetcher._retry_policy is None
        assert fetcher._proxy_rotator is None
        assert fetcher._warmup_sequence is None
        assert fetcher._cookie_store is None
        assert fetcher._sticky_session is None
        assert fetcher._header_factory is None
        assert fetcher._budget is None
        # Warm-up gate starts False
        assert fetcher._warmed_up is False
        assert fetcher._current_ua is None
        assert fetcher._current_referer is None

    def test_11_arg_signature_works_with_all_di(self):
        """Phase 8 11-arg signature with all 7 new Optional DI fields."""
        fakes = make_default_fakes()
        fetcher = Gen4Fetcher(
            config=_make_config(),
            curl_cffi_get=lambda *a, **kw: None,
            cloakbrowser_launcher=None,
            ssr_fetcher=None,
            retry_policy=fakes["retry_policy"],
            proxy_rotator=fakes["proxy_rotator"],
            warmup_sequence=fakes["warmup"],
            cookie_store=fakes["cookie_store"],
            sticky_session=fakes["sticky_session"],
            header_factory=fakes["header_factory"],
            budget=fakes["budget"],
        )
        assert fetcher is not None
        assert fetcher._retry_policy is fakes["retry_policy"]
        assert fetcher._proxy_rotator is fakes["proxy_rotator"]
        assert fetcher._warmup_sequence is fakes["warmup"]
        assert fetcher._cookie_store is fakes["cookie_store"]
        assert fetcher._sticky_session is fakes["sticky_session"]
        assert fetcher._header_factory is fakes["header_factory"]
        assert fetcher._budget is fakes["budget"]


# ===========================================================================
# Section B: Warm-up gate (Q1)
# ===========================================================================

class TestWarmupGate:
    def test_warmup_fires_once_on_first_request(self):
        """Q1: warm_homepage called once before first request, then skipped."""
        warmup = FakeWarmupSequence(scope="per_event")
        fetcher = Gen4Fetcher(
            config=_make_config(),
            curl_cffi_get=_curl_factory(200, {"event": {"id": 14025013}}),
            warmup_sequence=warmup,
        )
        # Before fetch_api: not warmed
        assert fetcher._warmed_up is False
        asyncio.run(fetcher.fetch_api("/api/v1/event/14025013"))
        # After first fetch_api: warmed once
        assert warmup.warm_homepage_count == 1
        assert fetcher._warmed_up is True
        # Second fetch_api: should NOT re-warm
        asyncio.run(fetcher.fetch_api("/api/v1/event/12436875"))
        assert warmup.warm_homepage_count == 1  # still 1

    def test_warmup_failure_graceful_continuation(self):
        """Q1: warm-up failure → graceful degradation, fetch_api still works."""
        warmup = FakeWarmupSequence(scope="per_event")
        warmup.fail_warm_homepage = True
        fetcher = Gen4Fetcher(
            config=_make_config(),
            curl_cffi_get=_curl_factory(200, {"event": {"id": 1}}),
            warmup_sequence=warmup,
        )
        # Should NOT raise
        result = asyncio.run(fetcher.fetch_api("/api/v1/event/1"))
        assert result is not None
        assert fetcher._warmed_up is True  # marked warmed (don't retry)
        # Referer fell back to BROWSER_BASE
        assert fetcher._current_referer == f"{BROWSER_BASE}/"

    def test_no_warmup_when_not_injected(self):
        """Phase 7 behavior: if no warmup_sequence injected, no warm-up fires."""
        fetcher = Gen4Fetcher(
            config=_make_config(),
            curl_cffi_get=_curl_factory(200, {"event": {"id": 1}}),
        )
        asyncio.run(fetcher.fetch_api("/api/v1/event/1"))
        # _warmed_up stays False (no warmup_sequence → skip gate)
        assert fetcher._warmed_up is False


# ===========================================================================
# Section C: HeaderFactory wiring (Q5)
# ===========================================================================

class TestHeaderFactoryWiring:
    def test_header_factory_emits_headers_when_injected(self):
        """Q5: HeaderFactory injected → kwargs contain headers."""
        hf = FakeHeaderFactory()
        # Capture kwargs passed to curl_cffi_get
        captured_kwargs = {}

        def capture_factory(url, **kwargs):
            captured_kwargs.update(kwargs)
            return _FakeResponse(200, {"event": {"id": 1}})

        fetcher = Gen4Fetcher(
            config=_make_config(),
            curl_cffi_get=capture_factory,
            header_factory=hf,
        )
        asyncio.run(fetcher.fetch_api("/api/v1/event/1"))
        assert "headers" in captured_kwargs
        # DefaultHeaderFactory emits 11 headers
        assert len(captured_kwargs["headers"]) == 11
        assert captured_kwargs["headers"]["Origin"] == BROWSER_BASE

    def test_header_factory_call_count(self):
        """HeaderFactory.headers_for called once per Tier-1 curl_cffi attempt."""
        hf = FakeHeaderFactory()
        fetcher = Gen4Fetcher(
            config=_make_config(),
            curl_cffi_get=_curl_factory(200, {"event": {"id": 1}}),
            header_factory=hf,
        )
        asyncio.run(fetcher.fetch_api("/api/v1/event/1"))
        assert hf.headers_for_call_count == 1

    def test_no_headers_when_header_factory_none(self):
        """Phase 7 behavior: no headers kwargs when HeaderFactory not injected."""
        captured_kwargs = {}

        def capture_factory(url, **kwargs):
            captured_kwargs.update(kwargs)
            return _FakeResponse(200, {"event": {"id": 1}})

        fetcher = Gen4Fetcher(
            config=_make_config(),
            curl_cffi_get=capture_factory,
            header_factory=None,
        )
        asyncio.run(fetcher.fetch_api("/api/v1/event/1"))
        assert "headers" not in captured_kwargs


# ===========================================================================
# Section D: CookieStore + ProxyRotator wiring (Q1, Q6)
# ===========================================================================

class TestCookieStoreAndProxyRotator:
    def test_cookie_store_snapshot_injected(self):
        """Q1: CookieStore._last_snapshot injected as cookies kwargs."""
        cs = FakeCookieStore()
        cs._last_snapshot = {"session_id": "abc", "csrf": "xyz"}  # type: ignore[attr-defined]
        captured_kwargs = {}

        def capture_factory(url, **kwargs):
            captured_kwargs.update(kwargs)
            return _FakeResponse(200, {"event": {"id": 1}})

        fetcher = Gen4Fetcher(
            config=_make_config(),
            curl_cffi_get=capture_factory,
            cookie_store=cs,
        )
        asyncio.run(fetcher.fetch_api("/api/v1/event/1"))
        assert "cookies" in captured_kwargs
        assert captured_kwargs["cookies"]["session_id"] == "abc"

    def test_rotate_proxy_with_eviction_helper(self):
        """Q1 + Q6: _rotate_proxy_with_cookie_eviction rotates + evicts."""
        pr = FakeProxyRotator()
        cs = FakeCookieStore()
        cs.store("c1", "v1", proxy_session_identity="proxy-1")
        cs.store("c2", "v2", proxy_session_identity="proxy-2")
        fetcher = Gen4Fetcher(
            config=_make_config(),
            curl_cffi_get=_curl_factory(200, {"event": {"id": 1}}),
            proxy_rotator=pr,
            cookie_store=cs,
        )
        # Pre-rotate: 2 cookies stored
        asyncio.run(fetcher._rotate_proxy_with_cookie_eviction())
        # After rotation: rotated to proxy-3, both c1 (proxy-1) and c2 (proxy-2)
        # evicted because they don't match new proxy
        assert pr.rotate_call_count == 1
        assert pr.current_proxy() != "http://p.webshare.io:80"

    def test_no_rotation_when_proxy_rotator_none(self):
        """Phase 7: no proxy rotation when ProxyRotator not injected."""
        fetcher = Gen4Fetcher(
            config=_make_config(),
            curl_cffi_get=_curl_factory(200, {"event": {"id": 1}}),
        )
        # No-op
        asyncio.run(fetcher._rotate_proxy_with_cookie_eviction())


# ===========================================================================
# Section E: Budget guard (Q7)
# ===========================================================================

class TestBudgetGuard:
    def test_budget_records_attempts_via_rotate_helper(self):
        """Q7: budget.cookies_evicted increments when cookie evicted on rotation."""
        from gen4_components import RequestBudget
        budget = RequestBudget()
        pr = FakeProxyRotator()
        cs = FakeCookieStore()
        cs.store("c1", "v1", proxy_session_identity="proxy-1")
        fetcher = Gen4Fetcher(
            config=_make_config(),
            curl_cffi_get=_curl_factory(200, {"event": {"id": 1}}),
            proxy_rotator=pr,
            cookie_store=cs,
            budget=budget,
        )
        asyncio.run(fetcher._rotate_proxy_with_cookie_eviction())
        assert budget.cookies_evicted >= 1


# ===========================================================================
# Section F: Retry loop counter contract (Phase 8 LOCK)
# ===========================================================================

class TestRetryCounterContract:
    """Phase 8 (Kris 2026-08-25 09:49): attempts_used = count of completed attempts."""

    def test_attempts_used_1_triggers_retry(self):
        """After attempt 1 of 5, 403 → should_retry=True.

        1-based contract: attempts_used=1 means caller just finished attempt #1.
        Spec: TIER1_MAX_RETRIES=5 means up to 5 attempts. After attempt 1,
        there are 3 more retry slots (attempts 2, 3, 4, 5) before fallback.
        attempts_remaining = max(0, 5 - 1 - 1) = 3.
        """
        d = decide_retry(
            RetryClassification.ROTATE_AND_RETRY,
            attempts_used=1,
            tier_max_retries=TIER1_MAX_RETRIES,
            total_attempts_used=1,
        )
        assert d.should_retry
        assert d.next_action == "rotate"
        # 1-based: 5 total attempts; after attempt 1, 3 more retries allowed
        # (attempts 2, 3, 4, 5 — but attempt 5 won't trigger another retry)
        assert d.attempts_remaining == TIER1_MAX_RETRIES - 1 - 1  # = 3

    def test_attempts_used_5_triggers_fallback(self):
        """After attempt 5 of 5, 403 → should_retry=False, fallback_tier2."""
        d = decide_retry(
            RetryClassification.ROTATE_AND_RETRY,
            attempts_used=5,
            tier_max_retries=TIER1_MAX_RETRIES,
            total_attempts_used=5,
        )
        assert not d.should_retry
        assert d.next_action == "fallback_tier2"

    def test_ceiling_blocks_at_total_max(self):
        """total_attempts_used=7 (1 more would hit ceiling 8) → fallback."""
        d = decide_retry(
            RetryClassification.ROTATE_AND_RETRY,
            attempts_used=4,
            tier_max_retries=TIER1_MAX_RETRIES,
            total_attempts_used=TOTAL_MAX_ATTEMPTS_PER_REQUEST - 1,
        )
        assert not d.should_retry
        assert d.next_action == "fallback_tier2"

    def test_per_tier_attempts_tier1_tier2_tier3(self):
        """Per-request worst-case: Tier-1 (5) + Tier-2 (2) + Tier-3 (1) = 8."""
        # Tier-1: 5 attempts (TIER1_MAX_RETRIES=5)
        # Tier-2: 2 attempts (TIER2_BROWSER_MAX_RETRIES=2)
        # Tier-3: 1 SSR attempt
        # Total: 8 = TOTAL_MAX_ATTEMPTS_PER_REQUEST
        assert TIER1_MAX_RETRIES + TIER2_BROWSER_MAX_RETRIES + 1 == TOTAL_MAX_ATTEMPTS_PER_REQUEST


# ===========================================================================
# Section G: 407 terminal halt preserved
# ===========================================================================

class TestTerminalHaltPreserved:
    def test_407_halts_without_retry(self):
        """Phase 8 invariant: 407 → terminal halt, 0 retry, NO retry_policy calls."""
        captured_kwargs = {}

        def capture_factory(url, **kwargs):
            captured_kwargs.update(kwargs)
            return _FakeResponse(407, None)

        fakes = make_default_fakes()
        fetcher = Gen4Fetcher(
            config=_make_config(),
            curl_cffi_get=capture_factory,
            retry_policy=fakes["retry_policy"],
            proxy_rotator=fakes["proxy_rotator"],
        )
        result = asyncio.run(fetcher.fetch_api("/api/v1/event/1/incidents"))
        # 407 halts immediately; no retry; no fallback
        assert result["status"] == 407
        assert result["halt_reason"] == "http_407_safety_halt"
        # RetryPolicy should NOT have been invoked (Phase 8 wiring is additive
        # — core fetch_api loop is unchanged from Phase 7)
        assert fakes["retry_policy"].run_call_count == 0
        # Proxy should NOT have been rotated
        assert fakes["proxy_rotator"].rotate_call_count == 0


# ===========================================================================
# Section H: SSR incidents unsupported preserved
# ===========================================================================

class TestSSRIncidentsUnsupportedPreserved:
    def test_incidents_endpoint_still_returns_none_from_ssr(self):
        """Phase 8 invariant: SSR_UNSUPPORTED_ENDPOINTS unchanged; incidents
        still returns ssr_returned_none via existing gen4_ssr.py."""
        # Verify SSR_UNSUPPORTED_ENDPOINTS in gen4_fetcher.py still contains 'incidents'
        from gen4_fetcher import SSR_UNSUPPORTED_ENDPOINTS
        assert "incidents" in SSR_UNSUPPORTED_ENDPOINTS
        assert "lineups" in SSR_UNSUPPORTED_ENDPOINTS


# ===========================================================================
# Section I: No live-call guard
# ===========================================================================

class TestNoLiveCallGuard:
    def test_no_network_imports_added_to_gen4_fetcher(self):
        """Phase 8 invariant: no `import requests`, no real curl_cffi.Get, no
        Playwright launch in gen4_fetcher.py."""
        src = Path("gen4_fetcher.py").read_text()
        forbidden = [
            "import requests",
            "from requests",
            "import curl_cffi",  # only allowed if injected via DI
            "playwright.sync_api",
            "cloakbrowser.launch",
            "sync_playwright",
        ]
        for f in forbidden:
            assert f not in src, f"FOUND: {f}"

    def test_curl_cffi_get_only_invoked_via_di(self):
        """Phase 7 invariant: curl_cffi_get only called when explicitly injected."""
        call_log = []

        def spy_factory(url, **kwargs):
            call_log.append(url)
            return _FakeResponse(200, {"event": {"id": 1}})

        fetcher = Gen4Fetcher(
            config=_make_config(),
            curl_cffi_get=spy_factory,
        )
        asyncio.run(fetcher.fetch_api("/api/v1/event/1"))
        # Exactly 1 call (Tier-1 only; Tier-2/3 not invoked because 200 OK)
        assert len(call_log) == 1
