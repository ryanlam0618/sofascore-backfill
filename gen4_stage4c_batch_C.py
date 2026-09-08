#!/usr/bin/env python3
"""gen4_stage4c_batch_C.py — Batch C: 7 new endpoints backfill.

Kris Stage 4c Option D approval 2026-09-07 05:37 GMT+8.
Targets all 25/26 finished matches, fetches 6 new endpoints:
  h2h, votes, graph(momentum), odds, average-positions, best-players
(player-statistics is embedded in lineups, already covered in Batch A).

Method: curl_cffi impersonate="chrome124", good_20260907.txt (33 IPs).
Inline insert methods (no backfill_runner edit except insert_odds reuse).
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
REPORT_FILE = ROOT / "data/gen4_stage4c_batch_C_new_endpoints_report.json"
EVIDENCE_FILE = ROOT / "data/gen4_stage4c_batch_C_new_endpoints_evidence.jsonl"

CONCURRENCY = 8
PACING_S = 0.15
MAX_RETRIES = 3
TIMEOUT = 20
COOLDOWN_S = 30
EARLY_STOP_CONSECUTIVE_403 = 5
SMOKE_ONLY = os.environ.get("STAGE4C_SMOKE_ONLY") == "1"
SMOKE_LIMIT = int(os.environ.get("STAGE4C_SMOKE_LIMIT", "20"))

# 6 endpoints (player-statistics embedded in lineups, skipped)
ENDPOINTS = [
    "h2h", "votes", "graph", "odds", "average_positions", "best_players",
]

ENDPOINT_PATHS = {
    "h2h": "/event/{}/h2h",
    "votes": "/event/{}/votes",
    "graph": "/event/{}/graph",
    "odds": "/event/{}/odds/1/all",
    "average_positions": "/event/{}/average-positions",
    "best_players": "/event/{}/best-players",
}


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
        proxy = "http://" + meta["user"] + ":" + meta["pw"] + "@" + meta["ip"] + ":" + meta["port"]
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


# ---------------------------------------------------------------------------
# Inline insert methods (self-contained; no backfill_runner dependency except odds)
# ---------------------------------------------------------------------------

def _ensure_player(conn, p):
    """Minimal ensure_player for new endpoints (insert players if missing)."""
    if not isinstance(p, dict) or not p.get("id"):
        return None
    pid = p["id"]
    name = p.get("name") or "Unknown"
    short = p.get("shortName") or name
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO players (player_id, name, short_name, position, slug)
           VALUES (%s,%s,%s,%s,%s)
           ON DUPLICATE KEY UPDATE name=COALESCE(VALUES(name),name),
               short_name=COALESCE(VALUES(short_name),short_name)""",
        (pid, name, short, p.get("position"), p.get("slug")),
    )
    return pid


def _insert_h2h(conn, match_id, home_team_id, away_team_id, body):
    td = (body or {}).get("teamDuel") or {}
    hw = td.get("homeWins") or 0
    aw = td.get("awayWins") or 0
    dr = td.get("draws") or 0
    total = hw + aw + dr
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO match_h2h (match_id, home_team_id, away_team_id,
               home_wins, draws, away_wins, total_matches)
           VALUES (%s,%s,%s,%s,%s,%s,%s)
           ON DUPLICATE KEY UPDATE home_wins=VALUES(home_wins), draws=VALUES(draws),
               away_wins=VALUES(away_wins), total_matches=VALUES(total_matches)""",
        (match_id, home_team_id, away_team_id, hw, dr, aw, total),
    )
    return 1


def _insert_votes(conn, match_id, body):
    vote = (body or {}).get("vote") or {}
    v1 = vote.get("vote1") or 0
    vx = vote.get("voteX") or 0
    v2 = vote.get("vote2") or 0
    total = v1 + vx + v2
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO match_votes (match_id, home_votes, draw_votes, away_votes,
               home_percentage, draw_percentage, away_percentage, fetched_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())
           ON DUPLICATE KEY UPDATE home_votes=VALUES(home_votes), draw_votes=VALUES(draw_votes),
               away_votes=VALUES(away_votes), fetched_at=NOW()""",
        (match_id, v1, vx, v2,
         round(100.0*v1/total, 2) if total else None,
         round(100.0*vx/total, 2) if total else None,
         round(100.0*v2/total, 2) if total else None),
    )
    return 1


def _insert_momentum(conn, match_id, body):
    points = (body or {}).get("graphPoints", []) or []
    cur = conn.cursor()
    count = 0
    for pt in points:
        minute = pt.get("minute")
        value = pt.get("value")
        if minute is None or value is None:
            continue
        period = "first" if float(minute) <= 45 else "second"
        # Signed momentum: positive = home, negative = away
        home_value = int(value) if int(value) >= 0 else 0
        away_value = -int(value) if int(value) < 0 else 0
        cur.execute(
            """INSERT INTO match_momentum (match_id, minute, period, home_value, away_value)
               VALUES (%s,%s,%s,%s,%s)
               ON DUPLICATE KEY UPDATE home_value=VALUES(home_value), away_value=VALUES(away_value)""",
            (match_id, minute, period, home_value, away_value),
        )
        count += 1
    return count


def _insert_avg_positions(conn, match_id, home_team_id, away_team_id, body):
    cur = conn.cursor()
    count = 0
    for side, team_id in (("home", home_team_id), ("away", away_team_id)):
        for entry in (body or {}).get(side, []) or []:
            p = entry.get("player") or {}
            pid = p.get("id")
            if not pid:
                continue
            _ensure_player(conn, p)
            ax = entry.get("averageX")
            ay = entry.get("averageY")
            cur.execute(
                """INSERT INTO match_average_positions (match_id, player_id, team_id, avg_x, avg_y)
                   VALUES (%s,%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE avg_x=VALUES(avg_x), avg_y=VALUES(avg_y)""",
                (match_id, pid, team_id, ax, ay),
            )
            count += 1
    return count


def _insert_best_players(conn, match_id, home_team_id, away_team_id, body):
    cur = conn.cursor()
    count = 0
    for key, team_id, is_home in (
        ("bestHomeTeamPlayer", home_team_id, 1),
        ("bestAwayTeamPlayer", away_team_id, 0),
    ):
        bp = (body or {}).get(key) or {}
        p = bp.get("player") or {}
        pid = p.get("id")
        if not pid:
            continue
        _ensure_player(conn, p)
        rating = bp.get("value")
        try:
            rating = float(rating) if rating is not None else None
        except (ValueError, TypeError):
            rating = None
        cur.execute(
            """INSERT INTO match_best_players (match_id, player_id, team_id, is_home, rating, `rank`)
               VALUES (%s,%s,%s,%s,%s,1)
               ON DUPLICATE KEY UPDATE rating=VALUES(rating)""",
            (match_id, pid, team_id, is_home, rating),
        )
        count += 1
    return count


INSERT_FUNCS = {
    "h2h": _insert_h2h,
    "votes": _insert_votes,
    "graph": _insert_momentum,
    "odds": None,  # reuse backfill_runner.insert_odds
    "average_positions": _insert_avg_positions,
    "best_players": _insert_best_players,
}


# MySQL error codes safe to retry: deadlock (1213), lock-wait timeout (1205).
_RETRYABLE_ERRNOS = (1213, 1205)


def _do_insert(ctx, inserter, ep_name, match_id, home_team_id, away_team_id, body):
    """Run the single-endpoint insert, returning row count."""
    if ep_name == "odds":
        return inserter.insert_odds(match_id, body)
    fn = INSERT_FUNCS[ep_name]
    if ep_name in ("h2h", "average_positions", "best_players"):
        return fn(ctx.conn, match_id, home_team_id, away_team_id, body)
    return fn(ctx.conn, match_id, body)


def _retry_deadlock(fn, *args, retries: int = 3, **kwargs):
    """Run ``fn``, retrying on InnoDB deadlock / lock-wait timeout.

    Under 8 concurrent workers, DELETE+INSERT / ON DUPLICATE writers on the
    shared ``match_odds`` / ``match_momentum`` / ``match_average_positions``
    tables hit ``InternalError 1213 (40001) Deadlock found``. InnoDB rolls
    back the victim transaction, so retrying the whole insert recovers it.
    """
    import time as _time
    attempt = 0
    while True:
        try:
            fn(*args, **kwargs)
            return
        except Exception as e:  # noqa: BLE001
            errno = getattr(e, "errno", None)
            if errno in _RETRYABLE_ERRNOS and attempt < retries:
                attempt += 1
                _time.sleep(0.1 * (2 ** attempt))
                continue
            raise


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
    ev = {"match_id": match_id, "result": "ok", "rows": {}, "endpoints": {},
          "elapsed_ms": 0}
    t0 = time.monotonic()

    from backfill_runner import DataInserter
    inserter = DataInserter(ctx.conn)

    for ep_name in ENDPOINTS:
        path = ENDPOINT_PATHS[ep_name].format(match_id)
        with pacing_lock:
            time.sleep(PACING_S)
        kind, body, http, ip, err = _fetch(path, retries=MAX_RETRIES)
        ev["endpoints"][ep_name] = {"kind": kind, "http": http, "ip": ip, "error": err}
        if kind != "ok":
            continue

        rows = 0
        try:
            rows = _do_insert(ctx, inserter, ep_name, match_id,
                              home_team_id, away_team_id, body)
            ctx.conn.commit()
        except Exception as e:
            # Deadlock recovery: retry the whole per-endpoint insert. InnoDB
            # rolls back the victim transaction, so a clean retry is safe.
            try:
                rows = _retry_deadlock(_do_insert, ctx, inserter, ep_name, match_id,
                                       home_team_id, away_team_id, body)
                ctx.conn.commit()
            except Exception:
                try:
                    ctx.conn.rollback()
                except Exception:
                    pass
                ev["endpoints"][ep_name]["insert_error"] = f"{type(e).__name__}: {str(e)[:160]}"
                ev["endpoints"][ep_name]["kind"] = "insert_error"
                continue
        ev["rows"][ep_name] = rows

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

    cur.execute("""
        SELECT m.match_id, m.home_team_id, m.away_team_id
        FROM matches m
        JOIN seasons s ON m.season_id = s.season_id
        WHERE s.year_label = '25/26'
          AND m.status = 'finished'
          AND m.home_score IS NOT NULL
        ORDER BY m.match_id
    """)
    rows = cur.fetchall()
    conn.close()

    print(f"[Stage4c Batch C] Candidate events: {len(rows)}", flush=True)
    if not rows:
        print("[Stage4c Batch C] No events.", flush=True)
        return 0

    if SMOKE_ONLY:
        rows = rows[:SMOKE_LIMIT]
        print(f"[Stage4c Batch C] SMOKE MODE: {len(rows)} events", flush=True)

    EVIDENCE_FILE.write_text("")
    ef = EVIDENCE_FILE.open("a")

    ep_counters = {ep: {"ok": 0, "no_data": 0, "ip_banned": 0, "insert_error": 0} for ep in ENDPOINTS}
    total_rows = 0
    pacing_lock = threading.Lock()
    ev_lock = threading.Lock()
    consecutive_403 = 0
    early_stop_triggered = False
    done = 0

    def worker(mid, hid, aid):
        ctx_conn = open_worker_conn()
        ctx = WorkerCtx(ctx_conn)
        try:
            return process_event(ctx, mid, hid, aid, pacing_lock, ev_lock)
        finally:
            ctx.close()

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futures = {ex.submit(worker, r[0], r[1], r[2]): r for r in rows}
        for fut in as_completed(futures):
            if early_stop_triggered:
                break
            ev = fut.result()
            done += 1
            any_403 = False
            for ep_name, ep_data in ev.get("endpoints", {}).items():
                k = ep_data.get("kind", "unknown")
                if k in ep_counters[ep_name]:
                    ep_counters[ep_name][k] += 1
                if k in ("ip_banned",):
                    any_403 = True
            for ep_name, n in ev.get("rows", {}).items():
                total_rows += n

            if any_403:
                consecutive_403 += 1
            else:
                consecutive_403 = 0

            ef.write(json.dumps(ev) + "\n")
            ef.flush()

            if done % 50 == 0 or done == len(rows):
                print(f"  [{done}/{len(rows)}] "
                      f"h2h={ep_counters['h2h']['ok']} votes={ep_counters['votes']['ok']} "
                      f"graph={ep_counters['graph']['ok']} odds={ep_counters['odds']['ok']} "
                      f"avgpos={ep_counters['average_positions']['ok']} "
                      f"bestpl={ep_counters['best_players']['ok']} "
                      f"rows={total_rows} consec_403={consecutive_403}", flush=True)

            if consecutive_403 >= EARLY_STOP_CONSECUTIVE_403:
                print(f"!! EARLY STOP: {EARLY_STOP_CONSECUTIVE_403} consecutive 403s", flush=True)
                early_stop_triggered = True

    ef.close()
    duration = round(time.monotonic() - t_start, 1)

    report = {
        "timestamp": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "task": "stage4c_batch_C_new_endpoints",
        "pool_file": str(POOL_FILE),
        "pool_size": len(POOL.pool),
        "events_targeted": len(rows),
        "events_processed": done,
        "duration_s": duration,
        "endpoint_counters": ep_counters,
        "total_rows_inserted": total_rows,
        "early_stop_triggered": early_stop_triggered,
        "note": "momentum: graph signed value split into home_value/away_value; player-statistics embedded in lineups (Batch A)",
    }
    REPORT_FILE.write_text(json.dumps(report, indent=2))
    print(f"\n[Stage4c Batch C] DONE duration={duration}s early_stop={early_stop_triggered}")
    print(json.dumps(ep_counters, indent=2))
    print(f"total_rows={total_rows} report={REPORT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())