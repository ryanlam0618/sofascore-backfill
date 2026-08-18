#!/usr/bin/env python3
"""
Three-way comparison: Direct IP vs Playwright+Proxy vs CloakBrowser+Proxy
Tests the same set of Sofascore API endpoints and reports results side-by-side.
"""

import json
import time
import subprocess
import requests
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────
PROXY = {
    "server": "http://p.webshare.io:80",
    "username": "aeptenjc-rotate",
    "password": "dztr57tcycoz",
}

# Test events — mix of leagues, eras, and competitions
TEST_EVENTS = [
    (14025013, "PL 25/26", "Liverpool vs Bournemouth"),
    (12436870, "PL 24/25", "Man Utd vs Fulham"),
    (11352303, "PL 23/24", "Burnley vs Man City"),
    (8896967,  "PL 20/21", "Fulham vs Arsenal"),
    (7827861,  "PL 18/19", "Man Utd vs Leicester"),
    (14056037, "Bundesliga 25/26", "Bayern vs RB Leipzig"),
    (12764526, "UCL 24/25", "Young Boys vs Aston Villa"),
    (13335241, "K League 2025", "Pohang vs Daejeon"),
    (11917915, "J1 2024", "Sanfrecce vs Urawa"),
    (12060227, "CSL 2024", "Shandong vs Changchun"),
]

ENDPOINTS = [
    ("event",      "/api/v1/event/{eid}"),
    ("incidents",  "/api/v1/event/{eid}/incidents"),
    ("lineups",    "/api/v1/event/{eid}/lineups"),
    ("shotmap",    "/api/v1/event/{eid}/shotmap"),
    ("graph",      "/api/v1/event/{eid}/graph"),
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.sofascore.com/",
}

OUTPUT_FILE = Path("/root/.openclaw/workspace/sofascore-backfill/data/three_way_compare.json")


# ── Method 1: Direct IP (curl) ──────────────────────────────────────────────
def test_direct_ip(eid):
    results = {}
    for ep_name, ep_path in ENDPOINTS:
        url = f"https://www.sofascore.com{ep_path.format(eid=eid)}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            results[ep_name] = {
                "status": r.status_code,
                "len": len(r.content),
                "body": r.text[:200] if r.status_code != 200 else f"OK ({len(r.content)} bytes)",
            }
        except Exception as e:
            results[ep_name] = {"status": "error", "error": str(e)[:100]}
        time.sleep(0.3)
    return results


# ── Method 2: Playwright + Proxy ─────────────────────────────────────────────
def test_playwright_proxy(eid):
    results = {}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {ep_name: {"status": "skipped", "error": "playwright not installed"} for ep_name, _ in ENDPOINTS}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            proxy={"server": PROXY["server"], "username": PROXY["username"], "password": PROXY["password"]},
        )
        ctx = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="en-US",
        )
        page = ctx.new_page()
        page.set_extra_http_headers({"Accept": "application/json"})

        for ep_name, ep_path in ENDPOINTS:
            url = f"https://www.sofascore.com{ep_path.format(eid=eid)}"
            try:
                resp = page.goto(url, timeout=20000)
                status = resp.status if resp else 0
                body = page.evaluate("() => document.body.innerText")
                results[ep_name] = {
                    "status": status,
                    "len": len(body) if body else 0,
                    "body": body[:200] if status != 200 else f"OK ({len(body) if body else 0} chars)",
                }
            except Exception as e:
                results[ep_name] = {"status": "error", "error": str(e)[:100]}
            time.sleep(0.3)

        browser.close()
    return results


# ── Method 3: CloakBrowser + Proxy ───────────────────────────────────────────
def test_cloakbrowser_proxy(eid):
    results = {}
    try:
        from cloakbrowser import launch
    except ImportError:
        return {ep_name: {"status": "skipped", "error": "cloakbrowser not installed"} for ep_name, _ in ENDPOINTS}

    browser = launch(
        headless=True,
        humanize=True,
        proxy=PROXY,
    )
    page = browser.new_page()

    for ep_name, ep_path in ENDPOINTS:
        url = f"https://www.sofascore.com{ep_path.format(eid=eid)}"
        try:
            resp = page.goto(url, timeout=20000)
            status = resp.status if resp else 0
            body = page.evaluate("() => document.body.innerText")
            results[ep_name] = {
                "status": status,
                "len": len(body) if body else 0,
                "body": body[:200] if status != 200 else f"OK ({len(body) if body else 0} chars)",
            }
        except Exception as e:
            results[ep_name] = {"status": "error", "error": str(e)[:100]}
        time.sleep(0.3)

    browser.close()
    return results


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    print("=" * 70)
    print("  Three-Way Comparison: Direct IP vs Playwright+Proxy vs CloakBrowser+Proxy")
    print("=" * 70)
    print(f"  Events: {len(TEST_EVENTS)} | Endpoints per event: {len(ENDPOINTS)}")
    print(f"  Start: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    all_results = []
    for i, (eid, label, match) in enumerate(TEST_EVENTS):
        print(f"\n[{i+1}/{len(TEST_EVENTS)}] {label} — {match} (event {eid})")
        print("-" * 70)

        print("  [1/3] Direct IP...")
        t0 = time.time()
        direct = test_direct_ip(eid)
        t_direct = time.time() - t0
        print(f"        Done ({t_direct:.1f}s)")

        print("  [2/3] Playwright + Proxy...")
        t0 = time.time()
        pw = test_playwright_proxy(eid)
        t_pw = time.time() - t0
        print(f"        Done ({t_pw:.1f}s)")

        print("  [3/3] CloakBrowser + Proxy...")
        t0 = time.time()
        cb = test_cloakbrowser_proxy(eid)
        t_cb = time.time() - t0
        print(f"        Done ({t_cb:.1f}s)")

        # Print comparison table
        print(f"\n  {'Endpoint':<12} {'Direct':>8} {'PW+Proxy':>10} {'CB+Proxy':>10}")
        print(f"  {'-'*12} {'-'*8} {'-'*10} {'-'*10}")
        for ep_name, _ in ENDPOINTS:
            d = direct.get(ep_name, {}).get("status", "?")
            p = pw.get(ep_name, {}).get("status", "?")
            c = cb.get(ep_name, {}).get("status", "?")
            print(f"  {ep_name:<12} {str(d):>8} {str(p):>10} {str(c):>10}")

        all_results.append({
            "event_id": eid,
            "label": label,
            "match": match,
            "direct_ip": {"results": direct, "time_s": round(t_direct, 2)},
            "playwright_proxy": {"results": pw, "time_s": round(t_pw, 2)},
            "cloakbrowser_proxy": {"results": cb, "time_s": round(t_cb, 2)},
        })

        # Save after each event
        OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(OUTPUT_FILE, "w") as f:
            json.dump(all_results, f, indent=2, ensure_ascii=False)

        time.sleep(1)

    # ── Summary ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  SUMMARY")
    print("=" * 70)

    for method in ["direct_ip", "playwright_proxy", "cloakbrowser_proxy"]:
        ok = fail_403 = fail_404 = other = 0
        total_time = 0
        for r in all_results:
            for ep_name, _ in ENDPOINTS:
                s = r[method]["results"].get(ep_name, {}).get("status", "error")
                if s == 200:
                    ok += 1
                elif s == 403:
                    fail_403 += 1
                elif s == 404:
                    fail_404 += 1
                else:
                    other += 1
            total_time += r[method]["time_s"]
        total = ok + fail_403 + fail_404 + other
        print(f"  {method:<22}: OK={ok:>3}  403={fail_403:>3}  404={fail_404:>3}  Other={other:>3}  Time={total_time:.0f}s  Rate={ok/total*100:.1f}%")

    print(f"\n  Results saved: {OUTPUT_FILE}")
    print(f"  End: {time.strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
