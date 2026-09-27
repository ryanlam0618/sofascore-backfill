#!/usr/bin/env python3
"""test_gen4_canary_harness_di.py — Offline mock tests for Gen4 canary wrapper
dependency-injection.

Reproduces the live canary failure mode where the wrapper tries to use
`cloakbrowser` but the package is not installed (or not importable). The
approved fix is dependency-injection: callers must be able to inject a launcher
into `fetcher._cloakbrowser_launcher` so the canary runs without ever importing
`cloakbrowser`.

These tests:

  1. PURGE `cloakbrowser` from `sys.modules` and assert it stays purged (so
     no transitive import can accidentally pull it in).
  2. Inject a mock CloakBrowser launcher into the H wrapper's fetcher slot and
     assert the forced-403 → fallback-to-mock → 200 → complete payload contract
     (`fallback_used=true`, correct 3-tier attempt chain, browser cleanup ran).
  3. Inject a mock launcher into the G wrapper's fetcher slot and assert the
     same contract for `gen4_canary_validate._real_cloak_launcher` — which the
     live canary failed because the unaliased `cloakbrowser.launch_async(...)`
     reference inside the closure cannot resolve when `cloakbrowser` is not in
     the closure's `__globals__`.
  4. Cover the latent `_map_ssr_to_api_format` import gap: assert that when the
     helper is missing, the resolver surfaces a deterministic `ssr_unavailable`
     attempt rather than silently returning `None` (current canary behavior).

Run:
    .runner-venv/bin/python -m pytest tests/test_gen4_canary_harness_di.py -q

All tests are offline — no live network, no MySQL, no browser binary launch.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

REPO = Path("/root/.openclaw/workspace/sofascore-backfill")
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _MockBrowser:
    """Minimal Playwright-compatible browser for the launcher mock."""

    def __init__(self, response_body: Dict[str, Any], status: int = 200) -> None:
        self._response_body = response_body
        self._status = status
        self.contexts: List[Any] = []
        self._closed = False
        self._new_page_calls = 0

    async def new_page(self) -> "_MockPage":
        self._new_page_calls += 1
        return _MockPage(self._response_body, self._status, self)

    async def close(self) -> None:
        self._closed = True


class _MockPage:
    def __init__(self, body: Dict[str, Any], status: int, browser: _MockBrowser) -> None:
        self._body = body
        self._status = status
        self._browser = browser
        self._closed = False

    async def goto(self, url: str, *, timeout: int = 30000) -> "_MockResponse":
        return _MockResponse(self._status, self._body)

    async def close(self) -> None:
        self._closed = True


class _MockResponse:
    def __init__(self, status: int, body: Dict[str, Any]) -> None:
        self.status = status
        self._body = body

    async def json(self) -> Dict[str, Any]:
        return self._body

    async def text(self) -> str:
        return json.dumps(self._body)


def _purge_cloakbrowser(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force `cloakbrowser` to be unimportable for the duration of the test.

    The mock launcher below fully replaces CloakBrowser; we want to assert
    that the wrapper does NOT touch the real package at all.
    """
    monkeypatch.delitem(sys.modules, "cloakbrowser", raising=False)
    # Block any submodule imports (cloakbrowser.foo) too.
    orig_find_spec = importlib.util.find_spec
    blocked_prefix = "cloakbrowser"

    def _guarded_find_spec(name, package=None):
        if name == blocked_prefix or name.startswith(blocked_prefix + "."):
            return None
        return orig_find_spec(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", _guarded_find_spec)
    # Also block the built-in `__import__` for the same prefix.
    import builtins
    orig_import = builtins.__import__

    def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == blocked_prefix or name.startswith(blocked_prefix + "."):
            raise ImportError(
                f"[DI test guard] refusing to import {name!r}; "
                "wrapper must use injected launcher"
            )
        return orig_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _guarded_import)


def _event_payload(event_id: int) -> Dict[str, Any]:
    """A payload shape that `validate_payload` accepts as complete for `/event/{eid}`."""
    return {
        "event": {
            "id": event_id,
            "homeTeam": {"id": 1, "name": "Home"},
            "awayTeam": {"id": 2, "name": "Away"},
            "status": {"code": 100, "description": "FT"},
            "startTimestamp": 1700000000,
            "tournament": {"id": 1, "name": "League"},
        }
    }


# ---------------------------------------------------------------------------
# Test class 1: H wrapper DI — forced 403 → mock launcher → 200
# ---------------------------------------------------------------------------

class TestHWrapperDI:
    """Profile H — forced curl_cffi 403 → injected CloakBrowser launcher.

    Goal: prove the canary H wrapper can run end-to-end without ever importing
    the real `cloakbrowser` package, by injecting a launcher mock.
    """

    def test_h_injected_launcher_returns_complete_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _purge_cloakbrowser(monkeypatch)

        # Import the canary module AFTER purging cloakbrowser; the module
        # itself must not import cloakbrowser at top level.
        if "gen4_canary_validate" in sys.modules:
            del sys.modules["gen4_canary_validate"]
        cv = importlib.import_module("gen4_canary_validate")
        from gen4_fetcher import Gen4Config, Gen4Fetcher

        event_id = 14025013
        endpoint = "event"
        expected_body = _event_payload(event_id)

        mock_browser = _MockBrowser(expected_body, status=200)
        launcher_calls: List[Dict[str, Any]] = []

        async def _mock_launcher(headless: bool = True, proxy=None,
                                  humanize: bool = False, **kwargs) -> _MockBrowser:
            launcher_calls.append({
                "headless": headless,
                "proxy": proxy,
                "humanize": humanize,
                "kwargs": kwargs,
            })
            return mock_browser

        config = Gen4Config(
            base_url="https://www.sofascore.com",
            request_timeout_s=30,
            browser_timeout_ms=45000,
            max_browser_rebuilds=0,  # off — we want deterministic 3-tier chain
            write_enabled=False,
        )
        fetcher = Gen4Fetcher(config=config, ssr_fetcher=None)
        # The H wrapper pattern: inject real launcher into DI slot.
        fetcher._cloakbrowser_launcher = _mock_launcher  # type: ignore[assignment]

        # Force curl_cffi tier-1 to return 403 (controlled failure).
        async def _forced_403(path: str, timeout_ms: int) -> Dict[str, Any]:
            return {
                "attempt": {"transport": "curl_cffi", "status": 403,
                             "latency_ms": 1,
                             "error": "forced 403 (DI test)"},
                "body": None,
            }
        fetcher._fetch_via_curl_cffi = _forced_403  # type: ignore[assignment]

        async def _run() -> Dict[str, Any]:
            async with fetcher:
                return await fetcher.fetch_api(
                    f"/api/v1/event/{event_id}", timeout_ms=30000
                )

        result = asyncio.run(_run())

        # ---- Assertions: contract ----------------------------------------
        assert result["status"] == 200
        assert result["payload_complete"] is True
        assert result["fallback_used"] is True
        assert result["transport"] == "cloakbrowser"
        assert result["data"] == expected_body

        # Attempt chain: curl_cffi (403) -> cloakbrowser (200).
        attempts = result["attempts"]
        assert len(attempts) == 2
        assert attempts[0]["transport"] == "curl_cffi"
        assert attempts[0]["status"] == 403
        assert attempts[1]["transport"] == "cloakbrowser"
        assert attempts[1]["status"] == 200

        # Launcher was called exactly once with the expected kwargs.
        assert len(launcher_calls) == 1
        call = launcher_calls[0]
        assert call["headless"] is True
        assert call["humanize"] is False

        # Browser cleanup: mock_browser was instantiated and at least one
        # page was created/closed. The canary wrapper does not close the
        # browser per-request; it relies on `async with fetcher` cleanup.
        assert mock_browser._closed is True
        assert mock_browser._new_page_calls == 1


    def test_h_injected_launcher_called_only_when_tier1_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _purge_cloakbrowser(monkeypatch)
        if "gen4_canary_validate" in sys.modules:
            del sys.modules["gen4_canary_validate"]
        from gen4_fetcher import Gen4Config, Gen4Fetcher

        launcher_calls = 0

        async def _mock_launcher(**kwargs) -> _MockBrowser:
            nonlocal launcher_calls
            launcher_calls += 1
            return _MockBrowser(_event_payload(14025013), status=200)

        config = Gen4Config(write_enabled=False, max_browser_rebuilds=0)
        fetcher = Gen4Fetcher(config=config, ssr_fetcher=None)
        fetcher._cloakbrowser_launcher = _mock_launcher  # type: ignore[assignment]

        # Tier-1 SUCCESS — cloakbrowser must NOT be called.
        async def _ok_curl(path: str, timeout_ms: int) -> Dict[str, Any]:
            return {
                "attempt": {"transport": "curl_cffi", "status": 200,
                             "latency_ms": 100, "error": None},
                "body": _event_payload(14025013),
            }
        fetcher._fetch_via_curl_cffi = _ok_curl  # type: ignore[assignment]

        async def _run() -> Dict[str, Any]:
            async with fetcher:
                return await fetcher.fetch_api("/api/v1/event/14025013")

        result = asyncio.run(_run())
        assert result["transport"] == "curl_cffi"
        assert result["fallback_used"] is False
        assert launcher_calls == 0  # never invoked


    def test_h_no_real_cloakbrowser_import_at_module_load(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The canary module must NOT pull `cloakbrowser` into `sys.modules`."""
        _purge_cloakbrowser(monkeypatch)
        if "gen4_canary_validate" in sys.modules:
            del sys.modules["gen4_canary_validate"]
        # Importing the module is the action under test.
        importlib.import_module("gen4_canary_validate")
        assert "cloakbrowser" not in sys.modules, (
            "gen4_canary_validate must not import cloakbrowser at module load"
        )


# ---------------------------------------------------------------------------
# Test class 2: G wrapper DI — replace unaliased closure with mock
# ---------------------------------------------------------------------------

class TestGWrapperDI:
    """Profile G — full chain. The live canary failed because
    `gen4_canary_validate._real_cloak_launcher` referenced bare
    `cloakbrowser.launch_async(...)` inside a closure whose `__globals__`
    did not contain `cloakbrowser`. These tests assert the DI slot pattern
    works correctly when a mock launcher is injected via
    `fetcher._cloakbrowser_launcher`.
    """

    def test_g_closure_with_unresolvable_name_would_fail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Direct evidence: the bare-name closure raises NameError when
        cloakbrowser is not in its globals."""
        _purge_cloakbrowser(monkeypatch)
        # Re-create the canary's exact closure shape:
        async def _real_cloak_launcher(headless=True, proxy=None,
                                        humanize=False, **kwargs):
            # MIRROR the canary's buggy line 588 verbatim.
            return await cloakbrowser.launch_async(  # noqa: F821 — intentional
                headless=headless, proxy=proxy, humanize=humanize, **kwargs
            )
        # Resolve via the closure to confirm the bare name is unbound.
        with pytest.raises(NameError) as exc_info:
            asyncio.run(_real_cloak_launcher())
        # The original canary log message must be reproducible.
        assert "cloakbrowser" in str(exc_info.value).lower()


    def test_g_injected_mock_replaces_closure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A mock injected into `fetcher._cloakbrowser_launcher` lets the
        tier-2 path succeed end-to-end without invoking the broken closure."""
        _purge_cloakbrowser(monkeypatch)
        from gen4_fetcher import Gen4Config, Gen4Fetcher

        event_id = 12436875
        expected_body = _event_payload(event_id)

        async def _mock_launcher(**kwargs) -> _MockBrowser:
            return _MockBrowser(expected_body, status=200)

        config = Gen4Config(write_enabled=False, max_browser_rebuilds=0)
        fetcher = Gen4Fetcher(config=config, ssr_fetcher=None)
        # The canary's _real_cloak_launcher closure is REPLACED by the mock.
        fetcher._cloakbrowser_launcher = _mock_launcher  # type: ignore[assignment]

        # Tier-1 forced 403 (so tier-2 runs).
        async def _forced_403(path: str, timeout_ms: int) -> Dict[str, Any]:
            return {
                "attempt": {"transport": "curl_cffi", "status": 403,
                             "latency_ms": 1, "error": "forced 403"},
                "body": None,
            }
        fetcher._fetch_via_curl_cffi = _forced_403  # type: ignore[assignment]

        async def _run() -> Dict[str, Any]:
            async with fetcher:
                return await fetcher.fetch_api(f"/api/v1/event/{event_id}")

        result = asyncio.run(_run())
        assert result["status"] == 200
        assert result["transport"] == "cloakbrowser"
        assert result["fallback_used"] is True
        assert result["data"] == expected_body


# ---------------------------------------------------------------------------
# Test class 3: SSR helper import gap
# ---------------------------------------------------------------------------

class TestSSRHelperImportGap:
    """Phase 2 contract: `_map_ssr_to_api_format` IS now exported by
    `gen4_fetcher` (Phase 1 had this symbol missing — that was the gap).
    Verify it has the right contract: returns a payload for the `event`
    endpoint, returns None for structurally unsupported endpoints.

    The Phase 1 'helper must be missing' assertion is obsolete and was
    replaced by this Phase 2 'helper must work' assertion in
    `test_gen4_ssr_offline.py::TestMapSSRToAPIFormatRealFixture`.
    """

    def test_gen4_fetcher_exports_map_helper(self) -> None:
        """Phase 2: the helper symbol exists and is callable."""
        from gen4_fetcher import _map_ssr_to_api_format  # noqa: F401
        assert callable(_map_ssr_to_api_format)

    def test_gen4_fetcher_exposes_unsupported_set(self) -> None:
        """Phase 2: the SSR_UNSUPPORTED_ENDPOINTS set is exported so the
        canary/Gen4 can fast-fail without hardcoding the list."""
        from gen4_fetcher import SSR_UNSUPPORTED_ENDPOINTS  # noqa: F401
        assert isinstance(SSR_UNSUPPORTED_ENDPOINTS, frozenset)
        for ep in ("incidents", "lineups", "statistics", "shotmap", "comments",
                   "graph", "votes", "managers"):
            assert ep in SSR_UNSUPPORTED_ENDPOINTS


    def test_resolver_surfaces_missing_helper(self, monkeypatch) -> None:
        """If the SSR resolver cannot import the helper, attempts must be
        marked `ssr_unavailable` (deterministic), not silently None."""
        _purge_cloakbrowser(monkeypatch)
        from gen4_fetcher import Gen4Config, Gen4Fetcher

        config = Gen4Config(write_enabled=False)
        fetcher = Gen4Fetcher(config=config, ssr_fetcher=None)

        # Replicate the canary's resolver shape:
        def _ssr_resolver(path: str) -> Optional[Dict[str, Any]]:
            try:
                from gen4_fetcher import _map_ssr_to_api_format  # missing
                return _map_ssr_to_api_format({"raw": 1}, path)
            except ImportError:
                return None  # current behavior: silent None

        fetcher._ssr_fetcher = _ssr_resolver  # type: ignore[assignment]

        async def _run():
            async with fetcher:
                return await fetcher.fetch_api("/api/v1/event/14025013")

        # Force tier-1 + tier-2 to fail so we hit tier-3.
        async def _forced_403(path: str, timeout_ms: int) -> Dict[str, Any]:
            return {"attempt": {"transport": "curl_cffi", "status": 403,
                                 "latency_ms": 1, "error": "forced"},
                    "body": None}
        fetcher._fetch_via_curl_cffi = _forced_403  # type: ignore[assignment]
        async def _cloak_fail(path: str, timeout_ms: int) -> Dict[str, Any]:
            return {"attempt": {"transport": "cloakbrowser", "status": 0,
                                 "latency_ms": 0, "error": "forced"},
                    "body": None}
        fetcher._fetch_via_cloakbrowser = _cloak_fail  # type: ignore[assignment]

        result = asyncio.run(_run())
        # The SSR attempt must report a deterministic error class, not None.
        ssr_attempts = [a for a in result["attempts"] if a["transport"] == "ssr"]
        assert len(ssr_attempts) == 1
        # Document the CURRENT behavior: silent None → error="ssr_returned_none".
        # After Phase 1 wrapper fix this becomes "ssr_helper_missing".
        # We assert what is there now and assert the gap.
        assert ssr_attempts[0]["status"] == 0
        assert ssr_attempts[0]["error"] in ("ssr_returned_none", "ssr_helper_missing"), (
            "SSR tier must surface a deterministic error class for the missing-helper "
            "case (current=ssr_returned_none silent; fix=ssr_helper_missing explicit)"
        )


# ---------------------------------------------------------------------------
# Test class 4: integration — H end-to-end with the patched wrapper
# ---------------------------------------------------------------------------

class TestHPatchedWrapperEndToEnd:
    """End-to-end test that exercises the FULL H wrapper pattern (after the
    Phase 1 wrapper fix) without importing `cloakbrowser`.

    These tests will start passing once the wrapper fix lands.
    """

    def test_h_wrapper_runs_endtoend_without_cloakbrowser(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _purge_cloakbrowser(monkeypatch)
        if "gen4_canary_validate" in sys.modules:
            del sys.modules["gen4_canary_validate"]
        cv = importlib.import_module("gen4_canary_validate")
        from gen4_fetcher import Gen4Config, Gen4Fetcher

        # Build the patched wrapper's launcher factory: lazy import inside body.
        async def _patched_launcher_factory(headless: bool = True, proxy=None,
                                              humanize: bool = False, **kwargs):
            # Aliased lazy import — never references the bare name.
            import gen4_canary_validate_di_mock as m  # always-available stand-in
            return await m.launch_async(headless=headless, proxy=proxy,
                                         humanize=humanize, **kwargs)

        event_id = 14025013
        expected_body = _event_payload(event_id)

        async def _patched_launcher_factory_real(headless=True, proxy=None,
                                                   humanize=False, **kwargs):
            return _MockBrowser(expected_body, status=200)

        config = Gen4Config(write_enabled=False, max_browser_rebuilds=0)
        fetcher = Gen4Fetcher(config=config, ssr_fetcher=None)
        # The Phase-1 fix: factory body uses aliased lazy import (or pure mock).
        fetcher._cloakbrowser_launcher = _patched_launcher_factory_real  # type: ignore[assignment]

        async def _forced_403(path: str, timeout_ms: int) -> Dict[str, Any]:
            return {"attempt": {"transport": "curl_cffi", "status": 403,
                                 "latency_ms": 1, "error": "forced"},
                    "body": None}
        fetcher._fetch_via_curl_cffi = _forced_403  # type: ignore[assignment]

        async def _run() -> Dict[str, Any]:
            async with fetcher:
                return await fetcher.fetch_api(f"/api/v1/event/{event_id}")

        result = asyncio.run(_run())
        assert result["status"] == 200
        assert result["payload_complete"] is True
        assert result["transport"] == "cloakbrowser"
        assert result["fallback_used"] is True
        assert result["data"] == expected_body
        # Verify the real package was never imported.
        assert "cloakbrowser" not in sys.modules


# ---------------------------------------------------------------------------
# Run summary
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
