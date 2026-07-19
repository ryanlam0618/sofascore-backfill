# SofaScore Backfill

Browser-first data collection toolkit for building a 10-year football dataset from SofaScore into SQLite and MySQL.

This repo is organized around one practical goal: reliably enumerate competitions, seasons, and events, fetch match and team data through a warmed browser session to reduce 403 blocking, and persist normalized results for downstream analysis.

## What this repo does

- Discovers valid SofaScore season IDs for supported competitions
- Enumerates rounds and events for league, cup, and international competitions
- Fetches match-level data such as incidents, attendance, referees, managers, shotmaps, and related-match/H2H data
- Fetches competition/team-level data such as standings, player season stats, and team rankings
- Stores intermediate/local results in SQLite and supports normalized MySQL writes into `appdb`
- Provides orchestration scripts for large multi-season backfills and resumable progress tracking

## Current architecture

There are effectively **two backfill paths** in the repo:

### 1) `backfill_runner.py` — the newer end-to-end pipeline
This is the clearest current implementation of the browser-first → normalized storage flow.

Flow:
1. warm homepage / tournament / event in Playwright
2. fetch SofaScore API endpoints through the browser session
3. parse payloads into normalized records
4. write directly into MySQL
5. track progress per season so interrupted runs can resume

Best for:
- understanding the intended long-term architecture
- direct MySQL ingestion
- seeing the normalized data model in action

### 2) `orchestrator.py` + `fetch_*.py` — the operational batch toolkit
This is the broader script runner for large backfills and recovery runs.

Flow:
1. discover season IDs
2. run fetch scripts by phase / dataset
3. persist results in SQLite per dataset/script
4. optionally migrate or sync results into MySQL

Best for:
- modular extraction by data type
- running one data family at a time
- experimentation and recovery when one script fails
- operational monitoring with state/log files and watcher helpers

Important caveat:
- `orchestrator.py` is currently more **operationally complete** than `main.py` for running the script-based toolkit end to end.
- `main.py` exposes a polished unified CLI, but its phase 2/3 backfill path is still marked not implemented in this version.

## Repo map

### Entry points
- `main.py` — unified CLI wrapper for discover / backfill / status / resume / verify / backup, but not every advertised path is fully implemented
- `backfill_runner.py` — primary end-to-end Playwright → MySQL pipeline
- `orchestrator.py` — multi-phase batch orchestrator for the individual fetch scripts
- `discover_seasons.py` — discovers season IDs per competition and writes `discoveries.json`

### Shared browser / endpoint layer
- `sofascore_browser.py` — shared Playwright browser client, session warming, SSR extraction, and endpoint bundle helpers
- `anti_block.py` — headers / jitter / anti-block helpers
- `browser_discovery_bridge.py` — bridges discovered events into fetch script runs
- `endpoint_catalog.py` — endpoint reference/constants used by browser-first fetch flows

### Data fetchers / backfill helpers
- `fetch_incidents.py` — goals/cards/substitutions
- `fetch_attendance.py` — match attendance
- `fetch_referees.py` — referee data
- `fetch_managers.py` — manager data
- `fetch_player_stats.py` — player season stats
- `fetch_related_matches.py` — H2H / related match data
- `fetch_shotmap_xg.py` — shot/xG aggregate layer
- `fetch_shotmap_details.py` — detailed per-shot rows
- `fetch_standings.py` — league standings
- `fetch_team_rankings.py` — team ranking data
- `fetch_events_browser.py` — browser-first event fetch/testing utility
- `backfill_incident_metadata.py` — metadata-oriented backfill helper for incident/event coverage

### Schema / storage / migration
- `mysql_schema.sql` — older schema
- `mysql_schema_v2.sql` — newer normalized schema for competitions, seasons, matches, incidents, shotmap, standings, rankings, etc.
- `mysql_helpers.py` — shared MySQL helpers
- `sqlite_to_mysql.py` — migration utilities from SQLite outputs to MySQL
- `migrate_attendance.py` — attendance migration helper
- `smoke_test_mysql.py` — simple MySQL write smoke test

### Operational / diagnostic files
- `competitions_10y.yaml` — curated competition configuration
- `discoveries.json` — discovered season metadata
- `DEEP_DIVE_2026-05-07.md` — investigation notes on hidden/secondary data sources
- `run_all_backfill.py`, `run_attendance.py`, `retry_failed_metadata.py`, `retry_seq.py`, `orchestrator_watcher.py` — operational helpers / drivers
- `survey_403.py`, `test_network_intercept.py`, `test_old_browser_scrape.py`, `test_random_matches_finance.py`, `data/test_random_matches_runner.py` — diagnostic / exploratory scripts
- `proxy_list.txt` — proxy list/input file for operations

## Supported competitions

The repo is configured around a 10-year backfill window for a curated set of football competitions, including:

- Top European leagues: Premier League, La Liga, Serie A, Bundesliga, Ligue 1
- Asian / APAC leagues: J1 League, K League 1, A-League Men, Chinese Super League
- UEFA competitions: UCL, UEL, UECL
- AFC competitions: AFC Champions League, AFC Champions League Two
- Domestic cups such as FA Cup, EFL Cup, Copa del Rey, Coppa Italia, Coupe de France, DFB Pokal, J.League Cup, Emperor's Cup, Australia Cup, Chinese FA Cup

Competition metadata lives in `competitions_10y.yaml`. Discovered season IDs live in `discoveries.json`.

## Data model at a glance

The normalized MySQL schema (`mysql_schema_v2.sql`) centers on:

- reference tables: `competitions`, `seasons`, `teams`, `players`, `countries`
- match core: `matches`
- match detail: `match_incidents`, `match_shotmap`, `match_player_stats`, `match_momentum`, `match_h2h`
- competition/team detail: `standings`, `player_season_stats`, `team_rankings`
- operational logging: `fetch_log`

This makes the repo useful both for raw collection and for analytics-friendly downstream storage.

## Browser-first strategy

A repeated pattern across the repo is:

1. open a real browser session with Playwright
2. optionally route through rotating Webshare proxy credentials
3. warm a homepage / tournament page / event page first
4. call SofaScore API endpoints via the in-page session
5. parse the JSON and persist it

Why this exists:
- direct API access is often rate-limited or blocked with 403s
- some useful data is present in SSR hydration (`__NEXT_DATA__`) even when a public endpoint is weak or absent
- the browser session provides more stable access than naive server-side HTTP requests

`sofascore_browser.py` is the shared implementation of this approach.

## What the deep-dive notes say

`DEEP_DIVE_2026-05-07.md` captures a few important findings:

- **referees** are available directly from event data / referee endpoints
- **managers** are available directly from team / manager endpoints
- **attendance** is available directly from event data
- **team rankings** appear scrapeable from team page `__NEXT_DATA__` even when direct ranking endpoints are inconsistent
- **historical squads** remain unresolved and may require reconstruction from lineups + transfers rather than a clean endpoint

That note is worth reading before extending the repo.

## Environment and dependencies

At minimum, the code expects:

- Python 3
- Playwright with Chromium available
- access to a `.env` file for proxy and MySQL credentials
- optional MySQL target database (`appdb`)

### Proxy environment variables

The repo currently uses **two proxy env styles**:

1. URL-style, used by `sofascore_browser.py` and `.env.example`
   - `SOFA_PROXY`
   - `SOFA_PROXY_USERNAME`
   - `SOFA_PROXY_PASSWORD`

2. split host/port/user/pass style, used by `backfill_runner.py`
   - `SOFA_PROXY_HOST`
   - `SOFA_PROXY_PORT`
   - `SOFA_PROXY_USER`
   - `SOFA_PROXY_PASS`

MySQL-related variables used in the repo include:
- `MYSQL_HOST`
- `MYSQL_PORT`
- `MYSQL_USER`
- `MYSQL_PASSWORD`
- `MYSQL_DATABASE`

If you are setting this repo up today, prefer matching `.env.example` first, then check the script you plan to run.

## Suggested setup

1. Create and populate `.env`
2. Install Python dependencies used by the scripts
3. Install Playwright browser binaries if not already installed
4. Verify MySQL connectivity if you plan to write to `appdb`
5. Run season discovery before large backfills

Because dependency management is not fully standardized in the repo, check imports in the scripts you plan to run. The main runtime dependencies are Playwright, `python-dotenv`, and `mysql-connector-python` for MySQL-writing flows.

## Typical workflows

### A) Discover seasons
```bash
python3 discover_seasons.py --all
```

Or for one competition:
```bash
python3 discover_seasons.py --name "Premier League"
```

### B) Inspect current status
```bash
python3 main.py status
```

Or, for the script-based runner:
```bash
python3 orchestrator.py --status
```

### C) Dry-run a competition backfill
```bash
python3 backfill_runner.py --competition "Premier League" --from-year 2024 --dry-run
```

Or through the wrapper:
```bash
python3 main.py backfill --competition "Premier League" --dry-run
```

### D) Run the newer end-to-end pipeline
```bash
python3 backfill_runner.py --competition "Premier League" --from-year 2024
```

Useful throttles for testing:
```bash
python3 backfill_runner.py --competition "Premier League" --limit-rounds 2 --limit-events 20
```

### E) Run the script-based orchestrator
```bash
python3 orchestrator.py --competition "Premier League"
```

Dry run:
```bash
python3 orchestrator.py --competition "Premier League" --dry-run
```

All configured competitions:
```bash
python3 orchestrator.py --all
```

Resume / status helpers:
```bash
python3 orchestrator.py --resume
python3 orchestrator.py --status
```

## Output locations

Common output paths:

- `discoveries.json` — discovered seasons and metadata
- `data/backfill_sofascore_10y/` — SQLite files, progress files, orchestrator state
- `data/logs/` — per-script / per-competition logs

Typical generated files include:
- `*_state.json`
- `progress_<competition>_<season>.json`
- per-dataset SQLite databases
- `orchestrator_state.json`

## Current strengths

- Good coverage of football data families relevant to match analytics
- Clear browser-first anti-403 strategy
- Competition discovery is already codified
- Normalized MySQL schema is substantially better than the older ad-hoc/raw shape
- Both modular script mode and end-to-end pipeline mode exist

## Current rough edges / things to know before editing

This repo is useful, but it is **not yet fully unified**. A few mismatches show up when reading the code:

- there are two orchestration approaches (`backfill_runner.py` and `orchestrator.py`)
- `orchestrator.py` still advertises deleted legacy files like `fetch_lineups.py` and `fetch_statistics.py` in its header docstring
- `main.py` presents a polished unified CLI, but parts of phase 2/3 are still marked not implemented there
- proxy environment variable naming is inconsistent across files
- some operational helpers look older than the newer normalized/MySQL-first path
- several diagnostic/test scripts live beside production scripts, so the repo map is broader than the main runtime path

So the repo should be read as **working infrastructure plus active refactor/migration**, not as a fully finished product.

## Recommended starting points for a new contributor

If you want to understand the code quickly, read in this order:

1. `README.md`
2. `main.py`
3. `backfill_runner.py`
4. `sofascore_browser.py`
5. `discover_seasons.py`
6. `mysql_schema_v2.sql`
7. `orchestrator.py`
8. one or two `fetch_*.py` scripts relevant to the data you care about

If you want to run something safely, start with:

1. `discover_seasons.py --name "Premier League"`
2. `backfill_runner.py --competition "Premier League" --limit-rounds 1 --limit-events 5`
3. inspect generated progress / logs / MySQL writes

## Recommended cleanup directions

If this repo is being actively developed, the most valuable follow-up improvements would be:

1. unify on one primary orchestration path
2. standardize proxy env names
3. document Python dependencies explicitly in a requirements file or pyproject
4. align `main.py`, `orchestrator.py`, and the fetch scripts so the wrapper matches reality
5. decide whether SQLite is only a staging layer or still a first-class storage target
6. clean up outdated docstrings/comments and separate diagnostic scripts from main runtime paths where helpful

## Quick verdict

This is a serious browser-first SofaScore backfill repo with:
- real competition/season discovery
- practical anti-block tactics
- normalized MySQL ingestion
- broad football coverage
- some unfinished refactor seams between old modular scripts and newer unified pipeline code

If you only need one mental model, treat **`backfill_runner.py` + `sofascore_browser.py` + `mysql_schema_v2.sql`** as the core of the current design, and treat the rest as supporting extraction/orchestration tooling around it.
