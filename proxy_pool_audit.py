#!/usr/bin/env python3
"""proxy_pool_audit.py — classify a static proxy list against SofaScore.

Purpose (Kris 2026-08-28 03:12): test the 100 Webshare STATIC proxy IPs to
find which ones SofaScore accepts (200) vs blocks (403). Output drives the
decision on which IPs to keep for the backfill rotation pool.

SECURITY
  - Input proxy list lines are `ip:port:user:pass`.
  - Credentials are NEVER written to any artifact, stdout, or commit.
  - Good-list file WITH credentials is written under data/proxy_audit/
    which MUST be gitignored.

METHOD
  For each proxy, run `repeats` GETs against the target URL(s) with
  curl_cffi (impersonate chrome, same posture as Gen4 Tier-1).
  Verdicts:
    GOOD    — all attempts 200
    MIXED   — some 200 some 403
    BLOCKED — all 403
    ERROR   — all network/proxy errors (incl. 407 proxy auth fail)
    DEAD    — all timeouts / conn refused

Usage:
  PYTHONUNBUFFERED=1 .runner-venv/bin/python proxy_pool_audit.py \
      --proxy-file /root/.openclaw/media/inbound/Webshare_100_proxies_8---....txt \
      --repeats 3 --concurrency 10
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_TARGETS = [
    "https://www.sofascore.com/api/v1/event/14025013",              # known-good endpoint
    "https://www.sofascore.com/api/v1/event/14025013/incidents",    # the historically-blocked one
]

REPO_ROOT = Path(__file__).resolve().parent
OUT_DIR = REPO_ROOT / "data" / "proxy_audit"


def parse_proxy_line(line: str) -> Optional[Dict[str, str]]:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = line.split(":")
    if len(parts) != 4:
        return None
    ip, port, user, pw = parts
    return {"ip": ip, "port": port, "user": user, "pw": pw,
            "url": f"http://{user}:{pw}@{ip}:{port}"}


def fetch_once(proxy_url: str, target: str, timeout_s: float) -> Dict[str, Any]:
    """Single attempt through one static proxy. Returns status/latency/error."""
    from curl_cffi import requests as cr  # lazy import
    t0 = time.monotonic()
    try:
        r = cr.get(target,
                   proxies={"http": proxy_url, "https": proxy_url},
                   impersonate="chrome", timeout=timeout_s)
        return {"status": r.status_code, "latency_ms": int((time.monotonic() - t0) * 1000),
                "error": None}
    except Exception as e:
        return {"status": 0, "latency_ms": int((time.monotonic() - t0) * 1000),
                "error": f"{type(e).__name__}: {e}"}


def classify(attempts: List[Dict[str, Any]]) -> str:
    codes = [a["status"] for a in attempts]
    if all(c == 200 for c in codes):
        return "GOOD"
    if 200 in codes and 403 in codes:
        return "MIXED"
    if all(c == 403 for c in codes):
        return "BLOCKED"
    if all(c == 0 for c in codes):
        kinds = {a["error"] for a in attempts}
        if any("timed out" in (k or "").lower() or "timeout" in (k or "").lower() for k in kinds):
            return "DEAD"
        return "ERROR"
    return "MIXED"


async def test_proxy(px: Dict[str, str], targets: List[str], repeats: int,
                     timeout_s: float, sem: asyncio.Semaphore,
                     jitter: float) -> Dict[str, Any]:
    results: Dict[str, List[Dict[str, Any]]] = {t: [] for t in targets}
    async with sem:
        for rep in range(repeats):
            for t in targets:
                r = await asyncio.to_thread(fetch_once, px["url"], t, timeout_s)
                results[t].append(r)
                await asyncio.sleep(random.uniform(0.2, jitter))
    verdicts = {t: classify(a) for t, a in results.items()}
    # overall: worst-case — if primary target blocked, IP is no use
    primary = verdicts[targets[0]]
    all_codes = [a["status"] for a in results[targets[0]]]
    row = {
        "ip": px["ip"], "port": px["port"],
        "verdict_primary": primary,
        "verdicts": {t.split("/")[-1] or "event": v for t, v in verdicts.items()},
        "statuses_primary": all_codes,
        "latency_ms_avg": int(sum(a["latency_ms"] for a in results[targets[0]]) / max(1, repeats)),
        "sample_errors": [a["error"] for a in results[targets[0]] if a["error"]][:2],
    }
    return row


async def run_audit(proxy_file: str, targets: List[str], repeats: int,
                    concurrency: int, timeout_s: float, jitter: float) -> Dict[str, Any]:
    lines = Path(proxy_file).read_text().splitlines()
    proxies = [p for p in (parse_proxy_line(l) for l in lines) if p]
    print(f"[audit] {len(proxies)} proxies | targets={len(targets)} | repeats={repeats} | concurrency={concurrency}")

    sem = asyncio.Semaphore(concurrency)
    tasks = [test_proxy(px, targets, repeats, timeout_s, sem, jitter) for px in proxies]
    rows: List[Dict[str, Any]] = []
    t0 = time.time()
    for i, fut in enumerate(asyncio.as_completed(tasks), 1):
        row = await fut
        rows.append(row)
        print(f"  [{i}/{len(proxies)}] {row['ip']} → {row['verdict_primary']} {row['statuses_primary']}")

    summary: Dict[str, int] = {}
    for r in rows:
        summary[r["verdict_primary"]] = summary.get(r["verdict_primary"], 0) + 1

    artifact = {
        "task": "proxy_pool_audit",
        "started_utc": datetime.fromtimestamp(t0, tz=timezone.utc).isoformat(),
        "duration_s": round(time.time() - t0, 1),
        "n_proxies": len(proxies),
        "targets": targets,
        "repeats": repeats,
        "summary": summary,
        "rows": sorted(rows, key=lambda r: (r["verdict_primary"], r["ip"])),
        "note": "credentials intentionally excluded from artifact",
    }
    return artifact, rows, proxies


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", required=True)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--concurrency", type=int, default=10)
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--jitter", type=float, default=1.0)
    ap.add_argument("--targets", nargs="*", default=DEFAULT_TARGETS)
    args = ap.parse_args()

    artifact, rows, proxies = asyncio.run(
        run_audit(args.proxy_file, args.targets, args.repeats,
                  args.concurrency, args.timeout, args.jitter))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    art_path = OUT_DIR / f"proxy_audit_{ts}.json"
    art_path.write_text(json.dumps(artifact, indent=2, ensure_ascii=False))

    # Good list with credentials — operational file; MUST stay out of git.
    good_ips = {r["ip"] for r in rows if r["verdict_primary"] == "GOOD"}
    good_path = OUT_DIR / f"good_proxies_{ts}.txt"
    with good_path.open("w") as f:
        for px in proxies:
            if px["ip"] in good_ips:
                f.write(f"{px['ip']}:{px['port']}:{px['user']}:{px['pw']}\n")

    blocked = [r["ip"] for r in rows if r["verdict_primary"] == "BLOCKED"]
    print("\n===== SUMMARY =====")
    for k, v in sorted(artifact["summary"].items()):
        print(f"  {k}: {v}")
    print(f"  GOOD list ({len(good_ips)}): {good_path}")
    print(f"  artifact: {art_path}")
    print(f"  GOOD IPs: {sorted(good_ips)}")
    print(f"  BLOCKED IPs: {blocked}")


if __name__ == "__main__":
    main()
