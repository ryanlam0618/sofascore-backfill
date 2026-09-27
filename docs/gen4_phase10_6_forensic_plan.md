# Gen4 Phase 10.6 — Forensic Deep-Dive Plan v0.7

- **Status**: PLAN-ONLY (plan-first workflow; Kris explicit 2026-08-29 19:39 GMT+8). **No execution until Kris approval of this plan.**
- **Author**: Forge (`agent:coding`), task `20260829-194000-coding-gen4-phase10-6-forensic-plan`
- **Requester**: Main Agent (Duncan) on behalf of Kris
- **Date**: 2026-08-29 (Asia/Taipei)

## 0. Decision Context (what this plan must explain)

| Milestone | Result | Source |
|---|---|---|
| Phase 10.6 Broad Canary (plan v0.5, Tier-1 curl_cffi first) | FAIL — `EVENT_CALL_CAP_EXCEEDED` after 32/800 HTTP calls | `data/gen4_phase10_6_broad_canary.json` |
| Phase 10.6 Tier-2 CloakBrowser primary pivot | FAIL — Tier-2 hit-rate 26.3% << 90% target | `data/gen4_phase10_6_broad_canary.json` |
| 100-IP full re-audit vs `/api/v1/event/8351333` | 15/100 PASS (83× FAIL-403, 2× FAIL-Timeout) | `data/proxy_audit/100_ip_full_reaudit_20260828_192327.json` |
| 15 PASS-IP sample verify (run 1) | **0/90 PASS** across 6 events | `data/proxy_audit/15_pass_ip_verify_19_20_20260828_193540.json` |
| 15 PASS-IP sample verify (run 2) | 23/90 PASS (per-IP pass-rate 0–50%, all tier C/D) | `data/proxy_audit/15_pass_ip_verify_19_20_20260828_193925.json` |
| Multi-source audit (6 sources, API vs frontend) | Host-egress = `PASS-BOTH` (API 200 + frontend 200); all 5 Webshare DC proxies = API 403 but **frontend 200** | `data/proxy_audit/multi_source_ip_audit_20260828_183336.json` |
| Host-direct urllib vs 19/20 API events | 6/6 checked = 403 | `data/proxy_audit/host_ip_verification_20260828_171322.json` |
| Phase 4 smoke event 14025013 | PASS (historical baseline) | `data/gen4_phase4_smoke_event_14025013.json` |

**Key contradiction this plan must resolve**: host-egress passed API (200) in the multi-source audit but host-direct urllib (raw `urllib`) failed with 403 on the same event class, and all upstream Tier-1/Tier-2 transports across Webshare rotate blocked. Meanwhile the frontend URL (`/event/8351333`) returned 200 for **every** source, including Webshare DC IPs whose API call 403'd. Conclusion direction (per Main Agent): "host-direct (no Webshare) required for 19/20 events" — but the host-direct urllib result shows **IP alone is not sufficient**; transport signature also matters.

Cross-reference: `MEMORY.md` — Gen4 Phase 10/10.5/10.6 FAIL history, no_event root cause lineage (2026-07-08), and render-bug notes are out of scope here but consistency-checked.

---

## 1. IP-level Forensic

**Objective**: Determine what distinguishes 15 PASS IPs from 85 FAIL IPs in the 100-IP re-audit, and why the PASS verdict didn't hold on re-verify.

**Existing data inventory**:
- `data/proxy_audit/100_ip_full_reaudit_20260828_192327.json` — per-IP `{ip_index (SHA-256 hash prefix, not raw IP), status, latency_ms, body_ok, verdict}`. **No geolocation, ASN, or proxy-detection score fields were captured.**
- `data/proxy_audit/15_pass_ip_verify_19_20_20260828_193540.json` / `..._193925.json` — two verify runs on the 15 PASS IPs, 30 min apart, with divergent results (0/90 vs 23/90 PASS).

**Assertions supported by existing logs** (no new captures needed):
- Two verify runs on the SAME 15 IPs within ~35 min differ (0/90 → 23/90). This is incompatible with a static IP-blocklist explanation and strongly suggests rate-based or session/state-based throttling.
- 83 of 85 FAIL verdicts are `FAIL-403`; 2 are `FAIL-Timeout` — i.e. blocking is active rejection (403), not silent drop. 403 = policy decision by an edge layer.
- The 15 PASS latencies span 1137–3243 ms (heavily skewed right) vs FAIL-403 latencies ~976–1100 ms — consistent with PASS = request survived a bot-check path (slower), FAIL = fast rejection.

**Proposed live probes (post-approval)** — all read-only, no MySQL write:
1. **P1-1 Geo/ASN enrichment (offline)**: re-derive per-IP metadata (ASN, country, type: DC/residential/mobile) from the Webshare account export (list we already hold). Then stratify the 100-IP audit: PASS-rate by ASN/country/type. Method: offline join, zero HTTP calls. Success criteria: contingency table PASS×{ASN,country,type} with at least one statistically significant split (chi-squared p<0.05 or a clean 0-vs-N separation). Rollback: none (offline).
2. **P1-2 Reproducibility probe**: pick 5 PASS IPs + 5 FAIL IPs; hit `/api/v1/event/8351333` 5× per IP with 60 s spacing (total 50 calls). Method: same transport as the re-audit (Tier-1), log status + latency + `server`/`cf-ray` headers. Success criteria: PASS IPs show ≥80% PASS repeatability OR confirm state-dependent flakiness (which refutes "IP-quality" as primary axis). Rollback: stop, no state change.
3. **P1-3 PASS-IP inter-event matrix**: for 2 of the 15 PASS IPs, probe the same 6 events used in the verify runs in randomized order × runs (6 events × 2 runs = 12 calls/IP). Success criteria: PASS-rate variance by *event id* vs by *run index* disambiguates event-specific blocking vs temporal throttling. Rollback: none.

**Budget**: P1-1 offline (~15 min manual), P1-2 50 calls / ~1 h wall-clock, P1-3 24 calls / ~30 min.

---

## 2. Network-level Forensic

**Objective**: Explain why host-egress passed but host-direct-urllib failed, and why Webshare DC IPs pass frontend but fail API.

**Existing evidence**:
- `multi_source_ip_audit_20260828_183336.json`: Webshare × 5 + host-egress tested against `/api/v1/event/8351333` AND `/event/8351333` (frontend). All 5 Webshare: API 403 / frontend 200 (both Tier-1 AND Tier-2). Host-egress: 200/200.
- `host_ip_verification_20260828_171322.json`: host-direct via **urllib** (default Python UA) → 403 on 6/6 19/20 events.
- Phase 10.6 `call_log` — warmup `/` returned 200 but event endpoints 403.

**Interpretation**:
- `host-egress 200` vs `host-direct (urllib) 403` — same host IP, different client → **SofaScore is checking request signature (TLS fingerprint / headers)**, not only IP.
- `Webshare API 403 but frontend 200` for the **same** transport → the API vhost applies stricter policy than the frontend vhost; the frontend HTML is CDN-cached, the API is not. This pattern is consistent with an edge rule that: (a) blocks DC IP ranges on the API route, and (b) challenges non-browser TLS signatures even from "good" IPs.

**Proposed live captures** (post-approval), all read-only:
1. **P2-1 Header fingerprint capture**: for one host-egress call AND one host-direct urllib call AND one Webshare Tier-1 call, capture full request+response headers, TLS ClientHello fingerprint (JA4 if available), and TCP handshake timing. Compare against a known-good browser request (DevTools-exported HAR from a normal Chrome visit by operator; **new** capture, do not reuse old artifacts to avoid staleness). Success criterion: isolate ≥1 deterministic header/TLS difference correlated with 200 vs 403. Rollback: none.
2. **P2-2 curl-impersonate host test**: host-egress via `curl_cffi impersonate="chrome124"` on 3 × 19/20 events (3 calls). If 200, transport fingerprint is sufficient on host; if 403, host IP itself is partially flagged. Rollback: none.
3. **P2-3 Webshare + browser-verify**: Webshare Tier-1 with full browser header set copied from the Chrome HAR (Referer/Origin/Accept-Language/sec-ch-ua etc.) on 3 events (3 calls). Success criterion: 200 → headers matter more than IP class; 403 → IP class matters. Rollback: none.

**Budget**: P2-1 offline+3 calls; P2-2 3 calls; P2-3 3 calls → 9 live calls total, ~30 min.

---

## 3. SofaScore-side Hypothesis Test Design

**Candidate explanations** (not mutually exclusive):
- **H-A**: IP reputation blocking (DC ranges blocklisted on API route).
- **H-B**: TLS fingerprint check (JA3/JA4; rejects curl-impersonate-less clients).
- **H-C**: Header/Referer/Origin check on API route.
- **H-D**: Cookie/session set by frontend required before API 200s (frontend-200-then-API-200 handshake — explains frontend-200/API-403 asymmetry if cookie gate missing).
- **H-E**: Geo-restriction (19/20 events restricted in HK/TW regions).

**Discrimination matrix** — designed so each hypothesis has a distinguishing experiment with ≤20 total calls:

| Hypothesis | Test | Expected result if TRUE |
|---|---|---|
| H-A IP reputation | P1-2 + P2-3 (Webshare + browser headers) | Same IP class still 403 even with perfect headers |
| H-B TLS fingerprint | P2-2 curl_cffi host (impersonate chrome) vs P2-1 urllib host | urllib=403 / impersonate=200 on same IP |
| H-C Header check | Webshare Tier-1 with full browser header set vs default | full-headers shift 403→200 without IP change |
| H-D Cookie gate | Two-stage probe: GET `/event/{id}` harvest Set-Cookie → replay `/api/v1/event/{id}` with cookies, same client/session, 3 events | Cookie-carrying API call 200s where no-cookie 403s |
| H-E Geo-restriction | Run P2-2 host test from host container (HK egress) AND (if approved) a Vultr/Linode JP/SG node probe via SSH session *read-only* (no deployment) | HK-200 / non-HK-403 or vice-versa |

**Success criteria**: at least one hypothesis returns a decisive signal (effect size: ≥2/3 calls flip verdict when toggling a single variable) with the alternative experiments as control.

**Rollback**: all probes are read-only GET; worst case = a few more 403s against public endpoints; no state mutated.

---

## 4. Root-Cause Matrix (5 layers × 5 hypotheses)

| Layer \ Hypothesis | H-A IP rep | H-B TLS | H-C Header | H-D Cookie | H-E Geo |
|---|---|---|---|---|---|
| **L1: IP / ASN** | P1-1, P1-2 | — | — | — | P1-1 |
| **L2: TLS / TCP** | — | P2-1, P2-2 | — | — | — |
| **L3: HTTP headers** | — | — | P2-1, P2-3 | P2-1 (cookie capture) | — |
| **L4: Session / cookie state** | — | — | — | H-D test | — |
| **L5: Request route (frontend vs API)** | existing `multi_source_ip_audit` | — | — | H-D test | P2-2 host |

**Current best-supported primary blocker** (from existing evidence, ranked):
1. **H-A IP reputation on API route (DC range block)** — supported by front/API asymmetry on Webshare + 85/100 FAIL skew.
2. **H-B TLS/UA fingerprint** — supported by urllib-host 403 vs host-egress 200.
3. **H-D cookie gate** — plausible moderator explaining frontend 200/API 403 same-IP pattern.

**Kill criteria for a hypothesis**: any single decisive counter-example (e.g. host + perfect TLS + no cookie still 403 on event 8351333 kills H-D-or-H-B-only explanations and elevates H-A or H-E).

---

## 5. Revised Execution Plan v0.7 (forensic stage)

**Principles**: (a) live-test only what's needed to disambiguate, (b) hard call budget, (c) every probe read-only GET, (d) rollback = stop, no cleanup needed, (e) results feed plan v0.8 before any further backfill activity.

| Step | Action | Live calls | Time est. | Success criterion | Rollback |
|---|---|---|---|---|---|
| S1 | P1-1 offline ASN/geo join | 0 | 15 min | 1 significant split | n/a |
| S2 | P2-1 HAR + fingerprint capture | 3 | 30 min | ≥1 deterministic diff | stop |
| S3 | P2-2 host curl_cffi chrome | 3 | 10 min | 200×3 → H-B primary | stop |
| S4 | P2-3 Webshare full-headers | 3 | 10 min | 200×3 → H-C primary | stop |
| S5 | H-D two-stage cookie test | 6 | 15 min | cookie-call 200, no-cookie 403 on same client | stop |
| S6 | P1-2 PASS-IP reproducibility | 50 | 60 min | repeatability R | stop |
| S7 | P1-3 PASS-IP × event matrix | 24 | 30 min | variance by event vs by run | stop |
| S8 | H-E geo probe (optional, needs second-egress approval) | 3 | 30 min | HK vs non-HK split | stop |

**Total budget**: ≤92 live GET calls (~1.5–2.5 h wall-clock); well under the previous 600/800 caps. **Time-box**: 3 h including analysis write-up.

**Rollback gates**: any test producing non-403 non-200 codes ≥50% of the time → halt the sub-test, document, don't improvise new probes within the same dispatch.

---

## 6. Forbidden Actions (explicit NO-GO for this phase)

- ❌ Live backfill (`backfill_runner`, `audit_season_coverage`, any mass-fetch) against SofaScore.
- ❌ Any MySQL write (`INSERT`/`UPDATE`/`DELETE`/`CREATE`, including temp tables) — forensic stage is read-only.
- ❌ Changing Gen4 default transport / tier-routing in code or config.
- ❌ Re-running Phase 4 smoke artifact generation or any historical artifact re-generation.
- ❌ Production rollout, CI/CD trigger, or deployment.
- ❌ Including credentials, cookies, session tokens, or authenticated URLs in any artifact, doc, log excerpt, or Telegram message.
- ❌ Rotating Webshare credentials mid-forensic (would invalidate the IP-level comparisons).

---

## 7. Linked Artifacts (verified present)

- `data/gen4_phase10_6_broad_canary.json` + `data/gen4_phase10_6_canary.log` ✅
- `data/gen4_phase10_5_broad_canary.json` + `data/gen4_phase10_5_canary.log` ✅
- `data/gen4_phase10_canary.json` + `data/gen4_phase10_canary.log` ✅
- `data/proxy_audit/100_ip_full_reaudit_20260828_192327.json` ✅ (note: filename differs from Main Agent dispatch label `100_ip_reaudit_*`; this is the only 100-IP re-audit artifact on disk)
- `data/proxy_audit/15_pass_ip_verify_19_20_20260828_193540.json` and `..._193925.json` ✅
- `data/proxy_audit/multi_source_ip_audit_20260828_183336.json` ✅
- `data/proxy_audit/host_ip_verification_20260828_171322.json` ✅
- `data/proxy_audit/full_evidence_pack_20260828_180537.json` ✅
- `data/gen4_phase4_smoke_event_14025013.json` ✅

## 8. Acceptance Self-Check

- [x] Static-only document (no code executed to produce this beyond file lint/read)
- [x] Sections 1–6 all present
- [x] Each test step: hypothesis, method, success criteria, rollback
- [x] Budget estimate (calls + time)
- [x] Linked existing artifacts verified by directory listing
- [x] Cross-referenced MEMORY.md Gen4 Phase 10.5/10.6 FAIL history (Section 0)
- [x] No credentials, no live HTTP performed for authoring this plan

**Gate**: this plan requires Kris approval (via Main Agent) before any of Steps S1–S8 execute.
