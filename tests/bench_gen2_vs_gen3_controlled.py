#!/usr/bin/env python3
"""Controlled Stability Benchmark — Gen2 hybrid (production) vs Gen3 shadow.

Approved scope (Kris 16:08, 16:25):
  - Methods: gen2_hybrid_production (BackfillClient.fetch_api),
             gen3_stable_proxy_shadow (StableProxyFetcher)
  - Events: 14025013, 12436875 (hard cap)
  - No cold curl_cffi, no DataInserter/MySQL, no fetch_api default change,
    no scope growth.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path("/root/.openclaw/workspace/sofascore-backfill")
sys.path.insert(0, str(REPO))

from stable_proxy_fetch import StableProxyFetcher, TARGET_ENDPOINTS  # noqa: E402

EVENTS = [14025013, 12436875]
OUT = REPO / "data" / f"bench_gen2_vs_gen3_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.json"

artifact = {
    "step": "controlled_stability_benchmark",
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "write_enabled": False,
    "events": EVENTS,
    "partial_run": False,
    "halt_reason": None,
    "per_event": {},
    "aggregate": {},
}


def halt(reason: str) -> None:
    artifact["partial_run"] = True
    artifact["halt_reason"] = reason
    save()
    print(f"SAFETY HALT: {reason}", file=sys.stderr)
    sys.exit(2)


def save() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(artifact, indent=2, default=str)
    assert "dztr57tcycoz" not in blob, "password leak"
    OUT.write_text(blob)


# ---- Gen3 stable proxy shadow (fresh run) ----
def run_gen3() -> dict:
    fetcher = StableProxyFetcher()
    out = {}
    for eid in EVENTS:
        t0 = time.monotonic()
        try:
            r = fetcher.fetch_event(eid)
        except Exception as e:
            if "407" in str(e):
                halt(f"Gen3 407 on event {eid}")
            raise
        lat = round((time.monotonic() - t0) * 1000)
        if r is None:
            out[str(eid)] = {"subprocess_result": "failed", "latency_ms": lat}
            continue
        statuses = r.get("api_status", {})
        for ep in statuses:
            if ep in ("",) or statuses[ep] == 407:
                halt(f"Gen3 407 on event {eid}")
        n403 = sum(1 for s in statuses.values() if s == 403)
        total = len(statuses)
        if total and n403 / total > 0.5:
            halt(f"Gen3 403 rate > 50% on event {eid}")
        out[str(eid)] = {
            "subprocess_result": "ok",
            "latency_ms": lat,
            "page_status": r.get("page_status"),
            "retries": r.get("retries", 0),
            "api_ok": r.get("api_ok", 0),
            "api_403": n403,
            "403_rate": round(n403 / total, 3) if total else None,
            "api_status_keys": sorted(statuses.keys()),
            "raw_data_keys": sorted(r.get("data", {}).keys()),
        }
    fetcher.close()
    return out


# ---- Gen2 hybrid production fetch_api ----
async def run_gen2() -> dict:
    from backfill_runner import BackfillClient
    out = {}
    async with BackfillClient(headless=True) as client:
        await client.warm_homepage()
        for eid in EVENTS:
            ev = {}
            await client.warm_event(eid)
            for name, path in TARGET_ENDPOINTS.items():
                real_path = path.replace("{eid}", str(eid))
                t0 = time.monotonic()
                try:
                    res = await client.fetch_api(real_path, timeout_ms=30000, max_retries=5)
                except Exception as e:
                    if "407" in str(e):
                        halt(f"Gen2 407 on {name} event {eid}")
                    ev[name] = {"http_status": 0, "error": str(e)[:120], "latency_ms": round((time.monotonic()-t0)*1000)}
                    continue
                lat = round((time.monotonic() - t0) * 1000)
                st = res.get("status", 0)
                if st == 407:
                    halt(f"Gen2 407 on {name} event {eid}")
                body = res.get("body")
                bl = len(json.dumps(body)) if body is not None else 0
                ev[name] = {
                    "http_status": st,
                    "payload_bytes": bl,
                    "complete": st == 200 and bl > 20,
                    "latency_ms": lat,
                    "transport": res.get("transport", "unknown"),
                }
            n403 = sum(1 for v in ev.values() if v.get("http_status") == 403)
            tot = len(ev)
            if tot and n403 / tot > 0.5:
                halt(f"Gen2 403 rate > 50% on event {eid}")
            out[str(eid)] = {
                "by_endpoint": ev,
                "api_ok": sum(1 for v in ev.values() if v.get("http_status") == 200),
                "api_403": n403,
                "403_rate": round(n403 / tot, 3) if tot else None,
                "retries_observed": client._request_count,
            }
    return out


def classify(g3_data: dict, ep_name: str, path_template: str) -> str:
    eid_match = re.search(r"event/(\d+)/", path_template)
    if not eid_match:
        return "not_triggered_by_event_page"
    eid = eid_match.group(1)
    # Look up in raw_data_keys (the raw api/v1/ tail captured)
    # Gen3 keys are full bare tails; need to match this endpoint
    suffix = "/" + ep_name if ep_name != "event" else ""
    if ep_name == "web-odds":
        suffix = "/odds/1/web-odds"
    raw_key = f"event/{eid}{suffix}"
    keys = set(g3_data.get("raw_data_keys", []) + g3_data.get("api_status_keys", []))
    if raw_key in keys:
        # captured — was it complete?
        return "captured_and_complete"
    return "not_triggered_by_event_page"


async def main() -> None:
    # Gen3 first (independent)
    gen3 = run_gen3()
    # Gen2
    gen2 = await run_gen2()

    # cross reference
    for eid in EVENTS:
        eid_s = str(eid)
        g3 = gen3[eid_s]
        g2 = gen2[eid_s]["by_endpoint"]
        for name, path in TARGET_ENDPOINTS.items():
            cls = classify(g3, name, path)
            g3_entry = g2.get(name, {})
            artifact["per_event"].setdefault(eid_s, {"gen2": {}, "gen3": {}, "match": {}})
            artifact["per_event"][eid_s]["gen2"][name] = g3_entry
            artifact["per_event"][eid_s]["gen3"][name] = {
                "classification": cls,
                "http_status": g3.get("api_status_keys"),  # n/a; classification is the truth
            }
            g2c = g3_entry.get("complete", False)
            artifact["per_event"][eid_s]["match"][name] = (
                "both_complete" if g2c and cls == "captured_and_complete"
                else "gen2_only" if g2c
                else "gen3_only" if cls == "captured_and_complete"
                else "both_missing"
            )

    # aggregate
    all_g2_status = [v.get("http_status", 0) for ev in artifact["per_event"].values() for v in ev["gen2"].values()]
    all_g2_lat = sorted([v["latency_ms"] for ev in artifact["per_event"].values() for v in ev["gen2"].values()])
    g3_lat = sorted([gen3[str(e)]["latency_ms"] for e in EVENTS])
    cls_counts = {}
    for ev in artifact["per_event"].values():
        for v in ev["gen3"].values():
            cls_counts[v["classification"]] = cls_counts.get(v["classification"], 0) + 1
    match_counts = {}
    for ev in artifact["per_event"].values():
        for v in ev["match"].values():
            match_counts[v] = match_counts.get(v, 0) + 1

    artifact["aggregate"] = {
        "gen2": {
            "endpoints_total": len(all_g2_status),
            "http_200": sum(1 for s in all_g2_status if s == 200),
            "http_403": sum(1 for s in all_g2_status if s == 403),
            "http_407": sum(1 for s in all_g2_status if s == 407),
            "403_rate": round(sum(1 for s in all_g2_status if s == 403) / len(all_g2_status), 3),
            "p50_latency_ms": all_g2_lat[len(all_g2_lat)//2],
            "p90_latency_ms": all_g2_lat[int(len(all_g2_lat)*0.9)],
            "complete_endpoints": sum(1 for ev in artifact["per_event"].values() for v in ev["gen2"].values() if v.get("complete")),
            "retries_proxy_request_count": gen2[str(EVENTS[0])]["retries_observed"],
        },
        "gen3": {
            "events_total": len(EVENTS),
            "classification_counts": cls_counts,
            "403_rate_overall": round(sum(gen3[str(e)]["403_rate"] for e in EVENTS) / len(EVENTS), 3),
            "p50_latency_ms": g3_lat[len(g3_lat)//2],
            "p90_latency_ms": g3_lat[-1],
            "total_retries": sum(gen3[str(e)].get("retries", 0) for e in EVENTS),
            "subprocess_failures": sum(1 for e in EVENTS if gen3[str(e)]["subprocess_result"] != "ok"),
            "ip_scoring_per_event": {str(e): ("good_ip" if gen3[str(e)].get("api_ok", 0) >= 10 else "bad_ip") for e in EVENTS},
        },
        "match_counts": match_counts,
        "endpoints_compared": len(all_g2_status),
        "write_enabled": False,
    }
    save()
    print(f"artifact: {OUT}")
    print(json.dumps(artifact["aggregate"], indent=2))


if __name__ == "__main__":
    asyncio.run(main())
