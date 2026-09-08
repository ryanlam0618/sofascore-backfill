#!/usr/bin/env python3
"""run_backfill.py — unified production backfill entry point (Stage 4d-3 flip).

Kris approval 2026-09-08 19:48 GMT+8 (partial GO): Gen4 fp-v2 + fixed-IP pool
replaces Gen2 hybrid as the production default for SofaScore backfill.

Engine selection order (highest priority wins):
  1. CLI flag  --engine gen2|gen4
  2. Env var   BACKFILL_ENGINE=gen2|gen4
  3. Built-in DEFAULT_ENGINE below   <-- "production default"

Rollback path (per docs/gen4_deployment_report_v1.md §4):
  - Instant:  run any command with `--engine gen2` (or BACKFILL_ENGINE=gen2).
  - No schema revert needed — both engines write the same tables idempotently.

Engine mapping:
  gen2 → backfill_runner.py          (legacy hybrid: unpinned 'chrome' impersonate
                                      + rotate-gateway proxy fallback)
  gen4 → Stage-4 family of runners   (curl_cffi impersonate=chrome124, pinned
                                      fingerprint fp-v2, audited fixed-IP pool)

Note: Gen4 runners are stage-scoped scripts (stage2 / stage3-expansion /
stage4c batches) rather than one generic --competition CLI. Stage 4d-4 runbook
(docs/gen4_stage4d4_deploy_runbook.md) documents which Gen4 script covers which
production scenario. This dispatcher validates the selection and hands off with
unchanged argv to keep behavior identical to calling the engine directly.
"""
from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

DEFAULT_ENGINE = "gen4"  # ← Stage 4d-3 flip (was implicitly gen2 before 2026-09-08)
VALID_ENGINES = {"gen2", "gen4"}

ENGINE_SCRIPT = {
    "gen2": "backfill_runner.py",
    # gen4 dispatches to the stage-appropriate runner; see RUNBOOK.
    # Default gen4 production runner for tournament backfill:
    "gen4": "gen4_stage3_expansion_backfill.py",
}


def pick_engine(argv: list[str]) -> tuple[str, list[str]]:
    """Return (engine, remaining_argv)."""
    # 1. CLI flag
    if "--engine" in argv:
        i = argv.index("--engine")
        if i + 1 >= len(argv):
            sys.exit("run_backfill: --engine requires gen2|gen4")
        eng = argv[i + 1].lower()
        rest = argv[:i] + argv[i + 2 :]
    else:
        eng = os.environ.get("BACKFILL_ENGINE", DEFAULT_ENGINE).lower()
        rest = list(argv)
    if eng not in VALID_ENGINES:
        sys.exit(f"run_backfill: unknown engine {eng!r} (valid: gen2, gen4)")
    return eng, rest


def main(argv: list[str]) -> int:
    engine, rest = pick_engine(argv)
    script = ROOT / ENGINE_SCRIPT[engine]
    if not script.exists():
        sys.exit(f"run_backfill: engine script missing: {script}")
    print(f"[run_backfill] engine={engine} -> {script.name} (rollback: --engine "
          f"{'gen2' if engine == 'gen4' else 'gen4'})", flush=True)
    sys.argv = [script.name, *rest]
    runpy.run_path(str(script), run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
