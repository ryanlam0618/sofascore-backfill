#!/usr/bin/env python3
"""
SofaScore Season Discovery
==========================
Discovers valid season IDs for ALL 23 competitions using:
  GET /unique-tournament/{ut_id}/seasons

This is the WORKING endpoint for season discovery.
Confirmed: 2026-05-04 - all 22 competitions return valid season lists.

Also tests event endpoints to verify backfill compatibility.

Usage:
    python3 discover_seasons.py --all
    python3 discover_seasons.py --name "La Liga"
    python3 discover_seasons.py --check-events
"""

from __future__ import annotations
import argparse, json, random, time, urllib.request, datetime, sys
from pathlib import Path

API_BASE = "https://www.sofascore.com/api/v1"
WORKDIR = Path(__file__).parent
OUT_FILE = WORKDIR / "discoveries.json"


# All 23 competitions with known uniqueTournament IDs
COMPETITIONS = [
    ("Premier League",          17,    1,  "national"),
    ("La Liga",                 8,   32,  "national"),
    ("Serie A",                23,   31,  "national"),
    ("Bundesliga",              9,   30,  "national"),
    ("Ligue 1",                34,    7,  "national"),
    ("J1 League",             196,   52,  "national"),
    ("K League 1",            410,  291,  "national"),
    ("A-League Men",          136,   34,  "national"),
    ("Chinese Super League",   649,   99,  "national"),
    ("UCL",                     7, 1465,  "uefa"),
    ("UEL",                   679, 1465,  "uefa"),
    ("UECL",               17015, 1465,  "uefa"),
    ("AFC Champions League",   463, 1467,  "uefa"),
    ("FA Cup",                 19,    1,  "cup"),
    ("EFL Cup",                21,    1,  "cup"),
    ("Copa del Rey",          329,   32,  "cup"),
    ("Coppa Italia",          328,   31,  "cup"),
    ("Coupe de France",       335,    7,  "cup"),
    ("DFB Pokal",             217,   30,  "cup"),
    ("J.League Cup",          101,   52,  "cup"),
    ("Emperor's Cup",         323,   52,  "cup"),
    ("Australia Cup",        1786,   34,  "cup"),
    ("Chinese FA Cup",       None,   99,  "cup"),
]


def api_get(path: str, timeout: int = 20) -> tuple[int, dict | str]:
    url = f"{API_BASE}/{path.lstrip('/')}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except Exception as e:
        return -1, str(e)[:80]


def test_events_endpoint(cat_id: int, ut_id: int | None, mode: str,
                         sample_sids: list) -> dict:
    """Test if events endpoint works for a competition."""
    results = {}

    # For national leagues (non-PL): test unique-tournament path
    if ut_id and mode != "national":
        for sid in sample_sids[:2]:
            status, data = api_get(f"unique-tournament/{ut_id}/season/{sid}/events")
            if status == 200 and isinstance(data, dict) and data.get("events"):
                results["ut_path"] = f"✅ unique-tournament/{ut_id}/season/{sid}/events"
                break
            time.sleep(0.3)

    # For national leagues: test tournament path
    if cat_id != 1:  # Already know PL (cat=1) works
        for sid in sample_sids[:2]:
            status, data = api_get(f"tournament/{cat_id}/season/{sid}/events")
            if status == 200 and isinstance(data, dict) and data.get("events"):
                results["tournament_path"] = f"✅ tournament/{cat_id}/season/{sid}/events"
                break
            time.sleep(0.3)

    # PL always works via cat=1
    if cat_id == 1:
        for sid in sample_sids[:1]:
            status, data = api_get(f"tournament/1/season/{sid}/events")
            if status == 200 and isinstance(data, dict) and data.get("events"):
                results["tournament_path"] = f"✅ tournament/1/season/{sid}/events"
                break
            time.sleep(0.3)

    return results


def discover_competition(name: str, ut_id: int | None, cat_id: int, mode: str) -> dict:
    """Discover all season IDs for one competition."""
    if ut_id is None:
        return {"status": "blocked", "reason": "ut_id unknown — need manual search"}

    # Get all seasons via unique-tournament/{ut}/seasons
    status, data = api_get(f"unique-tournament/{ut_id}/seasons")

    if status != 200 or not isinstance(data, dict):
        return {
            "status": "error",
            "http_status": status,
            "error": str(data)[:100] if isinstance(data, str) else "no data",
        }

    seasons_list = data.get("seasons", [])
    if not seasons_list:
        return {"status": "empty", "seasons": {}}

    # Build seasons dict
    seasons = {}
    for s in seasons_list:
        sid = s.get("id")
        year = s.get("year", "")
        if sid:
            seasons[str(sid)] = year

    # Get 10-year range (approximately 2015-16 to current)
    # Filter to 10-year window
    ten_yr_keys = [k for k, v in seasons.items()
                   if v and (
                       str(v).startswith("15") or str(v).startswith("16") or
                       str(v).startswith("17") or str(v).startswith("18") or
                       str(v).startswith("19") or str(v).startswith("20") or
                       str(v).startswith("21") or str(v).startswith("22") or
                       str(v).startswith("23") or str(v).startswith("24") or
                       str(v).startswith("25")
                   )]

    # Test events endpoint with recent SIDs
    sample_sids = ten_yr_keys[:3] if ten_yr_keys else list(seasons.keys())[:3]
    event_tests = test_events_endpoint(cat_id, ut_id, mode, sample_sids)

    return {
        "status": "discovered",
        "ut_id": ut_id,
        "cat_id": cat_id,
        "mode": mode,
        "seasons": seasons,
        "ten_yr_seasons": ten_yr_keys,
        "event_endpoint": event_tests if event_tests else "❌ NOT TESTED",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="SofaScore Season Discovery")
    ap.add_argument("--all", action="store_true", help="Discover all 23 competitions")
    ap.add_argument("--name", type=str, help="Discover one competition by name")
    ap.add_argument("--check-events", action="store_true", help="Also test events endpoints")
    ap.add_argument("--output", default=str(OUT_FILE), help="Output file")
    args = ap.parse_args()

    existing = {}
    if Path(args.output).exists():
        existing = json.loads(Path(args.output).read_text())

    if args.all:
        print(f"Discovering all {len(COMPETITIONS)} competitions...")
        print("=" * 60)

        for name, ut_id, cat_id, mode in COMPETITIONS:
            print(f"\n[{name}] ut={ut_id} cat={cat_id}")
            result = discover_competition(name, ut_id, cat_id, mode)

            if result.get("status") == "discovered":
                seasons = result.get("seasons", {})
                ten_yr = result.get("ten_yr_seasons", [])
                event = result.get("event_endpoint", "❌ NOT TESTED")
                print(f"  ✅ {len(seasons)} seasons, {len(ten_yr)} in 10yr window")
                print(f"  Events: {event}")
                if ten_yr:
                    print(f"  Recent: {ten_yr[:3]}")
            else:
                print(f"  ❌ {result}")

            existing[name] = result
            # Save incrementally
            Path(args.output).write_text(json.dumps(existing, ensure_ascii=False, indent=2))
            time.sleep(0.5)

        print(f"\n\nSaved to {args.output}")
        print(f"Total: {len(existing)} competitions")

    elif args.name:
        entry = next(((n, u, c, m) for n, u, c, m in COMPETITIONS if n == args.name), None)
        if entry:
            name, ut_id, cat_id, mode = entry
            result = discover_competition(name, ut_id, cat_id, mode)
            existing[args.name] = result
            print(json.dumps(result, indent=2))
            Path(args.output).write_text(json.dumps(existing, ensure_ascii=False, indent=2))
        else:
            print(f"Unknown: {args.name}")
            print(f"Options: {[n for n, *_ in COMPETITIONS]}")

    else:
        print("Use --all or --name")
        print(f"Options: {[n for n, *_ in COMPETITIONS]}")


if __name__ == "__main__":
    main()
