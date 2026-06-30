#!/usr/bin/env python3
"""Print the SofaScore endpoint catalog by category."""

from __future__ import annotations

from sofascore_browser import (
    CONFIG_ENDPOINTS,
    EVENT_ENDPOINTS,
    MISC_ENDPOINTS,
    PLAYER_ENDPOINTS,
    SPORT_ENDPOINTS,
    TEAM_ENDPOINTS,
    TOURNAMENT_ENDPOINTS,
    EndpointSpec,
)

ALL_ENDPOINTS: dict[str, tuple[EndpointSpec, ...]] = {
    "event": EVENT_ENDPOINTS,
    "sport": SPORT_ENDPOINTS,
    "config": CONFIG_ENDPOINTS,
    "tournament": TOURNAMENT_ENDPOINTS,
    "team": TEAM_ENDPOINTS,
    "player": PLAYER_ENDPOINTS,
    "misc": MISC_ENDPOINTS,
}


def print_catalog() -> None:
    total = sum(len(endpoint_specs) for endpoint_specs in ALL_ENDPOINTS.values())
    print(f"SofaScore endpoint catalog ({total} endpoints)")
    for category, endpoint_specs in ALL_ENDPOINTS.items():
        print(f"\n[{category}] {len(endpoint_specs)} endpoints")
        for spec in endpoint_specs:
            print(f"- {spec.name}: {spec.path_template}")


def main() -> None:
    print_catalog()


if __name__ == "__main__":
    main()
