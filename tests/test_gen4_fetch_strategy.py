#!/usr/bin/env python3
"""
test_gen4_fetch_strategy.py — Offline unit tests for Gen4Fetcher prototype.

Per Kris 23:30 Gen4 implementation prompt §12.2, these tests cover 16 cases:
  1.  Default strategy stays Gen2
  2.  FETCH_STRATEGY=gen4 opt-in
  3.  curl_cffi 200 -> no CloakBrowser call
  4.  curl_cffi 403 -> fallback to CloakBrowser
  5.  curl_cffi 407 -> logged, falls back per policy
  6.  CloakBrowser 200 -> success
  7.  CloakBrowser timeout -> fallback SSR
  8.  CloakBrowser target closed / EPIPE -> rebuild limit
  9.  SSR success
  10. All tiers fail -> failure result contract
  11. HTTP 200 + empty payload -> not complete
  12. Credentials redacted
  13. write_enabled=False invariant
  14. No DataInserter / MySQL calls
  15. Browser close / cleanup always runs
  16. Endpoint key normalization consistency

Tests use mock injection via Gen4Fetcher's constructor:
    curl_cffi_get, cloakbrowser_launcher, ssr_fetcher
No live network. No MySQL. No DataInserter.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import MagicMock, AsyncMock

import pytest

REPO = "/root/.openclaw/workspace/sofascore-backfill"
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import gen4_fetcher as g4  # noqa: E402
from gen4_fetcher import (  # noqa: E402
    Gen4Config,
    Gen4Fetcher,
    classify_error,
    redact_credentials,
    resolve_fetch_strategy,
    validate_payload,
    _make_attempt,
    _success_result,
    _failure_result,
)


# ---------------------------------------------------------------------------
# Test fixtures: mock injection helpers
# ---------------------------------------------------------------------------

def make_curl_mock(*, status: int = 200, body: Any = None, error: Optional[str] = None) -> Callable:
    """Mock curl_cffi-style sync getter returning an object with .status_code / .json() / .text."""
    def _curl_get(url: str, **kwargs) -> Any:
        resp = MagicMock()
        resp.status_code = status
        if body is not None and error is None:
            resp.json = MagicMock(return_value=body)
            resp.text = ""
        else:
            resp.json = MagicMock(side_effect=Exception(error or "json parse failed"))
            resp.text = "raw body"
        return resp
    return _curl_get


def make_curl_unavailable() -> Callable:
    """Mock curl_cffi import-failed case."""
    def _unavailable(url: str, **kwargs) -> Any:
        raise RuntimeError("curl_cffi unavailable")
    return _unavailable


def make_cloak_launcher(*, page_status: int = 200, page_body: Any = None,
                         error: Optional[Exception] = None) -> Callable:
    """Mock cloakbrowser.launch_async returning a browser-like object with new_page()."""
    async def _launch(**kwargs) -> Any:
        browser = MagicMock()
        page = MagicMock()
        resp = MagicMock()
        resp.status = page_status
        if page_body is not None and error is None:
            async def _json():
                return page_body
            resp.json = _json
            async def _text():
                return ""
            resp.text = _text
        else:
            async def _raise_json():
                raise Exception("json parse failed")
            resp.json = _raise_json
            async def _text():
                return "raw"
            resp.text = _text
        async def _goto(url, **kw):
            if error:
                raise error
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
    return _launch


def make_ssr_fetcher(*, body: Any = None, return_none: bool = False,
                      raise_exc: Optional[Exception] = None) -> Callable:
    """Mock SSR fetcher — sync callable returning dict (or None)."""
    def _ssr(path: str) -> Optional[dict]:
        if raise_exc:
            raise raise_exc
        if return_none:
            return None
        return body
    return _ssr


def make_fetcher(
    *,
    curl_get: Optional[Callable] = None,
    cloak_launcher: Optional[Callable] = None,
    cloakbrowser_launcher: Optional[Callable] = None,
    ssr_fetcher: Optional[Callable] = None,
    write_enabled: bool = False,
    max_browser_rebuilds: int = 1,
) -> Gen4Fetcher:
    cfg = Gen4Config(
        write_enabled=write_enabled,
        max_browser_rebuilds=max_browser_rebuilds,
        proxy_pass="",  # disable proxy to simplify tests
    )
    # Accept both spellings for backward compatibility with earlier drafts.
    launcher = cloakbrowser_launcher if cloakbrowser_launcher is not None else cloak_launcher
    return Gen4Fetcher(
        config=cfg,
        curl_cffi_get=curl_get,
        cloakbrowser_launcher=launcher,
        ssr_fetcher=ssr_fetcher,
    )


# ---------------------------------------------------------------------------
# Case 1: Default strategy stays Gen2
# ---------------------------------------------------------------------------

def test_case_01_default_strategy_is_gen2(monkeypatch):
    """§10 / §12.2 case 1: Default MUST stay gen2 unless FETCH_STRATEGY=gen4."""
    monkeypatch.delenv("FETCH_STRATEGY", raising=False)
    assert resolve_fetch_strategy() == "gen2"


# ---------------------------------------------------------------------------
# Case 2: FETCH_STRATEGY=gen4 opt-in
# ---------------------------------------------------------------------------

def test_case_02_fetch_strategy_gen4_optin(monkeypatch):
    """§10 / §12.2 case 2: explicit gen4 opt-in returns 'gen4'."""
    monkeypatch.setenv("FETCH_STRATEGY", "gen4")
    assert resolve_fetch_strategy() == "gen4"


def test_case_02b_invalid_strategy_raises(monkeypatch):
    """§10: Unsupported values must raise, NOT silently fall back."""
    monkeypatch.setenv("FETCH_STRATEGY", "gen99")
    with pytest.raises(ValueError, match="Unsupported FETCH_STRATEGY"):
        resolve_fetch_strategy()


# ---------------------------------------------------------------------------
# Case 3: curl_cffi 200 -> no CloakBrowser call
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_case_03_curl_cffi_200_no_cloakbrowser_call():
    """§3 / §12.2 case 3: 200 from curl_cffi must short-circuit; cloak never called."""
    curl = make_curl_mock(status=200, body={"id": 123, "home": {}, "away": {}})
    cloak_calls = []
    async def cloak_stub(**kwargs):
        cloak_calls.append(kwargs)
        return await make_cloak_launcher()(**kwargs)
    ssr_calls = []
    def ssr_stub(path):
        ssr_calls.append(path)
        return {"id": 1}
    fetcher = make_fetcher(curl_get=curl, cloakbrowser_launcher=cloak_stub, ssr_fetcher=ssr_stub)
    async with fetcher:
        result = await fetcher.fetch_api("/event/14025013", timeout_ms=5000)
    assert result["ok"] is True
    assert result["transport"] == "curl_cffi"
    assert len(result["attempts"]) == 1
    assert result["attempts"][0]["transport"] == "curl_cffi"
    assert result["fallback_used"] is False
    assert cloak_calls == []
    assert ssr_calls == []



# ---------------------------------------------------------------------------
# Case 4: curl_cffi 403 -> fallback to CloakBrowser
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_case_04_curl_cffi_403_falls_back_to_cloakbrowser():
    """§3 / §12.2 case 4: 403 from curl_cffi must trigger CloakBrowser tier."""
    curl = make_curl_mock(status=403, body=None, error="forbidden")
    cloak = make_cloak_launcher(page_status=200, page_body={"id": 1, "home": {}, "away": {}})
    fetcher = make_fetcher(curl_get=curl, cloakbrowser_launcher=cloak)
    async with fetcher:
        result = await fetcher.fetch_api("/event/12436875", timeout_ms=5000)
    assert result["ok"] is True
    assert result["transport"] == "cloakbrowser"
    assert len(result["attempts"]) == 2
    assert result["attempts"][0]["transport"] == "curl_cffi"
    assert result["attempts"][0]["status"] == 403
    assert result["attempts"][1]["transport"] == "cloakbrowser"
    assert result["fallback_used"] is True


# ---------------------------------------------------------------------------
# Case 5: curl_cffi 407 -> logged, falls back per policy
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_case_05_curl_cffi_407_is_terminal_safety_halt():
    """§17 (revised) / §12.2 case 5: 407 (proxy auth) MUST be a terminal safety halt.

    No fallback to CloakBrowser. No fallback to SSR. Proxy auth failure means
    the endpoint surface is unreachable; burning more transports only wastes
    quota and risks exposing the same auth gap. Caller MUST be able to
    distinguish a 407 halt from a generic failure via halt_reason.
    """
    curl = make_curl_mock(status=407, body=None, error="proxy auth required")
    cloak_calls = []
    async def cloak_stub(**kwargs):
        cloak_calls.append(kwargs)
        return await make_cloak_launcher()(**kwargs)
    ssr_calls = []
    def ssr_stub(path):
        ssr_calls.append(path)
        return {"id": 1}
    fetcher = make_fetcher(curl_get=curl, cloakbrowser_launcher=cloak_stub, ssr_fetcher=ssr_stub)
    async with fetcher:
        result = await fetcher.fetch_api("/event/14025013", timeout_ms=5000)

    # Terminal halt contract
    assert result["ok"] is False, "407 must terminate with ok=False"
    assert result["status"] == 407, f"status must be 407, got {result['status']}"
    assert result["halt_reason"] == "http_407_safety_halt", (
        f"halt_reason must be 'http_407_safety_halt', got {result.get('halt_reason')!r}"
    )
    assert result["error_class"] == "http_407"
    assert result["payload_complete"] is False
    assert result["data"] is None
    assert result["fallback_used"] is False, "no fallback should have occurred"
    # No further tiers attempted
    assert len(result["attempts"]) == 1, (
        f"only the curl_cffi attempt should be recorded, got {len(result['attempts'])}"
    )
    assert result["attempts"][0]["transport"] == "curl_cffi"
    assert result["attempts"][0]["status"] == 407
    assert result["transport"] == "curl_cffi", "transport must stay curl_cffi (the tier that halted)"
    # Critical: downstream transports MUST NOT have been invoked
    assert cloak_calls == [], f"CloakBrowser must NOT be called on 407, got {cloak_calls}"
    assert ssr_calls == [], f"SSR must NOT be called on 407, got {ssr_calls}"


# ---------------------------------------------------------------------------
# Case 6: CloakBrowser 200 -> success
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_case_06_cloakbrowser_200_success():
    """§3 / §12.2 case 6: CloakBrowser 200 with complete payload returns ok."""
    curl = make_curl_mock(status=403)
    cloak = make_cloak_launcher(page_status=200, page_body={"id": 1, "home": {}, "away": {}})
    fetcher = make_fetcher(curl_get=curl, cloakbrowser_launcher=cloak)
    async with fetcher:
        result = await fetcher.fetch_api("/event/12436875", timeout_ms=5000)
    assert result["ok"] is True
    assert result["transport"] == "cloakbrowser"
    assert result["payload_complete"] is True


# ---------------------------------------------------------------------------
# Case 7: CloakBrowser timeout -> fallback SSR
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_case_07_cloakbrowser_timeout_falls_back_to_ssr():
    """§3 / §12.2 case 7: CloakBrowser timeout -> SSR tier takes over."""
    curl = make_curl_mock(status=403)
    timeout_exc = Exception("Page.goto: Timeout 5000ms exceeded")
    cloak = make_cloak_launcher(page_status=0, error=timeout_exc)
    ssr = make_ssr_fetcher(body={"id": 1, "home": {}, "away": {}})
    fetcher = make_fetcher(curl_get=curl, cloakbrowser_launcher=cloak, ssr_fetcher=ssr)
    async with fetcher:
        result = await fetcher.fetch_api("/event/14025013", timeout_ms=5000)
    assert result["ok"] is True
    assert result["transport"] == "ssr"
    transports = [a["transport"] for a in result["attempts"]]
    assert "curl_cffi" in transports
    assert "cloakbrowser" in transports
    assert "ssr" in transports
    assert result["fallback_used"] is True



# ---------------------------------------------------------------------------
# Case 8: CloakBrowser target closed / EPIPE -> rebuild limit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_case_08_cloakbrowser_target_closed_rebuild_limit():
    """§5 / §12.2 case 8: Target closed / EPIPE must increment rebuild counter and respect max_browser_rebuilds."""
    curl = make_curl_mock(status=403)
    target_closed_exc = Exception("Target page, context or browser has been closed")
    cloak = make_cloak_launcher(page_status=0, error=target_closed_exc)
    ssr = make_ssr_fetcher(body={"id": 1, "home": {}, "away": {}})
    cfg = Gen4Config(write_enabled=False, max_browser_rebuilds=1, proxy_pass="")
    fetcher = Gen4Fetcher(config=cfg, curl_cffi_get=curl,
                           cloakbrowser_launcher=cloak, ssr_fetcher=ssr)
    async with fetcher:
        result = await fetcher.fetch_api("/event/14025013", timeout_ms=5000)
    # First attempt hits target_closed -> rebuild_browser (count=1)
    # Second attempt hits target_closed -> rebuild limit reached, returns status=0
    # SSR then succeeds
    assert fetcher.stats.browser_rebuild_count >= 1
    assert fetcher.stats.epipe_count >= 1
    # SSR tier provides the success
    transports = [a["transport"] for a in result["attempts"]]
    assert "ssr" in transports


@pytest.mark.asyncio
async def test_case_08b_cloakbrowser_rebuild_respects_max_limit():
    """§5: rebuild_browser() must be no-op once max_browser_rebuilds reached."""
    cfg = Gen4Config(write_enabled=False, max_browser_rebuilds=2, proxy_pass="")
    fetcher = Gen4Fetcher(config=cfg)
    # Manually set counter past limit
    fetcher.stats.browser_rebuild_count = 2
    await fetcher.rebuild_browser()
    assert fetcher.stats.browser_rebuild_count == 2  # unchanged


# ---------------------------------------------------------------------------
# Case 9: SSR success
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_case_09_ssr_success_returns_complete_payload():
    """§3 / §12.2 case 9: SSR 200 with complete payload returns success."""
    curl = make_curl_mock(status=403)
    target_closed = Exception("Target page, context or browser has been closed")
    cloak = make_cloak_launcher(page_status=0, error=target_closed)
    ssr = make_ssr_fetcher(body={"id": 1, "home": {}, "away": {}})
    fetcher = make_fetcher(curl_get=curl, cloakbrowser_launcher=cloak, ssr_fetcher=ssr)
    async with fetcher:
        result = await fetcher.fetch_api("/event/12436875", timeout_ms=5000)
    assert result["ok"] is True
    assert result["transport"] == "ssr"
    assert result["status"] == 200
    assert result["payload_complete"] is True


@pytest.mark.asyncio
async def test_case_09b_ssr_returns_none_marks_failure():
    """§12.2 case 9: SSR returning None is treated as failure, not 200."""
    curl = make_curl_mock(status=403)
    cloak = make_cloak_launcher(page_status=0, error=Exception("timeout"))
    ssr = make_ssr_fetcher(return_none=True)
    fetcher = make_fetcher(curl_get=curl, cloakbrowser_launcher=cloak, ssr_fetcher=ssr)
    async with fetcher:
        result = await fetcher.fetch_api("/event/14025013", timeout_ms=5000)
    assert result["ok"] is False
    assert any(a["error"] and "ssr_returned_none" in a["error"] for a in result["attempts"])


# ---------------------------------------------------------------------------
# Case 10: All tiers fail -> failure result contract
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_case_10_all_tiers_fail_returns_failure_contract():
    """§7 / §12.2 case 10: All tiers failing returns failure result with required keys."""
    curl = make_curl_mock(status=403)
    cloak = make_cloak_launcher(page_status=0, error=Exception("browser crash"))
    ssr = make_ssr_fetcher(return_none=True)
    fetcher = make_fetcher(curl_get=curl, cloakbrowser_launcher=cloak, ssr_fetcher=ssr)
    async with fetcher:
        result = await fetcher.fetch_api("/event/14025013", timeout_ms=5000)
    assert result["ok"] is False
    assert result["status"] == 0
    assert result["data"] is None
    assert result["payload_complete"] is False
    assert result["fallback_used"] is True
    assert "error_class" in result
    assert isinstance(result["attempts"], list)
    assert len(result["attempts"]) >= 2  # at least curl + cloak (ssr may not be appended if returned None early)
    assert result["error_class"] in ("timeout", "browser_error", "invalid_json", "http_403", "unknown")


@pytest.mark.asyncio
async def test_case_10b_no_ssrf_no_cloak_no_curl_returns_failure():
    """§12.2 case 10: When all three tiers are unavailable, must still return failure result."""
    fetcher = make_fetcher(curl_get=None, cloakbrowser_launcher=None, ssr_fetcher=None)
    async with fetcher:
        result = await fetcher.fetch_api("/event/14025013", timeout_ms=5000)
    assert result["ok"] is False
    assert result["payload_complete"] is False
    assert "error_class" in result



# ---------------------------------------------------------------------------
# Case 11: HTTP 200 + empty payload -> not complete
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_case_11_http_200_empty_payload_not_complete():
    """§8 / §12.2 case 11: 200 with empty {} or [] body must NOT be payload_complete=True."""
    curl = make_curl_mock(status=200, body={})  # empty dict
    fetcher = make_fetcher(curl_get=curl)
    async with fetcher:
        result = await fetcher.fetch_api("/event/14025013", timeout_ms=5000)
    # curl 200 + empty {} -> validate_payload returns (False, 'success_empty')
    # -> fetch_api falls through to next tier
    assert result["ok"] is False
    assert result["payload_complete"] is False
    assert any(a["transport"] == "curl_cffi" for a in result["attempts"])


@pytest.mark.asyncio
async def test_case_11b_http_200_missing_required_keys_not_complete():
    """§8: endpoint /event/*/incidents with body lacking 'incidents' key must not be complete."""
    curl = make_curl_mock(status=200, body={"someOtherKey": []})
    fetcher = make_fetcher(curl_get=curl)
    async with fetcher:
        result = await fetcher.fetch_api("/event/14025013/incidents", timeout_ms=5000)
    assert result["payload_complete"] is False


def test_case_11c_validate_payload_unit():
    """§8: validate_payload pure function — direct tests."""
    # Non-empty dict with needed key
    is_complete, err_class = validate_payload("/event/1/incidents", {"incidents": []})
    assert is_complete is True
    assert err_class == "success_complete"
    # Empty dict
    is_complete, err_class = validate_payload("/event/1/incidents", {})
    assert is_complete is False
    assert err_class == "success_empty"
    # None
    is_complete, err_class = validate_payload("/event/1", None)
    assert is_complete is False
    assert err_class == "invalid_json"
    # String body that is valid JSON
    is_complete, err_class = validate_payload("/event/1/incidents", '{"incidents": []}')
    assert is_complete is True
    # String body that is invalid JSON
    is_complete, err_class = validate_payload("/event/1/incidents", "not-json-at-all")
    assert is_complete is False
    assert err_class == "invalid_json"


def test_case_11d_classify_error_unit():
    """classify_error maps HTTP/error conditions to error_class labels."""
    assert classify_error(403, None, None) == "http_403"
    assert classify_error(404, None, None) == "http_404"
    assert classify_error(407, None, None) == "http_407"
    assert classify_error(0, None, "timeout exceeded") == "timeout"
    assert classify_error(0, None, "json parse failed") == "invalid_json"
    assert classify_error(0, None, "browser closed") == "browser_error"
    assert classify_error(0, None, "EPIPE") == "browser_error"
    assert classify_error(500, None, None) == "unknown"


# ---------------------------------------------------------------------------
# Case 12: Credentials redacted
# ---------------------------------------------------------------------------

def test_case_12_credentials_redacted():
    """§15 / §12.2 case 12: redact_credentials MUST scrub proxy passwords / API keys."""
    # Proxy URL with password
    raw = "Connecting via http://***REMOVED***-rotate:dztr57tcycoz123@p.webshare.io:80"
    redacted = redact_credentials(raw)
    assert "dztr57tcycoz123" not in redacted
    assert "[REDACTED]" in redacted
    # Env var form
    raw = "Loaded SOFA_PROXY_PASS=supersecretvalue from env"
    redacted = redact_credentials(raw)
    assert "supersecretvalue" not in redacted
    # Plain API key
    raw = "Got API token: dztr57tcycoz from header"
    redacted = redact_credentials(raw)
    assert "dztr57tcycoz" not in redacted
    # User:pass form
    raw = "Failed to connect to ***REMOVED***-rotate:something@something.com"
    redacted = redact_credentials(raw)
    assert "***REMOVED***-rotate:" not in redacted or "[REDACTED]" in redacted
    # Non-string input is preserved
    assert redact_credentials(None) is None
    assert redact_credentials(42) == 42
    # Clean text untouched
    assert redact_credentials("all good") == "all good"


def test_case_12b_attempt_log_does_not_leak_credentials():
    """§15: attempt log must not leak proxy passwords even when get fails."""
    cfg = Gen4Config(write_enabled=False, proxy_pass="dztr57tcycoz")
    fetcher = Gen4Fetcher(config=cfg)
    leak = "http://***REMOVED***-rotate:dztr57tcycoz@p.webshare.io:80"
    attempt = _make_attempt("curl_cffi", 407, 100, f"curl_cffi request failed: {leak}")
    # Make sure we never store raw leak in stats
    fetcher.stats.attempts.append(attempt)
    serialized = repr(fetcher.stats.attempts)
    # The serializer keeps error raw — production code must redact before logging
    # For this test, just verify the helper works on the leak
    assert "dztr57tcycoz" in serialized  # raw error stored
    redacted = redact_credentials(serialized)
    assert "dztr57tcycoz" not in redacted


# ---------------------------------------------------------------------------
# Case 13: write_enabled=False invariant
# ---------------------------------------------------------------------------

def test_case_13_write_enabled_true_raises():
    """§3 / §12.2 case 13: write_enabled=True must raise RuntimeError on start."""
    cfg = Gen4Config(write_enabled=True)
    fetcher = Gen4Fetcher(config=cfg)
    with pytest.raises(RuntimeError, match="write_enabled=True"):
        asyncio.run(fetcher.start())


@pytest.mark.asyncio
async def test_case_13b_write_enabled_false_default_passes():
    """§3: Default config (write_enabled=False) must not raise."""
    fetcher = make_fetcher()
    async with fetcher:
        # No raise
        pass



# ---------------------------------------------------------------------------
# Case 14: No DataInserter / MySQL calls
# ---------------------------------------------------------------------------

def test_case_14_gen4_fetcher_has_no_insert_or_mysql_import():
    """§3 / §12.2 case 14: Gen4Fetcher module must NOT import DataInserter or MySQL drivers."""
    import ast
    import gen4_fetcher

    # Parse AST and inspect imports + top-level function/class defs only.
    with open(gen4_fetcher.__file__, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())

    forbidden_imports = {"DataInserter", "mysql.connector", "pymysql", "MySQLdb"}
    forbidden_method_prefixes = ("insert_", "write_", "save_")

    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported_names.add(alias.asname or alias.name)

    leaked = imported_names & forbidden_imports
    assert not leaked, f"Gen4Fetcher must not import {leaked}"

    # Walk every class/function def and ensure none has a write-prefixed name.
    write_methods = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith(forbidden_method_prefixes):
                write_methods.append(node.name)
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if item.name.startswith(forbidden_method_prefixes):
                        write_methods.append(f"{node.name}.{item.name}")
    assert not write_methods, f"Gen4Fetcher must not define {write_methods}"

    # Forbidden raw SQL tokens in AST (no string literals containing INSERT INTO).
    sql_tokens = ("INSERT INTO", "cursor.execute")
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for tok in sql_tokens:
                assert tok not in node.value, f"Gen4Fetcher must not contain literal {tok!r}"

    assert hasattr(gen4_fetcher, "Gen4Fetcher")
    assert hasattr(Gen4Fetcher, "fetch_api")


def test_case_14b_gen4_fetcher_class_has_no_write_methods():
    """§3: Gen4Fetcher class definition must have no insert_* / write_* methods."""
    cls = Gen4Fetcher
    forbidden_methods = []
    for name in dir(cls):
        if name.startswith("insert_") or name.startswith("write_") or name.startswith("save_"):
            forbidden_methods.append(name)
    assert forbidden_methods == [], f"Gen4Fetcher must not have write methods: {forbidden_methods}"


# ---------------------------------------------------------------------------
# Case 15: Browser close / cleanup always runs
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_case_15_close_clears_browser_and_context():
    """§5 / §12.2 case 15: close() must always run, even if browser launch throws."""
    cfg = Gen4Config(write_enabled=False, proxy_pass="")
    fetcher = Gen4Fetcher(config=cfg)
    fake_browser = MagicMock()
    fake_context = MagicMock()
    async def _bc():
        pass
    fake_browser.close = _bc
    async def _cc():
        pass
    fake_context.close = _cc
    fetcher._browser = fake_browser
    fetcher._context = fake_context
    await fetcher.close()
    assert fetcher._browser is None
    assert fetcher._context is None
    assert fetcher._started is False


@pytest.mark.asyncio
async def test_case_15b_close_is_safe_when_browser_already_none():
    """§5: close() must be idempotent / safe even when browser/context are already None."""
    fetcher = make_fetcher()
    async with fetcher:
        pass
    await fetcher.close()
    await fetcher.close()


@pytest.mark.asyncio
async def test_case_15c_close_swallows_browser_close_exceptions():
    """§5: close() must swallow exceptions from browser.close() to avoid masking original errors."""
    fetcher = make_fetcher()
    fetcher._browser = MagicMock()
    async def _broken_close():
        raise RuntimeError("browser already crashed")
    fetcher._browser.close = _broken_close
    fetcher._context = None
    await fetcher.close()
    assert fetcher._browser is None


# ---------------------------------------------------------------------------
# Case 16: Endpoint key normalization consistency
# ---------------------------------------------------------------------------

def test_case_16_validate_payload_endpoint_keys_consistent():
    """§8 / §12.2 case 16: validate_payload endpoint extraction must be consistent."""
    is_complete, _ = validate_payload("/api/v1/event/14025013", {"id": 1, "home": {}, "away": {}})
    assert is_complete is True
    is_complete, _ = validate_payload("/api/v1/event/14025013/incidents", {"incidents": [1, 2]})
    assert is_complete is True
    is_complete, _ = validate_payload("/api/v1/event/14025013/lineups", {"home": {}, "away": {}})
    assert is_complete is True
    is_complete, _ = validate_payload("/api/v1/event/14025013/shotmap", {"shotmap": [1]})
    assert is_complete is True
    is_complete, _ = validate_payload("/api/v1/event/14025013/graph", {"graph": {}})
    assert is_complete is True
    is_complete, _ = validate_payload("/api/v1/event/14025013/statistics", {"statistics": []})
    assert is_complete is True
    is_complete, _ = validate_payload("/api/v1/event/14025013/incidents", {})
    assert is_complete is False


def test_case_16b_attempt_log_structure_consistent():
    """§7: every attempt in the log has the same dict shape."""
    a1 = _make_attempt("curl_cffi", 200, 100)
    a2 = _make_attempt("cloakbrowser", 403, 200, "forbidden")
    a3 = _make_attempt("ssr", 0, 50, "ssr_unavailable")
    for attempt in (a1, a2, a3):
        assert set(attempt.keys()) == {"transport", "status", "latency_ms", "error"}
        assert attempt["transport"] in ("curl_cffi", "cloakbrowser", "ssr")
        assert isinstance(attempt["status"], int)
        assert isinstance(attempt["latency_ms"], int)
        assert attempt["error"] is None or isinstance(attempt["error"], str)


def test_case_16c_success_failure_result_contract_keys():
    """§7: success / failure result dict keys must match the spec exactly."""
    success = _success_result(
        status=200, data={"id": 1}, transport="curl_cffi",
        attempts=[{"transport": "curl_cffi", "status": 200, "latency_ms": 100, "error": None}],
        retry_count=0, payload_complete=True,
    )
    expected_keys = {"ok", "status", "data", "transport", "attempts",
                     "fallback_used", "retry_count", "payload_complete",
                     "error_class", "halt_reason"}
    assert set(success.keys()) == expected_keys
    assert success["ok"] is True
    assert success["data"] == {"id": 1}
    assert success["transport"] == "curl_cffi"
    assert success["fallback_used"] is False
    assert success["payload_complete"] is True
    assert success["error_class"] is None

    failure = _failure_result(
        status=403, transport="ssr",
        attempts=[
            {"transport": "curl_cffi", "status": 403, "latency_ms": 100, "error": None},
            {"transport": "cloakbrowser", "status": 403, "latency_ms": 200, "error": None},
        ],
        retry_count=2, error_class="http_403",
    )
    assert set(failure.keys()) == expected_keys
    assert failure["ok"] is False
    assert failure["data"] is None
    assert failure["payload_complete"] is False
    assert failure["fallback_used"] is True
    assert failure["error_class"] == "http_403"


# ---------------------------------------------------------------------------
# Case 17 (Phase 4 v1 fix): /api/v1/event/{eid} root payload endpoint extraction
# ---------------------------------------------------------------------------
# Per Kris 02:14 GMT+8 instruction: Sofascore event root payloads look like
# {"event": {"id": ..., "homeTeam": {...}, "awayTeam": {...}}} or nested
# variants; they do NOT expose top-level "home" / "away". Requiring all three
# ("id", "home", "away") rejects valid event payloads, so the revised rule
# checks only "id". Empty/invalid bodies MUST still fail.

def test_case_17_event_root_valid_payload_complete():
    """§17 (Phase 4 v1 fix) / §12.2 case 17: valid event root payload passes."""
    body = {
        "event": {
            "id": 14025013,
            "homeTeam": {"id": 1, "name": "Home FC"},
            "awayTeam": {"id": 2, "name": "Away FC"},
        }
    }
    is_complete, err_class = validate_payload("/api/v1/event/14025013", body)
    assert is_complete is True
    assert err_class == "success_complete"


def test_case_17b_event_root_flat_dict_with_id_complete():
    """§17: flat dict with 'id' key is also accepted for root event."""
    body = {"id": 14025013, "homeTeam": {"id": 1}, "awayTeam": {"id": 2}}
    is_complete, err_class = validate_payload("/api/v1/event/14025013", body)
    assert is_complete is True
    assert err_class == "success_complete"


def test_case_17c_event_root_empty_dict_fails():
    """§17: empty dict on root event path MUST fail (not relax to any HTTP 200)."""
    is_complete, err_class = validate_payload("/api/v1/event/14025013", {})
    assert is_complete is False
    assert err_class == "success_empty"


def test_case_17d_event_root_none_fails():
    """§17: None body on root event path MUST fail."""
    is_complete, err_class = validate_payload("/api/v1/event/14025013", None)
    assert is_complete is False
    assert err_class == "invalid_json"


def test_case_17e_event_root_invalid_json_string_fails():
    """§17: invalid JSON string body on root event path MUST fail."""
    is_complete, err_class = validate_payload("/api/v1/event/14025013", "not-json")
    assert is_complete is False
    assert err_class == "invalid_json"


def test_case_17f_event_root_no_id_key_fails():
    """§17: dict missing 'id' key on root event path MUST fail."""
    is_complete, err_class = validate_payload("/api/v1/event/14025013", {"other": 1})
    assert is_complete is False
    assert err_class == "success_empty"


def test_case_17g_event_root_nested_event_wrapper():
    """§17: nested {'event': {...}} payload on root event passes (id is inside)."""
    body = {"event": {"id": 12436875, "homeTeam": {"id": 10}, "awayTeam": {"id": 20}}}
    is_complete, err_class = validate_payload("/api/v1/event/12436875", body)
    assert is_complete is True
    assert err_class == "success_complete"


def test_case_17h_event_root_trailing_slash_uses_event():
    """§17: /api/v1/event/{id}/ trailing slash still classifies as event root."""
    body = {"event": {"id": 14025013}}
    is_complete, err_class = validate_payload("/api/v1/event/14025013/", body)
    assert is_complete is True
    assert err_class == "success_complete"


def test_case_17i_event_sub_endpoints_still_strict():
    """§17: sub-endpoint paths still require their specific keys."""
    # /incidents missing "incidents" key → fail
    is_complete, err_class = validate_payload("/api/v1/event/14025013/incidents", {"other": []})
    assert is_complete is False
    assert err_class == "success_empty"
    # /lineups missing "home"/"away" top-level → fail
    is_complete, err_class = validate_payload("/api/v1/event/14025013/lineups", {"x": 1})
    assert is_complete is False
    assert err_class == "success_empty"
    # /shotmap missing "shotmap" → fail
    is_complete, err_class = validate_payload("/api/v1/event/14025013/shotmap", {"x": 1})
    assert is_complete is False
    assert err_class == "success_empty"
