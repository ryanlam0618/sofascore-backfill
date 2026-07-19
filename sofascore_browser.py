#!/usr/bin/env python3
"""Shared SofaScore browser helpers.

This module centralizes the browser-first strategy used across the repo:
- launch Chromium with the rotating Webshare proxy
- warm the session by loading the match page first
- extract SSR `__NEXT_DATA__`
- fetch selected SofaScore API endpoints through the page session

The goal is to keep browser logic in one place so the fetch scripts and the
orchestrator can share the same behavior.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional
import random

from playwright.async_api import async_playwright

BROWSER_BASE = "https://www.sofascore.com"
API_BASE = "https://www.sofascore.com/api/v1"
PROXY_SERVER = os.getenv("SOFA_PROXY", "http://p.webshare.io:80")
PROXY_USERNAME = os.getenv("SOFA_PROXY_USERNAME", "aeptenjc-rotate")
PROXY_SESSION_KEY = os.getenv("SOFA_PROXY_SESSION_KEY", "")
PROXY_PASSWORD = os.getenv("SOFA_PROXY_PASSWORD", "")
DEFAULT_USER_AGENT = os.getenv(
    "SOFA_USER_AGENT",
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
    ),
)
DEFAULT_ACCEPT_LANGUAGE = os.getenv("SOFA_ACCEPT_LANGUAGE", "en-US,en;q=0.9")


@dataclass(frozen=True)
class EndpointSpec:
    name: str
    path_template: str

    def path(self, **kwargs: Any) -> str:
        return self.path_template.format(**kwargs)


EVENT_ENDPOINTS: tuple[EndpointSpec, ...] = (
    EndpointSpec("event", "/api/v1/event/{event_id}"),
    EndpointSpec("statistics", "/api/v1/event/{event_id}/statistics"),
    EndpointSpec("lineups", "/api/v1/event/{event_id}/lineups"),
    EndpointSpec("incidents", "/api/v1/event/{event_id}/incidents"),
    EndpointSpec("graph", "/api/v1/event/{event_id}/graph"),
    EndpointSpec("managers", "/api/v1/event/{event_id}/managers"),
    EndpointSpec("comments", "/api/v1/event/{event_id}/comments"),
    EndpointSpec("votes", "/api/v1/event/{event_id}/votes"),
    EndpointSpec("pregame_form", "/api/v1/event/{event_id}/pregame-form"),
    EndpointSpec("official_tweets", "/api/v1/event/{event_id}/official-tweets"),
    EndpointSpec("best_players_summary", "/api/v1/event/{event_id}/best-players/summary"),
    EndpointSpec("average_positions", "/api/v1/event/{event_id}/average-positions"),
    EndpointSpec("highlights", "/api/v1/event/{event_id}/highlights"),
    EndpointSpec("media_summary", "/api/v1/event/{event_id}/media/summary/country/JP"),
    EndpointSpec("odds_featured", "/api/v1/event/{event_id}/odds/1/featured"),
    EndpointSpec("odds_all", "/api/v1/event/{event_id}/odds/1/all"),
    EndpointSpec("provider_winning_odds", "/api/v1/event/{event_id}/provider/1/winning-odds"),
    # Additional endpoints (added 2025-06-30)
    EndpointSpec("shotmap", "/api/v1/event/{event_id}/shotmap"),
    EndpointSpec("player_statistics", "/api/v1/event/{event_id}/player-statistics"),
    EndpointSpec("momentum", "/api/v1/event/{event_id}/momentum"),
    EndpointSpec("h2h", "/api/v1/event/{event_id}/h2h"),
    EndpointSpec("tv", "/api/v1/event/{event_id}/tv"),
)


SPORT_ENDPOINTS: tuple[EndpointSpec, ...] = (
    EndpointSpec("football_categories", "/api/v1/sport/football/categories"),
    EndpointSpec("football_categories_all", "/api/v1/sport/football/categories/all"),
    EndpointSpec("football_live_events", "/api/v1/sport/football/events/live"),
    EndpointSpec("football_live_tournaments", "/api/v1/sport/football/live-tournaments"),
    EndpointSpec("football_scheduled_events", "/api/v1/sport/football/scheduled-events/{date}"),
    EndpointSpec("football_scheduled_tournaments_page", "/api/v1/sport/football/scheduled-tournaments/{date}/page/{page}"),
    EndpointSpec("sport_event_count", "/api/v1/sport/{sport_id}/event-count"),
    EndpointSpec("newly_added_events", "/api/v1/event/newly-added-events"),
)

CONFIG_ENDPOINTS: tuple[EndpointSpec, ...] = (
    EndpointSpec("country_sport_priorities_country", "/api/v1/config/country-sport-priorities/country"),
    EndpointSpec("country_sport_priorities_country_code", "/api/v1/config/country-sport-priorities/country/{cc}"),
    EndpointSpec("default_unique_tournaments", "/api/v1/config/default-unique-tournaments/{cc}/football"),
    EndpointSpec("unique_tournaments_en_football", "/api/v1/config/unique-tournaments/en/football"),
)

TOURNAMENT_ENDPOINTS: tuple[EndpointSpec, ...] = (
    EndpointSpec("unique_tournament_featured_events", "/api/v1/unique-tournament/{tournament_id}/featured-events"),
    EndpointSpec("unique_tournament_media", "/api/v1/unique-tournament/{tournament_id}/media"),
    EndpointSpec("unique_tournament_scheduled_events", "/api/v1/unique-tournament/{tournament_id}/scheduled-events/{date}"),
    EndpointSpec("unique_tournament_season_cuptrees", "/api/v1/unique-tournament/{tournament_id}/season/{season_id}/cuptrees"),
    EndpointSpec("unique_tournament_season_editors", "/api/v1/unique-tournament/{tournament_id}/season/{season_id}/editors"),
    EndpointSpec("unique_tournament_events_last", "/api/v1/unique-tournament/{tournament_id}/season/{season_id}/events/last/{n}"),
    EndpointSpec("unique_tournament_events_next", "/api/v1/unique-tournament/{tournament_id}/season/{season_id}/events/next/{n}"),
    EndpointSpec("unique_tournament_events_round", "/api/v1/unique-tournament/{tournament_id}/season/{season_id}/events/round/{round}"),
    EndpointSpec("unique_tournament_groups", "/api/v1/unique-tournament/{tournament_id}/season/{season_id}/groups"),
    EndpointSpec("unique_tournament_info", "/api/v1/unique-tournament/{tournament_id}/season/{season_id}/info"),
    EndpointSpec("unique_tournament_player_of_season", "/api/v1/unique-tournament/{tournament_id}/season/{season_id}/player-of-the-season"),
    EndpointSpec("unique_tournament_player_of_season_race", "/api/v1/unique-tournament/{tournament_id}/season/{season_id}/player-of-the-season-race"),
    EndpointSpec("unique_tournament_player_statistics_types", "/api/v1/unique-tournament/{tournament_id}/season/{season_id}/player-statistics/types"),
    EndpointSpec("unique_tournament_power_rankings_round", "/api/v1/unique-tournament/{tournament_id}/season/{season_id}/power-rankings/round/{round}"),
    EndpointSpec("unique_tournament_power_rankings_rounds", "/api/v1/unique-tournament/{tournament_id}/season/{season_id}/power-rankings/rounds"),
    EndpointSpec("unique_tournament_rounds", "/api/v1/unique-tournament/{tournament_id}/season/{season_id}/rounds"),
    EndpointSpec("unique_tournament_standings_home", "/api/v1/unique-tournament/{tournament_id}/season/{season_id}/standings/home"),
    EndpointSpec("unique_tournament_standings_total", "/api/v1/unique-tournament/{tournament_id}/season/{season_id}/standings/total"),
    EndpointSpec("unique_tournament_statistics_info", "/api/v1/unique-tournament/{tournament_id}/season/{season_id}/statistics/info"),
    EndpointSpec("unique_tournament_team_events_total", "/api/v1/unique-tournament/{tournament_id}/season/{season_id}/team-events/total"),
    EndpointSpec("unique_tournament_team_of_periods_rated", "/api/v1/unique-tournament/{tournament_id}/season/{season_id}/team-of-the-period/periods/rated"),
    EndpointSpec("unique_tournament_team_statistics_types", "/api/v1/unique-tournament/{tournament_id}/season/{season_id}/team-statistics/types"),
    EndpointSpec("unique_tournament_team_performance_graph", "/api/v1/unique-tournament/{tournament_id}/season/{season_id}/team/{team_id}/team-performance-graph-data"),
    EndpointSpec("unique_tournament_top_teams_overall", "/api/v1/unique-tournament/{tournament_id}/season/{season_id}/top-teams/overall"),
    EndpointSpec("unique_tournament_trending_top_players", "/api/v1/unique-tournament/{tournament_id}/season/{season_id}/trending-top-players"),
    EndpointSpec("unique_tournament_venues", "/api/v1/unique-tournament/{tournament_id}/season/{season_id}/venues"),
    EndpointSpec("unique_tournament_seasons", "/api/v1/unique-tournament/{tournament_id}/seasons"),
    EndpointSpec("tournament_scheduled_events", "/api/v1/tournament/{tournament_id}/scheduled-events/{date}"),
    EndpointSpec("tournament_standings_home", "/api/v1/tournament/{tournament_id}/season/{season_id}/standings/home"),
    EndpointSpec("tournament_standings_total", "/api/v1/tournament/{tournament_id}/season/{season_id}/standings/total"),
    EndpointSpec("tournament_team_events_total", "/api/v1/tournament/{tournament_id}/season/{season_id}/team-events/total"),
)

TEAM_ENDPOINTS: tuple[EndpointSpec, ...] = (
    EndpointSpec("team_achievements", "/api/v1/team/{team_id}/achievements"),
    EndpointSpec("team_events_last", "/api/v1/team/{team_id}/events/last/{n}"),
    EndpointSpec("team_events_next", "/api/v1/team/{team_id}/events/next/{n}"),
    EndpointSpec("team_featured_event", "/api/v1/team/{team_id}/featured-event"),
    EndpointSpec("team_featured_players", "/api/v1/team/{team_id}/featured-players"),
    EndpointSpec("team_media_summary", "/api/v1/team/{team_id}/media/summary/country/{cc}"),
    EndpointSpec("team_media_videos", "/api/v1/team/{team_id}/media/videos"),
    EndpointSpec("team_official_tweets", "/api/v1/team/{team_id}/official-tweets"),
    EndpointSpec("team_performance", "/api/v1/team/{team_id}/performance"),
    EndpointSpec("team_player_statistics_seasons", "/api/v1/team/{team_id}/player-statistics/seasons"),
    EndpointSpec("team_season_best_result", "/api/v1/team/{team_id}/season/{season_id}/best-result"),
    EndpointSpec("team_standings_seasons", "/api/v1/team/{team_id}/standings/seasons"),
    EndpointSpec("team_team_statistics_seasons", "/api/v1/team/{team_id}/team-statistics/seasons"),
    EndpointSpec("team_ranks_overall", "/api/v1/team/{team_id}/unique-tournament/{tournament_id}/season/{season_id}/ranks/overall"),
    EndpointSpec("team_statistics_overall", "/api/v1/team/{team_id}/unique-tournament/{tournament_id}/season/{season_id}/statistics/overall"),
    EndpointSpec("team_top_players_overall", "/api/v1/team/{team_id}/unique-tournament/{tournament_id}/season/{season_id}/top-players/overall"),
    EndpointSpec("team_unique_tournaments_all", "/api/v1/team/{team_id}/unique-tournaments/all"),
    EndpointSpec("team_year_statistics", "/api/v1/team/{team_id}/year-statistics/{year}"),
)

PLAYER_ENDPOINTS: tuple[EndpointSpec, ...] = (
    EndpointSpec("player_attribute_overviews", "/api/v1/player/{player_id}/attribute-overviews"),
)

MISC_ENDPOINTS: tuple[EndpointSpec, ...] = (
    EndpointSpec("country_alpha2", "/api/v1/country/alpha2"),
    EndpointSpec("tv_country_channels", "/api/v1/tv/event/{event_id}/country-channels"),
    EndpointSpec("fantasy_event", "/api/v1/fantasy/event/{event_id}"),
    EndpointSpec("team_of_the_period", "/api/v1/team-of-the-period/{team_of_period_id}"),
    EndpointSpec("translation_description", "/api/v1/translation/description/{description_id}/language/en"),
    EndpointSpec("odds_providers_web", "/api/v1/odds/providers/{cc}/web"),
    EndpointSpec("odds_providers_web_featured", "/api/v1/odds/providers/{cc}/web-featured"),
    EndpointSpec("odds_providers_web_odds", "/api/v1/odds/providers/{cc}/web-odds"),
    EndpointSpec("odds_featured_events_football", "/api/v1/odds/{odds_id}/featured-events/football"),
    EndpointSpec("offers_banner_team", "/api/v1/offers/banner/team/{team_id}/{cc}/en"),
    EndpointSpec("sofascore_news_event_posts", "/api/v1/sofascore-news/en/event/{event_id}/posts/{post_id}"),
    EndpointSpec("sofascore_news_posts", "/api/v1/sofascore-news/en/posts"),
    EndpointSpec("sofascore_news_team_posts", "/api/v1/sofascore-news/en/team/{team_id}/posts/{post_id}"),
    EndpointSpec("sofascore_news_tournament_posts", "/api/v1/sofascore-news/en/tournament/{tournament_id}/posts/{post_id}"),
    EndpointSpec("branding_providers_web", "/api/v1/branding/providers/{cc}/web"),
    EndpointSpec("event_ai_insights", "/api/v1/event/{event_id}/ai-insights/en"),
    EndpointSpec("event_win_probability", "/api/v1/event/{event_id}/graph/win-probability"),
    EndpointSpec("event_video_highlights_extended", "/api/v1/event/{event_id}/sport-video-highlights/country/{cc}/extended"),
)


class SofaScoreBrowserClient:
    """Browser-first SofaScore client."""

    def __init__(self, headless: bool = True, sticky_session_key: Optional[str] = None):
        self.headless = headless
        self.sticky_session_key = sticky_session_key or PROXY_SESSION_KEY or None
        self._pw = None
        self.browser = None
        self.context = None
        self.page = None

    async def __aenter__(self):
        self._pw = await async_playwright().start()
        launch_args = [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--window-size=1920,1080",
            "--disable-blink-features=AutomationControlled",
        ]
        if self.headless:
            launch_args.append("--headless=new")
        self.browser = await self._pw.chromium.launch(
            headless=self.headless,
            args=launch_args,
        )
        proxy_username = PROXY_USERNAME
        if self.sticky_session_key:
            proxy_username = f"{proxy_username}-session-{self.sticky_session_key}"
        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=DEFAULT_USER_AGENT,
            proxy={
                "server": PROXY_SERVER,
                "username": proxy_username,
                "password": PROXY_PASSWORD,
            } if PROXY_SERVER else None,
            extra_http_headers={"Accept-Language": DEFAULT_ACCEPT_LANGUAGE},
        )
        self.page = await self.context.new_page()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.page:
            await self.page.close()
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self._pw:
            await self._pw.stop()

    async def _humanize_page(self) -> None:
        try:
            await self.page.mouse.move(random.randint(140, 640), random.randint(110, 420), steps=random.randint(8, 18))
            await asyncio.sleep(random.uniform(0.2, 0.6))
            await self.page.mouse.wheel(0, random.randint(180, 520))
            await asyncio.sleep(random.uniform(0.4, 1.0))
        except Exception:
            return

    async def warm_event_page(self, event_id: int, timeout: int = 60) -> None:
        await self.page.goto(
            f"{BROWSER_BASE}/event/{event_id}",
            timeout=timeout * 1000,
            wait_until="domcontentloaded",
        )
        # Give JS a moment to initialize session/cookies
        await asyncio.sleep(2)
        await self._humanize_page()

    async def warm_tournament_page(self, tournament_id: int, season_id: Optional[int] = None, timeout: int = 60) -> None:
        url = f"{BROWSER_BASE}/football/unique-tournament/{tournament_id}"
        if season_id is not None:
            url = f"{url}/season/{season_id}"
        await self.page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
        await asyncio.sleep(2)
        await self._humanize_page()

    async def warm_team_page(self, team_id: int, timeout: int = 60) -> None:
        await self.page.goto(
            f"{BROWSER_BASE}/team/football/{team_id}",
            timeout=timeout * 1000,
            wait_until="domcontentloaded",
        )
        await asyncio.sleep(2)
        await self._humanize_page()

    async def warm_sport_page(self, sport: str = "football", timeout: int = 60) -> None:
        await self.page.goto(
            f"{BROWSER_BASE}/{sport}",
            timeout=timeout * 1000,
            wait_until="domcontentloaded",
        )
        await asyncio.sleep(2)
        await self._humanize_page()

    async def get_ssr(self) -> Optional[dict]:
        return await self.page.evaluate(
            """
            () => {
              const script = document.querySelector('#__NEXT_DATA__');
              return script ? JSON.parse(script.textContent) : null;
            }
            """
        )

    async def fetch_json(self, path: str, timeout_ms: int = 10000) -> dict:
        result = await self.page.evaluate(
            """
            async ({ path, timeoutMs }) => {
              const controller = new AbortController();
              const timer = setTimeout(() => controller.abort(), timeoutMs);
              try {
                const resp = await fetch(path, {
                  credentials: 'include',
                  signal: controller.signal,
                });
                const text = await resp.text();
                let body = text;
                try { body = JSON.parse(text); } catch (e) {}
                return { status: resp.status, body };
              } finally {
                clearTimeout(timer);
              }
            }
            """,
            {"path": path, "timeoutMs": timeout_ms},
        )
        return result

    def default_endpoint_params(self, **overrides: Any) -> Dict[str, Any]:
        today = datetime.date.today()
        params: Dict[str, Any] = {
            "cc": "HK",
            "date": today.isoformat(),
            "description_id": overrides.get("event_id", 0),
            "event_id": 0,
            "n": 5,
            "odds_id": 1,
            "page": 0,
            "player_id": 0,
            "post_id": 0,
            "round": 1,
            "season_id": 0,
            "sport": "football",
            "sport_id": "football",
            "team_id": 0,
            "team_of_period_id": 0,
            "tournament_id": 0,
            "year": today.year,
        }
        params.update({key: value for key, value in overrides.items() if value is not None})
        return params

    async def fetch_endpoint_bundle(
        self,
        endpoint_specs: Iterable[EndpointSpec],
        bundle_metadata: Dict[str, Any],
        endpoint_params: Dict[str, Any],
    ) -> Dict[str, Any]:
        bundle: Dict[str, Any] = {
            **bundle_metadata,
            "source": "browser",
            "ssr": await self.get_ssr(),
            "apis": {},
        }
        for spec in endpoint_specs:
            try:
                path = spec.path(**endpoint_params)
                bundle["apis"][path] = await self.fetch_json(path)
            except Exception as exc:
                bundle["apis"][spec.path_template] = {"status": 0, "error": str(exc)}
        return bundle

    async def fetch_event_bundle(
        self,
        event_id: int,
        endpoint_specs: Iterable[EndpointSpec] = EVENT_ENDPOINTS,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        await self.warm_event_page(event_id)
        endpoint_params = self.default_endpoint_params(event_id=event_id, description_id=event_id, **kwargs)
        return await self.fetch_endpoint_bundle(endpoint_specs, {"event_id": event_id}, endpoint_params)

    async def fetch_tournament_bundle(
        self,
        tournament_id: int,
        season_id: int,
        endpoint_specs: Iterable[EndpointSpec] = TOURNAMENT_ENDPOINTS,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        await self.warm_tournament_page(tournament_id, season_id)
        endpoint_params = self.default_endpoint_params(
            tournament_id=tournament_id,
            season_id=season_id,
            **kwargs,
        )
        return await self.fetch_endpoint_bundle(
            endpoint_specs,
            {"tournament_id": tournament_id, "season_id": season_id},
            endpoint_params,
        )

    async def fetch_team_bundle(
        self,
        team_id: int,
        endpoint_specs: Iterable[EndpointSpec] = TEAM_ENDPOINTS,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        await self.warm_team_page(team_id)
        endpoint_params = self.default_endpoint_params(team_id=team_id, **kwargs)
        return await self.fetch_endpoint_bundle(endpoint_specs, {"team_id": team_id}, endpoint_params)

    async def fetch_sport_bundle(
        self,
        sport: str = "football",
        endpoint_specs: Iterable[EndpointSpec] = SPORT_ENDPOINTS,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        await self.warm_sport_page(sport)
        endpoint_params = self.default_endpoint_params(sport=sport, sport_id=sport, **kwargs)
        return await self.fetch_endpoint_bundle(endpoint_specs, {"sport": sport}, endpoint_params)

    async def fetch_config_bundle(
        self,
        country_code: str = "HK",
        endpoint_specs: Iterable[EndpointSpec] = CONFIG_ENDPOINTS,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        await self.warm_sport_page("football")
        endpoint_params = self.default_endpoint_params(cc=country_code, **kwargs)
        return await self.fetch_endpoint_bundle(endpoint_specs, {"country_code": country_code}, endpoint_params)


def run_async(coro):
    return asyncio.run(coro)
