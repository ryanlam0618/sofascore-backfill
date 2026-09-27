# Gen4 Canary Plan — v0.2 (GOOD-IP pool generalization canary)

> **Supersedes v0.1** (fingerprint-family comparison). v0.1 root assumption invalidated 2026-08-30 21:40 GMT+8: Sentinel control recon proved fingerprint family is NOT gated on GOOD IPs (F1==F4 both 200; server=nginx bypassed Varnish on the good pool). Kris 23:29 GMT+8 chose **Option B: pivot, don't drop**.
>
> **Status**: v0.2 awaiting Kris P1 approval. **Execution requires Kris explicit approval.**

- **Author**: Forge (coding agent)
- **Date**: 2026-08-30 ~23:4x GMT+8

---

## 1. Context Change (v0.1 → v0.2)

| | v0.1 | v0.2 |
|---|---|---|
| Hypothesis | fingerprint family is the divergent variable | GOOD-IP pool generalizes across historical events/endpoints |
| Variable under test | impersonate family (chrome120 / safari17_2_ios / firefox133) | none — single fixed baseline `chrome124` |
| Control | same-IP across fingerprints | same-fingerprint across events **and** the 22-IP pool (hash-assigned) |
| Budget | 80 (≤100 cap) | **20 (≤20 cap, hard)** |
| Early-stop | 40 consecutive 403s | **5 consecutive 403s** |

## 2. Scope

- **Pool**: 22 GOOD IPs — exactly `passed_ips` from `data/proxy_x_chrome124_full_canary.json` (== Sentinel good list: 21 GOOD + ex-MIXED 82.27.245.245).
- **Transport**: Tier-1 curl_cffi only, `impersonate="chrome124"` (fixed baseline — no fingerprint variants).
- **Matrix**: 5 events × 4 endpoints × 1 fingerprint × 1 hash-assigned IP = **20 calls**.

## 3. Events (selection standard)

Per Phase-10 plan §6 alphabetical fallback — the five COMP-BROKEN historical events from the 8/29 spot-check artifact (`data/proxy_audit/spot_check_19_20_20260828_031809.json`), alphabetically by competition, A-League Men first:

| # | Competition | Event ID | Prior verdict |
|---|---|---|---|
| 1 | A-League Men | 8351333 | COMP-BROKEN |
| 2 | La Liga | 8280661 | COMP-BROKEN |
| 3 | Ligue 1 | 8245839 | COMP-BROKEN |
| 4 | Serie A | 8337618 | COMP-BROKEN |
| 5 | UCL | 8390922 | COMP-BROKEN |

Rationale: these are the exact rows where the old stack failed. If the hashed-GOOD-IP assignment now passes them, the pool's historical-coverage generalization is proven end-to-end. (PL 8243388 was COMP-PARTIAL — excluded as ambiguous.)

## 4. Endpoints (4 per event)

`/event/{id}`, `/event/{id}/lineups`, `/event/{id}/statistics`, `/event/{id}/incidents`

## 5. IP Assignment Policy (deterministic, reproducible)

`ip_index = sha256(str(event_id)) mod 22` — same convention as `fixed_pool_rotator.py` (pool loaded from `good_proxies_20260827_192348.txt` GOOD rows; ex-MIXED included as 22nd).

- All 4 endpoints of one event go through that event's assigned IP → per-event isolation, reproducible, no collision risk across events unless hash coincides (acceptable; pool usage stats still recorded).

## 6. Budget & Abort

- Hard cap **20 calls**, `write_enabled=False`, timeout 20s, concurrency 1 (sequential — cheap and audit-friendly; 20 × ~1s ≈ 30s total).
- **Early-stop: 5 consecutive 403s** → abort, report partial matrix. (Down from v0.1's 40 — sample is small by design.)
- No retries: 1 attempt per cell. Non-200 recorded, never halts (except the early-stop rule).

## 7. Pass Threshold (clarified per Main audit 2026-08-30 23:5x)

Three layers, no contradiction:

1. **Per-cell goal: STRICT 100%** — every cell either 200 or fails; non-200s are recorded, never retried, never ignored.
2. **Overall gate: ≥ 80% (16/20)** — the pass/fail line for the canary as a whole. Perfect score (20/20) is the expectation; 16–19/20 = report per-failing-endpoint breakdown; < 16 = fail.
3. **Operational abort: 5 consecutive 403s** — pure safety valve. If it trips, the run stops early and the ≥80% decision is made on executed cells (denominator = calls executed). It does not redefine the pass bar.

So: STRICT tracking + ≥80% gate + 5-in-a-row abort. All three coexist; they answer different questions (data truth / gate / resource safety).

## 8. Invariants (carry-over from v0.1)

- `FETCH_STRATEGY=gen4_fingerprint_v2` opt-in; **default Gen2 path UNCHANGED**; no Gen2 edits.
- Protected files mtime asserted before/after: `gen4_fetcher.py`, `fixed_pool_rotator.py`, `backfill_runner.py`.
- Credentials runtime-read from good_proxies list only; never logged.
- Evidence → `/tmp/gen4_fp_v2_canary/evidence.jsonl`; summary → `data/gen4_fp_v2_canary.json`.
- Fingerprint dry-verify (carry-over): `chrome124` ∈ `BrowserTypeLiteral` ✅ (already verified 2026-08-30).

## 9. Carry-over: Phase-7 Q1–Q7 Decisions

Still applicable and unchanged: deterministic reproducibility over entropy, per-event hash assignment, no silent retries, budget-as-hard-cap, evidence-first artifacts, no creds in outputs, protected-file hygiene.

## 10. Gates

- **P1**: Kris approves v0.2 → write `gen4_fp_v2_canary.py`.
- **P2**: Main code review (invariants, hash policy, early-stop).
- **P3**: `--dry-run` matrix assembly (0 calls) verified.
- **P4**: 2-call smoke (event 1, endpoints 1–2) → mechanical check.
- **P5**: Full 20-call run; results audit (JSON parse, counts, no creds).
- **P6**: Main review → Kris report.
- **P7**: Kris decision on next step (e.g. widen to full pool × more events, or advance Phase 11 gate discussion).

## 11. Risks (revised)

1. **Sample size is small** (20 calls) — by design per Option B "cheap" directive; precision is limited to coarse pass/fail signal.
2. **Pool reputation drift** — list last refreshed 8/28; a stale GOOD IP flipping to 403 is possible mid-run. Early-stop + per-IP failure logging covers.
3. **v0.2 does NOT test fingerprint as variable** — if any cell 403s under chrome124-on-GOOD-IP, next diagnostic step is unclear until results seen; abort and escalate rather than ad-hoc retrying variants.

# Gen4 Canary Plan — v0.3 (Path 1: full 22-IP pool generalization)

> **v0.3 addendum** (Kris 2026-08-31 00:06 GMT+8, Path 1 approved): widen P5 into a 440-call pool-generalization test. v0.2-rev1 base remains in force except where this section overrides.
> **Status**: v0.3 awaiting Main audit / Kris implicit OK. Execution gated.

## A. Path 1 Scope

- Pool: ALL 22 GOOD IPs (no hash subsetting).
- Matrix: 22 IPs × 5 events × 4 endpoints × chrome124 = **440 calls hard cap**.
- Sequential (concurrency 1), ~1s/call → est. 5–8 min. No retries.
- Early-stop: 5 consecutive 403 **or** 5 consecutive conn/timeout errors → abort + escalate.
- Events + endpoints: same as v0.2 (§3–§4). Fingerprint: chrome124 only.

## B. Assignment Policy — **Event-major** chosen (overrides v0.2 §5 hash rule)

Order: `for event in EVENTS: for ip in POOL: for endpoint in ENDPOINTS:`.

Justification vs IP-major:
1. **Abort-robustness**: if one IP's reputation drifted (5 403s), IP-major wastes the early-stop on that single IP and leaves most of the pool untested; event-major interleaves IPs so a bad IP produces isolated failures, not a false abort — and if a genuine systemic 403 wave starts back-to-back, abort still triggers correctly.
2. **Event-integrity**: each event completes its 88-call block before moving on → per-event verdicts are never half-built; per-IP stats are still exactly computable post-hoc from evidence rows.
3. Deterministic and reproducible; no randomness, no hash collision issue.

## C. Gates (Path 1)

- P1': plan v0.3 (this addendum) — Main audit + Kris implicit OK
- P2': extend script (`gen4_fp_v2_wide_canary.py`)
- P3': dry-run, matrix = 440, 0 network calls
- P4': 4-call smoke (IP #0 × event #1 × 4 endpoints)
- P5': full 440 with per-IP + per-endpoint + per-event breakdown
- P6': report; Kris Gen4 production rollout decision (NOT auto-promoted)

## D. Pass semantics (Kris explicit)

- ≥80% overall (352/440) = PASS
- 80–90% = borderline → breakdown + escalate
- <80% = FAIL → identify failing IPs/segments, escalate

## E. Invariants — unchanged (v0.2 §6, §8, §9 all carry over)

Protected mtimes, write_enabled=False, Gen2 untouched, creds runtime-only, no ad-hoc retries.

---

## 12. Deliverables of THIS revision (now)

- [x] Plan v0.3 (top of this file)
- [x] Handoff to Main with assignment choice + justification
- [ ] Main audit → Kris implicit OK → P2'

