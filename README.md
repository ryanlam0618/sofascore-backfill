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
