#!/usr/bin/env python3
"""Audit discovered seasons through the same browser/proxy transport as v2."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright


ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")


def proxy_config() -> dict:
    server = os.getenv("SOFA_PROXY_HOST", "p.webshare.io")
    port = os.getenv("SOFA_PROXY_PORT", "80")
    return {
        "server": f"http://{server}:{port}",
        "username": os.getenv("SOFA_PROXY_USER", ""),
        "password": os.getenv("SOFA_PROXY_PASS", ""),
    }


async def fetch(page, path: str) -> tuple[int, object]:
    return await page.evaluate(
        """
        async path => {
          const response = await fetch(path, {credentials: 'include'});
          const text = await response.text();
          let body = text;
          try { body = JSON.parse(text); } catch (_) {}
          return [response.status, body];
        }
        """,
        path,
    )


async def main() -> None:
    discoveries = json.loads((ROOT / "discoveries.json").read_text(encoding="utf-8"))
    results = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            proxy=proxy_config(),
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await context.new_page()

        for name, competition in discoveries.items():
            ut_id = competition.get("ut_id")
            seasons = competition.get("seasons", {})
            target_ids = competition.get("ten_yr_seasons", [])
            for raw_sid in target_ids:
                sid = str(raw_sid)
                season_path = f"/api/v1/unique-tournament/{ut_id}/season/{sid}"
                rounds_path = f"{season_path}/rounds"
                try:
                    await page.goto(
                        f"https://www.sofascore.com/football/unique-tournament/{ut_id}/season/{sid}",
                        wait_until="domcontentloaded",
                        timeout=60000,
                    )
                    await page.wait_for_timeout(1200)
                    season_status, season_body = await fetch(page, season_path)
                    rounds_status, rounds_body = await fetch(page, rounds_path)
                    rounds = rounds_body.get("rounds", []) if isinstance(rounds_body, dict) else []
                    fallback = None
                    if not rounds:
                        for label, path in (
                            ("cuptrees", f"{season_path}/cuptrees"),
                            ("events_last", f"{season_path}/events/last/0"),
                        ):
                            fallback_status, fallback_body = await fetch(page, path)
                            has_data = isinstance(fallback_body, dict) and bool(
                                fallback_body.get("events") or fallback_body.get("cupTrees") or fallback_body.get("rounds")
                            )
                            if fallback_status == 200 and has_data:
                                fallback = {"method": label, "status": fallback_status}
                                break
                    if season_status == 200 and rounds:
                        status = "working"
                    elif season_status == 200 and fallback:
                        status = "working_fallback"
                    elif season_status == 200:
                        status = "season_only"
                    else:
                        status = "failed"
                    results.append({
                        "competition": name,
                        "season_id": sid,
                        "year": seasons.get(sid),
                        "status": status,
                        "season_http": season_status,
                        "rounds_http": rounds_status,
                        "round_count": len(rounds),
                        "fallback": fallback,
                    })
                    print(f"{name}\t{sid}\t{seasons.get(sid)}\t{status}\tseason={season_status}\trounds={rounds_status}/{len(rounds)}")
                except Exception as exc:
                    results.append({"competition": name, "season_id": sid, "year": seasons.get(sid), "status": "error", "error": str(exc)})
                    print(f"{name}\t{sid}\t{seasons.get(sid)}\terror\t{exc}")

        await browser.close()

    output = ROOT / "data" / "season_audit.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    counts = {}
    for row in results:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    print("SUMMARY", json.dumps(counts, ensure_ascii=False))
    print("SAVED", output)


if __name__ == "__main__":
    asyncio.run(main())
