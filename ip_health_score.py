#!/usr/bin/env python3
"""ip_health_score.py — Dynamic per-IP health scoring + quarantine (Tranche v2 proxy policy).

Replaces the plain round-robin IP selection in the production backfill with a
health-score-aware selector. The goal is to stop wasting fetch attempts on IPs
that SofaScore has already reputation-rejected (403) instead of waiting for a
slow burst-count to trip.

Semantics (budget = higher is *worse*; success lowers it, rejection raises it):

    success            score -= 1      (healthy traffic buys headroom)
    403 reputation-    score += 50     (strong penalty: IP likely burned)
      reject
    rest >= 30 min     score -= 20     (cooldown recovery, capped at floor)

Quarantine rules:
    * When an IP's score reaches QUARANTINE_THRESHOLD it is quarantined.
    * Event-based override: 2 *consecutive* 403s quarantine immediately,
      regardless of the numeric score (no need to wait for burst count).
    * After a 403 quarantine, the IP must sit out a minimum cooldown
      (QUARANTINE_MIN_COOLDOWN_S = 6h) before re-entering scoring, to avoid a
      30-min "rest" recovery flapping the IP back into rotation too quickly.

Every outcome is appended to a JSONL audit trail for replay/forensics.

This module is pure state + logic and has ZERO network/DB side effects. It is
imported by the production backfill via a thin adapter; tests run with
--selftest (offline).

Protected-file note: this module does not modify gen4_fetcher.py,
fixed_pool_rotator.py, or backfill_runner.py. It is consumed as a library.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Tunables (documented; adjust only with approval — these encode proxy policy)
# ---------------------------------------------------------------------------
SUCCESS_DEC = 1        # score -= on success
REJECT_403_PENALTY = 50   # score += on a 403 reputation reject
REST_RECOVERY = 20        # score -= after >= REST_MIN_S rest
REST_MIN_S = 30 * 60      # 30 min rest qualifies for recovery

QUARANTINE_THRESHOLD = 100   # numeric score at/above which an IP is quarantined
CONSEC_403_OVERRIDE = 2      # event-based: this many consecutive 403s forces quarantine
QUARANTINE_MIN_COOLDOWN_S = 6 * 60 * 60  # 6h min before re-entering scoring after a 403 ban

FLOOR = -10**9              # score never below this (prevents unbounded negative drift)

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
_LOCK = threading.RLock()  # re-entrant: pick_ip -> is_available both take the lock


class IpHealthState:
    """Per-IP mutable health state, guarded by a module-level lock."""

    __slots__ = ("ip", "score", "consec_403", "last_outcome_ts", "last_403_ts",
                 "quarantined", "quarantine_until")

    def __init__(self, ip: str):
        self.ip = ip
        self.score = 0
        self.consec_403 = 0
        self.last_outcome_ts = 0.0
        self.last_403_ts = 0.0
        self.quarantined = False
        self.quarantine_until = 0.0


_registry: dict[str, IpHealthState] = {}


def _state(ip: str) -> IpHealthState:
    st = _registry.get(ip)
    if st is None:
        st = IpHealthState(ip)
        _registry[ip] = st
    return st


def _now() -> float:
    return time.time()


def _utc_iso(ts: float | None = None) -> str:
    if ts is None:
        ts = _now()
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Outcome recording
# ---------------------------------------------------------------------------
def record_outcome(ip: str, outcome: str, *, path: str | None = None,
                   extra: dict | None = None, audit_dir: str | Path | None = None) -> dict:
    """Record one fetch outcome for `ip` and return the resulting decision.

    outcome  : one of "success" | "403" | "not_found" | "fail"
    path     : optional endpoout-path/event label for the audit line
    extra    : optional dict folded into the audit line (http code, tries, error)
    audit_dir: optional JSONL dir; default from env IP_HEALTH_AUDIT_DIR or cwd.

    Returns a decision dict:
        {"ip", "outcome", "score", "consec_403", "quarantined", "cooling_down",
         "quarantine_until_utc"}
    """
    with _LOCK:
        st = _state(ip)
        now = _now()

        # Apply cooldown recovery before this outcome is scored.
        if not st.quarantined:
            if st.last_outcome_ts and (now - st.last_outcome_ts) >= REST_MIN_S:
                st.score = max(FLOOR, st.score - REST_RECOVERY)

        # Score this outcome.
        if outcome == "success":
            st.score = max(FLOOR, st.score - SUCCESS_DEC)
            st.consec_403 = 0
        elif outcome == "403":
            st.score += REJECT_403_PENALTY
            st.consec_403 += 1
            st.last_403_ts = now
        else:
            # not_found / fail: neutral for reputation, but still marks activity.
            st.consec_403 = 0

        st.last_outcome_ts = now

        # Decide quarantine.
        if not st.quarantined:
            should_q = (st.score >= QUARANTINE_THRESHOLD) or \
                       (st.consec_403 >= CONSEC_403_OVERRIDE)
            if should_q:
                st.quarantined = True
                st.quarantine_until = now + QUARANTINE_MIN_COOLDOWN_S

        # Cooldown release check (for later inquiries).
        cooling = st.quarantined and now < st.quarantine_until
        if st.quarantined and now >= st.quarantine_until:
            # released back into scoring; reset the consecutive-403 streak
            st.quarantined = False
            st.consec_403 = 0
            cooling = False

        decision = {
            "ip": ip,
            "outcome": outcome,
            "score": st.score,
            "consec_403": st.consec_403,
            "quarantined": st.quarantined,
            "cooling_down": cooling,
            "quarantine_until_utc": _utc_iso(st.quarantine_until) if st.quarantined else None,
        }

        _write_audit(decision, path=path, extra=extra, audit_dir=audit_dir)
        return decision


def _write_audit(decision: dict, *, path: str | None, extra: dict | None,
                 audit_dir: str | Path | None):
    line = {
        "ts": _utc_iso(),
        **decision,
    }
    if path:
        line["path"] = path
    if extra:
        line.update(extra)
    d = _resolve_audit_dir(audit_dir)
    if d is not None:
        try:
            Path(d).mkdir(parents=True, exist_ok=True)
            with (Path(d) / "ip_health.jsonl").open("a") as fh:
                fh.write(json.dumps(line, sort_keys=True) + "\n")
        except Exception:
            pass  # audit must never break the fetch path


def _resolve_audit_dir(audit_dir: str | Path | None) -> str | None:
    if audit_dir is not None:
        return str(audit_dir)
    env = os.environ.get("IP_HEALTH_AUDIT_DIR")
    if env:
        return env
    default = os.environ.get("IP_HEALTH_AUDIT_DEFAULT", "")
    if default:
        return default
    return None


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------
def is_available(ip: str, *, release_stale: bool = False) -> bool:
    """True if `ip` is not currently quarantined (or cooldown expired)."""
    with _LOCK:
        st = _state(ip)
        if not st.quarantined:
            return True
        now = _now()
        if now >= st.quarantine_until:
            if release_stale:
                st.quarantined = False
                st.consec_403 = 0
            return True
        return False


def pick_ip(pool: list[dict], *, cache_dirty=False) -> dict | None:
    """Health-aware replacement for round-robin.

    Returns the first available (non-quarantined) IP. Falls back to the least
    recently quarantined IP only if every member is quarantined (never blocks
    the pipeline entirely — the caller owns the abort policy).
    """
    with _LOCK:
        # Prefer available, preserving deterministic order for auditability.
        for m in pool:
            if is_available(m["ip"]):
                return m
        # All quarantined: pick the one whose cooldown ends soonest.
        best = None
        best_until = None
        for m in pool:
            st = _state(m["ip"])
            until = st.quarantine_until if st.quarantined else 0.0
            if best is None or until < best_until:
                best = m
                best_until = until
        return best


def snapshot() -> dict:
    """Full registry snapshot for diagnostics/reporting (no side effects)."""
    with _LOCK:
        out = []
        for ip, st in _registry.items():
            now = _now()
            out.append({
                "ip": ip,
                "score": st.score,
                "consec_403": st.consec_403,
                "quarantined": st.quarantined,
                "cooling_down": st.quarantined and now < st.quarantine_until,
                "last_outcome_ts": _utc_iso(st.last_outcome_ts) if st.last_outcome_ts else None,
                "last_403_ts": _utc_iso(st.last_403_ts) if st.last_403_ts else None,
                "quarantine_until_utc": _utc_iso(st.quarantine_until) if st.quarantined else None,
            })
        return {"ips": out, "counts": {
            "total": len(out),
            "quarantined": sum(1 for i in out if i["quarantined"]),
            "cooling": sum(1 for i in out if i["cooling_down"]),
        }}


def reset() -> None:
    """Clear all in-memory state (used by tests)."""
    with _LOCK:
        _registry.clear()


# ---------------------------------------------------------------------------
# Selftest (offline, no network)
# ---------------------------------------------------------------------------
def _selftest() -> int:
    reset()
    fails = []

    def check(name, cond):
        if not cond:
            fails.append(name)

    # success lowers score
    d = record_outcome("ip1", "success")
    check("success_score_neg", d["score"] == -1)
    check("success_not_quarantined", d["quarantined"] is False)

    # 403 raises score strongly
    d = record_outcome("ip2", "403")
    check("403_score_50", d["score"] == 50)
    check("403_not_yet_quarantined", d["quarantined"] is False)

    # 2 consecutive 403 -> event-based override quarantine
    d = record_outcome("ip2", "403")
    check("2x403_override_quarantine", d["quarantined"] is True)
    check("2x403_consec2", d["consec_403"] == 2)

    # quarantined IP not available
    check("quarantined_unavailable", is_available("ip2") is False)

    # pick_ip skips quarantined
    pool = [{"ip": "ip2", "port": "1", "user": "u", "pw": "p"},
            {"ip": "ip3", "port": "1", "user": "u", "pw": "p"}]
    pick = pick_ip(pool)
    check("pick_skips_quarantined", pick["ip"] == "ip3")

    # single 403 on a fresh IP below threshold -> available
    d = record_outcome("ip4", "403")
    check("1x403_still_available", is_available("ip4") is True and d["quarantined"] is False)

    # threshold reached via score (two 403 = +100) also quarantines
    d = record_outcome("ip5", "403")
    d = record_outcome("ip5", "403")
    check("threshold_quarantine", d["quarantined"] is True)

    if fails:
        print(f"SELFTEST FAIL: {fails}", flush=True)
        return 1
    print("SELFTEST PASS", flush=True)
    return 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    # Default: print a state snapshot (for manual inspection).
    print(json.dumps(snapshot(), indent=2))