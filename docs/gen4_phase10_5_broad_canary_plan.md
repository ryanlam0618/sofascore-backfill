# Gen4 Phase 10.5 — Broad Validation Canary 計劃書

**文件版本:** v0.4 (Duncan v0.3 verify 後嘅 Detail A/B micro-patch)
**起草日期:** 2026-08-28
**起草人:** Forge (coding agent)
**審批人:** Duncan (Main Agent) → Kris 最終批准
**狀態:** DRAFT — 未批，未可執行
**變更紀錄 (v0.3 → v0.4):**
- §4：加 Tier-2 CloakBrowser/Tier-3 SSR 嘅 cookie/context isolation 說明（Duncan Detail A）
- §5 P10 scope 收窄：只 verify Tier-1 curl_cffi；Tier-2/3 歸 §7 cleanup + rebuild-on-403 機制（Detail A）
- §10 audit trail 加 `tier_distribution` field（Detail A）
- §10 L175 `plan_version` field typo 修正為 `"v0.4"`（v0.4 micro-patch；修正 v0.3 写嘅 header 唔 match 問題）
**變更紀錄 (v0.2 → v0.3):**
- §3 Mode：explicit 寫明 3-tier fallback chain（Duncan sub-issue 2B）
- §4 cookie policy 改為「cookie_store 唔注入，requests 零 cookie」— 由源頭消灭 cross-comp leak（Duncan #2 review：Option A-variant，比雙 key jar 更保守更简单）
- §5 加 P10 gate：verify 任何 request 冇帶 cookies
- §10 audit trail 加 `cookie_namespace_verified="disabled_no_cookie_injection"`
**變更紀錄 (v0.1 → v0.2):**
- §6 step 4：future-date guard 改用 event endpoint light-check（parse startTimestamp）+ startTimestamp 缺時保守 skip；**唔再全殺 25/26**（MUST FIX #1）
- §4：cookie isolation 實測 — Gen4Fetcher jar 係 keyed by proxy identity，唔係 competition_id；改為新 script 內 per-(competition, proxy) 雙 key cookie store，唔郁 protected（MUST FIX #2）
- §3：season 範圍統一寫 15/16 → 25/26，剔除淨靠 future-date guard（#3）
- §10：加 `ineligible_cells[]` field（#4）
- §9：加 season_label format 唔統一風險 + normalize mapping（#5）
**前置依據:**
- Phase 10 v1.2 VERIFIED PASS（2026-08-28，P1-P7 全綠，40/40 first-try）
- Kris 2026-08-28 04:26：「試埋全部10年 要爬嘅聯賽，每個賽季抽一場再試」
- Kris 2026-08-28 04:40：**揀 sequencing A** — rotate → re-audit → **本 canary** → Phase 11
- Duncan 2026-08-28 dispatch（10 個 design 點 consensus）

**Sequencing lock（Kris LOCKED）:**
```
Step 1: Webshare password rotation (Kris 親自做)          ← 阻塞
Step 2: Sentinel re-audit 21 IP pool (新 credentials)
Step 3: 本 Phase 10.5 canary (新 creds + 新 audit)
Step 4: Phase 11 plan doc
Step 5: Phase 11 rollout
```
⚠️ **本 canary 執行前，Step 1/2 必須已完成**（§7 checklist 有 verify gate）。

---

## 1. 目的

Phase 10 證明咗 hash-fixed pool 喺 10 場（5 聯賽）work；Kris 要求更大覆蓋：
**全部要爬嘅聯賽 × 每個 available 賽季 × 1 場**，驗證：

1. 跨聯賽 × 跨年（~10 年歷史）嘅 endpoint 覆蓋率
2. Per-competition sticky session 喺 long-running（30-60 min）下嘅穩定性
3. 時間軸上有冇結構性 gap（P8 season coverage gate）
4. 為 Phase 11（Gen2→Gen4 default swap）提供 production-grade 數據基礎

## 2. 同 Phase 10 嘅分別

| 項目 | Phase 10 v1.2 | Phase 10.5（本計劃） |
|---|---|---|
| Events | 10（5 聯賽 × 2） | ~113（13 聯賽 × 每季 1 場）|
| IP 分配 | per-event hash | **per-competition sticky**（每聯賽一 IP，403 先 rotate） |
| Burst guard | ≤2 events/IP + re-hash | 唔適用（per-comp sticky 取代） |
| Warm-up | run-level 1 次 | 同（global once per canary） |
| Gates | P1-P7（STRICT 100%）| P1-P9（P2 ≥99%，新 P7/P8/P9 coverage gates） |
| Budget | 64 | **600**（493 base + 21% buffer） |
| Expected runtime | ~1 min | 30-60 min |

## 3. Scope（嚴格限定）

| 項目 | 值 |
|---|---|
| Competitions | 13 個有可用樣本嘅聯賽（census: PL, La Liga, Serie A, UCL, UEL, Ligue 1, CSL, J1, A-League Men, AFC CL, UECL, J.League Cup, K League 1 — 以 sampling 時 CSV 實際過濾為準） |
| Seasons | 15/16 → 25/26（all available）；**§6 future-date guard 會剔除 `startTimestamp > execution_date` 嘅場**（會實際剔咗 25/26 入面未開波嘅場，已開波嘅保留） |
| Endpoints | `event`, `incidents`, `lineups`, `statistics`（4 個/event） |
| Expected cells | ~123 → 預期 ~113 after future-date filter |
| Mode | Gen4 + FixedPoolRotator，`write_enabled=False` invariant；**transport = 3-tier fallback chain**（Tier-1 curl_cffi → Tier-2 CloakBrowser → Tier-3 SSR，同 Phase 10 一致；Phase 10 全部 Tier-1 一 take 過，Tier-2 係 safety net 而未用過） |
| HTTP calls 上限 | **600**（empirical：493 base + 21% buffer。Phase 8 ceiling 3936 = 15% utilization，唔會混淆） |
| Per-event cap | **6 attempts**（Phase 10 係 10，123 events 要收緊） |
| Pool | 新 audit 嘅 clean IP pool（21 或 re-audit 後嘅實際數量） |
| Password | 預期 `password_status="rotated_fresh"`（本 canary 喺 re-audit 之後跑） |

## 4. Selection — per-competition sticky session（Duncan 方案 B）

```python
# 每個 competition pre-assign 一個 IP（locked at run start）
ip_idx = pool[ sha256(f"broad-v1.0:{competition}:{season_label_first}") % pool_size ]
# 實際 pre-assignment 用 competition-level seed（見 §6 algorithm）
```

規則：
- 每個 competition 嘅**所有賽季用同一 IP**，直到 403
- 403 → 該 competition rotate 到 pool 下一個 IP（clockwise），記入 `per_competition_rotation[]`（含 from/to/reason/attempt_n）
- 其他 competition 唔受影響
- **Cookie policy — v0.3 final（Duncan #2 review 後決定）：** 實測確認 `gen4_fetcher.py` 嘅 cookie jar 係 keyed by proxy identity（`evict_by_proxy`），唔係 competition_id；而 curl_cffi request 嘅 cookies 係由 `cookie_store.inject()` 經 `_last_snapshot` 注入。**Phase 10.5 採用最保守方案：cookie_store 唔注入 Fetcher（`cookie_store=None`）** — 即 Tier-1 curl_cffi request 完全唔帶 cookies，leak class 由源頭消失（3 comps / IP collision 下都安全）。理據：(1) Phase 9/10 已證明 cookies 無作用；(2) 避免改 Fetcher API。Set-Cookie 照舊由 instrumented curl 記錄做 audit，但**唔會 forward 落任何 request**。Audit 記 `cookie_namespace_verified="disabled_no_cookie_injection"`。
- **Tier-2/3 cookie 範圍聲明（v0.4 Detail A）：** `cookie_store=None` 只管 Tier-1 request header。Tier-2 CloakBrowser 嘅 browser context cookie 由 CloakBrowser 自己處理；hash collision（3 comps 共享 1 IP）下，Tier-2 fallback 唔會有 cross-comp cookie sharing，因為：
  - **CloakBrowser per-competition rebuild-on-403**（Phase 8 design Q6 rotation 機制）會喺 403 觸發重建 browser context，唔保留 old cookie
  - 每個 fetcher instance 只綁一個 competition 並 own lifecycle，browser context 唔會 cross-pollinate
  - Cross-comp collision 只喺同一 IP hash 撞車時發生，而呢啲 comp 各自嘅 CloakBrowser session 係獨立 context，唔同 IP
  - Tier-3 SSR 係無狀態 HTTP fetch，唔會 cookie-carry
- **唔好** inherit Phase 10 嘅 ≤2/IP burst guard（per-comp sticky 唔需要）
- Hash 撞車（兩個 comps 同 IP）：**允許**（21 IP vs 13 comps 好難避免），audit 時記低 `ip_assignment[]`；唔會觸發重抽。已知撞車分佈（21 IP pool 情境）：最多 3 comps/IP（如 CSL + Ligue 1 + UEL 同 idx 19）。

## 5. Gates（P1-P9，多層）

| Gate | 條件 | 點驗 |
|---|---|---|
| **P1**（informational）| 整體 ≥ 95% success | `n_200_complete / total ≥ 0.95` |
| **P2** | per-endpoint ≥ 99% success | 每個 endpoint ≤1 fail（123 calls，容 1 fail ≈ 0.8%） |
| **P4** | 總 calls ≤ 600；per-event ≤ 6 | call_stats + per_event_calls |
| **P5** | Cleanup：`cleanup_ok in [True, None]`；False 查 teardown log 先判定 | cleanup 欄位 + teardown log |
| **P6** | 冇誤觸發 safety halt | `halt_reason` 為 null 或合理 reason |
| **P7** | **每個 sampled competition 至少 1 場 PASS**（防某 comp 100% fail 被掩蓋）| per-comp any-PASS |
| **P8** | **每個 sampled season 至少 1 場 PASS**（防時間軸 gap）| per-season any-PASS |
| **P9** | **≥95% (comp, season) cells 4 endpoints 全 PASS** | cells all-pass rate |
| **P10** | **Cookie leak check（Tier-1 scope）**：`cookie_store=None`，**curl_cffi request 冇帶 cookies**（從 call_log 檢查所有 Tier-1 entry 嘅 `request_cookie_names` 為空）；Tier-2 CloakBrowser / Tier-3 SSR 嘅 context cookie 由 §7 step 6 cleanup verification + CloakBrowser rebuild-on-403 機制保證（詳 §4），唔屬 P10 scope | curl_cffi call_log |

**判定制式:** PASS = P2 + P4 + P5 + P6 + P7 + P8 + P9 + P10 全綠（P1 資訊性）。
**P3 移除**（per-competition sticky 下「連續 3×403 同 IP」唔適用）。

## 6. 抽樣方法（deterministic）

```python
seed = f"broad-v1.0:{competition}:{season_label}"
```

流程：
1. 來源：`data/coverage_audit/season_sample_coverage.csv`
2. Candidate filter：`status == "ok"` 且 `incidents_status == "200"` 且 `lineups_status == "200"` 且 `statistics_status == "200"`
3. 逐 (competition, season_label) cell：對所有 candidate events 計 `sha256(f"broad-v1.0:{competition}:{season_label}:{event_id}")` sort，揀第 1 場
4. **Future-date guard（v0.2 強化）：** 唔可以用 season_label 做 proxy（會全殺 25/26 入面已開波嘅場）。改為：
   - **Primary**: event endpoint 1-call light-check — fetch `/api/v1/event/{event_id}`，200 後 parse `startTimestamp`；if `startTimestamp > execution_now` → 記 `future_schedule_excluded`，skip，同一 cell 冇次選（CSV 每 cell 得一場）→ 整個 cell 記 ineligible
   - light-check 算 1 call，**計入 per-event cap 6 同 run cap 600**
   - **Fallback**: 若 `startTimestamp` 缺失 / 唔 parse 到 → 保守處理「skip 該場並標 `startTimestamp_unparseable`」（唔會當 pass，cell 記 ineligible）
   - 被剔除嘅 cell 全部寫入 `ineligible_cells[]`（含 comp/season/event_id/reason）

## 7. Execution checklist

1. [ ] Kris 批准 v0.1 plan
2. [ ] **Gate: Step 1/2 完成確認** — verify 新 audit file mtime > rotation timestamp；唔可以攞舊 audit 跑
3. [ ] Offline regression：`pytest tests/test_gen4_phase8_1_integration.py` 15/15
4. [ ] 新 script `gen4_phase10_5_canary.py`（reuse `gen4_fetcher.py` + `fixed_pool_rotator.py`，**唔郁 protected**）
5. [ ] Pre-flight：抽樣 ~123 cells → future filter → ~113；IP 分配表 `ip_assignment[]` lock-in
6. [ ] 執行 canary（30-60 min；global warm-up 1 次；493 base + retry buffer）
7. [ ] Cleanup（P5）
8. [ ] Artifact：`data/gen4_phase10_5_broad_canary.json`
9. [ ] Completion report：`subagent_results/<YYYYMMDD-HHMMSS>-coding-gen4-phase10-5-canary.json`
10. [ ] Gate eval P1-P9 → Duncan → Kris
10a. [ ] **如 fail**：定位 (comp, season, endpoint) → 三層 `failure_diagnosis{}`（competition-level / season-level / endpoint-level）→ **唔准 auto-rollback**（Gen4 仍 shadow）→ escalate Kris
11. [ ] Kris 決定 Phase 11 plan doc

## 8. Forbidden（Phase 10 全條 + 新增）

- ❌ 唔准用 rotate gateway（`***REMOVED***-rotate`）
- ❌ 唔准改 protected：`backfill_runner.py` / `gen4_fetcher.py` / `gen4_ssr.py` / `gen4_phase4_live_smoke.py`
- ❌ 唔准改 default 去 Gen4（Phase 11 先處理）
- ❌ 唔准擴大 events 超過 sampling lock-in 數量
- ❌ 唔准 credentials 寫入任何 artifact / git commit
- ❌ mid-canary run 中途 rotate password
- ❌ mid-canary 加新 IP 入 pool（hash 分配 lock-in at start）
- ❌ mid-canary 改 coverage CSV（sampling determinism 要 lock）
- ❌ mid-canary 調 warm-up 次數
- ❌ mid-canary 改 per-comp IP 分配（唯一例外：403 觸發 rotate）
- ❌ cross-competition IP sharing at runtime（一個 IP runtime 同時服務多個 comp；hash 撞車 pre-assignment 除外並已記錄）

## 9. 已知風險

| 風險 | 等級 | 應對 |
|---|---|---|
| Hash 撞車：13 comps / 21 IPs，部分 IP 服務 2-3 comps | 低 | 允許；`ip_assignment[]` 記錄；403 rotate 獨立運作；cookie 靠 per-(competition, proxy) double-key jar 隔離（§4） |
| CSV `season_label` format 唔統一（PL `15/16`，UCL `2016`）| 低 | cross-comparison 時 normalize：`2020` == `20/21`（mapping 寫入 script，sampling 內唔靠字串比較 season）；風險 = season bucket 錯位 → P8 gate 可能誤判，mitigate：bucket key 用 normalize 後嘅 start_year |
| 個別 (comp, season) data availability limit（CSV status=ok 但 runtime endpoint 空）| 中 | P9 容許 5% cell fail；P2 容許 1 fail/endpoint；failure_diagnosis 分層記錄 |
| Re-audit 後 IP pool 唔同（phase 10 嘅 21 IP 唔再 valid）| 中 | §7 step 2 verify 新 audit；pool_size 動態讀，唔寫死 21 |
| 123 場 × 4 endpoints 嘅 network blip（30-60 min 長跑）| 低 | P2 ≥99% 設計就係為咗容 1 次 blip |
| SofaScore rate-limit 長跑觸發 | 中 | per-event cap 6 + total cap 600 抑制；403 → per-comp rotate（不連坐） |
| CSV 冇 event_date 欄位，future guard 要 runtime 判斷 | 低 | §6 step 4 寫明 proxy 規則 + 記錄實際剔除原因 |

## 10. Audit trail fields（artifact 必填）

- `plan_version="v0.4"`、`approved_by`、`password_status`（預期 `rotated_fresh`）
- `audit_file_used: {path, mtime_iso, size_bytes, ip_count}`
- `sampling_seed_version="broad-v1.0:{competition}:{season_label}"`
- `cookie_namespace_verified`（固定值 `"disabled_no_cookie_injection"`）
- `tier_distribution: {tier1_count, tier2_count, tier3_count, fallback_rate}`（證明 Tier-1 主導 + fallback 比率）
- `sampling_filter: {total_cells, future_date_excluded, total_selected, ineligible_cells[]}`
- `ip_assignment: [{competition, ip_index, ip_addr_port}]`
- `ineligible_cells: [{competition, season_label, event_id, reason}]`（含 future_schedule_excluded / startTimestamp_unparseable）
- `per_competition_rotation: [{competition, from_ip, to_ip, reason, attempt_n}]`（即使空 array 都要寫）
- `failure_diagnosis: {competition_level, season_level, endpoint_level}`（PASS 時 null）
- `call_stats: {total_http_calls, endpoint_attempts, warmup_calls, per_competition_calls}`
- `write_enabled=False`（inherited invariant）

## 11. Time & cost budget

| 項目 | 預計 |
|---|---|
| Offline regression | ~1 min |
| Sampling + pre-flight | ~2 min |
| Canary run | 30-60 min（493 calls @ avg ~1s，間或 Tier-2 browser fallback 會慢啲） |
| Sentinel re-audit（唔係本 task，前置）| 5-10 min |
| **總 wall-clock** | **~40-70 min** |

---

> **等待三重 gate：**
> ① Duncan inline review 本 v0.1 → ② Kris 批准 v0.1 → ③ Kris 完成 password rotation + Sentinel re-audit 完成（Step 1/2）
> 三個齊晒先執行 §7。
