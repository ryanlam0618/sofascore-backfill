#!/usr/bin/python3
"""
Orchestrator Watcher Daemon
Ensures orchestrator.py is always running.
Restarts it if it exits (after --all one-shot completes or crashes).
"""
import subprocess, time, os, sys, signal
from datetime import datetime

ORCHESTRATOR_DIR = "/root/.openclaw/workspace/sofascore_backfill"
ORCHESTRATOR_SCRIPT = os.path.join(ORCHESTRATOR_DIR, "orchestrator.py")
ORCH_LOG = os.path.join(ORCHESTRATOR_DIR, "data/logs/orchestrator_full_run.log")
WATCHER_LOG = os.path.join(ORCHESTRATOR_DIR, "data/logs/orchestrator_watcher.log")
CHECK_INTERVAL = 3600  # 1 hour between restarts

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    with open(WATCHER_LOG, "a") as f:
        f.write(line)
    print(line, end="")

def is_running():
    r = subprocess.run(["pgrep", "-f", "orchestrator.py"], capture_output=True, text=True)
    return bool(r.stdout.strip())

def start_orchestrator():
    log(f"Starting orchestrator with --all --resume --max-concurrent 20")
    proc = subprocess.Popen(
        [sys.executable, ORCHESTRATOR_SCRIPT, "--all", "--resume", "--max-concurrent", "20"],
        cwd=ORCHESTRATOR_DIR,
        stdout=open(ORCH_LOG, "a"),
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid  # new process group
    )
    return proc.pid

log("=== Orchestrator Watcher Daemon Started ===")
log(f"Check interval: {CHECK_INTERVAL}s ({CHECK_INTERVAL//60} min)")

restart_count = 0
while True:
    if not is_running():
        restart_count += 1
        pid = start_orchestrator()
        log(f"Orchestrator started (PID={pid}, restart #{restart_count})")
    else:
        log(f"Orchestrator is running - OK")

    time.sleep(CHECK_INTERVAL)
