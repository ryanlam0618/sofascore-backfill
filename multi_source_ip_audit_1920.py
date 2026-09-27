#!/usr/bin/env python3
"""multi_source_ip_audit_1920.py — Audit 5+ IP sources on 19/20 historical event.

Per Duncan dispatch 2026-08-29 02:26. Uses event 8351333 (A-League Men 19/20).
Tests both API and frontend, Tier-1 curl and Tier-2 browser (CloakBrowser).

No IP/password/event_id plaintext in stdout; artifact JSON uses hashed index.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent
POOL_JSON = REPO_ROOT / "data" / "proxy_audit" / "ip_assignment_phase10_6.json"
BASE_API = "https://www.sofascore.com/api/v1/event/8351333"
BASE_HTML = "https://www.sofascore.com/event/8351333"
TIMEOUT_S = 10


def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _h8(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:8]


def _load_env() -> Dict[str, str]:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
    return {"user": os.environ.get("SOFA_PROXY_USER", ""),
            "pw": os.environ.get("SOFA_PROXY_PASS", "")}


def _get(url: str, proxy_url: Optional[str] = None) -> Dict[str, Any]:
    from curl_cffi import requests as cr
    kw: Dict[str, Any] = {"impersonate": "chrome", "timeout": TIMEOUT_S}
    if proxy_url:
        kw["proxies"] = {"http": proxy_url, "https": proxy_url}
    t0 = time.monotonic()
    try:
        r = cr.get(url, **kw)
        return {"status": r.status_code,
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "size": len(r.text)}
    except Exception as e:
        return {"status": 0, "latency_ms": int((time.monotonic() - t0) * 1000),
                "error": type(e).__name__}


async def _browser_check(proxy_url: Optional[str], url: str) -> Dict[str, Any]:
    """Direct CloakBrowser check (bypasses Gen4Fetcher wiring simplicity)."""
    import cloakbrowser as cb
    t0 = time.monotonic()
    browser = None
    try:
        launch_kwargs = {"headless": True, "humanize": False}
        if proxy_url:
            launch_kwargs["proxy"] = proxy_url
        browser = await cb.launch_async(**launch_kwargs)
        page = await browser.new_page() if hasattr(browser, "new_page") else None
        if page is None:
            ctx = await browser.new_context()
            page = await ctx.new_page()
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=int(TIMEOUT_S * 1000))
        status = resp.status if resp else 0
        # try to read body as JSON if it's api
        return {"status": status,
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "transport": "cloakbrowser",
                "ok": status == 200}
    except Exception as e:
        return {"status": 0, "latency_ms": int((time.monotonic() - t0) * 1000),
                "transport": "cloakbrowser", "ok": False, "error": type(e).__name__}
    finally:
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass


async def main() -> None:
    creds = _load_env()
    pool = json.loads(POOL_JSON.read_text())["recommended_pool"]

    # 5 Webshare IPs from pool (indices 0,1,2,3,4 for speed + consistency)
    sources: List[Dict[str, Any]] = []
    for i, p in enumerate(pool[:5]):
        sources.append({
            "label": f"ws-proxy-{_h8(p['ip'])}",
            "ip_category": "data_center_webshare",
            "ip_hash": _h8(p["ip"]),
            "proxy_url": "http://" + creds['user'] + ":" + creds['pw'] + "@" + p['ip'] + ":" + p['port'],
            "t1": True, "t2": True,
        })
    # host direct
    sources.append({
        "label": "host-egress",
        "ip_category": "data_center_host",
        "ip_hash": _h8("host-egress"),
        "proxy_url": None,
        "t1": True, "t2": True,
    })

    results: List[Dict[str, Any]] = []

    for src in sources:
        row = {"label": src["label"], "ip_category": src["ip_category"],
               "ip_hash": src["ip_hash"],
               "api": {}, "frontend": {}}

        # API - T1
        r = _get(BASE_API, src["proxy_url"])
        row["api"]["t1_curl"] = r
        # API - T2 (browser)
        r2 = await _browser_check(src["proxy_url"], BASE_API)
        row["api"]["t2_browser"] = r2
        # Frontend HTML - T1
        r3 = _get(BASE_HTML, src["proxy_url"])
        row["frontend"]["t1_curl"] = r3
        # Frontend HTML - T2
        r4 = await _browser_check(src["proxy_url"], BASE_HTML)
        row["frontend"]["t2_browser"] = r4

        # verdict
        api_pass = r3["status"] == 200 or r2.get("ok", False)
        fe_pass = r3["status"] == 200 or r4.get("ok", False)
        if api_pass and fe_pass:
            v = "PASS-BOTH"
        elif not api_pass and fe_pass:
            v = "PASS-FRONTEND-FAIL-API (Kris hypothesis: API-gate)"
        elif api_pass and not fe_pass:
            v = "PASS-API-FAIL-FRONTEND"
        else:
            v = "FAIL-BOTH"
        row["verdict"] = v
        results.append(row)

    ts = _now_ts()
    out_path = REPO_ROOT / "data" / "proxy_audit" / f"multi_source_ip_audit_{ts}.json"
    json.dump({
        "task": "multi_source_ip_audit_19_20",
        "target_api": "/api/v1/event/8351333",
        "target_frontend": "/event/8351333",
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "n_sources": len(results),
        "results": results,
    }, open(out_path, "w"), indent=2, ensure_ascii=False)

    print(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"written: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
