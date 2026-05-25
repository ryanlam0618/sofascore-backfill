#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Browser Event Discovery → Fetch Scripts Bridge
================================================
Bridges browser-discovered event IDs to existing SofaScore fetch scripts.

Intended for: Debug agent's DrissionPage event discovery output
Input:       JSON file with {competition: {season_id: [event_ids...]}}
Output:      Calls fetch_incidents.py, fetch_lineups.py, fetch_shotmap_xg.py with --event-id

No modifications to existing fetch scripts needed — they already support --event-id.

Usage:
  python3 browser_discovery_bridge.py --source discoveries_browser.json --all
  python3 browser_discovery_bridge.py --source discoveries_browser.json --scripts incidents lineups
  python3 browser_discovery_bridge.py --source discoveries_browser.json --competition "La Liga" --season-id 61643
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

WORKDIR = Path(__file__).parent

SCRIPTS = {
    "incidents": "fetch_incidents.py",
    "lineups": "fetch_lineups.py",
    "shotmap_xg": "fetch_shotmap_xg.py",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_source(path: Path) -> dict:
    """Load browser discovery output JSON."""
    if not path.exists():
        raise FileNotFoundError(f"Source file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    print(f"[INFO] Loaded {len(data)} competitions from {path}")
    return data


def build_target_list(data: dict, competition: str = None,
                      season_id: int = None) -> list[tuple[str, int, list[int]]]:
    """
    Extract (competition, season_id, event_ids) tuples from browser discovery data.
    
    Browser discovery output format:
    {
      "Premier League": {
        "61627": [11111111, 11111112, ...],  // season_id → event_ids
        "52186": [9999999, 9999998, ...],
      },
      "La Liga": {
        "61643": [22222221, ...],
      }
    }
    
    Returns list of (competition_name, int(season_id), [event_ids]).
    """
    targets = []
    competitions = [competition] if competition else list(data.keys())
    
    for comp in competitions:
        if comp not in data:
            print(f"[WARN] Competition '{comp}' not in source data")
            continue
        
        comp_data = data[comp]
        season_ids = [season_id] if season_id else list(comp_data.keys())
        
        for sid_str in season_ids:
            # Allow both string and int keys
            sid = int(sid_str)
            if sid_str not in comp_data and str(sid) not in comp_data:
                print(f"[WARN] Season {sid} not found for {comp}")
                continue
            
            event_ids = comp_data.get(sid_str) or comp_data.get(str(sid)) or []
            if not event_ids:
                print(f"[WARN] No events for {comp}/{sid}")
                continue
            
            targets.append((comp, sid, event_ids))
            print(f"[INFO] {comp} SID={sid}: {len(event_ids)} events")
    
    return targets


def run_fetch_script(script_name: str, event_ids: list[int],
                    competition: str, season_id: int,
                    use_mysql: bool = False,
                    sleep_min: float = 0.8,
                    sleep_max: float = 1.5) -> dict:
    """
    Call a fetch script with browser-discovered event IDs.
    
    All fetch scripts accept --event-id with space-separated integers:
      python3 fetch_incidents.py --event-id 11111 22222 33333
    """
    script_file = WORKDIR / SCRIPTS[script_name]
    if not script_file.exists():
        return {"status": "error", "reason": f"Script not found: {script_file}"}
    
    # Build DB/state paths
    safe_name = competition.replace(" ", "_")
    db_path = WORKDIR / "data" / "backfill_sofascore_10y" / f"{script_name}_{safe_name}.sqlite"
    state_path = WORKDIR / "data" / "backfill_sofascore_10y" / f"{script_name}_{safe_name}_state.json"
    
    # Format event IDs as space-separated string
    event_ids_str = " ".join(str(eid) for eid in event_ids)
    
    cmd = [
        sys.executable,
        str(script_file),
        "--db", str(db_path),
        "--state", str(state_path),
        "--event-id",
    ] + event_ids_str.split()
    
    # Add sleep args
    cmd.extend(["--sleep-min", str(sleep_min), "--sleep-max", str(sleep_max)])
    
    if use_mysql:
        cmd.append("--use-mysql")
    
    print(f"[RUN] {script_name} for {competition} SID={season_id}: {len(event_ids)} events")
    print(f"      DB: {db_path}")
    print(f"      CMD: {' '.join(cmd[:6])} ... [{len(event_ids)} event IDs]")
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=max(300, len(event_ids) * 2),  # At least 5min, +2s per event
            cwd=str(WORKDIR),
        )
        return {
            "status": "done",
            "returncode": result.returncode,
            "stdout": result.stdout[-500:] if result.stdout else "",
            "stderr": result.stderr[-200:] if result.stderr else "",
        }
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "reason": f"Timeout after {len(event_ids)} events"}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Bridge browser-discovered event IDs to SofaScore fetch scripts",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--source", type=Path, required=True,
                    help="Browser discovery JSON output file")
    ap.add_argument("--competition", type=str, default=None,
                    help="Filter to specific competition")
    ap.add_argument("--season-id", type=int, default=None,
                    help="Filter to specific season ID")
    ap.add_argument("--scripts", nargs="+", default=["incidents", "lineups", "shotmap_xg"],
                    choices=list(SCRIPTS.keys()),
                    help="Which fetch scripts to run (default: all 3)")
    ap.add_argument("--use-mysql", action="store_true",
                    help="Write to MySQL instead of SQLite")
    ap.add_argument("--sleep-min", type=float, default=0.8)
    ap.add_argument("--sleep-max", type=float, default=1.5)
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would run without executing")
    ap.add_argument("--batch-size", type=int, default=50,
                    help="Batch size for event IDs (default: 50)")
    
    args = ap.parse_args()
    
    print(f"=== Browser Discovery Bridge ===")
    print(f"Source: {args.source}")
    print(f"Scripts: {args.scripts}")
    print(f"Competition: {args.competition or 'ALL'}")
    print(f"Season: {args.season_id or 'ALL'}")
    print()
    
    # Load browser discovery data
    try:
        data = load_source(args.source)
    except FileNotFoundError as e:
        print(f"[ERROR] {e}")
        sys.exit(1)
    
    # Extract targets
    targets = build_target_list(data, args.competition, args.season_id)
    
    if not targets:
        print("[ERROR] No valid targets found")
        sys.exit(1)
    
    print(f"\n[INFO] {len(targets)} competition/season combinations to process")
    print()
    
    results = {}
    
    for comp, sid, event_ids in targets:
        print(f"\n{'='*60}")
        print(f"Processing: {comp} SID={sid} ({len(event_ids)} events)")
        print(f"{'='*60}")
        
        comp_results = {}
        
        for script_name in args.scripts:
            if script_name not in SCRIPTS:
                continue
            
            # Split into batches if needed
            batches = [
                event_ids[i:i + args.batch_size]
                for i in range(0, len(event_ids), args.batch_size)
            ]
            
            batch_results = []
            for batch_idx, batch in enumerate(batches):
                print(f"\n  [{script_name}] Batch {batch_idx + 1}/{len(batches)}: {len(batch)} events")
                
                if args.dry_run:
                    print(f"    [DRY] Would run: {SCRIPTS[script_name]} --event-id {batch[:3]}... ({len(batch)} total)")
                    batch_results.append({"status": "dry_run", "batch_size": len(batch)})
                    continue
                
                res = run_fetch_script(
                    script_name, batch, comp, sid,
                    use_mysql=args.use_mysql,
                    sleep_min=args.sleep_min,
                    sleep_max=args.sleep_max,
                )
                batch_results.append(res)
                print(f"    [RESULT] {res.get('status', 'unknown')}")
                if res.get("returncode") and res["returncode"] != 0:
                    print(f"    [STDERR] {res.get('stderr', 'N/A')[:200]}")
            
            comp_results[script_name] = batch_results
        
        results[f"{comp}/{sid}"] = comp_results
    
    # Summary
    print(f"\n\n{'='*60}")
    print("=== SUMMARY ===")
    print(f"{'='*60}")
    
    total_events = sum(len(evs) for _, _, evs in targets)
    print(f"Total events: {total_events}")
    print(f"Competitions: {len(targets)}")
    print(f"Scripts: {args.scripts}")
    print()
    
    for key, comp_res in results.items():
        for script, batches in comp_res.items():
            statuses = [b.get("status", "?") for b in batches]
            print(f"  {key}/{script}: {statuses}")
    
    # Write results
    result_file = WORKDIR / "data" / "backfill_sofascore_10y" / "browser_discovery_results.json"
    result_file.parent.mkdir(parents=True, exist_ok=True)
    result_data = {
        "timestamp": now_iso(),
        "source": str(args.source),
        "scripts": args.scripts,
        "targets": [(c, s, len(e)) for c, s, e in targets],
        "total_events": total_events,
        "results": results,
    }
    result_file.write_text(json.dumps(result_data, ensure_ascii=False, indent=2))
    print(f"\n[SAVE] Results → {result_file}")


if __name__ == "__main__":
    main()