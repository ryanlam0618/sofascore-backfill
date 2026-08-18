#!/usr/bin/env python3
"""Quick test: SSR fallback extraction from a single event page."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

WORKDIR = Path(__file__).resolve().parent
sys.path.insert(0, str(WORKDIR))

from dotenv import load_dotenv
load_dotenv(WORKDIR / ".env")

from backfill_runner import BackfillClient

async def main():
    event_id = 6767928
    
    print(f"🔧 Testing SSR extraction for event {event_id}...")
    
    client = BackfillClient(headless=True, sticky_session_key="test-ssr")
    await client.__aenter__()
    
    try:
        print("📄 Step 1: warm_homepage")
        await client.warm_homepage()
        
        print(f"📄 Step 2: warm_event({event_id})")
        await client.warm_event(event_id)
        
        endpoints = [
            "/api/v1/event/{event_id}",
            "/api/v1/event/{event_id}/incidents",
            "/api/v1/event/{event_id}/lineups",
            "/api/v1/event/{event_id}/statistics",
            "/api/v1/event/{event_id}/shotmap",
            "/api/v1/event/{event_id}/graph",
            "/api/v1/event/{event_id}/odds/1/all",
            "/api/v1/event/{event_id}/comments",
        ]
        
        results = {}
        for endpoint_template in endpoints:
            path = endpoint_template.format(event_id=event_id)
            
            # Check capture
            captured = await client._wait_for_captured_response(path, timeout_ms=5000)
            cap_status = str(captured.get("status")) if captured else "miss"
            
            # Try SSR
            ssr = await client.get_event_ssr(path)
            ssr_status = "found" if ssr else "not found"
            ssr_keys = list(ssr.keys()) if isinstance(ssr, dict) else None
            
            results[path] = {"capture": cap_status, "ssr": ssr_status, "ssr_keys": ssr_keys}
            print(f"   {path.split('/')[-1] or path.split('/')[-2]:20s} | capture={cap_status:>4s} | SSR={ssr_status:>9s}", end="")
            if ssr_keys:
                print(f" | keys={ssr_keys}", end="")
            print()
        
        print("\n=== Summary ===")
        capture_ok = sum(1 for r in results.values() if r['capture'] == 200)
        capture_403 = sum(1 for r in results.values() if r['capture'] == 403)
        ssr_ok = sum(1 for r in results.values() if r['ssr'] == 'found')
        print(f"Capture 200: {capture_ok}/8 | Capture 403: {capture_403}/8 | SSR found: {ssr_ok}/8")
        
    finally:
        await client.shutdown()

if __name__ == "__main__":
    asyncio.run(main())