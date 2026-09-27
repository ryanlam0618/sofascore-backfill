#!/usr/bin/env python3
"""gen4_stage3_expansion_backfill.py — Stage 3 expansion (Kris final go 2026-09-03).

Scope  : 19 comps (competitions_10y.yaml minus 5 Stage-2 done) x 25/26 season
         x 6 endpoints (event/lineups/statistics/incidents/odds/shotmap).

Skip policy (Kris-approved):
  - Gate on endpoint HTTP 200/404 + event flags hasXg / hasEventPlayerStatistics /
    hasEventPlayerHeatMap. NOT the bare coverage integer.
  - HTTP 404 -> no_data: NO retry, NO DB write (no empty row), NOT a failure.
  - Flag pre-gate: hasEventPlayerHeatMap is False -> skip shotmap fetch (no_data);
    hasEventPlayerStatistics is False -> skip statistics fetch (no_data).
    Flag missing/None -> fetch normally (404 handles absence).
  - Writers are only invoked on HTTP 200 payloads.

Fetch  : curl_cffi chrome124 + 21-IP good pool, rotate per request, tiered
         retries per comp (Phase 2.1 validated): 3/2/1.
Safety : appdb_rw only; no DROP/ALTER. Evidence JSONL + per-IP counters +
         before/after row counts. Per-comp early stop on consecutive infra
         fails (15/12/10 by tier); global abort at 30 consecutive infra fails.

Modes  : default live run (Kris launch approval required);
         --ids-only    enumerate event ids -> data/stage3_event_ids.json, exit;
         --selftest    offline mocked-fetch checks of skip semantics; no network,
                       no DB writes.
         --resume      skip event ids already present in matches table.
"""
from __future__ import annotations

import json
import sys
import time
import types
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
# Pool = combined 61-IP pool (good_20260906.txt), Kris decision 2026-09-06 15:06.
# Sentinel v3 re-audit (2026-09-06 15:05): 61/221 GOOD = 20 cred_main + 41 cred_rotate.
# Supersedes good_21.txt (Stage 3 original production pool). Mixed cred per line (ip:port:user:pw).
POOL_FILES = [
    ROOT / "data/proxy_pools/good_20260906.txt",  # 61 IPs mixed cred
]
YAML_FILE = ROOT / "competitions_10y.yaml"
IDS_FILE = ROOT / "data/stage3_event_ids.json"
REPORT = ROOT / "data/gen4_stage3_expansion_report.json"
EV = Path("/tmp/gen4_stage3/evidence.jsonl")
EV.parent.mkdir(exist_ok=True)
TIMEOUT = 20

DONE = {"Premier League", "La Liga", "Serie A", "Bundesliga", "UCL"}
TIER_3 = {"Emperor's Cup", "J.League Cup", "Chinese FA Cup", "AFC Champions League Two"}
TIER_2 = {"K League 1", "Chinese Super League", "Copa del Rey", "Coupe de France"}

TAIL = {"lineups": "/lineups", "statistics": "/statistics", "incidents": "/incidents",
        "odds": "/odds/1/all", "shotmap": "/shotmap"}
# event-flag pre-gates: endpoint -> (flag_name,)
FLAG_GATE = {"shotmap": "hasEventPlayerHeatMap", "statistics": "hasEventPlayerStatistics"}
# Endpoints hard-disabled for Stage 3 (Kris Path 2 2026-09-03): odds skipped due to
# Hermes-rebuilt match_odds schema mismatch. Recorded as no_data(odds_disabled), no fetch, no write.
DISABLED_ENDPOINTS = {"odds"}
SEASON_CUTOFF_TS = 1735689600  # 2025-01-01 UTC; 25/26-scope events are all >= this
ENUM_MAX_PAGES = 80
GLOBAL_ABORT_STREAK = 30


def load_pool():
    pool = []
    for pf in POOL_FILES:
        for l in pf.read_text().splitlines():
            if l.strip() and not l.startswith("#"):
                ip, port, user, pw = l.strip().split(":", 3)
                pool.append({"ip": ip, "port": port, "user": user, "pw": pw})
    assert len(pool) >= 20, f"pool too small: {len(pool)}"
    return pool


_pool = None
_ctr = {"n": 0}


def next_ip():
    global _pool
    if _pool is None:
        _pool = load_pool()
    m = _pool[_ctr["n"] % len(_pool)]
    _ctr["n"] += 1
    return m


# HTTP_GET is the single network seam (swapped by --selftest).
def _http_get_cffi(path, proxy):
    from curl_cffi import requests as cffi_requests
    return cffi_requests.get("https://api.sofascore.com/api/v1" + path,
                             impersonate="chrome124",
                             proxies={"http": proxy, "https": proxy},
                             timeout=TIMEOUT)


HTTP_GET = _http_get_cffi


class FetchResult:
    """one of: ok(200+body) / no_data(404) / fail(infra, after retries)."""

    def __init__(self, kind, body=None, http=None, ip=None, error=None, tries=0):
        self.kind = kind          # ok | no_data | fail
        self.body = body
        self.http = http
        self.ip = ip
        self.error = error
        self.tries = tries

    def __repr__(self):
        return f"FetchResult({self.kind}, http={self.http}, error={self.error})"


def fetch_json(path, retries):
    """404 -> immediate no_data (no retry). Others -> retry on fresh IP."""
    last = None
    for attempt in range(retries + 1):
        m = next_ip()
        proxy = f"http://{m['user']}:{m['pw']}@{m['ip']}:{m['port']}"
        try:
            r = HTTP_GET(path, proxy)
            if r.status_code == 200:
                try:
                    return FetchResult("ok", body=r.json(), http=200, ip=m["ip"], tries=attempt)
                except Exception as e:
                    last = {"http": 200, "ip": m["ip"], "error": f"invalid_json: {e}"[:120]}
            elif r.status_code == 404:
                return FetchResult("no_data", http=404, ip=m["ip"], error="http_404", tries=attempt)
            else:
                last = {"http": r.status_code, "ip": m["ip"],
                        "error": f"http_{r.status_code}"}
        except Exception as e:
            last = {"http": None, "ip": m["ip"],
                    "error": f"{type(e).__name__}: {str(e)[:120]}"}
    return FetchResult("fail", http=last.get("http"), ip=last.get("ip"),
                       error=last.get("error"), tries=retries)


def tier(name):
    if name in TIER_3:
        return 3, 15
    if name in TIER_2:
        return 2, 12
    return 1, 10


# ---------------------------------------------------------------------------
# Writers (inline, proven from Stage 2 + gen4_lineups_replay / shotmap_replay).
# All idempotent; only called on HTTP-200 payloads.
# ---------------------------------------------------------------------------

_POSMAP = {"G": "GK", "D": "DEF", "M": "MID", "F": "FWD"}


def insert_match_lineups(conn, match_id, data, event):
    """Minimal match_lineups writer (rebuilt schema; no legacy deep-stat cols)."""
    from backfill_runner import ensure_player as _ep, ensure_team as _et
    data.setdefault("homeTeam", event.get("homeTeam", {}))
    data.setdefault("awayTeam", event.get("awayTeam", {}))
    cur = conn.cursor()
    n = 0
    for is_home, key in ((1, "home"), (0, "away")):
        side = data.get(key, {}) or {}
        team = side.get("team") or {}
        team_id = team.get("id")
        players = side.get("players", []) or []
        if team_id is None and players:
            team_id = players[0].get("teamId")
        if team_id is None:
            team_id = (data["homeTeam"] if is_home else data["awayTeam"]).get("id")
        fallback_team = data["homeTeam"] if is_home else data["awayTeam"]
        for p in players:
            pl = p.get("player", {})
            if not pl.get("id") or team_id is None:
                continue
            stat = p.get("statistics") or {}
            pc = _POSMAP.get((pl.get("position") or "").upper()[:1], "MID")
            _et(conn, team_id, (team or fallback_team).get("name", ""), "", None, team or fallback_team)
            _ep(conn, pl["id"], pl.get("name", ""), pl.get("shortName", ""),
                pl.get("position") or "", None, pl, team_id)
            cur.execute(
                """INSERT INTO match_lineups
                   (match_id, team_id, player_id, is_home, is_starter,
                    jersey_number, position, position_category, is_captain,
                    minutes_played, rating)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE
                     is_starter=VALUES(is_starter), jersey_number=VALUES(jersey_number),
                     position=VALUES(position), position_category=VALUES(position_category),
                     is_captain=VALUES(is_captain), minutes_played=VALUES(minutes_played),
                     rating=VALUES(rating)""",
                (match_id, team_id, pl["id"], is_home,
                 1 if p.get("substitute") is False else 0,
                 (p.get("jerseyNumber") or None) if (p.get("jerseyNumber") not in ("", None)) else None,
                 pl.get("position") or p.get("position") or "",
                 pc, 1 if p.get("captain") else 0,
                 stat.get("minutesPlayed"), stat.get("rating")),
            )
            n += 1
    conn.commit()
    return n


def _ins_incidents(conn, mid, data, ev=None):
    from backfill_runner import DataInserter
    return DataInserter(conn).insert_incidents(mid, data)


def _ins_statistics(conn, mid, data, ev=None):
    from backfill_runner import DataInserter
    return DataInserter(conn).insert_statistics(mid, data)


def _ins_shotmap(conn, mid, data, ev=None):
    from backfill_runner import DataInserter
    return DataInserter(conn).insert_shotmap(mid, data)


def _ins_odds(conn, mid, data, ev=None):
    # Odds disabled for Stage 3 (Kris Path 2 2026-09-03): Hermes-rebuilt match_odds
    # schema mismatch. No-op guard so it never hits the SQL schema error, even if
    # re-enabled by accident.
    return 0


WRITERS = {
    "statistics": _ins_statistics,
    "incidents":  _ins_incidents,
    "shotmap":    _ins_shotmap,
    "odds":       _ins_odds,
    "lineups":    insert_match_lineups,
}


# ---------------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------------

def enumerate_events(name, ut, sid):
    """Return ordered event ids (newest->oldest) downto SEASON_CUTOFF_TS."""
    ids, seen = [], set()
    for page in range(ENUM_MAX_PAGES):
        r = fetch_json(f"/unique-tournament/{ut}/season/{sid}/events/last/{page}", 1)
        if r.kind != "ok":
            break
        evs = r.body.get("events", [])
        if not evs:
            break
        oldest = None
        for e in evs:
            eid = e.get("id")
            ts = e.get("startTimestamp")
            if eid and eid not in seen:
                seen.add(eid)
                ids.append(eid)
            if isinstance(ts, (int, float)):
                oldest = ts if oldest is None else min(oldest, ts)
        if oldest is not None and oldest < SEASON_CUTOFF_TS:
            break
    return ids


# ---------------------------------------------------------------------------
# Per-event processing
# ---------------------------------------------------------------------------

def plan_event_steps(event):
    """Return ordered list of endpoint names to fetch, honoring flag pre-gates.

    event here is the root event dict (may itself be the event object).
    """
    ev = event.get("event", event) if isinstance(event, dict) else event
    steps = []
    for ep in ("lineups", "statistics", "incidents", "odds", "shotmap"):
        if ep in DISABLED_ENDPOINTS:
            steps.append((ep, "skip_disabled"))
            continue
        flag = FLAG_GATE.get(ep)
        if flag is not None and ev.get(flag) is False:
            steps.append((ep, "skip_flag"))
        else:
            steps.append((ep, "fetch"))
    return steps


def process_event(conn, inserter, name, eid, comp_id, sid, retries, per_ip):
    """Returns a rec dict. Commits handled by writers; event insert commits."""
    rec = {"event_id": eid, "steps": {}, "ok": False, "no_data_only": True}
    # root event
    r = fetch_json(f"/event/{eid}", retries)
    if r.kind != "ok":
        rec["event_status"] = r.kind
        rec["event_error"] = f"http={r.http} {r.error}"
        _bump(per_ip, r.ip, "fail")
        return rec
    _bump(per_ip, r.ip, "ok")
    event = r.body.get("event", r.body)
    rec["flags"] = {f: event.get(f) for f in ("hasXg", "hasEventPlayerStatistics",
                                              "hasEventPlayerHeatMap")}
    try:
        match_id = inserter.insert_match(event, sid, comp_id)
    except Exception as e:
        rec["event_status"] = "insert_error"
        rec["event_error"] = f"{type(e).__name__}: {e}"[:200]
        try:
            conn.rollback()
        except Exception:
            pass
        return rec

    for ep, action in plan_event_steps(event):
        if action == "skip_disabled":
            rec["steps"][ep] = {"status": "no_data", "reason": "odds_disabled"}
            continue
        if action == "skip_flag":
            rec["steps"][ep] = {"status": "no_data", "reason": "flag_false"}
            continue
        rr = fetch_json(f"/event/{eid}{TAIL[ep]}", retries)
        if rr.kind == "no_data":
            rec["steps"][ep] = {"status": "no_data", "reason": "http_404"}
            _bump(per_ip, rr.ip, "ok")  # healthy IP, data absent
            continue
        if rr.kind != "ok":
            rec["steps"][ep] = {"status": "fail", "error": rr.error}
            _bump(per_ip, rr.ip, "fail")
            rec["no_data_only"] = False
            continue
        _bump(per_ip, rr.ip, "ok")
        try:
            rows = WRITERS[ep](conn, match_id, rr.body, event)
            rec["steps"][ep] = {"status": "ok", "rows": rows}
            if rows is not None and rows > 0:
                rec["no_data_only"] = False
        except Exception as e:
            rec["steps"][ep] = {"status": "fail", "error": f"{type(e).__name__}: {e}"[:160]}
            rec["no_data_only"] = False
            try:
                conn.rollback()
            except Exception:
                pass

    # event ok = every step ok or no_data
    rec["ok"] = all(s.get("status") in ("ok", "no_data") for s in rec["steps"].values())
    return rec


def _bump(per_ip, ip, kind):
    d = per_ip.setdefault(ip or "none", {"ok": 0, "fail": 0, "no_data": 0})
    d[kind] += 1


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def load_comps():
    return [c for c in yaml.safe_load(YAML_FILE.read_text())["competitions"]
            if c["name"] not in DONE]


def resume_done_ids(conn):
    try:
        cur = conn.cursor()
        cur.execute("SELECT match_id FROM matches")
        return {r[0] for r in cur.fetchall()}
    except Exception:
        return set()


def run_live(args):
    import mysql.connector
    env = dict(l.split("=", 1) for l in (ROOT / ".env").read_text().splitlines()
               if "=" in l and not l.startswith("#"))
    conn = mysql.connector.connect(host="127.0.0.1", port=3306, user="appdb_rw",
                                   password=env["MYSQL_PASSWORD"].strip(), database="appdb",
                                   autocommit=False)
    from backfill_runner import DataInserter, ensure_competition, ensure_season
    inserter = DataInserter(conn)
    tables = ("matches", "match_incidents", "match_lineups", "match_statistics",
              "match_shotmap", "match_odds")
    before = {t: _count(conn, t) for t in tables}
    resume = resume_done_ids(conn) if args.resume else set()

    comps = load_comps()
    per_ip, per_comp, global_fail_streak = {}, {}, 0
    t0 = time.monotonic()
    total_ok = total_fail = total_no_data_only = 0

    for c in comps:
        name, ut, sid = c["name"], c["ut_id"], c["season_id_2025_26"]
        retries, early_stop = tier(name)
        comp_id = ensure_competition(conn, name, c.get("category_id") or 0, ut,
                                     "uefa" if c.get("category_id") in (1465, 1467) else "national", "")
        ensure_season(conn, sid, comp_id, "25/26")
        ids = enumerate_events(name, ut, sid)
        if resume:
            ids = [e for e in ids if e not in resume]
        print(f"\n[{name}] ut={ut} season={sid} events={len(ids)} retry={retries}", flush=True)
        pc = per_comp.setdefault(name, {"events": len(ids), "ok": 0, "fail": 0,
                                        "no_data_only": 0, "aborted": None})
        fails_in_row = 0
        for eid in ids:
            rec = process_event(conn, inserter, name, eid, comp_id, sid, retries, per_ip)
            if rec["ok"]:
                total_ok += 1
                pc["ok"] += 1
                fails_in_row = 0
                global_fail_streak = 0
                if rec.get("no_data_only"):
                    total_no_data_only += 1
                    pc["no_data_only"] += 1
            else:
                total_fail += 1
                pc["fail"] += 1
                fails_in_row += 1
                global_fail_streak += 1
            with EV.open("a") as f:
                f.write(json.dumps({"competition": name, **rec}) + "\n")
            if fails_in_row >= early_stop:
                pc["aborted"] = f"early_stop_{early_stop}xfail"
                print(f"  !! abort {name}: {early_stop} consecutive fails", flush=True)
                break
            if global_fail_streak >= GLOBAL_ABORT_STREAK:
                print(f"!! GLOBAL abort: {GLOBAL_ABORT_STREAK} consecutive infra fails", flush=True)
                break
            if pc["ok"] + pc["fail"] >= 100 and (pc["ok"] + pc["fail"]) % 100 == 0:
                print(f"  [{name}] progress ok={pc['ok']} fail={pc['fail']}", flush=True)
        if global_fail_streak >= GLOBAL_ABORT_STREAK:
            break

    after = {t: _count(conn, t) for t in tables}
    conn.commit()
    conn.close()
    total = total_ok + total_fail
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stage": 3, "mode": "live_expansion",
        "events_ok": total_ok, "events_failed": total_fail,
        "no_data_only_events": total_no_data_only,
        "duration_s": round(time.monotonic() - t0, 1),
        "row_counts_before": before, "row_counts_after": after,
        "per_competition": {k: {kk: vv for kk, vv in v.items() if kk != "aborted" or vv}
                            for k, v in per_comp.items()},
        "per_ip": per_ip, "evidence_jsonl": str(EV),
    }
    REPORT.write_text(json.dumps(report, indent=2))
    print("\n" + json.dumps({"events_ok": total_ok, "events_failed": total_fail,
                             "no_data_only": total_no_data_only, "report": str(REPORT)}, indent=2))
    return 0


def _count(conn, t):
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {t}")
    return cur.fetchone()[0]


def run_ids_only(args):
    out = {}
    for c in load_comps():
        ids = enumerate_events(c["name"], c["ut_id"], c["season_id_2025_26"])
        out[c["name"]] = {"ut_id": c["ut_id"], "season_id": c["season_id_2025_26"], "events": ids}
        print(f"[{c['name']}] {len(ids)} events", flush=True)
    IDS_FILE.write_text(json.dumps(out, indent=2))
    print(f"ids artifact: {IDS_FILE}")
    return 0


# ---------------------------------------------------------------------------
# selftest: offline, mocked HTTP + fake inserter. No network, no MySQL.
# ---------------------------------------------------------------------------

def run_selftest(args):
    import types
    results = []

    class FakeResp:
        def __init__(self, status, body):
            self.status_code = status
            self._body = body
            self.content = b"" if body is None else json.dumps(body).encode()

        def json(self):
            return self._body

    class FakeConn:
        def cursor(self):
            return types.SimpleNamespace(execute=lambda *a, **k: None, fetchall=lambda: [])

        def commit(self):
            pass

        def rollback(self):
            pass

    class FakeInserter:
        def __init__(self):
            self.writes = []

        def insert_match(self, event, sid, comp_id):
            self.writes.append(("match", event.get("id")))
            return event.get("id")

    calls = {"n": 0}
    global HTTP_GET

    def fake_get(path, proxy):
        calls["n"] += 1
        p = path
        if p == "/event/1":
            return FakeResp(200, {"id": 1, "hasEventPlayerHeatMap": False,
                                  "hasEventPlayerStatistics": False})
        if p == "/event/2":
            return FakeResp(200, {"id": 2})
        if p == "/event/3":
            return FakeResp(404, None)
        if p == "/event/1/shotmap":
            return FakeResp(200, {"shotmap": [{"id": 1}]})
        if p == "/event/1/statistics":
            return FakeResp(200, {"statistics": [{"period": "ALL"}]})
        if p == "/event/1/lineups":
            return FakeResp(200, {"home": {"players": []}, "away": {"players": []}})
        if p == "/event/1/incidents":
            return FakeResp(200, {"incidents": []})
        if p == "/event/1/odds/1/all":
            return FakeResp(404, None)
        if p == "/event/2/shotmap":
            return FakeResp(404, None)
        if p == "/event/4/lineups":
            return FakeResp(500, None)
        return FakeResp(200, {})

    HTTP_GET = fake_get
    # Restore pool is irrelevant — next_ip still works off _pool but never called
    # into real network; fine.

    # 1) flag pre-gate: stats/shotmap flagged False -> skip_flag; odds disabled -> skip_disabled
    old_writers = dict(WRITERS)
    stub_writes = {"n": 0}
    WRITERS["lineups"] = lambda conn, mid, d, ev: stub_writes.update(n=stub_writes["n"] + 1) or 0
    WRITERS["statistics"] = lambda conn, mid, d, ev: stub_writes.update(n=stub_writes["n"] + 1) or 0
    WRITERS["incidents"] = lambda conn, mid, d, ev: stub_writes.update(n=stub_writes["n"] + 1) or 0
    WRITERS["odds"] = lambda conn, mid, d, ev: stub_writes.update(n=stub_writes["n"] + 1) or 0
    WRITERS["shotmap"] = lambda conn, mid, d, ev: stub_writes.update(n=stub_writes["n"] + 1) or 0
    conn = FakeConn()
    starts = calls["n"]
    rec = process_event(conn, FakeInserter(), "t", 1, 0, 0, 1, {})
    calls_for_1 = calls["n"] - starts
    # event + lineups + incidents = 3 fetches (statistics & shotmap flag-gated, odds disabled)
    results.append(("flag_gate_skips_statistics_and_shotmap_fetch",
                    rec["steps"]["statistics"]["status"] == "no_data" and
                    rec["steps"]["statistics"]["reason"] == "flag_false" and
                    rec["steps"]["shotmap"]["status"] == "no_data" and
                    calls_for_1 == 3))
    results.append(("odds_disabled_no_fetch_no_fail",
                    rec["steps"]["odds"]["status"] == "no_data" and
                    rec["steps"]["odds"]["reason"] == "odds_disabled" and
                    rec["ok"] is True))

    # 2) 404 on shotmap (no flag) -> no_data, no write
    starts = calls["n"]
    rec2 = process_event(conn, FakeInserter(), "t", 2, 0, 0, 1, {})
    calls_for_2 = calls["n"] - starts
    results.append(("shotmap_404_no_data", rec2["steps"]["shotmap"]["status"] == "no_data" and
                    rec2["ok"] is True))

    # 3) event root 404 -> event fail, no sub-endpoints fetched
    starts = calls["n"]
    rec3 = process_event(conn, FakeInserter(), "t", 3, 0, 0, 1, {})
    calls_for_3 = calls["n"] - starts
    results.append(("event_404_fails_no_subfetch", rec3["event_status"] == "no_data" and
                    calls_for_3 == 1 and rec3["ok"] is False))

    # 4) infra 500 on sub-endpoint -> fail after retries (retries=2 => 3 calls)
    starts = calls["n"]
    rec4 = process_event(conn, FakeInserter(), "t", 4, 0, 0, 1, {})
    results.append(("infra_fail_correctly_failed",
                    rec4["steps"]["lineups"]["status"] == "fail" and rec4["ok"] is False))

    WRITERS.clear()
    WRITERS.update(old_writers)

    passed = all(r[1] for r in results)
    print("\n--- selftest ---")
    for name, ok in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print(f"\n  result: {'PASS' if passed else 'FAIL'} ({sum(1 for _, o in results if o)}/{len(results)})")
    return 0 if passed else 1


def run_dryrun(n):
    """Read-only network probe: fetch n real events via dual pool, NO MySQL writes.
    Uses no-op inserter + no-op writers; validates Fix 2 (pool swap) live."""
    import types as _t

    class NoopConn:
        def cursor(self):
            return _t.SimpleNamespace(execute=lambda *a, **k: None, fetchall=lambda: [])
        def commit(self):
            pass
        def rollback(self):
            pass

    class NoopInserter:
        def insert_match(self, event, sid, comp_id):
            return event.get("id")

    # stub writers so process_event never touches MySQL / inserts
    old_writers = dict(WRITERS)
    for k in list(WRITERS):
        WRITERS[k] = lambda conn, mid, d, ev: 0

    per_ip = {}
    overall = {"ok": 0, "fail": 0, "no_data": 0}
    for c in load_comps()[:n if n else len(load_comps())]:
        name, ut, sid = c["name"], c["ut_id"], c["season_id_2025_26"]
        ids = enumerate_events(name, ut, sid)[:5]
        retries, _ = tier(name)
        for eid in ids:
            rec = process_event(NoopConn(), NoopInserter(), name, eid, 0, sid, retries, per_ip)
            overall["ok" if rec["ok"] else "fail"] += 1
            print(f"[{name}] event={eid} ok={rec['ok']} "
                  f"steps={ {k: v.get('status') for k, v in rec['steps'].items()} }", flush=True)
    WRITERS.clear()
    WRITERS.update(old_writers)
    print("\n--dry-run summary--")
    print(json.dumps({"ok": overall["ok"], "fail": overall["fail"],
                      "per_ip": per_ip}, indent=2))
    return 0


def main(argv):
    args = types.SimpleNamespace(
        ids_only="--ids-only" in argv,
        selftest="--selftest" in argv,
        resume="--resume" in argv,
        dryrun="--dry-run" in argv,
    )
    if args.selftest:
        return run_selftest(args)
    if args.dryrun:
        # optional --dry-run N: default 1 comp, 5 events each (read-only, no DB)
        idx = argv.index("--dry-run")
        n = int(argv[idx + 1]) if idx + 1 < len(argv) and argv[idx + 1].isdigit() else 1
        return run_dryrun(n)
    if args.ids_only:
        return run_ids_only(args)
    return run_live(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
