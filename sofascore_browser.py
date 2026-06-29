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
import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

from playwright.async_api import async_playwright

BROWSER_BASE = "https://www.sofascore.com"
API_BASE = "https://www.sofascore.com/api/v1"
PROXY_SERVER = os.getenv("SOFA_PROXY", "http://p.webshare.io:80")
PROXY_USERNAME = os.getenv("SOFA_PROXY_USERNAME", "aeptenjc-rotate")
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


class SofaScoreBrowserClient:
    """Browser-first SofaScore client."""

    def __init__(self, headless: bool = True):
        self.headless = headless
        self._pw = None
        self.browser = None
        self.context = None
        self.page = None

    async def __aenter__(self):
        self._pw = async_playwright()
        await self._pw.start()
        self.browser = await self._pw.chromium.launch(
            headless=self.headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-size=1920,1080",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=DEFAULT_USER_AGENT,
            proxy={
                "server": PROXY_SERVER,
                "username": PROXY_USERNAME,
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

    async def warm_event_page(self, event_id: int, timeout: int = 60) -> None:
        await self.page.goto(
            f"{BROWSER_BASE}/event/{event_id}",
            timeout=timeout * 1000,
            wait_until="networkidle",
        )

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
            async ({ path }) => {
              const resp = await fetch(path, { credentials: 'include' });
              const text = await resp.text();
              let body = text;
              try { body = JSON.parse(text); } catch (e) {}
              return { status: resp.status, body };
            }
            """,
            {"path": path},
        )
        return result

    async def fetch_event_bundle(self, event_id: int, endpoint_specs: Iterable[EndpointSpec] = EVENT_ENDPOINTS) -> Dict[str, Any]:
        await self.warm_event_page(event_id)
        bundle: Dict[str, Any] = {
            "event_id": event_id,
            "source": "browser",
            "ssr": await self.get_ssr(),
            "apis": {},
        }
        for spec in endpoint_specs:
            path = spec.path(event_id=event_id)
            try:
                bundle["apis"][path] = await self.fetch_json(path)
            except Exception as exc:
                bundle["apis"][path] = {"status": 0, "error": str(exc)}
        return bundle


def run_async(coro):
    return asyncio.run(coro)
