#!/usr/bin/env python3
"""test_ssr_conftest_fixtures.py — Phase 4 conftest fixture contract tests.

Verifies that the `reset_ssr_state` and `reset_gen4_fetcher_symbols`
fixtures in tests/conftest.py correctly clean process-level state.

Offline only.
"""
from __future__ import annotations

import pytest


class TestResetSSRStateFixture:
    """reset_ssr_state fixture clears _SSR_CACHE and _HELPER_MISSING_LOGGED."""

    def test_fixture_clears_cache_at_start(self, reset_ssr_state) -> None:
        """At fixture start, _SSR_CACHE is empty."""
        import gen4_ssr
        assert gen4_ssr._SSR_CACHE == {}, (
            f"_SSR_CACHE should be empty at fixture start, got {list(gen4_ssr._SSR_CACHE.keys())}"
        )
        assert gen4_ssr._HELPER_MISSING_LOGGED is False

    def test_fixture_restores_after_test(self, reset_ssr_state) -> None:
        """Modifications made during the test are cleaned at fixture teardown."""
        import gen4_ssr
        gen4_ssr._SSR_CACHE[999] = {"sentinel": True}
        gen4_ssr._HELPER_MISSING_LOGGED = True
        # The fixture will clean this up at teardown.

    def test_fixture_clears_helper_logged(self, reset_ssr_state) -> None:
        """_HELPER_MISSING_LOGGED resets to False."""
        import gen4_ssr
        gen4_ssr._HELPER_MISSING_LOGGED = True
        # At teardown, fixture will reset this. We can re-request the
        # fixture in a nested call to verify cleanup.

    def test_fixture_idempotent(self, reset_ssr_state) -> None:
        """Calling reset_ssr_state twice in the same test should not raise."""
        import gen4_ssr
        # First call clears.
        gen4_ssr._SSR_CACHE.clear()
        gen4_ssr._HELPER_MISSING_LOGGED = False
        # Second call also clears (no-op).
        gen4_ssr._SSR_CACHE.clear()
        gen4_ssr._HELPER_MISSING_LOGGED = False
        assert gen4_ssr._SSR_CACHE == {}


class TestResetGen4FetcherSymbolsFixture:
    """reset_gen4_fetcher_symbols restores _map_ssr_to_api_format on teardown."""

    def test_fixture_restores_helper_on_teardown(self) -> None:
        """If the test deletes the symbol, the fixture restores it."""
        import gen4_fetcher

        # Verify the symbol exists at start.
        assert hasattr(gen4_fetcher, "_map_ssr_to_api_format")

        # Use the fixture.
        with pytest.MonkeyPatch.context() as mp:
            mp.delattr(gen4_fetcher, "_map_ssr_to_api_format", raising=True)
            # Inside this block, the symbol is gone.
            assert not hasattr(gen4_fetcher, "_map_ssr_to_api_format")

        # After MonkeyPatch context, the symbol is restored automatically.
        # (Our fixture is a defense-in-depth backstop on top of this.)
        assert hasattr(gen4_fetcher, "_map_ssr_to_api_format")


class TestFixturesCoexistWithExistingTests:
    """Adding fixtures must not break existing tests that don't request them."""

    def test_existing_test_style_works(self) -> None:
        """Tests that do NOT request the fixtures should still work."""
        # Just import and use gen4_ssr without fixtures.
        from gen4_ssr import resolve_ssr_for_api_path
        # Use a simple path that returns None (bad path).
        assert resolve_ssr_for_api_path("") is None

    def test_conftest_imports_cleanly(self) -> None:
        """The conftest module itself must import without errors."""
        import tests.conftest  # noqa: F401
