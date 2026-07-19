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

The supported production path is intentionally small:

### `backfill_runner.py` — the production backfill entry point
This is the browser-first -> normalized MySQL pipeline. The old modular fetch/orchestrator scripts are retained in Git history, but are not part of the supported v2 runtime.

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

The supported flow is:
1. discover or verify season IDs
2. load the competition and season configuration
3. warm a browser session and enumerate rounds/events
4. fetch event bundles with retry and self-healing browser recovery
5. normalize and write records to MySQL
6. persist per-season progress so failed events can be resumed

The deleted legacy scripts are intentionally not documented as runnable entry points.

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

Configuration status is not the same as live URL validation: the YAML currently defines 24 target competitions, while the checked-in discovery snapshot currently contains 23 competitions. The discovery snapshot records only one event endpoint check (`Premier League`, season `76986`); it is not evidence that every 10-year season URL returns HTTP 200.

## Data model at a glance

The normalized MySQL schema (`mysql_schema_v2.sql`) centers on:

- reference tables: `competitions`, `seasons`, `teams`, `players`, `countries`
- match core: `matches`
- match detail: `match_incidents`, `match_shotmap`, `match_player_stats`, `match_momentum`, `match_h2h`
- competition/team detail: `standings`, `player_season_stats`, `team_rankings`
- operational logging: `fetch_log`

This makes the repo useful both for raw collection and for analytics-friendly downstream storage.

## Validation status

The repository has proven the core pipeline on real SofaScore requests and partial backfill runs. It has not yet completed a 24-competition x 10-year URL audit. Before a full run, validate each target season with the browser path and record HTTP status, redirect/failure reason, and the timestamp of the check. Treat `discoveries.json` as a progress snapshot, not as a completed audit.

Recommended gates:

```bash
python3 -m py_compile backfill_runner.py discover_seasons.py
python3 backfill_runner.py --competition "Premier League" --season-id 76986 --limit-rounds 1 --limit-events 1 --dry-run
python3 backfill_runner.py --competition "Premier League" --season-id 76986 --limit-rounds 1 --limit-events 1
```

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

Runtime dependencies for the supported runner are listed in `requirements.txt`: Playwright, `python-dotenv`, PyYAML, and `mysql-connector-python`.

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

Common local output paths:

- `discoveries.json` — checked-in discovery snapshot
- `data/backfill_sofascore_10y/` — local progress files and runtime state
- `logs/` — local backfill logs

Runtime outputs are intentionally ignored by Git. Do not commit credentials, proxy lists, logs, captured payloads, local databases, or virtual environments.

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

- The supported runner is operational, but full 24-competition / 10-year coverage has not been live-validated.
- SofaScore 403s and connection failures remain possible; retry and browser recovery reduce but do not eliminate them.
- `discoveries.json` and runtime progress files are snapshots and can become stale.
- Keep credentials, proxies, logs, captured payloads, SQLite files, and virtual environments local; they are ignored by Git.
- The repository is an active backfill project, not a claim of completed full coverage.

## Recommended starting points for a new contributor

Read in this order:

1. `README.md`
2. `backfill_runner.py`
3. `competitions_10y.yaml`
4. `discoveries.json`
5. `mysql_schema_v2.sql`
6. `sofascore_browser.py`
7. `discover_seasons.py`

If you want to run something safely, start with:

1. `discover_seasons.py --name "Premier League"`
2. `backfill_runner.py --competition "Premier League" --limit-rounds 1 --limit-events 5`
3. inspect generated progress / logs / MySQL writes

## Recommended next work

1. Add a dedicated browser URL audit that covers every configured competition and season and writes a timestamped, reproducible report.
2. Reconcile the 24-entry YAML target with the 23-entry discovery snapshot, including AFC Champions League Two.
3. Retry and classify failed events after the URL audit, separating anti-bot failures from parser/database failures.
4. Keep the production runner and diagnostics separate from historical legacy code.

## Quick verdict

This is a serious browser-first SofaScore backfill repo with:
- real competition/season discovery
- practical anti-block tactics
- normalized MySQL ingestion
- broad football coverage
- some unfinished refactor seams between old modular scripts and newer unified pipeline code

If you only need one mental model, treat **`backfill_runner.py` + `sofascore_browser.py` + `mysql_schema_v2.sql`** as the core of the current design, and treat the rest as supporting extraction/orchestration tooling around it.
