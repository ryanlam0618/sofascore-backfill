# SofaScore Backfill Scripts

Automated data collection from SofaScore API.

## Scripts

| Script | Description | Status |
|--------|-------------|--------|
| `fetch_standings.py` | Tournament standings (total/home/away) | ✅ Working |
| `fetch_player_stats.py` | Player rankings by stat (goals, assists, etc.) | ✅ Working |
| `fetch_related_matches.py` | Head-to-head match history | ✅ Working |
| `fetch_team_rankings.py` | Team world/regional rankings | ⚠️ No direct API |

## Usage

```bash
# Standings for Premier League (all types: total, home, away)
python3 fetch_standings.py --category-id 1 --all

# Player top scorers for PL
python3 fetch_player_stats.py --category-id 1 --stat-type goals

# All stat types for multiple leagues
python3 fetch_player_stats.py --all --all-stats

# H2H for specific event IDs
python3 fetch_related_matches.py --event-id 10210001

# Data goes to data/backfill_sofascore_10y/*.sqlite
```

## API Base

`https://www.sofascore.com/api/v1/`

## Confirmed Working Endpoints

- `GET /tournament/{category_id}/season/{season_id}/standings/{type}` — standings (type: total, home, away)
- `GET /tournament/{category_id}/seasons` — season IDs
- `GET /unique-tournament/{ut_id}/season/{season_id}/players?order_by={stat}&limit=50` — player rankings
- `GET /player/{player_id}/statistics` — full player season stats
- `GET /event/{event_id}/h2h` — head-to-head matches
- `GET /player/{player_id}` — player profile
- `GET /event/{event_id}` — event details
- `GET /event/{event_id}/h2h` — head-to-head
- `GET /search/{query}` — search (reveals new player IDs)

## Tournament ID Mappings

| League | category_id | uniqueTournament_id |
|--------|-------------|---------------------|
| Premier League | 1 | 17 |
| La Liga | 8 | 8 |
| Serie A | 23 | 23 |
| Bundesliga | 9 | 9 |
| Ligue 1 | 4 | 34 |
| UCL | 7 | 7 |
| Europa League | 459 | 459 |

## Known Issues

- **Team rankings**: No public API endpoint found. All tested patterns return 404. Would need browser automation or paid data provider.
- **Player stats**: The `players` endpoint returns basic info (name, team) but not actual stat values. Stats are available per-player via `/player/{id}/statistics`.
- **Player IDs**: Old numeric IDs are deprecated. Use `/search/{name}` to find new IDs.

## Dependencies

```
pip install DrissionPage  # optional, for browser-based scripts
```

The scripts in this repo use `urllib` (stdlib) for direct API calls and don't require DrissionPage.
