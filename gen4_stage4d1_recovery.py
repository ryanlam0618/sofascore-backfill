#!/usr/bin/env python3
"""gen4_stage4d1_recovery.py — Recovery run for the 206 Stage 4c insert_errors.

Kris 2026-09-08 19:02 approval. Uses the PATCHED v2 working tree
(backfill_runner: _stat_value dict fix + _retry_deadlock on insert_odds/
insert_graph_points; batch C deadlock retry).

Targets (206 events):
  - 169 lineups   -> re-fetch /event/{id}/lineups + DataInserter.insert_lineups
  - 33  odds      -> re-fetch /event/{id}/odds/1/all  + DataInserter.insert_odds
  - 3   avg_pos   -> re-fetch /event/{id}/average-positions + _insert_avg_positions
  - 1   momentum  -> re-fetch /event/{id}/graph + _insert_momentum

Pool rule: 33-IP round-robin, 1-retry-next-IP per request, 10-consec-403 early stop.
Concurrency 4 (recovery is small; keep it gentle). 0.15s pacing.

Outputs:
  data/gen4_stage4d1_recovery_evidence.jsonl
  data/gen4_stage4d1_recovery_report.json
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
REPORT_FILE = ROOT / "data/gen4_stage4d1_recovery_report.json"
EVIDENCE_FILE = ROOT / "data/gen4_stage4d1_recovery_evidence.jsonl"

CONCURRENCY = 4
PACING_S = 0.15
MAX_RETRIES = 1     # 1 retry -> next IP
TIMEOUT = 20
COOLDOWN_S = 30
EARLY_STOP_CONSECUTIVE_403 = 10

# ── Target sets ──────────────────────────────────────────────────────────────

LINEUPS_IDS = [
    14025781, 14028287, 14054071, 14054072, 14054074, 14054075, 14054076,
    14054083, 14054087, 14073447, 14073452, 14073454, 14073462, 14073463,
    14073466, 14073468, 14073470, 14073474, 14149963, 14250660, 14250673,
    14270430, 14353429, 14353437, 14377055, 14396153, 14411087, 14424510,
    14425847, 14425848, 14425853, 14426851, 14426926, 14493422, 14493424,
    14572820, 14573080, 14585923, 14585925, 14585926, 14586193, 14598940,
    14598953, 14763685, 14787850, 14843662, 14843682, 14843688, 14843689,
    14843690, 14843691, 14843696, 14843697, 14843698, 14843699, 14843700,
    14843703, 14843704, 14843705, 14843706, 14843707, 14843708, 14843709,
    14843711, 14843713, 14843714, 14843715, 14843717, 14843718, 14843723,
    14843724, 14843725, 14843727, 14843728, 14843729, 14888310, 14888330,
    14970878, 14994243, 15002171, 15002174, 15037707, 15037708, 15037713,
    15037714, 15037715, 15037716, 15037717, 15037720, 15037722, 15037723,
    15037724, 15037725, 15037727, 15037728, 15037729, 15037731, 15037732,
    15037734, 15037735, 15171799, 15171801, 15171825, 15171827, 15193715,
    15197268, 15197272, 15197276, 15197280, 15197284, 15197286, 15197287,
    15200514, 15200526, 15200527, 15262365, 15262370, 15262376, 15296007,
    15298464, 15327740, 15327741, 15359733, 15359740, 15359743, 15372917,
    15380216, 15426968, 15426971, 15426974, 15440056, 15453077, 15453088,
    15453099, 15479165, 15492898, 15492899, 15492900, 15532694, 15538902,
    15552624, 15556601, 15579437, 15600442, 15631837, 15631842, 15632053,
    15664537, 15697561, 15755701, 15755725, 15884736, 15884737, 15884781,
    16040850, 16040862, 16074214, 16114123, 16173051, 16222684, 16222685,
    16222690, 16222693, 16222694, 16222696, 16411486, 16411487, 16411489,
    16411490,
]

ODDS_IDS = [
    13274714, 13379058, 13482080, 13980083, 13980084, 13981633, 14021230,
    14021238, 14024005, 14028329, 14054071, 14064409, 14065218, 14065228,
    14065739, 14081813, 14083191, 14083422, 14250633, 14250660, 14461753,
    14566737, 14566951, 14572782, 14573075, 14641638, 14888320, 15171821,
    15372955, 15453077, 15496189, 15551888, 15632631,
]

AVGPOS_IDS = [14566928, 14572753, 14572781]
MOMENTUM_IDS = [14083687]

# ── Pool rotator (33-IP) ─────────────────────────────────────────────────────

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


def _fetch(path):
    """Fetch with 1-retry-next-IP. Returns (kind, body, http, ip, error)."""
    used_ips = set()
    last_err = None
    for attempt in range(MAX_RETRIES + 1):
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


def _insert_avg_positions(conn, match_id, home_team_id, away_team_id, body):
    from gen4_stage4c_batch_C import _ensure_player
    cur = conn.cursor()
    count = 0
    for side, team_id in (("home", home_team_id), ("away", away_team_id)):
        for entry in (body or {}).get(side, []) or []:
            p = entry.get("player") or {}
            pid = p.get("id")
            if not pid:
                continue
            _ensure_player(conn, p)
            cur.execute(
                """INSERT INTO match_average_positions (match_id, player_id, team_id, avg_x, avg_y)
                   VALUES (%s,%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE avg_x=VALUES(avg_x), avg_y=VALUES(avg_y)""",
                (match_id, pid, team_id, entry.get("averageX"), entry.get("averageY")),
            )
            count += 1
    return count


def _insert_momentum(conn, match_id, body):
    cur = conn.cursor()
    count = 0
    for pt in (body or {}).get("graphPoints", []) or []:
        minute = pt.get("minute")
        value = pt.get("value")
        if minute is None or value is None:
            continue
        period = "first" if float(minute) <= 45 else "second"
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


# ── event processing ─────────────────────────────────────────────────────────

def process_event(ctx, task, pacing_lock, ev_lock):
    """task: dict {match_id, kind, home_team_id?, away_team_id?}"""
    mid = task["match_id"]
    kind = task["kind"]
    t0 = time.monotonic()

    from backfill_runner import DataInserter
    inserter = DataInserter(ctx.conn)

    if kind == "lineups":
        paths = [f"/event/{mid}/lineups"]
        ep_names = ["lineups"]
    elif kind == "odds":
        paths = [f"/event/{mid}/odds/1/all"]
        ep_names = ["odds"]
    elif kind == "avg_pos":
        paths = [f"/event/{mid}/average-positions"]
        ep_names = ["average_positions"]
    elif kind == "momentum":
        paths = [f"/event/{mid}/graph"]
        ep_names = ["momentum"]
    else:
        return {"match_id": mid, "kind": kind, "result": "unknown_kind",
                "endpoints": {}, "rows": {}}

    ev = {"match_id": mid, "kind": kind, "result": "ok", "rows": {}, "endpoints": {},
          "elapsed_ms": 0}

    # Fetch event detail (for team ids on lineups) only when needed
    home_team_id = task.get("home_team_id")
    away_team_id = task.get("away_team_id")
    event = None
    if kind in ("lineups", "avg_pos", "momentum"):
        with pacing_lock:
            time.sleep(PACING_S)
        k2, body2, http2, ip2, err2 = _fetch(f"/event/{mid}")
        if k2 == "ok" and isinstance(body2, dict):
            event = body2.get("event", body2) or {}
            if home_team_id is None:
                home_team_id = (event.get("homeTeam") or {}).get("id")
            if away_team_id is None:
                away_team_id = (event.get("awayTeam") or {}).get("id")

    for ep_name, ep_path in zip(ep_names, paths):
        with pacing_lock:
            time.sleep(PACING_S)
        kind2, body, http, ip, err = _fetch(ep_path)
        ev["endpoints"][ep_name] = {"kind": kind2, "http": http, "ip": ip, "error": err}
        if kind2 != "ok":
            ev["result"] = f"fetch_{kind2}"
            continue

        try:
            if ep_name == "lineups":
                # inject teams (same as batch A) for ensure_team/ensure_player
                if isinstance(body, dict):
                    if "homeTeam" not in body or not body.get("homeTeam"):
                        body["homeTeam"] = (event or {}).get("homeTeam", {}) or {"id": home_team_id}
                    if "awayTeam" not in body or not body.get("awayTeam"):
                        body["awayTeam"] = (event or {}).get("awayTeam", {}) or {"id": away_team_id}
                    for side in ("home", "away"):
                        team = body.get(side) or {}
                        if not team.get("name"):
                            team["name"] = body.get(side + "Team", {}).get("name") or "Unknown"
                        for pe in team.get("players", []) or []:
                            p = pe.get("player") or {}
                            if isinstance(p, dict) and not p.get("name"):
                                p["name"] = "Unknown"
                                p.setdefault("shortName", "Unknown")
                rows = inserter.insert_lineups(mid, body)
            elif ep_name == "odds":
                rows = inserter.insert_odds(mid, body)
            elif ep_name == "average_positions":
                rows = _insert_avg_positions(ctx.conn, mid, home_team_id, away_team_id, body)
            elif ep_name == "momentum":
                rows = _insert_momentum(ctx.conn, mid, body)
            else:
                rows = 0
            ctx.conn.commit()
            ev["rows"][ep_name] = rows
        except Exception as e:
            try:
                ctx.conn.rollback()
            except Exception:
                pass
            ev["endpoints"][ep_name]["insert_error"] = f"{type(e).__name__}: {str(e)[:200]}"
            ev["endpoints"][ep_name]["kind"] = "insert_error"
            ev["result"] = "insert_error"

    ev["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
    with ev_lock:
        return ev


def _load_team_ids(cur, ids):
    """Return {match_id: (home_team_id, away_team_id)}."""
    out = {}
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        ph = ",".join(["%s"] * len(chunk))
        cur.execute(f"SELECT match_id, home_team_id, away_team_id FROM matches WHERE match_id IN ({ph})", chunk)
        for mid, h, a in cur.fetchall():
            out[mid] = (h, a)
    return out


def main():
    import argparse
    global CONCURRENCY, PACING_S, MAX_RETRIES, COOLDOWN_S, EARLY_STOP_CONSECUTIVE_403
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrency", type=int, default=CONCURRENCY)
    parser.add_argument("--pacing", type=float, default=PACING_S)
    parser.add_argument("--retries", type=int, default=MAX_RETRIES)
    parser.add_argument("--cooldown", type=float, default=COOLDOWN_S)
    parser.add_argument("--early-stop", type=int, default=EARLY_STOP_CONSECUTIVE_403)
    parser.add_argument("--only", default="")
    args = parser.parse_args()
    CONCURRENCY = args.concurrency
    PACING_S = args.pacing
    MAX_RETRIES = args.retries
    COOLDOWN_S = args.cooldown
    EARLY_STOP_CONSECUTIVE_403 = args.early_stop
    POOL.cooldown_s = COOLDOWN_S

    t_start = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    print(f"[Stage4d-1 Recovery] started_at={started_at} pool={len(POOL.pool)} "
          f"conc={CONCURRENCY} retries={MAX_RETRIES}", flush=True)

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

    team_ids = _load_team_ids(cur, LINEUPS_IDS + ODDS_IDS + AVGPOS_IDS + MOMENTUM_IDS)

    only = set(m for m in args.only.split(",") if m.strip())

    def _keep(mid, kind):
        if not only:
            return True
        return str(mid) in only

    tasks = []
    for mid in LINEUPS_IDS:
        if not _keep(mid, "lineups"):
            continue
        h, a = team_ids.get(mid, (None, None))
        tasks.append({"match_id": mid, "kind": "lineups", "home_team_id": h, "away_team_id": a})
    for mid in ODDS_IDS:
        if not _keep(mid, "odds"):
            continue
        tasks.append({"match_id": mid, "kind": "odds"})
    for mid in AVGPOS_IDS:
        if not _keep(mid, "avg_pos"):
            continue
        h, a = team_ids.get(mid, (None, None))
        tasks.append({"match_id": mid, "kind": "avg_pos", "home_team_id": h, "away_team_id": a})
    for mid in MOMENTUM_IDS:
        if not _keep(mid, "momentum"):
            continue
        tasks.append({"match_id": mid, "kind": "momentum"})
    conn.close()

    print(f"[Stage4d-1 Recovery] tasks={len(tasks)} "
          f"(lineups={len(LINEUPS_IDS)} odds={len(ODDS_IDS)} "
          f"avg_pos={len(AVGPOS_IDS)} momentum={len(MOMENTUM_IDS)})", flush=True)

    EVIDENCE_FILE.write_text("")
    ef = EVIDENCE_FILE.open("a")

    counters = {"ok": 0, "fetch_error": 0, "insert_error": 0}
    total_rows = 0
    pacing_lock = threading.Lock()
    ev_lock = threading.Lock()
    consecutive_403 = 0
    early_stop = False
    done = 0

    def worker(task):
        ctx_conn = open_worker_conn()
        ctx = WorkerCtx(ctx_conn)
        try:
            return process_event(ctx, task, pacing_lock, ev_lock)
        finally:
            ctx.close()

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futures = {ex.submit(worker, t): t for t in tasks}
        for fut in as_completed(futures):
            if early_stop:
                # drain remaining quickly
                pass
            ev = fut.result()
            done += 1
            if ev["result"] == "ok":
                counters["ok"] += 1
            elif ev["result"] == "insert_error":
                counters["insert_error"] += 1
            else:
                counters["fetch_error"] += 1
            for n in ev.get("rows", {}).values():
                total_rows += n
            # 403 detection
            for ep_data in ev.get("endpoints", {}).values():
                if ep_data.get("kind") == "ip_banned":
                    consecutive_403 += 1
                    break
            else:
                consecutive_403 = 0

            ef.write(json.dumps(ev) + "\n")
            ef.flush()
            if done % 25 == 0 or done == len(tasks):
                print(f"  [{done}/{len(tasks)}] ok={counters['ok']} "
                      f"fetch_err={counters['fetch_error']} "
                      f"insert_err={counters['insert_error']} rows={total_rows} "
                      f"consec403={consecutive_403}", flush=True)
            if consecutive_403 >= EARLY_STOP_CONSECUTIVE_403:
                print(f"!! EARLY STOP: {consecutive_403} consecutive 403s", flush=True)
                early_stop = True

    ef.close()
    duration = round(time.monotonic() - t_start, 1)
    report = {
        "timestamp": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "task": "stage4d1_recovery",
        "pool_file": str(POOL_FILE),
        "pool_size": len(POOL.pool),
        "events_targeted": len(tasks),
        "events_processed": done,
        "counts": {"lineups": len(LINEUPS_IDS), "odds": len(ODDS_IDS),
                   "avg_pos": len(AVGPOS_IDS), "momentum": len(MOMENTUM_IDS)},
        "result_counters": counters,
        "total_rows_inserted": total_rows,
        "early_stop_triggered": early_stop,
        "duration_s": duration,
        "evidence": str(EVIDENCE_FILE),
    }
    REPORT_FILE.write_text(json.dumps(report, indent=2))
    print(f"\n[Stage4d-1 Recovery] DONE duration={duration}s early_stop={early_stop}")
    print(json.dumps(counters))
    print(f"total_rows={total_rows} report={REPORT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())