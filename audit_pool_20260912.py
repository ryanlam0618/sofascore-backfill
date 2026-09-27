#!/usr/bin/env python3
"""Audit the fresh 2026-09-12 Webshare 100-IP pool against SofaScore.

3 probe rounds, ~6 min spacing between rounds, chrome124 impersonation.
Verdict per IP: GOOD (all probes 200) else BAD. Checkpoint after every probe.
Safety brake: abort round if rolling overall 403 rate exceeds 50%.
"""
from __future__ import annotations

import json, random, sys, time
from datetime import datetime, timezone
from pathlib import Path

from curl_cffi import requests as creq

ROOT = Path(__file__).resolve().parent
POOL = ROOT / "data" / "webshare_proxy_pool_20260912.txt"
OUT_GOOD = ROOT / "data" / "proxy_pools" / "good_20260912.txt"
AUDIT_DIR = ROOT / "data" / "proxy_audit"
CHECKPOINT = AUDIT_DIR / "audit_20260912_checkpoint.json"
TRAIL = AUDIT_DIR / "audit_20260912_trail.jsonl"

PROBE_URLS = [
    "https://api.sofascore.com/api/v1/event/14024019",
    "https://api.sofascore.com/api/v1/event/14062144",
    "https://api.sofascore.com/api/v1/event/13981661",
]
IMPERSONATE = "chrome124"
TIMEOUT_S = 15
ROUNDS = 3
ROUND_GAP_S = 360  # 6 min between rounds
PER_REQ_DELAY = (0.8, 1.8)
BRAKE_403_RATE = 0.50


def utc():
    return datetime.now(timezone.utc).isoformat()


def parse_pool():
    out = []
    for line in POOL.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        ip, port, user, pw = line.split(":", 3)
        out.append({"ip": ip, "port": port, "user": user, "pw": pw})
    return out


def key(m):
    return f"{m['ip']}:{m['port']}"


def probe(m, url):
    proxy = f"http://{m['user']}:{m['pw']}@{m['ip']}:{m['port']}"
    try:
        r = creq.get(url, proxies={"http": proxy, "https": proxy},
                     impersonate=IMPERSONATE, timeout=TIMEOUT_S)
        return {"status": r.status_code, "ok": r.status_code == 200,
                "err": None}
    except Exception as e:
        return {"status": None, "ok": False, "err": type(e).__name__}


def main():
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    pool = parse_pool()
    assert len(pool) == 100, len(pool)

    if CHECKPOINT.exists():
        ck = json.loads(CHECKPOINT.read_text())
    else:
        ck = {"started": utc(), "rounds": {}, "last_completed_round": 0}
    results = ck["rounds"]  # {round_idx: {key: probe_result}}

    for rnd in range(1, ROUNDS + 1):
        rkey = str(rnd)
        done = results.setdefault(rkey, {})
        previous = results.get(str(rnd - 1), {}) if rnd > 1 else {}
        statuses = []
        for i, m in enumerate(pool):
            k = key(m)
            if k in done:
                continue
            url = PROBE_URLS[(rnd - 1) % len(PROBE_URLS)]
            res = probe(m, url)
            res["ts"] = utc()
            done[k] = res
            statuses.append(res["status"])
            with TRAIL.open("a") as f:
                f.write(json.dumps({"round": rnd, "key": k, **res}) + "\n")
            CHECKPOINT.with_suffix(".tmp").write_text(json.dumps(ck))
            CHECKPOINT.with_suffix(".tmp").replace(CHECKPOINT)
            # safety brake: mid-round, after >=30 probes, check 403 rate of this run
            if len(statuses) >= 30 and rnd == 1:
                r403 = statuses.count(403) / len(statuses)
                if r403 > BRAKE_403_RATE:
                    ck["braked"] = f"round1 403 rate {r403:.2%} > 50%"
                    CHECKPOINT.write_text(json.dumps(ck))
                    finalize(ck, pool, braked=True)
                    return
            if i < len(pool) - 1:
                time.sleep(random.uniform(*PER_REQ_DELAY))
        ck["last_completed_round"] = rnd
        CHECKPOINT.with_suffix(".tmp").write_text(json.dumps(ck))
        CHECKPOINT.with_suffix(".tmp").replace(CHECKPOINT)
        # global brake across all rounds so far
        allst = [v["status"] for rd in results.values() for v in rd.values()]
        if allst and allst.count(403) / len(allst) > BRAKE_403_RATE:
            ck["braked"] = f"overall 403 rate {allst.count(403)/len(allst):.2%} > 50%"
            CHECKPOINT.write_text(json.dumps(ck))
            finalize(ck, pool, braked=True)
            return
        if rnd < ROUNDS:
            time.sleep(ROUND_GAP_S)
    finalize(ck, pool, braked=False)


def finalize(ck, pool, braked):
    results = ck["rounds"]
    good, verdicts = [], {}
    for m in pool:
        k = key(m)
        probes = [results.get(str(r), {}).get(k) for r in range(1, ROUNDS + 1)]
        got = [p for p in probes if p]
        is_good = len(got) == ROUNDS and all(p["ok"] for p in got)
        verdicts[k] = {
            "verdict": "GOOD" if is_good else "BAD",
            "statuses": [p["status"] if p else None for p in probes],
        }
        if is_good:
            good.append(m)
    summary = {
        "finished": utc(), "braked": braked, "brake_note": ck.get("braked"),
        "total_ips": len(pool), "good": len(good), "bad": len(pool) - len(good),
        "good_rate": len(good) / len(pool),
        "status_histogram": {},
    }
    for v in verdicts.values():
        for s in v["statuses"]:
            summary["status_histogram"][str(s)] = \
                summary["status_histogram"].get(str(s), 0) + 1
    if not braked:
        OUT_GOOD.write_text("\n".join(
            f"{m['ip']}:{m['port']}:{m['user']}:{m['pw']}" for m in good) + "\n")
    (AUDIT_DIR / "audit_20260912_final.json").write_text(json.dumps(
        {"summary": summary, "verdicts": verdicts}, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
