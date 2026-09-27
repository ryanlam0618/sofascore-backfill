# Gen4 Refactor Design — Phase 6 (Offline Design + Implementation Plan)

**Date:** 2026-08-25
**Author:** Duncan (Main Agent)
**Reviewer:** Kris (Webchat)
**Status:** DESIGN PROPOSAL — awaiting Kris review
**Triggered by:** Gen4 Phase 4 live smoke verdict (2026-08-25 08:10) + Gen2 vs Gen4 capability diff diagnosis (Kris 2026-08-25 08:57)

---

## Table of Contents

0. [Background & Trigger](#0-background--trigger)
1. [Component Extraction from Gen2 (6 Missing Capabilities)](#1-component-extraction-from-gen2-6-missing-capabilities)
   - 1.1 [Retry-with-rotation loop](#11-retry-with-rotation-loop)
   - 1.2 [Proxy rotation primitive](#12-proxy-rotation-primitive)
   - 1.3 [Warm-up sequence (homepage → tournament → event)](#13-warm-up-sequence-homepage--tournament--event)
   - 1.4 [Cookie store / injection](#14-cookie-store--injection)
   - 1.5 [Sticky session key](#15-sticky-session-key)
   - 1.6 [Request header factory](#16-request-header-factory)
2. [Gen4 Integration Sketch](#2-gen4-integration-sketch)
   - 2.1 [Constructor injection points](#21-constructor-injection-points)
   - 2.2 [Warm-up sequence flow](#22-warm-up-sequence-flow)
   - 2.3 [Per-tier retry-with-rotation loop](#23-per-tier-retry-with-rotation-loop)
   - 2.4 [Cookie / header / session state machine](#24-cookie--header--session-state-machine)
3. [Backward Compatibility](#3-backward-compatibility)
   - 3.1 [Gen4Fetcher public API freeze](#31-gen4fetcher-public-api-freeze)
   - 3.2 [Gen4Config additive fields](#32-gen4config-additive-fields)
   - 3.3 [Graceful degradation defaults](#33-graceful-degradation-defaults)
4. [Test Strategy](#4-test-strategy)
   - 4.1 [Mock fixtures](#41-mock-fixtures)
   - 4.2 [Test cases](#42-test-cases)
   - 4.3 [Co-existence with existing tests](#43-co-existence-with-existing-tests)
5. [Risk / Blast Radius](#5-risk--blast-radius)
6. [Phased Rollout Plan (Phase 7 → 12)](#6-phased-rollout-plan-phase-7--12)
7. [Open Questions / Decisions Awaiting Kris](#7-open-questions--decisions-awaiting-kris)
8. [References (file:line citations)](#8-references-fileline-citations)

---

## 0. Background & Trigger

### Phase 4 Live Smoke Verdict (LOCKED 2026-08-25 08:10)

| Endpoint | Result |
|---|---|
| `/api/v1/event/14025013` | ✅ 200 OK in 1606 ms via `curl_cffi` |
| `/api/v1/event/14025013/incidents` | ❌ 403 from `curl_cffi` + 403 from `cloakbrowser` + `ssr_returned_none` |
| Coverage | **1/2 (50%)** |
| DI injection | ✅ all 3 deps actually invoked |
| Safety / scope | ✅ all checks passed |

### Root Cause Diagnosis (Kris 2026-08-25 08:57)

Gen4 missing **6 critical mechanisms** that Gen2 has:

| # | Capability | Gen2 | Gen4 |
|---|---|---|---|
| 1 | 403 retry | ✅ up to 5 retries | ❌ single-shot |
| 2 | Proxy rotation on 403 | ✅ `rotate_context` per retry | ❌ single proxy |
| 3 | Warm-up sequence | ✅ `warm_homepage` → `warm_tournament` → `warm_event` | ❌ none |
| 4 | Cookie reuse | ✅ `cookie_dict` injected per request | ❌ none |
| 5 | Sticky session | ✅ `sticky_session_key` constructor arg | ❌ none |
| 6 | Header set (Origin/Referer/Sec-Fetch/UA/Accept-Lang) | ✅ 9 headers per request | ❌ none |
| (bonus) | Browser context reuse | ✅ persistent `self.context` | ❌ `self._browser.new_page()` fresh per call |

**Critical architectural insight (Kris 2026-08-25 08:57):** Gen4 嘅 3-tier fallback chain 唔係 Gen2 design 嘅 1:1 port — Gen2 嘅 tier 內部有 retry + rotation，Gen4 一次 fail 直接 next tier。

```
Gen2:  curl_cffi (retry×N + proxy_rotate) → browser (retry×N + sticky_session) → SSR
Gen4:  curl_cffi (once, no warm-up) → cloakbrowser (once, fresh context) → SSR
        ^ no retry      ^ no warm-up      ^ no cookie reuse      ^ SSR-unsupported
```

Gen2 production hybrid profile 對 event 14025013 嘅 `/incidents` 喺 `warm_session_hybrid` 入面要 retry 3 次先成功（latency 1012+1209+1698ms ≈ 4s），但 Gen4 Phase 4 第一次 fail 直接放棄。**Endpoint 唔係 deprecated**（H4 LOW confidence，Gen2 profile_b 對 2 events incidents 都 200 OK with payload_complete=True）。

### Kris 2026-08-25 08:57 GMT+8 Explicit Constraints

**Forbidden until new approval:**
1. ❌ Live fix / live modification
2. ❌ New canary run
3. ❌ `/incidents` blacklist
4. ❌ Gen4 default rollout
5. ❌ Full backfill

**OK to do now (this design doc only):**
- ✅ Offline design document (this file)
- ✅ Reusable component abstraction sketch (interface only)
- ✅ Unit tests with mocks (described, not yet committed)
- ✅ Dependency injection spec
- ✅ Architectural diagrams (text/markdown)

---

## 1. Component Extraction from Gen2 (6 Missing Capabilities)

Each capability below is a candidate **reusable component** with a typed interface. The interface is sketched as a Python `Protocol` class (PEP 544); no production code committed yet.

### 1.1 Retry-with-rotation loop

**Gen2 source location:** `backfill_runner.py:1090-1135` (`fetch_api` retry loop)

**Gen2 behavior:**
- `for attempt in range(max_retries=5)` — up to 5 attempts per request
- Each attempt: rotate proxy via `await self.rotate_context()` + random sleep 1.5-3.0s
- 403 from curl_cffi → fall to browser fetch within same attempt
- 0 from curl_cffi → fall to browser fetch within same attempt
- After `max_retries` exhausted, fall to SSR

**Public method signature (interface sketch):**

```python
from typing import Protocol, Awaitable, Callable, TypeVar

T = TypeVar("T")

class RetryWithRotationPolicy(Protocol):
    """Decide how many retries + how to rotate between attempts."""
    max_retries: int                              # default 5
    rotate_between_attempts: bool                 # default True
    sleep_between_attempts_seconds: tuple[float, float]  # (min, max); default (1.5, 3.0)

    async def rotate(self) -> None:
        """Side effect: rotate proxy / context. May sleep."""
        ...

    async def run_with_retry(
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        should_retry: Callable[[T], bool],
        on_exhausted: Callable[[], Awaitable[T | None]] | None = None,
    ) -> T | None:
        """Run operation up to max_retries; rotate between attempts.
        Return T on success, or call on_exhausted if all retries fail."""
        ...
```

**Dependencies from config:** `max_retries` (default 5), `proxy_rotation_enabled` (default True), `retry_sleep_seconds` (default (1.5, 3.0))

**Side effects:**
- Mutates `self._request_count` (Gen2:1092)
- Mutates `self._current_action` (Gen2:1093)
- Triggers `self.rotate_context()` (Gen2:1098) → context rebuild
- Triggers random sleep (Gen2:1099)
- May mutate `self._consecutive_403` (Gen2:1131)

**407 terminal halt (KEEP UNCHANGED):** Per Gen4 §17, 407 is terminal — do NOT retry. The interface must allow the caller to inject a "terminal_halt_check" that short-circuits the loop.

### 1.2 Proxy rotation primitive

**Gen2 source location:** `backfill_runner.py:973-1015` (`rotate_context`)

**Gen2 behavior:**
- Close current browser context
- Pick next proxy from pool (or rotate proxy URL via Webshare)
- Rebuild context with new proxy
- Re-warm homepage to re-establish session

**Public method signature (interface sketch):**

```python
class ProxyRotator(Protocol):
    """Pick and switch to a new proxy. Stateless or stateful per session."""
    proxy_pool: list[str]      # e.g. webshare URLs

    def current_proxy(self) -> str | None:
        """Return currently-active proxy URL or None for direct."""
        ...

    async def rotate(self) -> None:
        """Switch to next proxy (round-robin or random)."""
        ...

    def proxy_dict(self) -> dict[str, str] | None:
        """Return curl_cffi/requests-compatible proxies dict, or None."""
        ...
```

**Dependencies:** `proxy_pool` (Webshare rotation), `current_proxy_url` (Gen2 line 1050)

**Side effects:**
- Closes existing browser context (Gen2:998)
- Picks new proxy
- Optionally warms new homepage to re-establish session

**Reuse opportunity:** Gen4 already has `proxy_url()` on `Gen4Fetcher` (gen4_fetcher.py:499). The `ProxyRotator` interface could be a thin wrapper around `Gen4Config.proxy_server` + rotation logic.

### 1.3 Warm-up sequence (homepage → tournament → event)

**Gen2 source location:** `backfill_runner.py:850-895` (warm_homepage / warm_tournament / warm_event)

**Gen2 behavior:**
- `warm_homepage` — load `/`, wait for cookies, humanize
- `warm_tournament` — load `/football/tournament/{slug}/{slug}/{ut_id}#id:{season_id}`, capture tournament URL as Referer source
- `warm_event` — load `/event/{event_id}`, capture session

**Public method signature (interface sketch):**

```python
class WarmupSequence(Protocol):
    """Establish session by visiting pages in sequence."""
    async def warm_homepage(self) -> None:
        """Load homepage, wait for Set-Cookie."""
        ...

    async def warm_tournament(self, *, ut_id: int, season_id: int, country_slug: str = "", competition_slug: str = "") -> str:
        """Load tournament page; return URL to use as Referer."""
        ...

    async def warm_event(self, event_id: int) -> None:
        """Load event page; capture cookies + session state."""
        ...
```

**Dependencies:** Playwright browser context (Gen2 `self.context`), `BROWSER_BASE`

**Side effects:**
- Mutates `self._current_tournament_url` (Gen2:879) — used as Referer source
- Establishes session cookies in browser context
- Increments `_request_count` (warm operations count toward quota)

**Per-event vs per-batch warm-up design choice:** Per-event warm-up adds ~3-6s latency per request (Gen2:868 `await asyncio.sleep(3)` after tournament). For batch processing of 100+ events, this is expensive. **Design decision (open):** warm-up once per batch (tournament page), reuse cookies across all events in that batch. This is **NOT** a Gen2 behavior — Gen2 warms per event. The design defers this optimization decision to Kris.

### 1.4 Cookie store / injection

**Gen2 source location:** `backfill_runner.py:1027-1053` (cookie_dict construction + injection into curl_cffi call)

**Gen2 behavior:**
- After each Tier-1 curl_cffi call, capture cookies from browser context
- Inject cookies into NEXT curl_cffi call as `cookies=cookie_dict`
- Cookies survive across attempts within same event's request sequence

**Public method signature (interface sketch):**

```python
class CookieStore(Protocol):
    """Capture cookies from browser context; inject into HTTP requests."""
    async def capture_from_context(self, browser_context: Any) -> dict[str, str]:
        """Read cookies from Playwright context; return {name: value} dict."""
        ...

    def inject(self, request_kwargs: dict, cookie_dict: dict[str, str]) -> None:
        """Mutate request kwargs to include cookies."""
        request_kwargs["cookies"] = cookie_dict

    def is_expired(self, cookie_name: str) -> bool:
        """Check if cookie TTL has passed."""
        ...
```

**Dependencies:** Browser context (Playwright), cookie TTL strategy (default: never expire within batch)

**Side effects:**
- Cookies accumulate in browser context across requests
- Cookies expire via TTL or context rebuild

**Risk:** cookies accumulate across events → could carry identity from event A to event B if proxy doesn't rotate. **Mitigation (Section 5):** rotate context (and thus cookie store) on 403 or every N events.

### 1.5 Sticky session key

**Gen2 source location:** `backfill_runner.py:551-553` (constructor), `backfill_runner.py:2233` (default key construction)

**Gen2 behavior:**
- Constructor accepts `sticky_session_key: Optional[str]`
- If set, the same proxy + context is reused across requests with same key
- Rotation happens only when 403 received or key changes
- Default key construction: `f"{PROXY_SESSION_PREFIX}-{competition.lower().replace(' ', '-')}"` (line 2233)

**Public method signature (interface sketch):**

```python
class StickySessionKey(Protocol):
    """Tag that ties together requests sharing the same proxy + context."""
    key: str | None
    def __init__(self, key: str | None = None) -> None: ...
    def matches(self, other: "StickySessionKey") -> bool:
        """Two keys match if they pin to the same session."""
        ...
    def __str__(self) -> str: ...  # for logging
```

**Dependencies:** Constructor arg (CLI `--sticky-session-key`)

**Side effects:**
- Triggers `rotate_context` only when key changes
- Allows same IP across multiple requests (anti-detection)

**Default behavior:** Per-competition key (Gen2:2233). Per-event key would force rotation on every event (more aggressive but slower).

### 1.6 Request header factory

**Gen2 source location:** `backfill_runner.py:621-628` (`_build_context_headers`), `backfill_runner.py:1033-1044` (full header dict in `_fetch_via_curl_cffi`)

**Gen2 behavior:**
- Build per-UA headers (Accept-Language, Sec-CH-UA, Sec-CH-UA-Mobile, Sec-CH-UA-Platform) via `_build_context_headers`
- Combine with full request headers (Origin, Referer, User-Agent, Sec-Fetch-Dest, Sec-Fetch-Mode, Sec-Fetch-Site, Accept)
- Total: 9 headers per curl_cffi request

**Public method signature (interface sketch):**

```python
class HeaderFactory(Protocol):
    """Build request headers per UA + Referer + Origin."""
    def headers_for(
        self,
        *,
        user_agent: str,
        referer: str,
        origin: str = BROWSER_BASE,
    ) -> dict[str, str]:
        """Return full header dict for curl_cffi/requests call."""
        ...
```

**Headers emitted (Gen2 baseline):**
- `Accept`: `application/json, text/plain, */*`
- `Accept-Language`: `en-US,en;q=0.9`
- `Origin`: `BROWSER_BASE` (i.e. `https://www.sofascore.com`)
- `Referer`: `_current_tournament_url or BROWSER_BASE`
- `User-Agent`: `_current_ua` (random from USER_AGENTS pool)
- `Sec-CH-UA`: derived from UA
- `Sec-CH-UA-Mobile`: `?1` if mobile UA else `?0`
- `Sec-CH-UA-Platform`: from PLATFORM_MAP
- `Sec-Fetch-Dest`: `empty`
- `Sec-Fetch-Mode`: `cors`
- `Sec-Fetch-Site`: `same-origin`

**Side effects:** None (pure function).

**Reuse opportunity:** Gen2's `get_sec_ch_ua()` + `get_platform()` helpers should be moved to a `headers` module so both Gen2 and Gen4 can import.

---

## 2. Gen4 Integration Sketch

### 2.1 Constructor injection points

`Gen4Fetcher.__init__` (gen4_fetcher.py:401-408 currently) would extend to accept the 6 components. Sketch only — no production code.

```
Gen4Fetcher(
    config: Gen4Config,

    # === EXISTING (gen4_fetcher.py:401-408) ===
    curl_cffi_get: Optional[Callable] = None,
    cloakbrowser_launcher: Optional[Callable] = None,
    ssr_fetcher: Optional[Callable] = None,

    # === NEW (additive, all Optional for back-compat) ===
    retry_policy: Optional[RetryWithRotationPolicy] = None,
    proxy_rotator: Optional[ProxyRotator] = None,
    warmup_sequence: Optional[WarmupSequence] = None,
    cookie_store: Optional[CookieStore] = None,
    sticky_session: Optional[StickySessionKey] = None,
    header_factory: Optional[HeaderFactory] = None,
)
```

**Back-compat invariant:** all existing callers (e.g. `gen4_phase4_live_smoke.py:220`) keep working without modification. New fields default to `None`, which means **graceful degradation** (Section 3.3).

### 2.2 Warm-up sequence flow

**Before first request** of a batch:

```
warm_homepage()
  → wait_for_cookies()
  → store cookies in cookie_store
warm_tournament(ut_id, season_id, country_slug, competition_slug)
  → capture tournament URL
  → store as Referer source
warm_event(event_id)
  → final cookies locked in
```

**Per-event decision:** Gen2 warms per event (Gen2:893 `warm_event`). For batch processing, the design recommends warming once per (competition, season) and reusing the Referer + cookie store across events within the same scope. **Open question for Kris (Section 7).**

**Failure semantics:**
- If warm_homepage fails → fallback to direct (Gen4 1-tier behavior, may 403)
- If warm_tournament fails → use `BROWSER_BASE` as Referer (Gen2 fallback at line 996)
- If warm_event fails → proceed with whatever cookies captured so far

### 2.3 Per-tier retry-with-rotation loop

Current Gen4 `fetch_api` (gen4_fetcher.py:590) is a single-shot per tier. Refactored shape (pseudo-code only):

```
async def fetch_api(path, *, timeout_ms=30000):
    self._assert_no_write()
    attempts = []

    # === NEW: warm-up gate (once per batch) ===
    if not self._warmed_up:
        await self._ensure_warmed(event_id_from_path(path))
        self._warmed_up = True

    # === Tier 1: curl_cffi with retry+rotation ===
    for attempt in range(self._retry_policy.max_retries):
        t1 = await self._fetch_via_curl_cffi(path, timeout_ms)
        attempts.append(t1.attempt)

        # NEW: 407 terminal halt (UNCHANGED from current Gen4 §17)
        if t1.attempt.status == 407:
            return _failure_407(...)

        # NEW: 200 success → return
        if t1.attempt.status == 200 and validate_payload(path, t1.body).complete:
            return _success_curl_cffi(...)

        # NEW: rotate before next attempt
        await self._retry_policy.rotate()
        await asyncio.sleep(random.uniform(*self._retry_policy.sleep_seconds))

    # === Tier 2: CloakBrowser with retry (NEW: sticky session reuse) ===
    for attempt in range(self._retry_policy.max_retries // 2):  # fewer retries for browser
        t2 = await self._fetch_via_cloakbrowser(path, timeout_ms)
        attempts.append(t2.attempt)

        if t2.attempt.status == 200 and validate_payload(...).complete:
            return _success_cloakbrowser(...)

        await self._retry_policy.rotate()
        await asyncio.sleep(...)

    # === Tier 3: SSR (UNCHANGED, structurally unsupported for incidents) ===
    t3 = await self._fetch_via_ssr(path, timeout_ms)
    attempts.append(t3.attempt)
    if t3.attempt.status == 200 and t3.body is not None:
        return _success_ssr(...)

    # All tiers exhausted
    return _failure_result(...)
```

**Changes vs current Gen4:**
- Tier 1 retries 5× instead of 1× (Gen2:1090)
- Tier 2 retries ~2× instead of 1× (browser fallback is heavier)
- Each retry rotates proxy via `ProxyRotator.rotate()`
- Each retry uses fresh cookies from `CookieStore.capture_from_context()`
- Each retry builds headers via `HeaderFactory.headers_for()`
- Tier 1 carryover `page = self._browser.new_page()` (gen4_fetcher.py:538) → REPLACE with `self._browser.contexts[0].new_page()` or maintain a sticky `page` attribute

### 2.4 Cookie / header / session state machine

```
┌──────────────────────────────────────────────────────────────┐
│  WARM-UP PHASE (once per batch)                              │
│                                                              │
│   warm_homepage() ──→ cookies_captured ──→ cookie_store     │
│   warm_tournament() ──→ referer_url ──→ header_factory      │
│   warm_event() ──→ final_cookies ──→ cookie_store            │
└──────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────────┐
│  PER-REQUEST LOOP (per path)                                 │
│                                                              │
│   for attempt in retry_policy.max_retries:                   │
│     headers = header_factory.headers_for(                    │
│         user_agent = self._current_ua,                       │
│         referer = self._current_tournament_url,              │
│     )                                                        │
│     cookies = cookie_store.capture_from_context(self.context)│
│                                                              │
│     # Tier-1 attempt                                         │
│     response = curl_cffi_get(                                │
│         url,                                                 │
│         headers=headers,                                     │
│         cookies=cookies,                                     │
│         proxies=proxy_rotator.proxy_dict(),                  │
│         impersonate="chrome",                                │
│         timeout=timeout_ms / 1000,                           │
│     )                                                        │
│                                                              │
│     if response.status_code == 200:                          │
│         return SUCCESS                                       │
│                                                              │
│     # Rotate + retry                                         │
│     await proxy_rotator.rotate()                             │
│     await asyncio.sleep(random.uniform(1.5, 3.0))            │
│                                                              │
│   # Tier-1 exhausted, fall to Tier-2 (CloakBrowser)         │
└──────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────────┐
│  TIER-2 (CloakBrowser with sticky session)                   │
│                                                              │
│   page = self._browser.contexts[0].new_page()  # NOT fresh  │
│   await page.set_extra_http_headers(headers)                 │
│   await page.context.add_cookies([cookies_from_cookie_store])│
│   response = await page.goto(f"{base_url}{path}", ...)       │
│   # ... retry+rotate as Tier-1                               │
└──────────────────────────────────────────────────────────────┘
```

**Key state transitions:**
- `self._warmed_up: bool` — False → True after first warm-up call
- `self._current_tournament_url: str | None` — set by warm_tournament, consumed by HeaderFactory
- `self._current_ua: str` — set by warm_homepage, consumed by HeaderFactory
- `cookie_store.cache: dict[str, str]` — populated by CookieStore.capture_from_context, consumed by both Tier-1 and Tier-2
- `proxy_rotator.current_index: int` — incremented on each `.rotate()`

---

## 3. Backward Compatibility

### 3.1 Gen4Fetcher public API freeze

**MUST NOT CHANGE:**
- `Gen4Fetcher.__init__(config, curl_cffi_get, cloakbrowser_launcher, ssr_fetcher)` — existing 4 args keep their signatures and defaults
- `Gen4Fetcher.fetch_api(path, *, timeout_ms=30000)` — same signature
- `Gen4Fetcher._fetch_via_curl_cffi(path, timeout_ms)` — same signature (private but used in tests)
- `Gen4Fetcher._fetch_via_cloakbrowser(path, timeout_ms)` — same signature
- `Gen4Fetcher._fetch_via_ssr(path, timeout_ms)` — same signature
- `Gen4Fetcher._assert_no_write()` — unchanged
- `Gen4Fetcher.start()` / `Gen4Fetcher.stop()` — unchanged lifecycle hooks

**MAY BE ADDED (additive only):**
- New `Optional[Component]` args to `__init__` (all default `None`)
- New public methods on `Gen4Fetcher` (e.g. `ensure_warmed()`, `rotate_proxy()`)
- New attributes on `Gen4Config` (e.g. `max_retries: int = 5`)

**MAY BE CHANGED INTERNALLY:**
- `fetch_api` body — refactored to use retry loop (Section 2.3)
- `_fetch_via_curl_cffi` — may call `HeaderFactory` + `CookieStore` if injected
- `_fetch_via_cloakbrowser` — may reuse `self._browser.contexts[0]` instead of `self._browser.new_page()`

### 3.2 Gen4Config additive fields

Current `Gen4Config` (gen4_fetcher.py:370-389). New fields would all default to safe values:

```python
@dataclass
class Gen4Config:
    # EXISTING
    write_enabled: bool
    proxy_server: str
    proxy_port: str
    proxy_user: str
    proxy_pass: str
    base_url: str = "https://www.sofascore.com"

    # === NEW (all default to Gen4 current behavior) ===
    max_retries: int = 1                      # default = 1 (Gen4 current single-shot)
    retry_sleep_seconds: tuple[float, float] = (0.0, 0.0)  # no sleep by default
    warmup_enabled: bool = False              # default = no warm-up (Gen4 current)
    warmup_strategy: str = "per_event"        # or "per_batch" (open question)
    sticky_session_key: str | None = None     # default = no sticky session
    browser_max_retries: int = 1              # default = 1 (Gen4 current single-shot)
```

**Critical default invariant:** all new fields default to **Gen4's current behavior** (i.e. disabled/single-shot). To enable Gen2-level behavior, caller must explicitly opt in by setting `max_retries > 1`, `warmup_enabled=True`, etc. This protects Phase 4 from unintended behavior change.

### 3.3 Graceful degradation defaults

When new components are `None` (i.e. not injected):

| Component | None → behavior |
|---|---|
| `retry_policy` | Single-shot per tier (Gen4 current) |
| `proxy_rotator` | Use single proxy (Gen4 current `Gen4Config.proxy_*`) |
| `warmup_sequence` | No warm-up (Gen4 current) |
| `cookie_store` | No cookies injected (Gen4 current) |
| `sticky_session` | No session pinning (Gen4 current) |
| `header_factory` | Use minimal headers via `curl_cffi.requests.get` defaults (Gen4 current) |

**This means:** Phase 4 `gen4_phase4_live_smoke.py` works UNCHANGED. The new design is fully backward-compatible. Opt-in to Gen2-level behavior is via explicit DI.

### 3.4 Test artifact compatibility

`tests/test_gen4_phase4_di.py` (11/11 passing as of 2026-08-25) must continue to pass:
- All new `Optional` fields default to `None` → existing test wiring (`fetcher = Gen4Fetcher(config, curl_cffi_get=mock, ...)`) still works
- `_assert_no_write()` semantics unchanged
- Scope enforcement tests (`test_scope_violation_*`) unchanged
- `test_407_triggers_safety_halt` unchanged (407 terminal halt is design rule)

---

## 4. Test Strategy

### 4.1 Mock fixtures

To enable offline testing without live calls, the following fixtures are needed:

| Fixture | Purpose |
|---|---|
| `FakeProxyRotator` | Returns configurable proxy dicts; tracks `rotate()` call count |
| `FakeWarmupSequence` | Records warm-homepage/tournament/event calls; returns canned URL |
| `FakeCookieStore` | Returns canned `dict[str, str]`; tracks `capture_from_context` calls |
| `FakeStickySessionKey` | Returns canned key string; tracks `matches()` calls |
| `FakeHeaderFactory` | Returns canned header dict; tracks `headers_for()` calls |
| `FakeRetryPolicy` | Counts attempts; calls `rotate()` between; respects `max_retries` |

All fixtures follow the `Protocol` interfaces from Section 1.

### 4.2 Test cases

New test file `tests/test_gen4_refactor_components.py`:

| Test | Verifies |
|---|---|
| `test_retry_policy_rotates_between_attempts` | `RetryPolicy.rotate()` called between attempts; count == `max_retries - 1` |
| `test_retry_policy_407_terminal_halt` | 407 status short-circuits loop; no further retries |
| `test_proxy_rotator_increments_on_rotate` | `current_proxy()` returns different values across calls |
| `test_warmup_sequence_called_once_per_batch` | 2 events in same batch → 1 warm_homepage call |
| `test_cookie_store_captured_before_tier1` | `CookieStore.capture_from_context` called before Tier-1 curl_cffi |
| `test_cookie_store_injected_into_tier2` | Tier-2 receives cookies via `browser_context.add_cookies` |
| `test_sticky_session_reuses_context` | Same key across 2 requests → same browser context reused |
| `test_header_factory_emits_9_headers` | Headers dict contains all 9 Gen2-baseline headers |
| `test_header_factory_origin_matches_base_url` | `Origin` == `Gen4Config.base_url` |
| `test_back_compat_no_components_injected` | `Gen4Fetcher()` with only 4 args → single-shot behavior |
| `test_incidents_403_eventually_succeeds_with_retry` | Mock Tier-1 returns 403, 403, 200 → caller gets 200 (validates H5 fix) |
| `test_warmup_failure_graceful_degradation` | warm_homepage raises → request proceeds with `BROWSER_BASE` Referer |
| `test_cookie_store_ttl_expired` | Cookies past TTL → capture returns empty dict |
| `test_browser_context_reuse_reduces_overhead` | 2 requests in same session → 1 `new_context` call, 2 `new_page` calls |

### 4.3 Co-existence with existing tests

| Existing test file | Co-existence strategy |
|---|---|
| `tests/test_gen4_phase4_di.py` (11/11 pass) | Add no-op default for new fields; all tests pass unchanged |
| `tests/test_gen4_canary_harness_di.py` | New tests are additive; no overlap with canary logic |
| `tests/test_gen2_validation_harness.py` | Gen2 tests independent; Gen4 components don't touch Gen2 code path |
| `tests/test_endpoint_normalization.py` | Independent (URL parsing only) |

**Test command invariant:** `python3 -m pytest tests/test_gen4_*.py -v` must continue to show all existing tests passing + new tests passing.

---

## 5. Risk / Blast Radius

### Risk 1: Cookie accumulation → memory leak

**Scenario:** `CookieStore.capture_from_context` keeps growing across many requests in a long-running batch (10K+ events). Each event's session cookies accumulate.

**Mitigation:**
- Cookie TTL: default 30 minutes (Gen2 doesn't enforce but browsers do)
- Rotate context on 403 → fresh cookie store
- Rotate context every N events (default N=50, configurable)
- Explicit `cookie_store.clear()` after batch completion

### Risk 2: Proxy rotation rate-limit cliff

**Scenario:** Aggressive proxy rotation triggers Webshare rate-limit (e.g. > 100 proxy switches / minute) → 429 from proxy provider.

**Mitigation:**
- `retry_sleep_seconds` minimum 1.5s (Gen2 default) — even single retry waits
- `proxy_rotation_budget_per_minute` config field (default 20/min)
- Track rotation count; if budget exceeded, fall to single-proxy mode for the rest of batch

### Risk 3: CloakBrowser session state pollution

**Scenario:** Reusing `self._browser.contexts[0]` across requests carries localStorage / IndexedDB state that affects subsequent requests. E.g. login cookies from event A pollute event B.

**Mitigation:**
- `clear_cookies_on_event_boundary: bool = True` (default)
- Per-event `context = await browser.new_context()` (heavier than `new_page` but cleaner)
- `clear_storage_on_new_context: bool = True` (localStorage / IndexedDB reset)

### Risk 4: Warm-up latency cost

**Scenario:** `warm_homepage` + `warm_tournament` adds 3-6s latency (Gen2:868 `await asyncio.sleep(3)`) before first request. For 1K-event batch, this is amortized, but for 1-event test it's expensive.

**Mitigation:**
- `warmup_strategy: "per_event" | "per_batch" | "lazy"` config field
- `"per_batch"` (recommended) → warm once, reuse across batch
- `"lazy"` → warm on first request only (mid-batch)
- Skippable via `warmup_enabled: bool = False` for unit tests

### Risk 5: Retry amplification → quota exhaustion

**Scenario:** With `max_retries=5` per tier × 3 tiers, worst-case 15 attempts per request. Each attempt counts toward Webshare quota (Gen2 has 1TB/month). Aggressive retry could exhaust quota faster.

**Mitigation:**
- `total_max_attempts_per_request: int = 7` (default, configurable) — caps total attempts
- Track quota usage; if 80% exhausted, downgrade to `max_retries=1` for remaining batch
- Expose `quota_warning` artifact field (Gen4 already has this)

### Risk 6: Warm-up order coupling

**Scenario:** `warm_tournament(ut_id, season_id, country_slug, competition_slug)` requires 4 args. Gen4 doesn't currently know competition slug. If warm-up signature changes between Gen2 and Gen4, integration breaks.

**Mitigation:**
- `warmup_sequence.warm_tournament(**kwargs)` — pass-through
- Gen4 caller (likely Phase 9+) passes competition slug from `backfill_runner.ENDPOINT_PATHS` or new config
- If no slug available, fall back to `BROWSER_BASE` Referer (Gen2:996 fallback)

### Risk 7: SSR contract drift

**Scenario:** Gen4 already classifies `incidents` as SSR-unsupported (gen4_fetcher.py:265 `SSR_UNSUPPORTED_ENDPOINTS`). If a future version of SofaScore SSR pageProps includes incidents inline, SSR tier would still return None (early short-circuit at gen4_ssr.py:189).

**Mitigation:**
- Periodic re-verification against fresh SSR fixture (Kris policy from gen4_fetcher.py:259)
- `SSR_UNSUPPORTED_ENDPOINTS` change requires explicit Kris approval (gen4_fetcher.py:259)
- Document this in design review (Section 7)

---

## 6. Phased Rollout Plan (Phase 7 → 12)

Each phase has explicit Kris approval gate. No phase auto-advances.

### Phase 7: Offline component implementation (Unit tests pass)

**Scope:**
- Implement 6 components in `gen4_components.py` (new file) per Section 1 interfaces
- Implement 6 `Fake*` fixtures in `tests/fixtures/gen4_components_fakes.py`
- Add `tests/test_gen4_refactor_components.py` with 14 tests (Section 4.2)
- Update `Gen4Fetcher.__init__` to accept new Optional args (Section 2.1) — additive only
- All defaults preserve current Gen4 behavior (Section 3.3)

**Kris approval gate:**
- ✅ `pytest tests/test_gen4_refactor_components.py` shows 14/14 pass
- ✅ `pytest tests/test_gen4_phase4_di.py` still shows 11/11 pass
- ✅ `pytest tests/test_gen2_validation_harness.py` still passes
- ✅ No source code outside `gen4_components.py` + `tests/` + `gen4_fetcher.py:__init__` changed

**Forbidden during Phase 7:** live calls, canary, blacklist, rollout, backfill.

### Phase 8: Offline integration tests (Components wired into Gen4Fetcher, still no live)

**Scope:**
- Refactor `Gen4Fetcher.fetch_api` per Section 2.3 (retry loop)
- Refactor `_fetch_via_curl_cffi` per Section 2.4 (use HeaderFactory + CookieStore)
- Refactor `_fetch_via_cloakbrowser` per Section 2.4 (reuse context, not fresh page)
- Add integration tests in `tests/test_gen4_refactor_integration.py`:
  - `test_tier1_retry_then_success` — Tier-1 mock returns 403, 403, 200 → caller gets 200
  - `test_tier1_retry_then_fall_to_tier2` — Tier-1 403 ×5 → Tier-2 200 → caller gets 200
  - `test_warmup_failure_continues` — warm_homepage raises → request still proceeds
  - `test_407_terminal_halt_in_retry_loop` — 407 on attempt 2 of 5 → no further attempts
  - `test_cookie_store_thread_safety` — concurrent requests share cookie store

**Kris approval gate:**
- ✅ All Phase 7 tests still pass
- ✅ All Phase 8 integration tests pass (≥5 new tests)
- ✅ `gen4_phase4_live_smoke.py` (Phase 4 artifact source) still runs in dry-run mode without errors
- ✅ Code review by Kris

**Forbidden during Phase 8:** live calls, canary, blacklist, rollout, backfill.

### Phase 9: Single-event diagnostic smoke (event 14025013, all 4 endpoints)

**Scope:**
- New file `gen4_phase9_diagnostic_smoke.py`
- Scope: event 14025013, endpoints `[event, incidents, lineups, statistics]` (extend Phase 4's 2)
- Mode: Gen4 with all new components INJECTED (`max_retries=5`, `warmup_enabled=True`)
- Capture: response headers (including `Set-Cookie`) for diagnostic
- Compare: Gen4 success rate vs Gen2 production profile_b (target: same 100% / 100%)

**Kris approval gate:**
- ✅ Phase 8 gate cleared
- ✅ Diagnostic shows cookie reuse working (response has `Set-Cookie`, second request carries it)
- ✅ Diagnostic shows `/incidents` final status = 200 OK with `payload_complete=True`
- ✅ Diagnostic shows retry count > 1 on first request (proof retry loop fired)
- ✅ All safety stops (407 halt, quota warning, scope guard) verified

**Forbidden during Phase 9:** blacklist, rollout, backfill. Canary = THIS phase is the canary.

### Phase 10: Multi-event canary (10 events × 4 endpoints)

**Scope:**
- Run Phase 9 logic across 10 events sampled from existing backfill data
- Mode: Gen4 with all components, `max_retries=5`, `warmup_enabled=True`, `warmup_strategy="per_batch"`
- Quota budget: ≤ 20% of Webshare monthly quota
- Compare: per-event success rate vs Gen2 historical (target: ≥ 95%)

**Kris approval gate:**
- ✅ Phase 9 gate cleared
- ✅ Multi-event success rate ≥ 95%
- ✅ No new halt triggers fired
- ✅ Incident success rate per event ≥ 90%

**Forbidden during Phase 10:** rollout, backfill. Canary = THIS phase is the multi-event canary.

### Phase 11: Default rollout

**Scope:**
- Change Gen2 → Gen4 default in `backfill_runner.py:CLI entry`
- Keep Gen2 as `FETCH_STRATEGY=gen2` opt-in
- Run small backfill (100 events) in production mode

**Kris approval gate:**
- ✅ Phase 10 gate cleared
- ✅ 100-event production run shows success rate ≥ Gen2 baseline
- ✅ No quota warnings fired
- ✅ All MySQL writes intact

**Forbidden during Phase 11:** full backfill. Rollout = THIS phase flips the default.

### Phase 12: Full backfill

**Scope:**
- Run full backfill with Gen4 default
- Gen2 remains available as fallback

**Kris approval gate:**
- ✅ Phase 11 gate cleared
- ✅ Full batch completes within Webshare quota
- ✅ Backfill data integrity check passes

---

## 7. Open Questions / Decisions Awaiting Kris

These require explicit decision before Phase 7 implementation begins:

### Q1. Warm-up strategy: per-event or per-batch?

**Gen2 default:** per-event (warm_homepage → warm_tournament → warm_event for every event).
**Gen4 design recommendation:** per-batch (warm once per (competition, season), reuse across events in scope).
**Trade-off:** per-event = stronger anti-detection but +3-6s/event latency. Per-batch = faster but riskier (Sofascore may flag same-IP-different-events).
**Decision needed:** before Phase 7.

### Q2. Retry count for browser tier

**Gen2 equivalent:** max_retries=5 for curl_cffi; browser fallback inside same attempt.
**Gen4 design proposal:** Tier-1 = 5 retries, Tier-2 = 2 retries (browser is heavier).
**Trade-off:** more retries = higher quota cost but better success rate on flaky endpoints.
**Decision needed:** before Phase 8.

### Q3. Sticky session scope: per-competition or per-event?

**Gen2 default:** per-competition (gen2:2233 `f"{PROXY_SESSION_PREFIX}-{competition.lower().replace(' ', '-')}"`).
**Gen4 design proposal:** default per-competition, expose config.
**Trade-off:** per-event = aggressive rotation (better anti-detection, lower quota efficiency). Per-competition = sticky (better quota efficiency, weaker anti-detection).
**Decision needed:** before Phase 7.

### Q4. Cookie store TTL

**Gen2 behavior:** no explicit TTL; browser handles cookie expiry naturally.
**Gen4 design proposal:** explicit TTL (default 30 minutes) + `clear_on_event_boundary` (default True).
**Trade-off:** TTL = cleaner memory but may break long-running batches. No TTL = memory growth (Risk 1).
**Decision needed:** before Phase 7.

### Q5. Should we extract Gen2 helpers (`get_sec_ch_ua`, `get_platform`) into a shared module?

**Gen2 current:** helpers inline in `backfill_runner.py:621-628`.
**Gen4 design proposal:** move to `gen4_components.headers` for sharing.
**Trade-off:** refactor of Gen2 risk vs DRY benefit.
**Decision needed:** before Phase 7.

### Q6. Browser context reuse vs fresh per event?

**Gen2 default:** persistent `self.context` reused across all events in `BackfillClient` lifetime.
**Gen4 design proposal:** `self._browser.contexts[0]` for Tier-2 reuse, but `clear_cookies` on event boundary.
**Trade-off:** reuse = faster (no context setup overhead) but Risk 3 (state pollution). Fresh per event = safer but +1-2s overhead per event.
**Decision needed:** before Phase 8.

### Q7. Failure tolerance: 403 retry budget

**Scenario:** After 5 retries × 5 events = 25 attempts for `/incidents` across 5 events. If Webshare budget is 1TB/month, what's the failure tolerance?
**Open:** Need Webshare quota monitoring integration.
**Decision needed:** before Phase 10.

---

## 8. References (file:line citations)

### Gen2 source (`backfill_runner.py`)

| Capability | Line | Method/Attribute |
|---|---|---|
| Retry loop | 1090 | `for attempt in range(max_retries):` |
| Retry rotation | 1097-1099 | `if attempt > 0: await self.rotate_context(); await asyncio.sleep(...)` |
| 403 → browser fallback | 1106-1108 | `if result.get("status") == 403: result = await self._browser_fetch_api(...)` |
| SSR after rotations | 1133-1140 | `if last_result.get("status") == 403: ssr_result = await self.get_event_ssr(path)` |
| Constructor + sticky_session | 551-553 | `def __init__(self, headless, sticky_session_key)` |
| _current_ua | 569 | `self._current_ua = random.choice(USER_AGENTS)` |
| _current_headers | 570 | `self._current_headers = {}` |
| _build_context_headers | 621-628 | returns Accept-Language, Sec-CH-UA, etc. |
| warm_homepage | 850-866 | loads BROWSER_BASE, waits 2s, humanizes |
| warm_tournament | 868-891 | loads tournament URL, captures _current_tournament_url |
| warm_event | 893+ | loads event page |
| rotate_context | 973-1015 | closes + rebuilds browser context with new proxy |
| Extra headers applied | 995-1000 | `extra_http_headers=headers` on context |
| Cookie capture | 1027-1032 | `await self.context.cookies(BROWSER_BASE)` |
| Cookie injection | 1052 | `cookies=cookie_dict` in curl_cffi call |
| Full header set | 1033-1044 | 9 headers (Accept, Accept-Language, Origin, Referer, UA, Sec-CH-*, Sec-Fetch-*) |
| Proxy injection | 1050-1051 | `proxies={"https": ..., "http": ...}` |
| Sticky session default | 2233 | `f"{PROXY_SESSION_PREFIX}-{competition.lower().replace(' ', '-')}"` |

### Gen4 source (current state)

| File | Line | Description |
|---|---|---|
| gen4_fetcher.py | 370-389 | `Gen4Config` dataclass |
| gen4_fetcher.py | 401-408 | `Gen4Fetcher.__init__` (4-arg signature) |
| gen4_fetcher.py | 492-516 | `_fetch_via_curl_cffi` (no warm-up, no cookies, no headers beyond impersonate) |
| gen4_fetcher.py | 522-562 | `_fetch_via_cloakbrowser` (uses `self._browser.new_page()` fresh) |
| gen4_fetcher.py | 565-588 | `_fetch_via_ssr` |
| gen4_fetcher.py | 590-720 | `fetch_api` (single-shot per tier, no retry loop) |
| gen4_fetcher.py | 130-138 | `ENDPOINT_MIN_KEYS` |
| gen4_fetcher.py | 263-274 | `SSR_UNSUPPORTED_ENDPOINTS` |
| gen4_phase4_live_smoke.py | 107-114 | `_make_real_curl_cffi_get` factory (raw `curl_requests.get`) |
| gen4_phase4_live_smoke.py | 122-150 | `_make_real_cloakbrowser_launcher` factory (no sticky session) |
| gen4_phase4_live_smoke.py | 220-221 | Factory injection into `Gen4Fetcher` |

### Phase 4 artifact

- `data/gen4_phase4_smoke_event_14025013.json` (2026-08-25 08:09-08:10)
  - `/event` → 200 OK in 1606 ms via curl_cffi
  - `/incidents` → 403 (curl_cffi, 1619 ms) + 403 (cloakbrowser, 2675 ms) + ssr_returned_none
  - `summary.endpoint_coverage_rate = 0.5`

### Test artifacts

- `tests/test_gen4_phase4_di.py` — 11/11 passing (DI fix verified 2026-08-25 08:00)
- `tests/test_gen4_canary_harness_di.py` — existing canary DI tests

### Other artifacts

- `data/gen2_profile_b_production_hybrid.json` — Gen2 production runs (2/2 events incidents 200 OK with payload_complete=True)
- `data/gen2_profile_c_warm_session_hybrid.json` — Gen2 warm session (14025013 incidents 403×3 attempts then retry-success pattern)
- `data/phase5b_live_artifact.json` — Phase 5b artifact (mock-failure mode, both events `/event` → SSR success, `/incidents` → all tiers fail)

### MEMORY references

- MEMORY.md § "Gen4 Phase 4 Live Smoke Verdict (2026-08-25 08:10 GMT+8, LOCKED)" — locked verdict
- MEMORY.md § "Path 3 / Gen3 Verdict (2026-08-23, LOCKED)" — Gen2 production authoritative
- MEMORY.md § "Test Infrastructure Lessons" — hard-import failure mode for sub-agents

---

## Appendix A: Why Gen2 ≠ Gen4 (extended explanation)

```
Gen2's tier semantics:
  Tier 1:  curl_cffi with retry×N + proxy_rotate + warm_session + cookies + headers
  Tier 2:  browser with retry×M + sticky_session + cookies
  Tier 3:  SSR (after all rotations exhausted)

Gen4's tier semantics (current):
  Tier 1:  curl_cffi (single-shot, no warm-up, no cookies, no headers)
  Tier 2:  CloakBrowser (single-shot, fresh page, no cookies)
  Tier 3:  SSR (single-shot, but `incidents` is short-circuited as unsupported)

Why Phase 4 /incidents failed:
  1. Tier-1: 403 (no warm-up → no cookies → no session)
  2. Tier-2: 403 (fresh context → no cookies → no session)
  3. Tier-3: ssr_returned_none (incidents is SSR-unsupported by design)

Gen2 would have:
  1. Tier-1 attempt 1: 403 (cold session)
  2. rotate proxy, wait, retry attempt 2: 403 (warm-up not yet triggered)
  3. rotate proxy, wait, retry attempt 3: 200 (warm-up cookies now in jar)
  4. Return 200 (payload_complete=True)
```

---

## Appendix B: Glossary

| Term | Definition |
|---|---|
| Tier | One of the 3 fetch methods in Gen4 fallback chain (curl_cffi / CloakBrowser / SSR) |
| Warm-up | Pre-request page load to establish session cookies (Gen2:850-895) |
| Sticky session | Tying requests to a specific proxy/context to maintain identity (Gen2:551-553) |
| Cookie store | In-memory dict of {name: value} cookies, captured from browser context (Gen2:1027-1032) |
| Header factory | Pure function producing request headers from UA + Referer + Origin (Gen2:621-628, 1033-1044) |
| 403 retry budget | Total number of retries across all tiers per request (proposed: ≤ 7) |
| Per-batch warm-up | Warm once per (competition, season), reuse cookies across events |
| Per-event warm-up | Warm per event (Gen2 default, +3-6s/event latency) |

---

**End of design doc.** Awaiting Kris review on Section 7 open questions before Phase 7 implementation begins.
