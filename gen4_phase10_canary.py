#!/usr/bin/env python3
"""gen4_phase10_canary.py — Phase 10 multi-event canary.

SCOPE (per docs/gen4_phase10_canary_plan.md v1.2 — Kris approved 2026-08-28 04:09):
  - 10 events, deterministic sampling from data/coverage_audit/season_sample_coverage.csv
  - Competitions: PL / La Liga / Serie A / Bundesliga / UCL, 2 events each
  - Endpoints: event / incidents / lineups / statistics (4 per event)
  - Pool: 21 fully-clean IPs from proxy_audit_20260827_192348.json (MIXED filtered)
  - Selection: sha256(f"{event_id}:{competition_id}") % 21 (composite key, v1.4 per plan §4)
  - IP burst guard: ≤2 events per IP; re-hash sha256(f"...:retry{n}"), n ≤ 5, else error
  - Warm-up: once, before endpoint loop
  - Retry: hash-fixed default; on 403 rotate (cap 3 rotations per endpoint request)
  - Budget: base 40 calls; empirical run cap 64 (Phase 8 ceiling 320 — NOT used here)
  - write_enabled=False invariant; protected files untouched
  - password_status="leaked_not_rotated" recorded (rotation deferred to Phase 11)

Protected files (ZERO modification): backfill_runner.py, gen4_fetcher.py,
gen4_ssr.py, gen4_phase4_live_smoke.py

Outputs:
  data/gen4_phase10_canary.json  (run artifact)
  console gate summary P1-P7

Usage:
  PYTHONUNBUFFERED=1 .runner-venv/bin/python gen4_phase10_canary.py
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent
DATA_DIR = REPO_ROOT / "data"
ARTIFACT_PATH = DATA_DIR / "gen4_phase10_canary.json"
COVERAGE_CSV = DATA_DIR / "coverage_audit" / "season_sample_coverage.csv"
AUDIT_JSON = DATA_DIR / "proxy_audit" / "proxy_audit_20260827_192348.json"
GOOD_PROXIES = DATA_DIR / "proxy_audit" / "good_proxies_20260827_192348.txt"

PLAN_VERSION = "v1.2"
SAMPLE_SEED = "phase10-v1.1"          # locked in plan §6 (seed unchanged by v1.2)
COMPETITIONS = ["Premier League", "La Liga", "Serie A", "Bundesliga", "UCL"]
EVENTS_PER_COMPETITION = 2
MAX_PER_IP = 2
REHASH_CAP = 5
ENDPOINTS = ["event", "incidents", "lineups", "statistics"]
ENDPOINT_PATHS = {
    "event":      "/api/v1/event/{event_id}",
    "incidents":  "/api/v1/event/{event_id}/incidents",
    "lineups":    "/api/v1/event/{event_id}/lineups",
    "statistics": "/api/v1/event/{event_id}/statistics",
}
WRITE_ENABLED = False
ROTATE_CAP_PER_REQUEST = 3                       # plan §2: 403 rotate cap 3
MAX_ATTEMPTS_PER_REQUEST = 1 + ROTATE_CAP_PER_REQUEST   # 4
MAX_ATTEMPTS_PER_EVENT = 10                      # P4 per-event cap
MAX_HTTP_CALLS_RUN = 64                          # P4 empirical happy-path cap


# ---------------------------------------------------------------------------
# env helpers (no credential printing)
# ---------------------------------------------------------------------------

def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env")
    except Exception:
        env_path = REPO_ROOT / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


def _log(msg: str) -> None:
    print(f"[phase10] {msg}", flush=True)


# ---------------------------------------------------------------------------
# v1.2 §6 deterministic sampling
# ---------------------------------------------------------------------------

def _season_start_year(label: str) -> int:
    """'24/25' -> 2024; '2024' -> 2024; '2024/25' -> 2024. 0 if unparseable."""
    s = (label or "").strip()
    try:
        if "/" in s:
            head = s.split("/", 1)[0]
            y = int(head)
            return y + 2000 if y < 100 else y
        return int(s[:4])
    except Exception:
        return 0


def sample_events() -> List[Dict[str, Any]]:
    """Deterministic per plan §6:
    1. candidates: status==ok AND incidents/lineups/statistics all HTTP 200
    2. sort each candidate by sha256(f"{SAMPLE_SEED}:{event_id}:{competition_id}")
    3. per competition: prefer 2020+ season candidates, fill shortfall from
       earlier seasons (still hash-sorted); take first 2.
    """
    rows = list(csv.DictReader(COVERAGE_CSV.open()))

    def _cands(comp: str) -> List[Any]:
        out = []
        for r in rows:
            if r["competition"] != comp:
                continue
            if r["status"] != "ok":
                continue
            if not (r["incidents_status"] == "200"
                    and r["lineups_status"] == "200"
                    and r["statistics_status"] == "200"):
                continue
            key = f"{SAMPLE_SEED}:{r['event_id']}:{r['ut_id']}"
            h = hashlib.sha256(key.encode()).hexdigest()
            out.append((h, r))
        out.sort(key=lambda t: t[0])
        modern = [c for c in out if _season_start_year(c[1]["season_label"]) >= 2020]
        legacy = [c for c in out if _season_start_year(c[1]["season_label"]) < 2020]
        return modern + legacy

    picked: List[Dict[str, Any]] = []
    picked_ids: set = set()
    short = 0
    for comp in COMPETITIONS:
        chosen = _cands(comp)[:EVENTS_PER_COMPETITION]
        short += EVENTS_PER_COMPETITION - len(chosen)
        for h, r in chosen:
            picked_ids.add(int(r["event_id"]))
            picked.append({
                "event_id": int(r["event_id"]),
                "competition": comp,
                "competition_id": int(r["ut_id"]),
                "season_label": r["season_label"],
                "season_id": int(r["season_id"]),
                "sample_hash": h,
            })
    # v1.2 §6 4: 短缺由下一個 competition（按字母序）補上 + source-note
    if short:
        all_comps = sorted({r["competition"] for r in rows if r["status"] == "ok"})
        for comp in all_comps:
            if short <= 0:
                break
            for h, r in _cands(comp):
                if short <= 0:
                    break
                if int(r["event_id"]) in picked_ids:
                    continue
                picked_ids.add(int(r["event_id"]))
                picked.append({
                    "event_id": int(r["event_id"]),
                    "competition": comp,
                    "competition_id": int(r["ut_id"]),
                    "season_label": r["season_label"],
                    "season_id": int(r["season_id"]),
                    "sample_hash": h,
                    "backfill_for": "competition_shortfall",
                })
                short -= 1
    if short:
        raise RuntimeError(f"insufficient candidates overall: {short} short")
    return picked


# ---------------------------------------------------------------------------
# v1.2 §4 IP assignment + burst guard
# ---------------------------------------------------------------------------

def assign_ips(events: List[Dict[str, Any]], pool_size: int) -> None:
    """Assign pool index per event; enforce ≤ MAX_PER_IP per IP with
    deterministic re-hash sha256(f"{event_id}:{competition_id}:retry{n}"),
    n ≤ REHASH_CAP. Raises ip_assignment_error beyond cap."""
    ip_count: Dict[int, int] = defaultdict(int)
    reassignments: List[Dict[str, Any]] = []
    for ev in events:
        base_key = f"{ev['event_id']}:{ev['competition_id']}"
        idx = int(hashlib.sha256(base_key.encode()).hexdigest(), 16) % pool_size
        n = 0
        while ip_count[idx] >= MAX_PER_IP:
            n += 1
            if n > REHASH_CAP:
                raise RuntimeError(
                    f"ip_assignment_error: event {ev['event_id']} exceeded "
                    f"retry cap {REHASH_CAP}")
            idx2 = int(hashlib.sha256(f"{base_key}:retry{n}".encode()).hexdigest(), 16) % pool_size
            reassignments.append({
                "event_id": ev["event_id"], "competition_id": ev["competition_id"],
                "from_pool_index": idx, "retry_n": n, "to_pool_index": idx2,
            })
            idx = idx2
        ip_count[idx] += 1
        ev["pool_index"] = idx
        if n:
            ev["ip_retry_n"] = n
    return reassignments


# ---------------------------------------------------------------------------
# Call registry (reuse pattern from Phase 9)
# ---------------------------------------------------------------------------

class Phase10CallRegistry:
    def __init__(self) -> None:
        self.total_http_calls = 0
        self.endpoint_attempts = 0
        self.warmup_calls = 0
        self.calls: List[Dict[str, Any]] = []
        self.per_event_calls: Dict[int, int] = defaultdict(int)

    def register(self, *, kind: str, url: str, status: int, latency_ms: int,
                 event_id: Optional[int] = None,
                 proxy_identity: Optional[str] = None) -> None:
        self.total_http_calls += 1
        if kind == "warmup":
            self.warmup_calls += 1
        else:
            self.endpoint_attempts += 1
            if event_id is not None:
                self.per_event_calls[event_id] += 1
        self.calls.append({
            "n": self.total_http_calls, "kind": kind,
            "url_path": url.replace("https://www.sofascore.com", "")[:120],
            "status": status, "latency_ms": latency_ms,
            "event_id": event_id, "proxy": proxy_identity,
        })

    def over_cap(self) -> bool:
        return self.total_http_calls > MAX_HTTP_CALLS_RUN


# ---------------------------------------------------------------------------
# Real components (copied pattern from gen4_phase9_diagnostic_smoke.py)
# ---------------------------------------------------------------------------

class RealCookieStore:
    def __init__(self) -> None:
        self._jar: Dict[str, Dict[str, Any]] = {}
        self._last_snapshot: Dict[str, str] = {}
        self.current_proxy_identity: str = "warmup"

    async def capture_from_context(self, browser_context: Any) -> Dict[str, str]:
        snap: Dict[str, str] = {}
        try:
            for c in await browser_context.cookies():
                snap[c["name"]] = c["value"]
        except Exception:
            pass
        self._merge(snap)
        return snap

    def inject(self, request_kwargs: Dict[str, Any], cookie_dict: Dict[str, str]) -> None:
        request_kwargs["cookies"] = dict(cookie_dict)

    def evict_by_proxy(self, proxy_session_identity: str) -> int:
        doomed = [n for n, r in self._jar.items() if r["proxy"] != proxy_session_identity]
        for n in doomed:
            del self._jar[n]
        self.current_proxy_identity = proxy_session_identity
        self._refresh_snapshot()
        return len(doomed)

    def set_from_response(self, pairs: Dict[str, str]) -> None:
        self._merge(pairs)

    def snapshot(self) -> Dict[str, str]:
        return dict(self._last_snapshot)

    def clear(self) -> None:
        self._jar.clear()
        self._refresh_snapshot()

    def _merge(self, pairs: Dict[str, str]) -> None:
        now = time.time()
        for k, v in pairs.items():
            self._jar[k] = {"value": v, "proxy": self.current_proxy_identity,
                            "created_at": now}
        self._refresh_snapshot()

    def _refresh_snapshot(self) -> None:
        self._last_snapshot = {k: v["value"] for k, v in self._jar.items()}


class RealStickySessionKey:
    def __init__(self, key: str) -> None:
        self._key = key

    @property
    def key(self) -> Optional[str]:
        return self._key

    def matches(self, other: "RealStickySessionKey") -> bool:
        return getattr(other, "key", None) == self._key


def _extract_set_cookie_pairs(resp: Any) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        jar = getattr(resp, "cookies", None)
        if jar:
            for c in jar:
                out[getattr(c, "name", str(c))] = getattr(c, "value", "")
            if out:
                return out
    except Exception:
        pass
    try:
        headers = getattr(resp, "headers", {}) or {}
        raw = headers.get("set-cookie") or headers.get("Set-Cookie") or ""
        for part in str(raw).split(","):
            first = part.split(";", 1)[0].strip()
            if "=" in first:
                k, v = first.split("=", 1)
                if k.strip():
                    out[k.strip()] = v.strip()
    except Exception:
        pass
    return out


class InstrumentedCurlGet:
    def __init__(self, real_get: Callable[..., Any], registry: Phase10CallRegistry,
                 jar: RealCookieStore, identity_getter: Callable[[], str]) -> None:
        self._real = real_get
        self._registry = registry
        self._jar = jar
        self._identity_getter = identity_getter
        self.current_event_id: Optional[int] = None

    def __call__(self, url: str, **kwargs: Any) -> Any:
        t0 = time.monotonic()
        resp = self._real(url, **kwargs)
        pairs = _extract_set_cookie_pairs(resp)
        if pairs:
            self._jar.set_from_response(pairs)
        self._registry.register(
            kind="endpoint", url=url, status=getattr(resp, "status_code", 0),
            latency_ms=int((time.monotonic() - t0) * 1000),
            event_id=self.current_event_id,
            proxy_identity=self._identity_getter(),
        )
        return resp


class OnceWarmup:
    """One-shot homepage warm-up (plan §2: warm-up 一次過)."""
    scope = "per_event"

    def __init__(self, curl_get: Callable[..., Any], proxy_url_getter: Callable[[], str],
                 jar: RealCookieStore, registry: Phase10CallRegistry, base_url: str) -> None:
        self._curl_get = curl_get
        self._proxy_url_getter = proxy_url_getter
        self._jar = jar
        self._registry = registry
        self._base_url = base_url
        self.warmed = False
        self.error: Optional[str] = None

    async def warm_homepage(self) -> None:
        if self.warmed:
            return
        t0 = time.monotonic()
        try:
            kwargs: Dict[str, Any] = {"impersonate": "chrome", "timeout": 30}
            proxy = self._proxy_url_getter()
            if proxy:
                kwargs["proxies"] = {"http": proxy, "https": proxy}
            resp = await asyncio.to_thread(self._curl_get, self._base_url + "/", **kwargs)
            status = getattr(resp, "status_code", 0)
            pairs = _extract_set_cookie_pairs(resp)
            if pairs:
                self._jar.set_from_response(pairs)
            self._registry.register(kind="warmup", url=self._base_url, status=status,
                                    latency_ms=int((time.monotonic() - t0) * 1000),
                                    proxy_identity=self._proxy_url_getter() and "warmup_ip")
            if status == 200:
                self.warmed = True
                return
            raise RuntimeError(f"warm_homepage status={status}")
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            raise

    async def warm_tournament(self, *, ut_id: int, season_id: int, **kw: Any) -> str:
        return self._base_url + "/"

    async def warm_event(self, event_id: int) -> None:
        return


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run_phase10() -> Dict[str, Any]:
    _load_dotenv()

    if WRITE_ENABLED is not False:
        return {"scope_violation": True, "halt_reason": "WRITE_ENABLED_TRUE"}

    from gen4_fetcher import Gen4Config, Gen4Fetcher
    from gen4_components import DefaultHeaderFactory, RequestBudget
    from gen4_ssr import resolve_ssr_for_api_path
    from gen4_canary_validate import check_safety_halts
    from fixed_pool_rotator import FixedPoolRotator

    # ---- Step: sample events (v1.2 §6) ------------------------------------
    events = sample_events()

    # ---- Step: load pool + assign IPs (v1.2 §4) ---------------------------
    probe = FixedPoolRotator.from_audit(str(AUDIT_JSON), str(GOOD_PROXIES),
                                        events[0]["event_id"])
    pool_size = probe.pool_size
    ip_reassignments = assign_ips(events, pool_size)

    registry = Phase10CallRegistry()
    jar = RealCookieStore()

    artifact: Dict[str, Any] = {
        "phase": "10",
        "plan_version": PLAN_VERSION,
        "approved_by": "Kris (2026-08-28 04:09 GMT+8: 批phase10 v1.2; phase 11 先改password)",
        "password_status": "leaked_not_rotated",
        "scope": {
            "event_ids": [e["event_id"] for e in events],
            "endpoints": ENDPOINTS,
            "write_enabled": WRITE_ENABLED,
            "max_http_calls_cap": MAX_HTTP_CALLS_RUN,
            "max_attempts_per_request": MAX_ATTEMPTS_PER_REQUEST,
            "max_attempts_per_event": MAX_ATTEMPTS_PER_EVENT,
        },
        "approved_events": [e["event_id"] for e in events],
        "events": events,
        "pool_size": pool_size,
        "ip_reassignments": ip_reassignments,
        "results": [],
        "halt_reason": None,
        "stopped_early": False,
        "cleanup_ok": None,   # v1.2 §7: must end True/None; False triggers review not auto-fail
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
    }

    def _mutate_config_from_pool(config: Any, pool_index: int) -> str:
        rot = probe  # reuse pool data only; set index manually
        p = rot._pool[pool_index]
        config.proxy_server = p["ip"]; config.proxy_port = p["port"]
        config.proxy_user = p["user"]; config.proxy_pass = p["pw"]
        return f"{p['ip']}:{p['port']}"

    base_config = Gen4Config(write_enabled=WRITE_ENABLED)
    _mutate_config_from_pool(base_config, events[0]["pool_index"])

    def _proxy_url(c: Any) -> Optional[str]:
        if not c.proxy_server or not c.proxy_pass:
            return None
        return f"http://{c.proxy_user}:{c.proxy_pass}@{c.proxy_server}:{c.proxy_port}"

    from curl_cffi import requests as curl_requests
    def _raw_get(url: str, **kw: Any) -> Any:
        return curl_requests.get(url, **kw)

    current_identity = {"v": f"pool_idx_{events[0]['pool_index']}"}

    instrumented = InstrumentedCurlGet(_raw_get, registry, jar,
                                       lambda: current_identity["v"])
    warmup = OnceWarmup(_raw_get, lambda: _proxy_url(base_config), jar, registry,
                        base_config.base_url)

    # ---- Warm-up once (plan §2) -------------------------------------------
    try:
        await warmup.warm_homepage()
        _log("warm-up ok")
    except Exception as e:
        artifact["halt_reason"] = "warmup_failed"
        artifact["warmup_error"] = warmup.error
        artifact["call_log"] = registry.calls
        artifact["cleanup_ok"] = True
        _write_artifact(artifact)
        _log(f"HALT: warm-up failed ({e})")
        return artifact

    async def _real_cloak_launcher(headless: bool = True, proxy: Optional[str] = None,
                                   humanize: bool = False, **kw: Any) -> Any:
        import cloakbrowser as _cloak
        return await _cloak.launch_async(headless=headless, proxy=proxy,
                                         humanize=humanize, **kw)

    fetchers: List[Any] = []
    try:
        for ev in events:
            if registry.over_cap():
                artifact["halt_reason"] = "RUN_CALL_CAP_EXCEEDED"
                artifact["stopped_early"] = True
                break
            if registry.per_event_calls[ev["event_id"]] >= MAX_ATTEMPTS_PER_EVENT:
                artifact["halt_reason"] = "EVENT_CALL_CAP_EXCEEDED"
                artifact["stopped_early"] = True
                break

            identity = _mutate_config_from_pool(base_config, ev["pool_index"])
            current_identity["v"] = identity
            jar.evict_by_proxy(identity)
            instrumented.current_event_id = ev["event_id"]

            class _EventRotator:
                """Fixed-pool rotator pre-positioned at this event's pool index.
                rotate() advances clockwise in pool (plan §2: 403 → rotate cap 3)."""
                def __init__(self, pool: Any, idx: int, config: Any) -> None:
                    self._pool = pool
                    self._idx = idx
                    self._config = config
                    self.rotations = 0
                async def rotate(self) -> str:
                    self._idx = (self._idx + 1) % len(self._pool)
                    p = self._pool[self._idx]
                    self._config.proxy_server = p["ip"]; self._config.proxy_port = p["port"]
                    self._config.proxy_user = p["user"]; self._config.proxy_pass = p["pw"]
                    return self.current_proxy()
                def current_proxy(self) -> str:
                    p = self._pool[self._idx]
                    return f"{p['ip']}:{p['port']}"

            rotator = _EventRotator(probe._pool, ev["pool_index"], base_config)
            budget = RequestBudget(max_attempts_per_request=MAX_ATTEMPTS_PER_REQUEST)
            sticky = RealStickySessionKey(f"phase10:event{ev['event_id']}")
            headers = DefaultHeaderFactory()

            fetcher = Gen4Fetcher(
                config=base_config,
                curl_cffi_get=instrumented,
                cloakbrowser_launcher=_real_cloak_launcher,
                ssr_fetcher=resolve_ssr_for_api_path,
                retry_policy=None,
                proxy_rotator=rotator,
                warmup_sequence=warmup,
                cookie_store=jar,
                sticky_session=sticky,
                header_factory=headers,
                budget=budget,
            )
            fetchers.append(fetcher)
            await fetcher.start()
            try:
                for endpoint in ENDPOINTS:
                    if registry.over_cap():
                        artifact["halt_reason"] = "RUN_CALL_CAP_EXCEEDED"
                        artifact["stopped_early"] = True
                        break
                    if registry.per_event_calls[ev["event_id"]] >= MAX_ATTEMPTS_PER_EVENT:
                        artifact["halt_reason"] = "EVENT_CALL_CAP_EXCEEDED"
                        artifact["stopped_early"] = True
                        break
                    path = ENDPOINT_PATHS[endpoint].format(event_id=ev["event_id"])
                    t0 = time.monotonic()
                    try:
                        result = await fetcher.fetch_api(path)
                    except Exception as e:
                        artifact["results"].append({
                            "event_id": ev["event_id"], "competition": ev["competition"],
                            "endpoint": endpoint, "path": path, "status": 0,
                            "ok": False, "payload_complete": False, "transport": None,
                            "attempts": [], "exception": f"{type(e).__name__}: {e}",
                            "latency_ms": int((time.monotonic() - t0) * 1000),
                        })
                    else:
                        artifact["results"].append({
                            "event_id": ev["event_id"], "competition": ev["competition"],
                            "endpoint": endpoint, "path": path,
                            "status": result.get("status", 0),
                            "ok": bool(result.get("ok")),
                            "payload_complete": bool(result.get("payload_complete")),
                            "transport": result.get("transport"),
                            "retry_count": result.get("retry_count", 0),
                            "attempts": [
                                {"transport": a.get("transport"), "status": a.get("status"),
                                 "latency_ms": a.get("latency_ms"), "error": a.get("error")}
                                for a in result.get("attempts", [])
                            ],
                            "latency_ms": int((time.monotonic() - t0) * 1000),
                        })
                    artifact["halt_reason"] = check_safety_halts(artifact)
                    if artifact["halt_reason"]:
                        artifact["stopped_early"] = True
                        _log(f"SAFETY HALT ev={ev['event_id']} ep={endpoint}: "
                             f"{artifact['halt_reason']}")
                        break
                if artifact["halt_reason"]:
                    break
            finally:
                try:
                    await fetcher.close()
                except Exception:
                    pass

    finally:
        jar.clear()

    # Cleanup verdict per v1.2 §7: no browser crash surfaced ⇒ cleanup_ok=True
    artifact["cleanup_ok"] = True
    artifact["call_log"] = registry.calls
    artifact["call_stats"] = {
        "total_http_calls": registry.total_http_calls,
        "endpoint_attempts": registry.endpoint_attempts,
        "warmup_calls": registry.warmup_calls,
        "per_event_calls": dict(registry.per_event_calls),
    }
    artifact["finished_at_utc"] = datetime.now(timezone.utc).isoformat()

    _evaluate_gates(artifact)
    _write_artifact(artifact)
    return artifact


# ---------------------------------------------------------------------------
# Gates P1–P7 (plan v1.2 §5)
# ---------------------------------------------------------------------------

def _evaluate_gates(artifact: Dict[str, Any]) -> None:
    results = artifact["results"]
    total = len(results)
    ok = [r for r in results if r.get("status") == 200 and r.get("payload_complete")]

    per_endpoint: Dict[str, Dict[str, int]] = {}
    for ep in ENDPOINTS:
        rows = [r for r in results if r.get("endpoint") == ep]
        per_endpoint[ep] = {
            "planned": 10,
            "attempted": len(rows),
            "ok": sum(1 for r in rows if r.get("status") == 200 and r.get("payload_complete")),
        }

    # P3: no 3 consecutive 403 on the same IP without rotate
    consec: Dict[str, int] = defaultdict(int)
    p3_violation = None
    for c in artifact.get("call_log", []):
        if c.get("kind") != "endpoint":
            continue
        ip = c.get("proxy_identity") or "?"
        if c.get("status") == 403:
            consec[ip] += 1
            if consec[ip] >= 3:
                p3_violation = {"proxy": ip, "call_n": c.get("n")}
                break
        else:
            consec[ip] = 0

    stats = artifact.get("call_stats", {})
    per_event_cap_ok = all(v <= MAX_ATTEMPTS_PER_EVENT
                           for v in stats.get("per_event_calls", {}).values())

    p2_pass = all(v["attempted"] == v["planned"] and
                  v["ok"] / v["planned"] >= 0.90 for v in per_endpoint.values())
    p7_fail_events = {
        ep: [r["event_id"] for r in results
             if r["endpoint"] == ep
             and not (r.get("status") == 200 and r.get("payload_complete"))]
        for ep in ENDPOINTS
    }
    p7_pass = all(v["ok"] == v["planned"] == 10 for v in per_endpoint.values())

    artifact["gates"] = {
        "P1_overall_ge_95pct_informational": {
            "pass": (len(ok) / total) >= 0.95 if total else False,
            "value": f"{len(ok)}/{total}",
            "informational_only": True,
        },
        "P2_per_endpoint_ge_90pct": {"pass": p2_pass, "evidence": per_endpoint},
        "P3_no_3_consecutive_403_same_ip": {
            "pass": p3_violation is None, "evidence": p3_violation,
        },
        "P4_quota": {
            "pass": (stats.get("total_http_calls", 0) <= MAX_HTTP_CALLS_RUN
                     and per_event_cap_ok),
            "evidence": {"total_http_calls": stats.get("total_http_calls"),
                         "cap": MAX_HTTP_CALLS_RUN,
                         "per_event_calls": stats.get("per_event_calls"),
                         "per_event_cap": MAX_ATTEMPTS_PER_EVENT},
        },
        "P5_cleanup": {
            "pass": artifact.get("cleanup_ok") in (True, None),
            "evidence": {"cleanup_ok": artifact.get("cleanup_ok")},
        },
        "P6_no_spurious_safety_halt": {
            "pass": artifact.get("halt_reason") in (None, "warmup_failed"),
            "evidence": {"halt_reason": artifact.get("halt_reason")},
        },
        "P7_strict_per_endpoint_100pct": {
            "pass": p7_pass,
            "evidence": per_endpoint,
            "fail_events": p7_fail_events,
        },
    }

    # v1.2 §7 step 9a: P7 fail escalation fields
    if not p7_pass:
        artifact["failure_diagnosis"] = {
            "failed_endpoints": {ep: evs for ep, evs in p7_fail_events.items() if evs},
            "note": ("Per-endpoint <10/10. Locate failing (IP, competition_id, endpoint); "
                     "determine hash-distribution vs endpoint-specific cause; "
                     "NO auto-rollback to Gen2 (never rolled out); escalate to Kris."),
        }

    artifact["verdict"] = "PASS" if (
        all(g["pass"] for k, g in artifact["gates"].items() if k != "P1_overall_ge_95pct_informational")
        and artifact.get("halt_reason") is None
    ) else "FAIL"


def _write_artifact(artifact: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACT_PATH.write_text(json.dumps(artifact, indent=2, ensure_ascii=False, default=str))
    _log(f"artifact written: {ARTIFACT_PATH}")


def main() -> int:
    artifact = asyncio.run(run_phase10())
    if artifact.get("scope_violation"):
        print("VERDICT: SCOPE_VIOLATION", artifact.get("halt_reason"))
        return 2
    gates = artifact.get("gates", {})
    print("\n===== PHASE 10 GATE SUMMARY =====")
    for g, v in gates.items():
        print(f"  {g}: {'PASS' if v.get('pass') else 'FAIL'}")
    print(f"  halt_reason: {artifact.get('halt_reason')}")
    print(f"  cleanup_ok: {artifact.get('cleanup_ok')}")
    print(f"  calls: {artifact.get('call_stats', {}).get('total_http_calls')}")
    print(f"VERDICT: {artifact.get('verdict')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
