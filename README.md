# SofaScore Backfill Scripts

Automated data collection from SofaScore API using direct `urllib` (stdlib) — no browser automation required.

## Scripts

| Script | Description | Status |
|--------|-------------|--------|
| `fetch_standings.py` | Tournament standings (total/home/away) | ✅ Working |
| `fetch_player_stats.py` | Player rankings by stat (goals, assists, etc.) | ✅ Working |
| `fetch_related_matches.py` | Head-to-head aggregated stats | ✅ Working |
| `fetch_team_rankings.py` | Team world/regional rankings | ❌ No public API |
| `fetch_incidents.py` | Match events timeline (goals, cards, VAR, etc.) | ✅ Working — NEW |
| `fetch_shotmap_xg.py` | Shotmap xG aggregates per match | ✅ Working — NEW |
| `fetch_shotmap_details.py` | Per-shot detailed shotmap rows | ✅ Working — NEW |
| `fetch_lineups.py` | Match lineups (starting XI, subs) | ✅ Working — NEW |

## Usage

```bash
# Incidents for PL 24/25 season (past matches only)
python3 fetch_incidents.py --category-id 1 --season-id 61627 --limit 100

# Shotmap xG for PL
python3 fetch_shotmap_xg.py --category-id 1 --season-id 61627 --limit 100

# Detailed shotmap rows (depends on shotmap_xg.sqlite)
python3 fetch_shotmap_details.py --source-db data/backfill_sofascore_10y/shotmap_xg.sqlite --limit 20

# Match lineups
python3 fetch_lineups.py --category-id 1 --season-id 61627 --limit 50

# Existing scripts
python3 fetch_standings.py --category-id 1 --all
python3 fetch_player_stats.py --all --all-stats
python3 fetch_related_matches.py --event-id 14025013
```

## API Base

`https://www.sofascore.com/api/v1/`

## Confirmed Working Endpoints (2026-05-03)

| Endpoint | Status | Notes |
|----------|--------|-------|
| `GET /tournament/{category_id}/season/{season_id}/standings/{type}` | ✅ 200 | type: total, home, away |
| `GET /tournament/{category_id}/seasons` | ✅ 200 | Season IDs list |
| `GET /unique-tournament/{ut_id}/season/{season_id}/players?order_by={stat}&limit=50` | ✅ 200 | Player rankings |
| `GET /player/{player_id}/statistics` | ✅ 200 | Full player season stats |
| `GET /event/{event_id}/h2h` | ✅ 200 | Head-to-head aggregated stats |
| `GET /event/{event_id}` | ✅ 200 | Event details, venue data |
| `GET /event/{event_id}/incidents` | ✅ 200 | Match events timeline |
| `GET /event/{event_id}/shotmap` | ✅ 200 | Shotmap with xG data |
| `GET /event/{event_id}/lineups` | ✅ 200 | Match lineups |
| `GET /search/{query}` | ✅ 200 | Search (new player IDs) |
| `GET /team/{team_id}` | ✅ 200 | Team info |
| `GET /player/{player_id}` | ✅ 200 | Player profile |

## NOT Working (404 / Browser Required)

| Endpoint | Reason |
|----------|--------|
| `GET /team/{team_id}/squad` | Returns 404 — no public squad endpoint |
| `GET /team/{team_id}/events` | Returns 404 |

## Migration Notes (from old TakeData/sofa_score/)

The following scripts from the old repo were **evaluated but NOT migrated**:

| Old Script | Reason for Skip |
|------------|-----------------|
| `fetch_tournament_metadata.py` | Heavy DrissionPage browser dependency; relies on cross-table JOINs that don't exist in new repo DB |
| `fetch_player_profiles.py` | DrissionPage browser required; uses player_season_stats table not available in new repo |
| `fetch_squads.py` | `/team/{id}/squad` API returns 404 — no public squad endpoint exists |
| `fetch_attendance.py` | DrissionPage required; attendance data not reliably available in direct API event response |
| `fetch_managers.py` | DrissionPage required; relies on matches table JOINs not available in new repo |
| `fetch_referees.py` | DrissionPage required; referee data not exposed in direct API event response |
| `fetch_venues.py` | DrissionPage required; venue data available in `/event/{id}` but discovery relies on old DB schema |
| `shotmap_xg_backfill.py` | **Migrated** — `/event/{id}/shotmap` works directly via urllib; rewritten as `fetch_shotmap_xg.py` |
| `shotmap_detail_backfill.py` | **Migrated** — same endpoint; rewritten as `fetch_shotmap_details.py` |

**Key findings from API testing (2026-05-03):**
- Old event IDs (e.g. 18806708) no longer valid — new IDs are 14-digit (e.g. 14025013)
- Event discovery via `GET /tournament/{category_id}/season/{season_id}/events` returns 382+ events
- The `related_matches` in `fetch_related_matches.py` uses `/event/{id}/h2h` which works for aggregate stats (teamDuel) but `allEvents` array is empty

## Known Limitations

- **Squad data**: No public squad/roster endpoint. Historical season squads unavailable via API.
- **Referee data**: Not exposed in direct API responses.
- **Manager data**: Not reliably available in public API.
- **Player IDs**: Old numeric IDs are deprecated. Use `/search/{name}` to find current IDs.
- **Shotmap details**: Coordinate system (player_x, player_y) — verify scale with known shots.
- **Lineups**: `confirmed` flag only present if SofaScore has confirmed the lineup; pre-match lineups may be "unconfirmed".
- **Incidents**: `relatedPlayer` and `assist` fields may be null for some event types.

## Dependencies

None required — uses Python stdlib (`urllib`, `sqlite3`, `json`, `argparse`, `pathlib`, `datetime`).
No DrissionPage, no browser, no pip installs needed.

```bash
pip install DrissionPage  # optional — NOT needed for scripts in this repo
```

## Data Directory

All SQLite DBs and state JSON files are stored under `data/backfill_sofascore_10y/`.

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