#!/usr/bin/env python3
"""Debug: dump __NEXT_DATA__ structure to find missing endpoints."""
from __future__ import annotations

import asyncio, json, sys
from pathlib import Path

WORKDIR = Path(__file__).resolve().parent
sys.path.insert(0, str(WORKDIR))

from dotenv import load_dotenv
load_dotenv(WORKDIR / ".env")

from backfill_runner import BackfillClient

async def main():
    client = BackfillClient(headless=True, sticky_session_key="debug-ssr")
    await client.__aenter__()
    try:
        await client.warm_homepage()
        await client.warm_event(6767928)
        
        # Dump raw __NEXT_DATA__
        raw = await client.page.evaluate("""
            () => {
              const el = document.querySelector('#__NEXT_DATA__');
              if (!el) return null;
              try { return JSON.parse(el.textContent); }
              catch(e) { return {error: e.toString()}; }
            }
        """)
        
        if raw:
            # Save to file for inspection
            with open(WORKDIR / "debug_ssr_raw.json", "w") as f:
                json.dump(raw, f, indent=2, default=str)
            print("Raw SSR saved to debug_ssr_raw.json")
            
            # Recursively find all keys matching our targets
            targets = ["incidents", "shotmap", "shotMap", "graph", "comments", "lineup", "odds", "statistics", "stats"]
            
            def find_keys(obj, path="", depth=0):
                if depth > 8:
                    return
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        new_path = f"{path}.{k}"
                        if any(t in k.lower() for t in targets):
                            val_type = f"list[{len(v)}]" if isinstance(v, list) else (f"dict[{len(v)}]" if isinstance(v, dict) else type(v).__name__)
                            print(f"  {'  '*min(depth,3)}{new_path} → {val_type}")
                        find_keys(v, new_path, depth+1)
                elif isinstance(obj, list):
                    if len(obj) > 0:
                        find_keys(obj[0], f"{path}[0]", depth+1)
            
            print("\n🔍 Searching for target keys in __NEXT_DATA__:")
            find_keys(raw)
        else:
            print("❌ No __NEXT_DATA__ found!")
            
    finally:
        await client.shutdown()

asyncio.run(main())