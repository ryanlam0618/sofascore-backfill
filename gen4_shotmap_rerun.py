#!/usr/bin/env python3
"""gen4_shotmap_rerun.py — Shotmap-only re-run for 25/26 season events.

Kris explicit task 2026-09-06 23:44 GMT+8 (option (c)).
Root cause (Main Agent confirmed): good_21.txt 21-IP pool too small -> Stage 3
runner fetch step mostly failed -> flag always false -> never reached shotmap
write. Flag-gate itself NOT buggy (0 BUG_FLAG_FALSE cases). Pool swapped to
good_20260906.txt (61 IPs mixed cred) at 16:36 same day.

This script re-runs ONLY /shotmap endpoint for events already in `matches`
table for 25/26 season (skipping top-5 leagues + UCL which already have shotmap).

Per-event logic (Kris spec):
  1. GET /event/{id} -> status.code == 100 (Ended), skip non-ended
  2. GET /event/{id}/shotmap (no flag pre-gate)
     - 200 + shotmap list > 0 -> INSERT ... ON DUPLICATE KEY UPDATE
     - 404 -> no_data
     - 403/407/timeout -> rotate IP, retry max 3x -> ip_banned if all fail
  3. Each fetch rotates pool entry; 0.15s pacing; concurrency 8.

Outputs:
  - data/gen4_shotmap_rerun_report.json
  - data/gen4_shotmap_rerun_evidence.jsonl
"""
from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

POOL_FILE = ROOT / "data/proxy_pools/good_20260906.txt"
REPORT = ROOT / "data/gen4_shotmap_rerun_report.json"
EVIDENCE = ROOT / "data/gen4_shotmap_rerun_evidence.jsonl"

CONCURRENCY = 8     # overridable via --concurrency
PACING_S = 0.15     # overridable via --pacing
MAX_RETRIES = 3     # overridable via --retries
TIMEOUT = 20
COOLDOWN_S = 0      # overridable via --cooldown-s; skip IP for N s after any failure

# Competitions already have shotmap from Stage 3 (skip).
# ut_id set: PL=17, LaLiga=8, SerieA=23, Bundesliga=35, UCL=7.
ALREADY_DONE_UT = {17, 8, 23, 35, 7}


# ---------------------------------------------------------------------------
# Pool + random rotation + cooldown bookkeeping.
# ---------------------------------------------------------------------------
class PoolRotator:
    def __init__(self):
        self.pool = []
        for l in POOL_FILE.read_text().splitlines():
            if l.strip() and not l.startswith("#"):
                ip, port, user, pw = l.strip().split(":", 3)
                self.pool.append({"ip": ip, "port": port, "user": user, "pw": pw})
        assert len(self.pool) >= 20, f"pool too small: {len(self.pool)}"
        self.lock = threading.Lock()
        # cooldown: ip -> timestamp banned until
        self.cooldown = {}

    def next(self, cooldown_s=0.0):
        import random
        with self.lock:
            now = time.monotonic()
            if cooldown_s > 0:
                available = [i for i, m in enumerate(self.pool)
                             if self.cooldown.get(m["ip"], 0) <= now]
                if not available:
                    # force reuse if everything cooled down
                    available = list(range(len(self.pool)))
                idx = random.choice(available)
                return idx, self.pool[idx]
            idx = random.randrange(len(self.pool))
            return idx, self.pool[idx]

    def mark_bad(self, ip, cooldown_s):
        if cooldown_s <= 0:
            return
        with self.lock:
            self.cooldown[ip] = time.monotonic() + cooldown_s


POOL = PoolRotator()


# ---------------------------------------------------------------------------
# HTTP helpers.
# ---------------------------------------------------------------------------
def _fetch(path, retries=MAX_RETRIES):
    """Retry-with-rotation; avoid reusing IPs that failed this call.

    Returns tuple (kind, body, http, ip, error):
      kind in {"ok", "no_data", "ip_banned"}
      - "ok": HTTP 200 with parsed JSON body
      - "no_data": HTTP 404
      - "ip_banned": 403/407/timeout/other after all retries
    """
    import random
    last_err = None
    used_idx = set()
    pool = POOL.pool
    for attempt in range(retries + 1):
        # pick a random unused IP
        candidates = [i for i in range(len(pool)) if i not in used_idx]
        if not candidates:
            candidates = list(range(len(pool)))
        idx = random.choice(candidates)
        used_idx.add(idx)
        m = pool[idx]
        proxy = f"http://{m['user']}:{m['pw']}@{m['ip']}:{m['port']}"
        try:
            from curl_cffi import requests as cffi_requests
            r = cffi_requests.get("https://api.sofascore.com/api/v1" + path,
                                  impersonate="chrome124",
                                  proxies={"http": proxy, "https": proxy},
                                  timeout=TIMEOUT)
            if r.status_code == 200:
                try:
                    return ("ok", r.json(), 200, m["ip"], None)
                except Exception as e:
                    last_err = f"invalid_json: {str(e)[:120]}"
                    continue
            if r.status_code == 404:
                return ("no_data", None, 404, m["ip"], "http_404")
            # 403/407/429/5xx -> rotate
            last_err = f"http_{r.status_code}"
            continue
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:120]}"
            continue
    return ("ip_banned", None, None, None, last_err)


# ---------------------------------------------------------------------------
# Per-event worker.
# ---------------------------------------------------------------------------
class WorkerCtx:
    """Per-worker MySQL connection (mysql.connector is not thread-safe)."""

    def __init__(self, conn):
        self.conn = conn

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


def open_worker_conn():
    import mysql.connector
    env_path = ROOT / ".env"
    env = dict(l.split("=", 1) for l in env_path.read_text().splitlines()
               if "=" in l and not l.startswith("#"))
    conn = mysql.connector.connect(host="127.0.0.1", port=3306, user="appdb_rw",
                                   password=env["MYSQL_PASSWORD"].strip(),
                                   database="appdb", autocommit=False)
    return conn


def process_event(ctx, match_id, last_pacing_lock, ev_lock):
    """Run one event through the spec. Returns evidence dict."""
    ev = {"match_id": match_id, "steps": {}, "result": "ok", "rows": 0, "ip": None,
          "elapsed_ms": 0}
    t0 = time.monotonic()

    # pacing before first probe
    with last_pacing_lock:
        time.sleep(PACING_S)

    # 1) event detail (need status code). Use same retries as /shotmap so IP aging
    # does not silently skip events whose /shotmap endpoint would have succeeded.
    kind, body, http, ip, err = _fetch(f"/event/{match_id}", retries=MAX_RETRIES)
    ev["steps"]["event"] = {"kind": kind, "http": http, "ip": ip, "error": err}
    if kind != "ok":
        ev["result"] = f"event_{kind}"
        ev["ip"] = ip
        ev["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        with ev_lock:
            return ev

    event = body.get("event", body) if isinstance(body, dict) else {}
    sc = event.get("status", {})
    code = sc.get("code") if isinstance(sc, dict) else None
    ev["steps"]["event_status_code"] = code
    if code != 100:
        ev["result"] = "not_ended"
        ev["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        with ev_lock:
            return ev

    with last_pacing_lock:
        time.sleep(PACING_S)

    # 2) shotmap (no flag pre-gate per Kris spec)
    kind, body, http, ip, err = _fetch(f"/event/{match_id}/shotmap", retries=MAX_RETRIES)
    ev["steps"]["shotmap"] = {"kind": kind, "http": http, "ip": ip, "error": err}
    ev["ip"] = ip

    if kind == "no_data":
        ev["result"] = "no_data"
    elif kind == "ip_banned":
        ev["result"] = "ip_banned"
    elif kind == "ok":
        shots = (body or {}).get("shotmap", []) or []
        ev["shot_count"] = len(shots)
        if len(shots) == 0:
            # 200 but empty shotmap list -> treat as no_data per Kris spec
            # (1. step 5 is 404-only; empty list is rarer but happens; still
            # log ok-fetch + empty + 0 rows so we know endpoint reached)
            ev["result"] = "ok_empty"
        else:
            # /shotmap endpoint payload has NO homeTeam/awayTeam; merge from
            # /event/{id} so insert_shotmap can resolve team_id via isHome fallback.
            if "homeTeam" not in body and "awayTeam" not in body:
                body["homeTeam"] = event.get("homeTeam", {}) or {}
                body["awayTeam"] = event.get("awayTeam", {}) or {}
            # write
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
                ev["steps"]["shotmap"]["insert_error"] = f"{type(e).__name__}: {str(e)[:160]}"

    ev["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
    with ev_lock:
        return ev


def main():
    t_start = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()

    # Source event list (one-time query, read-only)
    import mysql.connector
    env_path = ROOT / ".env"
    env = dict(l.split("=", 1) for l in env_path.read_text().splitlines()
               if "=" in l and not l.startswith("#"))
    conn = mysql.connector.connect(host="127.0.0.1", port=3306, user="appdb_rw",
                                   password=env["MYSQL_PASSWORD"].strip(),
                                   database="appdb", autocommit=True)
    cur = conn.cursor()
    # 25/26 seasons except top-5 leagues + UCL (already have shotmap)
    placeholders = ",".join(["%s"] * len(ALREADY_DONE_UT))
    cur.execute(f"""
        SELECT m.match_id, m.season_id, m.competition_id, c.name
        FROM matches m
        JOIN seasons s ON m.season_id=s.season_id
        JOIN competitions c ON s.competition_id=c.competition_id
        WHERE s.year_label='25/26'
          AND c.ut_id NOT IN ({placeholders})
        ORDER BY m.match_id
    """, tuple(ALREADY_DONE_UT))
    rows = cur.fetchall()
    conn.close()

    print(f"[shotmap-rerun] candidate events from DB: {len(rows)}", flush=True)
    if not rows:
        print("[shotmap-rerun] no events to process, exit.", flush=True)
        return 0

    # Open evidence file (truncate)
    EVIDENCE.write_text("")
    ef = EVIDENCE.open("a")

    counters = {
        "ok_written": 0, "ok_empty": 0, "no_data": 0, "ip_banned": 0,
        "not_ended": 0, "event_no_data": 0, "event_ip_banned": 0,
        "insert_error": 0, "total": len(rows),
    }
    rows_inserted = 0
    per_ip = {}
    ip_lock = threading.Lock()

    pacing_lock = threading.Lock()
    ev_lock = threading.Lock()

    def worker(match_id):
        ctx_conn = open_worker_conn()
        ctx = WorkerCtx(ctx_conn)
        try:
            ev = process_event(ctx, match_id, pacing_lock, ev_lock)
            return ev
        finally:
            ctx.close()

    done = 0
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futures = {ex.submit(worker, r[0]): r for r in rows}
        for fut in as_completed(futures):
            ev = fut.result()
            done += 1
            res = ev["result"]
            counters[res] = counters.get(res, 0) + 1
            rows_inserted += ev.get("rows", 0)
            ip = ev.get("ip")
            if ip:
                with ip_lock:
                    d = per_ip.setdefault(ip, {"ok": 0, "fail": 0, "no_data": 0})
                    if res in ("ok_written", "ok_empty"):
                        d["ok"] += 1
                    elif res == "no_data":
                        d["no_data"] += 1
                    else:
                        d["fail"] += 1
            # write evidence JSONL
            ef.write(json.dumps(ev) + "\n")
            ef.flush()
            if done % 50 == 0 or done == len(rows):
                print(f"  [{done}/{len(rows)}] ok_w={counters['ok_written']} "
                      f"empty={counters['ok_empty']} no_data={counters['no_data']} "
                      f"ip_banned={counters['ip_banned']} "
                      f"not_ended={counters['not_ended']} "
                      f"rows_inserted={rows_inserted}", flush=True)

    ef.close()

    duration = round(time.monotonic() - t_start, 1)
    report = {
        "timestamp": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "task": "shotmap_rerun_25_26",
        "pool_file": str(POOL_FILE),
        "pool_size": len(POOL.pool),
        "concurrency": CONCURRENCY,
        "pacing_s": PACING_S,
        "max_retries": MAX_RETRIES,
        "skip_ut_ids": sorted(ALREADY_DONE_UT),
        "total_events": len(rows),
        "duration_s": duration,
        "counters": counters,
        "rows_inserted": rows_inserted,
        "per_ip": per_ip,
        "evidence_jsonl": str(EVIDENCE),
    }
    REPORT.write_text(json.dumps(report, indent=2))

    print(f"\n[shotmap-rerun] DONE  duration={duration}s")
    print(json.dumps({k: v for k, v in counters.items() if k != "total"}, indent=2))
    print(f"rows_inserted={rows_inserted}  report={REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
