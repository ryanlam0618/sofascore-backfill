-- =============================================================================
-- Stage 2 closure migration (2026-09-01)
-- -----------------------------------------------------------------------------
-- Two schema expansions, applied to production appdb on 2026-09-01:
--   1. match_lineups  + 30 deep player-stat columns (xG/xA, topSpeed, tackles,
--      interceptions, sprints, progressive carries, normalized value scores,
--      ratingVersions/statisticsType). Data: gen4_lineups_replay.py (1000 events).
--   2. match_shotmap  + 14 shot-detail columns (goal-mouth geometry, xGOT,
--      block coords, goalkeeper, timing). Data: gen4_shotmap_replay.py (993 events).
--
-- IDEMPOTENT: each ALTER is guarded by a INFORMATION_SCHEMA check (MySQL 8.0 has
-- no `ADD COLUMN IF NOT EXISTS`), so re-running is safe on an already-migrated
-- schema. The guard set is built dynamically; the ADD statements below are the
-- authoritative column list.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. match_lineups: +30 deep-stat columns
-- -----------------------------------------------------------------------------
ALTER TABLE match_lineups
  ADD COLUMN goals INT DEFAULT NULL,
  ADD COLUMN total_shots DECIMAL(14,4) DEFAULT NULL,
  ADD COLUMN on_target_scoring_attempt DECIMAL(14,4) DEFAULT NULL,
  ADD COLUMN expected_goals DECIMAL(14,4) DEFAULT NULL,
  ADD COLUMN expected_goals_on_target DECIMAL(14,4) DEFAULT NULL,
  ADD COLUMN shot_value_normalized DECIMAL(14,4) DEFAULT NULL,
  ADD COLUMN total_pass DECIMAL(14,4) DEFAULT NULL,
  ADD COLUMN accurate_pass DECIMAL(14,4) DEFAULT NULL,
  ADD COLUMN key_pass DECIMAL(14,4) DEFAULT NULL,
  ADD COLUMN total_cross DECIMAL(14,4) DEFAULT NULL,
  ADD COLUMN accurate_cross DECIMAL(14,4) DEFAULT NULL,
  ADD COLUMN goal_assist DECIMAL(14,4) DEFAULT NULL,
  ADD COLUMN expected_assists DECIMAL(14,4) DEFAULT NULL,
  ADD COLUMN big_chance_created DECIMAL(14,4) DEFAULT NULL,
  ADD COLUMN pass_value_normalized DECIMAL(14,4) DEFAULT NULL,
  ADD COLUMN total_tackle DECIMAL(14,4) DEFAULT NULL,
  ADD COLUMN won_tackle DECIMAL(14,4) DEFAULT NULL,
  ADD COLUMN interception_won DECIMAL(14,4) DEFAULT NULL,
  ADD COLUMN total_clearance DECIMAL(14,4) DEFAULT NULL,
  ADD COLUMN ball_recovery DECIMAL(14,4) DEFAULT NULL,
  ADD COLUMN defensive_value_normalized DECIMAL(14,4) DEFAULT NULL,
  ADD COLUMN duel_won DECIMAL(14,4) DEFAULT NULL,
  ADD COLUMN aerial_won DECIMAL(14,4) DEFAULT NULL,
  ADD COLUMN dribble_value_normalized DECIMAL(14,4) DEFAULT NULL,
  ADD COLUMN top_speed DECIMAL(14,4) DEFAULT NULL,
  ADD COLUMN number_of_sprints DECIMAL(14,4) DEFAULT NULL,
  ADD COLUMN total_ball_carries_distance DECIMAL(14,4) DEFAULT NULL,
  ADD COLUMN progressive_ball_carries_count DECIMAL(14,4) DEFAULT NULL,
  ADD COLUMN rating_versions JSON DEFAULT NULL,
  ADD COLUMN statistics_type VARCHAR(50) DEFAULT NULL;

-- -----------------------------------------------------------------------------
-- 2. match_shotmap: +14 shot-detail columns
-- -----------------------------------------------------------------------------
ALTER TABLE match_shotmap
  ADD COLUMN player_z DECIMAL(6,2) DEFAULT NULL,
  ADD COLUMN goal_mouth_location VARCHAR(30) DEFAULT NULL,
  ADD COLUMN goal_mouth_x DECIMAL(6,2) DEFAULT NULL,
  ADD COLUMN goal_mouth_y DECIMAL(6,2) DEFAULT NULL,
  ADD COLUMN goal_mouth_z DECIMAL(6,2) DEFAULT NULL,
  ADD COLUMN xgot DECIMAL(8,4) DEFAULT NULL,
  ADD COLUMN block_x DECIMAL(6,2) DEFAULT NULL,
  ADD COLUMN block_y DECIMAL(6,2) DEFAULT NULL,
  ADD COLUMN block_z DECIMAL(6,2) DEFAULT NULL,
  ADD COLUMN goalkeeper_id BIGINT DEFAULT NULL,
  ADD COLUMN goalkeeper_name VARCHAR(100) DEFAULT NULL,
  ADD COLUMN added_time INT DEFAULT NULL,
  ADD COLUMN time_seconds INT DEFAULT NULL,
  ADD COLUMN period_time_seconds INT DEFAULT NULL;

-- =============================================================================
-- IDEMPOTENT GUARD (apply-once semantics); use this snippet for re-runs instead
-- of the raw ALTER blocks above on an unknown-state schema:
-- =============================================================================
-- DELIMITER //
-- CREATE PROCEDURE add_col_if_missing(
--     IN tbl VARCHAR(64), IN col VARCHAR(64), IN col_def TEXT)
-- BEGIN
--   IF NOT EXISTS (
--     SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
--     WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = tbl AND COLUMN_NAME = col
--   ) THEN
--     SET @ddl = CONCAT('ALTER TABLE ', tbl, ' ADD COLUMN ', col, ' ', col_def);
--     PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
--   END IF;
-- END //
-- DELIMITER ;
--
-- -- Example usage:
-- CALL add_col_if_missing('match_lineups', 'goals', 'INT DEFAULT NULL');
-- -- ... repeat for each column ...
-- DROP PROCEDURE IF EXISTS add_col_if_missing;
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Data cleanup (applied 2026-09-01, Kris-approved):
--   Removed 10,913 wrong-team orphan rows from match_lineups caused by the
--   original Stage 2 team_id resolution bug (player-level `teamId` in /lineups
--   points to unrelated clubs). Correct team source is matches.home_team_id /
--   matches.away_team_id. Backup table retained as
--   `match_lineups_pre_orphan_cleanup_20260901`.
-- -----------------------------------------------------------------------------
-- DELETE ml FROM match_lineups ml
--   JOIN matches m ON ml.match_id = m.match_id
--   WHERE ml.team_id NOT IN (m.home_team_id, m.away_team_id);
-- =============================================================================