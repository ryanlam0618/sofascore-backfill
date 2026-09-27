# match_odds UNIQUE Key 分析 — match-odds-unique-key-20260927

**Status**: DRAFT — 等 Kris 批設計 + 揀執行者（DB 目前仍然 READ-ONLY，本分析全程只跑咗 SELECT/SHOW）
**Author**: Forge（agent:coding）· 委託：Main Agent（Duncan）· 2026-09-27
**DDL 檔**: `docs/match_odds_unique_key_ddl.sql`（推薦方案 B：virtual generated hash column + UNIQUE）

---

## 1. 總結（TL;DR）

- **推薦 Option B**：加一個 VIRTUAL generated column `odds_identity_hash`（SHA2-256，涵蓋 dedup 定義嗰 18 欄 identity，NULL-safe），加 `UNIQUE KEY uq_match_odds_identity` 喺個 hash 度。
- **點解唔可以直接用普通 composite UNIQUE**：identity 嗰 18 欄入面，**6 欄而家 100% NULL**（bookmaker_id、bookmaker_name、home_odds、draw_odds、away_odds、fetched_at），odds_type 仲有 55% NULL。**MySQL 對任何 key 欄係 NULL 嘅 row 完全唔 enforce uniqueness** — 即係普通 composite key 對真真正正積累 dup 嗰批 row（全 NULL 派）零保護。呢個唔係 edge case，係成張表。
- **可行性已驗證（live SELECT）**：18-col identity dup groups = **0**；row_count = **335,694** ✓ 同 dedup report 一致。今日行 DDL 唔會撞殘留 dup。
- **Pipeline 相容**：gen4 phaseB 系列（而家嘅 writer）全部係 `INSERT ... ON DUPLICATE KEY UPDATE` → 加 key 之後 exact re-insert 觸發 ON DUP（update 翻同值 = no-op）→ **re-run 由積累 dup 變成 idempotent，根因直接閂掣**；值有變（moved）嘅 row hash 唔同 → 照 append，歷史保留。唯一要留意係 `backfill_runner.py`（plain INSERT）— 建議喺 key 落地前加一行 ON DUP patch（見 §6.2，向後兼容）。
- **權限已驗證**：appdb_rw 對 appdb.* 有 `SELECT, INSERT, UPDATE, DELETE, CREATE, INDEX, ALTER` → **加 column、加/刪 unique index、rollback 全部夠權**，唔使 elevated/root（Main Agent 之前缺 DROP 係 table-level，呢個 DDL 完全唔需要 DROP）。
- **Movement 澄清**：appdb **冇**獨立 odds movement/history 表（24 張表全列出咗，見 §4.1）。12,221 個 moved groups 本身就係 **match_odds 入面跨 run 嘅多 row 歷史**（全部 fetched_at=NULL）。Option B 唔會 merge 佢哋（值唔同 → hash 唔同）→ **完全唔受影響**。

---

## 2. 背景

- 2026-09-27 dedup（odds-dedup-delete-20260927，Kris 批 option a）刪咗 **3,098** 行真 duplicate（v4 單一 transaction；338,792 → 335,694；dup_groups=0；backup `data/odds_dedup_backup_20260927.sql` 3,098 行可還原；report `data/odds_dedup_20260927_report.json`）。
- Dup 定義（Kris 批咗嗰個）：**17 個 data col + fetched_at 全同，只差 odds_id**（= 同批 fetch 重複插入）。
- 根因：**match_odds 冇 PK 以外嘅任何 UNIQUE key**（實測 SHOW CREATE TABLE：只有 `PRIMARY KEY(odds_id)` + `KEY match_id`）→ gen4 phaseB writer 嘅 `ON DUPLICATE KEY UPDATE` 永遠唔觸發 → re-run 每次都 append。

---

## 3. 現況證據（live DB，2026-09-27，appdb_rw read-only session）

**MySQL 8.0.46** · InnoDB · `utf8mb4_0900_ai_ci` · match_odds 現有 key：`PRIMARY KEY(odds_id)`、`KEY match_id(match_id)`、FK → matches。

### 3.1 NULL profile（335,694 行）

| Identity 欄 | NULL 行數 | 佔比 | 實際情況 |
|---|---|---|---|
| match_id … winning（11 個 core 欄） | 0 | 0% | 全部有值 |
| bookmaker_id / bookmaker_name | 335,694 | **100%** | 兩個 writer 家族都冇寫 |
| home_odds / draw_odds / away_odds | 335,694 | **100%** | 同上 |
| fetched_at | 335,694 | **100%** | 同上（column 定義係 `DEFAULT NULL`，唔係 CURRENT_TIMESTAMP） |
| odds_type | 184,989 | **55%** | `1x2`=150,705 / NULL=184,989 / 另兩個 enum 值 0 次 |

### 3.2 Feasibility（加 key 前必查項，已跑，全部符合）

| 檢查 | 結果 | 期望 |
|---|---|---|
| 18-col identity dup groups（= dedup 定義，ai_ci GROUP BY） | **0**（rows_to_delete=0） | 0 ✓ |
| 同上，只計 fetched_at NULL / 非 NULL 子集 | 0 / 0 | 0 ✓ |
| 11-col（core）dup groups | 0 | 0（參考用） |
| row_count | 335,694 | = report ✓ |

---

## 4. Key 設計

### 4.1 Writer 家族盤點（決定 key 語意嘅證據）

| Writer | Insert 形式 | odds_type | fetched_at | 附註 |
|---|---|---|---|---|
| **gen4_phaseB 系列**（17_18_rerun、17_18_rerun_v2、18_19、19_20、2021-25、auscup_gapfill、cup_retry…） | 18-col `INSERT … ON DUPLICATE KEY UPDATE`（UPDATE 只改 fractional_value、winning、home/draw/away、fetched_at） | 顯式 `None` → NULL | 顯式 `None` → NULL | **dup 積累源頭**：ON DUP 死信，re-run 即 append |
| **backfill_runner.py**（舊 runner） | 先 `DELETE FROM match_odds WHERE match_id=%s` 再 11-col plain INSERT | 冇 insert → DEFAULT `'1x2'` | 冇 insert → DEFAULT NULL | per-match wipe → 自家 re-run 唔積累 |
| smoke_test_mysql.py / test_odds*.py | 帶 ON DUP | — | — | 測試 script，有 ON DUP 唔會爆 |
| odds_dedup_*.py | 只讀 | — | — | 分析用 |
| **oddsharvester** | — | — | — | **唔寫 appdb**（src 入面冇 pymysql/mysql connector；只係 Playwright scraper） |

兩個家族加埋正好解釋 odds_type 分佈：150,705（backfill_runner）+ 184,989（gen4）。

### 4.2 候選方案

| | **Option B：hash-18 generated column + UNIQUE（推薦）** | Option A：普通 UNIQUE on 11 個 core 欄 | Option C：普通 UNIQUE 涉及 odds_type 等可空欄 |
|---|---|---|---|
| 擋唔擋到真 dup | ✅ 全部（NULL-safe sentinel） | ⚠️ 今日得（6 個 NULL 欄冇資訊量），但語意唔等於 dedup 定義 | ❌ **NULL row 完全唔受保護**（gen4 派 55–100% NULL）→ 直接否決 |
| 未來 bookmaker / fetched_at 有值之後 | ✅ identity 自動精確，唔使改 key | ❌ 兩個 bookmaker 同一 combo → false conflict → ON DUP 互相 clobber | ❌ |
| 跨 writer 家族（odds_type NULL vs '1x2'） | ✅ 兩行唔同 hash，各自保留 | ❌ 同 11 欄 → false conflict → gen4 行 merge 入 backfill_runner 行 | — |
| Index 大細 | 32-byte binary | ~1,698 bytes（utf8mb4，未爆 3,072 但肥） | — |
| 錯誤訊息可讀性 | ❗ 係 hex（可 `WHERE odds_identity_hash=UNHEX('<hex>')` 反查） | ✅ 人類可讀 | — |
| 對 pipeline 改動 | 零 | 零 | 零 |

**細節**：
- **MySQL NULL 語意**：composite UNIQUE 入面任何一欄係 NULL，嗰行就唔受 uniqueness 約束（NULL≠NULL）。所以揀 key 欄嘅唯一硬準則係「今日永遠非 NULL」→ 只有 11 個 core 欄合資格 → 但 11 欄唔等於 dedup 嘅 18 欄 identity（會 merge 唔應該 merge嘅行），未來又會爆 bookmaker 問題。→ 唯有 hash。
- **Collation nuance**：dedup 嘅 GROUP BY 用 `utf8mb4_0900_ai_ci`（case-insensitive），hash 就係 byte-exact（binary concat）。實際嗰 3,098 個 dup 全部 byte-identical，所以 byte-exact key 覆蓋實際 dup class；兩行淨係大小寫唔同 → hash 唔同 → 各自保留（其實更正確：唔會 silently merge）。0900 係 NO PAD collation，尾空格唔會搞事。
- **NULL sentinel**：`COALESCE(CAST(col AS CHAR), CHAR(30))` + `CONCAT_WS(CHAR(31), …)`。唔可以直接 CONCAT_WS 裸欄 — CONCAT_WS 會 skip NULL（歷史 v2 overcount bug 就係咁嚟），NULL 對 value 會撞 hash。Sofascore JSON 唔會出 control char 0x1E/0x1F，sentinel 撞實值機會 ≈ 0。
- **VIRTUAL vs STORED**：VIRTUAL = 加 column 唔 rebuild、row 唔使多 32 byte；UNIQUE index on virtual column 係 InnoDB 8.0 支援嘅正路組合。INSERT 時照計 hash 做 index 維護，成本可忽略。
- **SHA2-256 collision**：3.4×10⁵ 行規模，birthday-bound collision 機率 ~10⁻⁶⁴ 量級，實務上零。用 BINARY(32) 而唔係 MD5(16) 係保守揀法。

### 4.3 產出後嘅行為（重要 — 證明根因閂掣）

| 場景 | 加 key 前（現狀） | 加 key 後 |
|---|---|---|
| gen4 re-run，market 冇變 | append dup ❌（3,098 就係咁嚟） | hash 相同 → ON DUP 觸發 → update 同值 = **no-op，idempotent** ✅ |
| gen4 re-run，值有變 | append 新 snapshot（moved group 歷史） | hash 唔同 → 照 append ✅（行為不變，歷史保留） |
| backfill_runner re-run | DELETE 全 match 再插，唔積累 | 一樣；**例外**：同一個 payload 內完全相同嘅 market+choice 重複出現 → IntegrityError（見 §6.2 patch） |
| 12,221 moved groups（match_odds 內歷史） | 保留 | **唔受影響**（唔同值 → 唔同 hash，永遠唔 merge） |

---

## 5. Movement 表澄清（回應 task scope ⚠️ 項）

Task 寫「movement rows（12,221 movement groups）喺另一張表」。**實測唔係**：appdb 全部 24 張表（見附錄 A）冇任何 odds movement/history 表；唯一 odds 表就係 match_odds。12,221 個 moved groups 係 dedup report 統計喺 match_odds 入面嘅跨 run 多 row 歷史（全部 fetched_at=NULL、值隨 re-run 而變）。結論一樣甚至更強：**Option B 只對 byte-identical 行起約束，moved groups 值唔同 → hash 唔同 → 完全唔受影響**；同埋 key 冇 FK / 冇 trigger 拖累其他表。

---

## 6. Pipeline 相容性（scope 項 2 詳解）

### 6.1 gen4 phaseB 家族 — ✅ 唔 break，根因閂掣
全部係 `INSERT … ON DUPLICATE KEY UPDATE fractional_value=…, winning=…, home/draw/away_odds=…, fetched_at=…`。加 key 後 conflict 只會喺 18 欄全同（=真 dup）時發生 → ON DUP 執行 → 寫翻同值 → no-op。**唔會有 duplicate key error**（ON DUP 唔會 throw）。呢個正正係 writer 作者當年寫 ON DUP 嘅本意 — 佢哋一直以為有 unique key（lineups 就有 `(match_id, team_id, player_id)` key，gen4_lineups_replay.py 註明「relies on the unique key」）— match_odds 係漏咗嗰張表。

### 6.2 backfill_runner.py — ⚠️ 建議先落一個一行 patch
`_insert_odds_once` 係 per-match DELETE + plain INSERT（冇 ON DUP）。佢自家 re-run 冇問題（wipe 咗先），但**同一個 run 入面 payload 重複出現完全相同嘅 market+choice**（Sofascore 有時會）→ 而家係無聲 dup（今次 dedup 刪嘅 group size 2/3/4/10 有部分可能就係咁）→ 加 key 後變 IntegrityError → `_retry_deadlock` 只 retry 1213/1205 deadlock，IntegrityError 會向上拋 → 該 match 嘅 odds step fail。

**Patch（建議 key 落地前先行，向後完全兼容冇 key 狀態）**：
```sql
-- backfill_runner.py _insert_odds_once 嘅 INSERT 尾加：
ON DUPLICATE KEY UPDATE fractional_value=VALUES(fractional_value), winning=VALUES(winning)
```
冇 key 時呢句係 no-op（行為 100% 不變）→ 可以安全先 commit 先行；有 key 時變 idempotent。改動 1 行，blast radius 極細。**呢個 code change 要 Kris 另批**（佢係 protected-adjacent 嘅 runner 檔），我冇郁任何 code。

### 6.3 其他
- smoke/test script：全部有 ON DUP ✓
- odds_dedup_*.py：只讀 ✓
- oddsharvester：唔寫 MySQL ✓（grep src 冇 connector）
- mysqldump / 還原：8.0 對 generated column 支援完整；**注意**：如果將來用 `odds_dedup_backup_20260927.sql` 還原 3,098 行，會即刻撞 key（佢哋全部係 duplicate identity）— 還原前要先 DROP 個 unique key 或者只還原非重複子集。已喺 DDL 檔案註明。

---

## 7. 權限 / 執行路徑（scope 項）

- 實測 `SHOW GRANTS FOR CURRENT_USER()`（appdb_rw）：`GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, INDEX, ALTER ON appdb.*`。
- Step A（ADD COLUMN）、Step C（ADD UNIQUE INDEX）、Step R rollback（DROP INDEX + DROP COLUMN）全部只需 ALTER + INDEX → **appdb_rw 一個就夠**，唔使 Kris host-side root，唔使 elevated。Main Agent 之前缺 DROP 嘅前科（`_fpv2_temp_verify`）係 DROP TABLE 先中招 — 呢個 DDL 由頭到尾唔需要 DROP table。
- **建議執行者**：Main Agent 用 appdb_rw 直接行 DDL 檔三步（每步有 check）；或者 Kris 想自己 host-side 行都得，SQL 完全一樣。**呢個等 Kris 揀。**
- 約束重申：我而家全程 READ-ONLY，冇行過任何 DDL — DDL 檔係 draft，等批。

## 8. 風險

| 風險 | 等級 | 緩解 |
|---|---|---|
| backfill_runner 同 run 內重複 payload → IntegrityError | 低（但後果係 step fail） | §6.2 一行 patch，先 patch 後落 key |
| DDL 進行中 writer 插入 dup | 低 | ALTER transactional；Step C 失敗會乾淨 abort；建議 off-peak 行 |
| hash collision | ≈0 | SHA2-256 @ 3.4×10⁵ 行 |
| 未來還原 dedup backup 撞 key | 純文檔問題 | DDL 檔已註明還原前要 drop key |
| ON DUP 嘅 `VALUES()` 語法喺 8.0.20+ 有 deprecation warning | cosmetic | 冇功能影響；將來 gen4 代碼清理時換 `AS ... SELECT` 寫法 |

## 9. Rollout 順序（建議）

1. Kris 批本設計（Option B）＋ 揀執行者（appdb_rw 經 Main Agent / host-side root）
2. （可選但建議）批 backfill_runner 一行 ON DUP patch 並先行（與否都唔影響 DDL）
3. `docs/match_odds_unique_key_ddl.sql` Step A（加 hash column）
4. Step B check = 0（byte-exact）
5. Step C（加 unique index）
6. Post-checks + canary（期望 1062）→ 回報
7. 監察：之後 gen4 re-run 嘅 dup_groups 應該恆等於 0；`SELECT COUNT(*) FROM (SELECT odds_identity_hash, COUNT(*) c FROM match_odds GROUP BY 1 HAVING c>1) t` 做日常 guard
8. 如出事：Step R rollback（兩句 ALTER，秒級）

## 10. Open questions（要 Kris 定）

1. 批 Option B 設計？（或者想揀 Option A despite 佢嘅 false-merge 風險 — 我強烈唔建議）
2. 執行者：Main Agent（appdb_rw）定 Kris host-side？
3. backfill_runner 嘅一行 ON DUP patch：同 key 一齊批，定之後再算？

---

## 附錄 A — appdb 全部 24 張表（2026-09-27 實測）

`_duncan_verify`, `_fpv2_temp_verify`, `competitions`, `countries`, `fetch_log`, `match_average_positions`, `match_best_players`, `match_h2h`, `match_incidents`, `match_lineups`, `match_lineups_pre_orphan_cleanup_20260901`, `match_momentum`, `match_odds`, `match_player_stats`, `match_shotmap`, `match_statistics`, `match_votes`, `matches`, `player_season_stats`, `players`, `seasons`, `standings`, `team_rankings`, `teams`

→ 冇任何 odds movement / history 表。

## 附錄 B — 主要查證命令（全部 read-only）

- `SHOW CREATE TABLE match_odds`（schema + 現有 keys）
- `SELECT COUNT(*) / NULL profile / odds_type breakdown / 18-col & 11-col dup-group counts`（見 §3.1–3.2）
- `SHOW GRANTS FOR CURRENT_USER()`（§7）
- `SHOW TABLES`（附錄 A）
- 對照物：`data/odds_dedup_20260927_report.json`（Kris 批嘅 dedup 定義同數字）、`data/odds_dedup_backup_20260927.sql`（3,098 行 backup，實測 3,098 條 INSERT、零危險語句）
