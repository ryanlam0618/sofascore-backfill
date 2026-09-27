# Gen4 Stage 4a Dry Probe Plan — Shotmap 0% Root Cause Classification

**Status**: v0.3 — **RESUME-IMMINENT: TLS fingerprint fix identified** (Forge authored, awaiting Main Agent audit)  
**Locked facts per MEMORY**: pool = `good_20260906.txt` (61 IPs), no production default change, no schema mutation

## ✅ ROOT CAUSE RESOLVED (2026-09-07 01:55 GMT+8 — Main Agent verified)

**Previous BLOCKER WAS WRONG**: 403s were NOT due to IP reputation death or credential expiry.

**Actual root cause**: TLS fingerprint mismatch.
- Plain curl / curl_cffi with `impersonate="chrome"` (generic) → **403 on ALL IPs**
- Stage 3 method: curl_cffi with `impersonate="chrome124"` → **2/5 IPs 200 OK immediately**
- Varnish uses IP-reputation + TLS fingerprint DUAL check (Sep 1 verdict confirmed)
- `good_20260906.txt` pool is VALID — just needs correct TLS fingerprint

**Stage 4a can resume IMMEDIATELY** — no pool refresh needed, no Sentinel re-audit needed.

## 📋 Implementation Fix (applied to plan v0.3)
- HTTP client: curl_cffi with `impersonate="chrome124"` (NOT generic chrome)
- Reference pattern: `gen4_stage3_expansion_backfill.py` lines 95-100 (`_http_get_cffi`, `fetch_json`)
- Reuse `next_ip()` rotation + retry logic from Stage 3
- DO NOT import `gen4_fetcher.py` (uses generic chrome fingerprint — fails)

---

## 1. Scope

**16 competitions with 0% shotmap in 25/26 season** (as of 2026-09-07 audit):

| comp_id | name | matches_25_26 | shotmap | pct |
|---------|------|---------------|---------|-----|
| 34      | Ligue 1 | 311 | 0 | 0.0% |
| 679     | UEL | 271 | 0 | 0.0% |
| 17015   | UECL | 409 | 0 | 0.0% |
| 136     | A-League Men | 257 | 95 | 37.0% |
| 668     | AFC CL Two | 130 | 0 | 0.0% |
| 649     | Chinese Super League | 202 | 0 | 0.0% |
| 463     | AFC Champions League | 117 | 0 | 0.0% |
| 410     | K League 1 | 158 | 0 | 0.0% |
| 217     | DFB Pokal | 63 | 0 | 0.0% |
| 21      | EFL Cup | 94 | 0 | 0.0% |
| 882     | Chinese FA Cup | 75 | 0 | 0.0% |
| 329     | Copa del Rey | 137 | 0 | 0.0% |
| 19      | FA Cup | 128 | 0 | 0.0% |
| 335     | Coupe de France | 200 | 0 | 0.0% |
| 328     | Coppa Italia | 45 | 0 | 0.0% |

**Note**: J1 League (196) is at 98.6% — excluded from scope.

---

## 2. Probe Design

### 2.1 Sample Selection
- Per comp: 3 events with `shotmap=0` in 25/26 (already finished events from `matches` table)
- Total events: 16 comps × 3 = 48 events
- If comp has <3 events, use all available

### 2.2 Endpoint & Tier Strategy
- **Single endpoint**: `/api/v1/event/{event_id}/shotmap`
- **Tier-1**: curl_cffi (chrome124 impersonate) — first attempt
- **Tier-2**: CloakBrowser (Playwright) — fallback on 403
- **Tier-3**: SSR (Server-Side Rendering) — NOT used for this probe (per Gen4 spec, reserved for deeper investigation)

### 2.3 IP Pool & Assignment
- Pool file: `data/proxy_pools/good_20260906.txt` (61 IPs, mixed cred)
- Per-event deterministic assignment: `sha256(event_id) % len(pool)` → starting IP
- On 403: rotate to next IP in pool (round-robin, max 4 additional IPs for Tier-1)
- Tier-2 uses same pool, fresh browser context per call

### 2.4 Call Budget
- Per event: **max 5 sequential calls**
  1. Tier-1 attempt 1 (starting IP)
  2. Tier-1 attempt 2 (next IP on 403)
  3. Tier-1 attempt 3 (next IP on 403)
  4. Tier-1 attempt 4 (next IP on 403)
  5. Tier-2 attempt (CloakBrowser, fresh context, next IP)

- Total max calls: 48 × 5 = 240 calls (well under budget)

### 2.5 Timing
- Pacing: 0.5s between calls (respectful to SofaScore)
- Timeout: Tier-1 = 20s, Tier-2 = 30s

---

## 3. Verdict Classification (per event)

| Verdict | Definition | Indicates |
|---------|------------|-----------|
| `one_take_200` | Tier-1 attempt 1 → HTTP 200 + shotmap array | Normal; endpoint works |
| `transport_then_200` | Tier-1 403(s) → later Tier-1 attempt 200 + shotmap | IP reputation fixable via rotation |
| `transport_403` | All 5 calls (4 Tier-1 + 1 Tier-2) → 403 | Varnish IP-reputation hard block |
| `endpoint_empty` | Any call → HTTP 200 + `shotmap: []` empty array | SofaScore has no shotmap data |
| `endpoint_404` | Any call → HTTP 404 | Endpoint deprecated / event too old |
| `tier2_200` | Tier-1 all 403 → Tier-2 200 + shotmap | CloakBrowser bypasses Varnish |
| `tier2_empty` | Tier-1 all 403 → Tier-2 200 + empty shotmap | Data unavailable, not transport |
| `tier2_404` | Tier-1 all 403 → Tier-2 404 | Confirmed no shotmap |
| `error_other` | Timeout, 5xx, connection error, etc. | Infra issue, needs retry |

**Classification logic**: First non-error response determines verdict (priority: 200 with data > 200 empty > 404 > 403).

---

## 4. Gates (HARD PASS — must all pass)

| Gate | Check | Fail Action |
|------|-------|-------------|
| P1 | Probe completes all 48 events | Halt; fix script |
| P2 | Per-comp verdict unambiguous (3/3 same, or 2/3 clear majority) | Report mixed; recommend deeper probe |
| P3 | No event classified as `error_other` without retry | Halt; fix infra |
| P4 | No Varnish challenge hit during probe (good_20260906 should bypass) | Halt; pool issue |
| P5 | No rate-limit / IP ban hit (3 events/IP well under limit) | Halt; adjust pacing |
| P6 | Stop-on-consecutive-fail: 5 consecutive event-level failures → abort | Halt; investigate |
| P7 | Cleanup: no leaked subprocesses, all connections closed | Warn but continue |

---

## 5. Output Artifact

**`data/gen4_stage4a_shotmap_dry_probe.json`** structure:

```json
{
  "timestamp": "2026-09-07T...",
  "probe_version": "v0.1",
  "pool_file": "data/proxy_pools/good_20260906.txt",
  "pool_size": 61,
  "events_probed": 48,
  "events_by_comp": 16,
  "per_event": [
    {
      "comp_id": 34,
      "comp_name": "Ligue 1",
      "event_id": 12345678,
      "match_id": 87654321,
      "tier1_calls": [
        {"attempt": 1, "ip": "108.165.181.160", "http": 403, "latency_ms": 1200},
        {"attempt": 2, "ip": "108.165.181.65", "http": 200, "latency_ms": 980, "shot_count": 34}
      ],
      "tier2_call": null,
      "verdict": "transport_then_200",
      "shot_count": 34
    }
  ],
  "per_comp_verdict": {
    "34": {"name": "Ligue 1", "transport_blocked": 2, "data_unavailable": 1, "verdict": "mixed"}
  },
  "summary": {
    "transport_blocked_pct": 45.8,
    "data_unavailable_pct": 54.2,
    "recommendation": "targeted_replay"
  }
}
```

---

## 6. Next Steps (Decision Brief)

Based on per-comp verdict distribution:

| Scenario | Action |
|----------|--------|
| **transport_blocked ≥ 60%** | Stage 4a-2: targeted replay with enhanced IP rotation + Tier-2 forced |
| **data_unavailable ≥ 60%** | Accept coverage limit; no shotmap for these comps |
| **Mixed (40-60% each)** | Stage 4a-2: targeted replay ONLY on transport_blocked events |

**Estimated replay cost**: ~1,700 events × 3s avg = ~1.5h wall-clock at concurrency 4.

---

## 7. Forbidden (LOCKED)

- ❌ Production default change
- ❌ MySQL DELETE / schema mutation
- ❌ Varnish challenge hit (good_20260906 should bypass)
- ❌ Full backfill — dry probe + targeted replay only
- ❌ Stale `good_21.txt` reuse

---

## 8. Reference Code (Reuse) — UPDATED v0.3

- `gen4_stage3_expansion_backfill.py` lines 95-100 — **PRIMARY REFERENCE** for HTTP client (chrome124, retry-with-rotation)
- `gen4_shotmap_replay.py` lines 65-75 — backup reference (same pattern)
- `fixed_pool_rotator.py` — Deterministic IP assignment, rotate()
- `gen4_shotmap_rerun.py` — Stage 2 rerun pattern (random + cooldown)
- ❌ `gen4_fetcher.py` — DO NOT USE (generic chrome fingerprint fails 403)