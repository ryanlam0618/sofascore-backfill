#!/usr/bin/env python3
"""proxy_re_audit_v3_20260906.py — Credential-matrix proxy re-audit (v3, 2026-09-06).

Method (mirrors v2 proxy-pool-reaudit-v2-20260903):
  - Candidate IP universe = union of all known Webshare static lists
      proxy_list.txt, data/webshare_proxy_pool.txt,
      data/proxy_pools/good_21.txt, data/proxy_pools/good_refreshed_v2.txt
    deduped by ip:port.
  - For each IP:port probe TWICE — once with cred_main (***REMOVED***),
    once with cred_rotate (***REMOVED***-rotate). Password read from .env at runtime.
  - Probe target: https://www.sofascore.com/api/v1/event/14025013 (v2-consistent).
  - curl_cffi impersonate chrome, timeout 10s, concurrency 10, min spacing to
    avoid tripping Varnish (pacing ~ per-IP delay).
  - Verdict classes: 200=GOOD, 403=Varnish reputation ban, 407=proxy auth fail,
    0=timeout/network.

Security: password/cred comment never written to artifact. Artifact stores only
ip:port + per-cred status + latency.

Usage:
  PYTHONUNBUFFERED=1 .runner-venv/bin/python proxy_re_audit_v3_20260906.py
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent
OUT_DIR = REPO_ROOT / "data" / "proxy_audit"
OUT_DIR.mkdir(parents=True, exist_ok=True)

LIST_FILES = [
    REPO_ROOT / "proxy_list.txt",
    REPO_ROOT / "data" / "webshare_proxy_pool.txt",
    REPO_ROOT / "data" / "proxy_pools" / "good_21.txt",
    REPO_ROOT / "data" / "proxy_pools" / "good_refreshed_v2.txt",
]
TARGET = "https://www.sofascore.com/api/v1/event/14025013"
TIMEOUT_S = 10
CONCURRENCY = 10
MIN_GAP_S = 0.15  # pacing per probe


def load_env_password() -> Optional[str]:
    env_path = REPO_ROOT / ".env"
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path)
        return os.environ.get("SOFA_PROXY_PASS")
    except Exception:
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("SOFA_PROXY_PASS="):
                return line.split("=", 1)[1]
    return None


def load_candidates() -> List[Dict[str, str]]:
    seen: Dict[str, Dict[str, str]] = {}
    for f in LIST_FILES:
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(":")
            if len(parts) != 4:
                continue
            ip, port, user, _pw = parts
            key = f"{ip}:{port}"
            if key not in seen:
                seen[key] = {"ip": ip, "port": port, "user": user}
    return list(seen.values())


async def probe(sem: asyncio.Semaphore, proxy_url: str):
    from curl_cffi import requests as cr
    async with sem:
        await asyncio.sleep(MIN_GAP_S)
        t0 = time.monotonic()
        try:
            r = await asyncio.to_thread(
                cr.get, TARGET,
                proxies={"http": proxy_url, "https": proxy_url},
                impersonate="chrome", timeout=TIMEOUT_S)
            return {"status": r.status_code,
                    "latency_ms": int((time.monotonic() - t0) * 1000),
                    "error": None}
        except Exception as e:
            return {"status": 0,
                    "latency_ms": int((time.monotonic() - t0) * 1000),
                    "error": f"{type(e).__name__}"}


async def main():
    pw = load_env_password()
    if not pw:
        raise SystemExit("SOFA_PROXY_PASS not found in .env")
    cands = load_candidates()
    print(f"Candidates (deduped ip:port): {len(cands)}")

    creds = {
        "cred_main": ("***REMOVED***", pw),
        "cred_rotate": ("***REMOVED***-rotate", pw),
    }
    sem = asyncio.Semaphore(CONCURRENCY)

    results = []
    t_all0 = time.monotonic()
    for c in cands:
        row = {"ip": c["ip"], "port": c["port"], "source_user": c["user"],
               "probes": {}}
        for cred, (user, pwp) in creds.items():
            purl = f"http://{user}:{pwp}@{c['ip']}:{c['port']}"
            res = await probe(sem, purl)
            row["probes"][cred] = res
        results.append(row)
        ok_main = row["probes"]["cred_main"]["status"]
        ok_rot = row["probes"]["cred_rotate"]["status"]
        print(f"[{len(results)}/{len(cands)}] {c['ip']}:{c['port']} "
              f"main={ok_main} rotate={ok_rot}")

    elapsed = int(time.monotonic() - t_all0)
    good_main = [r for r in results if r["probes"]["cred_main"]["status"] == 200]
    good_rot = [r for r in results if r["probes"]["cred_rotate"]["status"] == 200]

    out = {
        "probe": {"target": TARGET, "timeout_s": TIMEOUT_S,
                   "concurrency": CONCURRENCY, "min_gap_s": MIN_GAP_S,
                   "elapsed_s": elapsed,
                   "total_candidates": len(cands)},
        "counts": {
            "candidates": len(cands),
            "good_cred_main": len(good_main),
            "good_cred_rotate": len(good_rot),
            "good_either": len({r["ip"]+":"+r["port"] for r in good_main + good_rot}),
        },
        "results": results,
    }
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = OUT_DIR / f"reaudit_v3_20260906_{ts}.json"
    out_path.write_text(json.dumps(out, indent=1))
    print(f"\nWrote {out_path}")
    print(f"GOOD cred_main={len(good_main)} | GOOD cred_rotate={len(good_rot)}"
          f" | either={out['counts']['good_either']} | elapsed={elapsed}s")


if __name__ == "__main__":
    asyncio.run(main())