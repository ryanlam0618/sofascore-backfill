# Gen4 Phase 10.6 — Broad Validation Canary v0.5 計劃書

**文件版本:** v0.5 (Phase 10.5 FAIL 後 pivot — Kris 05:43「全部 yes」)
**起草日期:** 2026-08-28
**起草人:** Forge (coding agent)
**審批人:** Duncan (Main Agent) → Kris 最終批准
**狀態:** DRAFT — 未批，未可執行
**前置依據:**
- Phase 10.5 v0.4 canary：FAIL @ first event — historical event 403 pattern（Tier-1 9 IP 全 403, Tier-2 403, Tier-3 0；但 incidents Tier-2 200）
- Root cause：403 係 event-age-specific / endpoint-specific，唔係 IP-specific
- Kris 05:43：「全部 yes」（Q1/Q2/Q3/Q4/D/F 6 個 decision points）
- Phase 10 LOCKED PASS（新賽事 Tier-1 全成功）；historical 賽事需要 Tier-2 primary

**Sequencing:**
```
Step 1: Forge 起 plan v0.5 (本文檔)
Step 2: Duncan inline verify
Step 3: Kris 簽 v0.5
Step 4: Forge re-audit proxy pool (multi-endpoint × multi-age, Task 2 methodology)
Step 5: Forge §7 execute canary (Tier-2 primary)
Step 6: Forge postmortem incident #1-#4 → memory/2026-08-28.md
Step 7: Handoff → Main Agent verify → 呈 Kris
```

---

## 1. 目的

Phase 10.5 FAIL 揭示：**歷史賽事（15/16–19/20）喺 Tier-1 curl_cffi 無論換幾多 IP 都係 403**——SofaScore 對歷史 endpoint 有 fingerprint/age-specific blocking，**唔係 IP 問題，換 IP 解決唔到**。
Phase 10.6 改用 **Tier-2 CloakBrowser 為主 transport**，配合 re-audit pool，驗證 113 cells × 4 endpoints 嘅廣覆蓋穩定性，為 Phase 11 rollout 提供 production-grade 數據。

## 2. 同 Phase 10.5 嘅分別

| 項目 | Phase 10.5 v0.4 | Phase 10.6 v0.5（本計劃） |
|---|---|---|
| Transport | Tier-1 first（3-tier fallback chain） | **Tier-2 CloakBrowser primary**，Tier-1 只係 fallback |
| Tier order | Tier-1 → Tier-2 → Tier-3 | **Tier-2 → Tier-1 → Tier-3** |
| Cookie (Tier-1) | cookie_store=None | 不變 |
| Cookie (Tier-2) | per-comp rebuild-on-403（未 verify） | per-comp rebuild-on-403（Phase 8 design Q6） |
| Budget cap | 600 | **800** |
| Per-event cap | 6 | **8** |
| Gates | P1-P10 | P1-P10 + **P11 tier2 hit rate ≥ 90%** |
| Re-audit base | single-endpoint probe (`event/14025013`) | **multi-endpoint × multi-age probe** |
| Time budget | ~40-70 min | **80-110 min** |

## 3. Scope

| 項目 | 值 |
|---|---|
| Competitions | 13 個（同 v0.4：PL, La Liga, Serie A, UCL, UEL, Ligue 1, CSL, J1, A-League, AFC CL, UECL, J.League Cup, K League 1 — census 凍結） |
| Seasons | 15/16 → 25/26（all available；future-date guard 剔除 `startTimestamp > now`） |
| Endpoints | `event`, `incidents`, `lineups`, `statistics`（4/cell） |
| Expected cells | 123 → 預期 ~113 after future filter |
| Mode | **Tier-2 CloakBrowser primary**；Tier-1 curl_cffi 只係 Tier-2 完全唔通時嘅 fallback；Tier-3 SSR 最後 |
| Budget | **≤800 HTTP calls**（452 base + buffer；vs Phase 8 ceiling 3936 ≈ 20%） |
| Per-event cap | **8 attempts** |
| Warm-up | global once（同 v0.4） |
| write_enabled | False invariant |
| Pool | 新 re-audit 嘅 GOOD IPs（multi-endpoint × multi-age probing，Task 2 輸出） |

## 4. Selection — per-competition sticky（繼承 v0.4）

```python
ip_idx = pool[ sha256(f"broad-v1.0:{competition}") % pool_size ]
```

- 每個 competition 預先 hash 分配 1 個 IP，該 comp 所有賽季用同一 IP，直到 403
- 403 → 該 comp rotate 到 pool 順序下一個 IP（clockwise），記入 `per_competition_rotation[]`
- Hash 撞車允許，記入 `ip_assignment[]`
- Cookie isolation：Tier-2 `cookie_store=None` + CloakBrowser per-comp rebuild-on-403；唔跨 comp
- **唔可以 echo IP / event_id plaintext 喺 handoff**（Option C policy expand, incident #4 lesson）

## 5. Gates

| Gate | 條件 | 點驗 |
|---|---|---|
| P1 (informational) | overall ≥ 95% | `n_ok / total` |
| P2 | per-endpoint ≥ 99%（容 ≤1 fail/endpoint） | per-endpoint breakdown |
| P4 | total ≤ 800, per-event ≤ 8 | call_stats |
| P5 | cleanup_ok in [True, None] | teardown log |
| P6 | halt_reason null 或合理（唔係 spurious safety） | halt_reason |
| P7 | 每個 sampled competition 至少 1 場全 PASS | per-comp |
| P8 | 每個 sampled season 至少 1 場全 PASS | per-season |
| P9 | ≥95% cells 4 endpoints 全 PASS | cell all-pass rate |
| P10 | Tier-1 request 冇帶 cookies（call_log 驗證） | cookie leak check |
| **P11**（NEW） | **Tier-2 hit rate ≥ 90%**（全部 ok rows 中用 Tier-2 成功嘅比例 ≥ 90%；要確保 Tier-2 primary 真係主導） | tier_distribution |

**判定制式:** PASS = P2 + P4 – P10 + P11 全綠（P1 informational only）

## 6. Sampling（唔變）

同 v0.4，seed `broad-v1.0:{competition}:{season_label}`，CSV 來源 `data/coverage_audit/season_sample_coverage.csv`。

**Future-date guard（v0.4 繼承）：** primary = event endpoint light-check；`startTimestamp > now` → `future_schedule_excluded`；fallback = 冇 startTimestamp 就 skip；剔除記入 `ineligible_cells[]`。

## 7. Re-audit methodology（Step 4 執行時）</br>

**Multi-endpoint × multi-age probe**（Kris Q1 YES）：

- Probe endpoints: `event`, `incidents`, `lineups`, `statistics`（4 個）
- Probe event-ages: **3 個**（historical 15/16 / mid 20/21 / recent 25/26），每個用 plan v0.4 一個已知嘅穩定歷史 event
- Per IP: 1 request per (endpoint × age) => **12 probes/IP**
- Source pool: `data/proxy_audit/good_proxies_20260827_211530.txt`（新 password 已包）
- Time: ~100 IPs × 12 probes @ concurrency 10 ≈ 10-15 min

**Verdict 分層：**
- **GOOD** — 全部 12 probes 都 200（可用）
- **PARTIAL** — 部分 200（唔考慮用，但 record）
- **STALE** — 全部 403（rate-limit）
- **BROKEN** — 連 proxy 都唔通（407 / timeout）

**Output:** `data/proxy_audit/proxy_re_audit_v2_<timestamp>.json` + `ip_assignment_phase10_6.json`（GOOD IP list，latency-sorted，credentials `.env` runtime inject）

## 8. Execution checklist

1. [ ] Kris 批准 v0.5 plan
2. [ ] Step 4 re-audit 完成 + ip_assignment_phase10_6.json 出嚟（Duncan verify）
3. [ ] Sampling 113 cells（lock-in；寫入 `sampling_filter.total_cells = 123`）
4. [ ] Offline regression `pytest tests/test_gen4_phase8_1_integration.py` 15/15（唔郁 protected）
5. [ ] 新 script `gen4_phase10_6_canary.py` — Tier-2 primary logic（protected files 零改動）
6. [ ] 預 flight IP assignment lock-in（hash + ip_assignment_phase10_6.json）
7. [ ] Warm-up global once
8. [ ] Execute canary（80-110 min，Tier-2 primary）
9. [ ] Cleanup（P5）
10. [ ] Artifact `data/gen4_phase10_6_broad_canary.json`
11. [ ] Gate eval P1-P11 → Duncan → Kris
12. [ ] Postmortem → `memory/2026-08-28.md`（Task 4）
13. [ ] Kris 決定 Phase 11

## 9. Forbidden（Phase 10/10.5 全部 + Option C expand）

- ❌ 唔准用 rotate gateway
- ❌ 唔准改 protected：`backfill_runner.py` / `gen4_fetcher.py` / `gen4_ssr.py` / `gen4_phase4_live_smoke.py`
- ❌ 唔准改 default 去 Gen4（Phase 11 先處理）
- ❌ 唔准擴大 events 超過 sampling lock-in
- ❌ 唔准 credentials 寫入 artifact / git / chat
- ❌ 唔准 echo IP/event_id/password 喺任何 output / handoff message
- ❌ mid-canary rotate / add IP / 改 sampling / 改 per-comp IP 分配
- ❌ auto-rollback（FAIL → escalate Kris）

## 10. 風險

| 風險 | 等級 | 應對 |
|---|---|---|
| Tier-2 browser 每 call 5–10s，canary 時間長（80-110 min） | 中 | 預算 time budget 已加大（Kris Q3 YES）；但 acceptance 就係 slower validation |
| Tier-2 都 403（懷疑 browser fingerprint 被 flag）| 中 | P11 gate 會 fail；escalate Kris |
| SofaScore 對歷史賽事嘅 block 係 per-event 梗死 | 中 | re-audit 驗證到 Step 4 先發現；P7/P9 fail → failure_diagnosis 層級分析 |
| IP count 不足（re-audit PARTIAL 多過 GOOD） | 中 | plan §7 re-audit 処理；Phase 11 前如果唔夠 IP 要 retry audit |
| Excpected 113 cells 同實際唔同（CSV 數據變化） | 低 | hash 鎖死 sampling；唔變化就 reproduce |
| Event metadata 中 startTimestamp 同 wall-clock 誤差 | 低 | `startTimestamp` 係 epoch second；用 UTC `now()` 今年凌晨 00:00 作 boundary |

## 11. Audit trail fields（artifact 必填）

- `plan_version="v0.5"`
- `tier_strategy="tier2_primary"`
- `approved_by`, `password_status`（預期 `rotated_fresh`）
- `audit_file_used {path, mtime_iso, size_bytes, ip_count, method="multi_endpoint_multi_age"}` 
- `sampling_seed_version="broad-v1.0:{competition}:{season_label}"`
- `cookie_namespace_verified="disabled_no_cookie_injection"`
- `tier_distribution {tier1_count, tier2_count, tier3_count, fallback_rate}`（P11 evidence）
- `ip_assignment []`、`per_competition_rotation[]`、`ineligible_cells[]`
- `failure_diagnosis {competition_level, season_level, endpoint_level, historical_pattern}`（FAIL 時填）
- `call_stats {total_http_calls, endpoint_attempts, warmup_calls, per_competition_calls}`
- `write_enabled=False`

## 12. Incident postmortem（Task 4 — Forge 執行時順便寫入）

- Incident #1 (04:54): Kris Telegram plaintext password → agent reject
- Incident #2 (05:04): Duncan `.env` overreach + echo password → policy locks
- Incident #3 (05:24): Sentinel handoff echo credential pair → policy locks
- Incident #4 (05:33): Forge handoff echo IP + event_id → Option C expand
- Root cause: agents 將 sensitive string 視為可分享 string；Option C 唔夠全面
- Mitigation: Option C policy locks + **expand IP/event_id echo lock** + AGENTS.md §8 update + TOOLS.md Webshare section update
- Reference: `memory/2026-08-28.md` postmortem section（Task 4 寫入）

---

> **等 Kris 簽 → Step 4 re-audit → Step 5 canary → Step 6 postmortem → Step 7 Phase 11 decide。**
> 現階段 Forge 唔郁手，等 Kris 簽 v0.5。
