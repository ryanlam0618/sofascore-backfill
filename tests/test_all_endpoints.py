#!/usr/bin/env python3

from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path
from typing import Any

from backfill_runner import BackfillClient, ENDPOINT_PATHS


WORKDIR = Path(__file__).parent
RESULT_PATH = Path("/root/.openclaw/workspace/subagent_results/endpoint_test_result.json")

TOURNAMENT_ID = 17
SEASON_ID = 61627
COUNTRY_SLUG = "england"
COMPETITION_SLUG = "premier-league"
ROUND_PATH = f"/api/v1/unique-tournament/{TOURNAMENT_ID}/season/{SEASON_ID}/rounds"
ROUND1_EVENTS_PATH = f"/api/v1/unique-tournament/{TOURNAMENT_ID}/season/{SEASON_ID}/events/round/1"
ENDPOINT_ORDER = [
    "event",
    "incidents",
    "lineups",
    "statistics",
    "shotmap",
    "graph",
    "odds",
    "comments",
]


def summarize_body(body: Any) -> dict[str, Any]:
    if isinstance(body, dict):
        return {
            "type": "dict",
            "keys": list(body.keys()),
            "size": len(body),
        }
    if isinstance(body, list):
        return {
            "type": "list",
            "length": len(body),
        }
    if isinstance(body, str):
        return {
            "type": "str",
            "length": len(body),
            "preview": body[:200],
        }
    return {
        "type": type(body).__name__,
        "value": body,
    }


def pick_event_ids(events_payload: dict[str, Any], limit: int = 3) -> list[int]:
    event_ids: list[int] = []
    for event in events_payload.get("events", []):
        if not isinstance(event, dict):
            continue
        event_id = event.get("id")
        if not event_id:
            continue
        event_ids.append(int(event_id))
        if len(event_ids) >= limit:
            break
    return event_ids


async def pick_event_ids_from_page(client: BackfillClient, limit: int = 3) -> list[int]:
    ids = await client.page.evaluate(
        """
        (maxCount) => {
          const values = new Set();
          const hrefs = Array.from(document.querySelectorAll('a[href]')).map((a) => a.getAttribute('href') || '');
          for (const href of hrefs) {
            const matches = href.match(/\/event\/(\d+)/g) || [];
            for (const match of matches) {
              const m = match.match(/(\d+)$/);
              if (m) values.add(Number(m[1]));
            }
          }
          return Array.from(values).slice(0, maxCount);
        }
        """,
        limit,
    )
    return [int(x) for x in ids if x]


def pick_round_number(rounds_body: Any) -> int | None:
    if not isinstance(rounds_body, dict):
        return None
    rounds = rounds_body.get("rounds")
    if not isinstance(rounds, list):
        return None
    for item in rounds:
        if isinstance(item, dict):
            value = item.get("round")
            if value is not None:
                return int(value)
        elif isinstance(item, int):
            return int(item)
    return None


async def pick_event_ids_from_embedded_state(client: BackfillClient, limit: int = 3) -> list[int]:
    ids = await client.page.evaluate(
        """
        (maxCount) => {
          const out = new Set();
          const scripts = Array.from(document.querySelectorAll('script'));
          for (const script of scripts) {
            const text = script.textContent || '';
            const matches = text.match(/\"id\":(\d{6,})/g) || [];
            for (const match of matches) {
              const m = match.match(/(\d{6,})$/);
              if (m) out.add(Number(m[1]));
              if (out.size >= maxCount) return Array.from(out).slice(0, maxCount);
            }
          }
          return Array.from(out).slice(0, maxCount);
        }
        """,
        limit,
    )
    return [int(x) for x in ids if x]


def print_result_table(rows: list[dict[str, Any]]) -> None:
    headers = ["Event ID", *ENDPOINT_ORDER]
    widths = {header: len(header) for header in headers}

    for row in rows:
        widths["Event ID"] = max(widths["Event ID"], len(str(row["event_id"])))
        for endpoint in ENDPOINT_ORDER:
            widths[endpoint] = max(widths[endpoint], len(str(row.get(endpoint, "-"))))

    def fmt(parts: list[str]) -> str:
        return " | ".join(parts)

    header_line = fmt([
        "Event ID".ljust(widths["Event ID"]),
        *[endpoint.ljust(widths[endpoint]) for endpoint in ENDPOINT_ORDER],
    ])
    divider = fmt([
        "-" * widths["Event ID"],
        *["-" * widths[endpoint] for endpoint in ENDPOINT_ORDER],
    ])
    print(header_line)
    print(divider)
    for row in rows:
        print(fmt([
            str(row["event_id"]).ljust(widths["Event ID"]),
            *[str(row.get(endpoint, "-")).ljust(widths[endpoint]) for endpoint in ENDPOINT_ORDER],
        ]))


async def main() -> None:
    endpoint_results: list[dict[str, Any]] = []
    body_summaries: list[dict[str, Any]] = []
    rounds_summary: dict[str, Any] | None = None
    round1_summary: dict[str, Any] | None = None

    async with BackfillClient(headless=True, sticky_session_key="endpoint-test") as client:
        print("[1/4] Warming homepage")
        await client.warm_homepage()

        print("[2/4] Warming tournament page")
        await client.warm_tournament(TOURNAMENT_ID, SEASON_ID, COUNTRY_SLUG, COMPETITION_SLUG)

        print(f"[3/4] Fetching rounds: {ROUND_PATH}")
        rounds_result = await client.fetch_api(ROUND_PATH, prefer_capture=True)
        rounds_summary = {
            "status": rounds_result.get("status"),
            "summary": summarize_body(rounds_result.get("body")),
        }
        event_ids: list[int] = []

        if rounds_result.get("status") == 200:
            await asyncio.sleep(random.uniform(2.0, 3.0))

            first_round = pick_round_number(rounds_result.get("body")) or 1
            round_path = f"/api/v1/unique-tournament/{TOURNAMENT_ID}/season/{SEASON_ID}/events/round/{first_round}"
            print(f"[4/4] Fetching round {first_round} events: {round_path}")
            round1_result = await client.fetch_api(round_path, prefer_capture=True)
            round1_summary = {
                "status": round1_result.get("status"),
                "summary": summarize_body(round1_result.get("body")),
                "path": round_path,
            }
            if round1_result.get("status") == 200:
                event_ids = pick_event_ids(round1_result.get("body") or {}, limit=3)
        else:
            print("Rounds endpoint returned non-200; falling back to event IDs visible on warmed tournament page")
            round1_summary = {
                "status": None,
                "summary": {"type": "fallback", "source": "tournament_page_links"},
            }

        if not event_ids:
            event_ids = await pick_event_ids_from_page(client, limit=3)

        if not event_ids:
            event_ids = await pick_event_ids_from_embedded_state(client, limit=3)

        if not event_ids:
            raise RuntimeError("no event ids found from API or tournament page")

        print(f"Selected event IDs: {event_ids}")

        for event_id in event_ids:
            print(f"\nTesting event {event_id}")
            row: dict[str, Any] = {"event_id": event_id}
            endpoint_detail: dict[str, Any] = {"event_id": event_id, "endpoints": {}}
            for endpoint in ENDPOINT_ORDER:
                path = ENDPOINT_PATHS[endpoint].format(event_id=event_id)
                print(f"  -> {endpoint}: {path}")
                result = await client.fetch_api(path, prefer_capture=False)
                status = result.get("status")
                row[endpoint] = status

                detail = {"status": status}
                if status == 200:
                    detail["body_summary"] = summarize_body(result.get("body"))
                else:
                    error = result.get("error")
                    if error:
                        detail["error"] = error
                endpoint_detail["endpoints"][endpoint] = detail

                print(f"     status={status}")
                if status == 200:
                    print(f"     summary={json.dumps(detail['body_summary'], ensure_ascii=False)}")

                await asyncio.sleep(random.uniform(2.0, 3.0))

            endpoint_results.append(row)
            body_summaries.append(endpoint_detail)

    print("\nResult table:")
    print_result_table(endpoint_results)

    working_endpoints = []
    failing_endpoints = []
    for endpoint in ENDPOINT_ORDER:
        statuses = [row.get(endpoint) for row in endpoint_results]
        if statuses and all(status == 200 for status in statuses):
            working_endpoints.append(endpoint)
        if any(status == 403 for status in statuses):
            failing_endpoints.append(endpoint)

    if endpoint_results and all(row.get("event") for row in endpoint_results):
        status = "PASS" if not failing_endpoints else "PARTIAL"
    else:
        status = "FAIL"

    result_payload = {
        "task": "endpoint_test",
        "status": status,
        "summary": (
            f"Tested {len(endpoint_results)} events across {len(ENDPOINT_ORDER)} endpoints; "
            f"working endpoints: {working_endpoints}; failing endpoints: {failing_endpoints}"
        ),
        "details": {
            "events_tested": len(endpoint_results),
            "endpoint_results": endpoint_results,
            "working_endpoints": working_endpoints,
            "failing_endpoints": failing_endpoints,
            "warmup": {
                "rounds": rounds_summary,
                "round1_events": round1_summary,
            },
            "body_summaries": body_summaries,
        },
    }

    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(json.dumps(result_payload, ensure_ascii=False, indent=2) + "\n")
    print(f"\nSaved result JSON to {RESULT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
