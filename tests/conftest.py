#!/usr/bin/env python3
"""conftest.py — shared pytest fixtures for the SSR test suite.

Phase 4 additions: cache reset fixture so tests that share the
process-level `_SSR_CACHE` and `_HELPER_MISSING_LOGGED` global state do
not leak between tests.

Offline only — no network, no playwright, no MySQL.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=False)
def reset_ssr_state():
    """Reset gen4_ssr process-level state before each test that requests it.

    Clears:
      - _SSR_CACHE (per-event_id Playwright load cache)
      - _HELPER_MISSING_LOGGED (one-shot stderr log flag)

    Use this fixture on tests that depend on a clean cache or a fresh
    helper-missing signal. Without it, tests can leak state to one another
    because both globals are module-level dicts / booleans.

    Example:
        def test_foo(reset_ssr_state):
            from gen4_ssr import _SSR_CACHE
            assert _SSR_CACHE == {}
    """
    import gen4_ssr

    gen4_ssr._SSR_CACHE.clear()
    gen4_ssr._HELPER_MISSING_LOGGED = False
    yield
    # Cleanup after the test too — defense in depth.
    gen4_ssr._SSR_CACHE.clear()
    gen4_ssr._HELPER_MISSING_LOGGED = False


@pytest.fixture(autouse=False)
def reset_gen4_fetcher_symbols():
    """Restore `_map_ssr_to_api_format` on gen4_fetcher after a test that
    deleted or monkeypatched it.

    Several tests use `monkeypatch.delattr(g4f, "_map_ssr_to_api_format")`
    to simulate the helper being missing. If the monkeypatch teardown
    somehow fails to restore (e.g. test raised before teardown), other
    tests would see the missing-helper path. This fixture guards against
    that by re-binding the original at fixture teardown.

    Note: monkeypatch already restores the attribute on teardown for any
    test that uses it. This fixture is a defense-in-depth backstop.
    """
    import gen4_fetcher
    from gen4_fetcher import _map_ssr_to_api_format as original_helper

    yield
    # Restore at fixture teardown regardless of test outcome.
    try:
        gen4_fetcher._map_ssr_to_api_format = original_helper
    except (AttributeError, ImportError):
        pass
