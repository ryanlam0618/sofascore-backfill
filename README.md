# SofaScore Backfill Scripts

Browser-first SofaScore data collection with Playwright + rotating proxy.

## Overview

SofaScore has strong anti-bot protection:
- Direct HTTP (`curl` / `requests` / stdlib `urllib`) -> usually 403
- `page.request.get()` / `fetch()` -> 403
- **Most reliable method**: Playwright `page.goto()` + Webshare rotating proxy
- **Useful bonus**: some match data is available in SSR / `__NEXT_DATA__`

### Verified data precision

| Data type | Precision | Example |
|---|---:|---|
| Goals / cards / substitutions | minute-level | 25', 50', 56' |
| Match statistics | full-match aggregate | possession 44% vs 56% |
| Player statistics | full-match aggregate | xG, xA, xGOT |
| Momentum / graph | per-minute | minute 20 = +56 |

## Browser strategy

### Proxy

```bash
Server: http://p.webshare.io:80
Username: aeptenjc-rotate
Password: dztr57tcycoz
```

### Main pattern

```javascript
const { chromium } = require('playwright');

const browser = await chromium.launch({
  headless: true,
  proxy: {
    server: 'http://p.webshare.io:80',
    username: 'aeptenjc-rotate',
    password: 'dztr57tcycoz'
  }
});

const context = await browser.newContext();
const page = await context.newPage();

// Load the match page first to warm cookies / session state.
await page.goto('https://www.sofascore.com/event/14023966', {
  waitUntil: 'networkidle',
  timeout: 60000
});

// Then open API endpoints with page.goto()
const resp = await page.goto('https://www.sofascore.com/api/v1/event/14023966/statistics', {
  waitUntil: 'domcontentloaded',
  timeout: 10000
});
const data = JSON.parse(await page.evaluate(() => document.body.innerText));

// SSR extraction
const nextData = await page.evaluate(() => {
  const script = document.querySelector('#__NEXT_DATA__');
  return script ? JSON.parse(script.textContent) : null;
});
```

### Supported page data sources

1. **SSR / `__NEXT_DATA__`**: event, incidents, meta
2. **API JSON**: statistics, lineups, incidents, player/team/tournament pages
3. **Graph endpoint**: `/api/v1/event/{event_id}/graph` for per-minute momentum

## Discovered endpoints

> These were verified by browser capture and `page.goto()` testing. Some endpoints may still return 404 depending on match / language / country parameters.

### EVENT
- `/api/v1/event/{event_id}`
- `/api/v1/event/{event_id}/statistics`
- `/api/v1/event/{event_id}/lineups`
- `/api/v1/event/{event_id}/incidents`
- `/api/v1/event/{event_id}/graph`
- `/api/v1/event/{event_id}/h2h`
- `/api/v1/event/{event_id}/shotmap`
- `/api/v1/event/{event_id}/managers`
- `/api/v1/event/{event_id}/comments`
- `/api/v1/event/{event_id}/votes`
- `/api/v1/event/{event_id}/pregame-form`
- `/api/v1/event/{event_id}/official-tweets`
- `/api/v1/event/{event_id}/ai-insights-postmatch/en`
- `/api/v1/event/{event_id}/best-players/summary`
- `/api/v1/event/{event_id}/graph/win-probability`
- `/api/v1/event/{event_id}/average-positions`
- `/api/v1/event/{event_id}/highlights`
- `/api/v1/event/{event_id}/odds/{provider_id}/featured`
- `/api/v1/event/{event_id}/odds/{provider_id}/all`
- `/api/v1/event/{event_id}/provider/{provider_id}/winning-odds`
- `/api/v1/event/{event_id}/media/summary/country/JP`
- `/api/v1/event/{event_id}/jersey/home/player/clean`
- `/api/v1/event/{event_id}/jersey/away/player/clean`
- `/api/v1/event/newly-added-events`

### PLAYER
- `/api/v1/player/{player_id}`
- `/api/v1/player/{player_id}/statistics`
- `/api/v1/player/{player_id}/statistics/seasons`
- `/api/v1/player/{player_id}/unique-tournaments`
- `/api/v1/player/{player_id}/events/last/{page}`
- `/api/v1/player/{player_id}/attribute-overviews`
- `/api/v1/player/{player_id}/characteristics`
- `/api/v1/player/{player_id}/media`
- `/api/v1/player/{player_id}/media/videos`
- `/api/v1/player/{player_id}/media/summary/country/JP`
- `/api/v1/player/{player_id}/national-team-statistics`
- `/api/v1/fantasy/player/{player_id}/competitions`

### TEAM
- `/api/v1/team/{team_id}`
- `/api/v1/team/{team_id}/events/last/{page}`
- `/api/v1/team/{team_id}/events/next/{page}`
- `/api/v1/team/{team_id}/performance`
- `/api/v1/team/{team_id}/featured-event`
- `/api/v1/team/{team_id}/player-statistics/seasons`
- `/api/v1/team/{team_id}/standings/seasons`
- `/api/v1/team/{team_id}/team-statistics/seasons`
- `/api/v1/team/{team_id}/unique-tournaments/all`
- `/api/v1/team/{team_id}/unique-tournament/{tournament_id}/season/{season_id}/statistics/overall`
- `/api/v1/team-of-the-period/{id}`

### TOURNAMENT
- `/api/v1/unique-tournament/{tournament_id}`
- `/api/v1/unique-tournament/{tournament_id}/featured-events`
- `/api/v1/unique-tournament/{tournament_id}/media`
- `/api/v1/unique-tournament/{tournament_id}/scheduled-events/{date}`
- `/api/v1/unique-tournament/{tournament_id}/season/{season_id}/standings/total`
- `/api/v1/unique-tournament/{tournament_id}/season/{season_id}/standings/home`
- `/api/v1/unique-tournament/{tournament_id}/season/{season_id}/events/last/{page}`
- `/api/v1/unique-tournament/{tournament_id}/season/{season_id}/events/next/{page}`
- `/api/v1/unique-tournament/{tournament_id}/season/{season_id}/events/round/{round}`
- `/api/v1/unique-tournament/{tournament_id}/season/{season_id}/rounds`
- `/api/v1/unique-tournament/{tournament_id}/season/{season_id}/venues`

### ODDS
- `/api/v1/odds/providers/JP/web`
- `/api/v1/odds/providers/JP/web-featured`
- `/api/v1/odds/providers/JP/web-odds`
- `/api/v1/odds/{event_id}/featured-events/football`

### NEWS
- `/api/v1/sofascore-news/en/posts`
- `/api/v1/sofascore-news/en/event/{event_id}/posts/{page}`
- `/api/v1/sofascore-news/en/player/{player_id}/posts/{page}`
- `/api/v1/sofascore-news/en/team/{team_id}/posts/{page}`
- `/api/v1/sofascore-news/en/tournament/{tournament_id}/posts/{page}`

### CONFIG
- `/api/v1/config/country-sport-priorities/country`
- `/api/v1/config/country-sport-priorities/country/JP`
- `/api/v1/config/default-unique-tournaments/JP/football`
- `/api/v1/country/alpha2`

### SPORT
- `/api/v1/sport/football/categories/all`
- `/api/v1/sport/football/live-tournaments`
- `/api/v1/sport/{sport_id}/event-count`

### OTHER
- `/api/v1/tv/event/{event_id}/country-channels`
- `/api/v1/fantasy/event/{event_id}`
- `/api/v1/offers/banner/player/{player_id}/JP/en`
- `/api/v1/offers/banner/team/{team_id}/JP/en`

## Key data structures

### Match statistics

```json
{
  "statistics": [
    {
      "period": "ALL",
      "groups": [
        {
          "groupName": "Match overview",
          "statisticsItems": [
            {
              "name": "Ball possession",
              "home": "44%",
              "away": "56%",
              "key": "ballPossession"
            },
            {
              "name": "Expected goals",
              "home": "1.84",
              "away": "0.89",
              "key": "expectedGoals"
            }
          ]
        }
      ]
    }
  ]
}
```

### Player statistics

```json
{
  "statistics": {
    "expectedGoals": 0.91,
    "expectedAssists": 0.36,
    "expectedGoalsOnTarget": 0.25,
    "goals": 0,
    "goalAssist": 1,
    "rating": 6.4
  }
}
```

### Incidents

```json
{
  "incidents": [
    {
      "incidentType": "goal",
      "time": 25,
      "addedTime": null,
      "player": { "name": "Trai Hume" },
      "homeScore": 1,
      "awayScore": 0
    }
  ]
}
```

### Momentum graph

```json
{
  "graphPoints": [
    { "minute": 1, "value": 6 },
    { "minute": 20, "value": 56 },
    { "minute": 25, "value": 15 }
  ]
}
```

## Usage

```bash
python3 fetch_incidents.py --event-id 14025013
python3 fetch_managers.py --team-id 42
python3 fetch_referees.py --referee-id 69853
python3 fetch_attendance.py --event-id 14025013
python3 fetch_team_rankings.py --team-id 42 44
```

## Notes

- Rotating proxy is important.
- Add enough delay between requests.
- `timeSeconds` is mostly `null` except HT/FT.
- Some endpoints need country/language parameters like `/JP/en`.
- Too many requests can trigger IP bans.

## Tested IDs

- Event: `14023966` — Sunderland vs Chelsea
- Player: `839956` — Erling Haaland
- Team: `41` — Sunderland
- Team: `38` — Chelsea
- Tournament: `17` — Premier League
- Season: `76986` — 2025/26

## Summary

We now have a browser-verified set of SofaScore endpoints and a working strategy for extracting:
- match event data
- statistics
- lineups
- incidents
- player/team/tournament data
- per-minute momentum graph
