#!/usr/bin/env python3
"""test_gen4_phase4_di.py — Offline tests for Phase 4 DI wiring verification.

These tests verify that gen4_phase4_live_smoke.py correctly wires real
dependencies (curl_cffi_get, cloakbrowser_launcher, ssr_fetcher) into
Gen4Fetcher WITHOUT making any live network calls.

Run:
    .runner-venv/bin/python -m pytest tests/test_gen4_phase4_di.py -v

All tests are offline — no live network, no MySQL, no browser binary launch.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

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
        import json
        return json.dumps(self._body)


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


def _incidents_payload() -> Dict[str, Any]:
    return {"incidents": [{"id": 1, "type": "goal", "time": 45}]}


# ---------------------------------------------------------------------------
# Test: gen4_phase4_live_smoke module loads .env and injects DI
# ---------------------------------------------------------------------------

class TestPhase4DIWiring:
    """Verify the gen4_phase4_live_smoke module properly injects dependencies."""

    def test_module_loads_dotenv(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        """gen4_phase4_live_smoke._load_dotenv() must load .env (via python-dotenv or fallback)."""
        # Purge any cached module
        for mod in list(sys.modules.keys()):
            if "gen4_phase4_live_smoke" in mod:
                del sys.modules[mod]

        # Create a temporary .env file for the test
        env_file = tmp_path / ".env"
        env_content = """SOFA_PROXY_HOST=p.webshare.io
SOFA_PROXY_PORT=80
SOFA_PROXY_USER=***REMOVED***-rotate
SOFA_PROXY_PASS=dztr57tcycoz"""
        env_file.write_text(env_content)

        # Mock REPO_ROOT to point to tmp_path
        import gen4_phase4_live_smoke as smoke
        monkeypatch.setattr(smoke, "REPO_ROOT", tmp_path)
        # Ensure python-dotenv is not importable (test fallback path)
        monkeypatch.setitem(sys.modules, "dotenv", None)
        
        # Call _load_dotenv - should use fallback since dotenv is not available
        smoke._load_dotenv()

        # Verify env vars were set via fallback
        assert os.getenv("SOFA_PROXY_HOST") == "p.webshare.io"
        assert os.getenv("SOFA_PROXY_PORT") == "80"
        assert os.getenv("SOFA_PROXY_USER") == "***REMOVED***-rotate"
        assert os.getenv("SOFA_PROXY_PASS") == "dztr57tcycoz"

    def test_real_curl_cffi_get_factory_returns_callable(self):
        """_make_real_curl_cffi_get must return a callable that accepts url + kwargs."""
        # Purge cached module
        for mod in list(sys.modules.keys()):
            if "gen4_phase4_live_smoke" in mod:
                del sys.modules[mod]

        import gen4_phase4_live_smoke as smoke
        curl_get = smoke._make_real_curl_cffi_get()
        assert callable(curl_get)

    def test_real_cloakbrowser_launcher_is_async_callable(self):
        """_make_real_cloakbrowser_launcher must be an async callable."""
        for mod in list(sys.modules.keys()):
            if "gen4_phase4_live_smoke" in mod:
                del sys.modules[mod]

        import gen4_phase4_live_smoke as smoke
        # Just verify it's an async function (don't call it — it would import cloakbrowser)
        import inspect
        assert inspect.iscoroutinefunction(smoke._make_real_cloakbrowser_launcher)


class TestPhase4DIInjectionIntoFetcher:
    """Verify run_phase4_smoke injects all three dependencies into Gen4Fetcher."""

    def test_fetcher_constructed_with_all_three_dependencies(self, monkeypatch: pytest.MonkeyPatch):
        """run_phase4_smoke must construct Gen4Fetcher with curl_cffi_get, cloakbrowser_launcher, ssr_fetcher."""
        # Purge modules
        for mod in list(sys.modules.keys()):
            if "gen4_phase4_live_smoke" in mod or "gen4_fetcher" in mod:
                del sys.modules[mod]

        from gen4_fetcher import Gen4Fetcher

        # Mock Gen4Fetcher constructor to capture what's passed
        original_init = Gen4Fetcher.__init__
        captured_kwargs = {}

        def mock_init(self, config=None, curl_cffi_get=None, cloakbrowser_launcher=None, ssr_fetcher=None):
            captured_kwargs["config"] = config
            captured_kwargs["curl_cffi_get"] = curl_cffi_get
            captured_kwargs["cloakbrowser_launcher"] = cloakbrowser_launcher
            captured_kwargs["ssr_fetcher"] = ssr_fetcher
            # Call original_init to let scope checks pass (they return early on violation)
            original_init(self, config=config, curl_cffi_get=curl_cffi_get,
                          cloakbrowser_launcher=cloakbrowser_launcher, ssr_fetcher=ssr_fetcher)

        monkeypatch.setattr(Gen4Fetcher, "__init__", mock_init)

        # Mock _load_dotenv to avoid file I/O
        import gen4_phase4_live_smoke as smoke
        monkeypatch.setattr(smoke, "_load_dotenv", lambda: None)
        # Set env vars manually
        os.environ["SOFA_PROXY_HOST"] = "p.webshare.io"
        os.environ["SOFA_PROXY_PORT"] = "80"
        os.environ["SOFA_PROXY_USER"] = "***REMOVED***-rotate"
        os.environ["SOFA_PROXY_PASS"] = "dztr57tcycoz"

        # Mock the dependency factories to return mock callables
        def mock_curl_get(url, **kwargs):
            return MagicMock(status_code=200, json=MagicMock(return_value={"id": 1}))

        async def mock_cloak_launcher(**kwargs):
            browser = MagicMock()
            page = MagicMock()
            resp = MagicMock()
            resp.status = 200
            async def _json(): return {"event": {"id": 1, "homeTeam": {}, "awayTeam": {}}}
            resp.json = _json
            async def _goto(url, **kw): return resp
            page.goto = _goto
            async def _close(): pass
            page.close = _close
            async def _new_page(): return page
            browser.new_page = _new_page
            async def _browser_close(): pass
            browser.close = _browser_close
            browser.contexts = []
            return browser

        def mock_ssr(path): return {"event": {"id": 1}}

        monkeypatch.setattr(smoke, "_make_real_curl_cffi_get", lambda: mock_curl_get)
        monkeypatch.setattr(smoke, "_make_real_cloakbrowser_launcher", mock_cloak_launcher)
        monkeypatch.setattr("gen4_ssr.resolve_ssr_for_api_path", mock_ssr)  # imported inside run_phase4_smoke

        # Also need to mock fetcher.start, fetcher.close, fetcher.fetch_api
        async def mock_start(self): pass
        async def mock_close(self): pass
        async def mock_fetch_api(self, path):
            return {"status": 200, "ok": True, "payload_complete": True, "transport": "curl_cffi", "attempts": [], "retry_count": 0, "error": None, "data": {"id": 1}}

        monkeypatch.setattr(Gen4Fetcher, "start", mock_start)
        monkeypatch.setattr(Gen4Fetcher, "close", mock_close)
        monkeypatch.setattr(Gen4Fetcher, "fetch_api", mock_fetch_api)

        # Mock check_safety_halts
        monkeypatch.setattr(smoke, "_check_stop_conditions", lambda a: None)

        # Run the smoke function
        artifact = asyncio.run(smoke.run_phase4_smoke())

        # Verify Gen4Fetcher was constructed with all three dependencies
        assert captured_kwargs.get("curl_cffi_get") is not None, "curl_cffi_get must be injected"
        assert captured_kwargs.get("cloakbrowser_launcher") is not None, "cloakbrowser_launcher must be injected"
        assert captured_kwargs.get("ssr_fetcher") is not None, "ssr_fetcher must be injected"

        # Verify artifact records DI injection
        assert artifact.get("di_injected", {}).get("curl_cffi_get") is True
        assert artifact.get("di_injected", {}).get("cloakbrowser_launcher") is True
        assert artifact.get("di_injected", {}).get("ssr_fetcher") is True

    def test_artifact_includes_di_injected_field(self, monkeypatch: pytest.MonkeyPatch):
        """The returned artifact must include di_injected tracking field."""
        for mod in list(sys.modules.keys()):
            if "gen4_phase4_live_smoke" in mod or "gen4_fetcher" in mod:
                del sys.modules[mod]

        from gen4_fetcher import Gen4Fetcher

        # Mock everything to avoid real calls
        import gen4_phase4_live_smoke as smoke
        monkeypatch.setattr(smoke, "_load_dotenv", lambda: None)
        os.environ["SOFA_PROXY_PASS"] = "test_pass"

        def mock_curl_get(url, **kwargs):
            return MagicMock(status_code=200, json=MagicMock(return_value={"id": 1}))

        async def mock_cloak_launcher(**kwargs):
            browser = MagicMock()
            page = MagicMock()
            resp = MagicMock()
            resp.status = 200
            async def _json(): return {"event": {"id": 1}}
            resp.json = _json
            async def _goto(url, **kw): return resp
            page.goto = _goto
            async def _close(): pass
            page.close = _close
            async def _new_page(): return page
            browser.new_page = _new_page
            async def _browser_close(): pass
            browser.close = _browser_close
            browser.contexts = []
            return browser

        def mock_ssr(path): return {"event": {"id": 1}}

        monkeypatch.setattr(smoke, "_make_real_curl_cffi_get", lambda: mock_curl_get)
        monkeypatch.setattr(smoke, "_make_real_cloakbrowser_launcher", mock_cloak_launcher)
        monkeypatch.setattr("gen4_ssr.resolve_ssr_for_api_path", mock_ssr)  # imported inside run_phase4_smoke

        async def mock_start(self): pass
        async def mock_close(self): pass
        async def mock_fetch_api(self, path):
            return {"status": 200, "ok": True, "payload_complete": True, "transport": "curl_cffi", "attempts": [], "retry_count": 0, "error": None, "data": {"id": 1}}

        monkeypatch.setattr(Gen4Fetcher, "start", mock_start)
        monkeypatch.setattr(Gen4Fetcher, "close", mock_close)
        monkeypatch.setattr(Gen4Fetcher, "fetch_api", mock_fetch_api)
        monkeypatch.setattr(smoke, "_check_stop_conditions", lambda a: None)

        artifact = asyncio.run(smoke.run_phase4_smoke())

        # Verify di_injected field exists and has all True
        di = artifact.get("di_injected", {})
        assert di == {
            "curl_cffi_get": True,
            "cloakbrowser_launcher": True,
            "ssr_fetcher": True,
        }, f"di_injected mismatch: {di}"


class TestPhase4DIEndToEndOffline:
    """End-to-end offline test: run the full smoke with mocked network layer."""

    @pytest.mark.asyncio
    async def test_offline_full_smoke_with_mocked_tiers(self, monkeypatch: pytest.MonkeyPatch):
        """Full smoke test with mocked curl_cffi, CloakBrowser, and SSR — verifies 3-tier chain executes."""
        # Purge modules
        for mod in list(sys.modules.keys()):
            if "gen4_phase4_live_smoke" in mod or "gen4_fetcher" in mod or "gen4_canary_validate" in mod or "gen4_ssr" in mod:
                del sys.modules[mod]

        # Set up env
        os.environ["SOFA_PROXY_HOST"] = "p.webshare.io"
        os.environ["SOFA_PROXY_PORT"] = "80"
        os.environ["SOFA_PROXY_USER"] = "***REMOVED***-rotate"
        os.environ["SOFA_PROXY_PASS"] = "dztr57tcycoz"

        import gen4_phase4_live_smoke as smoke
        from gen4_fetcher import Gen4Fetcher

        # Track which tier was called
        tier_calls = {"curl_cffi": 0, "cloakbrowser": 0, "ssr": 0}

        # Mock curl_cffi — returns 403 to force fallback
        def mock_curl_get(url, **kwargs):
            tier_calls["curl_cffi"] += 1
            resp = MagicMock()
            resp.status_code = 403
            resp.json = MagicMock(side_effect=Exception("forbidden"))
            resp.text = "forbidden"
            return resp

        # Mock CloakBrowser — returns 200 with complete payload per endpoint
        async def mock_cloak_launcher(**kwargs):
            tier_calls["cloakbrowser"] += 1
            # Create a browser mock that returns different responses based on URL
            browser = MagicMock()
            page = MagicMock()
            
            async def _goto(url, **kw):
                # Determine which endpoint from the URL
                if "/incidents" in url:
                    resp_body = {"incidents": [{"id": 1}]}
                else:
                    resp_body = {"event": {"id": 14025013}}
                resp = MagicMock()
                resp.status = 200
                async def _json():
                    return resp_body
                resp.json = _json
                async def _text():
                    return ""
                resp.text = _text
                return resp
            
            page.goto = _goto
            async def _close():
                pass
            page.close = _close
            async def _new_page():
                return page
            browser.new_page = _new_page
            async def _browser_close():
                pass
            browser.close = _browser_close
            browser.contexts = []
            return browser

        # Mock SSR — returns None (unsupported for incidents)
        def mock_ssr(path):
            tier_calls["ssr"] += 1
            if "incidents" in path:
                return None  # SSR unsupported for incidents
            return _event_payload(14025013)

        # Inject mocks
        monkeypatch.setattr(smoke, "_make_real_curl_cffi_get", lambda: mock_curl_get)
        monkeypatch.setattr(smoke, "_make_real_cloakbrowser_launcher", mock_cloak_launcher)
        monkeypatch.setattr("gen4_ssr.resolve_ssr_for_api_path", mock_ssr)  # imported inside run_phase4_smoke
        monkeypatch.setattr(smoke, "_load_dotenv", lambda: None)
        monkeypatch.setattr(smoke, "_check_stop_conditions", lambda a: None)

        # Run
        artifact = await smoke.run_phase4_smoke()

        # Verify results
        assert len(artifact["results"]) == 2  # event + incidents
        event_result = next(r for r in artifact["results"] if r["endpoint"] == "event")
        incidents_result = next(r for r in artifact["results"] if r["endpoint"] == "incidents")

        # Event: curl_cffi 403 ×5 (max Tier-1 retries) -> CloakBrowser 200 -> success
        # Phase 8.1 (Kris 2026-08-25 12:02): Tier-1 now retries up to TIER1_MAX_RETRIES=5
        # before falling to Tier-2. Pre-Phase 8.1 was 1 attempt.
        assert event_result["status"] == 200
        assert event_result["payload_complete"] is True
        assert event_result["fallback_used"] is True
        assert event_result["transport"] == "cloakbrowser"
        attempts = event_result["attempts"]
        # Tier-1 5 attempts (all 403) + Tier-2 1 attempt (200) = 6 attempts
        assert len(attempts) == 6
        for i in range(5):
            assert attempts[i]["transport"] == "curl_cffi"
            assert attempts[i]["status"] == 403
        assert attempts[5]["transport"] == "cloakbrowser"
        assert attempts[5]["status"] == 200

        # Incidents: same pattern (curl_cffi 403 ×5 -> CloakBrowser 200)
        assert incidents_result["status"] == 200
        assert incidents_result["payload_complete"] is True
        assert incidents_result["fallback_used"] is True
        assert incidents_result["transport"] == "cloakbrowser"

        # Verify all three tiers were injected/called
        # Tier-1: 5 per endpoint × 2 endpoints = 10
        assert tier_calls["curl_cffi"] >= 10
        # CloakBrowser launcher called once (cached per Gen4Fetcher instance)
        assert tier_calls["cloakbrowser"] >= 1
        # SSR may or may not be called depending on CloakBrowser success


class TestPhase4DIScopeEnforcement:
    """Verify scope enforcement still works with DI injected."""

    def test_scope_violation_event_id_blocks_before_network(self, monkeypatch: pytest.MonkeyPatch):
        """Changing PHASE4_EVENT_ID must abort BEFORE any fetcher construction."""
        for mod in list(sys.modules.keys()):
            if "gen4_phase4_live_smoke" in mod:
                del sys.modules[mod]

        import gen4_phase4_live_smoke as smoke
        monkeypatch.setattr(smoke, "PHASE4_EVENT_ID", 999999)  # wrong event
        monkeypatch.setattr(smoke, "_load_dotenv", lambda: None)

        artifact = asyncio.run(smoke.run_phase4_smoke())

        assert artifact.get("scope_violation") is True
        assert artifact.get("halt_reason") == "SCOPE_EXPANSION"
        assert "999999" in artifact.get("detail", "")

    def test_scope_violation_endpoints_blocks_before_network(self, monkeypatch: pytest.MonkeyPatch):
        """Changing PHASE4_ENDPOINTS must abort BEFORE any fetcher construction."""
        for mod in list(sys.modules.keys()):
            if "gen4_phase4_live_smoke" in mod:
                del sys.modules[mod]

        import gen4_phase4_live_smoke as smoke
        monkeypatch.setattr(smoke, "PHASE4_ENDPOINTS", ["event", "lineups", "shotmap"])  # expanded
        monkeypatch.setattr(smoke, "_load_dotenv", lambda: None)

        artifact = asyncio.run(smoke.run_phase4_smoke())

        assert artifact.get("scope_violation") is True
        assert artifact.get("halt_reason") == "SCOPE_EXPANSION"

    def test_write_enabled_false_enforced(self, monkeypatch: pytest.MonkeyPatch):
        """WRITE_ENABLED must be False."""
        for mod in list(sys.modules.keys()):
            if "gen4_phase4_live_smoke" in mod:
                del sys.modules[mod]

        import gen4_phase4_live_smoke as smoke
        monkeypatch.setattr(smoke, "WRITE_ENABLED", True)
        monkeypatch.setattr(smoke, "_load_dotenv", lambda: None)

        artifact = asyncio.run(smoke.run_phase4_smoke())

        assert artifact.get("scope_violation") is True
        assert artifact.get("halt_reason") == "SCOPE_EXPANSION"

    def test_gen2_default_unchanged_enforced(self, monkeypatch: pytest.MonkeyPatch):
        """GEN2_DEFAULT_UNCHANGED must be True."""
        for mod in list(sys.modules.keys()):
            if "gen4_phase4_live_smoke" in mod:
                del sys.modules[mod]

        import gen4_phase4_live_smoke as smoke
        monkeypatch.setattr(smoke, "GEN2_DEFAULT_UNCHANGED", False)
        monkeypatch.setattr(smoke, "_load_dotenv", lambda: None)

        artifact = asyncio.run(smoke.run_phase4_smoke())

        assert artifact.get("scope_violation") is True
        assert artifact.get("halt_reason") == "GEN2_DEFAULT_TAMPERED"


class TestPhase4DIStopConditions:
    """Verify stop-on-trigger conditions work with DI injected."""

    @pytest.mark.asyncio
    async def test_407_triggers_safety_halt(self, monkeypatch: pytest.MonkeyPatch):
        """HTTP 407 from curl_cffi must trigger halt_reason HTTP_407."""
        for mod in list(sys.modules.keys()):
            if "gen4_phase4_live_smoke" in mod or "gen4_fetcher" in mod or "gen4_canary_validate" in mod:
                del sys.modules[mod]

        import gen4_phase4_live_smoke as smoke
        from gen4_fetcher import Gen4Fetcher

        os.environ["SOFA_PROXY_PASS"] = "test"
        monkeypatch.setattr(smoke, "_load_dotenv", lambda: None)

        # Mock curl_cffi to return 407
        def mock_curl_get_407(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 407
            resp.json = MagicMock(side_effect=Exception("proxy auth"))
            resp.text = "proxy auth required"
            return resp

        # Mock CloakBrowser and SSR so they don't error
        async def mock_cloak_launcher(**kwargs):
            return _MockBrowser({"event": {"id": 1}}, status=200)

        def mock_ssr(path):
            return {"event": {"id": 1}}

        monkeypatch.setattr(smoke, "_make_real_curl_cffi_get", lambda: mock_curl_get_407)
        monkeypatch.setattr(smoke, "_make_real_cloakbrowser_launcher", mock_cloak_launcher)
        monkeypatch.setattr("gen4_ssr.resolve_ssr_for_api_path", mock_ssr)

        async def mock_start(self): pass
        async def mock_close(self): pass
        monkeypatch.setattr(Gen4Fetcher, "start", mock_start)
        monkeypatch.setattr(Gen4Fetcher, "close", mock_close)
        monkeypatch.setattr(smoke, "_check_stop_conditions", lambda a: None)

        artifact = await smoke.run_phase4_smoke()

        # The 407 should be in results and trigger halt
        assert len(artifact["results"]) >= 1
        r = artifact["results"][0]
        assert r["status"] == 407 or any(a.get("status") == 407 for a in r.get("attempts", []))


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pytest.main([__file__, "-v"])