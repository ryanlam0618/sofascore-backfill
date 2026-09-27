# Season 25/26 Expansion Plan — Path A (18 additional competitions)

> **task_id**: `season-25-26-expansion-20260901`
> **agent**: coding (Forge)
> **date**: 2026-09-01 19:05 GMT+8
> **status**: PLAN ONLY — awaiting Kris review + explicit approval before any execution
> **scope**: expand 25/26 season coverage from 5 → 23 competitions (19 new listed; net 18 after excluding the already-done UCL, see §0)

---

## 0. Scope Clarification (important correction)

Kris's task lists **19 new competitions**, but let me reconcile against `competitions_10y.yaml` (24 total entries):

Already done in Stage 2 (5): Prem, La Liga, Serie A, Bundesliga, **UCL**.

The 19 new targets are:
- European league (1): Ligue 1 (34)
- Asian leagues (4): J1 (196), K League 1 (410), A-League Men (136), CSL (649)
- UEFA (2): UEL (679), UECL (17015)
- AFC (2): ACL (463), ACL Two (668)
- Domestic cups (10): FA Cup (19), EFL Cup (21), Copa del Rey (329), Coppa Italia (328), Coupe de France (335), DFB Pokal (217), J.League Cup (101), Emperor's Cup (323), Australia Cup (1786), Chinese FA Cup (882)

**1 + 4 + 2 + 2 + 10 = 19** ✓ (Kris's arithmetic is correct; 24 − 5 = 19).

> ⚠️ Notable: `competitions_10y.yaml` also lists **Chinese FA Cup ut_id=882** with a note "ut_id 882 confirmed via API 2026-07-01, name: CFA Cup" — but Kris's list says ut_id 882 too. Also `ACL Two` already listed under a different entry ("AFC Champions League Two", ut_id 668). So the yaml is the authoritative source. No conflict.

---

## 1. Season ID Discovery

### Method
- Endpoint: `GET /api/v1/unique-tournament/{ut_id}/seasons`
- Transport: **Gen4 fp v2** (chrome124 curl_cffi) + 21-IP good pool + rotate-per-request.
- Match season by `year` label `"25/26"` (or `seasonLabel`/`name` containing "25/26"), same approach as Stage 2's `recover_no_event_samples.py` and the `no_event` recovery (memory 2026-07-08).
- On success, write `season_id_2025_26: <id>` into `competitions_10y.yaml` and set `status: discovered_25_26`; on ut_id/season miss, set `status: blocked`.

### Cost
- 19 competitions × 1 GET = **19 calls** (plus up to 1 retry each = ≤38 calls).
- Wall-clock: **~5 min** (network-bound, no throttle needed at this volume).

### Discovery output artifact
- `data/season_25_26_discovery.json` (list of `{name, ut_id, season_id_2025_26, status}`).

---

## 2. Event Enumeration

### Method
- `GET /api/v1/unique-tournament/{ut_id}/season/{season_id}/events/last/{page}`.
- Paginate until `target` events collected (200 for leagues, min(available, 200) for cups) or page cap (40 pages).

### Cost
- 19 comps × ~5–10 pages = **95–190 calls** (~10–15 min).
- Cups will have fewer matches (single-elimination). Expected event counts:
  - Leagues/UEFA/AFC (9 comps): ~150–380 events each (full season), cap at 200.
  - Cups (10 comps): ~30–100 events each (25/26 cup may still be in early rounds at capture time).

| Group | Comps | est. events each | sub-total |
|---|---|---|---|
| League (Ligue 1 + 4 Asian) | 5 | ~200 | ~1000 |
| UEFA (UEL, UECL) | 2 | ~200 | ~400 |
| AFC (ACL, ACL Two) | 2 | ~150–200 | ~300–400 |
| Cups | 10 | ~50–100 | ~500–1000 |
| **Total** | 19 | — | **~2200–2800 events** |

---

## 3. Backfill Execution Plan

### Script
- New file `gen4_stage3_expansion_backfill.py`, reusing `gen4_stage2_backfill.py` architecture:
  - `COMPS` list (name, ut_id, season_id, cat_id, year_label, target).
  - `ENDPOINTS`: `event`, `lineups`, `statistics`, `incidents`, `shotmap`.
  - `api_get(path, max_tries=2)` rotating good_21 IP, chrome124 curl_cffi.
  - Idempotent `ON DUPLICATE KEY UPDATE` for every table via `backfill_runner.DataInserter`.

### ⚠️ Two Stage-2 bugs MUST be fixed in Stage 3 (discovered during lineups replay, 2026-09-01)

1. **team_id resolution bug** — lineups payload's player-level `teamId` is garbage (points to unrelated clubs); the correct source is `event.homeTeam.id` / `event.awayTeam.id` (== `matches.home_team_id`/`away_team_id`). Stage 2 mis-resolved ~26% of lineups rows (10,913 wrong-team orphans, since cleaned). Stage 3 `insert_match_lineups` must resolve `team_id` from the event payload / matches table, NOT from lineup `side.team` or `player.teamId`.

2. **`ensure_team`/`ensure_player` commit-per-call bottleneck** — each call does an individual `commit()`, ~40+ commits/event → ~50× slowdown (Stage 2 took 6.5h vs ~10 min when skipped). Stage 3 should batch: skip `ensure_*` when the FK already exists (verified 0 missing on Stage 2 data), or wrap ensures so they share a single commit per event.

### Endpoint set per event
5 endpoints: `event`, `lineups` (deep stats now persisted to `match_lineups`), `statistics`, `incidents`, `shotmap`.

### Wall-clock estimate (honest, evidence-based)

Stage 2 measured **23,524s (~6.5h)** for 5 comps × 200 events × 4 endpoints — but that was inflated by the `ensure_*` commit bottleneck. After fixing it (lineups replay), measured throughput was **~1.7 events/s** for a single endpoint, and full 1000-event single-endpoint replay completed in ~10 min.

Realistic Stage 3 estimate (5 endpoints/event, fixed ensure, network-bound):

| Group | events | endpoints | est. time (seq) |
|---|---|---|---|
| League (9 comps) | ~1800 | ×5 | **~3–5 h** |
| Cups (10 comps) | ~500–1000 | ×5 | **~1.5–3 h** |
| **Total** | ~2300–2800 | ×5 | **~4.5–8 h sequential** |

> Stage 3 can be split into 3 sub-phases (League → UEFA/AFC → Cups) with checkpoint artifacts after each, matching Kris's phasing.

---

## 4. Risk Assessment

| Risk | Severity | Mitigation |
|---|---|---|
| **Australia Cup (1786) / Chinese FA Cup (882) season availability** — 25/26 cup season may not have started, or ut_id may be stale | medium | Phase 1 discovery will surface `blocked`; Phase 2 smoke (5 events) confirms endpoint 200 before full run |
| **AFC season_mode difference** — ACL/ACL Two use `international` mode; season structure may differ from `national` leagues | medium | Verify season_id resolves + events/last returns rows in Phase 2 smoke |
| **Historical 403 (IP reputation)** — good_21 pool is known-good but degrades over time | medium | Only use good_21; rotate per request; per-IP drift log; early-stop 10 consecutive fails |
| **Fail rate** — Stage 2 measured 0.7% (7/1000) | low | Expect 0.5–2%; retry-once-per-endpoint already built in |
| **Cup events count** — cups may have <200 matches; some may be mid-season | low | Cap target at 200, accept fewer for cups (record actual) |
| **Schema drift** — new competitions may expose new stat fields | low | Parser uses mapping tables; idempotent UPSERT tolerant of missing keys |

---

## 5. Cost Estimate

| Metric | Estimate |
|---|---|
| Total HTTP calls | ~19 × 200 × 5 = **~19,000** (worst case ~38,000 with 1 retry each) |
| 21-IP pool utilization | ~900 calls/IP (spread over 4.5–8h; well under any single-IP burst threshold) |
| DB writes | ~19,000 matches + ~38,000 lineups + ~38,000 stats + ~30,000 incidents + ~38,000 shots |
| Wall-clock | **~4.5–8 h sequential** (split into 3 phases) |
| New code | 1 script `gen4_stage3_expansion_backfill.py` + discovery/smoke helpers |

---

## 6. Recommended Execution Phasing

1. **Phase 1 — Season ID discovery** (19 calls, ~5 min): resolve all 19 `season_id_2025_26`; mark `discovered_25_26` / `blocked`; emit `data/season_25_26_discovery.json`.
2. **Phase 2 — Smoke test** (~475 calls, ~30 min): 5 events × 5 endpoints per comp; verify gen4 fp v2 200s across all 19 comps; gate full run on ≥95% pass.
3. **Phase 3a — League + UEFA/AFC** (9 comps, ~1800 events, ~3–5h).
4. **Phase 3b — Cups** (10 comps, ~500–1000 events, ~1.5–3h).

---

## 7. Idempotency / Safety

- Reuse Stage 2 `ON DUPLICATE KEY UPDATE` pattern for all 5 endpoints.
- Early-stop after 10 consecutive failures.
- Per-IP drift log (ok/fail counts per IP) in artifact.
- Pre-flight `mysqldump --no-data appdb` schema snapshot → `migrations/20260901_pre_stage3_schema.sql`.
- No DROP / TRUNCATE anywhere (scope lock — Stage 3 is read-mostly + idempotent writes).
- FK safety: match_lineups deep-stat columns already ALTERed (Stage 1 of prior task); no further ALTER required unless new competitions surface new fields (out of scope).

---

## 8. Open Decisions for Kris

- **(a) Smoke test before full run?** → recommended **YES** (~30 min, catches AFC/cup ut_id issues early).
- **(b) Sequential vs parallel?** → recommended **sequential** (single 21-IP pool shared; parallel would double per-IP burst and raise 403 risk).
- **(c) Update `competitions_10y.yaml` in-sync?** → recommended **YES** (write `season_id_2025_26` + `status` during Phase 1; avoids future staleness).
- **(d) Include ACL / ACL Two?** → these are the highest-uncertainty competitions (international mode, possible season-mode mismatch). Recommend: include but gate on Phase 1+2 smoke; if either fails discovery, mark `blocked` and proceed with the other 17.

---

## 9. Reference

- `competitions_10y.yaml` — 24 comps, ut_ids, season_mode, status field (source of truth).
- `docs/lineups_deep_stats_persist_plan.md` — prior Stage 2 plan (parser/SQL patterns).
- `gen4_stage2_backfill.py` — Stage 2 architecture to reuse.
- `gen4_shotmap_replay.py` — shotmap endpoint pattern (throttle 0.15s, 2 tries, early-stop).
- `data/gen4_stage2_canary.json` — Stage 2 evidence: 99.3% (993/1000), 23524s, 21 IPs.
- `data/proxy_pools/good_21.txt` — 21-IP good pool.