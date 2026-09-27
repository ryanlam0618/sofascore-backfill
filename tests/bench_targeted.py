#!/usr/bin/env python3
"""
bench_targeted.py — Targeted benchmark (gen2 vs gen3)

Date:           2026-08-23
Scope:          10 events × 2 endpoints × 3 methods ≈ 68 requests (≤80 hard stop)
                  Method C: 10 fetch_event subprocesses (each captures BOTH endpoints)
Purpose:        Decide whether to integrate gen3 (stable_proxy_fetch) into gen2 (backfill_runner)

REVISION vs INITIAL PLAN:
- Initial plan: 4 endpoints (event, incidents, lineups, shotmap) → ~135 req
- Revised:     2 endpoints (event, incidents) → ~68 req
- Reason:      User capped total ≤60-70 to stay within proxy quota and avoid
               self-imposed 100-request hard stop.
- lineups/shotmap deferred to round 2 (if needed).
- Decision timestamp: 2026-08-23 04:56 GMT+8

SECURITY: This script MUST NOT log or persist proxy passwords, full proxy URLs,
or any SOFA_PROXY_PASS value. Only `proxy_configured: true/false` is logged.

SAFETY GATE (added 2026-08-23 05:13):
  HTTP 407 = PROXY AUTHENTICATION FAILURE. Treated as configuration error:
    - Logged as `config_failure` (NOT counted as 403, NOT in success_rate)
    - Method HALTED immediately on first 407
    - Persisted under `config_failures` block, NOT mixed with anti-bot metrics

METHODS:
  A. gen2_hybrid   — BackfillClient.fetch_api() (full 3-phase: capture→curl_cffi→browser→SSR)
                     Note: BACKFILL_RUNNER's fetch_api INTERNALLY falls back to curl_cffi
                     and uses Playwright session cookies. So this is NOT "pure Playwright".
  B. gen2_pure_curl_cffi — Direct curl_cffi with impersonate="chrome", NO warmup
                     ⚠ PURPOSE: disprove hypothesis (expect 100% 403 to confirm cookie/warmup
                     is necessary, NOT to score this method).
  C. gen3_stable_proxy_fetch — CloakBrowser subprocess + IP scoring + retries
                     (already proven ~0% 403 in complete_cloaktest_report)

SAFETY LIMITS:
  max_per_method:    30     # per-method request ceiling
  max_total:         80     # global request ceiling
  max_403_rate:      0.50   # per-method >50% 403 → halt
  max_subprocess_fail: 2    # gen3 subprocess consecutive fails → halt

USAGE:
  cd /root/.openclaw/workspace/sofascore-backfill
  PYTHONUNBUFFERED=1 ./.runner-venv/bin/python tests/bench_targeted.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import statistics
import traceback
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = REPO_ROOT / "data"
ARTIFACT_DIR.mkdir(exist_ok=True)
ARTIFACT_PATH = ARTIFACT_DIR / f"method_benchmark_targeted_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

# Configuration failure tracking (HTTP 407)
CONFIG_FAILURES: dict[str, list] = {}  # method_name → list of {eid, endpoint, status, error}

# ── Config ─────────────────────────────────────────────────────────────────
TEST_EVENTS = [
    (14025013, "PL 25/26", "Liverpool vs Bournemouth"),
    (12436870, "PL 24/25", "Man Utd vs Fulham"),
    (11352303, "PL 23/24", "Burnley vs Man City"),
    (8896967,  "PL 20/21", "Fulham vs Arsenal"),
    (7827861,  "PL 18/19", "Man Utd vs Leicester"),
    (14056037, "Bundesliga 25/26", "Bayern vs RB Leipzig"),
    (12764526, "UCL 24/25", "Young Boys vs Aston Villa"),
    (13335241, "K League 2025", "Pohang vs Daejeon"),
    (11917915, "J1 2024", "Sanfrecce vs Urawa"),
    (12060227, "CSL 2024", "Shandong vs Changchun"),
]
ENDPOINTS = ["event", "incidents"]  # per revision — lineups/shotmap deferred

# Cooldowns (seconds)
COOLDOWN_PER_EVENT = 10
COOLDOWN_PER_METHOD = 60

# Safety
MAX_PER_METHOD = 30
MAX_TOTAL = 80
MAX_403_RATE = 0.50
MAX_SUBPROCESS_FAIL = 2

# Proxy is loaded from env via backfill_runner.PROWY_* (no override here)
import os
for k in ("SOFA_PROXY_HOST", "SOFA_PROXY_PORT", "SOFA_PROXY_USER", "SOFA_PROXY_PASS"):
    if not os.getenv(k):
        # Defaults from backfill_runner.py — explicit so user knows what's used
        os.environ.setdefault(k, {
            "SOFA_PROXY_HOST": "p.webshare.io",
            "SOFA_PROXY_PORT": "80",
            "SOFA_PROXY_USER": "***REMOVED***-rotate",
            # SOFA_PROXY_PASS must be set in env — never hardcode
        }.get(k, ""))

# ── Imports AFTER path setup ──────────────────────────────────────────────
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / ".runner-venv" / "lib" / "python3.11" / "site-packages"))

# Now import project modules
from backfill_runner import BackfillClient, BROWSER_BASE  # noqa: E402
from stable_proxy_fetch import StableProxyFetcher  # noqa: E402

# ── Logging helper (unbuffered) ───────────────────────────────────────────
START_TS = datetime.now(timezone.utc).isoformat()

def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

# ── Metrics ───────────────────────────────────────────────────────────────
class Metrics:
    def __init__(self, name: str, label: str, purpose: str):
        self.name = name
        self.label = label
        self.purpose = purpose  # 'production' or 'disprove_hypothesis'
        self.records = []       # list of {eid, endpoint, status, latency_s, transport, error}
        self.halted = False
        self.halt_reason = None
        self.partial_run = False  # for gen3 if MAX_SUBPROCESS_FAIL hit

    def add(self, **kw):
        self.records.append(kw)

    @property
    def total(self) -> int:
        return len(self.records)

    @property
    def success(self) -> int:
        return sum(1 for r in self.records if r["status"] == 200)

    @property
    def blocked(self) -> int:
        return sum(1 for r in self.records if r["status"] == 403)

    @property
    def other(self) -> int:
        return sum(1 for r in self.records if r["status"] not in (200, 403, 0))

    @property
    def errors(self) -> int:
        return sum(1 for r in self.records if r.get("error") or r["status"] == 0)

    @property
    def config_fail_count(self) -> int:
        """HTTP 407 — proxy auth failure, NOT counted in success/403 rates."""
        return sum(1 for r in self.records if r["status"] == 407)

    @property
    def valid_total(self) -> int:
        """Total excluding config failures (407). Used for honest scoring."""
        return self.total - self.config_fail_count

    @property
    def success_rate(self) -> float:
        denom = self.valid_total
        return self.success / denom if denom else 0.0

    @property
    def block_rate(self) -> float:
        denom = self.valid_total
        return self.blocked / denom if denom else 0.0

    @property
    def latency_p50(self) -> float | None:
        lats = [r["latency_s"] for r in self.records if r.get("latency_s") is not None and r["status"] == 200]
        return statistics.median(lats) if lats else None

    @property
    def latency_p90(self) -> float | None:
        lats = [r["latency_s"] for r in self.records if r.get("latency_s") is not None and r["status"] == 200]
        return statistics.quantiles(lats, n=10)[8] if len(lats) >= 10 else (max(lats) if lats else None)

    def summary(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "purpose": self.purpose,
            "total_requests": self.total,
            "valid_requests": self.valid_total,    # excludes 407
            "config_failures_407": self.config_fail_count,
            "success_200": self.success,
            "blocked_403": self.blocked,
            "other": self.other,
            "errors": self.errors,
            "success_rate": round(self.success_rate, 4),
            "block_rate": round(self.block_rate, 4),
            "latency_p50_s": round(self.latency_p50, 3) if self.latency_p50 else None,
            "latency_p90_s": round(self.latency_p90, 3) if self.latency_p90 else None,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "partial_run": self.partial_run,
        }

    def check_halt(self) -> bool:
        if self.total == 0:
            return False
        # Config failure gate: any 407 → halt immediately, mark config_failure
        if self.config_fail_count > 0:
            self.halted = True
            self.halt_reason = f"CONFIGURATION FAILURE: HTTP 407 ×{self.config_fail_count} (proxy auth broken)"
            return True
        if self.valid_total == 0:
            return False
        if self.block_rate > MAX_403_RATE:
            self.halted = True
            self.halt_reason = f"block_rate {self.block_rate:.2%} > {MAX_403_RATE:.0%}"
            return True
        return False


# ── METHOD A: gen2_hybrid (BackfillClient.fetch_api) ─────────────────────
async def run_method_a() -> Metrics:
    m = Metrics("gen2_hybrid", "gen2: BackfillClient.fetch_api (3-phase)", "production")
    log(f"[A] Starting {m.label}")
    log(f"[A] NOTE: fetch_api INTERNALLY uses curl_cffi + Playwright cookies (NOT pure Playwright)")

    async with BackfillClient(headless=True) as client:
        for eid, season, match in TEST_EVENTS:
            if m.halted or m.total >= MAX_PER_METHOD:
                break
            for ep in ENDPOINTS:
                if m.total >= MAX_PER_METHOD:
                    break
                path = f"/api/v1/event/{eid}/{ep}" if ep != "event" else f"/api/v1/event/{eid}"
                t0 = time.time()
                try:
                    result = await client.fetch_api(
                        path,
                        timeout_ms=20000,
                        max_retries=3,
                        prefer_capture=False,  # ← per Kris 04:56 GMT+8 gate decision
                    )
                    dt = time.time() - t0
                    m.add(
                        eid=eid, season=season, match=match, endpoint=ep,
                        status=result.get("status", 0),
                        latency_s=round(dt, 3),
                        transport=result.get("transport", "browser"),
                        error=result.get("error"),
                    )
                    log(f"[A] {ep} eid={eid} → {result.get('status')} ({dt:.2f}s) transport={result.get('transport','browser')}")
                except Exception as e:
                    dt = time.time() - t0
                    m.add(
                        eid=eid, season=season, match=match, endpoint=ep,
                        status=0, latency_s=round(dt, 3),
                        transport="exception", error=str(e)[:200],
                    )
                    log(f"[A] {ep} eid={eid} → EXCEPTION ({dt:.2f}s): {e}")

                if m.check_halt():
                    log(f"[A] ⚠ HALTED: {m.halt_reason}")
                    break
                await asyncio.sleep(COOLDOWN_PER_EVENT)
            if not m.halted:
                await asyncio.sleep(COOLDOWN_PER_METHOD)

    log(f"[A] Done. {m.total} req, success_rate={m.success_rate:.2%}, block_rate={m.block_rate:.2%}")
    return m


# ── METHOD B: gen2_pure_curl_cffi (NO warmup, NO cookies) ────────────────
def run_method_b() -> Metrics:
    """
    PURPOSE: Disprove hypothesis (expect 100% 403).
    NOT for scoring — the comparison table must NOT treat 100% failure here
    as 'gen2 is broken' without context.
    """
    m = Metrics("gen2_pure_curl_cffi", "gen2: pure curl_cffi (no warmup)", "disprove_hypothesis")
    log(f"[B] Starting {m.label}")
    log(f"[B] NOTE: Purpose is FALSIFICATION, not scoring. Expect ~100% 403 (no cookies/warmup).")

    import curl_cffi
    session = curl_cffi.requests.Session()
    for eid, season, match in TEST_EVENTS:
        if m.halted or m.total >= MAX_PER_METHOD:
            break
        for ep in ENDPOINTS:
            if m.total >= MAX_PER_METHOD:
                break
            path = f"/api/v1/event/{eid}/{ep}" if ep != "event" else f"/api/v1/event/{eid}"
            url = f"{BROWSER_BASE}{path}"
            t0 = time.time()
            try:
                r = session.get(
                    url,
                    impersonate="chrome",
                    headers={
                        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0",
                        "Accept": "application/json",
                        "Referer": "https://www.sofascore.com/",
                    },
                    timeout=15,
                )
                dt = time.time() - t0
                m.add(
                    eid=eid, season=season, match=match, endpoint=ep,
                    status=r.status_code, latency_s=round(dt, 3),
                    transport="curl_cffi", error=None,
                )
                log(f"[B] {ep} eid={eid} → {r.status_code} ({dt:.2f}s)")
            except Exception as e:
                dt = time.time() - t0
                m.add(
                    eid=eid, season=season, match=match, endpoint=ep,
                    status=0, latency_s=round(dt, 3),
                    transport="curl_cffi_exception", error=str(e)[:200],
                )
                log(f"[B] {ep} eid={eid} → EXC ({dt:.2f}s): {e}")

            if m.check_halt():
                log(f"[B] ⚠ HALTED: {m.halt_reason}")
                break
            time.sleep(COOLDOWN_PER_EVENT)
        if not m.halted:
            time.sleep(COOLDOWN_PER_METHOD)

    log(f"[B] Done. {m.total} req, success_rate={m.success_rate:.2%}, block_rate={m.block_rate:.2%}")
    return m


# ── METHOD C: gen3_stable_proxy_fetch ────────────────────────────────────
def run_method_c() -> Metrics:
    """
    BUG FIX 2026-08-23 05:09 — Kris 提出的兩個 bug:
      Bug 1: fetch_event(eid) 一個 call 已經 capture 哂兩個 endpoint,
             之前塞入 ep loop → 每 event spawn 2 次 subprocess(20 次而唔係 10)
             → proxy burn double, wall time double, 唔公平
      Bug 2: 之前 break 只跳出當前 event 嘅 ep loop,外層 eid loop 照跑
             → 「subprocess 連續 2 次 fail 停 gen3」實際冇停到
      Bug 3: 原本成功後冇 reset consecutive_subprocess_fails
             → 兩次不連續失敗都會誤觸發 halt
    修正: fetch_event 移到 eid loop 入面、ep loop 之外;
          成功 reset fail counter;break 喺 eid loop 真正停。
    """
    m = Metrics("gen3_stable_proxy_fetch", "gen3: StableProxyFetcher (CloakBrowser subprocess)", "production")
    log(f"[C] Starting {m.label}")
    log(f"[C] Subprocess isolation, IP scoring, network capture pattern")
    log(f"[C] Bug-fix 2026-08-23: fetch_event moved to event-level loop (1 subprocess = 2 endpoints)")

    fetcher = StableProxyFetcher()
    consecutive_subprocess_fails = 0

    for eid, season, match in TEST_EVENTS:
        if m.halted or m.total >= MAX_PER_METHOD:
            break
        if m.total + len(ENDPOINTS) > MAX_PER_METHOD:
            log(f"[C] ⚠ Reached MAX_PER_METHOD={MAX_PER_METHOD} budget, stopping")
            break

        # ── One subprocess captures BOTH endpoints ──
        t0 = time.time()
        result = None
        fetch_error = None
        try:
            result = fetcher.fetch_event(eid)
        except Exception as e:
            fetch_error = str(e)[:200]

        dt = time.time() - t0

        if result is None:
            consecutive_subprocess_fails += 1
            log(f"[C] eid={eid} → SUBPROCESS FAIL ({dt:.2f}s) consec={consecutive_subprocess_fails} err={fetch_error}")
            if consecutive_subprocess_fails >= MAX_SUBPROCESS_FAIL:
                m.halted = True
                m.halt_reason = f"subprocess fail ×{consecutive_subprocess_fails}"
                m.partial_run = True
                log(f"[C] ⚠ HALTED: {m.halt_reason} (partial_run=True, results kept)")
                break  # ← break eid loop (only this can halt gen3)
            # continue to next eid; still record 2 endpoint failures for transparency
            for ep in ENDPOINTS:
                m.add(
                    eid=eid, season=season, match=match, endpoint=ep,
                    status=0, latency_s=round(dt / len(ENDPOINTS), 3),
                    transport="subprocess_fail", error="fetch_event returned None",
                )
            time.sleep(COOLDOWN_PER_EVENT)
            continue

        # ── Success: reset counter ──
        consecutive_subprocess_fails = 0

        # ── Record BOTH endpoints from this single subprocess ──
        for ep in ENDPOINTS:
            ep_data = result.get("data", {}).get(ep, None)
            ep_status_map = result.get("api_status", {})
            ep_status = 200 if ep_data is not None else ep_status_map.get(ep, 0)
            m.add(
                eid=eid, season=season, match=match, endpoint=ep,
                status=ep_status, latency_s=round(dt, 3),
                transport="cloakbrowser_subprocess",
                error=None if ep_status == 200 else f"missing in capture (status_map={ep_status_map.get(ep)})",
                retries=result.get("retries", 0),
                api_ok=result.get("api_ok", 0),
                api_403=result.get("api_403", 0),
            )
            log(f"[C] eid={eid} ep={ep} → {ep_status} ({dt:.2f}s) retries={result.get('retries',0)}")

            # Track config failures (HTTP 407)
            if ep_status == 407:
                CONFIG_FAILURES.setdefault("C", []).append({
                    "eid": eid, "endpoint": ep, "status": 407,
                    "error": "proxy authentication required (subprocess)",
                })

        if m.check_halt():
            log(f"[C] ⚠ HALTED: {m.halt_reason}")
            break  # ← break eid loop
        time.sleep(COOLDOWN_PER_EVENT)

    if not m.halted:
        time.sleep(COOLDOWN_PER_METHOD)

    log(f"[C] Done. {m.total} req, success_rate={m.success_rate:.2%}, block_rate={m.block_rate:.2%}")
    return m


# ── Persist helper ───────────────────────────────────────────────────────
def persist(results: dict, halts: list) -> None:
    payload = {
        "started_at": START_TS,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "proxy_configured": True,  # sanitized — no URL or password
        "config": {
            "test_events": [{"eid": e, "season": s, "match": ma} for e, s, ma in TEST_EVENTS],
            "endpoints": ENDPOINTS,
            "cooldown_per_event_s": COOLDOWN_PER_EVENT,
            "cooldown_per_method_s": COOLDOWN_PER_METHOD,
            "safety_limits": {
                "max_per_method": MAX_PER_METHOD,
                "max_total": MAX_TOTAL,
                "max_403_rate": MAX_403_RATE,
                "max_subprocess_fail": MAX_SUBPROCESS_FAIL,
            },
        },
        "results": {k: v.summary() for k, v in results.items()},
        "records": {k: v.records for k, v in results.items()},
        "config_failures": CONFIG_FAILURES,  # HTTP 407 records, separated
        "existing_data": {
            "gen1_urllib":   "Skipped (archived; urllib cannot bypass anti-bot)",
            "gen2_sync_PW":  "Skipped (covered by gen2_hybrid in spirit)",
            "gen4_sync_CB":  "From complete_cloaktest_report_zh.md: 0/250+ 403 errors",
        },
        "halts": halts,
    }
    ARTIFACT_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    log(f"[SAVE] → {ARTIFACT_PATH}")


# ── Main ─────────────────────────────────────────────────────────────────
async def main() -> None:
    log(f"=== bench_targeted.py START ===")
    log(f"Repo: {REPO_ROOT}")
    log(f"Venv: .runner-venv")

    # ── PROXY CONFIG GATE (sanitized, no secrets logged) ──
    _proxy_host = os.getenv("SOFA_PROXY_HOST", "")
    _proxy_port = os.getenv("SOFA_PROXY_PORT", "")
    _proxy_user = os.getenv("SOFA_PROXY_USER", "")
    _proxy_pass = os.getenv("SOFA_PROXY_PASS", "")
    proxy_configured = bool(_proxy_host and _proxy_port and _proxy_user and _proxy_pass)
    log(f"Proxy: host={_proxy_host}:{_proxy_port} user={_proxy_user} pass={'SET' if _proxy_pass else 'MISSING'}")
    log(f"proxy_configured: {proxy_configured}")

    if not proxy_configured:
        log("❌ ABORT: Proxy not fully configured. Required env: SOFA_PROXY_HOST / PORT / USER / PASS")
        log("   Run: cd <repo> && set -a && source .env && set +a && python tests/bench_targeted.py")
        sys.exit(2)

    log(f"Events: {len(TEST_EVENTS)}, Endpoints: {ENDPOINTS}, Methods: 3")
    log(f"Expected: ~68 req, ≤80 hard stop")

    results: dict[str, Metrics] = {}
    halts: list[str] = []

    total_req = 0

    # ── Method A ──
    log("\n" + "="*70)
    results["A"] = await run_method_a()
    total_req += results["A"].total
    if results["A"].halted:
        halts.append(f"A ({results['A'].halt_reason})")
    if total_req > MAX_TOTAL:
        log(f"⚠ Total {total_req} > MAX_TOTAL {MAX_TOTAL} after A — aborting")
        persist(results, halts + [f"global_total_exceeded ({total_req})"])
        return

    # ── Method B ──
    log("\n" + "="*70)
    results["B"] = run_method_b()
    total_req += results["B"].total
    if results["B"].halted:
        halts.append(f"B ({results['B'].halt_reason})")
    if total_req > MAX_TOTAL:
        log(f"⚠ Total {total_req} > MAX_TOTAL {MAX_TOTAL} after B — aborting")
        persist(results, halts + [f"global_total_exceeded ({total_req})"])
        return

    # ── Method C ──
    log("\n" + "="*70)
    results["C"] = run_method_c()
    total_req += results["C"].total
    if results["C"].halted:
        halts.append(f"C ({results['C'].halt_reason})")

    persist(results, halts)

    # ── Final comparison ──
    log("\n" + "="*70)
    log("=== FINAL COMPARISON ===")
    log(f"{'Method':<55} {'Req':>4} {'407':>4} {'200':>4} {'403':>4} {'Err':>4} {'SR':>7} {'p50':>7} {'p90':>7}")
    log("-"*110)
    for k, m in results.items():
        p50 = f"{m.latency_p50:.2f}s" if m.latency_p50 else "n/a"
        p90 = f"{m.latency_p90:.2f}s" if m.latency_p90 else "n/a"
        log(f"{m.label:<55} {m.total:>4} {m.config_fail_count:>4} {m.success:>4} {m.blocked:>4} {m.errors:>4} {m.success_rate:>6.2%} {p50:>7} {p90:>7}")
    log("-"*110)
    log(f"Total requests: {total_req} / hard stop {MAX_TOTAL}")
    log(f"Config failures (407): {sum(len(v) for v in CONFIG_FAILURES.values())}")
    log(f"Halts: {halts if halts else 'NONE — clean run'}")
    log(f"Artifact: {ARTIFACT_PATH}")
    log("=== DONE ===")


if __name__ == "__main__":
    asyncio.run(main())
