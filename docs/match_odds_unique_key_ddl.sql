-- ============================================================================
-- match_odds UNIQUE key DRAFT (dedup prevention)
-- Task   : match-odds-unique-key-20260927
-- Status : EXECUTED 2026-09-27 23:26-23:42 GMT+8 by Duncan (Main Agent) via appdb_rw
--          Kris approved Option B + executor 2026-09-27 23:16 GMT+8 ("run option b").
--          All gates passed: precheck dup_groups=0 / hash_dup_groups=0 / unique key live
--          (non_unique=0) / canary REJECTED_ERR_1062 -> rollback, row_count 335,694 unchanged.
--          Report: data/match_odds_unique_key_deploy_20260927.json
-- Author : Forge (agent:coding), delegated by Main Agent (Duncan).
--
-- Context
--   2026-09-27 dedup deleted 3,098 true duplicates from match_odds
--   (report: data/odds_dedup_20260927_report.json, backup:
--    data/odds_dedup_backup_20260927.sql — 3,098 rows, restorable).
--   Root cause: match_odds has NO unique key on its data identity, so the
--   gen4 phaseB writers' INSERT ... ON DUPLICATE KEY UPDATE never fires and
--   every re-run appends duplicates.
--
--   NOTE (backup interaction): once this key exists, restoring the 3,098-row
--   dedup backup (data/odds_dedup_backup_20260927.sql) would violate the key
--   by definition (every row in it is a duplicate identity). If a restore is
--   ever needed: drop the key first (Step R), restore, re-dedup, then redeploy.
--
-- Design (Option B — RECOMMENDED)
--   * Identity = the same 18-column set used by the approved dedup:
--     17 data cols + fetched_at (odds_id excluded; it is the auto-inc row id).
--   * Today 6 of those cols are NULL in 100% of rows (bookmaker_id,
--     bookmaker_name, home_odds, draw_odds, away_odds, fetched_at) and
--     odds_type is NULL in 55% (184,989/335,694). MySQL does NOT enforce
--     uniqueness on rows where any key column is NULL, so a plain composite
--     UNIQUE containing those columns cannot protect the real dup class.
--   * Therefore: SHA2-256 hash over the 18-col identity (NULL-sentinel-safe,
--     byte-exact) stored as a VIRTUAL generated column, plus UNIQUE on the
--     hash (BINARY(32)). Future-proof: when bookmaker_id / fetched_at start
--     being populated, identity stays exact with no key change.
--
-- Verified environment (2026-09-27, read-only session as appdb_rw)
--   MySQL 8.0.46 / InnoDB / utf8mb4_0900_ai_ci
--   row_count = 335,694 ; dup groups on 18-col identity = 0 (DDL-safe today)
--   appdb_rw grants on appdb.*: SELECT, INSERT, UPDATE, DELETE, CREATE,
--   INDEX, ALTER → sufficient for Step A + Step C AND for rollback.
--   (No DROP TABLE privilege needed anywhere in this file.)
--
-- Rollout = 3 steps (A: add hash column; B: byte-exact dup check on real
--   computed hashes; C: add unique index). Deliberately NOT one ALTER:
--   Step B gives a definitive pre-index verification. If C fails, ALTER
--   aborts cleanly (transactional DDL), rollback = Step R.
--
-- Concurrent writers during rollout: gen4 phaseB scripts / backfill_runner
--   do not reference the new column (it is generated) → no code changes
--   required. See docs/match_odds_unique_key_analysis.md §Pipeline for the
--   one recommended pre-deploy patch to backfill_runner.py.
-- ============================================================================

-- ============================================================================
-- PRE-CHECK 0 — read-only sanity (all expected values shown in comments)
-- ============================================================================

-- Total rows (expected: 335,694 at time of drafting; tolerate growth)
SELECT COUNT(*) AS row_count FROM match_odds;

-- 18-col identity dup groups, collation-aware variant (expected: 0, 0).
-- This mirrors the dedup definition; verified live 2026-09-27.
SELECT COUNT(*) AS dup_groups, COALESCE(SUM(c-1),0) AS rows_to_delete FROM (
  SELECT match_id, market_id, market_name, market_group, market_period,
         structure_type, suspended, choice_name, initial_fractional_value,
         fractional_value, winning, bookmaker_id, bookmaker_name, odds_type,
         home_odds, draw_odds, away_odds, fetched_at, COUNT(*) c
  FROM match_odds
  GROUP BY match_id, market_id, market_name, market_group, market_period,
           structure_type, suspended, choice_name, initial_fractional_value,
           fractional_value, winning, bookmaker_id, bookmaker_name, odds_type,
           home_odds, draw_odds, away_odds, fetched_at
  HAVING COUNT(*) > 1
) t;

-- ============================================================================
-- STEP A — add the identity hash as a VIRTUAL generated column
--           (no rebuild, no data change; VIRTUAL = zero row storage)
--
--   Separator : CHAR(31)  (unit separator, 0x1F)
--   NULL mark : CHAR(30)  (record separator, 0x1E)
--   NULLs are wrapped in COALESCE so they hash as a distinct sentinel value
--   (CONCAT_WS would otherwise SKIP NULL args — the historical v2 overcount
--   bug — and two rows differing only by a NULL-vs-value would collide).
--   Hash is byte-exact (binary concat), i.e. it enforces the byte-identical
--   dup class; see analysis §Design for the collation nuance.
-- ============================================================================

ALTER TABLE match_odds
  ADD COLUMN odds_identity_hash BINARY(32)
    GENERATED ALWAYS AS (
      UNHEX(SHA2(CONCAT_WS(CHAR(31),
        COALESCE(CAST(match_id                AS CHAR), CHAR(30)),
        COALESCE(CAST(market_id               AS CHAR), CHAR(30)),
        COALESCE(CAST(market_name             AS CHAR), CHAR(30)),
        COALESCE(CAST(market_group            AS CHAR), CHAR(30)),
        COALESCE(CAST(market_period           AS CHAR), CHAR(30)),
        COALESCE(CAST(structure_type          AS CHAR), CHAR(30)),
        COALESCE(CAST(suspended               AS CHAR), CHAR(30)),
        COALESCE(CAST(choice_name             AS CHAR), CHAR(30)),
        COALESCE(CAST(initial_fractional_value AS CHAR), CHAR(30)),
        COALESCE(CAST(fractional_value        AS CHAR), CHAR(30)),
        COALESCE(CAST(winning                 AS CHAR), CHAR(30)),
        COALESCE(CAST(bookmaker_id            AS CHAR), CHAR(30)),
        COALESCE(CAST(bookmaker_name          AS CHAR), CHAR(30)),
        COALESCE(CAST(odds_type               AS CHAR), CHAR(30)),
        COALESCE(CAST(home_odds               AS CHAR), CHAR(30)),
        COALESCE(CAST(draw_odds               AS CHAR), CHAR(30)),
        COALESCE(CAST(away_odds               AS CHAR), CHAR(30)),
        COALESCE(CAST(fetched_at              AS CHAR), CHAR(30))
      ), 256))
    ) VIRTUAL
    AFTER fetched_at;

-- ============================================================================
-- STEP B — byte-exact duplicate check on the REAL computed hashes
--           (definitive predictor for Step C success)
--
--   MUST return 0 before Step C. If > 0: run the dedup (odds_dedup_delete.py
--   v4 semantics, adapted to GROUP BY odds_identity_hash) or investigate
--   which rows collide, then re-check. Step C on a non-zero result will
--   abort cleanly (MySQL 8.0 ALTER is transactional), but check first.
-- ============================================================================

SELECT COUNT(*) AS hash_dup_groups, COALESCE(SUM(c-1),0) AS hash_rows_to_delete FROM (
  SELECT odds_identity_hash, COUNT(*) c
  FROM match_odds
  GROUP BY odds_identity_hash
  HAVING COUNT(*) > 1
) t;

-- Inspect any collision (only needed if the count above is > 0):
-- SELECT match_id, market_id, market_name, choice_name, fractional_value,
--        odds_id, HEX(odds_identity_hash)
-- FROM match_odds
-- WHERE odds_identity_hash = UNHEX('<hex-from-error-or-query>')
-- ORDER BY odds_id;

-- ============================================================================
-- STEP C — add the unique index (INPLACE: no rebuild, concurrent DML OK)
--           ~336k rows → builds in seconds.
--           If MySQL rejects ALGORITHM=INPLACE for any reason, rerun without
--           the ALGORITHM/LOCK clauses; failure aborts cleanly either way.
-- ============================================================================

ALTER TABLE match_odds
  ADD UNIQUE KEY uq_match_odds_identity (odds_identity_hash),
  ALGORITHM=INPLACE, LOCK=NONE;

-- ============================================================================
-- POST-DEPLOY CHECKS
-- ============================================================================

-- 1) Key exists (expect uq_match_odds_identity, NON_UNIQUE=1? no — expect 0)
SHOW INDEX FROM match_odds WHERE Key_name = 'uq_match_odds_identity';

-- 2) Column is virtual generated, not stored
SHOW CREATE TABLE match_odds;

-- 3) Row count unchanged by the DDL itself (writers may add rows anytime)
SELECT COUNT(*) AS row_count FROM match_odds;

-- ============================================================================
-- CANARY TEST — proves the key blocks exact re-inserts.
--   Run interactively. The INSERT is EXPECTED to fail with
--   ERROR 1062 (23000): Duplicate entry '<hex>' for key 'uq_match_odds_identity'
--   After the expected error, ROLLBACK. If it SUCCEEDS instead: ROLLBACK
--   immediately and investigate (key not enforced).
-- ============================================================================

-- Pick an existing row id (read-only):
--   SET @oid = (SELECT MIN(odds_id) FROM match_odds);
-- START TRANSACTION;
-- INSERT INTO match_odds
--   (match_id, market_id, market_name, market_group, market_period,
--    structure_type, suspended, choice_name, initial_fractional_value,
--    fractional_value, winning, bookmaker_id, bookmaker_name, odds_type,
--    home_odds, draw_odds, away_odds, fetched_at)
-- SELECT match_id, market_id, market_name, market_group, market_period,
--        structure_type, suspended, choice_name, initial_fractional_value,
--        fractional_value, winning, bookmaker_id, bookmaker_name, odds_type,
--        home_odds, draw_odds, away_odds, fetched_at
-- FROM match_odds WHERE odds_id = @oid;
--   -- expect ERROR 1062 here
-- ROLLBACK;

-- ============================================================================
-- STEP R — ROLLBACK (full revert; requires only ALTER, which appdb_rw has)
-- ============================================================================

-- ALTER TABLE match_odds
--   DROP INDEX uq_match_odds_identity,
--   DROP COLUMN odds_identity_hash;

-- ============================================================================
-- OPTION A (REJECTED — kept for the record)
--   Plain composite UNIQUE on the 11 never-NULL cols:
--   (match_id, market_id, market_name, market_group, market_period,
--    structure_type, suspended, choice_name, initial_fractional_value,
--    fractional_value, winning)
--   * Fits the 3,072-byte index limit (~1,698 bytes utf8mb4) and is feasible
--     today (0 conflicts, verified live) BUT:
--   - identity ≠ dedup definition (ignores odds_type, which varies
--     NULL vs '1x2' across the two writer families → false merges between
--     a backfill_runner row and a gen4 row with the same 11 cols);
--   - offers no protection once bookmaker_id / fetched_at become populated
--     (bookmaker-specific rows would falsely conflict and ON DUPLICATE KEY
--     UPDATE would clobber one bookmaker's odds into another's row);
--   - fat 18-byte×utf8mb4 index vs 32-byte binary hash.
--   See docs/match_odds_unique_key_analysis.md §Design for the full matrix.
-- ============================================================================
