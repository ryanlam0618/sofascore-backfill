#!/usr/bin/env python3
"""
SofaScore 10-Year Backfill Orchestrator
========================================
Drives backfill of incidents, lineups, shotmaps for 23 competitions over 10 years.

Prerequisites (run in order):
  1. python3 discover_seasons.py --all        # Discover season IDs for all 23 competitions
  2. python3 orchestrator.py --check-config     # Validate discoveries.json

Running:
  python3 orchestrator.py --all              # Run full 10-year backfill (ALL competitions)
  python3 orchestrator.py --competition "Premier League"  # Run single competition
  python3 orchestrator.py --resume           # Resume from last checkpoint
  python3 orchestrator.py --status           # Show progress summary

Data scripts available (11 types):
  fetch_incidents.py        → sofascore_incidents (goal, card, sub, etc.)
  fetch_shotmap_xg.py        → shotmap aggregates (xg sums per match)
  fetch_shotmap_details.py   → per-shot rows (depends on shotmap_xg.sqlite)
  fetch_lineups.py           → match lineups (starting XI, subs)
  fetch_related_matches.py   → H2H aggregated stats (per event)
  fetch_statistics.py        → match stats (possession, shots, etc.)
  fetch_standings.py          → league standings + team IDs
  fetch_player_stats.py       → player season stats
  fetch_managers.py           → manager profiles (needs team IDs)
  fetch_referees.py           → referee profiles (needs event IDs)
  fetch_attendance.py         → match attendance (needs event IDs)
  fetch_team_rankings.py      → team rankings (needs team IDs)

Three phases:
  Phase 1 (category+season enumeration): incidents, lineups, shotmap_xg
  Phase 2 (event-based): statistics, related_matches, attendance, referees
  Phase 3 (team-based): standings, managers, team_rankings

State / logs:
  DATA_DIR/           = data/backfill_sofascore_10y/
  LOG_DIR/            = data/logs/
  STATE_FILE          = data/backfill_sofascore_10y/orchestrator_state.json
  DISCOVERIES_FILE    = discoveries.json
  COMPETITIONS_FILE   = competitions_10y.yaml
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import subprocess
import sys
import time
import threading

# ── Concurrency control ──────────────────────────────────────────────────────────
_MAX_CONCURRENT = 20  # Max simultaneous workers to avoid MySQL connection exhaustion
_SPAWN_SEM = threading.Semaphore(_MAX_CONCURRENT)
_ACTIVE_WORKERS = 0
_ACTIVE_LOCK = threading.Lock()
import urllib.request
import datetime
from pathlib import Path
from typing import Optional

# ── Config ────────────────────────────────────────────────────────────────────
WORKDIR = Path(__file__).parent
DATA_DIR = WORKDIR / "data" / "backfill_sofascore_10y"
LOG_DIR = WORKDIR / "data" / "logs"
DISCOVERIES_FILE = WORKDIR / "discoveries.json"
COMPETITIONS_FILE = WORKDIR / "competitions_10y.yaml"

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

API_BASE = "https://www.sofascore.com/api/v1"

# ── Competition season mapping (known + unknown) ─────────────────────────────
# Format: name → {cat_id, ut_id, season_ids: [sid_2024_25, sid_2025_26, ...]}
# Only PL is confirmed. All others need discovery first.
COMPETITION_MAPPING = {
    "Premier League": {
        "cat_id": 1,
        "ut_id": 17,
        "confirmed_sids": {10356: "15/16", 11733: "16/17", 13380: "17/18", 17359: "18/19", 23776: "19/20", 29415: "20/21", 37036: "21/22", 41886: "22/23", 52186: "23/24", 61627: "24/25", 76986: "25/26"},  # 2015-16 to 2025-26 ✅
        "needs_discovery": False,
    },
    "La Liga": {
        "cat_id": 32,
        "ut_id": 8,
        "confirmed_sids": {},
        "needs_discovery": True,
        "notes": "cat=8 confirmed, season IDs unknown",
    },
    "Serie A": {
        "cat_id": 31,
        "ut_id": 23,
        "confirmed_sids": {},
        "needs_discovery": True,
    },
    "Bundesliga": {
        "cat_id": 30,
        "ut_id": 9,
        "confirmed_sids": {},
        "needs_discovery": True,
    },
    "Ligue 1": {
        "cat_id": 7,
        "ut_id": 34,
        "confirmed_sids": {},
        "needs_discovery": True,
    },
    "J1 League": {
        "cat_id": 52,
        "ut_id": 196,
        "confirmed_sids": {},
        "needs_discovery": True,
    },
    "K League 1": {
        "cat_id": 291,
        "ut_id": 410,
        "confirmed_sids": {},
        "needs_discovery": True,
    },
    "A-League Men": {
        "cat_id": 34,
        "ut_id": 136,
        "confirmed_sids": {},
        "needs_discovery": True,
    },
    "Chinese Super League": {
        "cat_id": 99,
        "ut_id": 649,
        "confirmed_sids": {},
        "needs_discovery": True,
    },
    "UCL": {
        "cat_id": 1465,
        "ut_id": 7,
        "confirmed_sids": {},
        "needs_discovery": True,
        "notes": "unique-tournament path may be required",
    },
    "UEL": {
        "cat_id": 1465,
        "ut_id": 679,
        "confirmed_sids": {},
        "needs_discovery": True,
    },
    "UECL": {
        "cat_id": 1465,
        "ut_id": 17015,
        "confirmed_sids": {},
        "needs_discovery": True,
    },
    "AFC Champions League": {
        "cat_id": 1467,
        "ut_id": 463,
        "confirmed_sids": {},
        "needs_discovery": True,
    },
    "FA Cup": {
        "cat_id": 1,
        "ut_id": 19,
        "confirmed_sids": {},
        "needs_discovery": True,
    },
    "EFL Cup": {
        "cat_id": 1,
        "ut_id": 21,
        "confirmed_sids": {},
        "needs_discovery": True,
    },
    "Copa del Rey": {
        "cat_id": 32,
        "ut_id": 329,
        "confirmed_sids": {},
        "needs_discovery": True,
    },
    "Coppa Italia": {
        "cat_id": 31,
        "ut_id": 328,
        "confirmed_sids": {},
        "needs_discovery": True,
    },
    "Coupe de France": {
        "cat_id": 7,
        "ut_id": 335,
        "confirmed_sids": {},
        "needs_discovery": True,
    },
    "DFB Pokal": {
        "cat_id": 30,
        "ut_id": 217,
        "confirmed_sids": {},
        "needs_discovery": True,
    },
    "J.League Cup": {
        "cat_id": 52,
        "ut_id": 101,
        "confirmed_sids": {},
        "needs_discovery": True,
    },
    "Emperor's Cup": {
        "cat_id": 52,
        "ut_id": 323,
        "confirmed_sids": {},
        "needs_discovery": True,
    },
    "Australia Cup": {
        "cat_id": 34,
        "ut_id": 1786,
        "confirmed_sids": {},
        "needs_discovery": True,
    },
    "Chinese FA Cup": {
        "cat_id": 99,
        "ut_id": None,
        "confirmed_sids": {},
        "needs_discovery": True,
        "blocker": "ut_id not found in search",
    },
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def log(msg: str, file=None):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    if file:
        with open(file, "a") as f:
            f.write(line + "\n")


def load_state() -> dict:
    state_path = DATA_DIR / "orchestrator_state.json"
    if state_path.exists():
        return json.loads(state_path.read_text())
    return {
        "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "competitions": {},
    }


def save_state(state: dict) -> None:
    state["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    path = DATA_DIR / "orchestrator_state.json"
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def get_log_path(competition: str, script: str) -> Path:
    lp = LOG_DIR / f"{competition.replace(' ', '_')}_{script}.log"
    lp.parent.mkdir(parents=True, exist_ok=True)
    return lp


def run_fetch(script: str, competition: str, cat_id: int, season_id: int,
              limit: int = 0, db_suffix: str = "") -> bool:
    """
    Run a fetch script as a background subprocess.
    Uses SCRIPT_ARG_MAP to only pass flags the script supports.
    Returns True if started successfully.
    """
    # Derive script name (strip 'fetch_' prefix if present)
    script_key = script.replace("fetch_", "").replace(".py", "")
    data_name = script_key

    db_path = DATA_DIR / f"{data_name}_{competition.replace(' ', '_')}{db_suffix}.sqlite"
    state_path = DATA_DIR / f"{data_name}_{competition.replace(' ', '_')}_state.json"

    log_path = get_log_path(competition, script)

    # Build base cmd — SCRIPT_ARG_MAP filters out unsupported flags
    cmd = [
        sys.executable,
        str(WORKDIR / script),
        "--db", str(db_path),
        "--state", str(state_path),
        "--sleep-min", "0.8",
        "--sleep-max", "1.5",
        "--use-mysql",
    ]

    # Add cat/season flags only if the script supports them
    if "category-id" in SCRIPT_ARG_MAP.get(script_key, frozenset()):
        cmd.extend(["--category-id", str(cat_id)])
    if "season-id" in SCRIPT_ARG_MAP.get(script_key, frozenset()):
        cmd.extend(["--season-id", str(season_id)])
    if "checkpoint-every" in SCRIPT_ARG_MAP.get(script_key, frozenset()):
        cmd.extend(["--checkpoint-every", "50"])
    if limit > 0:
        cmd.extend(["--limit", str(limit)])

    safe_cmd = _filter_cmd(cmd, script_key)
    cmd_str = " ".join(str(c) for c in safe_cmd)

    log(f"[RUN] {script} for {competition} (SID={season_id})", log_path)
    log(f"[RUN] Command: {cmd_str}", log_path)

    pid = run_script_sequential(safe_cmd, log_path, script_key, context=f"{script}/{competition}/SID={season_id}")
    if pid:
        log(f"[PID] {pid} started for {competition}/{script}", log_path)
        return True
    log(f"[ERROR] Failed to start {script}: run_script_sequential returned None", log_path)
    return False


# ── Per-script argument support mapping ────────────────────────────────────────
# Keys = flag names (without leading --)
# Values = frozenset of flag names that the script accepts.
# Used to build safe cmd lists so we never pass unsupported flags.
SCRIPT_ARG_MAP: dict[str, frozenset[str]] = {
    "incidents":       frozenset({"db","state","sleep-min","sleep-max","checkpoint-every",
                                  "limit","event-id","category-id","season-id","past-only","use-mysql"}),
    "lineups":         frozenset({"db","state","sleep-min","sleep-max","checkpoint-every",
                                  "limit","event-id","category-id","season-id","past-only",
                                  "use-mysql","retry-403","retry-errors"}),
    "shotmap_xg":      frozenset({"db","state","sleep-min","sleep-max","checkpoint-every",
                                  "limit","event-id","category-id","season-id","past-only",
                                  "use-mysql","refetch-zero"}),
    "statistics":     frozenset({"db","state","sleep-min","sleep-max",
                                  "limit","event-id","category-id","season-id","past-only",
                                  "retry-errors"}),
    "related_matches": frozenset({"db","state","sleep-min","sleep-max",
                                  "limit","event-id","source-league","use-mysql"}),
    "attendance":      frozenset({"db","state","event-id","sleep-min","sleep-max",
                                  "use-mysql","limit","retry-errors"}),
    "referees":        frozenset({"db","state","event-id","referee-id","sleep-min","sleep-max",
                                  "use-mysql","limit"}),
    "standings":       frozenset({"db","state","sleep-min","sleep-max","category-id",
                                  "all","limit","use-mysql"}),
    "team_rankings":   frozenset({"db","state","sleep-min","sleep-max","limit","team-id",
                                  "use-mysql","retry-errors"}),
    "shotmap_details": frozenset({"db","state","sleep-min","sleep-max","checkpoint-every",
                                  "limit","source-db","use-mysql"}),
    "managers":        frozenset({"db","state","team-id","manager-id","sleep-min","sleep-max",
                                  "use-mysql","limit"}),
    "player_stats":    frozenset({"db","state","sleep-min","sleep-max","category-id",
                                  "stat-type","all-stats","all","limit",
                                  "no-player-stats","use-mysql"}),
}


def _filter_cmd(cmd: list, script: str) -> list:
    """
    Return a cmd list containing only flags that |script| accepts.
    cmd is a flat list like ["--db", "x.db", "--checkpoint-every", "50", ...]
    Flags not in SCRIPT_ARG_MAP[script] are dropped.
    """
    supported = SCRIPT_ARG_MAP.get(script, frozenset())
    filtered = []
    i = 0
    while i < len(cmd):
        arg = cmd[i]
        if arg.startswith("--"):
            flag = arg[2:]
            if flag in supported:
                filtered.append(arg)
                # consume the next element if it's a value (not a flag)
                if i + 1 < len(cmd) and not cmd[i + 1].startswith("--"):
                    filtered.append(cmd[i + 1])
                    i += 1
            # else: flag not supported, skip it and its value if present
        else:
            filtered.append(arg)
        i += 1
    return filtered


def _wait_and_release(pid: int, log_path: Path, context: str, sem: threading.Semaphore):
    """Background thread: wait for subprocess, release semaphore, log completion."""
    global _ACTIVE_WORKERS
    try:
        # Poll the process until it exits
        import subprocess as _sub
        while True:
            ret = _sub.call(["kill", "-0", str(pid)], stderr=_sub.DEVNULL)
            if ret != 0:
                break
            time.sleep(2)
    except Exception:
        pass
    finally:
        sem.release()
        with _ACTIVE_LOCK:
            global _ACTIVE_WORKERS
            _ACTIVE_WORKERS -= 1
        try:
            with open(log_path, "a") as lf:
                lf.write(f"[DONE] Background worker PID={pid} finished\n")
        except Exception:
            pass


def run_script_sequential(
    cmd: list,
    log_path: Path,
    script: str,
    context: str = "",
) -> int | None:
    """
    Run a subprocess with retry on transient SofaScore / DB errors.
    Uses a semaphore to limit concurrent workers (default 20).

    Retry policy:
      HTTP 403 (anti-bot)      → up to 3 retries, exponential backoff 2s/4s/8s
      HTTP 404 (not found)     → up to 2 retries, backoff 2s/4s
      "database is locked"     → up to 3 retries, backoff 2s/4s/8s
      Any other error          → no retry

    Returns the process PID on success, None on final failure.
    """
    global _ACTIVE_WORKERS, _ACTIVE_LOCK
    MAX_RETRIES = 3
    BACKOFF = [2, 4, 8]  # seconds

    safe_cmd = _filter_cmd(cmd, script)
    safe_cmd_str = " ".join(str(c) for c in safe_cmd)

    for attempt in range(MAX_RETRIES + 1):
        # ── Acquire semaphore slot ──────────────────────────────────────
        acquired = _SPAWN_SEM.acquire(timeout=300)  # wait up to 5 min
        if not acquired:
            log(f"[ERROR] Timeout waiting for concurrency slot after 5 min for {context}", log_path)
            return None

        with _ACTIVE_LOCK:
            _ACTIVE_WORKERS += 1
        log(f"[RUN] {context} (active={_ACTIVE_WORKERS}/{_MAX_CONCURRENT}) → {safe_cmd_str}", log_path)

        try:
            with open(log_path, "a") as lf:
                proc = subprocess.Popen(
                    safe_cmd,
                    stdout=lf,
                    stderr=subprocess.STDOUT,
                    cwd=str(WORKDIR),
                )
        except Exception as e:
            _SPAWN_SEM.release()
            with _ACTIVE_LOCK:
                _ACTIVE_WORKERS -= 1
            log(f"[ERROR] Failed to start subprocess: {e} | cmd={safe_cmd_str}", log_path)
            return None

        pid = proc.pid

        # Check if process exited immediately (fast failure)
        rc = proc.poll()
        if rc is not None:
            # Already exited — read tail, classify error
            _SPAWN_SEM.release()
            with _ACTIVE_LOCK:
                _ACTIVE_WORKERS -= 1
            try:
                with open(log_path, "rb") as lf:
                    lf.seek(max(0, lf.tell() - 2000))
                    tail = lf.read().decode("utf-8", errors="replace")
            except Exception:
                tail = ""

            if "403 Forbidden" in tail or "403" in tail or "access_denied" in tail.lower():
                if attempt < MAX_RETRIES:
                    retry_after = BACKOFF[attempt] if attempt < len(BACKOFF) else BACKOFF[-1]
                    log(f"[RETRY] HTTP 403 (attempt {attempt+1}/{MAX_RETRIES+1}), sleeping {retry_after}s", log_path)
                    time.sleep(retry_after)
                    continue
            elif "404" in tail or "not found" in tail.lower():
                if attempt < 2:
                    retry_after = BACKOFF[attempt] if attempt < len(BACKOFF) else BACKOFF[-1]
                    log(f"[RETRY] HTTP 404 (attempt {attempt+1}/3), sleeping {retry_after}s", log_path)
                    time.sleep(retry_after)
                    continue
                else:
                    log(f"[FAIL] HTTP 404 persisted after retries, skipping", log_path)
                    return None
            elif "database is locked" in tail.lower():
                if attempt < MAX_RETRIES:
                    retry_after = BACKOFF[attempt] if attempt < len(BACKOFF) else BACKOFF[-1]
                    log(f"[RETRY] DB locked (attempt {attempt+1}/{MAX_RETRIES+1}), sleeping {retry_after}s", log_path)
                    time.sleep(retry_after)
                    continue
            else:
                log(f"[FAIL] Subprocess exited with rc={rc}, not retrying → {safe_cmd_str}", log_path)
                return None

        # Process is still running — spawn a background thread to release semaphore later
        t = threading.Thread(target=_wait_and_release,
                             args=(pid, log_path, context, _SPAWN_SEM),
                             daemon=True)
        t.start()
        return pid

    log(f"[FAIL] All retries exhausted for {context}", log_path)
    return None


def load_discoveries() -> dict:
    """Load discovered season IDs from discoveries.json."""
    if DISCOVERIES_FILE.exists():
        return json.loads(DISCOVERIES_FILE.read_text())
    return {}


def load_competition_config() -> dict:
    """Load competition configuration."""
    # Try YAML first
    if COMPETITIONS_FILE.exists():
        try:
            import yaml
            with open(COMPETITIONS_FILE) as f:
                data = yaml.safe_load(f)
            return {c["name"]: c for c in data.get("competitions", [])}
        except ImportError:
            pass  # yaml not available
    return COMPETITIONS_FILE


# ── Backfill Logic ────────────────────────────────────────────────────────────

def backfill_competition(competition: str, dry_run: bool = False,
                        limit_per_script: int = 0,
                        from_year: int = 2015) -> dict:
    """
    Backfill one competition's 10-year data.
    Scripts: incidents, lineups, shotmap_xg, shotmap_details (depends on shotmap_xg).

    For competitions needing discovery, checks discoveries.json first.
    """
    cfg = COMPETITION_MAPPING.get(competition)
    if not cfg:
        return {"status": "error", "reason": f"Unknown competition: {competition}"}

    state = load_state()
    comp_state = state["competitions"].get(competition, {
        "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "scripts": {},
    })

    if cfg["needs_discovery"] and not cfg["confirmed_sids"]:
        discoveries = load_discoveries()
        disc = discoveries.get(competition, {})
        if not disc.get("seasons"):
            return {
                "status": "blocked",
                "reason": f"{competition} needs season discovery. Run: python3 discover_seasons.py --name '{competition}'",
                "action": "discover_seasons",
            }

    cat_id = cfg["cat_id"]
    ut_id = cfg["ut_id"]

    # Filter to 2015-16 onwards (configurable via --from-year)
    def season_year(slug: str) -> int:
        """Extract start year from slug for filtering.
        
        Format examples:
        - European: '15/16' -> 2015, '99/00' -> 1999, '02/03' -> 2002
        - Asian:    '2015' -> 2015, '2008' -> 2008
        """
        import re
        # European: '15/16' or '99/00'
        m = re.match(r'^(\d{2})/(\d{2})$', slug)
        if m:
            yy = int(m.group(1))  # first 2 digits
            # < 50 -> 20xx (e.g. '15' -> 2015), >= 50 -> 19xx (e.g. '99' -> 1999)
            return 2000 + yy if yy < 50 else 1900 + yy
        # Asian: '2015' or '2008'
        m = re.match(r'^(\d{4})', slug)
        if m:
            return int(m.group(1))
        return 0

    if cfg["needs_discovery"] or not cfg["confirmed_sids"]:
        discoveries = load_discoveries()
        disc = discoveries.get(competition, {})
        seasons = disc.get("seasons", {})
        # discoveries.json format: {sid_str: year_str}
        # Filter to from_year (default 2015)
        season_ids_to_process = sorted(
            int(k) for k, v in seasons.items()
            if k is not None and season_year(v) >= from_year
        )
    else:
        # confirmed_sids: filter by from_year
        cfg_seasons = cfg.get("confirmed_sids", {})
        season_ids_to_process = sorted(k for k in cfg_seasons.keys()
                                        if season_year(cfg_seasons[k]) >= from_year)

    if not season_ids_to_process:
        return {
            "status": "blocked",
            "reason": f"{competition}: no season IDs. Run: python3 discover_seasons.py --name '{competition}'",
            "action": "discover_seasons",
        }

    results = {}

    # ── Phase 1: Category+Season enumeration scripts ──────────────────────
    # IMPORTANT: Process ONE season at a time to avoid IP overload.
    # Nested loop: for each season → run ALL phase1 scripts sequentially.
    # This ensures: PL-SID1 incidents→lineups→shotmap→stats→related, THEN PL-SID2...
    phase1_scripts = [
        ("incidents",      "incidents"),
        ("lineups",        "lineups"),
        ("shotmap_xg",     "shotmap_xg"),
        ("statistics",     "statistics"),
        ("related_matches","related_matches"),
    ]
    supports_cat_season = {"incidents", "lineups", "shotmap_xg", "statistics"}
    supports_checkpoint = {"incidents", "lineups", "shotmap_xg", "statistics"}

    if dry_run:
        for sid in season_ids_to_process:
            for script, data_name in phase1_scripts:
                script_path = WORKDIR / f"fetch_{script}.py"
                if not script_path.exists():
                    continue
                log(f"[DRY] {script}/{competition}/SID={sid}")
        results["phase1"] = {"status": "dry_run", "seasons": season_ids_to_process,
                              "scripts": [s for s, _ in phase1_scripts]}

    else:
        # Run one season at a time, all scripts for that season before next
        for sid in season_ids_to_process:
            log(f"[PHASE1] Starting season {sid} for {competition} ({len(phase1_scripts)} scripts)")
            for script, data_name in phase1_scripts:
                script_path = WORKDIR / f"fetch_{script}.py"
                if not script_path.exists():
                    log(f"[SKIP] fetch_{script}.py not found")
                    continue

                db_path = DATA_DIR / f"{data_name}_{competition.replace(' ', '_')}.sqlite"
                state_path = DATA_DIR / f"{data_name}_{competition.replace(' ', '_')}_state.json"

                log_path = get_log_path(competition, f"{script}_sid{sid}")
                cmd = [
                    sys.executable, str(script_path),
                    "--db", str(db_path),
                    "--state", str(state_path),
                    "--sleep-min", "0.8",
                    "--sleep-max", "1.5",
                    "--use-mysql",
                ]
                if script in supports_cat_season:
                    cmd.extend(["--category-id", str(cat_id), "--season-id", str(sid)])
                if script in supports_checkpoint:
                    cmd.extend(["--checkpoint-every", "50"])
                if limit_per_script > 0:
                    cmd.extend(["--limit", str(limit_per_script)])

                pid = run_script_sequential(cmd, log_path, script,
                                            context=f"{script}/{competition}/SID={sid}")
                if pid:
                    log(f"[PID {pid}] {script}/{competition}/SID={sid} started")
                else:
                    log(f"[ERROR] {script}/{competition}/SID={sid}: run_script_sequential failed")

            log(f"[PHASE1] Season {sid} for {competition} complete")

        results["phase1"] = {"status": "completed", "seasons": season_ids_to_process}

    # ── shotmap_details: depends on shotmap_xg ──────────────────────────
    shotmap_db = DATA_DIR / f"shotmap_xg_{competition.replace(' ', '_')}.sqlite"
    if shotmap_db.exists() and not dry_run:
        details_state = DATA_DIR / f"shotmap_details_{competition.replace(' ', '_')}_state.json"
        details_log = get_log_path(competition, "shotmap_details")
        details_cmd = [
            sys.executable, str(WORKDIR / "fetch_shotmap_details.py"),
            "--source-db", str(shotmap_db),
            "--state", str(details_state),
            "--sleep-min", "0.5", "--sleep-max", "1.0",
            "--checkpoint-every", "100",
            "--use-mysql",
        ]
        if limit_per_script > 0:
            details_cmd.extend(["--limit", str(limit_per_script)])
        # Filter to supported flags so we never pass e.g. --checkpoint-every
        # to a script that doesn't understand it.
        safe_details_cmd = _filter_cmd(details_cmd, "shotmap_details")
        pid = run_script_sequential(safe_details_cmd, details_log, "shotmap_details",
                                    context=f"shotmap_details/{competition}")
        if pid:
            results["shotmap_details"] = {"status": "started", "pid": pid, "log": str(details_log)}
        else:
            results["shotmap_details"] = {"status": "error", "reason": "run_script_sequential returned None"}
    elif dry_run and shotmap_db.exists():
        results["shotmap_details"] = {"status": "dry_run", "phase": "dep-1", "depends_on": "shotmap_xg"}

    # ── Phase 2: Event-based scripts ─────────────────────────────────────
    # IMPORTANT: same one-season-at-a-time pattern as Phase 1.
    phase2_cat_season = [
        ("attendance", "attendance"),
        ("referees",  "referees"),
    ]

    if dry_run:
        for sid in season_ids_to_process:
            for script, data_name in phase2_cat_season:
                script_path = WORKDIR / f"fetch_{script}.py"
                if script_path.exists():
                    log(f"[DRY] {script}/{competition}/SID={sid}")
        results["phase2"] = {"status": "dry_run", "seasons": season_ids_to_process}
    else:
        for sid in season_ids_to_process:
            log(f"[PHASE2] Starting season {sid} for {competition} ({len(phase2_cat_season)} scripts)")
            for script, data_name in phase2_cat_season:
                script_path = WORKDIR / f"fetch_{script}.py"
                if not script_path.exists():
                    log(f"[SKIP] fetch_{script}.py not found")
                    continue

                db_path = DATA_DIR / f"{data_name}_{competition.replace(' ', '_')}.sqlite"
                state_path = DATA_DIR / f"{data_name}_{competition.replace(' ', '_')}_state.json"

                log_path = get_log_path(competition, f"{script}_sid{sid}")
                cmd = [
                    sys.executable, str(script_path),
                    "--db", str(db_path),
                    "--state", str(state_path),
                    "--sleep-min", "0.8",
                    "--sleep-max", "1.5",
                    "--use-mysql",
                ]
                if limit_per_script > 0:
                    cmd.extend(["--limit", str(limit_per_script)])
                if "category-id" in SCRIPT_ARG_MAP.get(script, frozenset()):
                    cmd.extend(["--category-id", str(cat_id)])
                if "season-id" in SCRIPT_ARG_MAP.get(script, frozenset()):
                    cmd.extend(["--season-id", str(sid)])
                if "checkpoint-every" in SCRIPT_ARG_MAP.get(script, frozenset()):
                    cmd.extend(["--checkpoint-every", "50"])

                safe_cmd = _filter_cmd(cmd, script)
                pid = run_script_sequential(safe_cmd, log_path, script,
                                            context=f"{script}/{competition}/SID={sid}")
                if pid:
                    log(f"[PID {pid}] {script}/{competition}/SID={sid} started")
                else:
                    log(f"[ERROR] {script}/{competition}/SID={sid}: run_script_sequential failed")

            log(f"[PHASE2] Season {sid} for {competition} complete")

        results["phase2"] = {"status": "completed", "seasons": season_ids_to_process}

    # ── Phase 3: Team-based scripts ──────────────────────────────────────
    # standings: runs once per competition (no season loop), fetches all seasons
    # managers/team_rankings: need team IDs from standings output
    phase3_scripts = [
        ("standings", "standings"),
    ]

    for script, data_name in phase3_scripts:
        script_path = WORKDIR / f"fetch_{script}.py"
        if not script_path.exists():
            results[script] = {"status": "skipped", "reason": "script not found"}
            continue

        db_path = DATA_DIR / f"{data_name}_{competition.replace(' ', '_')}.sqlite"
        state_path = DATA_DIR / f"{data_name}_{competition.replace(' ', '_')}_state.json"

        if dry_run:
            results[script] = {"status": "dry_run", "phase": 3, "ut_id": ut_id, "cat_id": cat_id}
            log(f"[DRY] {script} for {competition}: ut_id={ut_id} cat_id={cat_id}")
            continue

        log_path = get_log_path(competition, f"{script}")
        cmd = [
            sys.executable, str(script_path),
            "--db", str(db_path),
            "--state", str(state_path),
            "--category-id", str(cat_id),
            "--sleep-min", "0.8",
            "--sleep-max", "1.5",
            "--use-mysql",
        ]
        if limit_per_script > 0:
            cmd.extend(["--limit", str(limit_per_script)])

        # Filter flags to only what this script supports (e.g. standings has no --limit)
        safe_cmd = _filter_cmd(cmd, script)
        pid = run_script_sequential(safe_cmd, log_path, script,
                                    context=f"{script}/{competition}")
        if pid:
            results[script] = {"status": "started", "pid": pid, "phase": 3}
        else:
            results[script] = {"status": "error", "reason": "run_script_sequential returned None"}

    # team_rankings: post-process from standings team IDs
    team_rankings_path = WORKDIR / "fetch_team_rankings.py"
    if team_rankings_path.exists():
        # Extract team IDs from standings SQLite
        standings_db = DATA_DIR / f"standings_{competition.replace(' ', '_')}.sqlite"
        team_ids = []
        if standings_db.exists():
            try:
                conn = sqlite3.connect(standings_db)
                cur = conn.cursor()
                cur.execute("SELECT DISTINCT team_id FROM standings WHERE team_id IS NOT NULL LIMIT 200")
                team_ids = [r[0] for r in cur.fetchall()]
                conn.close()
            except Exception as e:
                log(f"[WARN] Could not read team IDs from {standings_db}: {e}")

        if team_ids:
            if dry_run:
                results["team_rankings"] = {
                    "status": "dry_run",
                    "phase": "post-3",
                    "team_ids_count": len(team_ids),
                    "teams_sample": team_ids[:5],
                }
                log(f"[DRY] team_rankings for {competition}: {len(team_ids)} teams")
            else:
                pids = []
                for tid in team_ids:
                    log_path = get_log_path(competition, f"team_rankings_tid{tid}")
                    cmd = [
                        sys.executable, str(team_rankings_path),
                        "--db", str(DATA_DIR / f"team_rankings_{competition.replace(' ', '_')}.sqlite"),
                        "--state", str(DATA_DIR / f"team_rankings_{competition.replace(' ', '_')}_state.json"),
                        "--team-id", str(tid),
                        "--sleep-min", "0.5",
                        "--sleep-max", "1.0",
                        "--use-mysql",
                    ]
                    safe_cmd = _filter_cmd(cmd, "team_rankings")
                    pid = run_script_sequential(safe_cmd, log_path, "team_rankings",
                                                context=f"team_rankings/{competition}/TID={tid}")
                    if pid:
                        log(f"[PID {pid}] team_rankings/{competition}/TID={tid} started")
                        pids.append(pid)
                results["team_rankings"] = {"status": "started", "pids": pids, "phase": "post-3",
                                            "note": "Sequential (one team at a time)"}
        else:
            results["team_rankings"] = {"status": "blocked", "reason": "No team IDs found in standings.db (run standings first)"}
    else:
        results["team_rankings"] = {"status": "skipped", "reason": "script not found"}

    save_state(state)
    return results


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="SofaScore 10-Year Backfill Orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--all", action="store_true",
                    help="Run backfill for all 23 competitions")
    ap.add_argument("--competition", type=str,
                    help="Run backfill for a specific competition")
    ap.add_argument("--resume", action="store_true",
                    help="Resume from last checkpoint")
    ap.add_argument("--status", action="store_true",
                    help="Show backfill status for all competitions")
    ap.add_argument("--check-config", action="store_true",
                    help="Validate discoveries.json and config")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would run without executing")
    ap.add_argument("--limit", type=int, default=0,
                    help="Limit events per script (0=unlimited)")
    ap.add_argument("--max-concurrent", type=int, default=20,
                    help="Max simultaneous workers (default: 20, MySQL has 151 max_connections)")
    ap.add_argument("--from-year", type=int, default=2015,
                    help="Only process seasons from this year onwards (default: 2015)")
    ap.add_argument("--only-confirmed", action="store_true",
                    help="Only run competitions with confirmed season IDs")

    args = ap.parse_args()

    # Apply --max-concurrent setting
    global _MAX_CONCURRENT, _SPAWN_SEM
    _MAX_CONCURRENT = args.max_concurrent
    _SPAWN_SEM = threading.Semaphore(_MAX_CONCURRENT)
    if args.max_concurrent < 5:
        print(f"[WARN] --max-concurrent={args.max_concurrent} is very low. Minimum 5 recommended.")

    if args.check_config:
        print("=== Config Check ===")
        discoveries = load_discoveries()
        print(f"Discoveries: {len(discoveries)} competitions")
        for name, data in sorted(discoveries.items()):
            seasons = data.get("seasons", {})
            status = "✅" if seasons else "❌"
            print(f"  {status} {name}: {len(seasons)} seasons")
        return

    if args.status:
        state = load_state()
        print("=== Backfill Status ===")
        for name, cfg in sorted(COMPETITION_MAPPING.items()):
            confirmed = cfg.get("confirmed_sids", {})
            needs = cfg.get("needs_discovery", False)
            comp_state = state.get("competitions", {}).get(name, {})
            if confirmed:
                print(f"  ✅ {name}: {len(confirmed)} confirmed SIDs")
            elif needs:
                disc = load_discoveries().get(name, {})
                seasons = disc.get("seasons", {})
                if seasons:
                    print(f"  🔄 {name}: {len(seasons)} discovered seasons")
                else:
                    print(f"  ❌ {name}: needs discovery")
            else:
                print(f"  ❓ {name}: status unknown")
        return

    if args.competition:
        result = backfill_competition(
            args.competition,
            dry_run=args.dry_run,
            limit_per_script=args.limit,
            from_year=args.from_year,
        )
        print(f"Result: {json.dumps(result, indent=2)}")
        return

    if args.all:
        print(f"=== Starting 10-Year Backfill ===")
        print(f"Data dir: {DATA_DIR}")
        print(f"Log dir: {LOG_DIR}")
        print()

        # Separate confirmed vs needs-discovery
        confirmed = [k for k, v in COMPETITION_MAPPING.items()
                     if not v.get("needs_discovery") or v.get("confirmed_sids")]
        needs_disc = [k for k, v in COMPETITION_MAPPING.items()
                      if v.get("needs_discovery") and not v.get("confirmed_sids")]

        print(f"✅ Ready to run: {len(confirmed)}")
        print(f"🔄 Need discovery: {len(needs_disc)}")
        for k in needs_disc:
            print(f"   - {k}")
        print()

        if args.only_confirmed:
            targets = confirmed
        else:
            targets = list(COMPETITION_MAPPING.keys())

        for comp in targets:
            cfg = COMPETITION_MAPPING[comp]
            if cfg.get("blocker"):
                print(f"⏭ {comp}: BLOCKED — {cfg['blocker']}")
                continue
            print(f"\n{'='*60}")
            print(f"Backfilling: {comp}")
            print(f"{'='*60}")
            result = backfill_competition(
                comp,
                dry_run=args.dry_run,
                limit_per_script=args.limit,
                from_year=args.from_year,
            )
            for script, res in result.items():
                if isinstance(res, dict):
                    status = res.get("status", "?")
                    pid = res.get("pid", "-")
                    log_ = res.get("log", "-")
                    print(f"  {script}: {status} (PID={pid})")
                    if status == "started":
                        print(f"    log: {log_}")
            time.sleep(1)
        print("\n[DONE] All backfill jobs started")
        return

    print("Usage: orchestrator.py --all | --competition NAME | --status | --check-config | --dry-run")
    return

if __name__ == "__main__":
    main()