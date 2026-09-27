# Gen4 Phase 10 — Multi-Event Canary 計劃書

**文件版本:** v1.2 (Duncan v1.1 inline review 後修訂 — 4 個 execution-time 細節補完)
**起草日期:** 2026-08-28
**起草人:** Forge (coding agent)
**審批人:** Duncan (Main Agent) → Kris 最終批准
**狀態:** DRAFT — 未批，未可執行
**變更紀錄 (v1.1 → v1.2):**
- §6/§7：抽樣來源改為明確 file 路徑 `data/coverage_audit/season_sample_coverage.{csv,json}`（Duncan v1.2 concern 1 — MUST FIX）
- §3/§5：64-call budget 標明係 v1.3 empirical happy-path，Phase 8 ceiling = 320，Phase 11 引用時唔好混淆（concern 2）
- §4：`retry{n}` 加上限 ≤5，超過即 error 唔會無限 spin（concern 3）
- §7：P7 fail escalation path — 唔會 auto-rollback 去 Gen2，定位 → 分析 → escalate Kris（concern 4）
**變更紀錄 (v1.0 → v1.1):**
- §3/§10：密碼未 rotate 情況顯式書明 `password_status="leaked_not_rotated"` + Phase 11 前必須 rotate（Duncan concern 5 — MUST FIX）
- §6：抽樣改為 deterministic algorithm（seed 固定，reproducible）（Duncan concern 1）
- §5：加 P7 strict gate — 每個 endpoint 100% pass（Duncan concern 2）
- §4/§6：每個 IP 最多服務 2 場賽事，超過觸發重抽（Duncan concern 3）
- §7：cleanup 驗證寫明 `cleanup_ok in [true, None]`，防 false-positive bug 重現（Duncan concern 4）
**前置依據:**
- `docs/gen4_refactor_design.md` §6 Phase 10（multi-event canary）
- Phase 9 v1.3 結果：hash-fixed pool（21 個 clean IP）對 event 14025013 四個 endpoint 全部一 take 200 COMPLETE
- 2026-08-28 新機制：`FixedPoolRotator`（hash 固定 + rotate() 時同步 mutate config）

---

## 1. 目的

Phase 9 v1.3 證明咗：**用 21 個已知乾淨 IP 做 hash 固定 pool，retry 根本唔使出場**。但單一 event 成功唔代表 multi-event 穩定 — Phase 10 就係用 10 場真賽事，驗證：

1. Hash 固定 pool 喺多個 event_id 下分散是否均勻（唔會焗住某幾個 IP）
2. 過往歷史回測到而家嘅 event latency / 403 率係咪真係跌到接近零
3. Phase 8.1 嘅 retry-with-rotation safety net 喺「罕見壞 IP」情況下幾時先會郁
4. 為後續 Phase 11（Gen2→Gen4 default 切換）提供真實數據基礎

## 2. 同 v1.3 嘅分別

| 項目 | Phase 9 v1.3 | Phase 10（本計劃） |
|---|---|---|
| Event 數量 | 1（14025013）| **10**（抽自現有 backfill 數據） |
| IP 池 | 21 個 clean（audit_20260827）| 同上（重用同一 audit）|
| Selection | `sha256(event_id) % 21` | 升級：`sha256(f"{event_id}:{competition_id}") % 21`（Phase 10 Q1 預備） |
| Warm-up | 每次 run 前打一次 homepage | 只打一次 homepage（warm-up 一次過） |
| Retry | 預設唔郁，403 先 rotate | 照舊（hash 預設 + 403 rotate，cap 3）|
| Report gate | G1-G5 | 改用 P10 新 gates（見 §5） |

## 3. Scope（嚴格限定）

| 項目 | 值 |
|---|---|
| Events | 10 場，從 `data/coverage_audit/` 現有記錄抽（盡量涵蓋唔同 competition） |
| Endpoints | `event`, `incidents`, `lineups`, `statistics` — 4 個 |
| Mode | Gen4 + FixedPoolRotator，`write_enabled=False` invariant |
| HTTP calls 上限 | 10 events × 4 endpoints = 40 次 base attempts；加 retry/rotate 上限 **64**（⚠️ 呢個 64 係 v1.3 empirical happy-path 上限 — 即「GOOD IP 一 take 過，最多每場 1 次 rotate」情況。Phase 8 嘅絕對 ceiling 係 10×4×8 attempts = **320**；Phase 11 rollout 引用 budget 數字時唔好攞錯 64 當 ceiling） |
| Pool | 21 個 IPs（來自 audit_20260827_192348.json，filter MIXED 後） |
| ⚠️ Password前提 | 分兩手處理（見 §10）：**已 rotate** → 必須重做 audit 先用新 credentials；**未 rotate** → 仍可執行，但 artifact 必須寫 `password_status="leaked_not_rotated"`，而且 **Phase 11 之前必須 rotate** 先可進行 |

## 4. Selection 機制（v1.3 → v1.4 upgrade）

```python
# Phase 9 v1.3（單一 event）
ip = pool[ sha256(event_id) % 21 ]

# Phase 10（multi-event，可能跨 competition）
ip = pool[ sha256(f"{event_id}:{competition_id}") % 21 ]
```

理由：
- 即使同一日多場賽事，唔同 competition 會分散到唔同 IP
- Reproducible：同一 (event, competition) 永遠同一個 IP
- 唔使 state（無需記住上次用邊個）

**IP burst 防護（v1.1 新增，v1.2 加 cap）:** 每個 IP 最多服務 **2 場賽事**。如果 10 場 hash 後某 IP 被分配 3+ 場 → 觸發重抽：將超出嘅 event 用 `sha256(f"{event_id}:{competition_id}:retry{n}")` re-hash（`n` 由 1 開始），直至每 IP ≤ 2 場。**`n` 上限 = 5**；re-hash 超過 5 次仍唔得即 error out（`ip_assignment_error`），唔會無限 spin。重抽過程寫入 artifact（`ip_reassignments[]`）。

## 5. Gates（同 Phase 9 唔同 — 重新定義）

| Gate | 條件 | 點驗 |
|---|---|---|
| **P1** | 整體 endpoint 成功率 ≥ 95%（資訊性；真正 pass/fail 以 P7 為準） | `n_200_with_payload / total_endpoints ≥ 0.95` |
| **P2** | 每個 endpoint （event/incidents/lineups/statistics）成功率 ≥ 90% | per-endpoint breakdown |
| **P3** | 無連續 3 次 403 於同一 IP（hash 固定下冇 rotate 觸發）| artifact trace |
| **P4** | Quota：總 HTTP calls ≤ 64；每 event ≤ 10 attempts | budget report |
| **P5** | Cleanup：browser context 關閉 + proxy session release + cookie jar 清空 | cleanup_ok + teardown log |
| **P6** | 冇誤觸發 safety halt（407 / EPIPE / quota_warning false positive）| halt_reason 應為 null 或合理 reason |
| **P7 (STRICT)** | **每個 endpoint（event/incidents/lineups/statistics）100% pass（10/10）**；任何一個 endpoint 有 1 場失敗即 Phase 10 FAIL | per-endpoint breakdown = 10/10 |

**判定制式:** PASS = P2-P7 全部過（P1 只作資訊參考）。v1.3 已證明 GOOD IP 一 take 過 200，strict 100% 係合理期望；做到先講到 Phase 11。

舊 G1（cookie reuse）同 G3（retry count > 1）**移除** — Phase 9 已證明 cookie 冇用、retry 唔需要。取而代之係 **P1-P6** 直接對應 production readiness。

## 6. 抽樣方法（10 events — deterministic，reproducible）

- 來源：`data/coverage_audit/season_sample_coverage.csv`（主；同名 `.json` 作 backup）— 現有 audit 記錄，唔使重造
- 篩選：優先 2020+ 賽季（有 xG/incidents/lineups 完整機會高啲）
- **Algorithm（固定，re-run 揀啲場一定一樣）:**
  1. 從 `season_sample_coverage.csv` 逐 competition 收集符合條件嘅 (event_id, competition_id) candidates
  2. 對每個 candidate 計 `sha256(f"phase10-v1.1:{event_id}:{competition_id}")` 並按呢個值 sort
  3. 逐 competition 按 sort 順序揀首 2 場：**PL 2 + La Liga 2 + Serie A 2 + Bundesliga 2 + UCL 2 = 10 場**
  4. 如個別 competition 候選不足 2 場，短缺數量由下一個 competition（按字母序）補上
- 揀完後跑 §4 嘅 IP burst check（每 IP ≤ 2 場）
- 每場記低 competition_id 以便 P4 / P7 追蹤

## 7. Execution checklist

1. [ ] Kris 批准 plan（Duncan 審後）
2. [ ] 喺 `data/coverage_audit/season_sample_coverage.csv` 搵 10 個 event（記低 competition_id），按 §6 deterministic algorithm
3. [ ] 新 script `gen4_phase10_canary.py`（import-reuse `gen4_fetcher.py` + `fixed_pool_rotator.py`，**唔郁 protected**）
4. [ ] Offline regression：`pytest tests/test_gen4_phase8_1_integration.py` 15/15
5. [ ] 執行 canary（約 5-10 分鐘，40 base + retry 上限 64 calls）
6. [ ] Cleanup verification（P5）：**判定 `cleanup_ok in [true, None]`** — `cleanup_ok=False` 未必係真 cleanup 失敗（v1.2 曾出現 safety checker 誤判嘅 false-positive bug）；若 `cleanup_ok=False`，要檢查 teardown log 確認 browser context 真係有冇關先判定，唔可以直接當 FAIL
7. [ ] Artifact：`data/gen4_phase10_canary.json`
8. [ ] Completion report：`subagent_results/20260828-HHMMSS-coding-gen4-phase10-canary.json`
9. [ ] Gate 評估 P1-P6 + P7 STRICT，報告交 Duncan → Kris
9a. [ ] **P7 fail escalation（如適用）:** 如果某個 endpoint 唔係 10/10 — ① 定位失敗場次嘅 IP / competition_id / endpoint；② 分析係 hash distribution 問題（同一 IP burst？IP 被黑？）定 endpoint-specific 問題（例如尖銳 per-endpoint 403 pattern）；③ 寫入 artifact `failure_diagnosis`；④ **唔會自動 rollback 去 Gen2**（Gen4 仍係 shadow mode，冇 default rollout 過）— 必須 escalate 畀 Kris 決定下一步，唔准 agent 自行改 plan
10. [ ] Kris 決定：進 Phase 11（default rollout） / 做調整 / hold（如 P7 fail，Kris 亦要揀 Phase 10 re-run 範圍）

## 8. Forbidden（未經 Kris 再批准，絕對唔准）

- 唔准用 rotate gateway（`***REMOVED***-rotate`）— 只准用 hash-fixed pool
- 唔准改 protected files：`backfill_runner.py` / `gen4_fetcher.py` / `gen4_ssr.py` / `gen4_phase4_live_smoke.py`
- 唔准改 default 去 Gen4 — Phase 11 先處理
- 唔准跑多過 10 events
- 唔准將 credentials 寫入任何 artifact / git commit
- 唔准 multi-competition 超過計劃範圍

## 9. 已知風險

| 風險 | 等級 | 應對 |
|---|---|---|
| 21 IP pool 入面某幾個被 SofaScore 列入黑名單（IT 變動）| 中 | 遇此情況 P3 應該觸發 rotate；artifact 會記錄 |
| 10 場賽事集中喺某啲 competition（懶random） | 低 | 用 hash 分散；competition_id 入 key |
| Cookie 無 Set-Cookie 持續 | 低 | 已移除 G1，唔再 track cookie 作為 gate |
| 新 password audit 未完成（如 rotate 咗）| 中 | 執行前檢查 audit 文件 timestamp 係咪新過 password rotation；Kris 未 rotate 嘅話按 §10 記錄 `password_status` |
| Latency outlier IP（如 2154ms 嗰個）| 低 | 已 filter MIXED；audit 顯示 IP 329-2154ms 範圍 |

## 10. Wait — 同 Kris confirm

- 而家暫時唔需要 Sentinel 做 statistics probe（v1.3 已證明 statistics 係 IP 問題）
- 但建議 Kris 盡快 rotate password 並俾新 proxy list — 之後 Sentinel 重 audit 先至穩陣
- **如果 Kris 未 rotate password 而批准照跑 Phase 10：** 照做，但 completion artifact（`subagent_results/*-gen4-phase10-canary.json`）必須包含 `password_status="leaked_not_rotated"`，並喺 `risks[]` 寫明 **MUST rotate password before Phase 11** — Phase 11 唔批得過未 rotate。

---

> **等待批核中。** 未經 Kris 明確批准（經 Duncan），唔會執行任何 Phase 10 動作。
