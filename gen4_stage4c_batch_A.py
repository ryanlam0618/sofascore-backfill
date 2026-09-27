#!/usr/bin/env python3
"""gen4_stage4c_batch_A.py — Batch A: low-coverage comps, 4 endpoints.

Kris Stage 4c Option D approval 2026-09-07 05:37 GMT+8.
Targets 5 worst comps (335, 17015, 19, 329, 882) + 10 small-gap comps.
Fetches lineups, statistics, incidents, shotmap for missing events.

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
REPORT_FILE = ROOT / "data/gen4_stage4c_batch_A_low_coverage_comps_report.json"
EVIDENCE_FILE = ROOT / "data/gen4_stage4c_batch_A_low_coverage_comps_evidence.jsonl"

CONCURRENCY = 8
PACING_S = 0.15
MAX_RETRIES = 3
TIMEOUT = 20
COOLDOWN_S = 30
EARLY_STOP_CONSECUTIVE_403 = 5
SMOKE_ONLY = os.environ.get("STAGE4C_SMOKE_ONLY") == "1"
SMOKE_LIMIT = int(os.environ.get("STAGE4C_SMOKE_LIMIT", "20"))

# Batch A target comps: 5 worst + 10 small gaps
TARGET_COMP_IDS = [
    335,    # Coupe de France (200)
    17015,  # UECL (409)
    19,     # FA Cup (128)
    329,    # Copa del Rey (137)
    882,    # Chinese FA Cup (75)
    # 10 small-gap comps from Stage 4b
    34,     # Ligue 1
    679,    # UEL
    668,    # AFC CL Two
    649,    # Chinese Super League
    463,    # AFC Champions League
    410,    # K League 1
    217,    # DFB Pokal
    21,     # EFL Cup
    136,    # A-League Men
    328,    # Coppa Italia
]

# 4 endpoints to backfill
ENDPOINTS = ["lineups", "statistics", "incidents", "shotmap"]


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
    """Fetch JSON with retry-with-rotation. Returns (kind, body, http, ip, error)."""
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
    conn = mysql.connector.connect(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ.get("MYSQL_USER", "appdb_rw"),
        password=os.environ.get("MYSQL_PASSWORD", ""),
        database=os.environ.get("MYSQL_DATABASE", "appdb"),
        autocommit=False,
    )
    return conn


def process_event(ctx, match_id, home_team_id, away_team_id, pacing_lock, ev_lock):
    """Process one event: fetch event detail + 4 endpoints, insert."""
    from backfill_runner import DataInserter
    inserter = DataInserter(ctx.conn)

    ev = {
        "match_id": match_id,
        "result": "ok",
        "rows": {},
        "elapsed_ms": 0,
        "endpoints": {},
    }
    t0 = time.monotonic()

    # pacing + event detail
    with pacing_lock:
        time.sleep(PACING_S)
    kind, body, http, ip, err = _fetch(f"/event/{match_id}", retries=MAX_RETRIES)
    if kind != "ok":
        ev["result"] = f"event_{kind}"
        ev["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        with ev_lock:
            return ev
    event = body.get("event", body) if isinstance(body, dict) else {}
    sc = event.get("status", {})
    code = sc.get("code") if isinstance(sc, dict) else None
    # Accept ended (100), after ET (110), after penalties (120) as finished
    if code not in (100, 110, 120):
        ev["result"] = "not_ended"
        ev["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        with ev_lock:
            return ev

    # Fetch 4 endpoints
    endpoint_paths = {
        "lineups": f"/event/{match_id}/lineups",
        "statistics": f"/event/{match_id}/statistics",
        "incidents": f"/event/{match_id}/incidents",
        "shotmap": f"/event/{match_id}/shotmap",
    }

    for ep_name, ep_path in endpoint_paths.items():
        with pacing_lock:
            time.sleep(PACING_S)
        kind, body, http, ip, err = _fetch(ep_path, retries=MAX_RETRIES)
        ev["endpoints"][ep_name] = {"kind": kind, "http": http, "ip": ip, "error": err}
        if kind != "ok":
            continue

        # Inject team ids where needed
        if ep_name == "shotmap":
            if "homeTeam" not in body:
                body["homeTeam"] = event.get("homeTeam", {}) or {"id": home_team_id}
            if "awayTeam" not in body:
                body["awayTeam"] = event.get("awayTeam", {}) or {"id": away_team_id}

        # For lineups/incidents: inject homeTeam/awayTeam (with name) from event
        # so ensure_team/ensure_player can resolve team name (teams.name NOT NULL).
        if ep_name in ("lineups", "incidents"):
            if "homeTeam" not in body or not body.get("homeTeam"):
                body["homeTeam"] = event.get("homeTeam", {}) or {"id": home_team_id}
            if "awayTeam" not in body or not body.get("awayTeam"):
                body["awayTeam"] = event.get("awayTeam", {}) or {"id": away_team_id}

        # Sanitize lineups payload: ensure team name + player names are non-null
        if ep_name == "lineups" and isinstance(body, dict):
            for side, team_key in (("home", "homeTeam"), ("away", "awayTeam")):
                team = body.get(side) or {}
                # Ensure side-level team name
                if not team.get("name"):
                    team["name"] = body.get(team_key, {}).get("name") or "Unknown"
                for pe in team.get("players", []) or []:
                    p = pe.get("player") or {}
                    if isinstance(p, dict) and not p.get("name"):
                        p["name"] = "Unknown"
                        p.setdefault("shortName", "Unknown")

        # Insert
        try:
            if ep_name == "lineups":
                rows = inserter.insert_lineups(match_id, body)
            elif ep_name == "statistics":
                rows = inserter.insert_statistics(match_id, body)
            elif ep_name == "incidents":
                rows = inserter.insert_incidents(match_id, body)
            elif ep_name == "shotmap":
                rows = inserter.insert_shotmap(match_id, body)
            ctx.conn.commit()
            ev["rows"][ep_name] = rows
        except Exception as e:
            try:
                ctx.conn.rollback()
            except Exception:
                pass
            ev["endpoints"][ep_name]["insert_error"] = f"{type(e).__name__}: {str(e)[:160]}"
            ev["endpoints"][ep_name]["kind"] = "insert_error"

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

    print(f"[Stage4c Batch A] Starting at {started_at}", flush=True)
    print(f"  Pool: {len(POOL.pool)} IPs, Concurrency: {CONCURRENCY}, Pacing: {PACING_S}s", flush=True)
    print(f"  Target comps: {TARGET_COMP_IDS}", flush=True)

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
    # Find events missing any of the 4 endpoints
    cur.execute(f"""
        SELECT m.match_id, m.home_team_id, m.away_team_id, c.name, c.competition_id
        FROM matches m
        JOIN seasons s ON m.season_id = s.season_id
        JOIN competitions c ON s.competition_id = c.competition_id
        WHERE s.year_label = '25/26'
          AND c.competition_id IN ({placeholders})
          AND m.status = 'finished'
          AND m.home_score IS NOT NULL
          AND (
            NOT EXISTS (SELECT 1 FROM match_lineups ml WHERE ml.match_id = m.match_id)
            OR NOT EXISTS (SELECT 1 FROM match_statistics mst WHERE mst.match_id = m.match_id)
            OR NOT EXISTS (SELECT 1 FROM match_incidents mi WHERE mi.match_id = m.match_id)
            OR NOT EXISTS (SELECT 1 FROM match_shotmap ms WHERE ms.match_id = m.match_id)
          )
        ORDER BY m.match_id
    """, tuple(TARGET_COMP_IDS))
    rows = cur.fetchall()
    conn.close()

    print(f"[Stage4c Batch A] Candidate events missing >=1 endpoint: {len(rows)}", flush=True)
    if not rows:
        print("[Stage4c Batch A] No events to process.", flush=True)
        return 0

    if SMOKE_ONLY:
        rows = rows[:SMOKE_LIMIT]
        print(f"[Stage4c Batch A] SMOKE MODE: {len(rows)} events", flush=True)

    EVIDENCE_FILE.write_text("")
    ef = EVIDENCE_FILE.open("a")

    counters = {
        "ok": 0, "not_ended": 0, "event_no_data": 0, "event_ip_banned": 0,
        "insert_error": 0, "total": len(rows),
    }
    ep_counters = {ep: {"ok": 0, "no_data": 0, "ip_banned": 0, "insert_error": 0} for ep in ENDPOINTS}
    total_rows = 0
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
            for ep_name, ep_data in ev.get("endpoints", {}).items():
                k = ep_data.get("kind", "unknown")
                if k in ep_counters[ep_name]:
                    ep_counters[ep_name][k] += 1
            for ep_name, n in ev.get("rows", {}).items():
                total_rows += n

            if res in ("event_ip_banned",):
                consecutive_403 += 1
            else:
                consecutive_403 = 0

            ef.write(json.dumps(ev) + "\n")
            ef.flush()

            if done % 25 == 0 or done == len(rows):
                print(f"  [{done}/{len(rows)}] ok={counters['ok']} "
                      f"lineups_ok={ep_counters['lineups']['ok']} "
                      f"stats_ok={ep_counters['statistics']['ok']} "
                      f"incidents_ok={ep_counters['incidents']['ok']} "
                      f"shotmap_ok={ep_counters['shotmap']['ok']} "
                      f"rows={total_rows} consec_403={consecutive_403}", flush=True)

            if consecutive_403 >= EARLY_STOP_CONSECUTIVE_403:
                print(f"!! EARLY STOP: {EARLY_STOP_CONSECUTIVE_403} consecutive 403s", flush=True)
                early_stop_triggered = True

    ef.close()
    duration = round(time.monotonic() - t_start, 1)

    report = {
        "timestamp": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "task": "stage4c_batch_A_low_coverage_comps",
        "pool_file": str(POOL_FILE),
        "pool_size": len(POOL.pool),
        "target_comp_ids": TARGET_COMP_IDS,
        "events_targeted": len(rows),
        "events_processed": done,
        "duration_s": duration,
        "counters": counters,
        "endpoint_counters": ep_counters,
        "total_rows_inserted": total_rows,
        "early_stop_triggered": early_stop_triggered,
    }
    REPORT_FILE.write_text(json.dumps(report, indent=2))

    print(f"\n[Stage4c Batch A] DONE duration={duration}s early_stop={early_stop_triggered}")
    print(json.dumps({k: v for k, v in counters.items() if k != "total"}, indent=2))
    print(json.dumps(ep_counters, indent=2))
    print(f"total_rows={total_rows} report={REPORT_FILE}")

    return 0


if __name__ == "__main__":
    sys.exit(main())