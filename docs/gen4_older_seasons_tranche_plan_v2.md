# Gen4 Older-Seasons Tranche Plan v2 — 15/16 First (10 Seasons)

**Author**: Forge (`agent:coding`) · **Requested**: Kris 22:06 2026-09-08 via Main Agent (expanded dispatch)
**Status**: 📄 REPORT ONLY — nothing launched; 24/25 included but also gated on approval
**Evidence**: `data/gen4_tranche_v2_availability_probe.json` (23 comps × 11 endpoints, live PinchTab)
**Supersedes**: tranche plan v1 (in `gen4_deployment_report_v1.md` §6) and the 22:0x interim v2

---

## 1. Executive summary

- **Data availability is far better than feared**: the big-5 leagues have **all 11 endpoints live as far back as 15/16** (55/55 endpoint probes = 200). CSL and UCL too. 
- **There is no season-level "cliff"** — cup/secondary-league gaps are **event-granular** (same cup = 200 in one season's sample, 404 on another event elsewhere). The correct mechanism is the existing skip-on-404 policy, not season exclusion.
- **Only true structural exclusion: UECL before 21/22** (competition did not exist; API returns earliest season = 21/22).
- **Pre-2020 caveat**: statistics payloads have possession/passes but **no xG**; shotmaps have coordinates but **no xG/xGOT fields**. Expected: xG-class columns stay NULL for 15/16–19/20. Upstream reality, not a bug.
- **Scope**: 10 seasons (15/16 → 24/25) × 24 competitions (UECL from 21/22 onward) × 11 endpoints ≈ **~40k events ≈ ~18 machine-hours**, i.e. about **10 evening batches** at Stage-4c measured throughput.

## 2. Availability matrix (15/16 sample, one finished event per competition)

Legend: ✅ 200 · ❌ 404 · — N/A. Order: base, incidents, lineups, statistics, shotmap, odds, h2h, votes, avg-pos, best-players, graph.

| Competition | base | inc | lineups | stats | shot | odds | h2h | votes | avgpos | bestpl | graph |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Premier League | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| La Liga | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Serie A | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Bundesliga | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Ligue 1 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| J1 League | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ |
| K League 1 | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ |
| A-League Men | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ |
| Chinese Super League | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| UCL | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| UEL | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ |
| UECL | — (did not exist pre-21/22) | | | | | | | | | | |
| AFC Champions League | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ |
| AFC Champions League Two | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ |
| FA Cup | ✅ | ✅ | ❌* | ❌* | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ |
| EFL Cup | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ | ❌ | ✅ | ❌ |
| Copa del Rey | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ |
| Coppa Italia | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ |
| Coupe de France | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ |
| DFB Pokal | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ | ❌ | ✅ | ❌ |
| J.League Cup | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ |
| Emperor's Cup | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ |
| Australia Cup | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ |
| Chinese FA Cup | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ |

\* FA Cup sampled event (Bolton v Leeds, Round 3 replay) was 404 — but cliff check (below) showed FA Cup **17/18 and 21/22 samples ARE 200** → event-granular, not season-wide.

**Universal across all 23 comps**: base / incidents / odds / h2h / votes live everywhere. **average_positions is the weakest endpoint** (200 only on big-5 + CSL + UCL).

### Cliff check (FA Cup, Copa del Rey)

| Comp | 17/18 | 19/20 | 21/22 | Verdict |
|---|---|---|---|---|
| FA Cup (stats+shotmap) | 200 | 404 (that event) | 200 | event-granular — no season cliff |
| Copa del Rey | 200 | 200 | 200 | covered |

**Interpretation for the dispatch question "find the cliff"**: there isn't one at season granularity. Availability is per-event (round prominence, data-provider coverage), and skip-on-404 already handles it correctly. Planning-wise, expect cups to deliver 40–80% endpoint coverage, worse in lower rounds, with no action needed from us.

## 3. Event-count projection per season

Live API counts were infeasible (MySQL unreachable from this container; full pagination would be ~500+ probe calls). Counts below are **structural estimates from competition formats** (league sizes are standard; cups from bracket sizes at the era's format). The runner's `--ids-only` step will produce exact counts before each phase starts.

| Season | Big-5 leagues | J1/K1/ALeague/CSL | UCL+UEL+(UECL) | AFC ×2 | Domestic ×10 | **Total** |
|---|---|---|---|---|---|---|
| 15/16 | 380×4+306 = 1,826 | ~305+230+145+240 = 920 | 125+205 = 330 | 120+90 = 210 | ~840 | **≈ 4,130** |
| 16/17 | 1,826 | 920 | 330 | 210 | 840 | **≈ 4,130** |
| 17/18 | 1,826 | 920 | 330 | 210 | 840 | **≈ 4,130** |
| 18/19 | 1,826 | 920 | 330 | 210 | 840 | **≈ 4,130** |
| 19/20 | ~1,826 (mostly completed) | ~850 (COVID) | 330 | 210 | ~800 | **≈ 4,020** |
| 20/21 | 1,826 | 920 | 330 | 210 | 840 | **≈ 4,130** |
| 21/22 | 1,826 | 920 | 515 (+UECL ~185) | 210 | 840 | **≈ 4,315** |
| 22/23 | 1,826 | 920 | 515 | 210 | 840 | **≈ 4,315** |
| 23/24 | 380×3+306+306 = 1,752 | 920 | 515 | 210 | 840 | **≈ 4,240** |
| 24/25 | 1,752 | 920 | 515 | 210 | 840 | **≈ 4,240** |
| **Total** | | | | | | **≈ 41,500 events** |

Note: 24/25 was substantially covered under Gen2's 10y backfill (matches + core endpoints); the Gen4 pass fills the new-endpoint set (h2h/votes/odds/momentum/avg-pos/best-players) + re-verifies. With `--resume`, effective new work there is smaller than the raw count.

## 4. Machine-hour budget

Measured Stage-4c throughput: **37.8 events/min mixed-endpoint** (4,799 events in 7,611 s, 2026-09-07).

| Phase | Seasons | Est. events | Est. machine-hours |
|---|---|---|---|
| A | 15/16 + 16/17 | ~8,260 | ~3.6 h |
| B | 17/18 + 18/19 | ~8,260 | ~3.6 h |
| C | 19/20 + 20/21 | ~8,150 | ~3.6 h |
| D | 21/22 + 22/23 | ~8,630 | ~3.8 h |
| E | 23/24 + 24/25 | ~8,480 | ~3.7 h |
| **Total** | 10 seasons | **≈ 41,500** | **≈ 18.3 machine-hours ≈ 10 evenings of ~2h** |

HTTP-call budget worst case ≈ 41,500 × 11 = 456k; realistic with skip-on-404 ≈ 320–350k. Pool capacity (33 IPs × 1.5 req/s ≈ 50 rps) is >20× our stage-4c utilization — no constraint.

## 5. Phased rollout & order — recommendation

**Oldest-first, two seasons per phase (A→E above).** Rationale:
1. Kris's stated intent (15/16 開始); probe evidence shows oldest seasons are viable, so intent and feasibility coincide.
2. Scarcity value: oldest data is hardest to replace if upstream availability ever shrinks.
3. Complexity ramp: earliest seasons have no UECL, no xG fields, smaller payloads → gentlest warm-up for the new default engine at scale; by the time we reach UECL-era seasons the loop is battle-tested.
4. 24/25 last is fine because Gen2 already covered it substantially; its Gen4 pass is the cheapest (resume-aware).

(Newest-first would also work mechanically; I've recommended oldest-first per Kris. If Kris reverses it, phases simply run E→A with zero config change.)

## 6. Gate plan per phase

| Gate | When | What |
|---|---|---|
| G-A | each phase start | P3 pool freshness (<24h re-audit) + P7 fp-v2 20-call fast canary (PASS_STRICT) |
| G-B | each phase start | P8 per-season availability probe (new): 1 sample event × statistics/shotmap 200 on 2–3 comps of the phase |
| G-C | each phase end | tranche summary JSON + evidence JSONL persisted + Main Agent sign-off before next phase |
| G-D | any time | kill-switch: global 30 consecutive infra-fails → auto halt (already in runner) |

Relationship to deployment-report G1–G5 (status as of now):
- G1 (4d-1 commit) ✅ done (`d1fe1cb`) · G2 (re-run 206 residuals) ⏳ pending — **runs as part of Phase A's first batch** (natural fit: same runner, idempotent)
- G3 (post-commit fp-v2 canary) ⏳ → G-A covers it per phase
- G4 (pool < 24h) ⏳ → G-A
- G5 (rollback script) ✅ `--engine gen2` flag is live since `9224af8` + verified passthrough

So the deployment gates are not replaced, only *scheduled*: they ride on each phase boundary instead of happening once.

## 7. Risk notes

- **Event-granular cup gaps** → handled by skip-on-404; expect coverage % < big-5 on cups (normal).
- **Pre-2020 xG absence** — analytics must not treat NULL xG as pipeline failure; document in consumer docs.
- **average_positions weak on old seasons for non-big-5** — runners count 404 as no_data cleanly.
- **Single-event sampling caveat** — the 15/16 grid is one event per comp; Stage-3 skip policy was explicitly designed around this variance.
- **UECL iterations** — AFC formats changed in 2024 (AFC CL Elite / Two); `season_id` mapping per `competitions_10y.yaml` notes must be re-verified at phase start (the P8 probe covers functionally).

## 8. Asks for Kris

1. Approve Phase A launch (15/16 + 16/17) — or blanket A→E with per-phase G-C sign-off.
2. Confirm order: oldest-first (recommended) vs newest-first.
3. Confirm run evenings (Asia/Taipei) so G-A canary lands same-day.
