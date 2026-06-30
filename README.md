# SofaScore Backfill Scripts

Browser-first SofaScore scraping toolkit.

## Setup

1. Copy `.env.example` to `.env`
2. Fill in the proxy / local config
3. Run the browser fetch scripts

## Main scripts

- `fetch_events_browser.py` — browser-first event fetcher
- `sofascore_browser.py` — shared browser + proxy helpers
- `fetch_incidents.py` — legacy direct fetcher
- `fetch_lineups.py` — legacy direct fetcher
- `fetch_shotmap_xg.py` — legacy direct fetcher
- `orchestrator.py` — batch backfill orchestration

## Notes

- Use Playwright `page.goto()` with rotating proxy for best results.
- `__NEXT_DATA__` can contain SSR event data.
- Some endpoints are match / locale dependent.
- `timeSeconds` is mostly absent except period markers.
- Momentum graphs are per-minute, not per-second.

## Tested match

- Event `14023966` — Sunderland vs Chelsea
- Tournament `17` — Premier League
- Season `76986` — 2025/26

## Supported API endpoints

The browser client currently catalogs 102 SofaScore API endpoints across 7 categories.

Use `python3 endpoint_catalog.py` to print the same catalog from code.

### Event (22)

- `event` — `/api/v1/event/{event_id}`
- `statistics` — `/api/v1/event/{event_id}/statistics`
- `lineups` — `/api/v1/event/{event_id}/lineups`
- `incidents` — `/api/v1/event/{event_id}/incidents`
- `graph` — `/api/v1/event/{event_id}/graph`
- `managers` — `/api/v1/event/{event_id}/managers`
- `comments` — `/api/v1/event/{event_id}/comments`
- `votes` — `/api/v1/event/{event_id}/votes`
- `pregame_form` — `/api/v1/event/{event_id}/pregame-form`
- `official_tweets` — `/api/v1/event/{event_id}/official-tweets`
- `best_players_summary` — `/api/v1/event/{event_id}/best-players/summary`
- `average_positions` — `/api/v1/event/{event_id}/average-positions`
- `highlights` — `/api/v1/event/{event_id}/highlights`
- `media_summary` — `/api/v1/event/{event_id}/media/summary/country/JP`
- `odds_featured` — `/api/v1/event/{event_id}/odds/1/featured`
- `odds_all` — `/api/v1/event/{event_id}/odds/1/all`
- `provider_winning_odds` — `/api/v1/event/{event_id}/provider/1/winning-odds`
- `shotmap` — `/api/v1/event/{event_id}/shotmap`
- `player_statistics` — `/api/v1/event/{event_id}/player-statistics`
- `momentum` — `/api/v1/event/{event_id}/momentum`
- `h2h` — `/api/v1/event/{event_id}/h2h`
- `tv` — `/api/v1/event/{event_id}/tv`

### Sport (8)

- `football_categories` — `/api/v1/sport/football/categories`
- `football_categories_all` — `/api/v1/sport/football/categories/all`
- `football_live_events` — `/api/v1/sport/football/events/live`
- `football_live_tournaments` — `/api/v1/sport/football/live-tournaments`
- `football_scheduled_events` — `/api/v1/sport/football/scheduled-events/{date}`
- `football_scheduled_tournaments_page` — `/api/v1/sport/football/scheduled-tournaments/{date}/page/{page}`
- `sport_event_count` — `/api/v1/sport/{sport_id}/event-count`
- `newly_added_events` — `/api/v1/event/newly-added-events`

### Config (4)

- `country_sport_priorities_country` — `/api/v1/config/country-sport-priorities/country`
- `country_sport_priorities_country_code` — `/api/v1/config/country-sport-priorities/country/{cc}`
- `default_unique_tournaments` — `/api/v1/config/default-unique-tournaments/{cc}/football`
- `unique_tournaments_en_football` — `/api/v1/config/unique-tournaments/en/football`

### Tournament (31)

- `unique_tournament_featured_events` — `/api/v1/unique-tournament/{tournament_id}/featured-events`
- `unique_tournament_media` — `/api/v1/unique-tournament/{tournament_id}/media`
- `unique_tournament_scheduled_events` — `/api/v1/unique-tournament/{tournament_id}/scheduled-events/{date}`
- `unique_tournament_season_cuptrees` — `/api/v1/unique-tournament/{tournament_id}/season/{season_id}/cuptrees`
- `unique_tournament_season_editors` — `/api/v1/unique-tournament/{tournament_id}/season/{season_id}/editors`
- `unique_tournament_events_last` — `/api/v1/unique-tournament/{tournament_id}/season/{season_id}/events/last/{n}`
- `unique_tournament_events_next` — `/api/v1/unique-tournament/{tournament_id}/season/{season_id}/events/next/{n}`
- `unique_tournament_events_round` — `/api/v1/unique-tournament/{tournament_id}/season/{season_id}/events/round/{round}`
- `unique_tournament_groups` — `/api/v1/unique-tournament/{tournament_id}/season/{season_id}/groups`
- `unique_tournament_info` — `/api/v1/unique-tournament/{tournament_id}/season/{season_id}/info`
- `unique_tournament_player_of_season` — `/api/v1/unique-tournament/{tournament_id}/season/{season_id}/player-of-the-season`
- `unique_tournament_player_of_season_race` — `/api/v1/unique-tournament/{tournament_id}/season/{season_id}/player-of-the-season-race`
- `unique_tournament_player_statistics_types` — `/api/v1/unique-tournament/{tournament_id}/season/{season_id}/player-statistics/types`
- `unique_tournament_power_rankings_round` — `/api/v1/unique-tournament/{tournament_id}/season/{season_id}/power-rankings/round/{round}`
- `unique_tournament_power_rankings_rounds` — `/api/v1/unique-tournament/{tournament_id}/season/{season_id}/power-rankings/rounds`
- `unique_tournament_rounds` — `/api/v1/unique-tournament/{tournament_id}/season/{season_id}/rounds`
- `unique_tournament_standings_home` — `/api/v1/unique-tournament/{tournament_id}/season/{season_id}/standings/home`
- `unique_tournament_standings_total` — `/api/v1/unique-tournament/{tournament_id}/season/{season_id}/standings/total`
- `unique_tournament_statistics_info` — `/api/v1/unique-tournament/{tournament_id}/season/{season_id}/statistics/info`
- `unique_tournament_team_events_total` — `/api/v1/unique-tournament/{tournament_id}/season/{season_id}/team-events/total`
- `unique_tournament_team_of_periods_rated` — `/api/v1/unique-tournament/{tournament_id}/season/{season_id}/team-of-the-period/periods/rated`
- `unique_tournament_team_statistics_types` — `/api/v1/unique-tournament/{tournament_id}/season/{season_id}/team-statistics/types`
- `unique_tournament_team_performance_graph` — `/api/v1/unique-tournament/{tournament_id}/season/{season_id}/team/{team_id}/team-performance-graph-data`
- `unique_tournament_top_teams_overall` — `/api/v1/unique-tournament/{tournament_id}/season/{season_id}/top-teams/overall`
- `unique_tournament_trending_top_players` — `/api/v1/unique-tournament/{tournament_id}/season/{season_id}/trending-top-players`
- `unique_tournament_venues` — `/api/v1/unique-tournament/{tournament_id}/season/{season_id}/venues`
- `unique_tournament_seasons` — `/api/v1/unique-tournament/{tournament_id}/seasons`
- `tournament_scheduled_events` — `/api/v1/tournament/{tournament_id}/scheduled-events/{date}`
- `tournament_standings_home` — `/api/v1/tournament/{tournament_id}/season/{season_id}/standings/home`
- `tournament_standings_total` — `/api/v1/tournament/{tournament_id}/season/{season_id}/standings/total`
- `tournament_team_events_total` — `/api/v1/tournament/{tournament_id}/season/{season_id}/team-events/total`

### Team (18)

- `team_achievements` — `/api/v1/team/{team_id}/achievements`
- `team_events_last` — `/api/v1/team/{team_id}/events/last/{n}`
- `team_events_next` — `/api/v1/team/{team_id}/events/next/{n}`
- `team_featured_event` — `/api/v1/team/{team_id}/featured-event`
- `team_featured_players` — `/api/v1/team/{team_id}/featured-players`
- `team_media_summary` — `/api/v1/team/{team_id}/media/summary/country/{cc}`
- `team_media_videos` — `/api/v1/team/{team_id}/media/videos`
- `team_official_tweets` — `/api/v1/team/{team_id}/official-tweets`
- `team_performance` — `/api/v1/team/{team_id}/performance`
- `team_player_statistics_seasons` — `/api/v1/team/{team_id}/player-statistics/seasons`
- `team_season_best_result` — `/api/v1/team/{team_id}/season/{season_id}/best-result`
- `team_standings_seasons` — `/api/v1/team/{team_id}/standings/seasons`
- `team_team_statistics_seasons` — `/api/v1/team/{team_id}/team-statistics/seasons`
- `team_ranks_overall` — `/api/v1/team/{team_id}/unique-tournament/{tournament_id}/season/{season_id}/ranks/overall`
- `team_statistics_overall` — `/api/v1/team/{team_id}/unique-tournament/{tournament_id}/season/{season_id}/statistics/overall`
- `team_top_players_overall` — `/api/v1/team/{team_id}/unique-tournament/{tournament_id}/season/{season_id}/top-players/overall`
- `team_unique_tournaments_all` — `/api/v1/team/{team_id}/unique-tournaments/all`
- `team_year_statistics` — `/api/v1/team/{team_id}/year-statistics/{year}`

### Player (1)

- `player_attribute_overviews` — `/api/v1/player/{player_id}/attribute-overviews`

### Misc (18)

- `country_alpha2` — `/api/v1/country/alpha2`
- `tv_country_channels` — `/api/v1/tv/event/{event_id}/country-channels`
- `fantasy_event` — `/api/v1/fantasy/event/{event_id}`
- `team_of_the_period` — `/api/v1/team-of-the-period/{team_of_period_id}`
- `translation_description` — `/api/v1/translation/description/{description_id}/language/en`
- `odds_providers_web` — `/api/v1/odds/providers/{cc}/web`
- `odds_providers_web_featured` — `/api/v1/odds/providers/{cc}/web-featured`
- `odds_providers_web_odds` — `/api/v1/odds/providers/{cc}/web-odds`
- `odds_featured_events_football` — `/api/v1/odds/{odds_id}/featured-events/football`
- `offers_banner_team` — `/api/v1/offers/banner/team/{team_id}/{cc}/en`
- `sofascore_news_event_posts` — `/api/v1/sofascore-news/en/event/{event_id}/posts/{post_id}`
- `sofascore_news_posts` — `/api/v1/sofascore-news/en/posts`
- `sofascore_news_team_posts` — `/api/v1/sofascore-news/en/team/{team_id}/posts/{post_id}`
- `sofascore_news_tournament_posts` — `/api/v1/sofascore-news/en/tournament/{tournament_id}/posts/{post_id}`
- `branding_providers_web` — `/api/v1/branding/providers/{cc}/web`
- `event_ai_insights` — `/api/v1/event/{event_id}/ai-insights/en`
- `event_win_probability` — `/api/v1/event/{event_id}/graph/win-probability`
- `event_video_highlights_extended` — `/api/v1/event/{event_id}/sport-video-highlights/country/{cc}/extended`
