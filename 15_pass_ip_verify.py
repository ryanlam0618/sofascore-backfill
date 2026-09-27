#!/usr/bin/env python3
"""15 PASS IP sample verification — recover 15 IPs from audit hash, probe 7 events each."""
import asyncio, csv, hashlib, json, os, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

def h8(s):
    return hashlib.sha256(str(s).encode()).hexdigest()[:8]

# 1) recover 15 IPs from 100-IP file by matching hash indices from results
old_audit = json.load(open(REPO_ROOT / "data/proxy_audit/proxy_audit_20260827_211530.json"))
res_100 = json.load(open(REPO_ROOT / "data/proxy_audit/100_ip_full_reaudit_20260828_192327.json"))
passed_hashes = {r["ip_index"] for r in res_100["results"] if r["verdict"] == "PASS"}

pool = []
for r in old_audit["rows"]:
    key = f"{r.get('ip','')}:{r.get('port','')}"
    if h8(key) in passed_hashes:
        pool.append({"ip": str(r.get("ip","")), "port": str(r.get("port",""))})
print(f"[verify] recovered {len(pool)} PASS IPs", flush=True)

# 2) events for 7 comps
rows = list(csv.DictReader(open(REPO_ROOT / "data/coverage_audit/season_sample_coverage.csv")))
CL = ["A-League Men","Premier League","La Liga","Serie A","UCL","Ligue 1","J1 League"]
picked = []
for comp in CL:
    cands = sorted([r for r in rows if r["competition"]==comp and r["season_label"]=="19/20" and r["status"]=="ok"],
                   key=lambda r: hashlib.sha256(f"spot19:{comp}:{r['event_id']}".encode()).hexdigest())
    if cands:
        ev = int(cands[0]["event_id"])
        picked.append({"competition": comp, "event_id": ev})
print(f"[verify] 7 events: {[e['competition'] for e in picked]}", flush=True)

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")
user = os.environ.get("SOFA_PROXY_USER", "***REMOVED***")
pw = os.environ.get("SOFA_PROXY_PASS", "")

import curl_cffi.requests as cr

def probe(ip, port, event_id):
    url = f"https://www.sofascore.com/api/v1/event/{event_id}"
    proxy = "http://" + user + ":" + pw + "@" + ip + ":" + port
    t0 = time.monotonic()
    try:
        r = cr.get(url, proxies={"http": proxy, "https": proxy}, impersonate="chrome", timeout=10)
        status = r.status_code
        ok = status == 200 and "event" in r.json()
        lat = int((time.monotonic() - t0) * 1000)
        return {"ip_hash": h8(f"{ip}:{port}"), "event_id": event_id,
                "status": status, "verdict": "PASS" if ok else "FAIL", "latency_ms": lat}
    except Exception as e:
        lat = int((time.monotonic() - t0) * 1000)
        return {"ip_hash": h8(f"{ip}:{port}"), "event_id": event_id,
                "status": 0, "verdict": f"FAIL-{type(e).__name__}", "latency_ms": lat}

async def run():
    loop = asyncio.get_event_loop()
    tasks = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        for ip in pool:
            for ev in picked:
                tasks.append(loop.run_in_executor(ex, probe, ip["ip"], ip["port"], ev["event_id"]))
        results = []
        for i, f in enumerate(asyncio.as_completed(tasks), 1):
            r = await f
            results.append(r)
            if i % 14 == 0 or i == len(tasks):
                print(f"[verify] {i}/{len(tasks)}", flush=True)

    # per-IP aggregation
    from collections import defaultdict
    agg = defaultdict(lambda: {"pass":0,"total":0,"latencies":[]})
    for r in results:
        iph = r["ip_hash"]
        agg[iph]["total"] += 1
        agg[iph]["latencies"].append(r["latency_ms"])
        if r["verdict"] == "PASS":
            agg[iph]["pass"] += 1
    for iph, s in agg.items():
        s["pass_rate"] = round(s["pass"] / s["total"], 3)
        s["avg_latency"] = int(sum(s["latencies"]) / len(s["latencies"]))
        s["tier"] = "A" if s["total"] == s["pass"] else ("B" if s["pass"] >= 5 else ("C" if s["pass"] >= 3 else "D"))

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    outfile = REPO_ROOT / "data" / "proxy_audit" / f"15_pass_ip_verify_19_20_{ts}.json"
    ev_map = {e["event_id"]: e["competition"] for e in picked}
    json.dump({
        "task": "15_pass_ip_verify_19_20",
        "season": "19/20",
        "n_ips": len(pool),
        "n_events": len(picked),
        "total_probes": len(results),
        "aggregates": [{"ip_hash": k, **v} for k, v in sorted(agg.items(), key=lambda x: -x[1]["pass"])],
        "per_probe": [{"ip_hash": r["ip_hash"], "event_id": r["event_id"], "comp": ev_map[r["event_id"]], "status": r["status"], "verdict": r["verdict"], "latency_ms": r["latency_ms"]} for r in results],
    }, open(outfile, "w"), indent=2, ensure_ascii=False)
    print(f"[verify] written: {outfile}", flush=True)

asyncio.run(run())
