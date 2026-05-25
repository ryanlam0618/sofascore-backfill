#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Anti-blocking utilities for SofaScore API requests.
Helps avoid IP/API key blocks by making requests look more like real browsers.
"""

from __future__ import annotations

import random
import time
from datetime import datetime, timezone
from typing import Optional

# ─────────────────────────────────────────────────────────────────────────────
# User Agents — mobile + desktop, Chrome + Firefox + Safari
# ─────────────────────────────────────────────────────────────────────────────

USER_AGENTS = [
    # Chrome on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    # Chrome on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    # Chrome on Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Firefox on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    # Firefox on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:126.0) Gecko/20100101 Firefox/126.0",
    # Firefox on Linux
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    # Safari on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    # Chrome on Mobile (Android)
    "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Mobile Safari/537.36",
    # Safari on iOS
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    # Edge on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
]

# Platform to sec-ch-ua-platform mapping
PLATFORM_MAP = {
    "Windows": '"Windows"',
    "Macintosh": '"macOS"',
    "X11": '"Linux"',
    "Linux": '"Linux"',
    "iPhone": '"iOS"',
}


def get_random_ua() -> str:
    """Return a random user agent."""
    return random.choice(USER_AGENTS)


def get_platform(ua: str) -> str:
    """Extract platform from UA string."""
    if "Windows" in ua:
        return "Windows"
    elif "Macintosh" in ua:
        return "Macintosh"
    elif "X11" in ua or "Linux" in ua:
        return "X11"
    elif "iPhone" in ua:
        return "iPhone"
    elif "Android" in ua:
        return "Linux"  # Treat Android as Linux
    return "Windows"


def get_sec_ch_ua(ua: str) -> str:
    """Get sec-ch-ua header value based on UA."""
    if "Chrome" in ua and "Edg" not in ua:
        if "macOS" in ua:
            return '"Chromium";v="124", "Google Chrome";v="124", ";Not A Brand";v="99"'
        elif "Windows" in ua:
            return '"Chromium";v="124", "Microsoft Edge";v="124", ";Not A Brand";v="99"'
        else:
            return '"Chromium";v="124", "Google Chrome";v="124", ";Not A Brand";v="99"'
    elif "Firefox" in ua:
        return '"Firefox";v="126", "Firefox";v="126"'
    elif "Safari" in ua and "Chrome" not in ua:
        return '"Safari";v="17", "Not.A Brand";v="8"'
    return '"Chromium";v="124", "Google Chrome";v="124"'


# ─────────────────────────────────────────────────────────────────────────────
# Headers factory
# ─────────────────────────────────────────────────────────────────────────────

def build_headers(
    referer: str = "https://www.sofascore.com/",
    accept: str = "application/json, text/plain, */*",
    extra_accept_encoding: bool = True,
) -> dict:
    """
    Build browser-like headers to avoid bot detection.
    
    Args:
        referer: Referer URL (important for SofaScore)
        accept: Accept header value
        extra_accept_encoding: Include Accept-Encoding for compression
    
    Returns:
        dict of headers
    """
    ua = get_random_ua()
    platform = get_platform(ua)
    
    headers = {
        "User-Agent": ua,
        "Accept": accept,
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6,zh-HK;q=0.5",
        "Referer": referer,
        "Origin": "https://www.sofascore.com",
        "sec-ch-ua": get_sec_ch_ua(ua),
        "sec-ch-ua-mobile": "?1" if platform in ("iPhone",) else "?0",
        "sec-ch-ua-platform": PLATFORM_MAP.get(platform, '"Windows"'),
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    
    if extra_accept_encoding:
        headers["Accept-Encoding"] = "gzip, deflate, br"
    
    return headers


def build_html_headers(referer: str = "https://www.sofascore.com/") -> dict:
    """Build headers for HTML page requests (like widget pages)."""
    ua = get_random_ua()
    platform = get_platform(ua)
    
    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6,zh-HK;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": referer,
        "sec-ch-ua": get_sec_ch_ua(ua),
        "sec-ch-ua-mobile": "?1" if platform in ("iPhone",) else "?0",
        "sec-ch-ua-platform": PLATFORM_MAP.get(platform, '"Windows"'),
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Rate limiting & daily quota
# ─────────────────────────────────────────────────────────────────────────────

class RequestCounter:
    """
    Track daily requests and enforce limits.
    Prevents hitting rate limits by pausing when approaching daily quota.
    """
    
    def __init__(
        self,
        daily_limit: int = 5000,
        warn_at: float = 0.8,  # Warn at 80% of limit
    ):
        self.daily_limit = daily_limit
        self.warn_at = warn_at
        self._reset()
    
    def _reset(self):
        self.today = datetime.now(timezone.utc).date()
        self.count = 0
        self.warnings_issued = 0
    
    def _check_date(self):
        """Reset counter if new day."""
        today = datetime.now(timezone.utc).date()
        if today != self.today:
            self._reset()
    
    def increment(self) -> int:
        """Increment counter, return new count."""
        self._check_date()
        self.count += 1
        return self.count
    
    @property
    def remaining(self) -> int:
        """Number of requests remaining today."""
        self._check_date()
        return max(0, self.daily_limit - self.count)
    
    @property
    def usage_pct(self) -> float:
        """Usage percentage (0.0 to 1.0+)."""
        self._check_date()
        return self.count / self.daily_limit
    
    @property
    def is_exhausted(self) -> bool:
        """True if daily limit reached."""
        return self.remaining <= 0
    
    def should_warn(self) -> bool:
        """True if we should warn about approaching limit."""
        return self.usage_pct >= self.warn_at and self.warnings_issued == 0
    
    def acknowledge_warning(self):
        """Acknowledge warning so we don't repeat it."""
        self.warnings_issued += 1


# ─────────────────────────────────────────────────────────────────────────────
# Sleep utilities
# ─────────────────────────────────────────────────────────────────────────────

def random_sleep(min_sec: float = 2.0, max_sec: float = 5.0) -> float:
    """
    Sleep for a random duration within range.
    Returns actual sleep time.
    """
    duration = random.uniform(min_sec, max_sec)
    time.sleep(duration)
    return duration


def jitter(base_sec: float, jitter_pct: float = 0.3) -> float:
    """
    Add random jitter to a base duration.
    
    Args:
        base_sec: Base sleep time in seconds
        jitter_pct: How much jitter to add (0.3 = ±30%)
    
    Returns:
        Sleep time with jitter applied
    """
    jitter_range = base_sec * jitter_pct
    return base_sec + random.uniform(-jitter_range, jitter_range)


# ─────────────────────────────────────────────────────────────────────────────
# Target shuffling
# ─────────────────────────────────────────────────────────────────────────────

def shuffle_targets(targets: list, conservatively: bool = True) -> list:
    """
    Shuffle targets to avoid sequential access patterns.
    
    Args:
        targets: List of event IDs or similar
        conservatively: If True, do a light shuffle; if False, full random
    
    Returns:
        Shuffled list
    """
    if not targets:
        return targets
    
    if conservatively:
        # Light shuffle: swap random pairs, don't fully randomize
        result = targets.copy()
        n_swaps = min(len(result) // 10, 50)
        for _ in range(n_swaps):
            i, j = random.sample(range(len(result)), 2)
            result[i], result[j] = result[j], result[i]
        return result
    else:
        # Full shuffle using Fisher-Yates
        result = targets.copy()
        random.shuffle(result)
        return result


# ─────────────────────────────────────────────────────────────────────────────
# Anti-block mixin for API clients
# ─────────────────────────────────────────────────────────────────────────────

class AntiBlockMixin:
    """
    Mixin class that adds anti-blocking features to API clients.
    
    Usage:
        class MyFetcher(AntiBlockMixin):
            def __init__(self):
                self.init_anti_block(
                    daily_limit=5000,
                    sleep_min=2.0,
                    sleep_max=5.0,
                )
    """
    
    def init_anti_block(
        self,
        daily_limit: int = 5000,
        sleep_min: float = 2.0,
        sleep_max: float = 5.0,
        shuffle_targets_opt: bool = True,
        browse_pages_every: int = 0,  # 0 = disabled
        referer: str = "https://www.sofascore.com/",
    ):
        """
        Initialize anti-blocking features.
        
        Args:
            daily_limit: Max requests per day
            sleep_min: Min sleep between requests (seconds)
            sleep_max: Max sleep between requests (seconds)
            shuffle_targets_opt: Shuffle event IDs to avoid patterns
            browse_pages_every: Fetch a random page every N requests (0 = disabled)
            referer: Base referer URL
        """
        self.counter = RequestCounter(daily_limit=daily_limit)
        self.sleep_min = sleep_min
        self.sleep_max = sleep_max
        self.shuffle_targets_opt = shuffle_targets_opt
        self.browse_pages_every = browse_pages_every
        self.browse_pages_count = 0
        self.referer = referer
    
    def build_headers(self, **kwargs) -> dict:
        """Build headers with anti-block features."""
        return build_headers(referer=self.referer, **kwargs)
    
    def should_sleep(self) -> bool:
        """Check if we should sleep (respects daily limit)."""
        return not self.counter.is_exhausted
    
    def sleep_and_count(self):
        """Sleep and increment request counter."""
        if self.counter.is_exhausted:
            raise RuntimeError(
                f"Daily request limit ({self.counter.daily_limit}) reached. "
                f"Stop fetching until tomorrow."
            )
        
        if self.counter.should_warn():
            print(f"[WARN] Daily usage at {self.counter.usage_pct*100:.1f}% "
                  f"({self.counter.count}/{self.counter.daily_limit})")
            self.counter.acknowledge_warning()
        
        self.counter.increment()
        random_sleep(self.sleep_min, self.sleep_max)
    
    def shuffle(self, targets: list) -> list:
        """Shuffle targets if option enabled."""
        if self.shuffle_targets_opt:
            return shuffle_targets(targets)
        return targets
    
    def maybe_browse_other_page(self, fetch_fn, page_urls: list):
        """
        Occasionally fetch a 'decoy' page to look like human browsing.
        
        Args:
            fetch_fn: Function to make HTTP requests
            page_urls: List of URLs to randomly pick from
        """
        if self.browse_pages_every <= 0 or not page_urls:
            return
        
        self.browse_pages_count += 1
        if self.browse_pages_count % self.browse_pages_every == 0:
            # Time to browse a decoy page
            url = random.choice(page_urls)
            print(f"[DECOY] Fetching: {url}")
            try:
                fetch_fn(url)
                random_sleep(1.0, 2.0)  # Brief pause after decoy
            except Exception as e:
                print(f"[DECOY] Failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Common decoy pages for SofaScore
# ─────────────────────────────────────────────────────────────────────────────

DECOY_PAGES = [
    "https://www.sofascore.com/",
    "https://www.sofascore.com/football",
    "https://www.sofascore.com/tennis",
    "https://www.sofascore.com/basketball",
]


def get_decoy_pages() -> list:
    """Return list of decoy page URLs."""
    return DECOY_PAGES.copy()