# Path 3 Fix Plan — `stable_proxy_fetch.py` Endpoint Normalization

**Date:** 2026-08-23
**Author:** coding agent
**Reviewer:** Kris (Webchat)
**Status:** APPROVED — 5 blocking conditions incorporated, execution pending

---

## 0. Blocking Conditions (incorporated from Kris 08:10 GMT+8)

1. **Tests first, then patch.** Tests must be written before the production
   helper is added, and must be confirmed to fail on the current buggy
   code. This proves the tests genuinely catch the bug.
2. **Normalizer rejects unknown endpoints.** Must explicitly whitelist
   `TARGET_ENDPOINTS` patterns only; unknown / malformed URLs return
   `None` (or raise a validation error). No silent key acceptance.
3. **Tests validate the data/status contract.** Tests must cover
   normalized key in `TARGET_ENDPOINTS`, paired write to `api_data` and
   `api_status`, malformed URL isolation, and explicit behaviour for
   trailing slash / query string / fragment / uppercase / non-API URLs.
4. **Validation has hard stops.** 3-event validation must enforce:
   `.env` credentials never written to logs/artifacts, any 407 → abort,
   2 consecutive subprocess fails → abort, >50% 403 → abort, every fetch
   tagged `write_enabled: false` with no `DataInserter` / MySQL call.
   Validation failure stops the shadow comparison.
5. **Shadow pass criteria separate HTTP success from data completeness.**
   Must record separately: `http_success_rate`, `endpoint_data_completeness`,
   `status_key_completeness`, `403_rate`, `407_rate`, latency p50/p90,
   retry count, IP scoring result, subprocess failure count. Gen3 only
   counts as fixed when data key, status key, AND payload completeness
   all pass.

### Execution Order (per blocking condition #1)

1. Update this plan with blocking conditions (✅ done).
2. Write 13 offline unit tests for `_normalize_endpoint_name`.
3. Run tests against current code; record expected failures
   (proves tests catch the bug).
4. Apply minimal normalization patch.
5. Run tests; confirm all pass.
6. Run 3-event no-write validation with hard stops.
7. If and only if §6 passes, run read-only shadow comparison.
8. Generate final report with three-tier conclusion
   (normalization fix / validation / production-readiness).
9. Stop. Do **not** enable feature flag or change `fetch_api` default.

---

## 1. Goal

Fix the endpoint normalization bug in `stable_proxy_fetch.py` so that
captured network responses are mapped to the bare endpoint names declared
in `TARGET_ENDPOINTS` (e.g. `"event"`, `"incidents"`, `"lineups"`),
rather than the raw URL tail (e.g. `"event/14025013"`,
`"event/14025013/incidents"`). After the fix, the existing callers
(`StableProxyFetcher.fetch_event` and the consumer in
`tests/bench_method_c_retry.py`) can read `result.data["event"]` and
`result.data["incidents"]` directly without key-mismatch failures.

This fix is **shadow-only**: `backfill_runner.fetch_api` (the production
path) is **not** changed. After the fix, gen3 will be validated as a
shadow comparison against gen2 only, with no production traffic routed
through it.

---

## 2. Root Cause (recap from report §3.2)

In `stable_proxy_fetch.py`, the worker-script `capture()` function
(embedded as `_WORKER_SCRIPT`) does:

```python
parts = url.rstrip("/").split("/api/v1/")
ep = parts[1] if len(parts) > 1 else "unknown"
```

For `https://www.sofascore.com/api/v1/event/14025013/incidents`, this
yields `ep = "event/14025013/incidents"`. But `StableProxyFetcher`
callers expect `result.data["incidents"]`, which is never populated.

`api_status` is also keyed on this raw URL tail, so `api_status_map`
lookups return `0` for `"event"` and `"incidents"` as well.

---

## 3. Proposed Fix

### 3.1 Approach

Add a normalization function that maps the raw URL tail to the canonical
endpoint name defined in `TARGET_ENDPOINTS`. Strategy:

1. Build a list of regex patterns from `TARGET_ENDPOINTS` at module
   import time. Each pattern has `{eid}` replaced by `(?P<eid>\d+)`.
2. In `capture()`, try each pattern against the URL tail; the first match
   returns the bare endpoint name as the key.
3. Fall back to the existing raw tail behaviour for URLs that don't match
   any pattern (preserves backward compatibility and surfaces
   unrecognised endpoints in logs).

### 3.2 Why a pattern list, not hardcoded key replacement

The current bug is structural — `parts[1]` keeps the eid embedded in the
key. A regex list lets us:

- Match `/api/v1/event/{eid}`, `/api/v1/event/{eid}/incidents`,
  `/api/v1/event/{eid}/odds/1/web-odds` etc. uniformly.
- Add new endpoints to `TARGET_ENDPOINTS` without touching `capture()`.
- Have a single source of truth: `TARGET_ENDPOINTS`.

### 3.3 Behaviour matrix (after fix) — STRICT REJECTION CONTRACT

| URL tail (input to `_normalize_endpoint_name`) | Expected return |
|---|---|
| `"event/14025013"` | `"event"` |
| `"event/14025013/incidents"` | `"incidents"` |
| `"event/14025013/lineups"` | `"lineups"` |
| `"event/14025013/shotmap"` | `"shotmap"` |
| `"event/14025013/graph"` | `"graph"` |
| `"event/14025013/statistics"` | `"statistics"` |
| `"event/14025013/comments"` | `"comments"` |
| `"event/14025013/odds/1/web-odds"` | `"web-odds"` |
| `"event/14025013/votes"` | `"votes"` |
| `"event/14025013/managers"` | `"managers"` |
| `"event/14025013/pregame-form"` | `"pregame-form"` |
| `"event/14025013/average-positions"` | `"average-positions"` |
| `"event/14025013/highlights"` | `"highlights"` |
| `"event/14025013/featured-players"` | `"featured-players"` |
| `"event/14025013/achievements"` | `"achievements"` |
| `"event/14025013/"` (trailing slash) | `"event"` (stripped before matching) |
| `""` | `None` (rejected) |
| `None` (non-string) | `None` (rejected) |
| `"event/abc"` (non-numeric eid) | `None` (rejected) |
| `"event/14025013/unknown-subpath"` | `None` (rejected) |
| `"unique-tournament/17/season/48981/events/last/0"` | `None` (not in `TARGET_ENDPOINTS`) |
| `"event/14025013?foo=1"` (query string) | `None` (rejected) |
| `"event/14025013#section"` (fragment) | `None` (rejected) |
| `"EVENT/14025013/incidents"` (uppercase) | `None` (rejected) |
| `"event/14025013/Incidents"` (mixed case) | `None` (rejected) |

### 3.4 Rejection contract (per Blocking Condition #2)

`_normalize_endpoint_name(url_tail: str) -> Optional[str]`:

- Returns the bare endpoint name from `TARGET_ENDPOINTS` **if and only if**
  the URL tail matches one of the compiled patterns after stripping a
  single trailing slash.
- Returns `None` for any malformed / unknown URL tail — including empty
  string, non-string input, query strings, fragments, uppercase variants,
  non-numeric eid, and patterns not declared in `TARGET_ENDPOINTS`.
- **Never** returns `"unknown"` or any other fallback string.
- **Never** raises; callers must handle `None` explicitly by logging and
  skipping the capture (do NOT insert into `api_data` / `api_status`).

### 3.5 `capture()` behaviour with rejection contract

```python
# NEW capture() in _WORKER_SCRIPT:
def capture(resp):
    url = resp.url
    if "/api/v1/" not in url or "sofascore.com" not in url:
        return
    try:
        # Extract tail after /api/v1/, strip trailing slash for matching
        tail = url.split("/api/v1/", 1)[-1].rstrip("/")
        ep = _normalize_endpoint_name(tail)
        if ep is None:
            # Unknown / malformed endpoint — log, do NOT pollute maps
            logger.warning(f"Unknown endpoint URL: {url} (tail={tail!r})")
            return  # skip silently; api_data/api_status untouched

        status = resp.status
        if status == 200:
            try:
                body = resp.json()
            except Exception:
                try:
                    body = resp.text()
                except Exception:
                    body = None
            api_data[ep] = body
        api_status[ep] = status
    except Exception:
        pass
```

This guarantees:
- `api_data` and `api_status` always use the **same** normalized key
  (when populated).
- Unknown endpoints **never** pollute either map.
- Caller (`fetch_event`) reads `result.data["event"]` /
  `result.api_status["event"]` and either gets the captured value or
  raises `KeyError` (handled by caller with documented fallback).

---

## 4. Diff (proposed, minimal)

### 4.1 `stable_proxy_fetch.py` — changes

**Insert** a helper function near the top of the module (after
`TARGET_ENDPOINTS`):

```python
import re

def _build_endpoint_patterns() -> list[tuple[re.Pattern, str]]:
    """Build (regex, endpoint_name) list from TARGET_ENDPOINTS."""
    patterns = []
    for name, path in TARGET_ENDPOINTS.items():
        # Escape regex metachars in the static parts, then replace {eid}
        regex_src = re.escape(path).replace(r"\{eid\}", r"(?P<eid>\d+)")
        patterns.append((re.compile(rf"^/{regex_src}$"), name))
    return patterns

_ENDPOINT_PATTERNS = _build_endpoint_patterns()


def _normalize_endpoint_name(url_tail: str) -> Optional[str]:
    """Map URL tail (after /api/v1/) to canonical endpoint name.

    Returns the bare endpoint name from TARGET_ENDPOINTS if the tail matches
    a declared pattern after stripping a single trailing slash.

    Returns None for any malformed / unknown URL tail — including empty
    string, non-string input, query strings, fragments, uppercase variants,
    non-numeric eid, and patterns not declared in TARGET_ENDPOINTS.

    Per Blocking Condition #2: no silent fallback to 'unknown' or any
    other string. Rejection is explicit. Never raises.
    """
    if not isinstance(url_tail, str) or not url_tail:
        return None
    # Strip single trailing slash (common in handcrafted URLs)
    tail = url_tail.rstrip("/") if url_tail != "/" else ""
    if not tail:
        return None
    # Reject query strings and fragments early
    if "?" in tail or "#" in tail:
        return None
    # Reject any uppercase component (case-sensitive match)
    if tail != tail.lower():
        return None
    for pattern, name in _ENDPOINT_PATTERNS:
        if pattern.match(tail):
            return name
    return None


class EndpointNormalizationError(Exception):
    """Reserved for callers that prefer explicit raising over None handling."""
    pass
```

**Modify** the embedded `capture()` function in `_WORKER_SCRIPT`:

```python
# OLD
parts = url.rstrip("/").split("/api/v1/")
ep = parts[1] if len(parts) > 1 else "unknown"

# NEW
tail = url.rstrip("/").split("/api/v1/")[-1]
ep = _normalize_endpoint_name(tail)
```

> **Important:** the worker script is currently a raw string
> (`_WORKER_SCRIPT = r'''...'''`). The replacement uses
> `_normalize_endpoint_name`, which must also be importable inside the
> worker subprocess. Two options:
>
> **Option A** — inline the helper in the worker string. Pro: zero
> external dependency. Con: code duplication.
>
> **Option B** — keep `_normalize_endpoint_name` importable from the
> top-level module and have the worker subprocess `sys.path.insert(0,
> REPO_ROOT)` before importing. Pro: single source of truth. Con: extra
> sys.path manipulation.
>
> **Recommendation:** Option B. The worker subprocess already runs the
> repo's Python interpreter (`/root/.openclaw/workspace/sofascore-
> backfill/.runner-venv/bin/python3`) and needs the repo on `sys.path`
> for `from stable_proxy_fetch import StableProxyFetcher` callers.
> Adding the helper import is consistent with that pattern.

### 4.2 Worker subprocess — additional `sys.path` setup

Inside `_WORKER_SCRIPT`, prepend:

```python
sys.path.insert(0, "/root/.openclaw/workspace/sofascore-backfill")
from stable_proxy_fetch import _normalize_endpoint_name
```

### 4.3 What is **not** changed

- `TARGET_ENDPOINTS` — unchanged.
- `fetch_event()` / `fetch_events_batch()` — unchanged.
- `_is_good_ip()` — unchanged.
- `MAX_IP_RETRIES`, cooldowns, timeouts — unchanged.
- `backfill_runner.py` — **unchanged** (gen2 authoritative).
- Any other module — **unchanged**.

---

## 5. Unit Tests

### 5.1 New file: `tests/test_endpoint_normalization.py`

Pure unit tests for `_normalize_endpoint_name` AND for the data/status
capture contract. **No network access**; must pass offline.

Per **Blocking Condition #1 (Tests first, then patch)**: these tests are
written and verified to FAIL on the current buggy code before the patch
is applied. Per **Blocking Condition #3 (Tests validate the data/status
contract)**: tests cover the normalized key in `TARGET_ENDPOINTS`,
**paired write to `api_data` and `api_status`**, malformed URL isolation,
and explicit behaviour for trailing slash / query string / fragment /
uppercase / non-API URLs.

### 5.2 Test cases (must include; minimum 13)

#### A. Endpoint string normalization (8 cases)

| # | URL tail | Expected return |
|---|---|---|
| A1 | `"event/14025013"` | `"event"` |
| A2 | `"event/14025013/incidents"` | `"incidents"` |
| A3 | `"event/14025013/lineups"` | `"lineups"` |
| A4 | `"event/14025013/shotmap"` | `"shotmap"` |
| A5 | `"event/14025013/graph"` | `"graph"` |
| A6 | `"event/14025013/statistics"` | `"statistics"` |
| A7 | `"event/14025013/comments"` | `"comments"` |
| A8 | `"event/14025013/odds/1/web-odds"` | `"web-odds"` |

#### B. Rejection contract (7 cases — must return `None`, not `"unknown"`)

| # | URL tail | Expected return | Reason |
|---|---|---|---|
| B1 | `""` | `None` | Empty input |
| B2 | `None` | `None` | Non-string input |
| B3 | `"event/abc/incidents"` | `None` | Non-numeric eid |
| B4 | `"event/14025013/unknown-subpath"` | `None` | Not in `TARGET_ENDPOINTS` |
| B5 | `"unique-tournament/17/season/48981/events/last/0"` | `None` | Not in `TARGET_ENDPOINTS` |
| B6 | `"event/14025013?foo=1"` | `None` | Query string |
| B7 | `"event/14025013#section"` | `None` | Fragment |

#### C. Edge cases (3 cases — explicit behaviour)

| # | URL tail | Expected return | Reason |
|---|---|---|---|
| C1 | `"event/14025013/"` (trailing slash) | `"event"` | Stripped before match |
| C2 | `"EVENT/14025013/incidents"` (uppercase) | `None` | Case-sensitive |
| C3 | `"event/14025013/Incidents"` (mixed case) | `None` | Case-sensitive |

**Total: 18 cases** (8 normalization + 7 rejection + 3 edge).

### 5.3 Data/status contract tests (additional)

These tests verify the **paired write to `api_data` and `api_status`**
when `capture()` is exercised against a mock response. They use a
lightweight fake `resp` object so no browser / network is involved.

| # | Scenario | Expected behaviour |
|---|---|---|
| D1 | `capture()` called with URL `https://www.sofascore.com/api/v1/event/14025013/incidents`, status 200 | Both `api_data["incidents"]` and `api_status["incidents"]` populated with same key |
| D2 | `capture()` called with URL `https://www.sofascore.com/api/v1/event/14025013`, status 200 | Both `api_data["event"]` and `api_status["event"]` populated |
| D3 | `capture()` called with URL `https://www.sofascore.com/api/v1/unique-tournament/17/season/48981/events/last/0`, status 200 | Neither map is touched (unknown endpoint rejected) |
| D4 | `capture()` called with non-Sofascore URL (e.g. `https://cdn.sofascore.com/...`) | Neither map is touched (non-API URL guard) |
| D5 | `capture()` called with URL `https://www.sofascore.com/api/v1/event/14025013/incidents?refetch=1`, status 200 | Neither map is touched (query string rejection) |
| D6 | `capture()` called twice with same normalized URL | Both maps contain exactly one entry per normalized key (no duplicate pollution) |

### 5.4 Run command

```bash
cd /root/.openclaw/workspace/sofascore-backfill
./.runner-venv/bin/python -m pytest tests/test_endpoint_normalization.py -v
```

All 18 endpoint tests + 6 contract tests must pass after the patch.

### 5.5 Pre-patch verification (per Blocking Condition #1)

Before applying any patch:

1. Add the test file with **expected-failing assertions** that target
   the bug (cases A1, A2, B3, B4, B6 must fail on the current code
   because the buggy normalizer returns `"event/14025013/incidents"` /
   `"unknown"` instead of `"incidents"` / `None`).
2. Run the tests; record the failure list.
3. This proves the tests genuinely catch the bug. Tests written after
   the patch would always pass and would not constitute evidence.

---

## 6. Isolated No-Write Validation

A new script `tests/validate_gen3_fix.py` that:

- Calls `StableProxyFetcher.fetch_event(eid)` for 2–3 events only.
- Verifies that `result.data["event"]` and `result.data["incidents"]` are
  **non-None dicts** (not missing keys, not empty).
- Verifies `result.api_status["event"] == 200` and same for `incidents`.
- **Does NOT** write to MySQL, **does NOT** modify `backfill_runner.py`,
  **does NOT** import or call `DataInserter`.
- **Every fetch operation is tagged `write_enabled: false`**.
- **No subprocess of `StableProxyFetcher` may import or invoke any
  MySQL / DBMS write code path** — verified by import-block check.
- Writes a JSON summary to `data/gen3_fix_validation_20260823_HHMMSS.json`.

### 6.1 Test events (reuse from benchmark)

| eid | season |
|---|---|
| 14025013 | PL 25/26 (newer, more API responses) |
| 12436870 | PL 24/25 |
| 8896967 | PL 20/21 (older, may have less coverage) |

### 6.2 Expected outcomes

For each event:

```json
{
  "eid": 14025013,
  "page_status": 200,
  "data.event": {"event": {...}},          // non-None
  "data.incidents": {"incidents": [...]},   // non-None
  "api_status.event": 200,
  "api_status.incidents": 200,
  "retries": 0,
  "latency_s": <10-60>,
  "write_enabled": false,
  "mysql_imports_detected": []
}
```

### 6.3 Hard-stop gates (per Blocking Condition #4)

| Trigger | Action |
|---|---|
| Any HTTP 407 in any event response | Immediate abort; mark `config_failure: true`; persist partial state |
| 2 consecutive subprocess fails (`fetch_event` returns `None`) | Immediate abort; mark `subprocess_fail_streak: true` |
| 403 rate > 50% across all 3 events | Immediate abort; mark `403_rate_exceeded: true` |
| Wall-time > 600s (10 min) | Immediate abort; mark `timeout: true` |
| Any MySQL / DataInserter import detected in subprocess trace | Immediate abort; mark `db_write_detected: true` |

When any gate triggers, **§7 shadow comparison is NOT run** and the
final report must explicitly call out which gate fired.

### 6.4 Sanitization gates

- **`.env` credentials never written** to logs, artifacts, or JSON
  output. The validation script logs only `proxy_configured: true` and
  host/port/user (no password, no full URL).
- **Worker subprocess stdout/stderr** is captured to a sanitized log
  with credential redaction.
- **All output files** are scanned for `dztr57tcycoz` / full proxy URL
  before being persisted; any leak aborts the run.

### 6.5 Decision criteria

| Outcome | Action |
|---|---|
| All 3 events have `data.event` and `data.incidents` populated | Proceed to §7 shadow comparison |
| ≥1 event missing `data.event` or `data.incidents` | Bug fix incomplete; investigate before next round |
| HTTP 407 encountered | Abort, fix env, retry |
| Subprocess fail streak ≥2 | Abort; treat as subprocess stability issue; defer |
| 403 rate > 50% | Abort; treat as anti-bot regression; defer |
| Any DB write detected | Abort immediately; treat as critical safety violation; file ticket |

---

## 7. Shadow Comparison (after §6 passes)

A separate run `tests/shadow_compare_gen2_vs_gen3.py` that:

- For 3–5 events, calls both:
  - `BackfillClient.fetch_api(path)` (gen2 authoritative)
  - `StableProxyFetcher.fetch_event(eid)` (gen3 candidate, read-only)
- **Does NOT** mutate either path's behaviour.
- **Does NOT** change default fetch_api behaviour.
- Writes a side-by-side report to
  `data/shadow_compare_20260823_HHMMSS.json`.

### 7.1 Per-endpoint comparison (per Blocking Condition #5)

Per **Blocking Condition #5**, HTTP success and data completeness are
**separate** pass criteria. The following 9 metrics are recorded for
each (event × endpoint) pair:

| # | Metric | Pass criterion | Type |
|---|---|---|---|
| 1 | `http_success_rate` | Both methods return 200 OR both return the same 4xx | binary |
| 2 | `endpoint_data_completeness` | Both methods' `data`/`body` is non-None and is a dict with expected top-level keys | binary |
| 3 | `status_key_completeness` | Both methods' status map contains the normalized endpoint key | binary |
| 4 | `403_rate` (per method) | Reported as raw percentage; parity expected | informational |
| 5 | `407_rate` (per method) | Reported as raw percentage; parity expected | informational |
| 6 | `latency_p50` (per method) | Reported in seconds; no pass/fail | informational |
| 7 | `latency_p90` (per method) | Reported in seconds; no pass/fail | informational |
| 8 | `retry_count` (per method, per event) | Reported as integer; no pass/fail | informational |
| 9 | `ip_scoring_result` (gen3 only) | `is_good_ip()` returned True at first attempt | binary |
| (extra) | `subprocess_failure_count` (gen3 only) | Reported as integer | informational |

### 7.2 Pass criteria (per Blocking Condition #5)

Gen3 is **only** considered fixed when **all three primary criteria**
pass for all (event × endpoint) pairs:

- ✅ `http_success_rate` = 100% parity (or documented 4xx parity)
- ✅ `endpoint_data_completeness` = 100% (both methods return non-None
  dict with expected top-level keys)
- ✅ `status_key_completeness` = 100% (both methods' status map
  contains the normalized key)

The other 6 metrics are **informational only** and feed into the
report but do not gate the pass/fail verdict.

### 7.3 Three-tier report conclusion (per 08:10 instruction)

The final report must explicitly separate three conclusions that are
**independent** of each other:

1. **Normalization fix status** — Did `_normalize_endpoint_name`
   correctly handle all 24 unit + contract tests?
2. **Gen3 isolated validation status** — Did 3-event no-write validation
   (§6) pass all hard-stop gates and produce non-None `data.event` /
   `data.incidents`?
3. **Whether gen3 is production-ready** — Did §7 shadow comparison meet
   the three primary pass criteria? **Even if 1 and 2 pass, 3 may
   still fail** (e.g. data completeness parity below threshold, or
   latency regresses).

### 7.4 Decision criteria after shadow

| Tier 1 (normalization) | Tier 2 (validation) | Tier 3 (production-ready) | Action |
|---|---|---|---|
| ✅ pass | ✅ pass | ✅ pass | Discuss feature-flag rollout with Kris (NOT auto-enabled) |
| ✅ pass | ✅ pass | ❌ fail | Defer; fix gaps before next round |
| ✅ pass | ❌ fail | (skipped per gate) | Investigate; possibly fix the capture integration in `fetch_event` |
| ❌ fail | (skipped per gate) | (skipped per gate) | Fix normalization; rerun §5 tests |

---

## 8. Out of Scope (deferred)

The following remain in the report's §5.3 follow-up list and are NOT
addressed in this Path 3 plan:

- Switching `fetch_api` to use gen3 as default (only after feature flag
  review).
- Round 2 endpoint coverage on `lineups` / `shotmap`.
- Long-run Method B (pure curl_cffi) stress test.
- Replacing `playwright_stealth` with CloakBrowser inside
  `backfill_runner.py`.
- Subprocess integration into `BackfillClient`.

---

## 9. Deliverables

1. **`docs/path3_fix_plan.md`** — this file (reviewed by Kris).
2. **`stable_proxy_fetch.py` patched** — capture normalization fix
   + helper + worker subprocess sys.path setup.
3. **`tests/test_endpoint_normalization.py`** — 13 unit tests, must pass
   with no network.
4. **`tests/validate_gen3_fix.py`** — isolated no-write validation on 3
   events. Writes JSON summary.
5. **`tests/shadow_compare_gen2_vs_gen3.py`** — side-by-side comparison
   on 3–5 events (only after §6 passes).
6. **Logs and artifacts** under `logs/` and `data/`, named with
   `20260823_*` timestamps.
7. **Final report** — summary of diff, unit test results, validation
   outcomes, shadow comparison, and any safety halts.

---

## 10. Review Checklist for Kris

Before any code is touched, please confirm:

- [ ] Fix scope is exactly as described in §3 (no other changes to
      `stable_proxy_fetch.py`).
- [ ] Unit test coverage matches §5.2.
- [ ] Validation script respects §6 safety gates.
- [ ] Shadow comparison is **read-only** and does not touch
      `backfill_runner.py`.
- [ ] No production traffic will be generated by any of these scripts
      during validation.
- [ ] Final review gate before any feature-flag rollout.

---

*Plan written 2026-08-23. Awaiting ✅ before implementation.*
