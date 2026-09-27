#!/usr/bin/env python3
"""gen4_ssr.py — Shared SSR (server-side rendered HTML) loader.

Phase 2 offline refactor: extracts the duplicated `_load_ssr` /
`_ssr_resolver` logic from Profile G and Profile I of
`gen4_canary_validate.py` into one place. Both profiles now share this
module.

Contract:
  - `load_event_ssr(event_id)` returns the parsed `__NEXT_DATA__` JSON for
    a Sofascore event page, or None when the page could not be loaded.
    Cached per process.

  - `resolve_ssr_for_api_path(path)` is the canary/Gen4 resolver: given an
    API path like `/api/v1/event/{eid}/lineups`, it returns the
    `_map_ssr_to_api_format` result (a dict payload for `event`, or None
    for unsupported sub-endpoints).

  - **Missing helper is NOT a silent None**: if `_map_ssr_to_api_format`
    cannot be imported from `gen4_fetcher`, the resolver raises
    `SSRHelperMissing` immediately. Callers MUST catch this and translate
    it to a deterministic attempt with `error='ssr_helper_missing'`. We
    never swallow the ImportError like the G/I Profile wrappers did before
    Phase 2.

Why this lives outside `gen4_fetcher.py`:
  - `gen4_fetcher.py` is the strategy/fetcher layer; loading the actual
    `__NEXT_DATA__` HTML requires Playwright and is a tier-3 specific
    concern. Keeping it here means tests can mock either side in isolation
    and the fetcher remains lean.
  - Playwright is imported lazily inside `load_event_ssr` so the canary
    can import `gen4_ssr` for type inspection without dragging
    playwright in.

Run-time surface (no live calls here — Playwright import is lazy):
    .runner-venv/bin/python -c "from gen4_ssr import resolve_ssr_for_api_path; print(resolve_ssr_for_api_path)"
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
from typing import Any, Callable, Dict, Optional


# Single global cache, lazily initialised on first call.
_SSR_CACHE: Dict[int, Optional[Dict[str, Any]]] = {}
_SSR_CACHE_LOCK = threading.Lock()
_HELPER_MISSING_LOGGED: bool = False


class SSRHelperMissing(ImportError):
    """Raised when `_map_ssr_to_api_format` cannot be imported.

    Phase 2: replaces the Phase-1 silent-None swallow. Callers must handle
    this explicitly (translate to attempt with error='ssr_helper_missing').
    """

    def __init__(self, original: Optional[Exception] = None) -> None:
        msg = (
            "gen4_ssr requires `_map_ssr_to_api_format` from gen4_fetcher, "
            "but the symbol could not be imported. The SSR helper is missing "
            "from the Gen4 fetcher module. Refusing to silently return None — "
            "callers MUST translate this to a deterministic attempt."
        )
        super().__init__(msg)
        self.original = original


def _get_map_helper() -> Callable[[Any, str], Optional[Dict[str, Any]]]:
    """Lazily import `_map_ssr_to_api_format` and surface a typed error
    if it's missing.

    Raises SSRHelperMissing on ImportError.
    """
    global _HELPER_MISSING_LOGGED
    try:
        from gen4_fetcher import _map_ssr_to_api_format  # noqa: WPS433 — intentional lazy import
    except ImportError as e:
        if not _HELPER_MISSING_LOGGED:
            _HELPER_MISSING_LOGGED = True
            # Surface to stderr so the artifact run-log carries the signal.
            import sys
            print(
                f"[gen4_ssr] FATAL: SSR helper missing — {e}",
                file=sys.stderr,
            )
        raise SSRHelperMissing(e) from e
    return _map_ssr_to_api_format


def _playwright_load_event_page(event_id: int, *, url_template: str = "https://www.sofascore.com/event/{eid}") -> Optional[Dict[str, Any]]:
    """Real Playwright load of a Sofascore event page.

    Returns the parsed `__NEXT_DATA__` JSON dict, or None on any failure
    (timeout, parse error, browser launch failure).

    Note: Playwright is imported lazily inside this function so importing
    `gen4_ssr` does NOT require Playwright at module load.

    `url_template` is parameterised so tests can point at a local fixture.
    This function does NOT touch the cache — caching is handled by
    `load_event_ssr` so the cache is testable independently.
    """
    url = url_template.format(eid=event_id)
    try:
        # Lazy import — keeps `import gen4_ssr` free of playwright.
        from playwright.sync_api import sync_playwright  # noqa: WPS433
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto(url, timeout=30000)
                page.wait_for_selector("#__NEXT_DATA__", timeout=15000)
                raw = page.evaluate(
                    "() => { const el = document.querySelector('#__NEXT_DATA__');"
                    " return el ? el.textContent : null; }"
                )
                if raw:
                    return json.loads(raw)
            finally:
                browser.close()
    except Exception:
        return None
    return None


def load_event_ssr(event_id: int, *, url_template: str = "https://www.sofascore.com/event/{eid}",
                    _loader: Optional[Callable[[int], Optional[Dict[str, Any]]]] = None) -> Optional[Dict[str, Any]]:
    """Load the `__NEXT_DATA__` JSON for a Sofascore event page, with caching.

    Returns the parsed JSON dict, or None on any failure (timeout, parse
    error, browser launch failure). Cached per `event_id`.

    Note: Playwright is imported lazily inside the default loader so importing
    `gen4_ssr` does NOT require Playwright at module load.

    `url_template` is parameterised so tests can point at a local fixture.
    `_loader` is an optional override so tests can inject a fake loader
    without touching Playwright (Phase 3 contract).
    """
    with _SSR_CACHE_LOCK:
        if event_id in _SSR_CACHE:
            return _SSR_CACHE[event_id]

    if _loader is not None:
        payload = _loader(event_id)
    else:
        payload = _playwright_load_event_page(event_id, url_template=url_template)

    with _SSR_CACHE_LOCK:
        _SSR_CACHE[event_id] = payload
    return payload


def resolve_ssr_for_api_path(path: str, *, event_id: Optional[int] = None,
                              url_template: str = "https://www.sofascore.com/event/{eid}",
                              _loader: Optional[Callable[[int], Optional[Dict[str, Any]]]] = None) -> Optional[Dict[str, Any]]:
    """Resolve an API path to its SSR-derived payload.

    Args:
      path: API path like "/api/v1/event/{eid}" or "/api/v1/event/{eid}/lineups".
      event_id: optional override; if absent, parsed from path.
      url_template: SSR URL template (parameterised for tests).
      _loader: optional loader override (for tests). Signature:
                 (event_id: int) -> Optional[Dict[str, Any]].

    Returns:
      - dict payload for endpoints that SSR supports (currently only "event"),
        shaped like the API response (e.g. {"event": {...}}).
      - None for endpoints that SSR structurally CANNOT serve (e.g. incidents,
        lineups, statistics). The caller is expected to translate this to
        a deterministic attempt with `error='ssr_unsupported_endpoint'`.

    Raises:
      SSRHelperMissing when `_map_ssr_to_api_format` cannot be imported.
      This is a FATAL signal — callers MUST NOT swallow it.

    Cache: SSR loads are cached per event_id so calling this N times for
    the same event_id hits the same payload.
    """
    if not path:
        return None
    m = re.search(r"/event/(\d+)(?:/([^/?]+))?", path)
    if not m:
        return None
    eid = event_id if event_id is not None else int(m.group(1))
    endpoint = m.group(2) or "event"

    # Fast-fail BEFORE invoking the loader for structurally unsupported
    # endpoints (lineups, incidents, statistics, ...). The mapping function
    # in gen4_fetcher would return None for these anyway, but we save the
    # Playwright roundtrip + worker-thread scheduling overhead.
    from gen4_fetcher import SSR_UNSUPPORTED_ENDPOINTS
    if endpoint in SSR_UNSUPPORTED_ENDPOINTS:
        return None

    # Phase 2 explicit contract: surface missing helper, do NOT silently return None.
    map_helper = _get_map_helper()

    loader = _loader if _loader is not None else (
        lambda _eid: load_event_ssr(_eid, url_template=url_template)
    )
    # load_event_ssr now handles its own cache, so call it directly when
    # no _loader override is supplied.
    if _loader is not None:
        raw = loader(eid)
    else:
        raw = load_event_ssr(eid, url_template=url_template)
    if raw is None:
        return None
    return map_helper(raw, path)


# ---------------------------------------------------------------------------
# Async executor wrapper (Phase 3)
# ---------------------------------------------------------------------------
#
# Production async path: `Gen4Fetcher._fetch_via_ssr` runs inside an
# asyncio event loop and must NOT block on Playwright (which is sync).
# These async wrappers dispatch to the sync loader via `asyncio.to_thread`
# and re-raise `SSRHelperMissing` so the caller still sees the typed error.
#
# Invariant: read-only. These wrappers do NOT touch MySQL, the file system
# beyond the process-level cache, or any write-enabled code path.


async def aload_event_ssr(event_id: int, *, url_template: str = "https://www.sofascore.com/event/{eid}") -> Optional[Dict[str, Any]]:
    """Async wrapper around `load_event_ssr`. Dispatches to a worker thread.

    Returns:
      - parsed `__NEXT_DATA__` dict on success, or
      - None on any failure (timeout, parse error, browser launch failure).

    Caching is identical to the sync version: per-event_id, process-level.

    Raises:
      SSRHelperMissing — re-raised so async callers MUST handle the
      typed error explicitly. Phase 2 contract: never silently swallowed.
    """
    try:
        return await asyncio.to_thread(load_event_ssr, event_id, url_template=url_template)
    except SSRHelperMissing:
        raise
    except Exception:
        return None


async def aresolve_ssr_for_api_path(path: str, *, event_id: Optional[int] = None,
                                      url_template: str = "https://www.sofascore.com/event/{eid}",
                                      _loader: Optional[Callable[[int], Optional[Dict[str, Any]]]] = None) -> Optional[Dict[str, Any]]:
    """Async wrapper around `resolve_ssr_for_api_path`.

    Mirrors the sync resolver's contract:
      - returns dict payload for SSR-supported endpoints (event),
      - returns None for SSR-unsupported endpoints (FAST-FAIL before loader),
      - raises SSRHelperMissing when `_map_ssr_to_api_format` is missing.

    The `_loader` parameter lets tests inject a fake loader without
    touching Playwright. In production `_loader` is None and the resolver
    uses `aload_event_ssr` (which runs Playwright in a worker thread).
    """
    # Re-implement path parsing + cache lookup in async land so we do
    # not block the event loop on a sync function. _loader stays sync.
    if not path:
        return None
    m = re.search(r"/event/(\d+)(?:/([^/?]+))?", path)
    if not m:
        return None
    eid = event_id if event_id is not None else int(m.group(1))
    endpoint = m.group(2) or "event"

    # Fast-fail BEFORE invoking the loader for structurally unsupported
    # endpoints (lineups, incidents, statistics, ...). Saves the Playwright
    # roundtrip + worker-thread scheduling overhead.
    # Lazy import avoids a circular dependency at module load (gen4_fetcher
    # imports SSRHelperMissing from gen4_ssr).
    from gen4_fetcher import SSR_UNSUPPORTED_ENDPOINTS
    if endpoint in SSR_UNSUPPORTED_ENDPOINTS:
        return None

    # Phase 2 explicit contract: surface missing helper, do NOT silently return None.
    map_helper = _get_map_helper()

    if _loader is not None:
        raw = _loader(eid)
    else:
        try:
            raw = await aload_event_ssr(eid, url_template=url_template)
        except SSRHelperMissing:
            raise
    if raw is None:
        return None
    return map_helper(raw, path)
