# Lineups Deep Stats Persist — Plan Doc

> **task_id**: `lineups-deep-stats-persist-20260901`
> **agent**: coding (Forge)
> **date**: 2026-09-01 05:45 GMT+8
> **status**: DRAFT — awaiting Kris explicit approval before ALTER TABLE
> **decision**: Kris 2026-09-01 05:33 GMT+8 — Option (a): `ALTER TABLE match_lineups` add ~30 columns + rewrite parser to persist full 40-field `statistics` object.

---

## 0. TL;DR

玩家深度數據（xG/xA、topSpeed、tackles、interceptions、ratingVersions、sprints、progressive carries、normalized value scores 等）**全部已經嵌入喺 `/event/{id}/lineups` payload**，唔需要新 endpoint。但 Stage 2 嘅 parser 只寫咗 11 個核心 lineups column，deep stats 被丟棄。

**關鍵結論：`evidence.jsonl` 冇存 raw payload，所以必須 re-fetch 1000 場嘅 `/lineups`。**

---

## 1. Evidence file payload 狀態

**結論：raw payload 冇 save 到。**

- `/tmp/gen4_stage2/evidence.jsonl`：1000 行，每行結構為：
  ```json
  {"competition": "...", "event_id": 14023948,
   "steps": {"event": {"ok": true, "ip": "..."}, "lineups": {...}, "statistics": {...}, "incidents": {...}},
   "ok": true}
  ```
- 每行只記錄 **endpoint status + 出口 IP**，**冇 raw lineups/statistics JSON body**。
- Stage 2 嘅 `insert_match_lineups` 用完 payload 即刻丟棄，冇落 disk。
- `/tmp/gen4_stage2/` 只有 `evidence.jsonl` (268KB) 同 `run.log`，冇其他 archive dir。

**→ 必須 re-fetch `/lineups`（1000 個 event_id）。** event_id list 可從 `evidence.jsonl` 100% 提取，唔使重新 enumerate。

---

## 2. ALTER TABLE — exact column list

### 2.1 現有 `match_lineups`（11 columns）

| column | type |
|---|---|
| lineup_id | bigint (PK auto) |
| match_id | bigint |
| team_id | int |
| player_id | bigint |
| is_home | tinyint(1) |
| is_starter | tinyint(1) |
| jersey_number | int |
| position | varchar(50) |
| position_category | enum('GK','DEF','MID','FWD') |
| is_captain | tinyint(1) |
| minutes_played | int |
| rating | decimal(3,1) |

### 2.2 現有 `match_player_stats`（92 columns — 已存在，但 Kris 揀 option (a) 唔用佢）

重要提示：另一張表 `match_player_stats` **已經有齊呢啲 deep stats columns**（含 `total_pass`、`accurate_pass`、`expected_goals`、`top_speed`、`rating_versions`、`raw_statistics` 等 92 列）。Kris 明確揀 **option (a)：直接加落 `match_lineups`**，**唔開新 table、唔用 `match_player_stats`**。本 plan 完全遵從。

### 2.3 新增 column list（Live verified 2026-09-01）

以下係 actual `/lineups` payload 嘅 `statistics` object **全 union（72 個 field）**，經 live fetch `event/14023948/lineups` 驗證。Kris 話「~30 個 + 40-field statistics object」——實際 SofaScore payload 比 40 更厚（含 GK + outfield 雙位置全量）。

建議：**唔好假設 40 就夠，寧願一次過加齊 72 個 union，跟足 live schema**，避免第二次 ALTER。

分類及建議 column + type：

#### A. 射門 / 入球（10）
| column | JSON key | type |
|---|---|---|
| goals | goals | int |
| total_shots | totalShots | decimal(14,4) |
| on_target_scoring_attempt | onTargetScoringAttempt | decimal(14,4) |
| shot_off_target | shotOffTarget | decimal(14,4) |
| blocked_scoring_attempt | blockedScoringAttempt | decimal(14,4) |
| hit_woodwork | hitWoodwork | decimal(14,4) |
| expected_goals | expectedGoals | decimal(14,4) |
| expected_goals_on_target | expectedGoalsOnTarget | decimal(14,4) |
| shot_value_normalized | shotValueNormalized | decimal(14,4) |
| big_chance_missed | bigChanceMissed | decimal(14,4) |

#### B. 傳球（10）
| column | JSON key | type |
|---|---|---|
| total_pass | totalPass | decimal(14,4) |
| accurate_pass | accuratePass | decimal(14,4) |
| total_long_balls | totalLongBalls | decimal(14,4) |
| accurate_long_balls | accurateLongBalls | decimal(14,4) |
| total_own_half_passes | totalOwnHalfPasses | decimal(14,4) |
| accurate_own_half_passes | accurateOwnHalfPasses | decimal(14,4) |
| total_opposition_half_passes | totalOppositionHalfPasses | decimal(14,4) |
| accurate_opposition_half_passes | accurateOppositionHalfPasses | decimal(14,4) |
| total_cross | totalCross | decimal(14,4) |
| accurate_cross | accurateCross | decimal(14,4) |

#### C. 創造機會 / 助攻（7）
| column | JSON key | type |
|---|---|---|
| key_pass | keyPass | decimal(14,4) |
| goal_assist | goalAssist | decimal(14,4) |
| big_chance_created | bigChanceCreated | decimal(14,4) |
| expected_assists | expectedAssists | decimal(14,4) |
| pass_value_normalized | passValueNormalized | decimal(14,4) |
| touches | touches | decimal(14,4) |
| dispossessed | dispossessed | decimal(14,4) |

#### D. 對抗 / 盤帶（10）
| column | JSON key | type |
|---|---|---|
| duel_won | duelWon | decimal(14,4) |
| duel_lost | duelLost | decimal(14,4) |
| aerial_won | aerialWon | decimal(14,4) |
| aerial_lost | aerialLost | decimal(14,4) |
| total_contest | totalContest | decimal(14,4) |
| won_contest | wonContest | decimal(14,4) |
| challenge_lost | challengeLost | decimal(14,4) |
| dribble_value_normalized | dribbleValueNormalized | decimal(14,4) |
| unsuccessful_touch | unsuccessfulTouch | decimal(14,4) |
| was_fouled | wasFouled | decimal(14,4) |

#### E. 防守（11）
| column | JSON key | type |
|---|---|---|
| total_tackle | totalTackle | decimal(14,4) |
| won_tackle | wonTackle | decimal(14,4) |
| interception_won | interceptionWon | decimal(14,4) |
| total_clearance | totalClearance | decimal(14,4) |
| clearance_off_line | clearanceOffLine | decimal(14,4) |
| ball_recovery | ballRecovery | decimal(14,4) |
| outfielder_block | outfielderBlock | decimal(14,4) |
| last_man_tackle | lastManTackle | decimal(14,4) |
| error_lead_to_a_shot | errorLeadToAShot | decimal(14,4) |
| fouls | fouls | decimal(14,4) |
| total_offside | totalOffside | decimal(14,4) |

#### F. 門將（10）
| column | JSON key | type |
|---|---|---|
| saves | saves | decimal(14,4) |
| saved_shots_from_inside_the_box | savedShotsFromInsideTheBox | decimal(14,4) |
| good_high_claim | goodHighClaim | decimal(14,4) |
| goals_prevented | goalsPrevented | decimal(14,4) |
| keeper_save_value | keeperSaveValue | decimal(14,4) |
| goalkeeper_value_normalized | goalkeeperValueNormalized | decimal(14,4) |

（註：`total_keeper_sweeper`、`accurate_keeper_sweeper` 喺 legacy mapping 有，但 live payload 冇 capture 到，可選加。）

#### G. 體能 / 速度（7）
| column | JSON key | type |
|---|---|---|
| top_speed | topSpeed | decimal(14,4) |
| kilometers_covered | kilometersCovered | decimal(14,4) |
| number_of_sprints | numberOfSprints | decimal(14,4) |
| meters_covered_running_km | metersCoveredRunningKm | decimal(14,4) |
| meters_covered_high_speed_running_km | metersCoveredHighSpeedRunningKm | decimal(14,4) |
| meters_covered_sprinting_km | metersCoveredSprintingKm | decimal(14,4) |
| ball_carries_count | ballCarriesCount | decimal(14,4) |

#### H. Progressive carries（4）
| column | JSON key | type |
|---|---|---|
| total_ball_carries_distance | totalBallCarriesDistance | decimal(14,4) |
| total_progression | totalProgression | decimal(14,4) |
| progressive_ball_carries_count | progressiveBallCarriesCount | decimal(14,4) |
| total_progressive_ball_carries_distance | totalProgressiveBallCarriesDistance | decimal(14,4) |
| best_ball_carry_progression | bestBallCarryProgression | decimal(14,4) |

#### I. 評分 / 版本（2）
| column | JSON key | type |
|---|---|---|
| statistics_type | statisticsType | varchar(50) |
| rating_versions | ratingVersions | json |

#### J. 防守價值（重複？）— 注意
`defensive_value_normalized` 同 `pass_value_normalized` 已列；`defensive_value_normalized` 補上：

| column | JSON key | type |
|---|---|---|
| defensive_value_normalized | defensiveValueNormalized | decimal(14,4) |

**合計新增 ~60 columns**（72 union 減去既有多餘重複）。實際以 live payload union 為準，寧多勿少。

> ⚠️ 建議**開 new table 前先同 Kris 確認 column count 預期**：Kris 預期「~30」但我 live 驗證到 72 個 field union。我會喺 plan 交畀 Kris 時標明「實際 payload 72 field，建議一次齊，定係只加你心裡嗰 30 個？」

---

## 3. Parser rewrite diff

Target: `gen4_stage2_backfill.py::insert_match_lineups`（唔郁 protected `backfill_runner.py`）。

### 現況（Stage 2 版本）
```python
stat = p.get("statistics") or {}
# 只 extract minutesPlayed + rating，其餘 deep stats 丟棄
cur.execute("INSERT INTO match_lineups (...) VALUES (...)", (...))
```

### 改動
1. 將 `stat` 展開到新 column，用一個 mapping dict（`JSON_KEY -> column`）。
2. `ON DUPLICATE KEY UPDATE` 加入所有新 column（`=VALUES(col)`），確保 idempotent。
3. `rating_versions` 用 `json.dumps` 存 json。
4. `is_starter` 邏輯保持不變（`substitute is False` → 1）。

```python
# 新增 mapping（與 backfill_runner PLAYER_STAT_JSON_KEYS 同款 snake_case 轉換）
DEEP_STAT_MAP = {
    "goals": "goals", "total_shots": "totalShots",
    "on_target_scoring_attempt": "onTargetScoringAttempt",
    "shot_off_target": "shotOffTarget",
    "blocked_scoring_attempt": "blockedScoringAttempt",
    "hit_woodwork": "hitWoodwork",
    "expected_goals": "expectedGoals",
    "expected_goals_on_target": "expectedGoalsOnTarget",
    "shot_value_normalized": "shotValueNormalized",
    "big_chance_missed": "bigChanceMissed",
    "total_pass": "totalPass", "accurate_pass": "accuratePass",
    "total_long_balls": "totalLongBalls", "accurate_long_balls": "accurateLongBalls",
    # ... 全 72 field
    "rating_versions": "ratingVersions",
    "statistics_type": "statisticsType",
    # ...
}
```

```python
def insert_match_lineups(conn, match_id, data, event):
    ...
    for p in players:
        ...
        stat = p.get("statistics") or {}
        values = {
            "match_id": match_id, "team_id": team_id, "player_id": pl["id"],
            "is_home": is_home,
            "is_starter": 1 if p.get("substitute") is False else 0,
            "jersey_number": p.get("jerseyNumber"),
            "position": pl.get("position") or p.get("position") or "",
            "position_category": _pc,
            "is_captain": 1 if p.get("captain") else 0,
            "minutes_played": stat.get("minutesPlayed"),
            "rating": stat.get("rating"),
        }
        for col, jk in DEEP_STAT_MAP.items():
            values[col] = stat.get(jk)
        values["rating_versions"] = json.dumps(stat.get("ratingVersions")) if stat.get("ratingVersions") is not None else None

        cols = list(values.keys())
        ph = ", ".join(["%s"]*len(cols))
        upd = ", ".join(f"{c}=VALUES({c})" for c in cols if c not in ("match_id","player_id"))
        cur.execute(f"INSERT INTO match_lineups ({', '.join(cols)}) VALUES ({ph}) ON DUPLICATE KEY UPDATE {upd}", [values[c] for c in cols])
        n += 1
    conn.commit()
    return n
```

> **注意**：呢度改用「全 column dynamic INSERT」取代原本 11-column 手寫 SQL，同 `backfill_runner.py::insert_player_stats` 做法一致，更易維護。

---

## 4. Re-fetch 需要性 + 預估 cost

### 必須 re-fetch（evidence 冇 raw payload）

- 1000 個 event_id，每個 1 次 `/event/{id}/lineups` GET。
- 用 **gen4 fp v2 + 21-IP good pool + chrome124 curl_cffi**（同 Stage 2）。

### 預估 cost

| 項目 | 估算 |
|---|---|
| total requests | **1000** `/lineups` GET |
| IP rotation | 21 IP round-robin → 每 IP ~48 次 request |
| retry policy | 每 endpoint max 2 tries（首試失敗換下一個 IP） |
| 最壞情況 requests | ~2000（全 fail 1 retry） |
| 單 request timeout | 20s |
| 預計總耗時 | 1000 × ~2-4s（含 network latency，無 sleep）≈ **40-70 min** |
| 早停 | 10 連續 fail → abort |

> 可選優化：如果只 re-fetch lineups（唔使 event/statistics/incidents），request 量係 Stage 2 嘅 1/4，風險更低。

---

## 5. Risk + rollback plan

### Risk

1. **403 / IP pool 質素**（歷史已知：GOOD 21 IP 通過，BAD 78 被 Varnish 擋）。mitigation：只用 `good_21.txt`，retry 換 IP。
2. **ALTER TABLE 影響**：加 column 對 production `match_lineups` 係 additive（`ADD COLUMN ... NULL`），唔影響現有 rows / 讀取。屬低風險 DDL。
3. **Column count mismatch**：Kris 預期 ~30，實際 72。需先確認，避免 scope 爭議。
4. **rating（decimal(3,1) vs decimal(14,4)）**：`rating` 現列係 `decimal(3,1)`，rating 最大值 10.0，OK；其他 stat 用 `decimal(14,4)` 偏大但安全。
5. **idempotency**：`ON DUPLICATE KEY UPDATE` 已保證，重跑唔會 duplicate。

### Rollback plan

- **ALTER TABLE 回滾**：`ALTER TABLE match_lineups DROP COLUMN <col>, ...`（逐個 drop）。additive change，除非寫入有 bug，否則無需回滾。
- **Parser 回滾**：`git checkout` 回復 `gen4_stage2_backfill.py`（如有 git）或保留 `.bak`。
- **Data 回滾**：新 column 全 NULL 預設，rollback 只係 drop column，唔會污染既有 11-column data。
- **安全網**：ALTER TABLE 前 `mysqldump --no-data appdb match_lineups` 存 schema snapshot。

**4 個階段 gate**：
1. ALTER TABLE（等 Kris 批）
2. parser rewrite + unit test（dry-run，唔寫 DB）
3. 小樣本 smoke（10 events）驗證 UPSERT + column 值
4. 全量 1000 events replay

---

## 6. Next action (blocked on Kris)

- [ ] Kris 確認 column count：**只加 ~30** 定係 **live payload .full 72-field union**
- [ ] Kris explicit 批 ALTER TABLE
- [ ] 之後先郁手

---

## 7. Reference

- `/root/.openclaw/workspace/subagent_results/20260901-053300-main-handoff.md`
- `/root/.openclaw/workspace/memory/2026-09-01.md`（Sentinel recon）
- `/tmp/gen4_stage2/evidence.jsonl`（1000 event_id，無 raw payload）
- `/root/.openclaw/workspace/sofascore-backfill/gen4_stage2_backfill.py`（Stage 2 source）
- `/root/.openclaw/workspace/sofascore-backfill/backfill_runner.py`（protected，含 PLAYER_STAT_FIELDS 72 列定義）