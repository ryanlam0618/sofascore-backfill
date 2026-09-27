# Gen4 / Gen2 Fetch Method Inventory v0.1

- **Status**: Static inventory (read-only repo audit). No live HTTP, no MySQL writes, no code changes.
- **Author**: Forge (`agent:coding`), task `20260829-221500-coding-gen4-fetch-method-inventory`
- **Repo**: `/root/.openclaw/workspace/sofascore-backfill/`
- **Purpose**: Pair with forensic v0.7 results to pick the plan v0.8 transport. All paths verified on disk (`ls -la` below).
- **Repo venv**: `.runner-venv/` (cloakbrowser 0.5.7 installed; system python does NOT have the module).

## 0. Verified on-disk files

| File | Bytes |
|---|---|
| `backfill_runner.py` | 101,984 |
| `gen4_canary_validate.py` | 57,754 |
| `gen4_fetcher.py` | 42,854 |
| `gen4_components.py` | 28,887 |
| `sofascore_browser.py` | 24,956 |
| `anti_block.py` | 16,525 |
| `phase5a_validate.py` | 18,714 |
| `gen4_ssr.py` | 12,217 |
| `stable_proxy_fetch.py` | 15,432 |
| `fixed_pool_rotator.py` | 4,615 |
| `backfill_incident_metadata.py` | 6,160 |
| `rotate_proxy.py` | 4,306 |
| `survey_403.py` | 2,053 |
| `multi_source_ip_audit_1920.py` | 5,808 |
| `100_ip_full_reaudit_1920.py` | 3,530 |

---

## Section 1: Tier-based Fetch Methods

### Tier-1 — curl_cffi

| Item | Detail |
|---|---|
| Gen4 module | `gen4_fetcher.py` → `Gen4Fetcher._fetch_via_curl_cffi` (L533), kwargs built in `_build_curl_cffi_kwargs` (L563) |
| Gen2 module | `backfill_runner.py` L188–192 (`HAS_CURL_CFFI` optional import), fetcher `_fetch_via_curl_cffi` (L1024) |
| Call shape | `curl_cffi.requests.get(url, proxies=..., impersonate="chrome", timeout=...)` |
| Impersonate target | **`"chrome"`** (generic alias — resolves to latest supported Chrome in installed curl_cffi). NOT pinned to chrome120/124. Verified across `gen4_fetcher.py:577`, `gen4_phase10_6_canary.py:200`, `spot_check_19_20.py:92`, `phase5b_validate.py:136,375`, `proxy_re_audit_step4.py:72`, `15_pass_ip_verify.py:49`, `multi_source_ip_audit_1920.py:45`, `gen2_full_validate.py:309`, `gen4_phase9_diagnostic_smoke.py:290` — all use `impersonate="chrome"` |
| Latency profile | ~1–3 s per call in Phase 10.5/10.6 canary artifacts; host-egress 1.6 s in Phase 4 smoke (event 14025013 200) |
| Retry policy | Gen4: up to `TIER1_MAX_RETRIES=5` (`gen4_components.py`), 403 → rotate+retry; Gen2: 403 → fallback to browser (L1103) |
| Cookie handling | Gen4 Phase 8: optional `CookieStore` DI; canonical Phase-10.x config passes `cookie_store=None` → **zero cookies sent** (per `gen4_phase10_6_canary.py:160` docstring) |

### Tier-2 — CloakBrowser (Playwright-wrapped patched Chromium)

| Item | Detail |
|---|---|
| Package | `cloakbrowser 0.5.7` at `.runner-venv/lib/python3.11/site-packages/cloakbrowser/` (local stealth-Chromium wrapper; NOT the stock `playwright` package) |
| Browser binary | `/root/.cloakbrowser/chromium-146.0.7680.177.5/chrome` (verified installed, linux-x64, Chromium 146) |
| Gen4 launch | `gen4_canary_validate.py::_real_cloak_launcher` → lazy `import cloakbrowser as _cloak; _cloak.launch_async(headless=True, proxy=<url>, humanize=False)` |
| Gen4 fetch path | `gen4_fetcher.py::_fetch_via_cloakbrowser` (L831): `browser.new_page()` → `page.goto(base_url+path)` → `resp.json()`; EPIPE/target-closed → `rebuild_browser()` |
| Gen3 module | `stable_proxy_fetch.py::StableProxyFetcher` — subprocess-per-event worker, network-capture strategy: `page.goto(event_page)` + intercept API responses (403-evasion by never navigating to the API URL directly) |
| Gen2 module | `rotate_proxy.py::ProxyManager` (CloakBrowser + Webshare rotate gateway; restart every 150 calls; 600 s cooldown on 403) |
| Session lifetime | Per-competition sticky browser; rebuild on proxy change / EPIPE / target_closed (`gen4_components.py` Q3/Q6); Gen3 restarts every 150 calls |
| Latency profile | ~2–4.7 s per call (multi-source audit Tier-2 frontend); Phase 10.6 Tier-2 hit-rate 26.3% vs 90% target |

### Tier-3 — SSR (`__NEXT_DATA__` scrape)

| Item | Detail |
|---|---|
| Module | `gen4_ssr.py` — `load_event_ssr(event_id)` (lazy Playwright import), `resolve_ssr_for_api_path(path)` |
| Implementation | **Local Playwright** (not Cloudflare Worker, not host SSR service). Loads `https://www.sofascore.com/event/{id}` HTML, parses embedded `__NEXT_DATA__`, maps to API shape via `gen4_fetcher._map_ssr_to_api_format` |
| Coverage | `event` endpoint only; `incidents` etc. explicitly unsupported → typed `SSRHelperMissing` / `ssr_returned_none` |
| Latency | Slowest tier (full page load + HTML); used only as last resort |

### Tier-0 / baseline — urllib & miscellaneous

| Module | Use |
|---|---|
| `backfill_incident_metadata.py` | raw `urllib.request.urlopen` (audit-only utility; 403s on current 19/20 events per `host_ip_verification` artifact) |
| `survey_403.py` | `urllib.request` for 403 pattern survey (diagnostic) |

### Other transports discovered

- `phase5a_validate.py`, `phase5b_validate.py`, `discover_all_season_ids.py` — curl_cffi + CloakBrowser combo validators (diagnostic only, not production)
- `multi_source_ip_audit_1920.py` / `100_ip_full_reaudit_1920.py` — per-IP curl_cffi audit harnesses
- No `httpx` / `aiohttp` in production fetch paths (grep across repo excluding `.venv`, `tests/`)

---

## Section 2: Retry / Rotation / Cookie / Header Logic

| Concern | Gen4 (locked invariants) | Gen2 (production) | Gen3 (stable_proxy_fetch) |
|---|---|---|---|
| Max attempts | `TOTAL_MAX_ATTEMPTS_PER_REQUEST=8` hard ceiling; Tier-1 ≤5, Tier-2 ≤2 | curl 1 attempt → browser fallback; browser-level retry implicit | ~2–3 proxy retries/event, subprocess crash → new worker |
| 407 handling | **Terminal halt, 0 retry**, no Tier-2 fallback (§17) | n/a | abort validation |
| 403 handling | rotate proxy + bounded retry (5), then next tier | fallback curl→browser; 600 s cooldown on 403 at rotate level | `_is_good_ip` scoring → relaunch browser for new IP |
| 404 handling | no retry → endpoint review | — | — |
| Timeout | ≤2 transient retries | implicit | 90 s subprocess timeout |
| Proxy rotation | `ProxyRotator` Protocol + `FixedPoolRotator` (21-IP static pool, sha256(event_id) selection, clockwise on 403) | per-browser-context rotation, random 1.5–3 s sleeps | relaunch browser per bad IP |
| Cookie isolation | `CookieIdentity(competition_id, proxy_session_identity)`; TTL 30 min cap; `evict_by_proxy()` on rotate (Q1 invariant: no cross-proxy cookie carry) | browser context cookies; sticky session key optional | browser context cookies per session |
| Headers | `HeaderFactory` Protocol (`DefaultHeaderFactory` in gen4_components): UA/Referer/Origin/Sec-Fetch-*; Phase 8 opt-in | `anti_block.py` UA pool + `_build_context_headers` (Origin/Referer) | UA via CloakBrowser default + stealth args |
| TLS fingerprint | curl_cffi `impersonate="chrome"` (generic, unpinned); CloakBrowser = real Chromium TLS | same (curl_cffi `impersonate="chrome"`) | real Chromium TLS |
| Budget guard | `RequestBudget` (attempts, cookies_evicted, n_403/404/407/timeout) | `RESTART_EVERY=150` call ceiling per browser | ~150-call restart |

---

## Section 3: Gen2 Production Path (default)

- **File**: `backfill_runner.py` (101,984 bytes) — production authoritative per MEMORY.md Path 3 / Gen3 Verdict (2026-08-23, LOCKED).
- **Key class**: `BackfillClient` (L548) — Playwright (stock) + stealth (`playwright_stealth` or fallback), proxy per browser context, resource allowlist route-blocking.
- **Fetch order**: optional Tier-1 curl_cffi (`HAS_CURL_CFFI`) → on 403 fallback to warmed browser fetch (`_fetch_via_curl_cffi` L1024; fallback L1101–1103).
- **Pipeline**: warm tournament page → rounds → events/round → warm event page → fetch bundle (incidents/lineups/statistics/shotmap/graph/odds/comments) → parse → MySQL insert.
- **Why still default** (per MEMORY.md): Gen2 hybrid p50 latency **1.76 s** vs Gen3 230 s p50 (~131× slower); Gen2 validation 21/30 HTTP 200 in 2-event validation vs Gen3 non-deterministic (Step 4 11/15 vs Step 5 0/15). Gen3 judged NOT READY for backfill.

---

## Section 4: Gen4 Strategy Layers (per MEMORY.md Phase 7/8/9)

- **Entrypoint**: `gen4_fetcher.py::Gen4Fetcher.fetch_api(path, timeout_ms=30000)` — opt-in via `FETCH_STRATEGY=gen4`; default remains `gen2`.
- **Chain**: Tier-1 curl_cffi → Tier-2 CloakBrowser → Tier-3 SSR (Phase 10.6 v0.5 plan inverted to Tier-2 primary with Tier-1 fallback — that pivot failed).
- **Phase 7 invariants** (`gen4_components.py`, Kris-approved 2026-08-25 09:16):
  - Warm-up `per_event` default; per-competition reserved toggle (Q1)
  - Browser retry max 2 (Q2)
  - Sticky session per-competition (Q3)
  - Cookie TTL 30 min cap (Q4)
  - Shared helpers additive (Q5) — `anti_block.py` remains the UA/anti-block source of truth
  - Browser context per-competition; rebuild on EPIPE/target_closed/proxy change (Q6)
  - Local budget guard `RequestBudget` (Q7)
- **Retry invariants** (Kris 2026-08-25 09:49): 407 → terminal halt 0 retry; 404 → no blind retry (endpoint review); 403 → rotate + bounded retry (5); timeout → ≤2 transient retries; 200+invalid payload → 1 retry then fall; total ceiling 8.
- **Phase 9.5 rotator**: `fixed_pool_rotator.py::FixedPoolRotator` — 21-IP fixed pool; deterministic `sha256(event_id)` selection; cookie eviction on rotate via `Gen4Fetcher.evict_by_proxy` semantics.
- **Phase 10.6 canary**: `gen4_phase10_6_canary.py` — Tier-2 primary, cookie_store=None (zero cookies), MAX_ATTEMPTS_PER_REQUEST=4.

---

## Section 5: Stale / Dead / Unused Modules (flag, do NOT delete)

| Module | Status |
|---|---|
| `backfill_incident_metadata.py` | urllib-based legacy utility; currently 403s on live endpoints; superseded by backfill_runner |
| `survey_403.py` | urllib diagnostic; one-off |
| `rotate_proxy.py` | Gen2-era CloakBrowser manager; referenced by docs but Gen4 uses Gen4Fetcher + FixedPoolRotator; contains **hard-coded proxy credentials at module level** ⚠️ security flag |
| `stable_proxy_fetch.py` | Gen3 — locked NOT READY (MEMORY.md 2026-08-23); retained as reference; also has **hard-coded proxy credentials** ⚠️ |
| `gen2_full_validate.py`, `phase4_live_validate.py`, `phase5a_validate.py`, `phase5b_validate.py` | one-shot validation harnesses; not in production path |
| `gen4_phase4_live_smoke.py`, `gen4_phase9_diagnostic_smoke.py`, `gen4_phase10_canary.py`, `gen4_phase10_5_canary.py`, `gen4_phase10_6_canary.py` | per-phase canary harnesses (retained as evidence trail) |
| `100_ip_full_reaudit_1920.py`, `15_pass_ip_verify.py`, `multi_source_ip_audit_1920.py`, `spot_check_19_20.py`, `proxy_re_audit_step2/step4.py`, `proxy_pool_audit.py` | IP-audit one-shots (forensic evidence trail) |
| `_archived/*` (orchestrator.py, fetch_*.py, retry_*.py, run_all_backfill.py, etc.) | already archived; excluded from active inventory |

---

## Section 6: Quick-Reference Table

| Transport | Module path | Class/Function | Impersonate / UA | Latency profile | Prod? | Wired into Gen4? | Status |
|---|---|---|---|---|---|---|---|
| curl_cffi (Tier-1) | `gen4_fetcher.py:533` | `Gen4Fetcher._fetch_via_curl_cffi` | `impersonate="chrome"` (unpinned) | ~1–3 s | yes (via Gen2 hybrid + Gen4 opt-in) | ✅ Tier-1 | Active |
| curl_cffi (Gen2) | `backfill_runner.py:1024` | `BackfillClient._fetch_via_curl_cffi` | `impersonate="chrome"` | 1.3–53 s (Phase bench) | ✅ production | — | Active |
| CloakBrowser (Tier-2) | `gen4_fetcher.py:831` + `gen4_canary_validate.py::_real_cloak_launcher` | `_fetch_via_cloakbrowser` / `_real_cloak_launcher` | real Chromium 146 binary | 2–4.7 s | no (canary only) | ✅ Tier-2 | Active |
| CloakBrowser (Gen3) | `stable_proxy_fetch.py:238` | `StableProxyFetcher` | real Chromium | ~230 s p50 ❌ | no | ❌ | NOT READY (locked) |
| CloakBrowser (Gen2 rotation) | `rotate_proxy.py::ProxyManager` | `launch()` | real Chromium | n/a | no | ❌ | Legacy + ⚠️ creds |
| SSR (Tier-3) | `gen4_ssr.py` | `load_event_ssr` / `resolve_ssr_for_api_path` | n/a (HTML) | slowest | no | ✅ Tier-3 | Event-only |
| Playwright stealth (Gen2) | `backfill_runner.py::BackfillClient` | Playwright + stealth_async | random UA pool (anti_block.py) | p50 1.76 s | ✅ production default | — | Active |
| urllib baseline | `backfill_incident_metadata.py`, `survey_403.py` | `urllib.request.urlopen` | static UA header | fast but 403s on 19/20 API | no | ❌ | Stale/diagnostic |
| Phase 10.x canaries | `gen4_phase10*_canary.py` | harness wrappers | Tier-1 `chrome` / Tier-2 Chromium | per-canary | no | uses Gen4Fetcher | Evidence trail |
| IP audit harnesses | `proxy_re_audit_step*.py`, `100_ip_full_reaudit_1920.py`, etc. | ad-hoc | `impersonate="chrome"` | 1–3 s | no | ❌ | Evidence trail |

---

## Notes for v0.8 pairing

1. Only ONE impersonate setting exists in the whole repo (`impersonate="chrome"`); there is no chrome120/124 pin — forensic H-B test results may require pinning to a specific version profile.
2. Phase 10.6 ran with `cookie_store=None` — zero cookies on Tier-1; combined with forensic H-D hypothesis, Phase-8 HeaderFactory + CookieStore DIs are already coded but unexercised.
3. CloakBrowser wrapper is a PyPI package pinned in `.runner-venv` only; launching from system python raises `ModuleNotFoundError` — any v0.8 plan must use `.runner-venv/bin/python`.
4. ⚠️ Two files contain hard-coded proxy credentials (rotate_proxy.py, stable_proxy_fetch.py) — flagged, not touched.
