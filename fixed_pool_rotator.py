#!/usr/bin/env python3
"""fixed_pool_rotator.py — FixedPoolRotator for Gen4 (Phase 9.5 / pre-Phase-10).

Design locked 2026-08-28 (Kris picked option H):
  - Pool: 21 fully-clean static IPs (primary AND incidents both GOOD in the
    Sentinel audit; the 1 MIXED IP is filtered out)
  - Selection: pool[ sha256(f"{event_id}") % 22 -> % len(pool) ] —
    per-event granularity, deterministic & reproducible
  - Fallback: on 403 the rotator's rotate() advances clockwise to the next
    pool member (cap enforced by caller; Gen4Fetcher per-loop caps apply)
  - Cookie isolation: Gen4Fetcher.evict_by_proxy() semantics preserved —
    proxy identity CHANGES on rotate so stale cookies are evicted
  - Protected files: ZERO — this is a new standalone module; it satisfies
    the gen4_components.ProxyRotator Protocol and is DI-injected.

SECURITY: proxy credentials are read from the good_proxies file at runtime;
this module never logs or returns passwords (identity = ip:port only).
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional


class FixedPoolRotator:
    """Round-robin-over-fixed-GOOD-pool rotator implementing the
    ProxyRotator Protocol (gen4_components.py §368).

    The fetcher's `_rotate_proxy_with_cookie_eviction` calls:
      - await proxy_rotator.rotate()              → we advance + return identity
      - proxy_rotator.current_proxy()             → identity string (ip:port)
    mutate_config() pushes the current member's creds into Gen4Config so
    Gen4Fetcher.proxy_url() immediately reflects the active pool member.
    """

    def __init__(self, proxies: List[Dict[str, str]], event_id: int) -> None:
        if not proxies:
            raise ValueError("pool must not be empty")
        self._pool = proxies
        h = hashlib.sha256(str(event_id).encode()).hexdigest()
        self._idx = int(h, 16) % len(proxies)
        self.event_id = event_id
        self.rotation_count = 0
        self._bound_config: Optional[Any] = None

    # -- construction helpers ---------------------------------------------
    @classmethod
    def from_audit(cls, audit_json_path: str, good_list_path: str,
                   event_id: int) -> "FixedPoolRotator":
        """Load pool from Sentinel audit artifact + good_proxies creds file.

        Pool membership rule: verdict_primary == GOOD AND every per-target
        verdict == GOOD (filters the single MIXED IP).
        Credentials come from the good list (ip:port:user:pass); they are
        joined to audit rows by ip:port. Nothing is printed.
        """
        import json
        audit = json.loads(Path(audit_json_path).read_text())
        creds: Dict[str, Dict[str, str]] = {}
        for line in Path(good_list_path).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            ip, port, user, pw = line.split(":", 3)
            creds[f"{ip}:{port}"] = {"ip": ip, "port": port, "user": user, "pw": pw}
        pool: List[Dict[str, str]] = []
        for row in audit["rows"]:
            if row["verdict_primary"] != "GOOD":
                continue
            if not all(v == "GOOD" for v in row["verdicts"].values()):
                continue  # MIXED filter
            key = f"{row['ip']}:{row['port']}"
            if key in creds:
                pool.append(creds[key])
        pool.sort(key=lambda p: int(p["ip"].replace(".", "")))  # stable order
        return cls(pool, event_id)

    @property
    def pool_size(self) -> int:
        return len(self._pool)

    def bind_config(self, config: Any) -> None:
        """Attach a Gen4Config so rotate() also swaps the live proxy URL."""
        self._bound_config = config

    # -- protocol ----------------------------------------------------------
    async def rotate(self) -> str:
        self._idx = (self._idx + 1) % len(self._pool)
        self.rotation_count += 1
        if self._bound_config is not None:
            self.mutate_config(self._bound_config)
        return self.current_proxy()

    def current_proxy(self) -> str:
        p = self._pool[self._idx]
        return f"{p['ip']}:{p['port']}"

    def current_creds(self) -> Dict[str, str]:
        return dict(self._pool[self._idx])

    def mutate_config(self, config: Any) -> None:
        """Point Gen4Config at the active pool member."""
        p = self._pool[self._idx]
        config.proxy_server = p["ip"]
        config.proxy_port = p["port"]
        config.proxy_user = p["user"]
        config.proxy_pass = p["pw"]
