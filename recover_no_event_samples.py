#!/usr/bin/env python3
"""Recover no_event rows from the SofaScore coverage audit.

Read-only for MySQL. Reads data/coverage_audit/season_sample_coverage.csv,
tries alternate event discovery endpoints, and writes recovery reports.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from audit_season_coverage import fetch_bundle, summarize_bundle
from backfill_runner import BackfillClient

IN_CSV = Path("data/coverage_audit/season_sample_coverage.csv")
OUT_JSON = Path("data/coverage_audit/no_event_recovery.json")
OUT_CSV = Path("data/coverage_audit/no_event_recovery.csv")
SUMMARY_MD = Path("data/coverage_audit/no_event_recovery_summary.md")


def _events_from_body(body: Any) -> list[dict[str, Any]]:
    if not isinstance(body, dict):
        return []
    events = body.get("events")
    if isinstance(events, list):
        return [event for event in events if isinstance(event, dict)]
    for key in ("event", "data"):
        value = body.get(key)
        if isinstance(value, list):
            return [event for event in value if isinstance(event, dict)]
    return []


def _pick_finished_event(events: list[dict[str, Any]]) -> int | None:
    for event in events:
        status = event.get("status") if isinstance(event.get("status"), dict) else {}
        if status.get("type") == "finished" and event.get("id"):
            return int(event["id"])
    for event in events:
        if event.get("id"):
            return int(event["id"])
    return None


def _norm_label(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "")


async def resolve_season_id(client: BackfillClient, ut_id: int, original_season_id: int, season_label: str | None) -> tuple[int, str]:
    """Resolve stale configured season IDs via SofaScore's live season list."""
    result = await client.fetch_api(f"/api/v1/unique-tournament/{ut_id}/seasons")
    if result.get("status") != 200:
        return original_season_id, f"seasons HTTP {result.get('status')}"
    seasons = result.get("body", {}).get("seasons", [])
    if not isinstance(seasons, list):
        return original_season_id, "seasons malformed"

    wanted = _norm_label(season_label)
    for season in seasons:
        if not isinstance(season, dict):
            continue
        labels = {_norm_label(season.get("year")), _norm_label(season.get("name"))}
        if wanted and wanted in labels:
            resolved = season.get("id")
            if resolved:
                resolved = int(resolved)
                if resolved == original_season_id:
                    return resolved, "season id current"
                return resolved, f"resolved {original_season_id}->{resolved}"
    return original_season_id, "season label not found"


async def try_original_rounds(client: BackfillClient, ut_id: int, season_id: int) -> tuple[int | None, str]:
    rounds = await client.fetch_api(f"/api/v1/unique-tournament/{ut_id}/season/{season_id}/rounds")
    if rounds.get("status") != 200:
        return None, f"rounds HTTP {rounds.get('status')}"
    round_numbers = [item.get("round") for item in rounds.get("body", {}).get("rounds", []) if item.get("round")]
    for round_number in round_numbers:
        await client.sleep_between(0.3, 0.8)
        result = await client.fetch_api(f"/api/v1/unique-tournament/{ut_id}/season/{season_id}/events/round/{round_number}")
        event_id = _pick_finished_event(_events_from_body(result.get("body")))
        if event_id:
            return event_id, f"round {round_number}"
    return None, "rounds had no events"


async def try_path(client: BackfillClient, path: str) -> tuple[int | None, str]:
    result = await client.fetch_api(path)
    status = result.get("status")
    if status != 200:
        return None, f"HTTP {status}"
    event_id = _pick_finished_event(_events_from_body(result.get("body")))
    if event_id:
        return event_id, "found"
    return None, "200 no events"


async def try_network_discovered_events(client: BackfillClient, ut_id: int, season_id: int) -> tuple[int | None, str]:
    """Load the tournament page, inspect browser performance URLs, and try event-like API paths."""
    try:
        await client.warm_tournament(ut_id, season_id)
    except Exception:
        pass
    await asyncio.sleep(5)
    urls = await client.page.evaluate(
        """
        () => performance.getEntriesByType('resource')
            .map(e => e.name)
            .filter(u => u.includes('/api/'))
        """
    )
    paths = []
    for url in urls:
        if not isinstance(url, str):
            continue
        marker = '/api/v1/'
        if marker not in url:
            continue
        path = url[url.index(marker):]
        # strip query later; keep both exact and queryless variants
        candidates = [path, path.split('?', 1)[0]]
        for candidate in candidates:
            lowered = candidate.lower()
            if any(token in lowered for token in ('event', 'events', 'match', 'matches')) and candidate not in paths:
                paths.append(candidate)
    for path in paths:
        result = await client.fetch_api(path)
        if result.get('status') != 200:
            continue
        event_id = _pick_finished_event(_events_from_body(result.get('body')))
        if event_id:
            return event_id, f'network {path[:180]}'
    return None, f'network no event urls={len(paths)}'


async def recover_row(client: BackfillClient, row: dict[str, str], fetch_details: bool) -> dict[str, Any]:
    name = row.get("competition") or ""
    ut_id = int(row.get("ut_id") or 0)
    original_season_id = int(row.get("season_id") or 0)
    season_id = original_season_id
    out: dict[str, Any] = {
        "competition": name,
        "ut_id": ut_id,
        "season_id": season_id,
        "season_label": row.get("season_label"),
        "previous_error": row.get("error"),
        "sampled_at": datetime.now(timezone.utc).isoformat(),
    }
    attempts: list[dict[str, Any]] = []
    try:
        await client.sleep_between(0.3, 0.8)
        resolved_season_id, detail = await resolve_season_id(client, ut_id, original_season_id, row.get("season_label"))
        attempts.append({"method": "resolve_season_id", "result": detail, "event_id": None})
        season_id = resolved_season_id
        out["original_season_id"] = original_season_id
        out["season_id"] = season_id
    except Exception as exc:
        attempts.append({"method": "resolve_season_id", "result": f"error {str(exc)[:200]}", "event_id": None})

    try:
        await client.warm_tournament(ut_id, season_id)
    except Exception as exc:
        attempts.append({"method": "warm_tournament", "result": f"error {str(exc)[:160]}"})

    methods = [
        ("rounds_events_round", lambda: try_original_rounds(client, ut_id, season_id)),
        ("season_events", lambda: try_path(client, f"/api/v1/unique-tournament/{ut_id}/season/{season_id}/events")),
        ("events_last", lambda: try_path(client, f"/api/v1/unique-tournament/{ut_id}/season/{season_id}/events/last/0")),
        ("events_next", lambda: try_path(client, f"/api/v1/unique-tournament/{ut_id}/season/{season_id}/events/next/0")),
        ("network_discovered", lambda: try_network_discovered_events(client, ut_id, season_id)),
    ]

    event_id = None
    recovered_by = None
    for method_name, method in methods:
        try:
            await client.sleep_between(0.3, 0.8)
            candidate, detail = await method()
            attempts.append({"method": method_name, "result": detail, "event_id": candidate})
            if candidate:
                event_id = candidate
                recovered_by = method_name
                break
        except Exception as exc:
            attempts.append({"method": method_name, "result": f"error {str(exc)[:200]}"})

    out["attempts"] = attempts
    out["event_id"] = event_id
    out["recovered_by"] = recovered_by
    if event_id:
        out["status"] = "recovered"
        if fetch_details:
            try:
                bundle, statuses = await fetch_bundle(client, event_id)
                out.update(statuses)
                out.update(summarize_bundle(bundle))
            except Exception as exc:
                out["detail_fetch_error"] = str(exc)[:500]
    else:
        out["status"] = "still_no_event"
    return out


def load_targets(limit: int = 0) -> list[dict[str, str]]:
    rows = list(csv.DictReader(IN_CSV.open()))
    targets = [row for row in rows if row.get("status") == "no_event"]
    if limit:
        targets = targets[:limit]
    return targets


def save(rows: list[dict[str, Any]]) -> None:
    OUT_JSON.write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    if rows:
        fieldnames = sorted({key for row in rows for key in row.keys() if key != "attempts"})
        with OUT_CSV.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                flat = {key: value for key, value in row.items() if key != "attempts"}
                writer.writerow(flat)
    recovered = [row for row in rows if row.get("status") == "recovered"]
    still = [row for row in rows if row.get("status") == "still_no_event"]
    by_method: dict[str, int] = {}
    for row in recovered:
        method = row.get("recovered_by") or "unknown"
        by_method[method] = by_method.get(method, 0) + 1
    lines = [
        "# no_event recovery summary",
        "",
        f"Rows tested: {len(rows)}",
        f"Recovered: {len(recovered)}",
        f"Still no_event: {len(still)}",
        "",
        "## Recovered by method",
    ]
    for method, count in sorted(by_method.items()):
        lines.append(f"- {method}: {count}")
    SUMMARY_MD.write_text("\n".join(lines) + "\n")


async def run(args: argparse.Namespace) -> int:
    targets = load_targets(args.limit)
    rows: list[dict[str, Any]] = []
    if OUT_JSON.exists() and not args.force:
        rows = json.loads(OUT_JSON.read_text())
    done = {(row.get("competition"), int(row.get("season_id", 0))) for row in rows}
    async with BackfillClient(headless=True) as client:
        await client.warm_homepage()
        for target in targets:
            key = (target.get("competition"), int(target.get("season_id") or 0))
            if key in done and not args.force:
                continue
            result = await recover_row(client, target, fetch_details=not args.no_details)
            rows = [row for row in rows if (row.get("competition"), int(row.get("season_id", 0))) != key]
            rows.append(result)
            save(rows)
            print(f"{result['status']} {result['competition']} {result['season_label']} sid={result['season_id']} event={result.get('event_id')} by={result.get('recovered_by')}", flush=True)
    save(rows)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Recover no_event rows from coverage audit with fallback event discovery")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--no-details", action="store_true", help="Only find event IDs; do not fetch detail endpoints")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
