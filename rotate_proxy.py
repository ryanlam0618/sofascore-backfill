#!/usr/bin/env python3
"""
Rotate Proxy Manager for CloakBrowser + SofaScore.
Handles:
- Browser lifecycle with Webshare rotate gateway
- Auto-restart every 150 API calls (EPIPE prevention)
- Auto cooldown 5min on 403 (rate-limit recovery)
- Incremental result saving
"""

import json
import time
from cloakbrowser import launch

PROXY = {
    'server': 'http://p.webshare.io:80',
    'username': 'aeptenjc-rotate',
    'password': 'dztr57tcycoz'
}

RESTART_EVERY = 150
COOLDOWN_SEC = 600  # 10 min — SofaScore needs longer cooldown after 150 calls


class ProxyManager:
    def __init__(self, result_file=None):
        self.call_count = 0
        self.consecutive_403 = 0
        self.browser = None
        self.page = None
        self.result_file = result_file
        self.use_proxy = True

    def launch(self):
        """Launch CloakBrowser with or without proxy."""
        self.close()
        if self.use_proxy:
            print(f"  [proxy] Launching CloakBrowser + rotate gateway...")
            self.browser = launch(headless=True, humanize=True, proxy=PROXY)
        else:
            print(f"  [proxy] Launching CloakBrowser direct (no proxy)...")
            self.browser = launch(headless=True, humanize=True)
        self.page = self.browser.new_page()
        self.call_count = 0
        self.consecutive_403 = 0
        print(f"  [proxy] Browser ready.")

    def get_page(self):
        """Get current page, auto-restart if needed."""
        if self.page is None or self.call_count >= RESTART_EVERY:
            self.launch()
        return self.page

    def record_call(self, status):
        """Record an API call result. Triggers cooldown on 403."""
        self.call_count += 1
        if status == 403:
            self.consecutive_403 += 1
        else:
            self.consecutive_403 = 0

        # 3 consecutive 403s → cooldown, then try direct if still blocked
        if self.consecutive_403 >= 3:
            if self.use_proxy:
                print(f"  [proxy] 3 consecutive 403s with proxy, cooling down {COOLDOWN_SEC}s...")
                self.close()
                time.sleep(COOLDOWN_SEC)
                print(f"  [proxy] Switching to direct mode (no proxy)...")
                self.use_proxy = False
                self.launch()
            else:
                print(f"  [proxy] 3 consecutive 403s direct, cooling down {COOLDOWN_SEC}s...")
                self.close()
                time.sleep(COOLDOWN_SEC)
                self.use_proxy = True
                self.launch()

    def maybe_restart(self):
        """Restart browser if call count exceeds threshold."""
        if self.call_count >= RESTART_EVERY:
            print(f"  [proxy] {self.call_count} calls reached, restarting browser...")
            self.launch()

    def close(self):
        """Close browser safely."""
        try:
            if self.browser:
                self.browser.close()
        except:
            pass
        self.browser = None
        self.page = None

    def api_get(self, url, timeout=20000):
        """Make an API GET request via the browser. Returns (status, body_text) or (0, None)."""
        page = self.get_page()
        try:
            resp = page.goto(url, timeout=timeout)
            status = resp.status if resp else 0
            self.record_call(status)
            if status == 200:
                body = page.evaluate("() => document.body.innerText")
                return status, body
            elif status == 403:
                body = page.evaluate("() => document.body.innerText")
                return status, body
            else:
                return status, None
        except Exception as e:
            self.record_call("error")
            return "error", None

    def api_get_json(self, url, timeout=20000):
        """Make an API GET and parse JSON. Returns (status, parsed_json) or (status, None)."""
        status, body = self.api_get(url, timeout)
        if status == 200 and body:
            try:
                return status, json.loads(body)
            except:
                return status, None
        return status, None


def save_json(result_file, data):
    """Save results to JSON file."""
    with open(result_file, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
