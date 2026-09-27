# WP1 Tranche v2 Rollout 執行方案 (Plan Doc)

**版本**: v0.2  
**起草日期**: 2026-09-12  
**起草人**: Forge (agent:coding)  
**審批人**: Duncan (Main Agent) → Kris 最終批准  
**狀態**: 📄 REPORT ONLY — Kris 批准前，禁止任何 live run / 大規模 fetch  
**變更**: v0.1 → v0.2：Scope 擴展至 **全部 24 個 competitions**（competitions_10y.yaml 全量），events 估計量、call budget、throughput、tier mapping 同步更新。

---

## 1. Executive Summary

Tranche v2 是 Gen4 的第二波大規模回填，針對 **全部 24 個 competitions**（Big-5、亞洲頂級聯賽、歐洲三大盃、亞冠兩級、各國國內盃）的 **歷史 10 個賽季（15/16 → 24/25）**，使用 **21 IP 預飛檢查通過的好池** 加上 **動態健康評分 + 隔離機制**，以 **Gen4 wired stack**（curl_cffi → CloakBrowser → SSR 三級回退 + warm-up + cookie 重用 + sticky session）執行全端點回填。

**關鍵數字預估**：
- 24 competitions × 10 seasons × 平均 ~170 events/season ≈ **~41,000 events**
- 10 endpoints × 41k events ≈ **410k HTTP 呼叫**（理論上限）；實際隨 skip-on-404/flag-gate 約 **250–300k**
- Stage 2 實測 ~5s/event → **~34 機器小時** ≈ **17 個晚上批次**（每晚 ~2h）

---

## 2. Scope（範圍）

### 2.1 Competitions（全部 24 個，依 competitions_10y.yaml）

| # | Competition | ut_id | category_id | season_mode | 25/26 season_id | Notes |
|---|-------------|-------|-------------|-------------|-----------------|-------|
| 1 | Premier League | 17 | 1 | national | 61627 | 已有 24/25 基礎 |
| 2 | La Liga | 8 | 32 | national | 待解析 |  |
| 3 | Serie A | 23 | 31 | national | 待解析 |  |
| 4 | Bundesliga | 35 | 30 | national | 待解析 |  |
| 5 | Ligue 1 | 34 | 7 | national | 77356 |  |
| 6 | J1 League | 196 | 52 | national | 69871 | calendar-year '2025' |
| 7 | K League 1 | 410 | 291 | national | 88606 | calendar-year '2026' |
| 8 | A-League Men | 136 | 34 | national | 82603 |  |
| 9 | Chinese Super League | 649 | 99 | national | 90049 | calendar-year '2026' |
| 10 | UCL | 7 | 1465 | uefa | 76953 |  |
| 11 | UEL | 679 | 1465 | uefa | 76984 |  |
| 12 | UECL | 17015 | 1465 | uefa | 76960 | 21/22 起存在 |
| 13 | AFC Champions League | 463 | 1467 | international | 77010 | 亞冠精英賽 |
| 14 | AFC Champions League Two | 668 | 1467 | international | 77009 | 亞冠二級（原 AFC Cup） |
| 15 | FA Cup | 19 | 1 | cup | 82557 |  |
| 16 | EFL Cup | 21 | 1 | cup | 77500 |  |
| 17 | Copa del Rey | 329 | 32 | cup | 82988 |  |
| 18 | Coppa Italia | 328 | 31 | cup | 77308 |  |
| 19 | Coupe de France | 335 | 7 | cup | 85565 |  |
| 20 | DFB Pokal | 217 | 30 | cup | 76910 |  |
| 21 | J.League Cup | 101 | 52 | cup | 97720 | calendar-year '2026' |
| 22 | Emperor's Cup | 323 | 52 | cup | 96180 | calendar-year '2026' |
| 23 | Australia Cup | 1786 | 34 | cup | 97074 | calendar-year '2026' |
| 24 | Chinese FA Cup | 882 | 99 | cup | 91152 | calendar-year '2026' |

> **Season 解析**：每個 competition × season 需 **live 解析 season_id**（呼叫 `/unique-tournament/{ut_id}/seasons` 按 year 匹配），保留 `original_season_id` 追溯。UECL 15/16–20/21 不存在（resolver 回傳 None → 自動跳過）。

### 2.2 Seasons（10 個賽季，最舊優先，Phase A–E 每階段 2 seasons）

| Phase | Seasons | 年份標籤 | 備註 |
|-------|---------|----------|------|
| A | 15/16, 16/17 | "15/16", "16/17" / 2015, 2016 | 最舊優先，xG 缺失已知 |
| B | 17/18, 18/19 | "17/18", "18/19" / 2017, 2018 |  |
| C | 19/20, 20/21 | "19/20", "20/21" / 2019, 2020 | 19/20 COVID 截斷 |
| D | 21/22, 22/23 | "21/22", "22/23" | UECL 21/22 起存在 |
| E | 23/24, 24/25 | "23/24", "24/25" | 24/25 Gen2 已大量覆蓋，`--resume` 會跳過 |

### 2.3 Endpoints（10 個 EVENT_ENDPOINTS，Core + Mid）

| 類別 | Endpoints | 風險等級 | 策略 |
|------|-----------|----------|------|
| Core | `event`, `incidents`, `lineups` | 低 | 全季段啟用 |
| Mid | `statistics`, `shotmap`, `graph`, `average_positions`, `best_players_summary`, `h2h`, `momentum` | 中 | 全季段啟用，flag-gate 生效 |

> **Flag-gate**（Phase 3/Phase A 既定邏輯）：
> - `shotmap` → 需 `hasEventPlayerHeatMap` 為 True 才抓
> - `statistics` → 需 `hasEventPlayerStatistics` 為 True 才抓
> - Flag 為 False → 記錄 `no_data(flag_false)`，不發請求，不寫 DB
>
> **High 風險端點**（odds, comments, votes 等 12 個）**不在 Tranche v2 範圍**，留待 Phase F+。

### 2.4 Events 估計量（全 24 competitions × 10 seasons）

| 類別 | Competition | 平均 events/season | 10 seasons 總計 | Tier |
|------|-------------|-------------------|-----------------|------|
| **Big-5 聯賽** | Premier League | 380 | 3,800 | 1 |
| | La Liga | 380 | 3,800 | 1 |
| | Serie A | 380 | 3,800 | 1 |
| | Bundesliga | 306 | 3,060 | 1 |
| | Ligue 1 | 306 | 3,060 | 1 |
| **亞洲頂級聯賽** | J1 League | 306 | 3,060 | 2 |
| | K League 1 | 198 | 1,980 | 2 |
| | A-League Men | 162 | 1,620 | 2 |
| | Chinese Super League | 240 | 2,400 | 2 |
| **歐洲三大盃** | UCL | 150 | 1,500 | 1 |
| | UEL | 180 | 1,800 | 1 |
| | UECL | 150 (×4 seasons) | 600 | 1 |
| **亞冠兩級** | AFC Champions League | 120 | 1,200 | 2 |
| | AFC Champions League Two | 90 | 900 | 3 |
| **英格蘭盃賽** | FA Cup | 130 | 1,300 | 1 |
| | EFL Cup | 90 | 900 | 3 |
| **歐洲其他盃賽** | Copa del Rey | 110 | 1,100 | 2 |
| | Coppa Italia | 80 | 800 | 3 |
| | Coupe de France | 120 | 1,200 | 2 |
| | DFB Pokal | 64 | 640 | 3 |
| **亞洲盃賽** | J.League Cup | 60 | 600 | 3 |
| | Emperor's Cup | 80 | 800 | 3 |
| | Australia Cup | 32 | 320 | 3 |
| | Chinese FA Cup | 64 | 640 | 3 |
| **總計** | **24 competitions** | | **~41,000 events** | |

> 實際 `--ids-only` 列舉會在各 Phase 開始前產出精確數字。上表為結構性估計（聯賽按制式，盃賽按歷年賽制），參考 `gen4_older_seasons_tranche_plan_v2.md §3`。

---

## 3. Call Budget（HTTP 呼叫預算）

### 3.1 單 Event × Endpoint 預算

| Endpoint | 基礎呼叫 | Retry 預算 (max) | 實際平均呼叫 (Stage 2 經驗) |
|----------|----------|------------------|----------------------------|
| event (root) | 1 | 3 | 1.05 |
| incidents | 1 | 3 | 1.15 |
| lineups | 1 | 3 | 1.10 |
| statistics | 1 | 3 | 1.10 |
| shotmap | 1 | 3 | 1.10 |
| graph | 1 | 2 | 1.05 |
| average_positions | 1 | 2 | 1.05 |
| best_players_summary | 1 | 2 | 1.05 |
| h2h | 1 | 2 | 1.05 |
| momentum | 1 | 2 | 1.05 |

**單 Event 理論上限**：10 endpoints × (1 + 3) = **40 calls**（極端全 retry）  
**單 Event 實際平均**：~6–8 calls（含 flag-gate 跳過、404 即時返回、盃賽弱端點高 404 率）

### 3.2 分 Phase 總 Call Cap（估計）

| 階段 | Seasons | 估計 Events | 理論上限 (×40) | 實際預估 (×6.5) |
|------|---------|-------------|----------------|-----------------|
| Phase A (15/16, 16/17) | 2 | ~8,200 | 328k | ~53k |
| Phase B (17/18, 18/19) | 2 | ~8,200 | 328k | ~53k |
| Phase C (19/20, 20/21) | 2 | ~8,000 | 320k | ~52k |
| Phase D (21/22, 22/23) | 2 | ~8,500 | 340k | ~55k |
| Phase E (23/24, 24/25) | 2 | ~8,300 | 332k | ~54k |
| **全 Tranche v2** | **10** | **~41,200** | **~1.65M** | **~267k** |

### 3.3 vs Phase 8 Ceiling 對照

| 指標 | Phase 8 (Canary) | Tranche v2 (Phase A) | Tranche v2 (全程) |
|------|------------------|----------------------|-------------------|
| 總 calls | ~3,500 (10 events × ~35 endpoints) | ~53k | ~267k |
| 並發度 | 序列 | 序列 (單進程) | 序列 (單進程) |
| Pool 大小 | 21 IPs | 21 IPs (動態補充) | 21 IPs (動態補充) |
| Call/IP/小時 | ~175 | ~1,500 | ~1,500 |

**結論**：單 IP 負載仍遠低於 Webshare 配額上限（~1.5 req/s/IP ≈ 5,400 req/h），pool 容量充裕。真正瓶頸在 **SofaScore 端的 403 reputation**，故健康評分 + 隔離機制至關重要。

---

## 4. Pool 策略

### 4.1 種子池

- **來源**: `data/proxy_pools/good_v2_preflight_result.txt`（21 IPs，各 3/3 probe PASS，2026-09-11 完成）
- **格式**: `ip:port:user:pw`，cred 內嵌，不讀 .env
- **補充池**: `data/webshare_proxy_pool.txt` (100 raw) 減去 `in-audit` + `BURNED`（`good_phaseA_20260909_BURNED.txt`）

### 4.2 Health-Score Selection 行為

參考 `ip_health_score.py`，核心參數：

| 參數 | 值 | 說明 |
|------|-----|------|
| `SUCCESS_DEC` | 1 | 成功 -1 分 |
| `REJECT_403_PENALTY` | 50 | 403 reputation reject +50 分 |
| `REST_RECOVERY` | 20 | 休息 ≥30 分鐘 -20 分 |
| `REST_MIN_S` | 1800 | 30 分鐘 |
| `QUARANTINE_THRESHOLD` | 100 | 分數 ≥100 隔離 |
| `CONSEC_403_OVERRIDE` | 2 | **連續 2 次 403 即時隔離**，不待分數 |
| `QUARANTINE_MIN_COOLDOWN_S` | 21600 | 隔離後最少 6h 冷卻才能重新評分 |
| `FLOOR` | -1e9 | 分數地板防止負漂移 |

**選擇邏輯** (`pick_ip`)：
1. 遍歷 pool 依序，回傳第一個 `is_available(ip)` 為 True 的 IP
2. 全部隔離時，挑 `quarantine_until` 最早的那個（避免 pipeline 卡死，由 caller 負責 abort policy）

**審計軌跡**：每次 outcome 寫入 `IP_HEALTH_AUDIT_DIR/ip_health.jsonl`，含 `ts, ip, outcome, score, consec_403, quarantined, cooling_down, quarantine_until_utc, path, extra`。

### 4.3 403 隔離 / 補充邏輯（Live Run 觸發點）

| 觸發條件 | 行為 |
|----------|------|
| 單 IP 連續 2 次 403 | **即時隔離**，`quarantine_until = now + 6h`，記錄 audit |
| IP 分數 ≥ 100 | 隔離，同上 |
| 隔離 IP 數 ≥ 15/21 (≈70%) | **全局 early-stop**：觸發 `GLOBAL_ABORT_STREAK=30` 邏輯，runner 會在 30 連續 infra fail 時自動停機 |
| 補充需求 | 從 `replenish_pool` 取下一個候選，跑 3-probe preflight（間隔 5–10 min），全 PASS 才入池 |

> **關鍵差異**：Phase A/Stage 3 用死板 round-robin；Tranche v2 引入 `ip_health_score`，403 不再等 burst count，改用 **event-based override**（連續 2 次 403 即隔離），大幅降低浪費在燒壞 IP 上的重試。

---

## 5. Throughput / 預計時長

### 5.1 基準數據

| 階段 | 實測吞吐 | 環境 |
|------|----------|------|
| Stage 2 | **37.8 events/min** (4,799 events / 7,611s) | curl_cffi + 61-IP pool, Gen2 |
| Stage 3 | ~30 events/min (較保守) | curl_cffi + 21-IP pool, Gen2 |
| Phase A (預估) | **~30 events/min** | curl_cffi + 21-IP pool, **Gen4 wired** |

Gen4 wired stack 多了 warm-up + cookie 重用，單 event 開銷略增（~+1–2s），但重試成功率預期提升（Phase 9 驗證 retry loop live 有效）。

### 5.2 分 Phase 預計（41k events 總量）

| Phase | Seasons | 估計 Events | 純跑時間 (30 ev/min) | 加上 overhead (enumeration, warm-up, retries) | 單晚 2h 批次數 |
|-------|---------|-------------|----------------------|---------------------------------------------|----------------|
| A | 2 | ~8,200 | 4.6h | ~6.5h | 3–4 |
| B | 2 | ~8,200 | 4.6h | ~6.5h | 3–4 |
| C | 2 | ~8,000 | 4.4h | ~6h | 3 |
| D | 2 | ~8,500 | 4.7h | ~7h | 4 |
| E | 2 | ~8,300 | 4.6h | ~6.5h | 3–4 |
| **總計** | **10** | **~41,200** | **~22.9h** | **~33h** | **~17 晚** |

> 實際可並行多 competition（不同 ut_id 用不同 sticky session），但為簡化監控與配額控制，建議 **單進程序列跑**，每晚 1–2 個 competition。

---

## 6. Gates / 停止條件

### 6.1 Phase 級 Gate（每階段開始/結束）

| Gate | 時機 | 條件 | 不通過後果 |
|------|------|------|------------|
| **G-A** | Phase 開始前 | 1. Pool freshness < 24h (re-audit PASS)  <br>2. fp-v2 20-call fast canary PASS_STRICT <br>3. `pytest tests/test_gen4_phase8_1_integration.py` 15/15 PASS | 延後 Phase，重新審計 pool |
| **G-B** | Phase 開始時 | 1 sample event × statistics/shotmap 200 on 2–3 comps of the phase（新增 per-season availability probe） | 延後 Phase，調查 endpoint 可用性 |
| **G-C** | Phase 結束 | 1. Tranche summary JSON 產出 <br>2. evidence JSONL 完整 <br>3. Main Agent sign-off | 不進入下一 Phase |
| **G-D** | 任何時候 | **Global kill-switch**: 30 連續 infra fails（已在 runner 內建） | 即時 halt，寫 artifact |

### 6.2 Per-Competition Early-Stop（更新後的 Tier Mapping）

| Tier | Competitions | Retries | Early-stop (連續 infra fail) |
|------|--------------|---------|------------------------------|
| 1 | Premier League, La Liga, Serie A, Bundesliga, Ligue 1, UCL, UEL, UECL, FA Cup, J1 League, A-League Men, AFC Champions League | 3 | 10 |
| 2 | K League 1, Chinese Super League, Copa del Rey, Coupe de France | 2 | 12 |
| 3 | AFC Champions League Two, EFL Cup, Coppa Italia, DFB Pokal, J.League Cup, Emperor's Cup, Australia Cup, Chinese FA Cup | 1 | 15 |

> Tier 規則：Tier 1 = 歐洲/亞洲頂級聯賽 + 歐洲三大盃 + 英格蘭兩大盃；Tier 2 = 亞洲次級聯賽 + 歐洲主要國內盃；Tier 3 = 亞冠二級 + 次級國內盃。代碼在 `gen4_tranche_v2_backfill.py` 會體現此 mapping。

### 6.3 Mid-Run Checkpoint

- 每 **100 events** 輸出進度：`ok=X fail=Y no_data_only=Z`
- `evidence.jsonl` 即時 append，格式見 §7
- IP health snapshot 每 50 events dump 一次到 `logs/ip_health_snapshot_<ts>.json`

### 6.4 暫停 / 恢復機制

| 動作 | 實現方式 |
|------|----------|
| 暫停 | `Ctrl+C` → runner 捕捉 `SIGINT`，完成當前 event commit 後乾淨退出 |
| 恢復 | 重跑同樣命令加 `--resume`：讀 `matches` 表已有 `match_id` 跳過 |
| 強制中止 | `SIGTERM` / kill -9 → 下次 `--resume` 會從最後 commit 的 event 繼續（idempotent writers） |

---

## 7. 監察點

### 7.1 Log Paths

| 檔案 | 內容 | 更新頻率 |
|------|------|----------|
| `/tmp/gen4_tranche_v2/evidence_<phase>.jsonl` | 每 event 一行：`{competition, event_id, event_status, flags, steps: {ep: {status, rows, error}}}` | 即時 append |
| `data/gen4_tranche_v2_report_<phase>.json` | Phase 級總結：before/after row counts, per_comp, per_ip, duration | Phase 結束 |
| `logs/ip_health_snapshot_<ts>.json` | `ip_health_score.snapshot()` 全量輸出 | 每 50 events |
| `data/proxy_pools/ip_health_audit_<phase>.jsonl` | `record_outcome` 每次決策 | 即時 |

### 7.2 evidence.jsonl 格式範例

```json
{"competition":"Premier League 15/16","event_id":12345678,"event_status":"ok","flags":{"hasXg":false,"hasEventPlayerStatistics":true,"hasEventPlayerHeatMap":true},"steps":{"event":{"status":"ok","http":200},"incidents":{"status":"ok","rows":45},"lineups":{"status":"ok","rows":22},"statistics":{"status":"ok","rows":180},"shotmap":{"status":"ok","rows":28},"graph":{"status":"no_data","reason":"http_404"},"average_positions":{"status":"no_data","reason":"flag_false"},"best_players_summary":{"status":"ok","rows":2},"h2h":{"status":"ok","rows":1},"momentum":{"status":"no_data","reason":"http_404"}},"ok":true,"no_data_only":false}
```

### 7.3 Smoke 檢查頻率

- **Phase 開始前**: G-A + G-B（見 §6.1）
- **Run 中**: 每 100 events 人工抽查 1 個 event 的 evidence 行 + IP health snapshot
- **Phase 結束**: 完整讀取 report JSON + 抽查 5 events 的 DB 寫入一致性

---

## 8. 回滾 / 中斷方案

### 8.1 資料一致性保證

| 層面 | 機制 |
|------|------|
| **寫入冪等** | 所有 writers 用 `INSERT ... ON DUPLICATE KEY UPDATE`；同一 `match_id` 重跑不會產生重複行 |
| **事務邊界** | `process_event` 內：root event `insert_match` → commit；各 endpoint writer 各自 commit；失敗時 `conn.rollback()` 僅回滾當前 event 的未完成步驟 |
| **無空行** | 404 / flag-false → `no_data`，**不寫入任何行**（連空 row 都不生成） |
| **Checkpoint** | `--resume` 讀 `SELECT match_id FROM matches` 跳過已完成 events |

### 8.2 中斷流程

```
Ctrl+C (SIGINT)
    ↓
Runner 完成當前 event 的所有步驟（含 commit）
    ↓
印出進度摘要：processed=N, ok=X, fail=Y, last_event=EID
    ↓
退出碼 130
    ↓
恢復：相同命令 + --resume → 從 EID+1 繼續
```

### 8.3 災難回滾（極端情況）

若需**物理刪除**已寫入的 Phase 數據（極少見，需 Kris 批准）：
```sql
-- 僅刪除特定 competition + season 的資料
DELETE m, i, l, s, sh, g, o, c
FROM matches m
LEFT JOIN match_incidents i ON m.match_id = i.match_id
LEFT JOIN match_lineups l ON m.match_id = l.match_id
LEFT JOIN match_statistics s ON m.match_id = s.match_id
LEFT JOIN match_shotmap sh ON m.match_id = sh.match_id
LEFT JOIN match_graph_points g ON m.match_id = g.match_id
LEFT JOIN match_odds o ON m.match_id = o.match_id
LEFT JOIN match_comments c ON m.match_id = c.match_id
WHERE m.competition_id = ? AND m.season_id = ?;
```
> **警告**：此操作不可逆，需 Kris 明確授權 + Main Agent 執行。

---

## 9. DB 寫入行為與 Stage 4d Item 1 (nested-dict refactor)

### 9.1 新 Endpoint 有冇？

Tranche v2 **不新增資料表**，僅填充既有表：
- `match_incidents` ← incidents
- `match_lineups` ← lineups
- `match_statistics` ← statistics
- `match_shotmap` ← shotmap
- `match_graph_points` ← graph
- `match_average_positions` ← average_positions（新表，Stage 4d 建立）
- `match_best_players` ← best_players_summary（新表，Stage 4d 建立）
- `match_h2h` ← h2h（新表，Stage 4d 建立）
- `match_momentum` ← momentum（新表，Stage 4d 建立）

### 9.2 Stage 4d Item 1 狀態

> **現狀**：`backfill_runner.py` 第 1-50 行已有 `_stat_value()` 針對 nested dict/list 做 **標量化轉換**（dict → 取 `statisticsType` 或 JSON string；list → JSON string），**但僅用於 `insert_player_stats` 路徑**。  
> **Tranche v2 衝擊**：
> - `statistics` endpoint payload 內的 `playerStatistics` 陣列會經過 `_stat_value()` 處理，**已兼容 nested 結構**。
> - `shotmap` / `graph` / `h2h` / `average_positions` / `momentum` / `best_players_summary` 的 writer（`DataInserter.insert_*`）**尚未統一套用同樣標量化**，若 API 回傳 nested dict 會噴 `MySQLInterfaceError: Python type dict cannot be converted`。

### 9.3 Tranche v2 的處理策略

| 方案 | 優點 | 缺點 | 決定 |
|------|------|------|------|
| A: Tranche v2 前完成所有 writer 的 nested-dict refactor | 徹底解決，無 runtime 驚喜 | 需額外 1–2 天開發 + 測試，**阻塞 Tranche v2 啟動** | ❌ 不採用（阻塞太久） |
| B: **Run-time guard**：在 `DataInserter.insert_*` 入口統一加一層 `json.dumps(v, separators=(',', ':'))` fallback，遇 dict/list 自動序列化 | 0 代碼改動風險，即時生效，向後兼容 | 少數欄位會存 JSON string 而非標量，分析端需知悉 | ✅ **採用**（風險最小） |
| C: 僅針對已知會噴錯的 endpoint（shotmap, graph, h2h 等）加 try-catch + json.dumps | 精準修補 | 遺漏未知欄位風險 | 方案 B 的子集 |

**具體實作**（在 Tranche v2 Phase A 啟動前，由 Forge 完成，不需 Kris 額外批准）：
```python
# 在 backfill_runner.py DataInserter 每個 insert_* 方法開頭加入：
def _coerce_scalar(v):
    if isinstance(v, dict):
        return v.get("statisticsType") or json.dumps(v, separators=(",", ":"))
    if isinstance(v, list):
        return json.dumps(v, separators=(",", ":"))
    return v

# 對所有從 API payload 取出的值，寫入前統一跑一遍 _coerce_scalar()
```

> 此改動 **不改 schema、不改表結構、不遷移數據**，純屬寫入路徑防禦性修正，屬於「代碼級 bugfix」而非「schema 變更」。

---

## 10. 執行 Checklist（逐格剔，Kris 批准後才能執行）

### Phase A 啟動前
- [ ] Kris `/approve Tranche v2 Phase A` 到手（經 Duncan 轉達）
- [ ] Pool freshness < 24h：`python preflight_pool_audit.py --resume` 確認 21 GOOD
- [ ] fp-v2 20-call canary PASS_STRICT：`python gen4_preprod_smoke_21ip.py`
- [ ] Offline regression gate：`pytest tests/test_gen4_phase8_1_integration.py` 15/15 PASS
- [ ] Nested-dict guard 部署：`backfill_runner.py` 加入 `_coerce_scalar()` fallback
- [ ] `--ids-only` 列舉 Phase A events → `data/phaseA_event_ids.json` 確認總數
- [ ] G-B probe：2–3 comps × statistics/shotmap 200 PASS

### Phase A 執行中
- [ ] 單進程序列跑：`python gen4_tranche_v2_backfill.py --resume`（新建 runner 繼承 Phase A 邏輯，支援 24 comps）
- [ ] 每 100 events：人工抽查 evidence.jsonl + IP health snapshot
- [ ] 任何 early-stop / global abort → 即時 artifact + sessions_send Main Agent

### Phase A 結束
- [ ] Report JSON 產出：`data/gen4_tranche_v2_report_A.json`
- [ ] Evidence JSONL 完整性檢查（行數 = processed events）
- [ ] DB row count delta 核對（抽樣 5 events 驗證所有 10 endpoints）
- [ ] Main Agent sign-off → 進入 Phase B

### Phase B–E 重複上述流程

---

## 11. 風險登記

| 風險 | 等級 | 應對 |
|------|------|------|
| 403 reputation wave 導致 pool 快速耗盡 | 高 | Health-score + 連續 2×403 即隔離 + 6h 冷卻 + 自動補充；G-D kill-switch 兜底 |
| 盃賽弱端點 404 率高導致覆蓋率低 | 中 | skip-on-404 policy 已處理；只影響覆蓋率不影響成功率；Tier 3 早停閾值較寬 |
| Pre-2020 xG/shotmap 欄位全 NULL | 中 | 屬上游資料缺失，**非 pipeline bug**；consumer 端需知悉，文檔標註 |
| UECL 21/22 前不存在 | 低 | Phase D 起自動跳過（resolver 回傳 None） |
| Stage 4d nested-dict 導致寫入噴錯 | 中 | **Run-time guard (方案 B) 上線前必裝**；若仍有遺漏，evidence 記錄 `fail`，下一 Phase 修補 |
| Gen4 wired stack 某 endpoint 系統性失敗 | 中 | Phase 9 已驗證 retry loop + cookie reuse + CloakBrowser fallback live 有效；仍失敗則 G-D halt |
| 事件總量 ~41k 導致單 Phase 時間拉長 | 低 | 每 Phase 增至 3–4 晚；若需加速可並行多 comp（不同 ut_id/sticky session） |

---

## 12. 請求 Kris 批准事項

1. **批准 Phase A 啟動**（15/16 + 16/17，24 comps，~8,200 events）
2. **確認順序**：最舊優先（A→B→C→D→E）vs 最新優先（E→D→C→B→A） — 推薦最舊優先
3. **確認執行窗口**：Asia/Taipei 晚上 20:00–23:00（約 2–3h/晚），方便 G-A canary 當日落地
4. **授權 nested-dict guard 部署**（屬代碼級 bugfix，不涉及 schema 變更）

---

## 13. Artifacts & Handoff

| Artifact | Path | 產出時機 |
|----------|------|----------|
| Plan Doc (本檔) | `docs/tranche_v2_rollout_plan.md` | v0.2 現在 |
| Phase A IDs | `data/phaseA_event_ids.json` | Phase A G-A 後 |
| Phase A Report | `data/gen4_tranche_v2_report_A.json` | Phase A 結束 |
| Phase A Evidence | `/tmp/gen4_tranche_v2/evidence_A.jsonl` | 即時 |
| IP Health Audit | `data/proxy_pools/ip_health_audit_A.jsonl` | 即時 |
| Phase B–E 同類 | 對應路徑 | 各 Phase 結束 |
| 最終總結 | `data/gen4_tranche_v2_final_summary.json` | Phase E 結束 |

---

> **硬規則重申**：Kris 未批准前，**禁止任何 live run / 大規模 fetch**。本 plan doc 完成後，Forge 將 artifact JSON 交回 Duncan (Main Agent) 審核，由 Duncan 轉交 Kris 批准。