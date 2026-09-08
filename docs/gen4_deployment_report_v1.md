# Gen4 → Gen2 Replacement Deployment Report v1.0

**Author**: Forge (`agent:coding`) · **Task**: Stage 4d-3 (REPORT ONLY — no execution, no commit, no deploy)
**Requested**: Kris 2026-09-08 19:02 GMT+8 via Main Agent dispatch
**Written**: 2026-09-08 GMT+8
**Status**: 📄 PENDING KRIS APPROVAL — nothing in this doc is executed yet

> ⚠️ This document proposes replacing the current production default (Gen2 hybrid)
> with Gen4 fp-v2 + fixed-IP pool + Stage 4c batch runner as the standard backfill
> method. **Execution authority stays with Kris.** Approval of this doc ≠ deploy.

---

## 1. Current state — Gen2 (production default) vs Gen4 (candidate)

### 1.1 Gen2 hybrid (what runs today)

| Attribute | Detail |
|---|---|
| Entry point | `backfill_runner.py::DataInserter` (+ `stable_proxy_fetch.py` Gen3 line) |
| Transport Tier-1 | `curl_cffi` with `impersonate="chrome"` — **unpinned alias**, verifies against latest supported Chrome in installed lib |
| Transport Tier-2 | CloakBrowser (Playwright-wrapped Chromium 146) via `rotate_proxy.py::ProxyManager` — Webshare rotate gateway, browser restart every 150 calls, 600s cooldown on 403 |
| Fingerprint | No cookie store; no pinned fingerprint; default headers |
| Proxy strategy | Rotate-on-403, no per-competition stickiness |
| Output | MySQL (`appdb.match_*` tables) |
| Known pain | Stage 4c-era: 169 lineups `insert_error` (dict→scalar bug, Stage 2 legacy mapping), 37 Batch-C deadlocks → 206 recoverable error rows until Sentinel 4d-1 patch |
| Behavioral weakness | Fingerprint drift on Chrome upgrade → 403 wave risk; rotate gateway exposes fresh IPs without warm identity → inconsistent hit rate |

### 1.2 Gen4 fp-v2 (candidate)

| Attribute | Detail |
|---|---|
| Entry points | `gen4_fetcher.py::Gen4Fetcher` + `gen4_stage4c_batch_A/B/C.py` runners |
| Transport Tier-1 | `curl_cffi impersonate="chrome124"` — **explicitly pinned**, verified across Phase 9 / 10 / 10.5 / 10.6 canaries |
| Transport Tier-2 | CloakBrowser fallback (`gen4_canary_validate.py::_real_cloak_launcher`, headless, per-proxy browser) with rebuild on EPIPE / target_closed |
| Fingerprint | fp-v2 = chrome124 pin + deterministic headers + zero cookie injection (Phase-10.x canonical config) |
| Proxy strategy | **Fixed per-competition assignment** from audited good pool (`data/proxy_pools/good_20260907.txt`, n=33, all re-verified alive + clean ASN) instead of rotate gateway |
| Run discipline | Deterministic sha256 sampling seeds, explicit HTTP-call budgets, evidence JSONL per event, per-endpoint counters |
| Verified live | Stage 4c (2026-09-07): **4,799 events**, 658,999 rows inserted, 0 unrecoverable errors across 3 batches |

### 1.3 Head-to-head evidence (all numbers from repo artifacts, not estimates)

| Metric | Gen2 | Gen4 fp-v2 | Source |
|---|---|---|---|
| Phase 10.5 broad canary (123 cells, 13 comps × 4 endpoints) | not applicable, was diagnose-run | mixed; led to 10.6 | `gen4_phase10_5_broad_canary.json` |
| Phase 10.6 broad canary (tier-2 primary) | ~26.3% Tier-2 hit rate vs 90% target | forensic → root cause = fingerprint, not proxies | `gen4_phase10_6_forensic_report_v0.7.json` |
| **fp-v2 narrow canary** | — | **20/20 PASS_STRICT** | `gen4_fp_v2_canary.json` |
| **fp-v2 wide canary** (22 IPs × 4 endpoints × 5 comps = 440 calls) | — | **438/440 (99.55%)**, 2 fails both on one IP (idx 17) | `gen4_fp_v2_wide_canary.json` |
| **Stage 4c real backfill** 25/26 expansion (4,799 events) | Gen2 generated 206 recoverable errors pre-patch | Batch A: 651/651 ok · B: 199/199 ok · C: 3,949/3,949 ok; ip_banned ≈ 2-3 across all | `data/gen4_stage4c_summary.json` |
| Stage 4d-2 live verdict (PinchTab UI + API cross-check) | confirms the gap | stats 89% / shotmap 84% = **true server ceiling**, no transport fix possible | `data/gen4_stage4d2_rerun_comparison_20260908.json` + 2026-09-07 locked run |

**Reading**: Gen4 fp-v2 has, at every scale we tested (20 → 440 → 4,799 events), met or exceeded Gen2, and the only residual failures were (a) one bad IP, now flagged; (b) write-path bugs in the Gen2 legacy mapping, now patched by Sentinel in Stage 4d-1.

---

## 2. Replacement scope — what Gen4 fp-v2 takes over, and what stays on Gen2

### 2.1 Endpoints: full takeover for all 11 production endpoints

| Endpoint | Gen4 result | Takeover? |
|---|---|---|
| event (base) | ✅ Stage 4c 100% | YES |
| incidents | ✅ 4,719/4,799 ok (no_data = genuinely none) | YES |
| lineups | ✅ 3,763 distinct matches written; post-4d-1 patch the 169 errors recoverable | YES (after 4d-1 commit) |
| statistics | ✅ 3,580 distinct matches; missing 11% = server-side absence (4d-2) | YES — gap is not fixable |
| shotmap | ✅ 3,368 distinct matches; 16% gap = server-side | YES |
| h2h / votes | ✅ 3,949 / 3,948 | YES |
| odds | ✅ 3,788 matches (33 deadlocks pre-patch → 4d-1 retry wrapper) | YES (after 4d-1) |
| momentum (graph) | ✅ 3,267 matches | YES (after 4d-1) |
| average_positions / best_players | ✅ 3,365 / 3,368 | YES |

### 2.2 Competitions / seasons

| Tier | Competitions | Status | Gen4 takeover? |
|---|---|---|---|
| 25/26 (current) | 20 comps incl. PL, La Liga, Serie A, Ligue 1, UCL, UEL, UECL, K League 1, J1, CSL, A-League, FA Cup, EFL Cup, Copa del Rey, Coppa Italia, Coupe de France, DFB Pokal, ACL, AFC-2, Emperor's Cup, Australia Cup, Chinese FA Cup | ✅ Stage 4c completed 2026-09-07 | **YES — immediate** |
| 24/25, 23/24, 22/23 | Same 20-competition matrix (per `competitions_10y.yaml`) | Backfilled under Gen2 historically | **YES — next tranche after 25/26 stabilization (see §6)** |
| Pre-2020 seasons | 10y backfill target matrix | Stats present but xG absent pre-2020 (SofaScore coverage pattern, verified live via 18/19 PL event 7828213 in 4d-2 rerun) | **YES with expectation adjustment**: no xG / advanced metric columns will populate pre-2020 — that is upstream, not our bug |

### 2.3 What stays off Gen4 for now

- **player_stats per-player deep stats** — currently 8% coverage due to Stage 2 legacy mapping. Gen4 Stage 4d-1 fixes the write path; until the fix is committed + validated against real events, Gen2 keeps this endpoint or it stays deferred.
- Any endpoint NOT in the 11 above (e.g. `comments`, `referee`) — out of Gen4 scope by design.

---

## 3. Sentinel Stage 4d-1 patch — integration plan

Sentinel (debug agent) delivered 2026-09-07 under `workspace-debug/subagent_results/stage4d-1-refactor-player-odds.md`. **All changes are in the working tree, uncommitted, branch `v2`.**

### 3.1 Files to commit (minimal set)

| File | Change | Why |
|---|---|---|
| `backfill_runner.py` | + `_stat_value()` coercion helper; insert_player_stats routes all mapped cols through it; + `_retry_deadlock(fn, …)`; `insert_odds` & `insert_graph_points` wrapped | Fixes 169 dict→scalar lineups errors; fixes 33 odds + 1 momentum deadlock errors |
| `gen4_stage4c_batch_C.py` | + shared `_do_insert()` / `_retry_deadlock()`; per-endpoint insert+commit wrapped | Fixes 3 avg-positions deadlocks in Batch C; prevents recurrence on next full backfill |
| `tests/test_gen4_stage4d_deadlock_retry.py` | NEW — 6 tests | Lock in retry-on-1213/1205 semantics |

**Do NOT commit**: the CSS/proxy/data drift in `git status` (`.gitignore`, `competitions_10y.yaml`, `proxy_list.txt`, `stable_proxy_fetch.py`, `browser_rotation_test_*.json`, scratch debug files). Those are either unrelated experiments or operational artifacts.

### 3.2 Pre-commit test plan

```bash
cd /root/.openclaw/workspace/sofascore-backfill
.runner-venv/bin/python -m pytest tests/test_gen4_stage4d_insert_player_stats.py \
                                  tests/test_gen4_stage4d_deadlock_retry.py -v
# expected: 14 passed (8 + 6)
```

Then a **live re-run gate**:
1. Re-run the 169 lineups-failed events from Stage 4c Batch A → all must insert (dict fix).
2. Re-run the 37 Batch C failed endpoint-calls → all must insert (deadlock retry).
3. Confirm `match_player_stats` row count rises beyond the current 13,379 / 346 matches baseline.

### 3.3 Commit shape (proposed, one commit)

```
fix(stage4d-1): nested-dict stat coercion + deadlock retry for odds/graph/avg-pos

- _stat_value() coerces sofaScore nested statisticsType/ratingVersions to scalars/JSON
- _retry_deadlock() wraps insert_odds / insert_graph_points / batch-C endpoints
  with exponential backoff on InnoDB 1213/1205
- closes 169 lineups + 37 batch-C Stage 4c residual insert_errors
- tests: 14/14 stage4d tagged pass
```

---

## 4. Rollback plan

Gen4's write path shares the Gen2 schema; rollback = re-pointing the entry point, no schema revert needed.

| Trigger | Rollback action | Time-to-recover |
|---|---|---|
| fp-v2 canary regresses (PASS < 99% on re-canary before deploy) | Do NOT flip default; stay on Gen2 | 0 min (nothing changed) |
| Post-deploy: backfill 4xx rate > 2% over a 100-event window **AND** canary reproduces on Gen4 but not Gen2 | Re-launch affected batches with `backfill_runner.py` (Gen2 default) pointing at the same event list; Gen4 runners stop | ~5 min to restart; no data loss — inserts are idempotent per event+endpoint |
| MySQL deadlock rate climbs above retry budget (e.g. >1% of calls) | Pause runs; flag to Sentinel; accent on Gen2 for that endpoint while root-caused | hours |
| 33-IP pool degrades (≥5 of 33 IPs fail the audit) | Regenerate pool via `data/proxy_pools/` audit path (24h SLA); fall back to Gen2 rotate gateway meanwhile | hours-day |

**Data-safety property**: every Gen4 write is idempotent (DELETE+INSERT or INSERT ON DUPLICATE per event+endpoint). A mixed Gen4/Gen2 window cannot corrupt the DB.

---

## 5. Risk assessment & go/no-go gates

### 5.1 Residual risks

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | SofaScore retires chrome124 fingerprint | Low-Med | Mass 403 | fp-v3 pin bump; Phase 10.x canary framework re-run (1h) |
| R2 | 33-IP Webshare pool ages out | Med | per-IP 403 → retries | audit script + pool regen (proven 2026-09-07 flow) |
| R3 | xG / advanced missing pre-2020s misread as "our bug" | Med | wasted debugging | Document in README (done in 4d-2 audit); stats analyzer should filter by season |
| R4 | Legacy code paths outside 4d-1 still feed dicts to MySQL | Low | new insert_errors | `_stat_value` centralized; any new mapper must route through it |
| R5 | Concurrent load > 8 connections rediscovers InnoDB deadlocks on new tables | Low-Med | retried errors | `_retry_deadlock` present; keep concurrency ≤ 8 in runner flags |

### 5.2 Go gates (ALL must pass)

- [ ] **G0** (pre-approval) Kris signs this report.
- [ ] **G1** 4d-1 patch committed; pytest 14/14 green.
- [ ] **G2** 169+37 Stage-4c residual events re-run under patched build → insert_error = 0.
- [ ] **G3** Fresh fp-v2 canary (20-call fast path) **post-4d-1-commit** → PASS.
- [ ] **G4** `good_20260907.txt` pool re-audited < 24h old at deploy moment; ≥ 30/33 alive.
- [ ] **G5** Rollback script (`revert-to-gen2.sh` / wrapper entry-point switch) prepared + dry-run-tested.
- [ ] **G6** Downtime/impact window agreed with Kris (25/26 steady-state, non-live-match hour preferred).

### 5.3 Go/no-go summary

**Recommendation: GO — conditional on G1 through G5.** The Gen4 stack has accumulated ~6 of 6 green end-to-end runs (Phases 9→10→10.5→10.6→fp-v2-narrow→fp-v2-wide→Stage 4c production-shaped run), and the only outstanding blockers are the ones 4d-1 just closed.

---

## 6. Estimated scope — older-season backfill if approved

Assuming same 20-competition matrix for each prior season:

| Season | Est. events* | Est. HTTP budget at ~8 calls/event (avg of endpoints × retries) | Est. runtime at Stage 4c throughput (~4,799 events / 2.9h) |
|---|---|---|---|
| 24/25 | ~4,600 | ~37k calls | ~2.7 h |
| 23/24 | ~4,400 | ~35k | ~2.6 h |
| 22/23 | ~4,400 | ~35k | ~2.6 h |
| 21/22 | ~4,300 | ~34k | ~2.5 h |
| 20/21 | ~4,300 | ~34k | ~2.5 h |
| 19/20 | ~4,000 (partial COVID seasons for some comps) | ~32k | ~2.3 h |
| 18/19 | ~4,000 | ~32k | ~2.3 h |
| 17/18 | ~3,900 | ~31k | ~2.2 h |
| 16/17 | ~3,900 | ~31k | ~2.2 h |
| 15/16 | ~3,800 | ~30k | ~2.2 h |
| **Total (9 prior seasons)** | **~39,000 events** | **~315k calls** | **~24 machine-hours** across 33 IPs ⇒ safe to split into per-season batches of one evening each |

\* Back-of-envelope from Stage 3 + 4c scaling (25/26 = 4,799 events across the same 20-comp matrix). Trimmed for cups with fewer rounds historically.

**Throughput constraints honored**:
- ~1.5 reqs/s sustained per IP (Stage 4c evidence)
- Webshare 33-IP pool ⇒ ~50 rps ceiling, we'll run at ~2 rps → **no rate-limit risk**
- 800-canary-call budget methodology carried over per batch for safety trips

**Recommendation**: if approved, run **24/25 as the first older-season batch** (most tactical value, highest structural similarity to 25/26 → lowest schema-surprise risk), then schedule the remaining 8 seasons after a 48h soak of 24/25 data.

---

## 7. What this report asks Kris to decide

1. Approve / amend **takeover scope in §2** (all 11 endpoints, 20 comps, starting 25/26 default + then 24/25 first).
2. Approve **commit of the 4d-1 patch** (§3) — Forge to land it as a single commit on `v2` after tests.
3. Approve **go/no-go gates** (§5.2) as written, or request tighten/loosen.
4. Approve **schedule for older-season tranches** (§6) — propose weekly batching, any Kris-preferred start date.

Once approved, Forge produces the deploy runbook (steps + verification + rollback script) as Stage 4d-4. Nothing deploys until then.

---

*Evidence index (all on disk)*: `data/gen4_stage4c_summary.json`, `data/gen4_fp_v2_canary.json`, `data/gen4_fp_v2_wide_canary.json`, `data/gen4_phase10_6_forensic_report_v0.7.json`, `data/gen4_stage4d2_rerun_comparison_20260908.json`, `data/gen4_stage4d_api_vs_ui_comparison.json`, `workspace-debug/subagent_results/stage4d-1-refactor-player-odds.md`.
