# Gen4 Phase 9 — 單 Event Live Diagnostic Smoke 計劃書

**文件版本:** v1.2
**起草日期:** 2026-08-28
**起草人:** Forge (coding agent)
**審批人:** Duncan (Main Agent) → Kris 最終批准
**狀態:** ✅ APPROVED — Kris 2026-08-28 02:46 揀 A批執行；02:59 批准 warm-up retry extension
**前置依據:** `docs/gen4_refactor_design.md` §6 Phase 9; `subagent_results/gen4_phase8_1_completion.json` phase_9_requirements

**v1.1 變更（回應 Main Agent review 5 個 concerns）：**
1. §5 加入「PASS ≠ production-ready」明確聲明（Concern 1）
2. §5.1 加入 PASS / FAIL 兩種 outcome 嘅 next-step 路線圖（Concern 2）
3. §8 明寫新 script **import reuse `gen4_fetcher.py`**，禁止 fork / 唔會改 protected files（Concern 3）
4. §6 warm-up 失敗即 halt 嘅 edge case 處理 + warm-up 計入 quota budget（Concern 4）
5. §8 加入 cleanup 驗證項目 + artifact 記錄 `cleanup_ok` flag（Concern 5）

**v1.2 變更（Kris 2026-08-28 02:59 option A extension）：**
1. §6.1 warm-up 改為「最多 2 次 attempts」：第一次 fail 後 rotate proxy identity + evict cookies → retry 一次；兩次都 fail 先 halt
2. warm-up attempts 照計入 35-call cap

---

## 1. 目的

Gen4 經過 Phase 7/8/8.1 之後，retry-with-rotation、warm-up、cookie store、header factory、sticky session 已經全部喺 **offline (fake/mock)** 環境接上同測試通過（167/167）。但一切只係車房測試，未出過馬路。

Phase 9 係 **第一次揸住全套 wired components 出真路試**，用一個已知 Gen2 行得通嘅 event (14025013)，驗證：

1. retry loop 真係 live 環境有郁（retry count > 1）
2. cookie 重用真係 work（`Set-Cookie` 收到，第二次 request 有帶出）
3. `/incidents` endpoint 最終攞到 200 + `payload_complete=True`（即係兩個月前嘅 403 blocker 被破解）
4. 所有安全掣 (safety stops) 喺真槍實彈下照樣 work

**呢個係 diagnostic smoke，唔係 production cutover。** Gen2 繼續係唯一 production default，唔會郁。

## 2. 背景速成 (點解有 Phase 9)

| 時間 | 事件 |
|---|---|
| 2026-08-23 | Gen4 canary Profile G 實測 **58.3% PC < 85% gate → FAIL**，Kris HALT |
| 2026-08-25 08:10 | Phase 4 live smoke: `/event` 200 ✅; `/incidents` 403×3 tiers ❌；verdict: NOT production-ready |
| 2026-08-25 | 診斷出 Gen4 缺 Gen2 嘅 6 樣嘢（retry loop / proxy rotation / warm-up / cookie / sticky session / header factory） |
| 2026-08-25 | Phase 7 → 8 → 8.1 全部 offline 完成，167/167 tests pass |

**核心問題未解：** `/incidents` 403 嘅 root cause 未確定。Phase 9 其中一個目標就係用完整 wired stack (warm-up + cookies + headers + retry) 去驗證：到底係咪撼頭埋牆都得？如果仲係 403，先至需要開 root-cause investigation 支線。

## 3. Scope (嚴格限定，超晒即停)

| 項目 | 值 |
|---|---|
| Event | `14025013` only (同 Phase 4 一致，方便對比) |
| Endpoints | `event`, `incidents`, `lineups`, `statistics` (4 個，Phase 4 嘅 2 個加 2) |
| 模式 | `FETCH_STRATEGY=gen4`，全部新 components **INJECTED** |
| 寫入 | `write_enabled=False` — **絕對唔掂 MySQL**，invariant 保留 |
| Proxy 配額 | 呢次 smoke 嘅 attempt 上限 = `TOTAL_MAX_ATTEMPTS_PER_REQUEST=8` × 4 endpoints = **≤32 attempts** |
| 方向 | Read-only；唔 modify 任何外部嘢 |

### Gen4 配置 (Phase 9-mode)

```
max_retries        = 5        # Tier-1 (curl_cffi)，同 Gen2 baseline
browser_retries    = 2        # Tier-2 (CloakBrowser)，Phase 7 Q2 鎖咗
warmup_enabled     = True     # homepage → tournament → event
warmup_strategy    = "per_event"
sticky_session     = enabled  # per_competition
cookie_ttl_cap     = 30 min
retry_state_machine: 407→停 | 403→rotate+retry | 404→落下一 tier | timeout→≤2 次全局
```

## 4. 要 Capture 嘅診斷資料

每個 endpoint request 記錄：

1. **Response headers**（特別係 `Set-Cookie`）— 驗證 cookie flow
2. **每次 attempt**：tier、proxy session id、attempt number、status code、latency
3. **Retry 觸發記錄**：幾多次 retry、邊次 rotate、最後邊個 tier 攞到
4. **Cookie 重用證據**：第二次 request 有冇帶返第一次收到嘅 cookie
5. **payload_complete 標記**：`/incidents` 最終結果係咪 `200 + payload_complete=True`

**輸出 artifact:** `sofascore-backfill/data/gen4_phase9_diagnostic_smoke.json`

## 5. Gate Criteria (過關條件 — 全部✅先算 PASS)

根據 design doc §6 Phase 9：

| # | 條件 | 點驗 |
|---|---|---|
| G1 | Cookie reuse 生效：response 有 `Set-Cookie`，第二次 request 帶出 | artifact header dump |
| G2 | `/incidents` 最終 **200 OK + payload_complete=True** | artifact |
| G3 | 至少一個 endpoint 嘅 **retry count > 1**（證明 retry loop 喺 live 郁過） | artifact per-attempt trace |
| G4 | Safety stops 運作：407 halt / quota warning / scope guard 測試冇誤觸發亦冇失效 | run log |
| G5 | 對比 Gen2 profile_b：4 endpoints 成功率 = 100% / 100% | 同 Gen2 歷史數據對照 |

**任何一項 fail → Phase 9 = FAIL，寫 diagnostic 報告，唔郁手，等 Kris 定 next step。**

### ⚠️ G5 特別聲明：PASS ≠ production-ready（Duncan Concern 1）

就算 G1–G5 全 PASS，**都只代表 wired components 喺 live 環境 work**，**唔代表 Gen4 已經 production-ready**。

根據 Kris 2026-08-25 嘅明確鎖定：「單次 success 唔升級」— 單 event 全 200 **唔可以** trigger 任何形式嘅 promotion / rollout / default change。Phase 9 PASS 嘅唯一後續 = **申請 Phase 10 multi-event canary**（10 events，要另外批）。冇 shortcut。

## 5.1 PASS / FAIL Next-step 路線圖（Duncan Concern 2）

| Outcome | 之後點行 |
|---|---|
| **全 PASS** | 寫 completion artifact → Duncan 報告 → **等 Kris 批 Phase 10 multi-event canary**（唔自動進入） |
| **FAIL：`/incidents` 仲係 403**（其餘 endpoint pass） | Artifact 即 root-cause 第一手資料 → 三個 branch 俾 Kris 揀：<br>**(a)** 開 403 root-cause investigation 支線（查 session/cookies/headers/auth 層）<br>**(b)** ~~blacklist `/incidents`~~ ❌ **Kris 已明令禁止，永久剔除選項**<br>**(c)** HALT Gen4，全線撤回 Gen2<br>Fork 自己唔揀，**一定等 Kris 定** |
| **FAIL：其他原因**（quota 爆、407、timeout 風暴等） | Halt → artifact 記錄 halt reason → Duncan 決定 retry 或更改計劃 |

## 6. Forbidden （未經 Kris 再批准，絕對唔准）

1. ❌ Multi-event canary（嗰個係 Phase 10）
2. ❌ Full backfill
3. ❌ Production rollout / 改 Gen2 default
4. ❌ `/incidents` blacklist
5. ❌ 超過 scope：其他 event / 其他 endpoints / 重跑多次「補數」
6. ❌ 改 `backfill_runner.py`、`gen4_ssr.py`、`gen4_phase4_live_smoke.py`、`gen4_fetcher.py`（protected files，Phase 8.1 zero-diff 清單 + fetcher 本體）

## 6.1 Warm-up 失敗嘅 edge case（Duncan Concern 4）

- Warm-up 本身都 burn quota — **warm-up attempts 計埋入 32 attempts budget 之內**
- 若 warm-up **兩次 attempts 都失敗**（v1.2：first fail → rotate proxy + evict cookies → retry 一次；仍 fail 先 halt）：
  - **立即 halt**，**唔准進入 4 個 endpoint 嘅 retry loop**
  - Artifact 記錄 `halt_reason="warmup_failed"`，避免喺冇 session 嘅情況下亂撞 4 個 endpoint
- 若warm-up pass 但之後某個 endpoint burn 爆 8-attempt ceiling → individual fail，繼續下一個 endpoint（每個 endpoint 獨立 budget）

## 7. Rollback / 出事點收科

- 個 script 只讀唔寫，最大損傷 = 用咗幾十次 proxy request → **天然低風險**
- 任何時候 407 / 異常 403 風暴 / quota 警告 → **自動 halt**，artifact 記錄 halt reason
- 即使 run 中途殺咗，Gen2 production path 完全零影響（zero shared state）

## 8. 執行 Checklist (逐格剔)

1. [ ] Kris `/approve Phase 9` 到手（Duncan 轉達）
2. [ ] 新 file `gen4_phase9_diagnostic_smoke.py`：
   - **import reuse `gen4_fetcher.py`**（Gen4Fetcher + Gen4Config 現成 API）
   - **禁止 fork / copy-paste** fetcher 邏輯 — 發現要改 fetcher 行為 → 停手問 Duncan
   - 只寫 orchestration：config 注入 → 4 endpoints loop → artifact dump
3. [ ] 執行前檢查：`pytest tests/test_gen4_phase8_1_integration.py` 仍然全綠（ regression gate）
4. [ ] Run: single event 14025013, 4 endpoints, Gen4 full-wired mode
5. [ ] **Cleanup 驗證**（同 Phase 4 看齊）：
   - kill browser context / close CloakBrowser
   - 釋放 proxy session
   - clear cookie store（TTL 30min cap 之外主動清）
   - artifact 寫 `cleanup_ok: true/false` + 每項 cleanup 嘅結果
6. [ ] Artifact: `data/gen4_phase9_diagnostic_smoke.json`
7. [ ] Completion report: `subagent_results/gen4_phase9_completion.json`
8. [ ] Gate 評估 G1–G5 → PASS / FAIL verdict（連 §5.1 next-step 路線圖）
9. [ ] 報告交返 Duncan → Kris 決定：入 Phase 10 / 開 403 root-cause 支線 / HALT

## 9. 執行 Script 約束（Duncan Concern 3）

| 項目 | 做法 |
|---|---|
| 檔名 | `gen4_phase9_diagnostic_smoke.py`（新 file） |
| Import 策略 | `from gen4_fetcher import Gen4Fetcher, Gen4Config` — **reuse，唔 fork** |
| 若發現 fetcher 要改先 work | **停手**，報 Duncan — 唔可以為咗 pass 偷偷改 `gen4_fetcher.py`（protected） |
| Protected files | `backfill_runner.py` / `gen4_ssr.py` / `gen4_phase4_live_smoke.py` / `gen4_fetcher.py` — 零改動 |
| 測試 | 執行前先跑 `pytest tests/test_gen4_phase8_1_integration.py` 確認 15/15 仍然 pass |

## 10. 已知風險

| 風險 | 等級 | 應對 |
|---|---|---|
| `/incidents` 仲係 403（warm-up+cookies 都救唔返）| 中 | 本身都係預期內嘅 possible outcome；fail 嘅 artifact 就係 root-cause investigation 嘅第一手資料 |
| Proxy 403 風暴觸發 rate limit | 低 | ≤32 attempts 上限；自動 halt on 407 |
| CloakBrowser 環境問題 (v1_buggy 歷史) | 低 | Phase 8.1 已驗證 DI injection；執行前先跑一次 offline DI check |

---

> **等待批核中。** 未經 Kris (經 Duncan) 明確 `/approve Phase 9`，唔會執行任何 live 動作。
