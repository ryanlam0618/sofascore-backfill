# Stage 4c Plan v0.1 — Complete 25/26 Season Backfill (Option D)

**Author**: Forge | **Date**: 2026-09-07 05:37 GMT+8 (Kris approval)
**Status**: PRE-FLIGHT COMPLETE → ready to execute

## Schema Pre-flight Results (2026-09-07 ~06:45 GMT+8)

### 1. match_odds.market_id schema bug — FIXED ✅
- **Problem**: `market_id`, `market_name`, `market_group`, `market_period`, `structure_type`, `suspended`, `choice_name`, `initial_fractional_value`, `fractional_value`, `winning` columns were MISSING from `match_odds`.
- **Fix applied**: `ALTER TABLE match_odds ADD COLUMN ...` (10 columns added). Table was empty (0 rows), safe migration.
- **Verify**: 19 columns now present; backfill_runner.insert_odds compatible.

### 2. match_player_stats write path — CONFIRMED ✅
- 92 columns present (Hermes-rebuilt schema with legacy stat columns).
- Write path via `backfill_runner.DataInserter` compatible.
- NOTE: player stats are embedded in `/event/{id}/lineups` (home/away arrays), NOT a standalone `player-statistics` endpoint.

### 3. Endpoint Spot-check (3 sample events, IP 82.21.248.191)

| Endpoint | URL | Result |
|----------|-----|--------|
| h2h | `/event/{id}/h2h` | ✅ 200 (~102B: teamDuel, managerDuel) |
| votes | `/event/{id}/votes` | ✅ 200 (~220B) |
| graph | `/event/{id}/graph` | ✅ 200 (~2.4KB) |
| odds | `/event/{id}/odds/1/all` | ✅ 200 (~8.5KB: markets, eventId) |
| odds (wrong) | `/event/{id}/odds` | ❌ 404 |
| average-positions | `/event/{id}/average-positions` | ✅ 200 (~40KB) |
| best-players | `/event/{id}/best-players` | ✅ 200 (~20KB) |
| player-statistics (wrong) | `/event/{id}/player-statistics` | ❌ 404 |
| player stats (correct) | `/event/{id}/lineups` | ✅ 200 (~90KB: home, away, statisticalVersion) |

**Key finding**: Odds endpoint uses `/event/{id}/odds/1/all` suffix. Player stats are embedded in lineups endpoint.

## Execution Plan

### Batch A — Low-coverage comps (5 worst + 10 small gaps)
- **Comps**: 335 (200), 17015 (409), 19 (128), 329 (137), 882 (75) + 10 others
- **Endpoints**: lineups, statistics, incidents, shotmap (existing 4 endpoints)
- **Est calls**: ~800 events × 4 endpoints = ~3200
- **Est time**: ~1.5h

### Batch B — 9 zero comps shotmap
- **Comps**: 7, 8, 17, 23, 35, 101, 196, 323, 1786 (1481 events)
- **Endpoints**: shotmap only (1 endpoint per event)
- **Est calls**: ~1500
- **Est time**: ~1.5h

### Batch C — 7 new endpoints
- **Comps**: all 25/26 finished matches (~3987 events)
- **Endpoints**: h2h, votes, graph, odds (with /1/all), average-positions, best-players, player-statistics (from lineups)
- **Est calls**: ~28K
- **Est time**: ~2-3h

## Technical Config (all batches)
- Pool: `good_20260907.txt` (33 IPs, chrome124 verified)
- Method: curl_cffi impersonate="chrome124"
- Concurrency: 8, Pacing: 0.15s, Retries: 3, Cooldown: 30s
- Early stop: 5 consecutive 403s
- Budget: ~33K calls (under 60K ceiling)

## Deliverables
1. `data/gen4_stage4c_batch_A_low_coverage_comps_report.json` + `_evidence.jsonl`
2. `data/gen4_stage4c_batch_B_zero_comps_shotmap_report.json` + `_evidence.jsonl`
3. `data/gen4_stage4c_batch_C_new_endpoints_report.json` + `_evidence.jsonl`
4. `subagent_results/20260907-XXXXXX-coding-stage4c-{A,B,C}.json`
5. `data/gen4_stage4c_summary.json`