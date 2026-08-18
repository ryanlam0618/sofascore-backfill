#!/usr/bin/env python3
"""
Test SofaScore 403 challenge using real Playwright browser with proxy.
"""

import asyncio
import json
from playwright.async_api import async_playwright

PROXY_IP = "31.57.90.24"
PROXY_PORT = "5593"
PROXY_USER = "aeptenjc"
PROXY_PASS = "dztr57tcycoz"
TEST_URL = "https://api.sofascore.com/api/v1/unique-tournament/17/season/76986/rounds"
FRONTEND_URL = "https://www.sofascore.com/football/tournament/england/premier-league/17#id:76986"

async def main():
    proxy_url = f"http://{PROXY_USER}:{PROXY_PASS}@{PROXY_IP}:{PROXY_PORT}"
    print(f"Proxy: {PROXY_IP}:{PROXY_PORT}")
    print(f"API URL: {TEST_URL}")
    print("=" * 70)
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        
        # Test 1: API endpoint via browser + proxy
        print("\n=== Test 1: API endpoint via browser + proxy ===")
        context = await browser.new_context(
            proxy={"server": proxy_url},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        
        # Listen for responses
        responses = []
        page.on("response", lambda r: responses.append({
            "url": r.url[:100],
            "status": r.status,
            "headers": dict(r.headers) if r.status != 200 else {}
        }))
        
        try:
            resp = await page.goto(TEST_URL, wait_until="networkidle", timeout=30000)
            body = await resp.text()
            print(f"Status: {resp.status}")
            print(f"Body: {body[:500]}")
            print(f"\nResponse headers:")
            for k, v in resp.headers.items():
                print(f"  {k}: {v}")
        except Exception as e:
            print(f"Error: {e}")
        
        # Check all network responses
        print(f"\nAll responses ({len(responses)}):")
        for r in responses:
            print(f"  {r['status']} {r['url']}")
        
        await context.close()
        
        # Test 2: Frontend page via browser + proxy (to see if challenge page loads)
        print("\n=== Test 2: Frontend page via browser + proxy ===")
        context2 = await browser.new_context(
            proxy={"server": proxy_url},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
        page2 = await context2.new_page()
        
        responses2 = []
        page2.on("response", lambda r: responses2.append({
            "url": r.url[:120],
            "status": r.status
        }))
        
        try:
            await page2.goto(FRONTEND_URL, wait_until="networkidle", timeout=30000)
            title = await page2.title()
            content = await page2.content()
            print(f"Title: {title}")
            print(f"Page length: {len(content)} chars")
            
            # Check if there's a challenge/captcha
            if "challenge" in content.lower() or "captcha" in content.lower() or "verify" in content.lower():
                print("⚠️ CHALLENGE/CAPTCHA detected in page!")
                # Extract challenge-related content
                for keyword in ["challenge", "captcha", "verify", "human", "bot", "cloudflare", "hcaptcha", "recaptcha"]:
                    idx = content.lower().find(keyword)
                    if idx >= 0:
                        snippet = content[max(0,idx-100):idx+200]
                        print(f"\n  [{keyword}] context: ...{snippet}...")
            else:
                print("✅ No challenge detected in page content")
            
            print(f"\nAll responses ({len(responses2)}):")
            for r in responses2[:20]:
                print(f"  {r['status']} {r['url']}")
        except Exception as e:
            print(f"Error: {e}")
            # Take screenshot
            try:
                await page2.screenshot(path="proxy_challenge_screenshot.png")
                print("Screenshot saved to proxy_challenge_screenshot.png")
            except:
                pass
        
        await context2.close()
        await browser.close()

asyncio.run(main())
