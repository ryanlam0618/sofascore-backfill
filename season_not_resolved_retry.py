#!/usr/bin/env python3
"""
season_not_resolved_retry.py — retry the 25 season-not-resolved cup-seasons
(5 cups x 20/21-24/25) via /unique-tournament/{ut}/seasons.

Kris 2026-09-25 09:36 GMT+8 "試多次" — multiple attempts per cup (3 outer
attempts, fresh HealthProxyPool per attempt so health/burst state resets and
different IPs get picked). Reuses the EXACT production fetch path
(HealthProxyPool + fetch_with_retry + curl_cffi chrome124) from
gen4_phaseB_2021-25_backfill.py — read-only probe; NO DB writes here.
Season row seeding happens only after resolution results are reviewed.

Output: data/season_resolve_retry_20260925.json
"""

import asyncio
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Import production module (module name contains hyphens -> importlib)
_spec = importlib.util.spec_from_file_location(
    "phaseB_2125", ROOT / "gen4_phaseB_2021-25_backfill.py")
mod = importlib.util.module_from_spec(_spec)
sys.modules["phaseB_2125"] = mod  # dataclass 需要 module 註冊入 sys.modules，否則 exec_module fail
_spec.loader.exec_module(mod)

# The 25 gaps: 5 cups x 5 seasons (20/21-24/25) — 2021-25 tranche season-not-resolved
TARGETS = [
    {"name": "DFB Pokal", "ut_id": 217},
    {"name": "J.League Cup", "ut_id": 101},
    {"name": "Emperor's Cup", "ut_id": 323},
    {"name": "Australia Cup", "ut_id": 1786},
    {"name": "Chinese FA Cup", "ut_id": 882},
]
MISSING_LABELS = ["20/21", "21/22", "22/23", "23/24", "24/25"]
ATTEMPTS_PER_COMP = 3  # 試多次 — fresh pool per attempt
SLEEP_BETWEEN_ATTEMPTS_S = 5


def label_year_start(label: str) -> int:
    return int(label[:2]) + 2000


def match_label(seasons, label: str):
    """Same matching logic as production resolve_season_id (exact + substring fallback)."""
    if not seasons:
        return None, None
    ystart = label_year_start(label)
    for cand in (label, str(ystart)):
        for s in seasons:
            if str(s.get("year")) == cand:
                return s.get("id"), str(s.get("year"))
    for s in seasons:
        y = str(s.get("year") or "")
        if y.startswith(str(ystart)) or label in y:
            return s.get("id"), y
    return None, None


async def fetch_seasons_once(ut_id: int, name: str, attempt_no: int) -> dict:
    pool = mod.HealthProxyPool(mod.POOL_FILE)  # fresh per attempt -> reset health + burst
    body, status, error, ip_used, tries = await mod.fetch_with_retry(
        "/unique-tournament/%s/seasons" % ut_id, pool, max_retries=2,
        comp_name=name, ut_id=ut_id, season_id=0,
    )
    seasons = []
    if status == 200 and isinstance(body, dict):
        seasons = body.get("seasons", []) or []
    return {"attempt": attempt_no, "status": status, "error": error, "ip": ip_used,
            "tries": tries, "n_seasons": len(seasons), "seasons": seasons}


async def main() -> None:
    results = {"timestamp": datetime.now().isoformat(),
               "phase_tag": "season-not-resolved-retry",
               "attempts_per_comp": ATTEMPTS_PER_COMP,
               "missing_labels": MISSING_LABELS,
               "targets": []}

    for t in TARGETS:
        name, ut_id = t["name"], t["ut_id"]
        print("\n" + "=" * 60)
        print("  %s (ut_id=%s)" % (name, ut_id))
        print("=" * 60)

        entry = {"name": name, "ut_id": ut_id, "attempts": [], "resolved": {}}
        all_seasons = {}  # merge unique by year across attempts

        for a in range(1, ATTEMPTS_PER_COMP + 1):
            r = await fetch_seasons_once(ut_id, name, a)
            entry["attempts"].append({k: v for k, v in r.items() if k != "seasons"})
            for s in r["seasons"]:
                all_seasons[str(s.get("year"))] = s.get("id")
            print("  attempt %d: HTTP %s | seasons=%d | ip=%s | err=%s" % (
                a, r["status"], r["n_seasons"], r["ip"], r["error"]))
            if a < ATTEMPTS_PER_COMP:
                await asyncio.sleep(SLEEP_BETWEEN_ATTEMPTS_S)

        print("  merged seasons list (%d):" % len(all_seasons))
        for y in sorted(all_seasons):
            print("    %s -> %s" % (y, all_seasons[y]))

        season_objs = [{"year": y, "id": sid} for y, sid in all_seasons.items()]
        for label in MISSING_LABELS:
            sid, matched = match_label(season_objs, label)
            entry["resolved"][label] = {"season_id": sid, "matched_year": matched}
            print("  %s -> %s %s" % (label, sid, "✓ RESOLVED" if sid else "✗ not resolved"))

        results["targets"].append(entry)

    out = ROOT / "data" / "season_resolve_retry_20260925.json"
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print("\nSaved: %s" % out)

    total_resolved = sum(1 for t in results["targets"]
                         for v in t["resolved"].values() if v["season_id"])
    print("TOTAL resolved: %d / 25" % total_resolved)


if __name__ == "__main__":
    asyncio.run(main())
