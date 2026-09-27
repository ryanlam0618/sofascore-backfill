#!/usr/bin/env python3
"""Round-B: candidates with round-1 200 get 2 confirmation probes (>=5 min apart).
3/3 200 -> good_20260912.txt. Writes final summary JSON."""
import json, random, time
from datetime import datetime, timezone
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_pool_20260912 import (parse_pool, probe, PROBE_URLS, TRAIL,
                                 OUT_GOOD, AUDIT_DIR)

FINAL = AUDIT_DIR / "audit_20260912_final_v2.json"

pool = {f"{m['ip']}:{m['port']}": m for m in parse_pool()}
r1 = {}
for l in TRAIL.open():
    d = json.loads(l)
    if d["round"] in (1, "1b"):
        r1[d["key"]] = d["status"]
cands = [k for k, s in r1.items() if s == 200]
print(f"candidates: {len(cands)}", flush=True)

results = {k: [200] for k in cands}  # round-1 result already recorded
for rnd in (2, 3):
    print(f"sleeping before round {rnd}...", flush=True)
    time.sleep(390 if rnd == 2 else 330)
    for k in cands:
        res = probe(pool[k], PROBE_URLS[(rnd - 1) % len(PROBE_URLS)])
        res["ts"] = datetime.now(timezone.utc).isoformat()
        with TRAIL.open("a") as f:
            f.write(json.dumps({"round": rnd, "key": k, **res}) + "\n")
        results[k].append(res["status"])
        print(f"r{rnd} {k} -> {res['status'] or res['err']}", flush=True)
        time.sleep(random.uniform(1.0, 2.0))

good = [k for k, v in results.items() if all(s == 200 for s in v)]
g9 = {l.split(":")[0] for l in open(
    Path(__file__).parent / "data/proxy_pools/good_20260909_v2.txt")}
OUT_GOOD.write_text("\n".join(
    f"{pool[k]['ip']}:{pool[k]['port']}:{pool[k]['user']}:{pool[k]['pw']}"
    for k in good) + "\n")
summary = {
    "finished": datetime.now(timezone.utc).isoformat(),
    "total_pool": len(pool), "round1_200": len(cands),
    "good_3of3": len(good),
    "good_keys": good,
    "good_ips": [k.split(":")[0] for k in good],
    "overlap_with_good_20260909_v2": sorted(
        k.split(":")[0] for k in good if k.split(":")[0] in g9),
    "results": results,
}
FINAL.write_text(json.dumps(summary, indent=2))
print(json.dumps({k: summary[k] for k in
                  ("good_3of3", "good_keys", "overlap_with_good_20260909_v2")},
                 indent=2), flush=True)
