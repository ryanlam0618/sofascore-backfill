#!/usr/bin/env python3
"""gen4_stage4c_batch_B.py — Batch B: zero comps shotmap backfill.

Kris Stage 4c Option D approval 2026-09-07 05:37 GMT+8.
Targets 9 "zero comps" (top-5 leagues + UCL + JP/AU cups) that Stage 4b skipped.
Fetches ONLY shotmap endpoint for missing events.

Method: curl_cffi impersonate="chrome124", good_20260907.txt (33 IPs).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

POOL_FILE = ROOT / "data/proxy_pools/good_20260907.txt"
REPORT_FILE = ROOT / "data/gen4_stage4c_batch_B_zero_comps_shotmap_report.json"
EVIDENCE_FILE = ROOT / "data/gen4_stage4c_batch_B_zero_comps_shotmap_evidence.jsonl"

CONCURRENCY = 8
PACING_S = 0.15
MAX_RETRIES = 3
TIMEOUT = 20
COOLDOWN_S = 30
EARLY_STOP_CONSECUTIVE_403 = 5
SMOKE_ONLY = os.environ.get("STAGE4C_SMOKE_ONLY") == "1"
SMOKE_LIMIT = int(os.environ.get("STAGE4C_SMOKE_LIMIT", "20"))

# Batch B: 9 zero comps (excluded from Stage 4b via ALREADY_DONE_UT)
TARGET_COMP_IDS = [
    7,      # UCL
    8,      # La Liga
    17,     # Premier League
    23,     # Serie A
    35,     # Bundesliga
    101,    # J.League Cup
    196,    # J1 League
    323,    # Emperor's Cup
    1786,   # Australia Cup
]


class PoolRotator:
    def __init__(self, pool_file, cooldown_s=30.0):
        self.pool = []
        for line in pool_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                parts = line.split(":", 3)
                if len(parts) == 4:
                    ip, port, user, pw = parts
                    self.pool.append({"ip": ip, "port": port, "user": user, "pw": pw})
        if len(self.pool) < 10:
            raise RuntimeError(f"pool too small: {len(self.pool)}")
        self.lock = threading.Lock()
        self.index = 0
        self.cooldown = {}
        self.cooldown_s = cooldown_s
        self.stats = {m["ip"]: {"ok": 0, "fail": 0} for m in self.pool}

    def next(self):
        with self.lock:
            now = time.monotonic()
            available = [i for i, m in enumerate(self.pool)
                         if self.cooldown.get(m["ip"], 0) <= now]
            if not available:
                earliest = min(self.cooldown.values()) if self.cooldown else now
                wait = earliest - now + 0.1
                if wait > 0:
                    time.sleep(wait)
                available = list(range(len(self.pool)))
            idx = available[self.index % len(available)]
            self.index += 1
            return self.pool[idx]

    def mark_bad(self, ip):
        if self.cooldown_s <= 0:
            return
        with self.lock:
            self.cooldown[ip] = time.monotonic() + self.cooldown_s
            if ip in self.stats:
                self.stats[ip]["fail"] += 1

    def mark_ok(self, ip):
        with self.lock:
            if ip in self.stats:
                self.stats[ip]["ok"] += 1


POOL = PoolRotator(POOL_FILE, COOLDOWN_S)


def _fetch(path, retries=MAX_RETRIES):
    used_ips = set()
    last_err = None
    for attempt in range(retries + 1):
        meta = POOL.next()
        ip = meta["ip"]
        if ip in used_ips and len(used_ips) < len(POOL.pool):
            continue
        used_ips.add(ip)
        proxy = "http://{}:{}@{}:{}".format(meta["user"], meta["pw"], meta["ip"], meta["port"])
        try:
            from curl_cffi import requests as cffi_requests
            r = cffi_requests.get(
                "https://api.sofascore.com/api/v1" + path,
                impersonate="chrome124",
                proxies={"http": proxy, "https": proxy},
                timeout=TIMEOUT,
            )
            if r.status_code == 200:
                try:
                    POOL.mark_ok(ip)
                    return ("ok", r.json(), 200, ip, None)
                except Exception as e:
                    last_err = f"invalid_json: {str(e)[:120]}"
                    POOL.mark_bad(ip)
                    continue
            if r.status_code == 404:
                return ("no_data", None, 404, ip, "http_404")
            last_err = f"http_{r.status_code}"
            POOL.mark_bad(ip)
            continue
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:120]}"
            POOL.mark_bad(ip)
            continue
    return ("ip_banned", None, None, None, last_err)


class WorkerCtx:
    def __init__(self, conn):
        self.conn = conn

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


def open_worker_conn():
    import mysql.connector
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    return mysql.connector.connect(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ.get("MYSQL_USER", "appdb_rw"),
        password=os.environ.get("MYSQL_PASSWORD", ""),
        database=os.environ.get("MYSQL_DATABASE", "appdb"),
        autocommit=False,
    )


def process_event(ctx, match_id, home_team_id, away_team_id, pacing_lock, ev_lock):
    ev = {"match_id": match_id, "result": "ok", "rows": 0, "shot_count": 0,
          "ip": None, "elapsed_ms": 0}
    t0 = time.monotonic()

    with pacing_lock:
        time.sleep(PACING_S)
    kind, body, http, ip, err = _fetch(f"/event/{match_id}", retries=MAX_RETRIES)
    if kind != "ok":
        ev["result"] = f"event_{kind}"
        ev["ip"] = ip
        ev["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        with ev_lock:
            return ev

    event = body.get("event", body) if isinstance(body, dict) else {}
    sc = event.get("status", {})
    code = sc.get("code") if isinstance(sc, dict) else None
    if code not in (100, 110, 120):
        ev["result"] = "not_ended"
        ev["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        with ev_lock:
            return ev

    with pacing_lock:
        time.sleep(PACING_S)
    kind, body, http, ip, err = _fetch(f"/event/{match_id}/shotmap", retries=MAX_RETRIES)
    ev["ip"] = ip

    if kind == "no_data":
        ev["result"] = "no_data"
    elif kind == "ip_banned":
        ev["result"] = "ip_banned"
    elif kind == "ok":
        shots = (body or {}).get("shotmap", []) or []
        ev["shot_count"] = len(shots)
        if len(shots) == 0:
            ev["result"] = "ok_empty"
        else:
            if "homeTeam" not in body:
                body["homeTeam"] = event.get("homeTeam", {}) or {"id": home_team_id}
            if "awayTeam" not in body:
                body["awayTeam"] = event.get("awayTeam", {}) or {"id": away_team_id}
            from backfill_runner import DataInserter
            inserter = DataInserter(ctx.conn)
            try:
                rows = inserter.insert_shotmap(match_id, body)
                ctx.conn.commit()
                ev["rows"] = rows
                ev["result"] = "ok_written"
            except Exception as e:
                try:
                    ctx.conn.rollback()
                except Exception:
                    pass
                ev["result"] = "insert_error"
                ev["insert_error"] = f"{type(e).__name__}: {str(e)[:160]}"

    ev["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
    with ev_lock:
        return ev


def main():
    import argparse
    global CONCURRENCY, PACING_S, MAX_RETRIES, COOLDOWN_S, EARLY_STOP_CONSECUTIVE_403
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrency", type=int, default=CONCURRENCY)
    parser.add_argument("--pacing", type=float, default=PACING_S)
    parser.add_argument("--retries", type=int, default=MAX_RETRIES)
    parser.add_argument("--cooldown", type=float, default=COOLDOWN_S)
    parser.add_argument("--early-stop", type=int, default=EARLY_STOP_CONSECUTIVE_403)
    args = parser.parse_args()
    CONCURRENCY = args.concurrency
    PACING_S = args.pacing
    MAX_RETRIES = args.retries
    COOLDOWN_S = args.cooldown
    EARLY_STOP_CONSECUTIVE_403 = args.early_stop
    POOL.cooldown_s = COOLDOWN_S

    t_start = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()

    import mysql.connector
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    conn = mysql.connector.connect(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ.get("MYSQL_USER", "appdb_rw"),
        password=os.environ.get("MYSQL_PASSWORD", ""),
        database=os.environ.get("MYSQL_DATABASE", "appdb"),
        autocommit=True,
    )
    cur = conn.cursor()

    placeholders = ",".join(["%s"] * len(TARGET_COMP_IDS))
    cur.execute(f"""
        SELECT m.match_id, m.home_team_id, m.away_team_id
        FROM matches m
        JOIN seasons s ON m.season_id = s.season_id
        JOIN competitions c ON s.competition_id = c.competition_id
        LEFT JOIN match_shotmap ms ON ms.match_id = m.match_id
        WHERE s.year_label = '25/26'
          AND c.competition_id IN ({placeholders})
          AND m.status = 'finished'
          AND m.home_score IS NOT NULL
          AND ms.match_id IS NULL
        ORDER BY m.match_id
    """, tuple(TARGET_COMP_IDS))
    rows = cur.fetchall()
    conn.close()

    print(f"[Stage4c Batch B] Candidate events missing shotmap: {len(rows)}", flush=True)
    if not rows:
        print("[Stage4c Batch B] No events to process.", flush=True)
        return 0

    if SMOKE_ONLY:
        rows = rows[:SMOKE_LIMIT]
        print(f"[Stage4c Batch B] SMOKE MODE: {len(rows)} events", flush=True)

    EVIDENCE_FILE.write_text("")
    ef = EVIDENCE_FILE.open("a")

    counters = {"ok_written": 0, "ok_empty": 0, "no_data": 0, "ip_banned": 0,
                "not_ended": 0, "event_no_data": 0, "event_ip_banned": 0,
                "insert_error": 0, "total": len(rows)}
    rows_inserted = 0
    pacing_lock = threading.Lock()
    ev_lock = threading.Lock()
    consecutive_403 = 0
    early_stop_triggered = False

    def worker(mid, hid, aid):
        ctx_conn = open_worker_conn()
        ctx = WorkerCtx(ctx_conn)
        try:
            return process_event(ctx, mid, hid, aid, pacing_lock, ev_lock)
        finally:
            ctx.close()

    done = 0
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futures = {ex.submit(worker, r[0], r[1], r[2]): r for r in rows}
        for fut in as_completed(futures):
            if early_stop_triggered:
                break
            ev = fut.result()
            done += 1
            res = ev["result"]
            counters[res] = counters.get(res, 0) + 1
            rows_inserted += ev.get("rows", 0)
            if res in ("ip_banned", "event_ip_banned"):
                consecutive_403 += 1
            else:
                consecutive_403 = 0
            ef.write(json.dumps(ev) + "\n")
            ef.flush()
            if done % 25 == 0 or done == len(rows):
                print(f"  [{done}/{len(rows)}] ok_w={counters['ok_written']} "
                      f"empty={counters['ok_empty']} no_data={counters['no_data']} "
                      f"ip_banned={counters['ip_banned']} not_ended={counters['not_ended']} "
                      f"rows={rows_inserted} consec_403={consecutive_403}", flush=True)
            if consecutive_403 >= EARLY_STOP_CONSECUTIVE_403:
                print(f"!! EARLY STOP: {EARLY_STOP_CONSECUTIVE_403} consecutive 403s", flush=True)
                early_stop_triggered = True

    ef.close()
    duration = round(time.monotonic() - t_start, 1)

    report = {
        "timestamp": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "task": "stage4c_batch_B_zero_comps_shotmap",
        "pool_file": str(POOL_FILE),
        "pool_size": len(POOL.pool),
        "target_comp_ids": TARGET_COMP_IDS,
        "events_targeted": len(rows),
        "events_processed": done,
        "duration_s": duration,
        "counters": counters,
        "rows_inserted": rows_inserted,
        "early_stop_triggered": early_stop_triggered,
    }
    REPORT_FILE.write_text(json.dumps(report, indent=2))
    print(f"\n[Stage4c Batch B] DONE duration={duration}s early_stop={early_stop_triggered}")
    print(json.dumps({k: v for k, v in counters.items() if k != "total"}, indent=2))
    print(f"rows_inserted={rows_inserted} report={REPORT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())