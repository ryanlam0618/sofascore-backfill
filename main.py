#!/usr/bin/env python3
"""
SofaScore Backfill — Unified CLI Entry Point
=============================================

Single entry point for all backfill operations:
  • discovery    — Discover season IDs for competitions
  • backfill     — Run full/partial backfill with Playwright (primary)
  • status       — Show current backfill progress
  • resume       — Resume interrupted backfill from checkpoint
  • verify       — Verify data completeness
  • backup       — PinchTab fallback for specific endpoints

Usage:
  python3 main.py discover --all
  python3 main.py backfill --competition "Premier League"
  python3 main.py status
  python3 main.py resume
  python3 main.py verify --competition "Premier League"

Architecture:
  • PRIMARY: Playwright + Webshare Proxy (bulk backfill, concurrent)
  • BACKUP: PinchTab Browser (quick lookup, manual recovery)
  • FALLBACK: Direct API (deprecated, mostly blocked)

Author: Kris
Version: 2025-06-30
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

# Repo paths
WORKDIR = Path(__file__).parent
DATA_DIR = WORKDIR / "data" / "backfill_sofascore_10y"
LOG_DIR = WORKDIR / "data" / "logs"
STATE_FILE = DATA_DIR / "orchestrator_state.json"
DISCOVERIES_FILE = WORKDIR / "discoveries.json"
COMPETITIONS_FILE = WORKDIR / "competitions_10y.yaml"

# Ensure dirs exist
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)


# ──────────────────────────────────────────────────────────────────────────────
# Data Models
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Competition:
    name: str
    cat_id: int
    ut_id: int
    mode: str  # national, cup, uefa, international
    seasons: dict[str, str]  # sid -> year
    ten_yr_seasons: list[str]
    status: str = "discovered"  # discovered, confirmed, blocked


@dataclass
class BackfillProgress:
    competition: str
    script: str
    season_id: Optional[str]
    status: str  # pending, running, completed, failed
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    events_processed: int = 0
    events_total: int = 0
    error_msg: Optional[str] = None


# ──────────────────────────────────────────────────────────────────────────────
# State Management
# ──────────────────────────────────────────────────────────────────────────────

class StateManager:
    """Manage backfill state with resume support."""

    def __init__(self, state_file: Path = STATE_FILE):
        self.state_file = state_file
        self._state = self._load()

    def _load(self) -> dict:
        if self.state_file.exists():
            return json.loads(self.state_file.read_text())
        return {
            "version": "1.0",
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "discoveries_verified": False,
            "competitions": {},
            "current_job": None,
            "completed_jobs": [],
        }

    def save(self):
        self._state["updated_at"] = datetime.now().isoformat()
        self.state_file.write_text(json.dumps(self._state, indent=2, ensure_ascii=False))

    def get_competition(self, name: str) -> dict:
        return self._state["competitions"].get(name, {})

    def set_competition(self, name: str, data: dict):
        self._state["competitions"][name] = data
        self.save()

    def list_competitions(self) -> list[str]:
        return list(self._state["competitions"].keys())

    def get_current_job(self) -> Optional[dict]:
        return self._state.get("current_job")

    def set_current_job(self, job: Optional[dict]):
        self._state["current_job"] = job
        if job and job.get("status") == "completed":
            self._state["completed_jobs"].append(job)
        self.save()

    def get_summary(self) -> dict:
        """Get human-readable progress summary."""
        comps = self._state["competitions"]
        total = len(comps)
        completed = sum(1 for c in comps.values() if c.get("status") == "completed")
        in_progress = sum(1 for c in comps.values() if c.get("status") == "in_progress")
        failed = sum(1 for c in comps.values() if c.get("status") == "failed")

        return {
            "total_competitions": total,
            "completed": completed,
            "in_progress": in_progress,
            "failed": failed,
            "pending": total - completed - in_progress - failed,
            "current_job": self._state.get("current_job"),
        }


# ──────────────────────────────────────────────────────────────────────────────
# Discovery Layer
# ──────────────────────────────────────────────────────────────────────────────

class DiscoveryService:
    """Handle season discovery for competitions."""

    def __init__(self):
        self.discoveries_file = DISCOVERIES_FILE

    def load_discoveries(self) -> dict:
        if self.discoveries_file.exists():
            return json.loads(self.discoveries_file.read_text())
        return {}

    def save_discoveries(self, data: dict):
        self.discoveries_file.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    def discover_all(self) -> dict:
        """Run discover_seasons.py --all."""
        print("🔍 Running season discovery for all competitions...")
        result = subprocess.run(
            [sys.executable, str(WORKDIR / "discover_seasons.py"), "--all"],
            capture_output=True,
            text=True,
            cwd=str(WORKDIR),
        )
        if result.returncode == 0:
            print("✅ Discovery completed")
            return self.load_discoveries()
        else:
            print(f"❌ Discovery failed:\n{result.stderr}")
            return {}

    def discover_single(self, name: str) -> dict:
        """Run discover_seasons.py --name."""
        print(f"🔍 Discovering seasons for {name}...")
        result = subprocess.run(
            [sys.executable, str(WORKDIR / "discover_seasons.py"), "--name", name],
            capture_output=True,
            text=True,
            cwd=str(WORKDIR),
        )
        if result.returncode == 0:
            print(f"✅ {name} discovery completed")
            return self.load_discoveries().get(name, {})
        else:
            print(f"❌ {name} discovery failed:\n{result.stderr}")
            return {}

    def get_competition(self, name: str) -> Optional[Competition]:
        """Get competition as typed object."""
        data = self.load_discoveries().get(name)
        if not data:
            return None

        return Competition(
            name=name,
            cat_id=data.get("cat_id", 0),
            ut_id=data.get("ut_id", 0),
            mode=data.get("mode", "unknown"),
            seasons=data.get("seasons", {}),
            ten_yr_seasons=data.get("ten_yr_seasons", []),
            status=data.get("status", "unknown"),
        )

    def list_competitions(self) -> list[str]:
        return list(self.load_discoveries().keys())


# ──────────────────────────────────────────────────────────────────────────────
# Backfill Layer (Primary: Playwright)
# ──────────────────────────────────────────────────────────────────────────────

class PlaywrightBackfillService:
    """Primary backfill using Playwright + Webshare proxy."""

    # Phase 1: Per-season enumeration scripts
    PHASE_1_SCRIPTS = [
        ("incidents", "入球、黃紅牌、換人"),
        ("lineups", "正選陣容、後備"),
        ("shotmap_xg", "射門位置 + xG"),
        ("statistics", "比賽統計數據"),
        ("related_matches", "對賽紀錄 H2H"),
    ]

    # Phase 2: Event-based (after events known)
    PHASE_2_SCRIPTS = [
        ("attendance", "入場人數"),
        ("referees", "球證資料"),
    ]

    # Phase 3: Team/league-based
    PHASE_3_SCRIPTS = [
        ("standings", "聯賽積分榜"),
        ("team_rankings", "球隊排名"),
        ("managers", "領隊資料"),
    ]

    def __init__(self, state: StateManager):
        self.state = state

    def backfill_competition(
        self,
        competition: str,
        dry_run: bool = False,
        limit: int = 0,
        from_year: int = 2015,
        phases: list[int] = [1, 2, 3],
    ) -> dict:
        """Backfill single competition."""
        print(f"\n{'='*60}")
        print(f"🏆 Backfill: {competition}")
        print(f"{'='*60}")

        discovery = DiscoveryService()
        comp = discovery.get_competition(competition)

        if not comp:
            print(f"❌ {competition} not discovered. Run: main.py discover --name '{competition}'")
            return {"status": "blocked", "reason": "not_discovered"}

        if comp.status == "blocked":
            print(f"⏭ {competition} blocked — {comp.status}")
            return {"status": "blocked", "reason": comp.status}

        # Update state
        self.state.set_competition(competition, {
            "status": "in_progress",
            "started_at": datetime.now().isoformat(),
            "phases": phases,
        })

        results = {}

        # Phase 1: Per-season scripts
        if 1 in phases:
            print(f"\n📊 Phase 1: Per-Season Data ({len(comp.ten_yr_seasons)} seasons)")
            for sid in comp.ten_yr_seasons[:3] if dry_run else comp.ten_yr_seasons:
                print(f"\n  Season {sid}: {comp.seasons.get(sid, 'N/A')}")
                for script, desc in self.PHASE_1_SCRIPTS:
                    if dry_run:
                        print(f"    [DRY] fetch_{script}.py")
                        continue

                    # Run actual fetch script via orchestrator
                    result = self._run_fetch_script(
                        script=script,
                        competition=competition,
                        cat_id=comp.cat_id,
                        season_id=int(sid),
                        ut_id=comp.ut_id,
                    )
                    results[f"{script}_{sid}"] = result

        # Phase 2 & 3 would need event IDs first
        if 2 in phases:
            print(f"\n📊 Phase 2: Event-Based Data (requires event IDs)")
            print("   (Not implemented in this version)")

        if 3 in phases:
            print(f"\n📊 Phase 3: Team/League Data")
            print("   (Not implemented in this version)")

        # Update final state
        self.state.set_competition(competition, {
            "status": "completed",
            "completed_at": datetime.now().isoformat(),
            "results": results,
        })

        return {"status": "completed", "competition": competition, "phases": phases}

    def _run_fetch_script(
        self,
        script: str,
        competition: str,
        cat_id: int,
        season_id: int,
        ut_id: int,
    ) -> dict:
        """Run a single fetch script."""
        script_path = WORKDIR / f"fetch_{script}.py"
        if not script_path.exists():
            return {"status": "error", "reason": f"Script not found: {script_path}"}

        # Delegate to orchestrator.py for actual execution
        # (orchestrator has retry logic, semaphore, etc.)
        cmd = [
            sys.executable,
            str(WORKDIR / "orchestrator.py"),
            "--competition", competition,
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(WORKDIR))
        return {
            "status": "completed" if result.returncode == 0 else "failed",
            "returncode": result.returncode,
            "stdout": result.stdout[-500:] if result.stdout else "",
            "stderr": result.stderr[-500:] if result.stderr else "",
        }

    def backfill_all(
        self,
        dry_run: bool = False,
        limit: int = 0,
        from_year: int = 2015,
        only_confirmed: bool = False,
    ) -> dict:
        """Backfill all discovered competitions."""
        discovery = DiscoveryService()
        competitions = discovery.list_competitions()

        print(f"\n{'='*60}")
        print(f"🏆 Full Backfill: {len(competitions)} competitions")
        print(f"{'='*60}")

        results = {}
        for comp_name in competitions:
            comp = discovery.get_competition(comp_name)
            if comp and comp.status == "blocked":
                print(f"⏭ Skipping {comp_name} (blocked)")
                continue

            result = self.backfill_competition(
                competition=comp_name,
                dry_run=dry_run,
                limit=limit,
                from_year=from_year,
            )
            results[comp_name] = result

            if not dry_run:
                time.sleep(1)  # Rate limiting between competitions

        return {"status": "completed", "competitions": results}


# ──────────────────────────────────────────────────────────────────────────────
# PinchTab Backup Layer
# ──────────────────────────────────────────────────────────────────────────────

class PinchTabBackupService:
    """Backup service using PinchTab browser."""

    def fetch_single_endpoint(
        self,
        url: str,
        output_file: Optional[Path] = None,
    ) -> dict:
        """Fetch single endpoint via PinchTab.

        Use this when:
        • Playwright fails for specific endpoint
        • Need quick manual verification
        • Debugging data issues
        """
        print(f"🔍 PinchTab fetch: {url}")
        # This would use pinchtab_* MCP tools
        # For now, placeholder
        return {"status": "not_implemented", "url": url}


# ──────────────────────────────────────────────────────────────────────────────
# CLI Commands
# ──────────────────────────────────────────────────────────────────────────────

def cmd_discover(args):
    """Handle discover subcommand."""
    service = DiscoveryService()

    if args.all:
        results = service.discover_all()
        print(f"\n✅ Discovered {len(results)} competitions")
        for name, data in sorted(results.items()):
            seasons = len(data.get("seasons", {}))
            ten_yr = len(data.get("ten_yr_seasons", []))
            print(f"  • {name}: {seasons} seasons ({ten_yr} in 10yr window)")

    elif args.name:
        result = service.discover_single(args.name)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.list:
        comps = service.list_competitions()
        print(f"\n📋 {len(comps)} competitions discovered:")
        for name in sorted(comps):
            comp = service.get_competition(name)
            status_icon = "✅" if comp and comp.status == "discovered" else "❌"
            print(f"  {status_icon} {name}")

    else:
        print("Usage: main.py discover --all | --name NAME | --list")


def cmd_backfill(args):
    """Handle backfill subcommand."""
    state = StateManager()
    service = PlaywrightBackfillService(state)

    if args.competition:
        result = service.backfill_competition(
            competition=args.competition,
            dry_run=args.dry_run,
            limit=args.limit,
            from_year=args.from_year,
            phases=args.phases or [1, 2, 3],
        )
        print(f"\nResult:\n{json.dumps(result, indent=2, ensure_ascii=False)}")

    elif args.all:
        result = service.backfill_all(
            dry_run=args.dry_run,
            limit=args.limit,
            from_year=args.from_year,
            only_confirmed=args.only_confirmed,
        )
        print(f"\n{'='*60}")
        print("Backfill Summary")
        print(f"{'='*60}")
        for comp, res in result.get("competitions", {}).items():
            status = res.get("status", "?")
            icon = "✅" if status == "completed" else "❌"
            print(f"  {icon} {comp}: {status}")

    else:
        print("Usage: main.py backfill --all | --competition NAME [--dry-run]")


def cmd_status(args):
    """Handle status subcommand."""
    state = StateManager()
    summary = state.get_summary()

    print(f"\n{'='*60}")
    print("📊 Backfill Status")
    print(f"{'='*60}")
    print(f"Total competitions: {summary['total_competitions']}")
    print(f"  ✅ Completed: {summary['completed']}")
    print(f"  🔄 In Progress: {summary['in_progress']}")
    print(f"  ⏳ Pending: {summary['pending']}")
    print(f"  ❌ Failed: {summary['failed']}")

    if summary['current_job']:
        print(f"\nCurrent Job:")
        print(f"  {json.dumps(summary['current_job'], indent=2)}")

    # Also show discoveries status
    discovery = DiscoveryService()
    discoveries = discovery.load_discoveries()
    print(f"\n📋 Discovered Competitions: {len(discoveries)}")


def cmd_resume(args):
    """Handle resume subcommand."""
    state = StateManager()
    current = state.get_current_job()

    if not current:
        print("✅ No interrupted jobs found. All clear!")
        return

    print(f"Resuming: {current.get('competition')} - {current.get('script')}")
    # Implementation would check state and continue from checkpoint


def cmd_verify(args):
    """Handle verify subcommand."""
    print("🔍 Verifying data completeness...")
    # Implementation would check SQLite/MySQL for missing data


def cmd_backup(args):
    """Handle backup subcommand (PinchTab fallback)."""
    service = PinchTabBackupService()
    result = service.fetch_single_endpoint(args.url)
    print(json.dumps(result, indent=2))


# ──────────────────────────────────────────────────────────────────────────────
# Main Entry
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="SofaScore Backfill — Unified CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s discover --all                    # Discover all seasons
  %(prog)s backfill --competition "Premier League"  # Backfill PL
  %(prog)s backfill --all --dry-run          # Dry run all competitions
  %(prog)s status                          # Show progress
  %(prog)s resume                          # Resume from checkpoint

For help on subcommands:
  %(prog)s COMMAND --help
        """,
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # ── discover ───────────────────────────────────────────────────────────
    discover = subparsers.add_parser("discover", help="Discover season IDs")
    discover.add_argument("--all", action="store_true", help="Discover all competitions")
    discover.add_argument("--name", type=str, help="Discover single competition")
    discover.add_argument("--list", action="store_true", help="List discovered competitions")
    discover.set_defaults(func=cmd_discover)

    # ── backfill ────────────────────────────────────────────────────────────
    backfill = subparsers.add_parser("backfill", help="Run backfill")
    backfill.add_argument("--all", action="store_true", help="Backfill all competitions")
    backfill.add_argument("--competition", type=str, help="Backfill single competition")
    backfill.add_argument("--dry-run", action="store_true", help="Show what would be done")
    backfill.add_argument("--limit", type=int, default=0, help="Limit events per script")
    backfill.add_argument("--from-year", type=int, default=2015, help="Start from year")
    backfill.add_argument("--only-confirmed", action="store_true", help="Only confirmed competitions")
    backfill.add_argument("--phases", type=int, nargs="+", help="Phases to run (1,2,3)")
    backfill.set_defaults(func=cmd_backfill)

    # ── status ──────────────────────────────────────────────────────────────
    status = subparsers.add_parser("status", help="Show backfill status")
    status.set_defaults(func=cmd_status)

    # ── resume ───────────────────────────────────────────────────────────────
    resume = subparsers.add_parser("resume", help="Resume interrupted backfill")
    resume.set_defaults(func=cmd_resume)

    # ── verify ────────────────────────────────────────────────────────────────
    verify = subparsers.add_parser("verify", help="Verify data completeness")
    verify.set_defaults(func=cmd_verify)

    # ── backup ───────────────────────────────────────────────────────────────
    backup = subparsers.add_parser("backup", help="PinchTab backup fetch")
    backup.add_argument("--url", type=str, required=True, help="URL to fetch")
    backup.set_defaults(func=cmd_backup)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
