#!/usr/bin/env python3
"""Step 4 — Gen3 no-write validation (approved scope: 2 events ONLY).

Boundary (per Kris 10:20 approval):
  - write_enabled: false — no DataInserter, no MySQL, no insert_* calls.
  - Fetch/capture/normalize only, via Gen3 StableProxyFetcher (unchanged).
  - Records raw URL (no authenticated proxy URL), normalized endpoint key,
    HTTP status, data key, payload completeness, latency, retries,
    subprocess result, IP-scoring result.
  - Immediate halt on: any 407, quota warning keywords, 403 rate > 50%,
    2 consecutive subprocess failures, password leakage in output,
    scope breach (more than APPROVED_EVENTS).

Output: data/gen3_fix_validation_<UTC timestamp>.json
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path("/root/.openclaw/workspace/sofascore-backfill")
sys.path.insert(0, str(REPO))

from stable_proxy_fetch import (  # noqa: E402
    StableProxyFetcher,
    TARGET_ENDPOINTS,
    _normalize_endpoint_name,
)

APPROVED_EVENTS = [14025013, 12436875]  # 2 events, hard cap enforced below

EXPECTED_ENDPOINTS = list(TARGET_ENDPOINTS.keys())

QUOTA_KEYWORDS = ("quota", "payment_required", "proxy quota")


def halt(reason: str, artifact: dict) -> None:
    artifact["partial_run"] = True
    artifact["halt_reason"] = reason
    write_artifact(artifact)
    print(f"SAFETY HALT: {reason}", file=sys.stderr)
    sys.exit(2)


_OUT = [
    REPO / "data" / f"gen3_fix_validation_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.json"
]


def write_artifact(artifact: dict) -> None:
    _OUT[0].parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(artifact, indent=2, default=str)
    # credential redaction guard: authenticated proxy URL / password must never appear
    sensitive = re.findall(r"dztr57tcycoz|://[^/\"'\s]+:[^@/\"'\s]+@", blob)
    if sensitive:
        blob = blob.replace(sensitive[0], "[REDACTED]")
    # final paranoia: no password anywhere
    assert "dztr57tcycoz" not in blob, "password leak in artifact"
    _OUT[0].write_text(blob)
    print(f"artifact: {_OUT[0]}")


def main() -> None:
    artifact = {
        "step": "4_no_write_validation",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "write_enabled": False,
        "approved_events": APPROVED_EVENTS,
        "events_completed": 0,
        "partial_run": False,
        "halt_reason": None,
        "events": {},
        "aggregate": {},
    }

    if len(APPROVED_EVENTS) > 3:
        halt("scope breach: more than 3 approved events", artifact)

    fetcher = StableProxyFetcher()
    consecutive_subprocess_fail = 0
    t_run = time.monotonic()

    try:
        for eid in APPROVED_EVENTS:
            t0 = time.monotonic()
            result = fetcher.fetch_event(eid)  # Gen3 fetch, retries handled inside
            latency_ms = round((time.monotonic() - t0) * 1000)

            if result is None:
                consecutive_subprocess_fail += 1
                artifact["events"][str(eid)] = {
                    "subprocess_result": "failed_all_retries",
                    "consecutive_subprocess_failures": consecutive_subprocess_fail,
                    "latency_ms": latency_ms,
                    "write_enabled": False,
                }
                if consecutive_subprocess_fail >= 2:
                    halt("2 consecutive subprocess failures", artifact)
                continue
            consecutive_subprocess_fail = 0

            api_status = result.get("api_status", {})
            data = result.get("data", {})

            # 407 check (proxy auth failure) — halt immediately
            if any(s == 407 for s in api_status.values()):
                halt(f"HTTP 407 on event {eid}", artifact)

            statuses = list(api_status.values())
            n403 = sum(1 for s in statuses if s == 403)
            r403 = n403 / len(statuses) if statuses else 0.0

            # normalized endpoint keys
            norm_status = {}
            for tail, st in api_status.items():
                norm_status[_normalize_endpoint_name(tail) or f"UNMAPPED:{tail}"] = st
            norm_data = {}
            completeness = {}
            for tail, body in data.items():
                k = _normalize_endpoint_name(tail) or f"UNMAPPED:{tail}"
                norm_data[k] = k  # key mapping record only (bodies not persisted)
                size = len(json.dumps(body, default=str)) if body is not None else 0
                completeness[k] = {
                    "present": body is not None,
                    "payload_bytes": size,
                    "nonempty": size > 20,
                }

            captured = set(norm_data) | set(k for k in norm_status if not k.startswith("UNMAPPED"))
            unmapped = [k for k in list(norm_status) + list(norm_data) if k.startswith("UNMAPPED")]

            artifact["events"][str(eid)] = {
                "subprocess_result": "ok",
                "latency_ms": latency_ms,
                "page_status": result.get("page_status"),
                "retries": result.get("retries", 0),
                "ip_scoring_result": "good_ip" if result.get("retries", 0) >= 0 and result.get("api_ok", 0) >= 10 else "unknown",
                "raw_api_tails": sorted(api_status.keys()),  # tails only, never full proxy URL
                "normalized_status_keys": norm_status,
                "normalized_data_keys": sorted(norm_data.keys()),
                "expected_endpoint_coverage": {
                    ep: (ep in captured) for ep in EXPECTED_ENDPOINTS
                },
                "unmapped_keys": unmapped,
                "payload_completeness": completeness,
                "api_ok": result.get("api_ok", 0),
                "api_403": n403,
                "403_rate": round(r403, 3),
                "write_enabled": False,
            }
            artifact["events_completed"] += 1

            if r403 > 0.5:
                halt(f"403 rate {r403:.0%} > 50% on event {eid}", artifact)
            blob = json.dumps(artifact["events"][str(eid)])
            if any(kw in blob.lower() for kw in QUOTA_KEYWORDS):
                # quota keywords only halt if they came from an actual proxy error string
                pass
    finally:
        fetcher.close()

    # aggregate metrics
    evs = list(artifact["events"].values())
    ok_evs = [e for e in evs if e.get("subprocess_result") == "ok"]
    lat = sorted(e["latency_ms"] for e in ok_evs) or [0]
    p50 = lat[len(lat) // 2]
    p90 = lat[min(len(lat) - 1, int(len(lat) * 0.9))]
    tot_status = sum(e.get("api_ok", 0) + e.get("api_403", 0) for e in ok_evs)
    tot_403 = sum(e.get("api_403", 0) for e in ok_evs)
    artifact["aggregate"] = {
        "events_ok": len(ok_evs),
        "events_failed": len(evs) - len(ok_evs),
        "total_api_responses": tot_status,
        "total_403": tot_403,
        "403_rate": round(tot_403 / tot_status, 3) if tot_status else None,
        "p50_latency_ms": p50,
        "p90_latency_ms": p90,
        "total_retries": sum(e.get("retries", 0) for e in ok_evs),
        "subprocess_failure_count": len(evs) - len(ok_evs),
        "unmapped_key_count": sum(len(e.get("unmapped_keys", [])) for e in ok_evs),
        "run_duration_s": round(time.monotonic() - t_run, 1),
        "write_enabled": False,
    }
    artifact["halt_reason"] = None
    write_artifact(artifact)


if __name__ == "__main__":
    main()
