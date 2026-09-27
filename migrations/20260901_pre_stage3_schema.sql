-- Pre-Stage3 schema snapshot (no data), generated 2026-09-02
-- Produced via SHOW CREATE TABLE (mysqldump binary unavailable)
-- tables: 24

-- ---- _duncan_verify ----
CREATE TABLE `_duncan_verify` (
  `id` int NOT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- _fpv2_temp_verify ----
CREATE TABLE `_fpv2_temp_verify` (
  `id` int NOT NULL AUTO_INCREMENT,
  `v` varchar(16) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- competitions ----
CREATE TABLE `competitions` (
  `competition_id` int NOT NULL,
  `name` varchar(100) NOT NULL,
  `short_name` varchar(50) DEFAULT NULL,
  `category_id` int NOT NULL,
  `ut_id` int DEFAULT NULL,
  `type` enum('league','cup','international') DEFAULT 'league',
  `country_code` varchar(3) DEFAULT NULL,
  `season_mode` varchar(20) DEFAULT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`competition_id`),
  KEY `country_code` (`country_code`),
  CONSTRAINT `competitions_ibfk_1` FOREIGN KEY (`country_code`) REFERENCES `countries` (`country_code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---- countries ----
CREATE TABLE `countries` (
  `country_code` varchar(3) NOT NULL,
  `country_name` varchar(100) NOT NULL,
  `continent` varchar(20) DEFAULT NULL,
  `alpha2` varchar(2) DEFAULT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`country_code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---- fetch_log ----
CREATE TABLE `fetch_log` (
  `log_id` bigint NOT NULL AUTO_INCREMENT,
  `table_name` varchar(50) NOT NULL,
  `match_id` bigint DEFAULT NULL,
  `season_id` int DEFAULT NULL,
  `competition_id` int DEFAULT NULL,
  `operation` varchar(50) DEFAULT NULL,
  `status` enum('success','error','retry','skip') NOT NULL,
  `status_code` int DEFAULT NULL,
  `rows_affected` int DEFAULT '0',
  `error_message` text,
  `duration_ms` int DEFAULT NULL,
  `source` varchar(50) DEFAULT NULL,
  `fetched_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`log_id`),
  KEY `idx_table` (`table_name`),
  KEY `idx_match` (`match_id`),
  KEY `idx_time` (`fetched_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---- match_average_positions ----
CREATE TABLE `match_average_positions` (
  `avg_pos_id` bigint NOT NULL AUTO_INCREMENT,
  `match_id` bigint NOT NULL,
  `player_id` bigint NOT NULL,
  `team_id` int NOT NULL,
  `avg_x` decimal(6,2) DEFAULT NULL,
  `avg_y` decimal(6,2) DEFAULT NULL,
  PRIMARY KEY (`avg_pos_id`),
  UNIQUE KEY `match_id` (`match_id`,`player_id`),
  KEY `player_id` (`player_id`),
  CONSTRAINT `match_average_positions_ibfk_1` FOREIGN KEY (`match_id`) REFERENCES `matches` (`match_id`),
  CONSTRAINT `match_average_positions_ibfk_2` FOREIGN KEY (`player_id`) REFERENCES `players` (`player_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---- match_best_players ----
CREATE TABLE `match_best_players` (
  `best_player_id` bigint NOT NULL AUTO_INCREMENT,
  `match_id` bigint NOT NULL,
  `player_id` bigint NOT NULL,
  `team_id` int NOT NULL,
  `is_home` tinyint(1) NOT NULL,
  `rating` decimal(3,1) NOT NULL,
  `rank` int DEFAULT NULL,
  PRIMARY KEY (`best_player_id`),
  UNIQUE KEY `match_id` (`match_id`,`player_id`),
  KEY `player_id` (`player_id`),
  KEY `team_id` (`team_id`),
  CONSTRAINT `match_best_players_ibfk_1` FOREIGN KEY (`match_id`) REFERENCES `matches` (`match_id`),
  CONSTRAINT `match_best_players_ibfk_2` FOREIGN KEY (`player_id`) REFERENCES `players` (`player_id`),
  CONSTRAINT `match_best_players_ibfk_3` FOREIGN KEY (`team_id`) REFERENCES `teams` (`team_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---- match_h2h ----
CREATE TABLE `match_h2h` (
  `h2h_id` bigint NOT NULL AUTO_INCREMENT,
  `match_id` bigint NOT NULL,
  `home_team_id` int NOT NULL,
  `away_team_id` int NOT NULL,
  `home_wins` int DEFAULT '0',
  `draws` int DEFAULT '0',
  `away_wins` int DEFAULT '0',
  `total_matches` int DEFAULT '0',
  `home_goals` int DEFAULT '0',
  `away_goals` int DEFAULT '0',
  PRIMARY KEY (`h2h_id`),
  UNIQUE KEY `match_id` (`match_id`),
  CONSTRAINT `match_h2h_ibfk_1` FOREIGN KEY (`match_id`) REFERENCES `matches` (`match_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---- match_incidents ----
CREATE TABLE `match_incidents` (
  `incident_id` bigint NOT NULL,
  `match_id` bigint NOT NULL,
  `team_id` int NOT NULL,
  `player_id` bigint DEFAULT NULL,
  `related_player_id` bigint DEFAULT NULL,
  `assist_player_id` bigint DEFAULT NULL,
  `incident_type` enum('goal','card','substitution','period','var') NOT NULL,
  `minute` int NOT NULL,
  `added_time` int DEFAULT '0',
  `period` enum('first','second','extra_first','extra_second') NOT NULL,
  `is_home` tinyint(1) NOT NULL,
  `goal_type` enum('regular','penalty','own_goal','free_kick') DEFAULT NULL,
  `card_type` enum('yellow','red','yellow_red') DEFAULT NULL,
  `incident_text` text,
  `reason` varchar(255) DEFAULT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`incident_id`),
  KEY `team_id` (`team_id`),
  KEY `player_id` (`player_id`),
  KEY `idx_incidents_match` (`match_id`),
  CONSTRAINT `match_incidents_ibfk_1` FOREIGN KEY (`match_id`) REFERENCES `matches` (`match_id`),
  CONSTRAINT `match_incidents_ibfk_2` FOREIGN KEY (`team_id`) REFERENCES `teams` (`team_id`),
  CONSTRAINT `match_incidents_ibfk_3` FOREIGN KEY (`player_id`) REFERENCES `players` (`player_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---- match_lineups ----
CREATE TABLE `match_lineups` (
  `lineup_id` bigint NOT NULL AUTO_INCREMENT,
  `match_id` bigint NOT NULL,
  `team_id` int NOT NULL,
  `player_id` bigint NOT NULL,
  `is_home` tinyint(1) NOT NULL,
  `is_starter` tinyint(1) NOT NULL,
  `jersey_number` int DEFAULT NULL,
  `position` varchar(50) DEFAULT NULL,
  `position_category` enum('GK','DEF','MID','FWD') NOT NULL,
  `is_captain` tinyint(1) DEFAULT '0',
  `minutes_played` int DEFAULT '0',
  `rating` decimal(3,1) DEFAULT NULL,
  `goals` int DEFAULT NULL,
  `total_shots` decimal(14,4) DEFAULT NULL,
  `on_target_scoring_attempt` decimal(14,4) DEFAULT NULL,
  `expected_goals` decimal(14,4) DEFAULT NULL,
  `expected_goals_on_target` decimal(14,4) DEFAULT NULL,
  `shot_value_normalized` decimal(14,4) DEFAULT NULL,
  `total_pass` decimal(14,4) DEFAULT NULL,
  `accurate_pass` decimal(14,4) DEFAULT NULL,
  `key_pass` decimal(14,4) DEFAULT NULL,
  `total_cross` decimal(14,4) DEFAULT NULL,
  `accurate_cross` decimal(14,4) DEFAULT NULL,
  `goal_assist` decimal(14,4) DEFAULT NULL,
  `expected_assists` decimal(14,4) DEFAULT NULL,
  `big_chance_created` decimal(14,4) DEFAULT NULL,
  `pass_value_normalized` decimal(14,4) DEFAULT NULL,
  `total_tackle` decimal(14,4) DEFAULT NULL,
  `won_tackle` decimal(14,4) DEFAULT NULL,
  `interception_won` decimal(14,4) DEFAULT NULL,
  `total_clearance` decimal(14,4) DEFAULT NULL,
  `ball_recovery` decimal(14,4) DEFAULT NULL,
  `defensive_value_normalized` decimal(14,4) DEFAULT NULL,
  `duel_won` decimal(14,4) DEFAULT NULL,
  `aerial_won` decimal(14,4) DEFAULT NULL,
  `dribble_value_normalized` decimal(14,4) DEFAULT NULL,
  `top_speed` decimal(14,4) DEFAULT NULL,
  `number_of_sprints` decimal(14,4) DEFAULT NULL,
  `total_ball_carries_distance` decimal(14,4) DEFAULT NULL,
  `progressive_ball_carries_count` decimal(14,4) DEFAULT NULL,
  `rating_versions` json DEFAULT NULL,
  `statistics_type` varchar(50) DEFAULT NULL,
  PRIMARY KEY (`lineup_id`),
  UNIQUE KEY `match_id` (`match_id`,`team_id`,`player_id`),
  KEY `team_id` (`team_id`),
  KEY `player_id` (`player_id`),
  KEY `idx_lineups_match` (`match_id`),
  CONSTRAINT `match_lineups_ibfk_1` FOREIGN KEY (`match_id`) REFERENCES `matches` (`match_id`),
  CONSTRAINT `match_lineups_ibfk_2` FOREIGN KEY (`team_id`) REFERENCES `teams` (`team_id`),
  CONSTRAINT `match_lineups_ibfk_3` FOREIGN KEY (`player_id`) REFERENCES `players` (`player_id`)
) ENGINE=InnoDB AUTO_INCREMENT=90287 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---- match_lineups_pre_orphan_cleanup_20260901 ----
CREATE TABLE `match_lineups_pre_orphan_cleanup_20260901` (
  `lineup_id` bigint NOT NULL DEFAULT '0',
  `match_id` bigint NOT NULL,
  `team_id` int NOT NULL,
  `player_id` bigint NOT NULL,
  `is_home` tinyint(1) NOT NULL,
  `is_starter` tinyint(1) NOT NULL,
  `jersey_number` int DEFAULT NULL,
  `position` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci DEFAULT NULL,
  `position_category` enum('GK','DEF','MID','FWD') CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NOT NULL,
  `is_captain` tinyint(1) DEFAULT '0',
  `minutes_played` int DEFAULT '0',
  `rating` decimal(3,1) DEFAULT NULL,
  `goals` int DEFAULT NULL,
  `total_shots` decimal(14,4) DEFAULT NULL,
  `on_target_scoring_attempt` decimal(14,4) DEFAULT NULL,
  `expected_goals` decimal(14,4) DEFAULT NULL,
  `expected_goals_on_target` decimal(14,4) DEFAULT NULL,
  `shot_value_normalized` decimal(14,4) DEFAULT NULL,
  `total_pass` decimal(14,4) DEFAULT NULL,
  `accurate_pass` decimal(14,4) DEFAULT NULL,
  `key_pass` decimal(14,4) DEFAULT NULL,
  `total_cross` decimal(14,4) DEFAULT NULL,
  `accurate_cross` decimal(14,4) DEFAULT NULL,
  `goal_assist` decimal(14,4) DEFAULT NULL,
  `expected_assists` decimal(14,4) DEFAULT NULL,
  `big_chance_created` decimal(14,4) DEFAULT NULL,
  `pass_value_normalized` decimal(14,4) DEFAULT NULL,
  `total_tackle` decimal(14,4) DEFAULT NULL,
  `won_tackle` decimal(14,4) DEFAULT NULL,
  `interception_won` decimal(14,4) DEFAULT NULL,
  `total_clearance` decimal(14,4) DEFAULT NULL,
  `ball_recovery` decimal(14,4) DEFAULT NULL,
  `defensive_value_normalized` decimal(14,4) DEFAULT NULL,
  `duel_won` decimal(14,4) DEFAULT NULL,
  `aerial_won` decimal(14,4) DEFAULT NULL,
  `dribble_value_normalized` decimal(14,4) DEFAULT NULL,
  `top_speed` decimal(14,4) DEFAULT NULL,
  `number_of_sprints` decimal(14,4) DEFAULT NULL,
  `total_ball_carries_distance` decimal(14,4) DEFAULT NULL,
  `progressive_ball_carries_count` decimal(14,4) DEFAULT NULL,
  `rating_versions` json DEFAULT NULL,
  `statistics_type` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci DEFAULT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- match_momentum ----
CREATE TABLE `match_momentum` (
  `momentum_id` bigint NOT NULL AUTO_INCREMENT,
  `match_id` bigint NOT NULL,
  `minute` int NOT NULL,
  `period` enum('first','second') NOT NULL,
  `home_value` int NOT NULL,
  `away_value` int NOT NULL,
  PRIMARY KEY (`momentum_id`),
  UNIQUE KEY `match_id` (`match_id`,`minute`),
  CONSTRAINT `match_momentum_ibfk_1` FOREIGN KEY (`match_id`) REFERENCES `matches` (`match_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---- match_odds ----
CREATE TABLE `match_odds` (
  `odds_id` bigint NOT NULL AUTO_INCREMENT,
  `match_id` bigint NOT NULL,
  `bookmaker_id` int DEFAULT NULL,
  `bookmaker_name` varchar(50) DEFAULT NULL,
  `odds_type` enum('1x2','asian_handicap','over_under') DEFAULT '1x2',
  `home_odds` decimal(10,3) DEFAULT NULL,
  `draw_odds` decimal(10,3) DEFAULT NULL,
  `away_odds` decimal(10,3) DEFAULT NULL,
  `fetched_at` datetime DEFAULT NULL,
  PRIMARY KEY (`odds_id`),
  KEY `match_id` (`match_id`),
  CONSTRAINT `match_odds_ibfk_1` FOREIGN KEY (`match_id`) REFERENCES `matches` (`match_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---- match_player_stats ----
CREATE TABLE `match_player_stats` (
  `player_stat_id` bigint NOT NULL AUTO_INCREMENT,
  `match_id` bigint NOT NULL,
  `team_id` int NOT NULL,
  `player_id` bigint NOT NULL,
  `is_home` tinyint(1) NOT NULL,
  `minutes_played` int DEFAULT '0',
  `goals` int DEFAULT '0',
  `shots` int DEFAULT '0',
  `shots_on_target` int DEFAULT '0',
  `xg` decimal(5,3) DEFAULT '0.000',
  `assists` int DEFAULT '0',
  `xa` decimal(5,3) DEFAULT '0.000',
  `key_passes` int DEFAULT '0',
  `passes_completed` int DEFAULT '0',
  `tackles` int DEFAULT '0',
  `interceptions` int DEFAULT '0',
  `duels_won` int DEFAULT '0',
  `fouls_committed` int DEFAULT '0',
  `yellow_cards` int DEFAULT '0',
  `red_cards` int DEFAULT '0',
  `rating` decimal(3,1) DEFAULT NULL,
  `total_pass` decimal(14,4) DEFAULT NULL,
  `accurate_pass` decimal(14,4) DEFAULT NULL,
  `total_long_balls` decimal(14,4) DEFAULT NULL,
  `accurate_long_balls` decimal(14,4) DEFAULT NULL,
  `total_own_half_passes` decimal(14,4) DEFAULT NULL,
  `accurate_own_half_passes` decimal(14,4) DEFAULT NULL,
  `total_opposition_half_passes` decimal(14,4) DEFAULT NULL,
  `accurate_opposition_half_passes` decimal(14,4) DEFAULT NULL,
  `total_cross` decimal(14,4) DEFAULT NULL,
  `accurate_cross` decimal(14,4) DEFAULT NULL,
  `key_pass` decimal(14,4) DEFAULT NULL,
  `goal_assist` decimal(14,4) DEFAULT NULL,
  `big_chance_created` decimal(14,4) DEFAULT NULL,
  `total_shots` decimal(14,4) DEFAULT NULL,
  `on_target_scoring_attempt` decimal(14,4) DEFAULT NULL,
  `shot_off_target` decimal(14,4) DEFAULT NULL,
  `blocked_scoring_attempt` decimal(14,4) DEFAULT NULL,
  `expected_goals` decimal(14,4) DEFAULT NULL,
  `expected_goals_on_target` decimal(14,4) DEFAULT NULL,
  `expected_assists` decimal(14,4) DEFAULT NULL,
  `big_chance_missed` decimal(14,4) DEFAULT NULL,
  `shot_value_normalized` decimal(14,4) DEFAULT NULL,
  `duel_lost` decimal(14,4) DEFAULT NULL,
  `aerial_won` decimal(14,4) DEFAULT NULL,
  `aerial_lost` decimal(14,4) DEFAULT NULL,
  `total_contest` decimal(14,4) DEFAULT NULL,
  `won_contest` decimal(14,4) DEFAULT NULL,
  `challenge_lost` decimal(14,4) DEFAULT NULL,
  `total_tackle` decimal(14,4) DEFAULT NULL,
  `won_tackle` decimal(14,4) DEFAULT NULL,
  `interception_won` decimal(14,4) DEFAULT NULL,
  `total_clearance` decimal(14,4) DEFAULT NULL,
  `ball_recovery` decimal(14,4) DEFAULT NULL,
  `outfielder_block` decimal(14,4) DEFAULT NULL,
  `last_man_tackle` decimal(14,4) DEFAULT NULL,
  `error_lead_to_a_goal` decimal(14,4) DEFAULT NULL,
  `error_lead_to_a_shot` decimal(14,4) DEFAULT NULL,
  `dribble_value_normalized` decimal(14,4) DEFAULT NULL,
  `dispossessed` decimal(14,4) DEFAULT NULL,
  `unsuccessful_touch` decimal(14,4) DEFAULT NULL,
  `fouls` decimal(14,4) DEFAULT NULL,
  `was_fouled` decimal(14,4) DEFAULT NULL,
  `total_offside` decimal(14,4) DEFAULT NULL,
  `own_goals` decimal(14,4) DEFAULT NULL,
  `saves` decimal(14,4) DEFAULT NULL,
  `saved_shots_from_inside_the_box` decimal(14,4) DEFAULT NULL,
  `good_high_claim` decimal(14,4) DEFAULT NULL,
  `total_keeper_sweeper` decimal(14,4) DEFAULT NULL,
  `accurate_keeper_sweeper` decimal(14,4) DEFAULT NULL,
  `goals_prevented` decimal(14,4) DEFAULT NULL,
  `keeper_save_value` decimal(14,4) DEFAULT NULL,
  `goalkeeper_value_normalized` decimal(14,4) DEFAULT NULL,
  `top_speed` decimal(14,4) DEFAULT NULL,
  `kilometers_covered` decimal(14,4) DEFAULT NULL,
  `number_of_sprints` decimal(14,4) DEFAULT NULL,
  `meters_covered_running_km` decimal(14,4) DEFAULT NULL,
  `meters_covered_high_speed_running_km` decimal(14,4) DEFAULT NULL,
  `meters_covered_sprinting_km` decimal(14,4) DEFAULT NULL,
  `total_ball_carries_distance` decimal(14,4) DEFAULT NULL,
  `ball_carries_count` decimal(14,4) DEFAULT NULL,
  `total_progression` decimal(14,4) DEFAULT NULL,
  `progressive_ball_carries_count` decimal(14,4) DEFAULT NULL,
  `total_progressive_ball_carries_distance` decimal(14,4) DEFAULT NULL,
  `best_ball_carry_progression` decimal(14,4) DEFAULT NULL,
  `touches` decimal(14,4) DEFAULT NULL,
  `possession_lost_ctrl` decimal(14,4) DEFAULT NULL,
  `pass_value_normalized` decimal(14,4) DEFAULT NULL,
  `defensive_value_normalized` decimal(14,4) DEFAULT NULL,
  `statistics_type` varchar(50) DEFAULT NULL,
  `rating_versions` json DEFAULT NULL,
  `raw_statistics` json DEFAULT NULL,
  PRIMARY KEY (`player_stat_id`),
  UNIQUE KEY `match_id` (`match_id`,`player_id`),
  KEY `team_id` (`team_id`),
  KEY `player_id` (`player_id`),
  CONSTRAINT `match_player_stats_ibfk_1` FOREIGN KEY (`match_id`) REFERENCES `matches` (`match_id`),
  CONSTRAINT `match_player_stats_ibfk_2` FOREIGN KEY (`team_id`) REFERENCES `teams` (`team_id`),
  CONSTRAINT `match_player_stats_ibfk_3` FOREIGN KEY (`player_id`) REFERENCES `players` (`player_id`)
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---- match_shotmap ----
CREATE TABLE `match_shotmap` (
  `shot_id` bigint NOT NULL,
  `match_id` bigint NOT NULL,
  `team_id` int NOT NULL,
  `player_id` bigint NOT NULL,
  `is_home` tinyint(1) NOT NULL,
  `minute` int NOT NULL,
  `incident_type` varchar(50) DEFAULT NULL,
  `shot_type` varchar(50) DEFAULT NULL,
  `situation` varchar(50) DEFAULT NULL,
  `body_part` varchar(30) DEFAULT NULL,
  `player_x` decimal(6,2) DEFAULT NULL,
  `player_y` decimal(6,2) DEFAULT NULL,
  `player_z` decimal(6,2) DEFAULT NULL,
  `xg` decimal(6,4) DEFAULT NULL,
  `is_goal` tinyint(1) DEFAULT '0',
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `goal_mouth_location` varchar(30) DEFAULT NULL,
  `goal_mouth_x` decimal(6,2) DEFAULT NULL,
  `goal_mouth_y` decimal(6,2) DEFAULT NULL,
  `goal_mouth_z` decimal(6,2) DEFAULT NULL,
  `xgot` decimal(8,4) DEFAULT NULL,
  `block_x` decimal(6,2) DEFAULT NULL,
  `block_y` decimal(6,2) DEFAULT NULL,
  `block_z` decimal(6,2) DEFAULT NULL,
  `goalkeeper_id` bigint DEFAULT NULL,
  `goalkeeper_name` varchar(100) DEFAULT NULL,
  `added_time` int DEFAULT NULL,
  `time_seconds` int DEFAULT NULL,
  `period_time_seconds` int DEFAULT NULL,
  PRIMARY KEY (`shot_id`),
  KEY `team_id` (`team_id`),
  KEY `player_id` (`player_id`),
  KEY `idx_shotmap_match` (`match_id`),
  CONSTRAINT `match_shotmap_ibfk_1` FOREIGN KEY (`match_id`) REFERENCES `matches` (`match_id`),
  CONSTRAINT `match_shotmap_ibfk_2` FOREIGN KEY (`team_id`) REFERENCES `teams` (`team_id`),
  CONSTRAINT `match_shotmap_ibfk_3` FOREIGN KEY (`player_id`) REFERENCES `players` (`player_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---- match_statistics ----
CREATE TABLE `match_statistics` (
  `stat_id` bigint NOT NULL AUTO_INCREMENT,
  `match_id` bigint NOT NULL,
  `stat_group` varchar(50) NOT NULL,
  `stat_name` varchar(50) NOT NULL,
  `home_value` varchar(100) DEFAULT NULL,
  `away_value` varchar(100) DEFAULT NULL,
  `home_value_num` decimal(10,2) DEFAULT NULL,
  `away_value_num` decimal(10,2) DEFAULT NULL,
  PRIMARY KEY (`stat_id`),
  UNIQUE KEY `match_id` (`match_id`,`stat_group`,`stat_name`),
  CONSTRAINT `match_statistics_ibfk_1` FOREIGN KEY (`match_id`) REFERENCES `matches` (`match_id`)
) ENGINE=InnoDB AUTO_INCREMENT=132349 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---- match_votes ----
CREATE TABLE `match_votes` (
  `vote_id` bigint NOT NULL AUTO_INCREMENT,
  `match_id` bigint NOT NULL,
  `home_votes` int DEFAULT '0',
  `draw_votes` int DEFAULT '0',
  `away_votes` int DEFAULT '0',
  `home_percentage` decimal(5,2) DEFAULT NULL,
  `draw_percentage` decimal(5,2) DEFAULT NULL,
  `away_percentage` decimal(5,2) DEFAULT NULL,
  `fetched_at` datetime DEFAULT NULL,
  PRIMARY KEY (`vote_id`),
  KEY `match_id` (`match_id`),
  CONSTRAINT `match_votes_ibfk_1` FOREIGN KEY (`match_id`) REFERENCES `matches` (`match_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---- matches ----
CREATE TABLE `matches` (
  `match_id` bigint NOT NULL,
  `season_id` int NOT NULL,
  `competition_id` int NOT NULL,
  `round_name` varchar(50) DEFAULT NULL,
  `match_date` date NOT NULL,
  `match_time` time DEFAULT NULL,
  `home_team_id` int NOT NULL,
  `away_team_id` int NOT NULL,
  `home_score` int DEFAULT NULL,
  `away_score` int DEFAULT NULL,
  `status` enum('scheduled','live','finished','postponed') DEFAULT 'scheduled',
  `attendance` int DEFAULT NULL,
  `referee_id` int DEFAULT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`match_id`),
  KEY `competition_id` (`competition_id`),
  KEY `home_team_id` (`home_team_id`),
  KEY `away_team_id` (`away_team_id`),
  KEY `idx_matches_date` (`match_date`),
  KEY `idx_matches_season` (`season_id`),
  CONSTRAINT `matches_ibfk_1` FOREIGN KEY (`season_id`) REFERENCES `seasons` (`season_id`),
  CONSTRAINT `matches_ibfk_2` FOREIGN KEY (`competition_id`) REFERENCES `competitions` (`competition_id`),
  CONSTRAINT `matches_ibfk_3` FOREIGN KEY (`home_team_id`) REFERENCES `teams` (`team_id`),
  CONSTRAINT `matches_ibfk_4` FOREIGN KEY (`away_team_id`) REFERENCES `teams` (`team_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---- player_season_stats ----
CREATE TABLE `player_season_stats` (
  `stat_id` bigint NOT NULL AUTO_INCREMENT,
  `season_id` int NOT NULL,
  `competition_id` int NOT NULL,
  `stat_type` varchar(30) NOT NULL,
  `rank` int DEFAULT NULL,
  `player_id` bigint NOT NULL,
  `team_id` int NOT NULL,
  `goals` int DEFAULT '0',
  `assists` int DEFAULT '0',
  `appearances` int DEFAULT '0',
  `minutes_played` int DEFAULT '0',
  `xg` decimal(5,2) DEFAULT '0.00',
  `xa` decimal(5,2) DEFAULT '0.00',
  `yellow_cards` int DEFAULT '0',
  `red_cards` int DEFAULT '0',
  `rating` decimal(3,1) DEFAULT NULL,
  PRIMARY KEY (`stat_id`),
  UNIQUE KEY `season_id` (`season_id`,`stat_type`,`player_id`),
  KEY `player_id` (`player_id`),
  KEY `team_id` (`team_id`),
  CONSTRAINT `player_season_stats_ibfk_1` FOREIGN KEY (`season_id`) REFERENCES `seasons` (`season_id`),
  CONSTRAINT `player_season_stats_ibfk_2` FOREIGN KEY (`player_id`) REFERENCES `players` (`player_id`),
  CONSTRAINT `player_season_stats_ibfk_3` FOREIGN KEY (`team_id`) REFERENCES `teams` (`team_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---- players ----
CREATE TABLE `players` (
  `player_id` bigint NOT NULL,
  `name` varchar(100) NOT NULL,
  `short_name` varchar(50) DEFAULT NULL,
  `slug` varchar(100) DEFAULT NULL,
  `country_code` varchar(3) DEFAULT NULL,
  `birth_date` date DEFAULT NULL,
  `age` int DEFAULT NULL,
  `height` int DEFAULT NULL,
  `weight` int DEFAULT NULL,
  `position` varchar(50) DEFAULT NULL,
  `preferred_foot` enum('left','right','both') DEFAULT NULL,
  `current_team_id` int DEFAULT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`player_id`),
  KEY `country_code` (`country_code`),
  KEY `current_team_id` (`current_team_id`),
  CONSTRAINT `players_ibfk_1` FOREIGN KEY (`country_code`) REFERENCES `countries` (`country_code`),
  CONSTRAINT `players_ibfk_2` FOREIGN KEY (`current_team_id`) REFERENCES `teams` (`team_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---- seasons ----
CREATE TABLE `seasons` (
  `season_id` int NOT NULL,
  `competition_id` int NOT NULL,
  `year_label` varchar(20) NOT NULL,
  `year_start` int DEFAULT NULL,
  `year_end` int DEFAULT NULL,
  `start_date` date DEFAULT NULL,
  `end_date` date DEFAULT NULL,
  `is_current` tinyint(1) DEFAULT '0',
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`season_id`),
  UNIQUE KEY `competition_id` (`competition_id`,`year_label`),
  CONSTRAINT `seasons_ibfk_1` FOREIGN KEY (`competition_id`) REFERENCES `competitions` (`competition_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---- standings ----
CREATE TABLE `standings` (
  `standing_id` bigint NOT NULL AUTO_INCREMENT,
  `season_id` int NOT NULL,
  `competition_id` int NOT NULL,
  `standing_type` varchar(20) DEFAULT 'overall',
  `position` int NOT NULL,
  `team_id` int NOT NULL,
  `played` int DEFAULT '0',
  `wins` int DEFAULT '0',
  `draws` int DEFAULT '0',
  `losses` int DEFAULT '0',
  `goals_for` int DEFAULT '0',
  `goals_against` int DEFAULT '0',
  `goal_diff` int DEFAULT '0',
  `points` int DEFAULT '0',
  `form` varchar(10) DEFAULT NULL,
  PRIMARY KEY (`standing_id`),
  UNIQUE KEY `season_id` (`season_id`,`standing_type`,`team_id`),
  KEY `competition_id` (`competition_id`),
  KEY `team_id` (`team_id`),
  CONSTRAINT `standings_ibfk_1` FOREIGN KEY (`season_id`) REFERENCES `seasons` (`season_id`),
  CONSTRAINT `standings_ibfk_2` FOREIGN KEY (`competition_id`) REFERENCES `competitions` (`competition_id`),
  CONSTRAINT `standings_ibfk_3` FOREIGN KEY (`team_id`) REFERENCES `teams` (`team_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---- team_rankings ----
CREATE TABLE `team_rankings` (
  `ranking_id` bigint NOT NULL AUTO_INCREMENT,
  `team_id` int NOT NULL,
  `year` int NOT NULL,
  `ranking_type` int NOT NULL,
  `ranking_type_name` varchar(50) DEFAULT NULL,
  `ranking` int NOT NULL,
  `points` int DEFAULT '0',
  `ranking_class` varchar(50) DEFAULT NULL,
  `fetched_at` datetime DEFAULT NULL,
  PRIMARY KEY (`ranking_id`),
  UNIQUE KEY `team_id` (`team_id`,`year`,`ranking_type`),
  CONSTRAINT `team_rankings_ibfk_1` FOREIGN KEY (`team_id`) REFERENCES `teams` (`team_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---- teams ----
CREATE TABLE `teams` (
  `team_id` int NOT NULL,
  `name` varchar(100) NOT NULL,
  `short_name` varchar(50) DEFAULT NULL,
  `slug` varchar(100) DEFAULT NULL,
  `country_code` varchar(3) DEFAULT NULL,
  `city` varchar(100) DEFAULT NULL,
  `stadium` varchar(100) DEFAULT NULL,
  `founded_year` int DEFAULT NULL,
  `team_type` enum('club','national') DEFAULT 'club',
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`team_id`),
  KEY `country_code` (`country_code`),
  CONSTRAINT `teams_ibfk_1` FOREIGN KEY (`country_code`) REFERENCES `countries` (`country_code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
