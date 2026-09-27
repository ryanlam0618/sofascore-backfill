"""gen4_components.py — Reusable components for Gen4 fetcher refactor (Phase 7).

Phase 7 APPROVED by Kris 2026-08-25 09:16 GMT+8. Scope:
  - Interfaces / protocols (this file)
  - Fake fixtures (tests/fixtures/gen4_components_fakes.py)
  - Offline tests (tests/test_gen4_components.py)
  - Static checks + request budget calculation

FORBIDDEN in Phase 7 (Kris 2026-08-25 09:16):
  - Real proxy requests
  - New canary runs
  - Gen2 default change
  - Full backfill
  - Production rollout
  - SSR_UNSUPPORTED_ENDPOINTS changes
  - Live browser launch

This module is OFFLINE ONLY — protocols, dataclasses, pure helpers. No network,
no async I/O at import time, no Playwright/CloakBrowser instantiation.

Architecture (7 decisions from Kris 2026-08-25 09:16):
  Q1: warm-up strategy "per_event" (default) with reserved "per_competition" toggle
  Q2: browser retry max 2
  Q3: sticky session per_competition
  Q4: cookie TTL 30 minutes (cap, not guarantee)
  Q5: shared helpers additive extraction; backward-compat wrappers preserve
      Gen2 behaviour (anti_block.py is the shared helper source of truth)
  Q6: browser context per-competition reuse; rebuild on EPIPE / target_closed /
      proxy change
  Q7: local retry/quota budget guard (no external quota API)

Retry state-machine (Kris 2026-08-25 09:16):
  407 → terminal halt, 0 retry
  403 → rotate proxy, bounded retry (Gen2 default 5, configurable)
  404 → endpoint review, NO blind retry
  timeout / connection error → max 2 retry
  200 + invalid payload → validation, then fallback / retry

Cookie isolation tuple (Kris 2026-08-25 09:16):
  (competition_id, proxy_session_identity, user_agent_identity, created_at, expires_at)
  Do NOT carry cookies from one proxy IP to another proxy IP.

Backward-compatibility invariants (must NOT break):
  - anti_block.py public API unchanged
  - Gen2 backfill_runner.py behavior unchanged (verified via existing tests)
  - Gen4Fetcher existing 4-arg signature preserved
  - gen4_phase4_live_smoke.py still works (verified via 11/11 DI tests)
"""
from __future__ import annotations

import hashlib
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Iterable,
    Mapping,
    Optional,
    Protocol,
    Tuple,
)

# Reuse Gen2 helpers — DO NOT reimplement. backward-compat wrapper at end.
try:
    from anti_block import (
        PLATFORM_MAP,
        USER_AGENTS,
        get_platform,
        get_sec_ch_ua,
    )
except ImportError as _exc:  # pragma: no cover
    # offline tests may run without anti_block.py on PYTHONPATH
    PLATFORM_MAP = {"Windows": '"Windows"', "Macintosh": '"macOS"', "X11": '"Linux"'}
    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    ]
    def get_platform(ua: str) -> str:  # type: ignore[no-redef]
        return "Windows" if "Windows" in ua else "Linux"
    def get_sec_ch_ua(ua: str) -> str:  # type: ignore[no-redef]
        return '"Chromium";v="124", "Google Chrome";v="124"'


# ---------------------------------------------------------------------------
# 0. Shared constants (Q5: additive, anti_block unchanged)
# ---------------------------------------------------------------------------

BROWSER_BASE = "https://www.sofascore.com"

# Cookie isolation (Q4): explicit 30-min cap. Not a hard guarantee — the
# CookieStore may clear sooner on rotation, but never store longer than this.
COOKIE_TTL_SECONDS = 30 * 60

# Retry budgets (Q2, Q7)
TIER1_MAX_RETRIES = 5             # Gen2 baseline for curl_cffi
TIER2_BROWSER_MAX_RETRIES = 2     # Kris 2026-08-25 09:16: "browser retry 最多 2 次"
TIER3_TIMEOUT_MAX_RETRIES = 2     # Kris 2026-08-25 09:16: timeout/conn-error ≤ 2
TOTAL_MAX_ATTEMPTS_PER_REQUEST = 8  # hard ceiling to bound quota burn


# ---------------------------------------------------------------------------
# 1. Retry policy state-machine
# ---------------------------------------------------------------------------

class RetryClassification(str, Enum):
    """Classify an attempt outcome to drive retry policy.

    Maps to Kris 2026-08-25 09:16 retry state-machine:
      - TERMINAL_HALT      → 407 (proxy auth failure; do NOT retry)
      - ROTATE_AND_RETRY   → 403 (rotate proxy + bounded retry)
      - ENDPOINT_REVIEW    → 404 (do NOT blindly retry; flag for review)
      - TRANSIENT_RETRY    → timeout / connection error (≤ 2 retry)
      - VALIDATE_THEN_RETRY → 200 + invalid payload (validate, then fallback)
      - SUCCESS            → 200 + valid payload (no retry)
    """
    TERMINAL_HALT = "terminal_halt"      # 407
    ROTATE_AND_RETRY = "rotate_and_retry"  # 403
    ENDPOINT_REVIEW = "endpoint_review"   # 404
    TRANSIENT_RETRY = "transient_retry"   # timeout / connection error
    VALIDATE_THEN_RETRY = "validate_then_retry"  # 200 + invalid
    SUCCESS = "success"                   # 200 + valid


def classify_attempt(
    *,
    status: int,
    body_valid: Optional[bool] = None,
    exception: Optional[str] = None,
) -> RetryClassification:
    """Map an HTTP attempt outcome to retry classification.

    Args:
      status: HTTP status code. 0 indicates transport error.
      body_valid: Only meaningful when status == 200. None = unknown
                  (treat as validate_then_retry so caller re-runs validation).
      exception: Optional exception class name (e.g. "TimeoutError",
                 "ConnectionError"). Non-empty implies TRANSIENT_RETRY.

    Returns: RetryClassification enum value.
    """
    if exception:
        return RetryClassification.TRANSIENT_RETRY
    if status == 0:
        return RetryClassification.TRANSIENT_RETRY
    if status == 407:
        return RetryClassification.TERMINAL_HALT
    if status == 403:
        return RetryClassification.ROTATE_AND_RETRY
    if status == 404:
        return RetryClassification.ENDPOINT_REVIEW
    if status == 200:
        if body_valid is True:
            return RetryClassification.SUCCESS
        if body_valid is False or body_valid is None:
            return RetryClassification.VALIDATE_THEN_RETRY
    # Other status (5xx, etc.): treat as transient
    return RetryClassification.TRANSIENT_RETRY


@dataclass(frozen=True)
class RetryDecision:
    """Result of applying retry policy to one classification."""
    classification: RetryClassification
    should_retry: bool
    next_action: str  # "none" | "rotate" | "fallback_tier2" | "fallback_tier3" | "halt"
    attempts_remaining: int

    def __post_init__(self) -> None:
        valid_actions = {"none", "rotate", "fallback_tier2", "fallback_tier3", "halt"}
        if self.next_action not in valid_actions:
            raise ValueError(f"next_action must be one of {valid_actions}")


def decide_retry(
    classification: RetryClassification,
    *,
    attempts_used: int,
    tier_max_retries: int,
    total_attempts_used: int,
) -> RetryDecision:
    """Apply state-machine to decide whether to retry, fallback, or halt.

    Args:
      classification: from classify_attempt()
      attempts_used: attempts ALREADY MADE on the CURRENT tier. Counter is
                     1-based: caller has completed `attempts_used` attempts
                     and is asking "should I attempt #(attempts_used+1)?".
                     Example: just finished attempt 1 of 5 → pass
                     attempts_used=1. Just finished attempt 5 of 5 → pass
                     attempts_used=5 → returns fallback.
      tier_max_retries: max attempts on the current tier (e.g. TIER1_MAX_RETRIES=5)
      total_attempts_used: total attempts across all tiers so far (1-based)

    Returns: RetryDecision with should_retry + next_action.

    Invariants:
      - total_attempts_used < TOTAL_MAX_ATTEMPTS_PER_REQUEST enforced.
      - attempts_used semantics: 1-based, count of completed attempts on tier.
      - Caller convention (LOCKED Phase 8, Kris 2026-08-25 09:49): after each
        attempt, caller passes attempts_used = count of attempts SO FAR on
        the current tier. NO off-by-one adjustment by caller.

    Examples (TIER1_MAX_RETRIES=5):
      - Just finished attempt 1, 403 → attempts_used=1 → should_retry=True
      - Just finished attempt 5, 403 → attempts_used=5 → should_retry=False,
        next_action=fallback_tier2 (5 attempts exhausted)
      - Just finished attempt 3, total_attempts=7 → next attempt would hit
        ceiling 8 → should_retry=False, fallback_tier2
    """
    if classification == RetryClassification.SUCCESS:
        return RetryDecision(
            classification=classification,
            should_retry=False,
            next_action="none",
            attempts_remaining=0,
        )
    if classification == RetryClassification.TERMINAL_HALT:
        # 407 → terminal halt (Kris 2026-08-25 09:16)
        return RetryDecision(
            classification=classification,
            should_retry=False,
            next_action="halt",
            attempts_remaining=0,
        )
    if classification == RetryClassification.ENDPOINT_REVIEW:
        # 404 → NO blind retry (Kris 2026-08-25 09:16)
        # We flag for review but DO NOT escalate to next tier automatically
        # — caller decides whether to treat 404 as soft failure.
        return RetryDecision(
            classification=classification,
            should_retry=False,
            next_action="none",
            attempts_remaining=0,
        )
    if classification == RetryClassification.ROTATE_AND_RETRY:
        # 403 → rotate + bounded retry (Gen2 baseline = 5)
        if total_attempts_used + 1 >= TOTAL_MAX_ATTEMPTS_PER_REQUEST:
            return RetryDecision(
                classification=classification,
                should_retry=False,
                next_action="fallback_tier2",
                attempts_remaining=0,
            )
        if attempts_used >= tier_max_retries:
            return RetryDecision(
                classification=classification,
                should_retry=False,
                next_action="fallback_tier2",
                attempts_remaining=0,
            )
        return RetryDecision(
            classification=classification,
            should_retry=True,
            next_action="rotate",
            attempts_remaining=max(0, tier_max_retries - attempts_used - 1),
        )
    if classification == RetryClassification.TRANSIENT_RETRY:
        # timeout / connection error → max 2 (Kris 2026-08-25 09:16)
        transient_max = min(TIER3_TIMEOUT_MAX_RETRIES, tier_max_retries)
        if total_attempts_used + 1 >= TOTAL_MAX_ATTEMPTS_PER_REQUEST:
            return RetryDecision(
                classification=classification,
                should_retry=False,
                next_action="fallback_tier3",
                attempts_remaining=0,
            )
        if attempts_used >= transient_max:
            return RetryDecision(
                classification=classification,
                should_retry=False,
                next_action="fallback_tier2",
                attempts_remaining=0,
            )
        return RetryDecision(
            classification=classification,
            should_retry=True,
            next_action="rotate",
            attempts_remaining=max(0, transient_max - attempts_used - 1),
        )
    if classification == RetryClassification.VALIDATE_THEN_RETRY:
        # 200 + invalid payload → validate then fallback (one retry allowed)
        if attempts_used >= 1:
            return RetryDecision(
                classification=classification,
                should_retry=False,
                next_action="fallback_tier2",
                attempts_remaining=0,
            )
        return RetryDecision(
            classification=classification,
            should_retry=True,
            next_action="rotate",
            attempts_remaining=max(0, 1 - attempts_used),
        )
    # Should not reach here
    raise ValueError(f"unhandled classification: {classification}")


# ---------------------------------------------------------------------------
# 2. Cookie store with isolation tuple (Kris 2026-08-25 09:16)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CookieIdentity:
    """Identity tuple for cookie isolation. Cookies MUST be bound to:
      - competition_id: which competition
      - proxy_session_identity: which proxy IP / session
      - user_agent_identity: which UA (hashed; full UA not stored)
      - created_at: unix timestamp
      - expires_at: unix timestamp (capped at COOKIE_TTL_SECONDS)

    INVARIANT: cookies from one (competition_id, proxy_session_identity)
    pair MUST NOT carry to another pair (Kris 2026-08-25 09:16).
    """
    competition_id: str
    proxy_session_identity: str
    user_agent_identity: str
    created_at: float
    expires_at: float

    def is_expired(self, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.time()
        return now >= self.expires_at

    def matches(self, other: "CookieIdentity", *, require_ua_match: bool = True) -> bool:
        """Two identities match if they share competition + proxy + (optionally UA)."""
        if self.competition_id != other.competition_id:
            return False
        if self.proxy_session_identity != other.proxy_session_identity:
            return False
        if require_ua_match and self.user_agent_identity != other.user_agent_identity:
            return False
        return True


def make_cookie_identity(
    *,
    competition_id: str,
    proxy_session_identity: str,
    user_agent: str,
    created_at: Optional[float] = None,
    ttl_seconds: int = COOKIE_TTL_SECONDS,
) -> CookieIdentity:
    """Construct a CookieIdentity with hashed UA and explicit TTL cap.

    Q4 (Kris 2026-08-25 09:16): TTL is a cap, not a guarantee.
    """
    now = created_at if created_at is not None else time.time()
    ua_hash = hashlib.sha256(user_agent.encode("utf-8")).hexdigest()[:16]
    return CookieIdentity(
        competition_id=competition_id,
        proxy_session_identity=proxy_session_identity,
        user_agent_identity=ua_hash,
        created_at=now,
        expires_at=now + ttl_seconds,
    )


# ---------------------------------------------------------------------------
# 3. Component Protocols (interfaces — production impls come in Phase 8)
# ---------------------------------------------------------------------------

class ProxyRotator(Protocol):
    """Interface for proxy rotation.

    Replaces: Gen2 BackfillClient._current_proxy_url rotation logic
    Source: backfill_runner.py:973-1015 (rotate_context)
    """
    def current_proxy(self) -> Optional[str]:
        """Return currently-active proxy URL or None for direct."""
        ...

    async def rotate(self) -> None:
        """Switch to next proxy. May block / sleep."""
        ...

    def proxy_dict(self) -> Optional[Dict[str, str]]:
        """Return curl_cffi-compatible proxies dict, or None for direct."""
        ...


class WarmupSequence(Protocol):
    """Interface for warm-up sequence.

    Replaces: Gen2 BackfillClient.warm_homepage / warm_tournament / warm_event
    Source: backfill_runner.py:850-895
    Scope: per_event (Phase 7 default). per_competition reserved for Phase 9+.
    """
    @property
    def scope(self) -> str:
        """Return "per_event" or "per_competition" (Q1)."""
        ...

    @property
    def last_referer_url(self) -> Optional[str]:
        """URL captured by last warm_tournament call. Used as Referer source."""
        ...

    async def warm_homepage(self) -> None:
        """Load BROWSER_BASE, capture initial cookies."""
        ...

    async def warm_tournament(
        self,
        *,
        ut_id: int,
        season_id: int,
        country_slug: str = "",
        competition_slug: str = "",
    ) -> str:
        """Load tournament page; return URL for Referer use."""
        ...

    async def warm_event(self, event_id: int) -> None:
        """Load event page; finalize session."""
        ...


class CookieStore(Protocol):
    """Interface for cookie storage with isolation enforcement.

    Replaces: Gen2 cookie_dict injected per request
    Source: backfill_runner.py:1027-1052
    """
    async def capture_from_context(self, browser_context: Any) -> Dict[str, str]:
        """Read cookies from a Playwright browser context."""
        ...

    def inject(self, request_kwargs: Dict[str, Any], cookie_dict: Dict[str, str]) -> None:
        """Mutate request_kwargs to include cookies=cookie_dict."""
        ...

    def is_expired(self, cookie_name: str) -> bool:
        """Check whether a named cookie has crossed its TTL cap."""
        ...

    def evict_expired(self) -> int:
        """Remove all expired cookies. Returns count evicted."""
        ...

    def evict_by_proxy(self, proxy_session_identity: str) -> int:
        """Remove all cookies bound to a proxy identity (Q1 isolation rule).

        Invariant: caller MUST call this when proxy rotates to prevent cookie
        carryover from one IP to another (Kris 2026-08-25 09:16).
        Returns count evicted.
        """
        ...


class StickySessionKey(Protocol):
    """Interface for sticky session pinning.

    Replaces: Gen2 BackfillClient.sticky_session_key constructor arg
    Source: backfill_runner.py:551-553, 2233
    Default scope: per_competition (Kris 2026-08-25 09:16 Q3).
    """
    @property
    def key(self) -> Optional[str]:
        """Return the session key string, or None if no pinning."""
        ...

    def matches(self, other: "StickySessionKey") -> bool:
        """Two keys match if they pin to the same proxy/session."""
        ...

    def __str__(self) -> str:
        ...


class HeaderFactory(Protocol):
    """Interface for building request headers.

    Replaces: Gen2 BackfillClient._build_context_headers + full header dict
    Source: backfill_runner.py:621-628, 1033-1044
    Backward-compat: reuses anti_block.get_platform / get_sec_ch_ua
    """
    def headers_for(
        self,
        *,
        user_agent: str,
        referer: str,
        origin: str = BROWSER_BASE,
    ) -> Dict[str, str]:
        """Return 9-header dict for curl_cffi/requests call.

        Headers emitted (Gen2 baseline):
          Accept, Accept-Language, Origin, Referer, User-Agent,
          Sec-CH-UA, Sec-CH-UA-Mobile, Sec-CH-UA-Platform,
          Sec-Fetch-Dest, Sec-Fetch-Mode, Sec-Fetch-Site
        """
        ...


class RetryPolicy(Protocol):
    """Interface for retry policy execution.

    Wraps classify_attempt + decide_retry; provides the async retry loop
    with sleep + rotation between attempts.
    """
    tier_max_retries: int
    sleep_seconds: Tuple[float, float]

    async def run_with_retry(
        self,
        operation: Callable[[], Awaitable[Any]],
        *,
        on_attempt: Optional[Callable[[int, Any], None]] = None,
    ) -> Optional[Any]:
        """Run operation with retry; return result on SUCCESS, None if exhausted.

        on_attempt: optional callback(attempt_index, result) for observability.
        """
        ...


# ---------------------------------------------------------------------------
# 4. Default HeaderFactory implementation (reuses anti_block helpers)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DefaultHeaderFactory:
    """Production-quality HeaderFactory implementation.

    Reuses anti_block.get_platform / get_sec_ch_ua (Q5: additive extraction,
    backward-compat — anti_block.py is not modified).

    Emits 11 headers (slightly more than Gen2 baseline of 9 because Sec-CH-UA-
    Mobile is split out per Gen2 line 626):
      Accept, Accept-Language, Origin, Referer, User-Agent,
      Sec-CH-UA, Sec-CH-UA-Mobile, Sec-CH-UA-Platform,
      Sec-Fetch-Dest, Sec-Fetch-Mode, Sec-Fetch-Site
    """
    platform_override: Optional[str] = None  # for tests

    def headers_for(
        self,
        *,
        user_agent: str,
        referer: str,
        origin: str = BROWSER_BASE,
    ) -> Dict[str, str]:
        platform = self.platform_override or get_platform(user_agent)
        is_mobile = "Mobile" in user_agent or "Android" in user_agent or "iPhone" in user_agent
        return {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": origin,
            "Referer": referer,
            "User-Agent": user_agent,
            "Sec-CH-UA": get_sec_ch_ua(user_agent),
            "Sec-CH-UA-Mobile": "?1" if is_mobile else "?0",
            "Sec-CH-UA-Platform": PLATFORM_MAP.get(platform, '"Windows"'),
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }


# ---------------------------------------------------------------------------
# 5. Request budget calculator (Q7 — local counters, no external API)
# ---------------------------------------------------------------------------

@dataclass
class RequestBudget:
    """Local retry/quota budget guard.

    Q7 (Kris 2026-08-25 09:16): local counters, no external quota API.
    Tracks:
      - attempts (Tier-1 + Tier-2 + Tier-3)
      - proxy_rotations
      - cookies_evicted
      - 403_count, 404_count, 407_count, timeout_count
    Provides:
      - can_attempt() — True if within budget
      - record_attempt(status) — track outcome
      - report() — snapshot dict for diagnostics
    """
    total_attempts: int = 0
    proxy_rotations: int = 0
    cookies_evicted: int = 0
    n_403: int = 0
    n_404: int = 0
    n_407: int = 0
    n_timeout: int = 0
    n_200: int = 0
    max_attempts_per_request: int = TOTAL_MAX_ATTEMPTS_PER_REQUEST
    quota_warning_threshold: int = 80  # % of an arbitrary budget

    def can_attempt(self, attempts_used: int) -> bool:
        return attempts_used < self.max_attempts_per_request

    def record_attempt(
        self,
        *,
        status: int,
        is_timeout: bool = False,
        classification: Optional[RetryClassification] = None,
    ) -> None:
        self.total_attempts += 1
        if status == 200:
            self.n_200 += 1
        elif status == 403:
            self.n_403 += 1
        elif status == 404:
            self.n_404 += 1
        elif status == 407:
            self.n_407 += 1
        if is_timeout:
            self.n_timeout += 1

    def quota_warning(self, *, max_total: int) -> bool:
        """Return True if total attempts has crossed the warning threshold."""
        return self.total_attempts >= int(max_total * self.quota_warning_threshold / 100)

    def report(self) -> Dict[str, int]:
        return {
            "total_attempts": self.total_attempts,
            "proxy_rotations": self.proxy_rotations,
            "cookies_evicted": self.cookies_evicted,
            "n_200": self.n_200,
            "n_403": self.n_403,
            "n_404": self.n_404,
            "n_407": self.n_407,
            "n_timeout": self.n_timeout,
        }


# ---------------------------------------------------------------------------
# 6. Request budget estimator (for offline calculation)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RequestBudgetEstimate:
    """Static estimate of request budget per (event, endpoint) attempt.

    Used by offline tests and ops planning. NOT used at runtime.
    """
    tier1_max: int
    tier2_max: int
    tier3_max: int
    worst_case_total: int
    expected_success_total: int  # under nominal conditions

    @classmethod
    def nominal(cls) -> "RequestBudgetEstimate":
        """Nominal scenario: Tier-1 succeeds on attempt 1."""
        return cls(
            tier1_max=TIER1_MAX_RETRIES,
            tier2_max=TIER2_BROWSER_MAX_RETRIES,
            tier3_max=TIER3_TIMEOUT_MAX_RETRIES,
            worst_case_total=TOTAL_MAX_ATTEMPTS_PER_REQUEST,
            expected_success_total=1,  # Tier-1 first attempt 200 OK
        )


def estimate_batch_budget(
    *,
    n_events: int,
    n_endpoints_per_event: int = 4,
    avg_retries_per_request: float = 1.5,  # empirically 50% need retry
    include_warmup: bool = True,
) -> Dict[str, int]:
    """Estimate Webshare request budget for a batch of events.

    Q7 (Kris 2026-08-25 09:16): local calculation, no external API.

    CORRECTED 2026-08-25 09:49 (Phase 8 review C1): api_requests_upper_bound
    now uses TOTAL_MAX_ATTEMPTS_PER_REQUEST=8 ceiling, NOT TIER1_MAX_RETRIES=5.
    Per-request worst-case = Tier-1 (5) + Tier-2 (2) + Tier-3 (1) = 8 = ceiling.

    Returns dict with:
      - total_requests: requests to Webshare proxy (curl_cffi only;
        browser/SSR don't count toward Webshare quota)
      - warmup_requests: per-event warm-homepage/tournament/event overhead
      - total_with_warmup: total budget including warm-up
      - upper_bound: worst-case with all retries + all warm-ups
        = events × endpoints × 8 ceiling + warmup overhead
    """
    api_requests_per_event = int(n_endpoints_per_event * avg_retries_per_request)
    warmup_per_event = 3 if include_warmup else 0  # homepage + tournament + event
    total_api = n_events * api_requests_per_event
    total_warmup = n_events * warmup_per_event
    # CORRECTED: use TOTAL_MAX_ATTEMPTS_PER_REQUEST (8) as per-request ceiling
    upper_api = n_events * n_endpoints_per_event * TOTAL_MAX_ATTEMPTS_PER_REQUEST
    upper_warmup = total_warmup
    return {
        "n_events": n_events,
        "n_endpoints_per_event": n_endpoints_per_event,
        "avg_retries_per_request": avg_retries_per_request,
        "api_requests_estimate": total_api,
        "warmup_requests_estimate": total_warmup,
        "total_with_warmup_estimate": total_api + total_warmup,
        "api_requests_upper_bound": upper_api,
        "warmup_requests_upper_bound": upper_warmup,
        "total_upper_bound": upper_api + upper_warmup,
        "_ceiling_per_request": TOTAL_MAX_ATTEMPTS_PER_REQUEST,  # documentation field
    }


# ---------------------------------------------------------------------------
# 7. Static type / compile self-check (runs at import time in dev)
# ---------------------------------------------------------------------------

def _self_check() -> Dict[str, Any]:
    """Internal: verify module invariants at import time.

    Returns dict with self-check results. Raises if any invariant broken.
    """
    # Q4: TTL = 30 min
    assert COOKIE_TTL_SECONDS == 30 * 60, f"COOKIE_TTL_SECONDS={COOKIE_TTL_SECONDS}"
    # Q2: browser retry ≤ 2
    assert TIER2_BROWSER_MAX_RETRIES == 2, f"TIER2_BROWSER_MAX_RETRIES={TIER2_BROWSER_MAX_RETRIES}"
    # Q7: hard ceiling
    assert TOTAL_MAX_ATTEMPTS_PER_REQUEST == 8
    # Q5: backward-compat — anti_block helpers importable
    assert callable(get_platform)
    assert callable(get_sec_ch_ua)
    assert "Windows" in PLATFORM_MAP
    # State-machine coverage: every classification has a decide_retry branch
    for c in RetryClassification:
        d = decide_retry(
            c,
            attempts_used=0,
            tier_max_retries=TIER1_MAX_RETRIES,
            total_attempts_used=0,
        )
        assert isinstance(d, RetryDecision)
    return {
        "COOKIE_TTL_SECONDS": COOKIE_TTL_SECONDS,
        "TIER1_MAX_RETRIES": TIER1_MAX_RETRIES,
        "TIER2_BROWSER_MAX_RETRIES": TIER2_BROWSER_MAX_RETRIES,
        "TIER3_TIMEOUT_MAX_RETRIES": TIER3_TIMEOUT_MAX_RETRIES,
        "TOTAL_MAX_ATTEMPTS_PER_REQUEST": TOTAL_MAX_ATTEMPTS_PER_REQUEST,
        "classifications_covered": len(list(RetryClassification)),
        "header_factory_emits_n_headers": len(DefaultHeaderFactory().headers_for(
            user_agent=USER_AGENTS[0],
            referer=BROWSER_BASE + "/",
        )),
    }


# Run self-check at import; raise on invariant violation.
_SELF_CHECK_RESULT: Dict[str, Any] = _self_check()


__all__ = [
    # constants
    "BROWSER_BASE",
    "COOKIE_TTL_SECONDS",
    "TIER1_MAX_RETRIES",
    "TIER2_BROWSER_MAX_RETRIES",
    "TIER3_TIMEOUT_MAX_RETRIES",
    "TOTAL_MAX_ATTEMPTS_PER_REQUEST",
    # retry state-machine
    "RetryClassification",
    "RetryDecision",
    "classify_attempt",
    "decide_retry",
    # cookie isolation
    "CookieIdentity",
    "make_cookie_identity",
    # protocols
    "ProxyRotator",
    "WarmupSequence",
    "CookieStore",
    "StickySessionKey",
    "HeaderFactory",
    "RetryPolicy",
    # production impls
    "DefaultHeaderFactory",
    "RequestBudget",
    "RequestBudgetEstimate",
    "estimate_batch_budget",
    # self-check result (for tests to inspect)
    "_SELF_CHECK_RESULT",
]
