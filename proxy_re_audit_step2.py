#!/usr/bin/env python3
"""proxy_re_audit_step2.py — Phase 10.5 Step 2: IP pool re-audit post-rotation.

Purpose: After Kris rotated the Webshare password (host-side, .env mtime
2026-08-28 05:09), re-probe the proxy pool with the NEW password to find
clean IPs for the Phase 10.5 per-competition sticky assignment.

Method (per Duncan dispatch 2026-08-28 05:19):
  - Source list: 100 Webshare static IPs (Webshare_100_proxies_8 file)
  - Per IP: exactly 1 GET https://www.sofascore.com/api/v1/event/14025013
    (10s timeout, concurrency 10)
  - Password: NEW one from .env via dotenv injection (SOFA_PROXY_PASS);
    the credential in the source file is the OLD password and is IGNORED.
  - Verdict: CLEAN(200+payload) / STALE(403) / BROKEN(407/timeout/network)
  - Drift vs data/proxy_audit/proxy_audit_20260827_192348.json

Security:
  - Password never echoed/logged/written to artifact.
  - Artifact contains ONLY ip:port + status codes + latencies.
  - No changes to .env, no protected files touched.

Usage:
  PYTHONUNBUFFERED=1 .runner-venv/bin/python proxy_re_audit_step2.py
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent
OUT_DIR = REPO_ROOT / "data" / "proxy_audit"
SRC_LIST = Path("/root/.openclaw/media/inbound/Webshare_100_proxies_8---95ae8c18-b195-49a3-99df-96c7897c2184.txt")
OLD_AUDIT = REPO_ROOT / "data" / "proxy_audit" / "proxy_audit_20260827_192348.json"
TARGET = "https://www.sofascore.com/api/v1/event/14025013"
TIMEOUT_S = 10
CONCURRENCY = 10


def load_now() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return ts


def load_env_password() -> str:
    """Inject .env SOFA_PROXY_PASS into environ (never print/return-free via caller)."""
    env_path = REPO_ROOT / ".env"
    pw = None
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path)
        pw = os.environ.get("SOFA_PROXY_PASS")
    except Exception:
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("SOFA_PROXY_PASS="):
                pw = line.split("=", 1)[1].strip()
                break
    if not pw:
        raise RuntimeError("SOFA_PROXY_PASS not found in .env")
    os.environ["SOFA_PROXY_PASS"] = pw
    return pw


def parse_ip_port(line: str) -> Optional[Dict[str, str]]:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = line.split(":")
    if len(parts) < 2:
        return None
    return {"ip": parts[0], "port": parts[1]}


def probe_one(ip: str, port: str, user: str, pw: str) -> Dict[str, Any]:
    from curl_cffi import requests as cr
    proxy_url = f"http://{user}:{pw}@{ip}:{port}"
    t0 = time.monotonic()
    try:
        r = cr.get(TARGET,
                   proxies={"http": proxy_url, "https": proxy_url},
                   impersonate="chrome", timeout=TIMEOUT_S)
        latency = int((time.monotonic() - t0) * 1000)
        status = r.status_code
        payload_ok = False
        if status == 200:
            try:
                body = r.json()
                payload_ok = isinstance(body, dict) and bool(body.get("event"))
            except Exception:
                payload_ok = False
        return {"status": status, "latency_ms": latency,
                "payload_ok": payload_ok, "error": None}
    except Exception as e:
        return {"status": 0, "latency_ms": int((time.monotonic() - t0) * 1000),
                "payload_ok": False, "error": f"{type(e).__name__}"}


def classify(r: Dict[str, Any]) -> str:
    if r["status"] == 200 and r["payload_ok"]:
        return "CLEAN"
    if r["status"] == 403:
        return "STALE"
    if r["status"] == 407:
        return "BROKEN"  # proxy auth fail
    if r["status"] == 0:
        return "BROKEN"
    return "BROKEN"


async def main() -> None:
    pw = load_env_password()
    new_user = os.environ.get("SOFA_PROXY_USER", "***REMOVED***")

    entries: List[Dict[str, str]] = []
    for line in SRC_LIST.read_text().splitlines():
        p = parse_ip_port(line)
        if p:
            entries.append(p)
    print(f"[re-audit] {len(entries)} IPs loaded from source list", flush=True)

    rows: List[Dict[str, Any]] = []
    loop = asyncio.get_event_loop()

    def _probe(entry: Dict[str, str]) -> Dict[str, Any]:
        r = probe_one(entry["ip"], entry["port"], new_user, pw)
        verdict = classify(r)
        return {"ip": entry["ip"], "port": entry["port"],
                "status": r["status"], "latency_ms": r["latency_ms"],
                "verdict": verdict,
                "error": r["error"]}

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = [loop.run_in_executor(ex, _probe, e) for e in entries]
        for i, fut in enumerate(asyncio.as_completed(futs), 1):
            row = await fut
            rows.append(row)
            if i % 10 == 0:
                print(f"[re-audit] {i}/{len(entries)} done", flush=True)

    n_clean = sum(1 for r in rows if r["verdict"] == "CLEAN")
    n_stale = sum(1 for r in rows if r["verdict"] == "STALE")
    n_broken = sum(1 for r in rows if r["verdict"] == "BROKEN")

    # Drift vs old audit
    old = json.loads(OLD_AUDIT.read_text())
    old_map = {f"{r['ip']}:{r['port']}": r["verdict_primary"] for r in old["rows"]}
    clean_to_stale = stale_to_clean = broken_count = 0
    for r in rows:
        key = f"{r['ip']}:{r['port']}"
        old_v = old_map.get(key)
        if old_v == "GOOD" and r["verdict"] in ("STALE", "BROKEN"):
            clean_to_stale += 1
        if old_v == "BLOCKED" and r["verdict"] == "CLEAN":
            stale_to_clean += 1
        if r["verdict"] == "BROKEN":
            broken_count += 1

    clean_ips = [r for r in rows if r["verdict"] == "CLEAN"]
    clean_ips.sort(key=lambda r: r["latency_ms"])

    artifact = {
        "task": "phase10_5_step2_re_audit",
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "n_proxies": len(rows),
        "target": TARGET,
        "repeats": 1,
        "timeout_s": TIMEOUT_S,
        "concurrency": CONCURRENCY,
        "password_status": "rotated_fresh",
        "summary": {"CLEAN": n_clean, "STALE": n_stale, "BROKEN": n_broken},
        "drift_vs_phase10": {
            "clean_to_stale_count": clean_to_stale,
            "stale_to_clean_count": stale_to_clean,
            "broken_count": broken_count,
        },
        "rows": rows,
        "note": "Single-shot probe per IP with new Webshare password (post-rotation). "
                "No credentials recorded. Source list: Webshare_100_proxies_8 "
                "(filename suffix 95ae8c18).",
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = load_now()
    audit_path = OUT_DIR / f"proxy_re_audit_{ts}.json"
    audit_path.write_text(json.dumps(artifact, indent=2, ensure_ascii=False))

    # Recommended Phase 10.5 pool (top clean IPs by latency)
    assign = {
        "generated_utc": audit["started_utc"],
        "audit_file": str(audit_path.relative_to(REPO_ROOT)),
        "pool_size": len(clean_ips),
        "recommended_pool": [
            {"ip": r["ip"], "port": r["port"], "latency_ms": r["latency_ms"]}
            for r in clean_ips
        ],
        "note": "Phase 10.5 per-competition sticky assignment source. "
                "Compose per-comp index at canary run-time via plan §4 hash; "
                "13 comps with pool_size IPs. All creds from .env at runtime.",
    }
    assign_path = OUT_DIR / f"ip_assignment_{ts}.json"
    assign_path.write_text(json.dumps(assign, indent=2, ensure_ascii=False))

    print(f"[re-audit] CLEAN={n_clean} STALE={n_stale} BROKEN={n_broken}", flush=True)
    print(f"[re-audit] drift clean->stale={clean_to_stale} stale->clean={stale_to_clean}", flush=True)
    print(f"[re-audit] artifacts: {audit_path} / {assign_path}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
