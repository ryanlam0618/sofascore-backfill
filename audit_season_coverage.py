#!/usr/bin/env python3
"""Sample one match per configured competition season and compare data coverage.

This is a read-only SofaScore audit: it fetches API bundles through the repo's
browser client and writes CSV/JSON reports. It does not insert into MySQL.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backfill_runner import BackfillClient, load_competitions


OUT_DIR = Path("data/coverage_audit")
STATE_PATH = OUT_DIR / "season_sample_coverage.json"
CSV_PATH = OUT_DIR / "season_sample_coverage.csv"
SUMMARY_JSON_PATH = OUT_DIR / "coverage_summary.json"
SUMMARY_MD_PATH = OUT_DIR / "coverage_summary.md"

ENDPOINTS = {
    "event": "/api/v1/event/{event_id}",
    "lineups": "/api/v1/event/{event_id}/lineups",
    "statistics": "/api/v1/event/{event_id}/statistics",
    "incidents": "/api/v1/event/{event_id}/incidents",
    "shotmap": "/api/v1/event/{event_id}/shotmap",
    "graph": "/api/v1/event/{event_id}/graph",
    "comments": "/api/v1/event/{event_id}/comments",
    "odds": "/api/v1/event/{event_id}/odds/1/all",
}


def _season_start_year(label: str) -> int:
    if not label:
        return 0
    if "/" in label:
        left = label.split("/", 1)[0]
        if len(left) == 2 and left.isdigit():
            yy = int(left)
            return 2000 + yy if yy <= 50 else 1900 + yy
    if len(label) >= 4 and label[:4].isdigit():
        return int(label[:4])
    return 0


def _iter_values(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _iter_values(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _iter_values(value)


def _count_key(obj: Any, *keys: str) -> int:
    wanted = set(keys)
    count = 0
    for item in _iter_values(obj):
        for key in wanted:
            if item.get(key) not in (None, ""):
                count += 1
    return count


def _list_len(obj: Any, key: str) -> int:
    if isinstance(obj, dict) and isinstance(obj.get(key), list):
        return len(obj[key])
    return 0


def _lineup_player_count(lineups: Any) -> int:
    count = 0
    for item in _iter_values(lineups):
        if isinstance(item.get("players"), list):
            count += len(item["players"])
    return count


def _statistics_group_count(statistics: Any) -> int:
    return _list_len(statistics, "statistics")


def summarize_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    odds_markets = _list_len(bundle.get("odds"), "markets")
    odds_choices = 0
    if isinstance(bundle.get("odds"), dict):
        for market in bundle["odds"].get("markets", []):
            if isinstance(market, dict):
                odds_choices += len(market.get("choices", []) or [])

    event = bundle.get("event", {})
    event_obj = event.get("event", event) if isinstance(event, dict) else {}

    return {
        "lineup_players": _lineup_player_count(bundle.get("lineups")),
        "statistics_groups": _statistics_group_count(bundle.get("statistics")),
        "incidents": _list_len(bundle.get("incidents"), "incidents"),
        "shotmap_shots": _list_len(bundle.get("shotmap"), "shotmap"),
        "graph_points": _list_len(bundle.get("graph"), "graphPoints"),
        "comments": _list_len(bundle.get("comments"), "comments"),
        "odds_markets": odds_markets,
        "odds_choices": odds_choices,
        "xg_values": _count_key(bundle, "xg", "expectedGoals"),
        "xa_values": _count_key(bundle, "xa", "expectedAssists"),
        "xgot_values": _count_key(bundle, "xgot", "expectedGoalsOnTarget"),
        "top_speed_values": _count_key(bundle, "topSpeed"),
        "total_pass_values": _count_key(bundle, "totalPass"),
        "event_status": event_obj.get("status", {}).get("type") if isinstance(event_obj.get("status"), dict) else None,
        "home_score": event_obj.get("homeScore", {}).get("current") if isinstance(event_obj.get("homeScore"), dict) else None,
        "away_score": event_obj.get("awayScore", {}).get("current") if isinstance(event_obj.get("awayScore"), dict) else None,
    }


def status_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"ok": 0, "error": 0, "no_event": 0}
    for row in rows:
        status = str(row.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def build_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok_rows = [row for row in rows if row.get("status") == "ok"]
    older_rows = [row for row in ok_rows if _season_start_year(str(row.get("season_label") or "")) < 2020]
    recent_rows = [row for row in ok_rows if _season_start_year(str(row.get("season_label") or "")) >= 2020]

    def availability(sample: list[dict[str, Any]], key: str) -> dict[str, int]:
        return {
            "rows": len(sample),
            "rows_with_values": sum(1 for row in sample if int(row.get(key) or 0) > 0),
            "total_values": sum(int(row.get(key) or 0) for row in sample),
        }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_rows": len(rows),
        "status_counts": status_counts(rows),
        "competitions": len({row.get("competition") for row in rows if row.get("competition")}),
        "ok_rows": len(ok_rows),
        "older_pre_2020_ok_rows": len(older_rows),
        "recent_2020_plus_ok_rows": len(recent_rows),
        "availability": {
            "all": {
                "xg": availability(ok_rows, "xg_values"),
                "xa": availability(ok_rows, "xa_values"),
                "xgot": availability(ok_rows, "xgot_values"),
                "topSpeed": availability(ok_rows, "top_speed_values"),
                "totalPass": availability(ok_rows, "total_pass_values"),
            },
            "older_pre_2020": {
                "xg": availability(older_rows, "xg_values"),
                "xa": availability(older_rows, "xa_values"),
                "xgot": availability(older_rows, "xgot_values"),
                "topSpeed": availability(older_rows, "top_speed_values"),
                "totalPass": availability(older_rows, "total_pass_values"),
            },
            "recent_2020_plus": {
                "xg": availability(recent_rows, "xg_values"),
                "xa": availability(recent_rows, "xa_values"),
                "xgot": availability(recent_rows, "xgot_values"),
                "topSpeed": availability(recent_rows, "top_speed_values"),
                "totalPass": availability(recent_rows, "total_pass_values"),
            },
        },
    }


def write_summary(rows: list[dict[str, Any]]) -> None:
    summary = build_summary(rows)
    SUMMARY_JSON_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    counts = summary["status_counts"]
    older_xg = summary["availability"]["older_pre_2020"]["xg"]
    older_xa = summary["availability"]["older_pre_2020"]["xa"]
    recent_xg = summary["availability"]["recent_2020_plus"]["xg"]
    recent_xa = summary["availability"]["recent_2020_plus"]["xa"]
    lines = [
        "# SofaScore Season Sample Coverage Audit",
        "",
        f"Generated at: {summary['generated_at']}",
        "",
        f"Total rows: {summary['total_rows']}",
        f"OK: {counts.get('ok', 0)}",
        f"Error: {counts.get('error', 0)}",
        f"No event: {counts.get('no_event', 0)}",
        "",
        "## xG/xA availability",
        "",
        f"Pre-2020 OK rows with xG: {older_xg['rows_with_values']}/{older_xg['rows']} ({older_xg['total_values']} values)",
        f"Pre-2020 OK rows with xA: {older_xa['rows_with_values']}/{older_xa['rows']} ({older_xa['total_values']} values)",
        f"2020+ OK rows with xG: {recent_xg['rows_with_values']}/{recent_xg['rows']} ({recent_xg['total_values']} values)",
        f"2020+ OK rows with xA: {recent_xa['rows_with_values']}/{recent_xa['rows']} ({recent_xa['total_values']} values)",
        "",
        "See season_sample_coverage.csv and season_sample_coverage.json for row-level endpoint statuses and counts.",
    ]
    SUMMARY_MD_PATH.write_text("\n".join(lines) + "\n")


def load_state() -> list[dict[str, Any]]:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return []


def save_state(rows: list[dict[str, Any]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    if rows:
        fieldnames = sorted({key for row in rows for key in row.keys()})
        with CSV_PATH.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    write_summary(rows)


async def first_event_for_season(client: BackfillClient, ut_id: int, season_id: int) -> tuple[int | None, str | None]:
    rounds = await client.fetch_api(f"/api/v1/unique-tournament/{ut_id}/season/{season_id}/rounds")
    if rounds.get("status") != 200:
        return None, f"rounds HTTP {rounds.get('status')}"
    round_numbers = [item.get("round") for item in rounds.get("body", {}).get("rounds", []) if item.get("round")]
    for round_number in round_numbers:
        await client.sleep_between(0.5, 1.0)
        events = await client.fetch_api(f"/api/v1/unique-tournament/{ut_id}/season/{season_id}/events/round/{round_number}")
        if events.get("status") != 200:
            continue
        for event in events.get("body", {}).get("events", []):
            event_id = event.get("id")
            if event_id:
                return int(event_id), None
    return None, "no event found"


async def fetch_bundle(client: BackfillClient, event_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
    bundle: dict[str, Any] = {}
    statuses: dict[str, Any] = {}
    await client.warm_event(event_id)
    for name, template in ENDPOINTS.items():
        await client.sleep_between(0.5, 1.0)
        result = await client.fetch_api(template.format(event_id=event_id))
        statuses[f"{name}_status"] = result.get("status")
        if result.get("status") == 200:
            bundle[name] = result.get("body", {})
    return bundle, statuses


async def run(args: argparse.Namespace) -> int:
    global OUT_DIR, STATE_PATH, CSV_PATH, SUMMARY_JSON_PATH, SUMMARY_MD_PATH
    if args.out_dir:
        OUT_DIR = Path(args.out_dir)
        STATE_PATH = OUT_DIR / "season_sample_coverage.json"
        CSV_PATH = OUT_DIR / "season_sample_coverage.csv"
        SUMMARY_JSON_PATH = OUT_DIR / "coverage_summary.json"
        SUMMARY_MD_PATH = OUT_DIR / "coverage_summary.md"

    existing = load_state()
    done_keys = {(row.get("competition"), int(row.get("season_id", 0))) for row in existing if row.get("season_id")}
    rows = existing[:]
    competitions = load_competitions()
    names = sorted(competitions)
    if args.competition:
        names = [name for name in names if name == args.competition]
    if args.limit_competitions:
        names = names[: args.limit_competitions]

    async with BackfillClient(headless=True) as client:
        await client.warm_homepage()
        for name in names:
            comp = competitions[name]
            seasons = {
                int(sid): label
                for sid, label in comp.get("seasons", {}).items()
                if _season_start_year(str(label)) >= args.from_year
            }
            season_items = sorted(seasons.items(), key=lambda item: item[1])
            if args.limit_seasons:
                season_items = season_items[: args.limit_seasons]
            for season_id, label in season_items:
                key = (name, season_id)
                if key in done_keys and not args.force:
                    continue
                row = {
                    "competition": name,
                    "ut_id": comp.get("ut_id"),
                    "season_id": season_id,
                    "season_label": label,
                    "sampled_at": datetime.now(timezone.utc).isoformat(),
                }
                try:
                    await client.warm_tournament(comp["ut_id"], season_id)
                    event_id, reason = await first_event_for_season(client, comp["ut_id"], season_id)
                    row["event_id"] = event_id
                    if not event_id:
                        row["status"] = "no_event"
                        row["error"] = reason
                    else:
                        bundle, statuses = await fetch_bundle(client, event_id)
                        row.update(statuses)
                        row.update(summarize_bundle(bundle))
                        row["status"] = "ok"
                except Exception as exc:  # keep audit resumable
                    row["status"] = "error"
                    row["error"] = str(exc)[:500]
                rows = [item for item in rows if not (item.get("competition") == name and int(item.get("season_id", 0)) == season_id)]
                rows.append(row)
                save_state(rows)
                print(f"{row['status']} {name} {label} sid={season_id} event={row.get('event_id')}", flush=True)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit SofaScore data coverage by sampling one match per season")
    parser.add_argument("--competition")
    parser.add_argument("--from-year", type=int, default=2015)
    parser.add_argument("--limit-competitions", type=int, default=0)
    parser.add_argument("--limit-seasons", type=int, default=0)
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
