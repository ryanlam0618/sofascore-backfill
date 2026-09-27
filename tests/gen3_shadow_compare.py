#!/usr/bin/env python3
"""Step 5 — Read-only shadow comparison: Gen2 (authoritative) vs Gen3 (shadow).

Approved scope (Kris 10:55): the SAME 2 events from Step 4 only. No scope growth.

Gen2 = cold curl_cffi direct fetch per endpoint (read-only reference; does NOT
reuse backfill_runner.fetch_api, no browser, no MySQL, no DataInserter).
Gen3 = results REUSED from Step 4 artifact (no re-fetch; avoids extra requests).

Classification per endpoint/event for Gen3:
  captured_and_complete | captured_but_empty | not_triggered_by_event_page | blocked_or_failed

Output: data/shadow_compare_<UTC ts>.json (new artifact; Step 4 file untouched).
Safety halts: any 407, quota keywords, 403 rate >50%, credential leak, scope breach.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path("/root/.openclaw/workspace/sofascore-backfill")
sys.path.insert(0, str(REPO))

from curl_cffi import requests as curl_requests  # noqa: E402
from stable_proxy_fetch import TARGET_ENDPOINTS  # noqa: E402

BASE = "https://www.sofascore.com"
STEP4 = REPO / "data" / "gen3_fix_validation_20260823_022720.json"
EVENTS = [14025013, 12436875]  # hard-coded = exactly the Step 4 approved events

OUT = REPO / "data" / f"shadow_compare_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.json"

artifact = {
    "step": "5_read_only_shadow_comparison",
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "write_enabled": False,
    "events": EVENTS,
    "gen3_source_artifact": str(STEP4),
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
    blob = json.dumps(artifact, indent=2, default=str)
    assert "dztr57tcycoz" not in blob and "***REMOVED***-rotate" not in blob, "credential leak"
    OUT.write_text(blob)


def load_proxy() -> dict | None:
    pwd = os.environ.get("SOFA_PROXY_PASS", "")
    if not pwd:
        # read from workspace .env without printing it
        env = Path("/root/.openclaw/workspace/.env")
        for line in env.read_text().splitlines():
            if line.startswith("SOFA_PROXY_PASS="):
                pwd = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not pwd:
        halt("proxy password missing (would cause 407)")  # treat as pre-flight stop
    user = os.environ.get("SOFA_PROXY_USER", "***REMOVED***-rotate")
    return {"http": f"http://{user}:{pwd}@p.webshare.io:80",
            "https": f"http://{user}:{pwd}@p.webshare.io:80"}


# ---- Gen3 shadow: reuse Step 4 artifact (read-only) ----
step4 = json.loads(STEP4.read_text())
assert step4["write_enabled"] is False and step4["events_completed"] == 2

# ---- Gen2 authoritative: cold curl_cffi per endpoint ----
proxies = load_proxy()
gen2_statuses = []
gen2_lat = []
total_403 = 0

for eid in EVENTS:
    ev = {"gen2": {}, "gen3": {}, "match": {}}
    g3 = step4["events"][str(eid)]
    g3_status = g3["normalized_status_keys"]
    g3_complete = g3["payload_completeness"]

    for name, path in TARGET_ENDPOINTS.items():
        real_path = path.replace("{eid}", str(eid))
        url = BASE + real_path
        t0 = time.monotonic()
        try:
            r = curl_requests.get(url, proxies=proxies, impersonate="chrome", timeout=30)
            status = r.status_code
            try:
                body = r.json()
            except Exception:
                body = None
        except Exception as e:
            status, body = 0, None
            if "407" in str(e):
                halt(f"Gen2 407 on {name} event {eid}")
        lat = round((time.monotonic() - t0) * 1000)

        if status == 407:
            halt(f"HTTP 407 on {url}")
        if status == 403:
            total_403 += 1
        bl = len(json.dumps(body)) if body is not None else 0
        gen2_statuses.append(status)
        gen2_lat.append(lat)
        ev["gen2"][name] = {
            "url": url, "http_status": status, "payload_bytes": bl,
            "complete": status == 200 and bl > 20,
            "latency_ms": lat, "transport": "curl_cffi_cold", "retries": 0,
        }

        # Gen3 classification
        st = g3_status.get(name)
        comp = g3_complete.get(name)
        if st is None:
            cls = "not_triggered_by_event_page"
        elif st == 403 or (st and st >= 400):
            cls = "blocked_or_failed"
        elif comp and comp.get("nonempty"):
            cls = "captured_and_complete"
        else:
            cls = "captured_but_empty"
        complete = cls == "captured_and_complete"
        ev["gen3"][name] = {
            "http_status": st, "classification": cls, "complete": complete,
            "payload_bytes": comp["payload_bytes"] if comp else 0,
            "transport": "cloakbrowser_subprocess", "retries": g3.get("retries", 0),
        }
        g2c = ev["gen2"][name]["complete"]
        ev["match"][name] = (
            "both_complete" if (g2c and complete)
            else "gen2_only" if g2c
            else "gen3_only" if complete
            else "both_missing"
        )
    artifact["per_event"][str(eid)] = ev
    if total_403 and total_403 / max(1, len(gen2_statuses)) > 0.5:
        halt("Gen2 403 rate > 50%")

# ---- aggregate ----
lat_s = sorted(gen2_lat)
n = len(lat_s)
match_counts = {}
for ev in artifact["per_event"].values():
    for v in ev["match"].values():
        match_counts[v] = match_counts.get(v, 0) + 1

g3_lat = [e["latency_ms"] for e in step4["events"].values()]
artifact["aggregate"] = {
    "endpoints_compared": sum(len(ev["match"]) for ev in artifact["per_event"].values()),
    "match_counts": match_counts,
    "gen2": {
        "http_success_rate": round(sum(1 for s in gen2_statuses if s == 200) / len(gen2_statuses), 3),
        "403_rate": round(sum(1 for s in gen2_statuses if s == 403) / len(gen2_statuses), 3),
        "407_count": sum(1 for s in gen2_statuses if s == 407),
        "p50_latency_ms": lat_s[n // 2],
        "p90_latency_ms": lat_s[min(n - 1, int(n * 0.9))],
        "retry_count": 0,
        "subprocess_failures": 0,
        "write_enabled": False,
    },
    "gen3": {
        "http_success_rate": None,  # see per-endpoint classifications
        "classification_counts": {
            cls: sum(
                1 for ev in artifact["per_event"].values()
                for ep in ev["gen3"].values() if ep["classification"] == cls
            ) for cls in (
                "captured_and_complete", "captured_but_empty",
                "not_triggered_by_event_page", "blocked_or_failed")
        },
        "403_rate": step4["aggregate"]["403_rate"],
        "407_count": 0,
        "p50_latency_ms": sorted(g3_lat)[len(g3_lat) // 2],
        "p90_latency_ms": max(g3_lat),
        "retry_count": step4["aggregate"]["total_retries"],
        "subprocess_failures": step4["aggregate"]["subprocess_failure_count"],
        "ip_scoring": [e.get("ip_scoring_result") for e in step4["events"].values()],
        "write_enabled": False,
    },
    "write_enabled": False,
}
save()
print(f"artifact: {OUT}")
print(json.dumps(artifact["aggregate"], indent=2))
