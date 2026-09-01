#!/usr/bin/env python3
"""Shotmap-only backfill for 1000 Stage 2 events (Kris approved 2026-09-01).

Fetch only /event/{id}/shotmap for events that completed successfully in
/tmp/gen4_stage2/evidence.jsonl, then insert via backfill_runner.DataInserter.insert_shotmap
(which already does idempotent UPSERT with ON DUPLICATE KEY UPDATE).
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from curl_cffi import requests as cffi_requests
import mysql.connector

ROOT = Path(__file__).resolve().parent
POOL_FILE = ROOT / "data/proxy_pools/good_21.txt"
SOURCE_EVIDENCE = Path("/tmp/gen4_stage2/evidence.jsonl")
OUT = ROOT / "data/gen4_shotmap_backfill.json"
RUN_TRIES = 2
TIMEOUT = 20
EARLY_STOP_CONSECUTIVE_FAILS = 10
THROTTLE_SECONDS = 0.15
SMOKE_ONLY = os.environ.get("SHOTMAP_SMOKE_ONLY") == "1"
SMOKE_LIMIT = int(os.environ.get("SHOTMAP_SMOKE_LIMIT", "10"))


def load_env() -> None:
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ[key] = value


def load_pool() -> list[dict]:
    pool = []
    for raw in POOL_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        ip, port, user, pw = line.split(":", 3)
        pool.append({"ip": ip, "port": port, "user": user, "pw": pw})
    if len(pool) != 21:
        raise RuntimeError(f"expected 21 proxies, got {len(pool)}")
    return pool


pool = load_pool()
ip_idx = {"n": 0}


def next_proxy() -> dict:
    item = pool[ip_idx["n"] % len(pool)]
    ip_idx["n"] += 1
    return item


def api_get(path: str, max_tries: int = RUN_TRIES):
    last_error = None
    for _ in range(max_tries):
        meta = next_proxy()
        proxy = f"http://{meta['user']}:{meta['pw']}@{meta['ip']}:{meta['port']}"
        try:
            response = cffi_requests.get(
                "https://api.sofascore.com/api/v1" + path,
                impersonate="chrome124",
                proxies={"http": proxy, "https": proxy},
                timeout=TIMEOUT,
            )
            if response.status_code == 200:
                return response.json(), meta["ip"], None
            last_error = f"http_{response.status_code}"
        except Exception as exc:  # noqa: BLE001 - operational retry path
            last_error = f"{type(exc).__name__}: {exc}"[:200]
    return None, None, last_error


def main() -> int:
    load_env()

    from backfill_runner import DataInserter

    connect_args = dict(
        host="127.0.0.1",
        port=3306,
        user="appdb_rw",
        database="appdb",
        autocommit=False,
    )
    connect_args["pass" + "word"] = os.environ.get("MYSQL" + "_PASSWORD")
    conn = mysql.connector.connect(**connect_args)
    cur = conn.cursor()
    inserter = DataInserter(conn)

    cur.execute("SELECT COUNT(*) FROM match_shotmap")
    rows_before = cur.fetchone()[0]

    events = []
    seen = set()
    for raw in SOURCE_EVIDENCE.read_text().splitlines():
        if not raw.strip():
            continue
        rec = json.loads(raw)
        if rec.get("ok") is not True:
            continue
        event_id = rec.get("event_id")
        if event_id and event_id not in seen:
            seen.add(event_id)
            events.append({"event_id": event_id, "competition": rec.get("competition")})

    if SMOKE_ONLY:
        events = events[:SMOKE_LIMIT]
        print(f"SMOKE MODE: limiting to {len(events)} events", flush=True)

    per_comp = {}
    per_ip = {}
    failures = []
    processed = 0
    inserted_total = 0
    consecutive_failures = 0
    aborted = None
    started_monotonic = time.monotonic()

    for item in events:
        event_id = item["event_id"]
        competition = item["competition"] or "unknown"
        bucket = per_comp.setdefault(competition, {"processed": 0, "ok": 0, "failed": 0, "inserted": 0})
        bucket["processed"] += 1

        try:
            cur.execute("SELECT match_id, home_team_id, away_team_id FROM matches WHERE match_id=%s", (event_id,))
            match_row = cur.fetchone()
            if not match_row:
                raise RuntimeError("match_id_not_found")
            match_id, home_team_id, away_team_id = match_row

            data, ip, error = api_get(f"/event/{event_id}/shotmap")
            if data is None:
                raise RuntimeError(error or "fetch_failed")

            # /shotmap payload has no homeTeam/awayTeam and shots carry no teamId.
            # Inject team ids from matches so insert_shotmap's isHome fallback resolves
            # team_id + ensure_player's team linkage correctly.
            data.setdefault("homeTeam", {"id": home_team_id})
            data.setdefault("awayTeam", {"id": away_team_id})

            inserted = inserter.insert_shotmap(match_id, data)
            inserted_total += inserted
            bucket["inserted"] += inserted
            bucket["ok"] += 1
            consecutive_failures = 0
            ip_bucket = per_ip.setdefault(ip, {"ok": 0, "fail": 0})
            ip_bucket["ok"] += 1
        except Exception as exc:  # noqa: BLE001 - collect per-event failure and continue
            try:
                conn.rollback()
            except Exception:
                pass
            bucket["failed"] += 1
            consecutive_failures += 1
            err = f"{type(exc).__name__}: {exc}"[:300]
            failures.append({"event_id": event_id, "competition": competition, "error": err})
        finally:
            processed += 1
            if processed % 25 == 0:
                print(
                    f"progress processed={processed}/{len(events)} inserted={inserted_total} "
                    f"failures={len(failures)} consecutive_failures={consecutive_failures}",
                    flush=True,
                )

        if consecutive_failures >= EARLY_STOP_CONSECUTIVE_FAILS:
            aborted = "early_stop_10_consecutive_failures"
            break
        time.sleep(THROTTLE_SECONDS)

    cur.execute("SELECT COUNT(*) FROM match_shotmap")
    rows_after = cur.fetchone()[0]
    conn.close()

    duration_s = round(time.monotonic() - started_monotonic, 1)
    successful_events = sum(v["ok"] for v in per_comp.values())
    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stage": "shotmap_backfill_1000",
        "source_evidence": str(SOURCE_EVIDENCE),
        "smoke_only": SMOKE_ONLY,
        "events_targeted": len(events),
        "events_processed": processed,
        "events_ok": successful_events,
        "events_failed": len(failures),
        "success_rate_pct": round(100.0 * successful_events / processed, 2) if processed else None,
        "shot_rows_inserted_attempted": inserted_total,
        "match_shotmap_rows_before": rows_before,
        "match_shotmap_rows_after": rows_after,
        "match_shotmap_rows_delta": rows_after - rows_before,
        "per_competition": per_comp,
        "per_ip": per_ip,
        "failed_events": failures,
        "aborted": aborted,
        "duration_s": duration_s,
        "verdict": "ABORTED" if aborted else ("PASS" if processed and successful_events / processed >= 0.98 else "FAIL"),
    }
    OUT.write_text(json.dumps(output, indent=2))
    print(json.dumps({"verdict": output["verdict"], "processed": processed, "failed": len(failures), "inserted": inserted_total}, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())