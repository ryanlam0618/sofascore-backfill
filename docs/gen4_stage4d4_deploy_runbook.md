# Stage 4d-4 Deploy Runbook — Gen4 fp-v2 as Production Default

**Effective**: 2026-09-08 19:48 GMT+8 (Kris partial GO)
**Owner**: Forge (`agent:coding`) · **Approver**: Kris via Main Agent
**Supersedes**: Gen2 hybrid as default (Gen2 remains available as rollback)
**Scope guard**: 24/25 (and older) season tranches are **NOT** authorized by this runbook. They need the next green light.

---

## 0. What changed, in one paragraph

`run_backfill.py` is now the production entry point. Default engine = **gen4** (pinned `curl_cffi impersonate=chrome124` + audited fixed-IP pool + evidence-JSONL discipline). Legacy Gen2 (`backfill_runner.py` direct invocation) is still intact and reachable in one flag. The Sentinel Stage 4d-1 patch (nested-dict coercion + InnoDB deadlock retry) is already committed, so the 206 Stage-4c residual errors are recoverable on next run.

Commits landed:
- `d1fe1cb` — `fix(stage4d-1)` (backfill_runner.py, gen4_stage4c_batch_C.py, tests/test_gen4_stage4d_deadlock_retry.py)
- `9224af8` — `feat(stage4d-3)` (run_backfill.py dispatcher + README)

---

## 1. Preconditions (verify before ANY production run)

| # | Check | Command | Pass |
|---|---|---|---|
| P1 | On `v2` branch with 4d-3 commits | `git log --oneline -2` shows 9224af8 + d1fe1cb | ✅ |
| P2 | 4d-1 tests green | `.runner-venv/bin/python -m pytest tests/test_gen4_stage4d_insert_player_stats.py tests/test_gen4_stage4d_deadlock_retry.py -q` → 14 passed | ✅ (verified 2026-09-08) |
| P3 | Proxy pool freshness | pool file `data/proxy_pools/good_20260907.txt` is < 24h old at run start; else re-audit via Sentinel flow | gate |
| P4 | MySQL reachable | `mysql appdb -e "select 1"` from runner host | gate |
| P5 | No `MYSQL_PASSWORD` / tokens in repo | `git grep -inE "password\\s*=\\s*['\\\"]" -- '*.py' ':!*test*'` returns only getenv references | gate |
| P6 | Dispatcher smoke | `.runner-venv/bin/python run_backfill.py --engine gen2 --limit-rounds 0 --dry-run` prints the engine banner + exits 0 | gate |
| P7 | fp-v2 canary (20-call fast path) rerun same-day | per `docs/gen4_fingerprint_v2_plan.md` PASS_STRICT | gate |

If any gate fails: STOP, do not run; escalate to Main Agent.

## 2. How to run production backfill now

| Scenario | Command |
|---|---|
| Default (Gen4, 25/26 remaining endpoints/events) | `.runner-venv/bin/python run_backfill.py --resume` |
| Explicit Gen4 | `.runner-venv/bin/python run_backfill.py --engine gen4` |
| Gen2 rollback for a single competition | `.runner-venv/bin/python run_backfill.py --engine gen2 --competition "Premier League" --season-id 76986` |
| Gen2 via env for a whole session | `BACKFILL_ENGINE=gen2 .runner-venv/bin/python run_backfill.py …` |
| Stage-4c replay of the 206 residuals (lineups 169, odds/avg-pos/momentum 37) | re-run the same Stage 4c batch scripts; patched code retries deadlocks and coerces nested dicts automatically |

Rules that still apply (from Stage 3/4c proven discipline):
- One process per (competition × season) — progress files are not locked.
- Per-IP sustained throughput ≈ 1.5 req/s; do not raise without a fresh canary.
- HTTP 404 → `no_data`, not retried (Stage-3 skip policy + 4d-2 confirmation).
- All runs emit evidence JSONL + per-IP counters + before/after row counts.

## 3. Rollback (verbatim from deployment report §4)

| Trigger | Action | RTO |
|---|---|---|
| fp-v2 canary fails (PASS < 100% on 20-call fast path) | Do not run; default stays but invocation blocked until canary passes again | 0 min |
| In-flight 4xx rate > 2% over a 100-event window, and reproduces in canary | Re-run affected events with `--engine gen2` | ~5 min |
| Deadlock-rate climbs above retry budget (>1% of calls) | Pause; Sentinel triage; run affected endpoints on gen2 meanwhile | hours |
| Pool degrades (≥ 5/33 IPs fail re-audit) | Regenerate pool (24h SLA); gen2 rotate gateway meanwhile | hours–day |

No schema migration exists anywhere in Gen4 — rollback is an entry-point decision only.

## 4. Monitoring checklist during a run

1. `tail -f logs/backfill/*.log` — watch for consecutive infra-fail counters (15/12/10 tier thresholds from Stage 3 policy).
2. Evidence JSONL: `grep -c '"ip_banned": true' <evidence>.jsonl` — should be 0 per event; any spike = pool issue.
3. Global abort tripwire: 30 consecutive infra fails (Stage-3 rule) — script stops by itself; do not relaunch until root-caused.
4. Post-run: row-count delta in `match_*` tables matches the batch report's `events_processed` × expected endpoints (Stage 4c summary format).

## 5. What is explicitly NOT covered here

- ❌ 24/25 (or older) backfill launch — pending Kris's separate green light.
- ❌ Changes to `competitions_10y.yaml`, proxy credentials, or MySQL schema.
- ❌ Any force-push, history rewrite, or CI/deploy trigger.
- ❌ Direct Telegram to Kris — all coordination via Main Agent.

## 6. Sign-off trail

| When | Who | What |
|---|---|---|
| 2026-09-07 20:21 | Main Agent audit | Stage 4d-2 locked (gap = server-side) |
| 2026-09-08 16:00 | Forge | Stage 4d-2 re-run reproduced locked verdict |
| 2026-09-08 19:02 | Kris via Main | Stage 4d-3 report-only commissioned |
| 2026-09-08 19:48 | Kris via Main | Partial GO: commit 4d-1, flip default, write runbook |
| 2026-09-08 (this doc) | Forge | Commits `d1fe1cb` + `9224af8` on `v2` |

Next required signature: **Kris green light for 24/25 tranche**.
