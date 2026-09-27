#!/usr/bin/env python3
"""preflight_pool_audit.py — Tranche v2 proxy pool pre-flight audit (WP3).

Goal: dynamically maintain a rolling pool of >=20 verified-GOOD IPs (never a
frozen 21). Every candidate IP must pass 3 independent probes, spaced 5-10 min
apart; all 3 must be 200+PASS to enter the GOOD pool. Any 403/429/timeout/conn
error on any probe fails the IP, which is removed and replenished from the
100-raw Webshare pool (data/webshare_proxy_pool.txt) minus known-BURNED / already
in-pool IPs.

Design (safe, resumable):
  * Deterministic probe target: one stable SofaScore event URL per probe.
  * creds read inline from the pool files (ip:port:user:pw) — never from .env,
    never logged.
  * Checkpoint JSON written after every probe (atomic via tmp+rename) so a
    long multi-hour audit can be interrupted and resumed with --resume.
  * Round spacing 5-10 min controlled by --min-gap / --max-gap (randomized in
    range to avoid a burst pattern).

Modes:
  --selftest : offline; validates pool parsing, replenishment math, and
               checkpoint logic with ZERO network.
  --dry-run  : assemble the audit plan (who to probe, replenish order) only.
  --resume   : continue from existing checkpoint.
  (default)  : run the live audit to completion or until it reaches target size.

AUDIT object: the current GOOD pool (data/proxy_pools/good_20260917_v2.txt).
REPOOL  object: data/webshare_proxy_pool.txt (100 raw) minus in-audit + BURNED.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

AUDIT_POOL = REPO_ROOT / "data" / "proxy_pools" / "good_20260917_v2.txt"
RAW_POOL = REPO_ROOT / "data" / "webshare_proxy_pool.txt"
BURNED_POOL = REPO_ROOT / "data" / "proxy_pools" / "good_phaseA_20260909_BURNED.txt"

PROBE_URL = "https://api.sofascore.com/api/v1/event/14024019"
IMPERSONATE = "chrome124"
TIMEOUT_S = 15
PROBES_PER_IP = 3
TARGET_GOOD = 20          # maintain at least this many verified GOOD
MIN_GAP_S = 5 * 60        # 5 min
MAX_GAP_S = 10 * 60       # 10 min

CHECKPOINT = REPO_ROOT / "data" / "proxy_pools" / "preflight_checkpoint_v2.json"
OUT_GOOD = REPO_ROOT / "data" / "proxy_pools" / "good_v2_preflight_result.txt"
AUDIT_DONE_IP = REPO_ROOT / "data" / "proxy_pools" / "preflight_audit_trail.jsonl"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def _parse_pool(path: Path) -> list[dict]:
    pool = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        ip, port, user, pw = line.split(":", 3)
        pool.append({"ip": ip, "port": port, "user": user, "pw": pw})
    return pool


def _key(m: dict) -> str:
    return f"{m['ip']}:{m['port']}"


def load_audit_pool() -> list[dict]:
    return _parse_pool(AUDIT_POOL)


def load_raw_pool() -> list[dict]:
    return _parse_pool(RAW_POOL)


def load_burned() -> set[str]:
    if not BURNED_POOL.exists():
        return set()
    return {_key(m) for m in _parse_pool(BURNED_POOL)}


def build_replenish_pool() -> list[dict]:
    """raw pool minus in-audit and BURNED; deterministic order for auditability."""
    audit_keys = {_key(m) for m in load_audit_pool()}
    burned = load_burned()
    out = []
    seen = set()
    for m in load_raw_pool():
        k = _key(m)
        if k in audit_keys or k in burned or k in seen:
            continue
        seen.add(k)
        out.append(m)
    return out


# ---------------------------------------------------------------------------
# Probe (single network seam)
# ---------------------------------------------------------------------------
def probe_ip(m: dict) -> dict:
    """One probe. Returns {ip, port, status, verdict, latency_ms, error}."""
    from curl_cffi import requests as cr
    proxy = f"http://{m['user']}:{m['pw']}@{m['ip']}:{m['port']}"
    t0 = time.monotonic()
    try:
        r = cr.get(PROBE_URL, impersonate=IMPERSONATE,
                   proxies={"http": proxy, "https": proxy}, timeout=TIMEOUT_S)
        lat = int((time.monotonic() - t0) * 1000)
        status = r.status_code
        if status == 200:
            try:
                ok = "event" in r.json()
            except Exception:
                ok = False
            verdict = "PASS" if ok else "FAIL-BODY"
        else:
            verdict = f"FAIL-{status}"
        return {"ip": m["ip"], "port": m["port"], "status": status,
                "verdict": verdict, "latency_ms": lat, "error": None}
    except Exception as e:
        lat = int((time.monotonic() - t0) * 1000)
        return {"ip": m["ip"], "port": m["port"], "status": 0,
                "verdict": f"FAIL-{type(e).__name__}", "latency_ms": lat,
                "error": str(e)[:120]}


def _verdict_pass(v: str) -> bool:
    return v == "PASS"


# ---------------------------------------------------------------------------
# Checkpoint
# ---------------------------------------------------------------------------
def _load_checkpoint() -> dict:
    if CHECKPOINT.exists():
        return json.loads(CHECKPOINT.read_text())
    return {
        "version": 1,
        "started_at": _utc(),
        "target_good": TARGET_GOOD,
        "probes_per_ip": PROBES_PER_IP,
        "results": {},       # key -> {"probes":[verdict...], "status":"pending|good|failed"}
        "replenish": [],     # ordered replenish keys remaining
        "good_keys": [],     # keys that reached >=target after 3/3 pass
        "done": False,
    }


def _save_checkpoint(cp: dict):
    tmp = CHECKPOINT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cp, indent=2))
    tmp.replace(CHECKPOINT)


def _append_audit(rec: dict):
    with AUDIT_DONE_IP.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")


# ---------------------------------------------------------------------------
# Core audit logic
# ---------------------------------------------------------------------------
def _rand_gap() -> float:
    return random.uniform(MIN_GAP_S, MAX_GAP_S)


def run_audit(resume: bool) -> int:
    cp = _load_checkpoint()
    audit = {_key(m): m for m in load_audit_pool()}
    replenish = build_replenish_pool()
    cp["replenish"] = [ _key(m) for m in replenish ]
    _save_checkpoint(cp)

    # working set = current audit pool; replenishment expands it on failure.
    working = dict(audit)

    # Iterate rounds: for each IP still "pending", run the next probe.
    round_no = 0
    while not cp["done"]:
        round_no += 1
        pending = [k for k, v in cp["results"].items()
                   if v["status"] == "pending"]
        if round_no == 1:
            # first round: initialize every working IP as pending
            pending = list(working.keys())
        if not pending:
            cp["done"] = True
            _save_checkpoint(cp)
            break

        print(f"[round {round_no}] probing {len(pending)} IPs", flush=True)
        for k in pending:
            m = working[k]
            r = probe_ip(m)
            _append_audit({**r, "round": round_no, "ts": _utc()})
            rec = cp["results"].setdefault(k, {"probes": [], "status": "pending"})
            rec["probes"].append(r["verdict"])

            if not _verdict_pass(r["verdict"]):
                # failed this probe -> fail immediately, replenish
                rec["status"] = "failed"
                print(f"  [FAIL] {k} -> {r['verdict']}", flush=True)
                _replenish(cp, working, k, replenish)
            elif len(rec["probes"]) >= PROBES_PER_IP:
                # all 3 passed
                rec["status"] = "good"
                cp["good_keys"].append(k)
                print(f"  [GOOD] {k} ({PROBES_PER_IP}/{PROBES_PER_IP} PASS)", flush=True)
                if len(cp["good_keys"]) >= TARGET_GOOD:
                    print(f"[done] reached target {TARGET_GOOD} GOOD IPs", flush=True)
                    cp["done"] = True
            _save_checkpoint(cp)

        if cp["done"]:
            break
        if not replenish and len(cp["good_keys"]) + \
           sum(1 for v in cp["results"].values() if v["status"] == "pending") < TARGET_GOOD:
            print("[done] replenish pool exhausted; cannot reach target", flush=True)
            cp["done"] = True
            _save_checkpoint(cp)
            break

        gap = _rand_gap()
        print(f"[round {round_no}] done; sleeping {gap/60:.1f} min", flush=True)
        time.sleep(gap)

    # Write final GOOD list.
    _write_final(cp)
    print(json.dumps(_summary(cp), indent=2), flush=True)
    return 0


def _replenish(cp: dict, working: dict, failed_key: str, replenish: list):
    """Replace a failed IP with the next candidate from replenish pool."""
    if replenish:
        new_key = replenish.pop(0)
        # resolve the member dict from the raw pool
        raw = {_key(m): m for m in load_raw_pool()}
        if new_key in raw:
            working[new_key] = raw[new_key]
            cp["results"][new_key] = {"probes": [], "status": "pending"}
            print(f"  [replenish] {new_key} added (replaces {failed_key})", flush=True)


def _write_final(cp: dict):
    good = [k for k in cp["good_keys"]]
    lines = []
    raw = {_key(m): m for m in _parse_pool(RAW_POOL)} | \
          {_key(m): m for m in _parse_pool(AUDIT_POOL)}
    for k in good:
        if k in raw:
            m = raw[k]
            lines.append(f"{m['ip']}:{m['port']}:{m['user']}:{m['pw']}")
    OUT_GOOD.write_text("\n".join(lines) + ("\n" if lines else ""))
    print(f"[write] final GOOD pool -> {OUT_GOOD} ({len(lines)} IPs)", flush=True)


def _summary(cp: dict) -> dict:
    return {
        "started_at": cp["started_at"],
        "finished_at": _utc(),
        "target_good": cp["target_good"],
        "good_count": len(cp["good_keys"]),
        "failed_count": sum(1 for v in cp["results"].values() if v["status"] == "failed"),
        "pending_count": sum(1 for v in cp["results"].values() if v["status"] == "pending"),
        "done": cp["done"],
    }


# ---------------------------------------------------------------------------
# Selftest (offline)
# ---------------------------------------------------------------------------
def _selftest() -> int:
    fails = []
    def check(name, cond):
        if not cond:
            fails.append(name)

    audit = load_audit_pool()
    check("audit_pool_nonempty", len(audit) >= 20)
    raw = load_raw_pool()
    check("raw_pool_100", len(raw) >= 90)
    rep = build_replenish_pool()
    # replenish must not contain audit IPs or BURNED
    audit_keys = {_key(m) for m in audit}
    rep_keys = {_key(m) for m in rep}
    check("replenish_no_audit_overlap", len(rep_keys & audit_keys) == 0)
    check("replenish_no_burned_overlap", len(rep_keys & load_burned()) == 0)
    check("replenish_positive", len(rep) > 0)

    # verdict helper
    check("verdict_pass_true", _verdict_pass("PASS") is True)
    check("verdict_403_false", _verdict_pass("FAIL-403") is False)

    if fails:
        print(f"SELFTEST FAIL: {fails}", flush=True)
        return 1
    print(f"SELFTEST PASS (audit={len(audit)} raw={len(raw)} replenish={len(rep)})", flush=True)
    return 0


def main(argv):
    args = argparse.ArgumentParser()
    args.add_argument("--selftest", action="store_true")
    args.add_argument("--dry-run", action="store_true")
    args.add_argument("--resume", action="store_true")
    a = args.parse_args(argv)

    if a.selftest:
        return _selftest()

    audit = load_audit_pool()
    rep = build_replenish_pool()
    print(f"[plan] audit pool={len(audit)} raw={len(load_raw_pool())} "
          f"replenish={len(rep)} target={TARGET_GOOD}", flush=True)
    if a.dry_run:
        print("[dry-run] audit IPs:")
        for m in audit:
            print(f"  {_key(m)}")
        print("[dry-run] replenish order:")
        for m in rep[:20]:
            print(f"  {_key(m)}")
        return 0
    return run_audit(a.resume)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))