# SofaScore Backfill Scripts

Automated data collection from SofaScore API using direct `urllib` (stdlib) — no browser automation required.

## Scripts

| Script | Description | Status |
|--------|-------------|-------|
| `fetch_standings.py` | Tournament standings (total/home/away) | ✅ Working |
| `fetch_player_stats.py` | Player rankings by stat (goals, assists, etc.) | ✅ Working |
| `fetch_related_matches.py` | Head-to-head aggregated stats | ✅ Working |
| `fetch_team_rankings.py` | Team world/UEFA/home/away rankings from team page HTML | ✅ Working |
| `fetch_incidents.py` | Match events timeline (goals, cards, VAR, etc.) | ✅ Working |
| `fetch_shotmap_xg.py` | Shotmap xG aggregates per match | ✅ Working |
| `fetch_shotmap_details.py` | Per-shot detailed shotmap rows | ✅ Working |
| `fetch_lineups.py` | Match lineup (starting XI, bench, captain) | ✅ Working |
| `fetch_managers.py` | Manager profiles + current club from team API | ✅ Working |
| `fetch_referees.py` | Referee profiles from event + profile API | ✅ Working |
| `fetch_attendance.py` | Match attendance from event API | ✅ Working |
| `discover_seasons.py` | Discover available seasons per category | ✅ Utility |
| `orchestrator.py` | Orchestrate batch backfill across scripts | ✅ Utility |
| `smoke_test_mysql.py` | MySQL connectivity smoke test | ✅ Utility |
| `survey_403.py` | Survey 403 patterns for lineups/shotmap | ✅ Utility |

### Deprecated / No direct API
| Script | Reason | Status |
|--------|--------|--------|
| `fetch_statistics.py` | API returns limited data; needs review | ⚠️ Review |
| `fetch_team_rankings.py` (old) | Direct ranking API returns 404 | ❌ Replaced |

## Data Types

**Core:** events, standings, player stats, H2H  
**Match-level:** incidents, shotmap xG, shotmap details, lineups, attendance  
**Entity:** team rankings, managers, referees  

## Usage

```bash
# Smoke test (SQLite)
python3 fetch_incidents.py --event-id 14025013
python3 fetch_managers.py --team-id 42
python3 fetch_referees.py --referee-id 69853
python3 fetch_attendance.py --event-id 14025013
python3 fetch_team_rankings.py --team-id 42 44

# Batch backfill (SQLite)
python3 fetch_incidents.py --db data/backfill_sofascore_10y/incidents_PL.sqlite

# MySQL (set USE_MYSQL=1 or --use-mysql)
USE_MYSQL=1 python3 fetch_incidents.py --db appdb
```

## Architecture

- All scripts use stdlib `urllib` — no external dependencies needed.
- Resume-safe with `state.json` + SQLite or MySQL.
- MySQL via `mysql_helpers.py` (credentials from `~/.openclaw/workspace/.env`).
- Team rankings scrape `__NEXT_DATA__` hydration JSON from team web pages.
- All other scripts use direct API endpoints under `https://www.sofascore.com/api/v1/`.

## MySQL Setup

```bash
mysql -u A100 -p appdb < mysql_schema.sql
```
Or call `ensure_mysql_tables()` from `mysql_helpers.py`.

## Key Endpoints

- Events: `/tournament/{cat_id}/season/{season_id}/events`
- Standings: `/tournament/{cat_id}/season/{season_id}/standings`
- Player stats: `/unique-tournament/{ut_id}/season/{season_id}/player-stats`
- Incidents: `/event/{event_id}/incidents`
- Shotmap: `/event/{event_id}/shotmap`
- Lineups: `https://widgets.sofascore.com/embed/lineups?id={event_id}`
- Team: `/team/{team_id}` → contains `manager`, `venue`
- Team players: `/team/{team_id}/players`
- Manager profile: `/manager/{manager_id}`
- Referee profile: `/referee/{referee_id}`
- Team rankings: `https://www.sofascore.com/team/football/{slug}/{team_id}` (HTML `__NEXT_DATA__`)

## Known Limitations

- **Historical squads:** no direct API; reconstruction from lineups across season possible but not yet implemented.
- **Team rankings:** no direct API; scraped from HTML `__NEXT_DATA__` on team pages.
- Old 7-digit event IDs (pre-2024) return 404 — discover new IDs via `/tournament/.../events`.
- Lineups and shotmap may occasionally return 403 (anti-bot); script handles with retry + backoff.