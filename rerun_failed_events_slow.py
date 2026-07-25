#!/usr/bin/env python3
"""Slow failed-event rerunner for SofaScore backfill.

Reads data/backfill_sofascore_10y/progress_*.json, retries failed event IDs one at
a time, and lets ProgressTracker.mark_done remove successful IDs from failed list.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv

import backfill_runner as runner

WORKDIR = Path(__file__).parent
DATA_DIR = WORKDIR / "data" / "backfill_sofascore_10y"


def progress_competition_name(path: Path) -> str:
    name = path.stem.removeprefix("progress_")
    return re.sub(r"_\d+$", "", name).replace("_", " ")


def failed_event_ids(state: dict) -> list[int]:
    done = set(int(eid) for eid in state.get("events_done", []))
    result: list[int] = []
    seen: set[int] = set()
    for item in state.get("events_failed", []):
        if isinstance(item, dict):
            eid = item.get("id")
        else:
            eid = item
        if eid is None:
            continue
        eid = int(eid)
        if eid in done or eid in seen:
            continue
        seen.add(eid)
        result.append(eid)
    return result


def iter_failed_progress_files(paths: Iterable[Path], *, competition: str | None, season_id: int | None):
    for path in sorted(paths):
        if season_id is not None and not path.name.endswith(f"_{season_id}.json"):
            continue
        comp_name = progress_competition_name(path)
        if competition and comp_name.lower() != competition.lower():
            continue
        try:
            state = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        failed = failed_event_ids(state)
        if failed:
            yield path, comp_name, failed


def load_season_competition_ids(conn) -> dict[int, int]:
    cursor = conn.cursor()
    cursor.execute("SELECT season_id, competition_id FROM seasons")
    return {int(season_id): int(competition_id) for season_id, competition_id in cursor.fetchall()}


async def main() -> int:
    parser = argparse.ArgumentParser(description="Retry failed SofaScore events slowly, one by one")
    parser.add_argument("--competition", help="Restrict to one competition name")
    parser.add_argument("--season-id", type=int, help="Restrict to one season ID")
    parser.add_argument("--max-events", type=int, default=1, help="Max failed events to retry in this run; 0 = no limit")
    parser.add_argument("--sleep-min", type=float, default=45.0, help="Minimum sleep between events")
    parser.add_argument("--sleep-max", type=float, default=90.0, help="Maximum sleep between events")
    parser.add_argument("--sticky-session-key", help="Optional sticky proxy session key")
    parser.add_argument("--headful", action="store_true", help="Run browser with visible UI")
    parser.add_argument("--dry-run", action="store_true", help="Only list queued failed events")
    args = parser.parse_args()

    load_dotenv(WORKDIR / ".env")
    conn = runner.get_mysql_conn()
    season_competition_ids = load_season_competition_ids(conn)
    inserter = runner.DataInserter(conn)

    queue: list[tuple[Path, str, int, int]] = []
    progress_files = DATA_DIR.glob("progress_*.json")
    for path, comp_name, failed in iter_failed_progress_files(
        progress_files,
        competition=args.competition,
        season_id=args.season_id,
    ):
        sid = int(path.stem.rsplit("_", 1)[1])
        competition_id = season_competition_ids.get(sid)
        if competition_id is None:
            print(f"SKIP {path.name}: season_id={sid} not found in DB")
            continue
        for eid in failed:
            queue.append((path, comp_name, sid, eid))

    print(f"failed_event_queue={len(queue)}")
    for path, comp_name, sid, eid in queue[:20]:
        print(f"QUEUE {comp_name} season={sid} event={eid} progress={path.name}")
    if len(queue) > 20:
        print(f"... {len(queue) - 20} more")

    if args.dry_run or not queue:
        conn.close()
        return 0

    max_events = len(queue) if args.max_events == 0 else min(args.max_events, len(queue))
    processed = 0
    succeeded = 0
    failed = 0

    async with runner.BackfillClient(
        headless=not args.headful,
        sticky_session_key=args.sticky_session_key,
    ) as client:
        await client.warm_homepage()
        for path, comp_name, sid, eid in queue[:max_events]:
            competition_id = season_competition_ids[sid]
            progress = runner.ProgressTracker(path)
            if progress.is_done(eid):
                print(f"SKIP already done event={eid}")
                continue
            season_stats = {
                "processed": 0,
                "skipped": 0,
                "failed": 0,
                "incidents": 0,
                "lineups": 0,
                "statistics": 0,
                "shotmap": 0,
                "graph": 0,
                "odds": 0,
                "comments": 0,
            }
            processed += 1
            print(f"\nRETRY {processed}/{max_events} {comp_name} season={sid} event={eid}")
            ok = await runner._process_event_with_retries(
                client,
                inserter,
                progress,
                season_stats,
                eid,
                sid,
                competition_id,
                reporter=None,
            )
            if ok:
                succeeded += 1
                print(f"OK event={eid} stats={season_stats}")
            else:
                failed += 1
                print(f"FAIL event={eid} stats={season_stats}")
            if processed < max_events:
                sleep_s = random.uniform(args.sleep_min, args.sleep_max)
                print(f"sleep={sleep_s:.1f}s")
                await asyncio.sleep(sleep_s)

    conn.close()
    print(f"\nSUMMARY processed={processed} succeeded={succeeded} failed={failed} remaining_initial={len(queue) - processed}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
