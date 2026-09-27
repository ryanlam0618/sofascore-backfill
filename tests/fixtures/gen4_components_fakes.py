"""gen4_components_fakes.py — Fake fixtures for offline testing of Gen4 components.

Phase 7 APPROVED scope: fake fixtures, no live requests, no real browser.
Each fake implements a Protocol from gen4_components.py and tracks call
counts + state for test inspection.

All fakes are async-safe where Protocol demands async, but use no real I/O.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from gen4_components import (
    BROWSER_BASE,
    COOKIE_TTL_SECONDS,
    RetryClassification,
    RetryDecision,
    TIER1_MAX_RETRIES,
    classify_attempt,
    decide_retry,
    make_cookie_identity,
)


# ---------------------------------------------------------------------------
# FakeProxyRotator
# ---------------------------------------------------------------------------

class FakeProxyRotator:
    """Fake ProxyRotator: returns canned proxy URLs; tracks rotate() calls."""

    def __init__(
        self,
        proxies: Optional[List[str]] = None,
        rotate_delay_seconds: float = 0.0,
        initial_proxy: Optional[str] = None,
    ) -> None:
        self._proxies = proxies or [
            "http://p.webshare.io:80",
            "http://p.webshare.io:81",
            "http://p.webshare.io:82",
        ]
        self._index = 0
        # explicit None → no proxy (direct); otherwise default to first
        if initial_proxy is None and proxies is None:
            self._current: Optional[str] = self._proxies[0]
        else:
            self._current = initial_proxy
        self.rotate_call_count = 0
        self.rotate_delay_seconds = rotate_delay_seconds

    def current_proxy(self) -> Optional[str]:
        return self._current

    async def rotate(self) -> None:
        self.rotate_call_count += 1
        self._index = (self._index + 1) % len(self._proxies)
        self._current = self._proxies[self._index]
        if self.rotate_delay_seconds > 0:
            await asyncio.sleep(self.rotate_delay_seconds)

    def proxy_dict(self) -> Optional[Dict[str, str]]:
        if self._current is None:
            return None
        return {"http": self._current, "https": self._current}


# ---------------------------------------------------------------------------
# FakeWarmupSequence
# ---------------------------------------------------------------------------

class FakeWarmupSequence:
    """Fake WarmupSequence: records warm-up calls; returns canned URLs."""

    def __init__(self, scope: str = "per_event") -> None:
        self._scope = scope
        self._last_referer: Optional[str] = None
        self.warm_homepage_count = 0
        self.warm_tournament_count = 0
        self.warm_event_count = 0
        self.warm_tournament_calls: List[Dict[str, Any]] = []
        self.warm_event_calls: List[int] = []
        self.fail_warm_homepage = False
        self.fail_warm_tournament = False
        self.fail_warm_event = False

    @property
    def scope(self) -> str:
        return self._scope

    @property
    def last_referer_url(self) -> Optional[str]:
        return self._last_referer

    async def warm_homepage(self) -> None:
        self.warm_homepage_count += 1
        if self.fail_warm_homepage:
            raise RuntimeError("FakeWarmupSequence.warm_homepage failed")

    async def warm_tournament(
        self,
        *,
        ut_id: int,
        season_id: int,
        country_slug: str = "",
        competition_slug: str = "",
    ) -> str:
        self.warm_tournament_count += 1
        self.warm_tournament_calls.append({
            "ut_id": ut_id,
            "season_id": season_id,
            "country_slug": country_slug,
            "competition_slug": competition_slug,
        })
        if self.fail_warm_tournament:
            raise RuntimeError("FakeWarmupSequence.warm_tournament failed")
        url = (
            f"{BROWSER_BASE}/football/tournament/"
            f"{country_slug}/{competition_slug}/{ut_id}#id:{season_id}"
        )
        self._last_referer = url
        return url

    async def warm_event(self, event_id: int) -> None:
        self.warm_event_count += 1
        self.warm_event_calls.append(event_id)
        if self.fail_warm_event:
            raise RuntimeError("FakeWarmupSequence.warm_event failed")


# ---------------------------------------------------------------------------
# FakeCookieStore
# ---------------------------------------------------------------------------

class FakeCookieStore:
    """Fake CookieStore: in-memory dict with isolation enforcement.

    Eviction rules (Kris 2026-08-25 09:16):
      - evict_expired(): drop cookies past TTL
      - evict_by_proxy(proxy_id): drop cookies bound to another proxy IP
    """

    def __init__(self, ttl_seconds: int = COOKIE_TTL_SECONDS) -> None:
        self._cookies: Dict[str, Tuple[Dict[str, str], float, str]] = {}
        # value: (cookie_dict, expires_at, proxy_session_identity)
        self._ttl = ttl_seconds
        self.capture_call_count = 0
        self.inject_call_count = 0
        self.evict_expired_call_count = 0
        self.evict_by_proxy_call_count = 0
        # canned cookies to return from capture_from_context
        self.canned_capture: Dict[str, str] = {
            "session_id": "abc123",
            "csrf_token": "xyz789",
        }

    async def capture_from_context(self, browser_context: Any) -> Dict[str, str]:
        self.capture_call_count += 1
        # Fake: simulate 1-second capture overhead
        await asyncio.sleep(0)
        return dict(self.canned_capture)

    def inject(self, request_kwargs: Dict[str, Any], cookie_dict: Dict[str, str]) -> None:
        self.inject_call_count += 1
        request_kwargs["cookies"] = cookie_dict

    def is_expired(self, cookie_name: str) -> bool:
        if cookie_name not in self._cookies:
            return True
        _, expires_at, _ = self._cookies[cookie_name]
        return time.time() >= expires_at

    def evict_expired(self) -> int:
        self.evict_expired_call_count += 1
        now = time.time()
        expired = [k for k, (_, exp, _) in self._cookies.items() if now >= exp]
        for k in expired:
            del self._cookies[k]
        return len(expired)

    def evict_by_proxy(self, proxy_session_identity: str) -> int:
        """Evict cookies bound to a SPECIFIC proxy identity (Q1 isolation).

        Note: when caller rotates to a NEW proxy, they should call this
        with the NEW proxy identity to evict any cookies left from a prior
        proxy that may have leaked in. Default behavior: evict nothing
        unless caller specifies a different identity than what's stored.
        """
        self.evict_by_proxy_call_count += 1
        evicted = 0
        for k in list(self._cookies.keys()):
            _, _, stored_proxy = self._cookies[k]
            if stored_proxy != proxy_session_identity:
                del self._cookies[k]
                evicted += 1
        return evicted

    def store(self, name: str, value: str, proxy_session_identity: str) -> None:
        """Test helper: store a cookie with a proxy binding."""
        self._cookies[name] = (
            {name: value},
            time.time() + self._ttl,
            proxy_session_identity,
        )


# ---------------------------------------------------------------------------
# FakeStickySessionKey
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FakeStickySessionKey:
    """Fake StickySessionKey: per_competition scope by default (Q3)."""
    key: Optional[str] = None

    def matches(self, other: "FakeStickySessionKey") -> bool:
        if self.key is None and other.key is None:
            return True
        if self.key is None or other.key is None:
            return False
        return self.key == other.key

    def __str__(self) -> str:
        return f"FakeStickySessionKey({self.key!r})"


# ---------------------------------------------------------------------------
# FakeHeaderFactory
# ---------------------------------------------------------------------------

class FakeHeaderFactory:
    """Fake HeaderFactory: returns canned headers; tracks call count."""

    def __init__(
        self,
        canned_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self._canned = canned_headers or {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": BROWSER_BASE,
            "Referer": BROWSER_BASE + "/",
            "User-Agent": "FakeUA/1.0",
            "Sec-CH-UA": '"Fake";v="1"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"Linux"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        self.headers_for_call_count = 0
        self.last_call_kwargs: Optional[Dict[str, str]] = None

    def headers_for(
        self,
        *,
        user_agent: str,
        referer: str,
        origin: str = BROWSER_BASE,
    ) -> Dict[str, str]:
        self.headers_for_call_count += 1
        self.last_call_kwargs = {
            "user_agent": user_agent,
            "referer": referer,
            "origin": origin,
        }
        result = dict(self._canned)
        result["User-Agent"] = user_agent
        result["Referer"] = referer
        result["Origin"] = origin
        return result


# ---------------------------------------------------------------------------
# FakeRetryPolicy
# ---------------------------------------------------------------------------

class FakeRetryPolicy:
    """Fake RetryPolicy: runs operation up to tier_max_retries.

    Uses gen4_components.classify_attempt + decide_retry to apply the
    state-machine. Sleep is mocked (no real sleep).
    """

    def __init__(
        self,
        tier_max_retries: int = TIER1_MAX_RETRIES,
        sleep_seconds: Tuple[float, float] = (0.0, 0.0),
        proxy_rotator: Optional[FakeProxyRotator] = None,
    ) -> None:
        self.tier_max_retries = tier_max_retries
        self.sleep_seconds = sleep_seconds
        self._proxy_rotator = proxy_rotator
        self.run_call_count = 0
        self.rotate_call_count = 0
        self.attempts_log: List[Dict[str, Any]] = []

    async def run_with_retry(
        self,
        operation: Callable[[], Awaitable[Any]],
        *,
        on_attempt: Optional[Callable[[int, Any], None]] = None,
    ) -> Optional[Any]:
        self.run_call_count += 1
        # CORRECTED 2026-08-25 09:49 (Phase 8 review C2): 1-based contract
        # per decide_retry docstring (LOCKED Phase 8). attempts_used = number
        # of attempts ALREADY MADE on this tier. We pass attempts_used
        # directly to decide_retry — no off-by-one adjustment.
        attempts_used = 0
        total_attempts = 0
        while True:
            total_attempts += 1
            if total_attempts > 20:  # safety fuse for tests
                return None
            result = await operation()
            attempts_used += 1
            # Inspect result: support duck-typed attempt result
            status = getattr(result, "status", None)
            if status is None and isinstance(result, dict):
                status = result.get("status", 0)
            exception = getattr(result, "exception", None)
            body_valid = getattr(result, "body_valid", None)
            classification = classify_attempt(
                status=status or 0,
                body_valid=body_valid,
                exception=exception,
            )
            self.attempts_log.append({
                "attempt": attempts_used,
                "status": status,
                "classification": classification.value,
            })
            if on_attempt:
                on_attempt(attempts_used, result)
            decision = decide_retry(
                classification,
                attempts_used=attempts_used,  # 1-based: count of completed attempts
                tier_max_retries=self.tier_max_retries,
                total_attempts_used=total_attempts,
            )
            if not decision.should_retry:
                if classification == RetryClassification.SUCCESS:
                    return result
                return None
            if self._proxy_rotator is not None:
                await self._proxy_rotator.rotate()
                self.rotate_call_count += 1
            # no real sleep (sleep_seconds mocked to 0)


# ---------------------------------------------------------------------------
# Convenience builders
# ---------------------------------------------------------------------------

def make_default_fakes(
    *,
    scope: str = "per_event",
    with_proxy_rotator: bool = True,
    with_cookie_store: bool = True,
    with_warmup: bool = True,
    with_header_factory: bool = True,
    with_retry_policy: bool = True,
    with_sticky_session: bool = True,
) -> Dict[str, Any]:
    """Build a default set of fakes wired together.

    Returns dict with all 6 component fakes + a budget tracker.
    """
    fakes: Dict[str, Any] = {}
    if with_proxy_rotator:
        fakes["proxy_rotator"] = FakeProxyRotator()
    if with_warmup:
        fakes["warmup"] = FakeWarmupSequence(scope=scope)
    if with_cookie_store:
        fakes["cookie_store"] = FakeCookieStore()
    if with_sticky_session:
        fakes["sticky_session"] = FakeStickySessionKey(
            key="competition-10431-A-League"
        )
    if with_header_factory:
        fakes["header_factory"] = FakeHeaderFactory()
    if with_retry_policy:
        fakes["retry_policy"] = FakeRetryPolicy(
            tier_max_retries=TIER1_MAX_RETRIES,
            proxy_rotator=fakes.get("proxy_rotator"),
        )
    # budget
    from gen4_components import RequestBudget
    fakes["budget"] = RequestBudget()
    return fakes


__all__ = [
    "FakeProxyRotator",
    "FakeWarmupSequence",
    "FakeCookieStore",
    "FakeStickySessionKey",
    "FakeHeaderFactory",
    "FakeRetryPolicy",
    "make_default_fakes",
]
