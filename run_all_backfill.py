#!/usr/bin/env python3
"""Driver: run all remaining backfill tasks via Webshare proxy.

Tasks:
1. retry_failed_metadata.py  - fix 1061 events with missing metadata
2. fetch_player_stats.py     - all stat types (goals, assists, xG, xA, etc.)
3. fetch_attendance.py       - match attendance
4. fetch_managers.py         - manager data
"""
import subprocess, sys, time

WORKDIR = "/root/.openclaw/workspace/sofascore_backfill"
LOGDIR = f"{WORKDIR}/data/logs"
SCRIPTS = {
    "retry_metadata":  ["python3", "retry_failed_metadata.py"],
    "player_stats":    ["python3", "fetch_player_stats.py", "--use-mysql", "--run-all",
                        "--sleep-min", "3", "--sleep-max", "8",
                        "--stat-types", "goals,assists,xg,xa,appearances,minutes_played,yellow_cards,red_cards"],
    "attendance":      ["python3", "fetch_attendance.py", "--use-mysql"],
    "managers":        ["python3", "fetch_managers.py", "--use-mysql"],
}

def run_background(name, cmd, wait=0):
    if wait > 0:
        print(f"[{name}] Waiting {wait}s before starting...")
        time.sleep(wait)
    logfile = f"{LOGDIR}/{name}.log"
    full_cmd = ["nohup"] + cmd + [">", logfile, "2>&1", "&"]
    print(f"[{name}] Starting: {' '.join(cmd)}")
    result = subprocess.run(" ".join(full_cmd), shell=True, cwd=WORKDIR,
                           capture_output=True, text=True)
    return result.stdout.strip()

def main():
    print("=== SofaScore Backfill - All Tasks ===")
    print(f"Started: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print(f"Workspace: {WORKDIR}")
    print()

    # 1. Retry metadata (no wait, starts immediately)
    run_background("retry_metadata", SCRIPTS["retry_metadata"])

    # 2. Player stats (wait 30s for metadata to start)
    run_background("player_stats", SCRIPTS["player_stats"], wait=30)

    # 3. Attendance (wait 60s)
    run_background("attendance", SCRIPTS["attendance"], wait=60)

    # 4. Managers (wait 120s)
    run_background("managers", SCRIPTS["managers"], wait=120)

    print("\n[DONE] All background jobs launched!")
    print("Logs: data/logs/{retry_metadata,player_stats,attendance,managers}.log")
    print("Monitor: tail -f data/logs/*.log")

if __name__ == "__main__":
    main()