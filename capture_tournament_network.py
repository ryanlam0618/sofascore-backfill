#!/usr/bin/env python3
"""Capture SofaScore API requests while loading a tournament season page."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from backfill_runner import BROWSER_BASE, BackfillClient


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ut-id", type=int, required=True)
    parser.add_argument("--season-id", type=int, required=True)
    parser.add_argument("--out", default="/tmp/tournament_network.json")
    args = parser.parse_args()

    captured: list[dict[str, str | int]] = []
    async with BackfillClient(headless=True) as client:
        await client.warm_homepage()

        def on_response(resp):
            url = resp.url
            if "sofascore.com/api/" in url or f"season/{args.season_id}" in url:
                captured.append({"status": resp.status, "url": url})

        client.page.on("response", on_response)
        url = f"{BROWSER_BASE}/football/unique-tournament/{args.ut_id}/season/{args.season_id}"
        try:
            await client.page.goto(url, timeout=45000, wait_until="networkidle")
        except Exception:
            try:
                await client.page.goto(url, timeout=45000, wait_until="domcontentloaded")
                await asyncio.sleep(8)
            except Exception as exc:
                captured.append({"status": 0, "url": f"NAV_ERROR {exc}"})
        await asyncio.sleep(5)

    unique = []
    seen = set()
    for item in captured:
        key = (item["status"], item["url"])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    Path(args.out).write_text(json.dumps(unique, indent=2))
    print(f"captured={len(unique)} out={args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
