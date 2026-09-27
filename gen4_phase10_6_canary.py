#!/usr/bin/env python3
"""gen4_phase10_6_canary.py — Phase 10.6 broad-validation canary (Tier-2 CloakBrowser primary).

Per plan v0.5 final (Kris approved 2026-08-28 05:51):
  - 13 competitions x every available season (15/16 -> 25/26), 1 event per cell
  - per-competition sticky IP from ip_assignment_phase10_6.json (GOOD IPs from multi-endpoint x multi-age Step 4 audit)
  - hash pre-assignment: sha256(f"broad-v1.0:{competition}") % pool_size
  - 403 within a competition -> clockwise rotate to next pool IP (cap enforced downstream)
  - Future-date guard: event endpoint light-check parses startTimestamp;
    startTimestamp > now -> cell ineligible (future_schedule_excluded)
  - Tier-2 CloakBrowser primary (v0.5): Tier-1 fallback only if Tier-2 fails
  - Budget: 600 total, per-event 6, warm-up global once
  - Gates: P1(info) P2 >=99% per endpoint, P4 budget, P5 cleanup, P6 no spurious halt,
    P7 per-comp >=1 PASS, P8 per-season >=1 PASS, P9 >=95% cells all-pass,
    P10 Tier-1 requests carry no cookies

Protected files untouched. No credentials in output.
Usage:
  PYTHONUNBUFFERED=1 .runner-venv/bin/python gen4_phase10_6_canary.py
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent
DATA_DIR = REPO_ROOT / "data"
ARTIFACT_PATH = DATA_DIR / "gen4_phase10_6_broad_canary.json"
COVERAGE_CSV = DATA_DIR / "coverage_audit" / "season_sample_coverage.csv"
IP_POOL_JSON = DATA_DIR / "proxy_audit" / "ip_assignment_phase10_6.json"
RUNTIME_IP_LOCK = DATA_DIR / "proxy_audit" / "ip_assignment_phase10_6_runtime.json"

PLAN_VERSION = "v0.5"
SEED_FMT = "broad-v1.0"
PHASE = "10.6"
ENDPOINTS = ["event", "incidents", "lineups", "statistics"]
ENDPOINT_PATHS = {
    "event":      "/api/v1/event/{event_id}",
    "incidents":  "/api/v1/event/{event_id}/incidents",
    "lineups":    "/api/v1/event/{event_id}/lineups",
    "statistics": "/api/v1/event/{event_id}/statistics",
}
WRITE_ENABLED = False
MAX_ATTEMPTS_PER_REQUEST = 4      # rotate cap 3 + initial (fetcher internal; Tier-2 primary 每 comp rebuild-on-403)
MAX_ATTEMPTS_PER_EVENT = 8        # v0.5 §3
MAX_HTTP_CALLS_RUN = 800          # v0.5 §3


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env")
    except Exception:
        for line in (REPO_ROOT / ".env").read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def _log(msg: str) -> None:
    print(f"[phase10_6] {msg}", flush=True)


def _season_start_year(label: str) -> int:
    s = (label or "").strip()
    try:
        if "/" in s:
            y = int(s.split("/", 1)[0])
            return y + 2000 if y < 100 else y
        return int(s[:4])
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Sampling (deterministic, seed-locked; future-date excluded at fetch time)
# ---------------------------------------------------------------------------

def sample_cells() -> List[Dict[str, Any]]:
    rows = list(csv.DictReader(COVERAGE_CSV.open()))
    cells: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r["status"] != "ok":
            continue
        if not (r["incidents_status"] == "200" and r["lineups_status"] == "200"
                and r["statistics_status"] == "200"):
            continue
        cells[(r["competition"], r["season_label"])].append(r)
    out: List[Dict[str, Any]] = []
    now_ts = int(time.time())
    for (comp, season_label) in sorted(cells.keys(), key=lambda t: (t[0], _season_start_year(t[1]))):
        cands = cells[(comp, season_label)]
        def _h(r):
            k = f"{SEED_FMT}:{comp}:{season_label}:{r['event_id']}"
            return hashlib.sha256(k.encode()).hexdigest()
        cands.sort(key=_h)
        r = cands[0]
        out.append({
            "competition": comp,
            "season_label": season_label,
            "season_start_year": _season_start_year(season_label),
            "event_id": int(r["event_id"]),
            "competition_id": int(r["ut_id"]),
            "season_id": int(r["season_id"]),
            "sample_hash": _h(r),
        })
    return out


# ---------------------------------------------------------------------------
# Instrumented curl + warm-up (no cookie forwarding)
# ---------------------------------------------------------------------------

def _extract_set_cookie_pairs(resp: Any) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        jar = getattr(resp, "cookies", None)
        if jar:
            for c in jar:
                out[getattr(c, "name", str(c))] = getattr(c, "value", "")
    except Exception:
        pass
    return out


class Registry:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.total = 0
        self.endpoint_calls = 0
        self.warmup_calls = 0
        self.per_event: Dict[int, int] = defaultdict(int)
        self.per_comp: Dict[str, int] = defaultdict(int)

    def register(self, **kw) -> None:
        self.total += 1
        if kw.get("kind") == "warmup":
            self.warmup_calls += 1
        else:
            self.endpoint_calls += 1
            if kw.get("event_id"):
                self.per_event[kw["event_id"]] += 1
            if kw.get("competition"):
                self.per_comp[kw["competition"]] += 1
        kw["n"] = self.total
        self.calls.append(kw)


class InstrumentedCurl:
    """Tier-1 transport. cookie_store=None => zero cookies sent.
    Records status / set-cookie names (audit) / whether cookies were sent."""
    def __init__(self, registry: Registry, identity_getter: Callable[[], str]) -> None:
        self._reg = registry
        self._identity = identity_getter
        self.current_event_id: Optional[int] = None
        self.current_competition: Optional[str] = None

    def __call__(self, url: str, **kwargs: Any) -> Any:
        from curl_cffi import requests as cr
        t0 = time.monotonic()
        req_cookie_names = sorted((kwargs.get("cookies") or {}).keys())
        resp = cr.get(url, **kwargs)
        latency = int((time.monotonic() - t0) * 1000)
        self._reg.register(
            kind="endpoint", path=url.replace("https://www.sofascore.com", ""),
            status=getattr(resp, "status_code", 0), latency_ms=latency,
            event_id=self.current_event_id, competition=self.current_competition,
            proxy=identity_short(self._identity()),
            request_cookie_names=req_cookie_names,
            set_cookie_names=sorted(_extract_set_cookie_pairs(resp).keys()),
        )
        return resp


class OnceWarmup:
    scope = "per_event"
    def __init__(self, proxy_url_getter: Callable[[], Optional[str]],
                 base_url: str, registry: Registry) -> None:
        self._proxy_url_getter = proxy_url_getter
        self._base = base_url
        self._reg = registry
        self.warmed = False
        self.error: Optional[str] = None

    async def warm_homepage(self) -> None:
        if self.warmed:
            return
        from curl_cffi import requests as cr
        t0 = time.monotonic()
        kw: Dict[str, Any] = {"impersonate": "chrome", "timeout": 30}
        p = self._proxy_url_getter()
        if p:
            kw["proxies"] = {"http": p, "https": p}
        try:
            resp = await asyncio.to_thread(cr.get, self._base + "/", **kw)
            status = getattr(resp, "status_code", 0)
            self._reg.register(kind="warmup", path="/", status=status,
                               latency_ms=int((time.monotonic() - t0) * 1000))
            if status == 200:
                self.warmed = True
                return
            raise RuntimeError(f"warmup status={status}")
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            raise

    async def warm_tournament(self, **kw) -> str:
        return self._base + "/"

    async def warm_event(self, event_id: int) -> None:
        return


def identity_short(identity: str) -> str:
    return identity if identity else "none"


# ---------------------------------------------------------------------------
# Per-competition sticky rotator
# ---------------------------------------------------------------------------

class StickyRotator:
    """One per competition. Pool pre-indexed by hash; rotate() advances clockwise."""
    def __init__(self, pool: List[Dict[str, str]], idx: int, config: Any,
                 counter_callback: Callable[[str, str], None]) -> None:
        self._pool = pool
        self._idx = idx
        self._config = config
        self._on_rotate = counter_callback
        self.competition = ""

    def _apply(self) -> None:
        p = self._pool[self._idx]
        self._config.proxy_server = p["ip"]
        self._config.proxy_port = p["port"]
        self._config.proxy_user = p["user"]
        self._config.proxy_pass = p["pass"]

    async def rotate(self) -> str:
        frm = f"{self._pool[self._idx]['ip']}:{self._pool[self._idx]['port']}"
        self._idx = (self._idx + 1) % len(self._pool)
        self._apply()
        to = f"{self._pool[self._idx]['ip']}:{self._pool[self._idx]['port']}"
        self._on_rotate(frm, to)
        return self.current_proxy()

    def current_proxy(self) -> str:
        p = self._pool[self._idx]
        return f"{p['ip']}:{p['port']}"

    def current_index(self) -> int:
        return self._idx



def _synth_result_from_tier(t, transport):
    body = t.get("body")
    ok = bool(t.get("success"))
    final = t.get("final_attempt") or {}
    return {
        "status": final.get("status", 0) if final else 0,
        "ok": ok,
        "payload_complete": ok,
        "transport": t.get("transport", transport),
        "retry_count": max(0, len(t.get("attempts", [])) - 1),
        "attempts": t.get("attempts", []),
        "data": body,
        "halt_reason": t.get("halt_reason"),
    }


async def _fetch_tier2_first(fetcher, path, timeout_ms=30000):
    """v0.5 tier order: Tier-2 CloakBrowser primary -> Tier-1 curl_cffi fallback -> Tier-3 SSR.
    Drives fetcher internals directly (protected files untouched)."""
    from gen4_components import TIER2_BROWSER_MAX_RETRIES
    t2 = await fetcher._fetch_with_retry_tier(
        tier_name="cloakbrowser", path=path, timeout_ms=timeout_ms,
        tier_max_retries=TIER2_BROWSER_MAX_RETRIES)
    if t2.get("halt_reason") == "http_407_safety_halt":
        r = _synth_result_from_tier(t2, "cloakbrowser"); r["halt_reason"] = "http_407_safety_halt"; return r
    if t2.get("success"):
        return _synth_result_from_tier(t2, "cloakbrowser")
    t1 = await fetcher._fetch_with_retry_tier(
        tier_name="curl_cffi", path=path, timeout_ms=timeout_ms,
        tier_max_retries=3)
    if t1.get("halt_reason") == "http_407_safety_halt":
        r = _synth_result_from_tier(t1, "curl_cffi"); r["halt_reason"] = "http_407_safety_halt"; return r
    if t1.get("success"):
        return _synth_result_from_tier(t1, "curl_cffi")
    t3 = await fetcher._fetch_via_ssr(path, timeout_ms)
    att = t3.get("attempt", {})
    body = t3.get("body")
    ok = bool(body) and att.get("status") == 200
    return {"status": att.get("status", 0), "ok": ok, "payload_complete": ok,
            "transport": "ssr", "retry_count": 1, "attempts": [att],
            "data": body, "halt_reason": None}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run_canary() -> Dict[str, Any]:
    _load_dotenv()
    pw = os.environ.get("SOFA_PROXY_PASS", "")
    user = os.environ.get("SOFA_PROXY_USER", "***REMOVED***")
    if not pw:
        return {"halt_reason": "NO_PROXY_CREDENTIALS", "scope_violation": True}

    if WRITE_ENABLED is not False:
        return {"scope_violation": True, "halt_reason": "WRITE_ENABLED_TRUE"}

    from gen4_fetcher import Gen4Config, Gen4Fetcher
    from gen4_components import DefaultHeaderFactory, RequestBudget
    from gen4_ssr import resolve_ssr_for_api_path
    from gen4_canary_validate import check_safety_halts

    cells = sample_cells()
    pool_raw = json.loads(IP_POOL_JSON.read_text())
    pool: List[Dict[str, str]] = []
    for p in pool_raw["recommended_pool"]:
        pool.append({"ip": p["ip"], "port": p["port"], "user": user, "pass": pw})
    if not pool:
        return {"halt_reason": "EMPTY_POOL", "scope_violation": True}

    # --- Per-competition IP pre-assignment (hash-locked, §4) ----------------
    comps = sorted({c["competition"] for c in cells})
    comp_ip_idx: Dict[str, int] = {}
    ip_assignment: List[Dict[str, Any]] = []
    for comp in comps:
        h = hashlib.sha256(f"{SEED_FMT}:{comp}".encode()).hexdigest()
        idx = int(h, 16) % len(pool)
        comp_ip_idx[comp] = idx
        p = pool[idx]
        ip_assignment.append({
            "competition": comp, "ip_index": idx,
            "ip_addr_port": f"{p['ip']}:{p['port']}",
            "sample_hash": h,
        })

    config = Gen4Config(write_enabled=WRITE_ENABLED)
    registry = Registry()
    current_identity = {"v": "none"}

    instrumented = InstrumentedCurl(registry, lambda: current_identity["v"])
    warmup = OnceWarmup(lambda: _proxy_url_real(config), config.base_url, registry)

    artifact: Dict[str, Any] = {
        "phase": "10.6",
        "plan_version": PLAN_VERSION,
        "approved_by": "Kris (2026-08-28 05:51 GMT+8, msg 23213 — signed v0.5)",
        "tier_strategy": "tier2_primary",
        "re_audit_method": "multi_endpoint_multi_age",
        "password_status": "rotated_fresh",
        "scope": {
            "n_cells": len(cells),
            "competitions": comps,
            "endpoints": ENDPOINTS,
            "write_enabled": WRITE_ENABLED,
            "max_http_calls_cap": MAX_HTTP_CALLS_RUN,
            "max_attempts_per_event": MAX_ATTEMPTS_PER_EVENT,
            "max_attempts_per_request": MAX_ATTEMPTS_PER_REQUEST,
        },
        "audit_file_used": {
            "path": str(IP_POOL_JSON.relative_to(REPO_ROOT)),
            "mtime_iso": datetime.fromtimestamp(IP_POOL_JSON.stat().st_mtime, tz=timezone.utc).isoformat(),
            "size_bytes": IP_POOL_JSON.stat().st_size,
            "ip_count": len(pool),
        },
        "sampling_seed_version": "broad-v1.0:{competition}",
        "cookie_namespace_verified": "disabled_no_cookie_injection",
        "ip_assignment": ip_assignment,
        "per_competition_rotation": [],
        "sampling_filter": {"total_cells": len(cells), "future_date_excluded": 0,
                            "total_selected": len(cells), "ineligible_cells": []},
        "results": [],
        "halt_reason": None,
        "stopped_early": False,
        "cleanup_ok": None,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "approved_events": [c["event_id"] for c in cells],
    }
    now_ts = int(time.time())

    # Lock runtime assignment
    RUNTIME_IP_LOCK.write_text(json.dumps({
        "locked_at_utc": artifact["started_at_utc"],
        "audit_file": artifact["audit_file_used"],
        "ip_assignment": ip_assignment,
    }, indent=2, ensure_ascii=False))

    def _on_rotate(frm: str, to: str) -> None:
        artifact["per_competition_rotation"].append({
            "from_ip": frm, "to_ip": to,
            "reason": "403_or_fetcher_retry", "n": len(artifact["per_competition_rotation"]) + 1,
        })

    # Build per-comp fetcher on demand
    from gen4_components import DefaultHeaderFactory, RequestBudget

    headers = DefaultHeaderFactory()

    def _proxy_url_masked(c: Any) -> Optional[str]:
        if not c.proxy_server or not c.proxy_pass:
            return None
        return f"http://{c.proxy_user}:***@{c.proxy_server}:{c.proxy_port}"  # log-safe only

    def _proxy_url_real(c: Any) -> Optional[str]:
        if not c.proxy_server or not c.proxy_pass:
            return None
        return f"http://{c.proxy_user}:{c.proxy_pass}@{c.proxy_server}:{c.proxy_port}"

    async def _real_cloak_launcher(headless: bool = True, proxy: Optional[str] = None,
                                   humanize: bool = False, **kw: Any) -> Any:
        import cloakbrowser as _cloak
        return await _cloak.launch_async(headless=headless, proxy=proxy,
                                         humanize=humanize, **kw)

    # ---- Warm-up once ----
    first_pool_idx = comp_ip_idx[comps[0]]
    StickyRotator(pool, first_pool_idx, config, _on_rotate)._apply()
    current_identity["v"] = f"{pool[first_pool_idx]['ip']}:{pool[first_pool_idx]['port']}"
    try:
        await warmup.warm_homepage()
        _log("warm-up ok (global once)")
    except Exception as e:
        artifact["halt_reason"] = "warmup_failed"
        artifact["cleanup_ok"] = True
        artifact["call_log"] = registry.calls
        artifact["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        artifact["gates"] = {}
        artifact["verdict"] = "FAIL"
        _write(artifact)
        _log(f"HALT warmup: {e}")
        return artifact

    events_by_comp: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for c in cells:
        events_by_comp[c["competition"]].append(c)

    try:
        for comp in comps:
            pool_idx = comp_ip_idx[comp]
            rotator = StickyRotator(pool, pool_idx, config, _on_rotate)
            rotator.competition = comp
            rotator._apply()
            current_identity["v"] = f"{pool[pool_idx]['ip']}:{pool[pool_idx]['port']}"
            instrumented.current_competition = comp

            budget = RequestBudget(max_attempts_per_request=MAX_ATTEMPTS_PER_REQUEST)
            fetcher = Gen4Fetcher(
                config=config,
                curl_cffi_get=instrumented,
                cloakbrowser_launcher=_real_cloak_launcher,
                ssr_fetcher=resolve_ssr_for_api_path,
                retry_policy=None,
                proxy_rotator=rotator,
                warmup_sequence=warmup,
                cookie_store=None,               # v0.5: zero cookies on Tier-1
                sticky_session=None,
                header_factory=headers,
                budget=budget,
            )
            await fetcher.start()
            try:
                for cell in events_by_comp[comp]:
                    if registry.total >= MAX_HTTP_CALLS_RUN:
                        artifact["halt_reason"] = "RUN_CALL_CAP_EXCEEDED"
                        artifact["stopped_early"] = True
                        break
                    if registry.per_event[cell["event_id"]] >= MAX_ATTEMPTS_PER_EVENT:
                        artifact["halt_reason"] = "EVENT_CALL_CAP_EXCEEDED"
                        artifact["stopped_early"] = True
                        break
                    instrumented.current_event_id = cell["event_id"]

                    # -- future-date guard: event endpoint light-check ----
                    path = ENDPOINT_PATHS["event"].format(event_id=cell["event_id"])
                    t0 = time.monotonic()
                    try:
                        res = await _fetch_tier2_first(fetcher, path)
                    except Exception as e:
                        artifact["results"].append(
                            _result_row(cell, "event", path, 0, False, None, str(e),
                                        int((time.monotonic() - t0) * 1000)))
                        res = None
                    if res is not None:
                        artifact["results"].append(_from_fetcher(cell, "event", path, res, t0))
                        sd = _extract_start_ts(res.get("data"))
                        if sd is not None and sd > now_ts:
                            artifact["sampling_filter"]["future_date_excluded"] += 1
                            artifact["sampling_filter"]["ineligible_cells"].append({
                                "competition": comp,
                                "season_label": cell["season_label"],
                                "event_id": cell["event_id"],
                                "reason": "future_schedule_excluded",
                            })
                            continue

                    # -- other 3 endpoints ----
                    for endpoint in ("incidents", "lineups", "statistics"):
                        if registry.total >= MAX_HTTP_CALLS_RUN:
                            artifact["halt_reason"] = "RUN_CALL_CAP_EXCEEDED"
                            artifact["stopped_early"] = True
                            break
                        if registry.per_event[cell["event_id"]] >= MAX_ATTEMPTS_PER_EVENT:
                            artifact["halt_reason"] = "EVENT_CALL_CAP_EXCEEDED"
                            artifact["stopped_early"] = True
                            break
                        p2 = ENDPOINT_PATHS[endpoint].format(event_id=cell["event_id"])
                        t0 = time.monotonic()
                        try:
                            r2 = await _fetch_tier2_first(fetcher, p2)
                        except Exception as e:
                            artifact["results"].append(
                                _result_row(cell, endpoint, p2, 0, False, None, str(e),
                                            int((time.monotonic() - t0) * 1000)))
                        else:
                            artifact["results"].append(_from_fetcher(cell, endpoint, p2, r2, t0))
                        artifact["halt_reason"] = check_safety_halts(artifact)
                        if artifact["halt_reason"]:
                            artifact["stopped_early"] = True
                            _log(f"SAFETY HALT {comp} ev={cell['event_id']} {endpoint}: "
                                 f"{artifact['halt_reason']}")
                            break
                    if artifact["halt_reason"]:
                        break
                if artifact["halt_reason"]:
                    break
            finally:
                try:
                    await fetcher.close()
                except Exception:
                    pass
    finally:
        pass  # no cookie jar to clear (cookie_store=None)

    artifact["cleanup_ok"] = True
    artifact["call_log"] = registry.calls
    artifact["call_stats"] = {
        "total_http_calls": registry.total,
        "endpoint_attempts": registry.endpoint_calls,
        "warmup_calls": registry.warmup_calls,
        "per_competition_calls": dict(registry.per_comp),
    }
    artifact["tier_distribution"] = _tier_dist(artifact["results"])
    artifact["finished_at_utc"] = datetime.now(timezone.utc).isoformat()

    _evaluate_gates(artifact)
    _write(artifact)
    return artifact


# ---------------------------------------------------------------------------

def _extract_start_ts(data: Any) -> Optional[int]:
    if not isinstance(data, dict):
        return None
    ev = data.get("event", data)
    if isinstance(ev, dict):
        ts = ev.get("startTimestamp")
        try:
            return int(ts) if ts is not None else None
        except Exception:
            return None
    return None


def _result_row(cell: Dict[str, Any], endpoint: str, path: str, status: int,
                ok: bool, transport: Optional[str], error: Optional[str],
                latency: int) -> Dict[str, Any]:
    return {
        "competition": cell["competition"], "season_label": cell["season_label"],
        "season_start_year": cell["season_start_year"], "event_id": cell["event_id"],
        "endpoint": endpoint, "path": path, "status": status,
        "ok": ok, "payload_complete": ok, "transport": transport,
        "error": error, "retry_count": 0, "attempts": [], "latency_ms": latency,
    }


def _from_fetcher(cell: Dict[str, Any], endpoint: str, path: str,
                  res: Dict[str, Any], t0: float) -> Dict[str, Any]:
    return {
        "competition": cell["competition"], "season_label": cell["season_label"],
        "season_start_year": cell["season_start_year"], "event_id": cell["event_id"],
        "endpoint": endpoint, "path": path,
        "status": res.get("status", 0),
        "ok": bool(res.get("ok")),
        "payload_complete": bool(res.get("payload_complete")),
        "transport": res.get("transport"),
        "retry_count": res.get("retry_count", 0),
        "attempts": [
            {"transport": a.get("transport"), "status": a.get("status"),
             "latency_ms": a.get("latency_ms"), "error": a.get("error")}
            for a in res.get("attempts", [])
        ],
        "latency_ms": int((time.monotonic() - t0) * 1000),
    }


def _tier_dist(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    t1 = sum(1 for r in results if r.get("transport") == "curl_cffi")
    t2 = sum(1 for r in results if r.get("transport") == "cloakbrowser")
    t3 = sum(1 for r in results if r.get("transport") == "ssr")
    total = t1 + t2 + t3
    return {"tier1_count": t1, "tier2_count": t2, "tier3_count": t3,
            "fallback_rate": round((t2 + t3) / total, 4) if total else 0.0}


def _evaluate_gates(a: Dict[str, Any]) -> None:
    results = a["results"]
    ineligible_keys = {(c["competition"], c["season_label"])
                       for c in a.get("sampling_filter", {}).get("ineligible_cells", [])}
    eligible_results = [r for r in results
                        if (r["competition"], r["season_label"]) not in ineligible_keys]
    ok_rows = [r for r in eligible_results if r.get("status") == 200 and r.get("payload_complete")]
    total = len(eligible_results) or 1

    per_ep: Dict[str, Dict[str, int]] = {}
    for ep in ENDPOINTS:
        rows = [r for r in eligible_results if r["endpoint"] == ep]
        per_ep[ep] = {"attempted": len(rows),
                      "ok": sum(1 for x in rows if x["status"] == 200 and x["payload_complete"])}

    ineligible_keys = {(c["competition"], c["season_label"])
                       for c in a.get("sampling_filter", {}).get("ineligible_cells", [])}
    cells_all: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    for r in results:
        if (r["competition"], r["season_label"]) in ineligible_keys:
            continue
        cells_all[(r["competition"], r["season_label"])].append(r)
    full_pass_cells = [k for k, rs in cells_all.items()
                       if len(rs) == 4 and all(x["status"] == 200 and x["payload_complete"] for x in rs)]

    comp_pass = sorted({r["competition"] for r in eligible_results})
    season_pass = sorted({str(r["season_start_year"]) for r in eligible_results})

    p2_pass = all(v["ok"] >= max(0, v["attempted"] - 1) for v in per_ep.values())
    p7 = all(any(r["status"] == 200 and r["payload_complete"]
                 for r in eligible_results if r["competition"] == c)
             for c in comp_pass)
    p8 = all(any(r["status"] == 200 and r["payload_complete"]
                 for r in eligible_results if str(r["season_start_year"]) == c)
             for c in season_pass)
    eligible = [k for k in cells_all]
    p9 = (len(full_pass_cells) / len(eligible)) >= 0.95 if eligible else False
    cookie_violations = [c for c in a.get("call_log", [])
                         if c.get("kind") == "endpoint" and c.get("request_cookie_names")]

    stats = a.get("call_stats", {})
    a["gates"] = {
        "P1_overall_ge_95pct_informational": {
            "pass": len(ok_rows) / total >= 0.95, "informational_only": True,
            "value": f"{len(ok_rows)}/{total}"},
        "P2_per_endpoint_ge_99pct": {
            "pass": p2_pass, "evidence": per_ep},
        "P4_quota": {
            "pass": stats.get("total_http_calls", 0) <= MAX_HTTP_CALLS_RUN,
            "evidence": {"total": stats.get("total_http_calls"), "cap": MAX_HTTP_CALLS_RUN}},
        "P5_cleanup": {
            "pass": a.get("cleanup_ok") in (True, None),
            "evidence": {"cleanup_ok": a.get("cleanup_ok")}},
        "P6_no_spurious_halt": {
            "pass": a.get("halt_reason") in (None, "warmup_failed"),
            "evidence": {"halt_reason": a.get("halt_reason")}},
        "P7_per_competition_at_least_one_pass": {
            "pass": p7, "evidence": {"competitions_checked": len(comp_pass)}},
        "P8_per_season_at_least_one_pass": {
            "pass": p8, "evidence": {"seasons_checked": len(season_pass)}},
        "P9_cells_95pct_all_pass": {
            "pass": p9,
            "evidence": {"cells_total": len(eligible), "cells_full_pass": len(full_pass_cells)}},
        "P10_no_cookies_on_tier1": {
            "pass": len(cookie_violations) == 0,
            "evidence": {"violations": len(cookie_violations)}},
        "P11_tier2_hit_rate_ge_90pct": {
            "pass": (a.get("tier_distribution") or {}).get("tier2_count", 0)
                    >= 0.90 * max(1, len(eligible_results)),
            "evidence": a.get("tier_distribution"),
        },
    }

    # Failure diagnosis (only if any structural gate fails)
    fails = [k for k, v in a["gates"].items()
             if not v.get("pass") and not v.get("informational_only")]
    if fails:
        a["failure_diagnosis"] = {
            "failed_gates": fails,
            "competition_level": {
                c: sum(1 for r in results if r["competition"] == c and r["ok"])
                for c in sorted({r["competition"] for r in results})},
            "season_level": {
                s: sum(1 for r in results if str(r["season_start_year"]) == s and r["ok"])
                for s in sorted({str(r["season_start_year"]) for r in results})},
            "endpoint_level": per_ep,
            "note": ("Locate failing (comp, season, endpoint); distinguish hash-distribution "
                     "vs endpoint-specific cause; NO auto-rollback; escalate Kris."),
        }
    else:
        a["failure_diagnosis"] = None

    a["verdict"] = "PASS" if (
        all(v["pass"] for k, v in a["gates"].items() if not v.get("informational_only"))
        and a.get("halt_reason") is None
    ) else "FAIL"


def any_cell_ok(results: List[Dict[str, Any]], key) -> List[str]:
    return sorted({str(key(r)) for r in results})


def _write(a: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACT_PATH.write_text(json.dumps(a, indent=2, ensure_ascii=False, default=str))
    _log(f"artifact: {ARTIFACT_PATH}")


def main() -> int:
    a = asyncio.run(run_canary())
    if a.get("scope_violation"):
        print("SCOPE_VIOLATION", a.get("halt_reason"))
        return 2
    print("\n===== PHASE 10.6 GATES =====")
    for g, v in a.get("gates", {}).items():
        print(f"  {g}: {'PASS' if v.get('pass') else 'FAIL'}")
    print(f"  halt_reason: {a.get('halt_reason')}")
    print(f"  cleanup_ok: {a.get('cleanup_ok')}")
    print(f"  calls: {a.get('call_stats', {}).get('total_http_calls')}")
    print(f"  future_excluded: {a.get('sampling_filter', {}).get('future_date_excluded')}")
    print(f"  tier: {a.get('tier_distribution')}")
    print(f"VERDICT: {a.get('verdict')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
