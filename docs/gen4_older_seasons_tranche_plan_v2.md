# Gen4 Older-Seasons Tranche Plan v2 (15/16 → 23/24)

**Author**: Forge (`agent:coding`) · **Requested**: Kris 22:06 2026-09-08 via Main Agent — oldest-first, NOT 24/25-first
**Status**: 📄 REPORT ONLY — no backfill started; awaiting Kris green light per stage
**Supersedes**: `gen4_deployment_report_v1.md` §6 (which assumed 24/25 first)

---

## 1. Scope

Seasons **15/16, 16/17, 17/18, 18/19, 19/20, 20/21, 21/22, 22/23, 23/24** (9 seasons) × the 20-competition matrix from `competitions_10y.yaml` × 11 endpoints (event, incidents, lineups, statistics, shotmap, h2h, votes, odds, momentum, average_positions, best_players).

25/26 is already done (Stage 4c, 4,799 events). This plan covers everything older.

## 2. Data availability spot-check — oldest seasons (LIVE PinchTab, 2026-09-08 22:0x GMT+8)

Method: same PinchTab chrome session as Stage 4d-2; resolved season IDs via `/unique-tournament/{ut}/seasons`, picked the last played round's first finished event per comp-season, then browser XHR to `/api/v1/event/{id}/{statistics|shotmap|odds/1/all}`.

| League (ut_id) | 15/16 event | statistics | shotmap | odds | 16/17 | 17/18 |
|---|---|---|---|---|---|---|
| Premier League (17) | 6768365 (Arsenal v Norwich, 2016-04-30) | **200 / 12,558B** | **200 / 30,079B** | **200 / 590B** | 200/200/200 (7090315) | 200/200/200 (7438093) |
| La Liga (8) | 6807758 (Sporting v Eibar) | **200 / 13,083B** | **200 / 16,968B** | **200 / 591B** | 200/200/200 (7127784) | 200/200/200 (7490862) |
| Serie A (23) | 6830403 (Udinese v Torino) | **200 / 12,548B** | **200 / 33,002B** | **200 / 591B** | 200/200/200 (7142335) | 200/200/200 (7498347) |
| Bundesliga (35) | 6784846 (Frankfurt v Mainz) | **200 / 12,391B** | **200 / 19,822B** | **200 / 590B** | 200/200/200 (7108719) | 200/200/200 (7459172) |
| Ligue 1 (34) | 6771051 (Nantes v Nice) | **200 / 13,631B** | **200 / 24,357B** | **200 / 593B** | 200/200/200 (7080217) | 200/200/200 (7438669) |

**Result: 15/15 comp-season samples × 3 endpoints = ALL HTTP 200.** No endpoint exclusion needed, even for 15/16.

### Payload richness caveats (set expectations)

- **No xG anywhere pre-2020** (verified): 15/16 PL statistics = 21 items, has Ball possession / Passes / Accurate passes; **no "Expected goals (xG)", no Big chances**.
- **Shotmap pre-2020 carries coordinates only**: key inventory on event 6768365 shows `playerCoordinates`, `goalMouthLocation/Coordinates`, `bodyPart`, `shotType`, `situation`, `blockCoordinates` — **no `xg` / `xgot` keys at all** (26 shots checked).
- Consequence: DB columns for xG-class fields will stay NULL for 15/16–19/20 events. This is upstream reality, not a pipeline bug. Analytics must filter by season.
- Cups (FA Cup, CdR, CdF, DFB Pokal, Asian cups) for oldest seasons were **not** spot-checked (15 samples covered top-5 leagues only). Mitigation is built in: skip-on-404, no_data ≠ failure (Stage-3 policy). Expect smaller cups to regress toward 4d-2's "cards-only/no-stats" profile the further back we go.

## 3. Event-count estimates per season

Basis: Stage 4c 25/26 measured **4,799 events over 20 comps**; historical variation driven by (a) Ligue 1 shrank 20→18 teams in 23/24, (b) cup round counts drift, (c) COVID-shortened 19/20 in some comps, (d) UECL did not exist before 21/22, (e) AFC formats changed 2021 & 2024.

| Season | Est. events | Notes |
|---|---|---|
| 15/16 | ~4,150 | No UECL; standard calendars |
| 16/17 | ~4,150 | — |
| 17/18 | ~4,150 | — |
| 18/19 | ~4,150 | — |
| 19/20 | ~3,900 | COVID truncation (CSL restructured, etc.) |
| 20/21 | ~4,250 | compressed but mostly complete |
| 21/22 | ~4,600 | UECL begins (+~140 events) |
| 22/23 | ~4,600 | — |
| 23/24 | ~4,550 | Ligue 1 at 306 league matches from here on |
| **Total** | **≈ 38,450 events** | |

## 4. Machine-hours

Throughput reference (Stage 4c measured, mixed endpoint weights):
- Batch A 651 events / 2,951 s · B 199 / 242 s · C 3,949 / 4,418 s ⇒ **overall ≈ 37.8 events/min** (≈ 2.27k events/machine-hour)

| Season | Est. events | Est. machine-hours |
|---|---|---|
| each 15/16–23/24 | 3,900–4,600 | **~1.7–2.0 h** |
| **Total (9 seasons)** | ≈ 38,450 | **≈ 17 machine-hours** |

At current safe concurrency (one process per comp-season; ~1.5 req/s/IP over the 33-IP pool), each season is **one evening batch**. Whole 9-season tranche ≈ **9 evenings** of wall-clock, comfortably inside a fortnight with gate days in between.

HTTP-call estimate: ≈ 11 endpoints × ~35,000 in-scope events ≈ **~385k calls** worst case if nothing is skipped; realistic skip-on-404 (cups) trims this to roughly **250–300k** based on 4c hit-rates.

## 5. Execution order — recommendation: **oldest-first (sequential)**

**Recommend chronological oldest→newest**, aligning with Kris's 15/16-first instruction, for these reasons:

1. **Data-availability risk is now verified LOW for the oldest seasons** (this doc §2), so "do the risky ones first" and "do them oldest-first" coincide — no conflict.
2. **Upstream attrition risk only grows with time**: seats/IPs/fingerprint drift are bigger threats than SofaScore deleting 2015 data, but if anything ever does get pruned, oldest data goes first. Completing 15/16 first locks in the scarcest asset.
3. **Schema/semantics drift is monotonic**: older seasons are simpler (no xG, no momentum graph in earliest years — momentum endpoint exists but likely no data pre-2019); starting simple lets the pipeline warm up before hitting 21/22+ where UECL + momentum + richer payloads add volume.
4. **Reporting/QA continuity**: season-by-season closure statements read naturally 15/16→23/24 and interleave cleanly with the already-done 25/26.

(If Kris prioritizes recency value instead, newest-first is equally valid mechanically — runner behavior is identical. My recommendation stands with oldest-first per his instruction.)

## 6. Gate / runbook impact (vs docs/gen4_stage4d4_deploy_runbook.md)

| Gate | Change needed? |
|---|---|
| P1 branch/commits check | Same |
| P2 4d-1 tests | Same (still 14/14 gate) |
| P3 pool freshness (<24h) | **Same, but mandatory before EVERY season batch**, not just program start (9 batches = 9 checks) |
| P4 MySQL reachability | Same |
| P5 secret scan | Same |
| P6 dispatcher smoke | Same |
| P7 fp-v2 20-call canary | **Same, per season batch** (cheap: 20 calls) |
| **NEW P8** | **Per-season event-flag semantics probe**: before each season batch, fetch one sample event's `statistics`/`shotmap` and confirm 200 (not one per comp — one per season, 3 calls). Protects against season-specific upstream removal. |
| **NEW P9** | **COVID/format anomaly note review** for 19/20 & 20/21: skip-policy already correct (404 = no_data) — just flag in the season report that completion ≠ uniform rounds. |

Rollback unchanged: `--engine gen2` per competition batch; no schema work.

## 7. Suggested tranche schedule (for Kris to approve)

| Tranche | Seasons | When | Output |
|---|---|---|---|
| T1 | 15/16 + 16/17 | Evening 1 + 2 | tranche report + per-season evidence JSONL |
| T2 | 17/18 + 18/19 | Evening 3 + 4 | same |
| T3 | 19/20 + 20/21 | Evening 5 + 6 (COVID annotations) | same |
| T4 | 21/22 + 22/23 | Evening 7 + 8 (UECL added) | same |
| T5 | 23/24 | Evening 9 | final; then full-history QA sweep |

Checkpoint at end of each evening: tranche summary JSON + `sessions_send` Main. Any gate failure → halt the tranche, report, no silent continue.

## 8. Open items for Kris

1. Green light per tranche (T1 first) — or blanket approval for T1–T5 with per-evening checkpoint reports.
2. Confirm endpoint set stays at 11 for all seasons (recommend yes — verified availability makes exclusions unnecessary).
3. Confirm acceptable run window (Asia/Taipei evenings) so pool/canary gates align with traffic patterns.
