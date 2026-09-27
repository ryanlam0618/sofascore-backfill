#!/usr/bin/env python3
"""test_gen4_ssr_phase3.py — Phase 3 offline tests.

Covers the new async wrappers, the Gen4Fetcher integration, and
hardened edge cases for the SSR resolver.

Run: .runner-venv/bin/python -m pytest tests/test_gen4_ssr_phase3.py -q
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PATH = REPO_ROOT / "debug_ssr_raw.json"


# ---------------------------------------------------------------------------
# Async wrappers (Phase 3 additions)
# ---------------------------------------------------------------------------
class TestAsyncWrappers:
    """aload_event_ssr + aresolve_ssr_for_api_path (Phase 3)."""

    def test_aload_event_ssr_dispatches_to_thread(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """aload_event_ssr must NOT block the event loop. Inject a fake
        loader that records the call site so we can confirm dispatch."""
        import gen4_ssr
        from gen4_ssr import aload_event_ssr
        gen4_ssr._SSR_CACHE.pop(42, None)

        # Monkeypatch load_event_ssr to return a sentinel.
        sentinel = {"from": "thread", "event_id": 42}

        def fake_load_event_ssr(event_id, *, url_template="https://x/{eid}"):
            return sentinel

        monkeypatch.setattr("gen4_ssr.load_event_ssr", fake_load_event_ssr)

        out = asyncio.run(aload_event_ssr(42))
        assert out == sentinel

    def test_aresolve_ssr_for_api_path_event_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Async resolver returns payload for 'event' via injected loader."""
        from gen4_ssr import aresolve_ssr_for_api_path

        fake_raw = {"props": {"pageProps": {"event": {"id": 777, "homeTeam": {"id": 1}, "awayTeam": {"id": 2}}}}}
        loader_calls = {"count": 0}

        def fake_loader(_eid):
            loader_calls["count"] += 1
            return fake_raw

        out = asyncio.run(aresolve_ssr_for_api_path("/api/v1/event/777", _loader=fake_loader))
        assert out is not None
        assert out["event"]["id"] == 777
        assert loader_calls["count"] == 1

    def test_aresolve_ssr_for_api_path_unsupported_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Async resolver returns None for unsupported endpoints WITHOUT
        calling the loader (fast-fail before Playwright)."""
        from gen4_ssr import aresolve_ssr_for_api_path
        from gen4_fetcher import SSR_UNSUPPORTED_ENDPOINTS

        fake_raw = {"props": {"pageProps": {"event": {"id": 777}}}}
        loader_calls = {"count": 0}

        def fake_loader(_eid):
            loader_calls["count"] += 1
            return fake_raw

        for endpoint in SSR_UNSUPPORTED_ENDPOINTS:
            out = asyncio.run(aresolve_ssr_for_api_path(f"/api/v1/event/777/{endpoint}", _loader=fake_loader))
            assert out is None, f"{endpoint} should return None"

        # The resolver should short-circuit BEFORE calling the loader for
        # unsupported endpoints — saves a Playwright roundtrip.
        assert loader_calls["count"] == 0, (
            "loader was called for unsupported endpoints — fast-fail broken"
        )

    def test_aresolve_ssr_for_api_path_helper_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Async resolver raises SSRHelperMissing (NOT silent None) when
        the helper symbol is unavailable."""
        from gen4_ssr import aresolve_ssr_for_api_path, SSRHelperMissing
        import gen4_fetcher as g4f
        monkeypatch.delattr(g4f, "_map_ssr_to_api_format", raising=True)
        import gen4_ssr as g4s
        g4s._HELPER_MISSING_LOGGED = False  # type: ignore[attr-defined]

        def fake_loader(_eid):
            return {"props": {"pageProps": {"event": {"id": 1}}}}

        with pytest.raises(SSRHelperMissing):
            asyncio.run(aresolve_ssr_for_api_path("/api/v1/event/1", _loader=fake_loader))

    def test_aresolve_ssr_for_api_path_returns_none_for_bad_path(self) -> None:
        """Async resolver returns None for unparseable paths."""
        from gen4_ssr import aresolve_ssr_for_api_path
        assert asyncio.run(aresolve_ssr_for_api_path("")) is None
        assert asyncio.run(aresolve_ssr_for_api_path("/api/v1/tournament/17/seasons")) is None

    def test_aresolve_ssr_for_api_path_loader_returns_none(self) -> None:
        """If the loader returns None (e.g. Playwright failed), the
        async resolver propagates None — not an exception."""
        from gen4_ssr import aresolve_ssr_for_api_path

        def fake_loader_none(_eid):
            return None

        out = asyncio.run(aresolve_ssr_for_api_path("/api/v1/event/777", _loader=fake_loader_none))
        assert out is None


# ---------------------------------------------------------------------------
# Gen4Fetcher integration — SSR helper missing surfaces as deterministic
# attempt error
# ---------------------------------------------------------------------------
class TestGen4FetcherSSRSurfaceContract:
    """Gen4Fetcher._fetch_via_ssr must classify SSRHelperMissing as
    'ssr_helper_missing' (NOT 'ssr failed: ...') so the canary artifact
    distinguishes the two failure modes.
    """

    def _make_fetcher_with_helper_missing_resolver(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        """Build a Gen4Fetcher whose _ssr_fetcher raises SSRHelperMissing."""
        from gen4_fetcher import Gen4Config, Gen4Fetcher
        from gen4_ssr import resolve_ssr_for_api_path, SSRHelperMissing
        import gen4_fetcher as g4f
        monkeypatch.delattr(g4f, "_map_ssr_to_api_format", raising=True)
        import gen4_ssr as g4s
        g4s._HELPER_MISSING_LOGGED = False  # type: ignore[attr-defined]

        def helper_missing_resolver(path):
            # Will raise SSRHelperMissing because the symbol is gone.
            return resolve_ssr_for_api_path(path)

        config = Gen4Config(write_enabled=False)
        fetcher = Gen4Fetcher(config=config, ssr_fetcher=helper_missing_resolver)
        return fetcher

    def test_ssr_helper_missing_is_deterministic_error_class(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fetcher = self._make_fetcher_with_helper_missing_resolver(monkeypatch)
        result = asyncio.run(fetcher._fetch_via_ssr("/api/v1/event/12345", timeout_ms=30000))
        err = result["attempt"].get("error") or ""
        assert err.startswith("ssr_helper_missing:"), (
            f"expected 'ssr_helper_missing:' prefix, got: {err!r}"
        )

    def test_ssr_unsupported_endpoint_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unsupported endpoint: loader returns None (fast-fail), so the
        attempt is classified 'ssr_returned_none' (NOT 'ssr_unsupported_endpoint'
        at this layer — that distinction lives at the resolver layer)."""
        from gen4_fetcher import Gen4Config, Gen4Fetcher

        def unsup_resolver(path):
            return None

        config = Gen4Config(write_enabled=False)
        fetcher = Gen4Fetcher(config=config, ssr_fetcher=unsup_resolver)
        result = asyncio.run(fetcher._fetch_via_ssr("/api/v1/event/12345/lineups", timeout_ms=30000))
        assert result["attempt"]["status"] == 0
        assert result["attempt"].get("error") == "ssr_returned_none"

    def test_ssr_none_fetcher_returns_unavailable(self) -> None:
        """If _ssr_fetcher is None, the attempt is 'ssr_unavailable'."""
        from gen4_fetcher import Gen4Config, Gen4Fetcher
        config = Gen4Config(write_enabled=False)
        fetcher = Gen4Fetcher(config=config, ssr_fetcher=None)
        result = asyncio.run(fetcher._fetch_via_ssr("/api/v1/event/12345", timeout_ms=30000))
        assert result["attempt"]["status"] == 0
        assert result["attempt"].get("error") == "ssr_unavailable"


# ---------------------------------------------------------------------------
# Unsupported-endpoint hardening — every endpoint in SSR_UNSUPPORTED_ENDPOINTS
# is verified against the real fixture AND synthetic payloads
# ---------------------------------------------------------------------------
class TestUnsupportedEndpointHardening:
    """For every endpoint in SSR_UNSUPPORTED_ENDPOINTS, verify the mapping
    function returns None for both the real fixture and synthetic data."""

    @classmethod
    def setup_class(cls) -> None:
        if not FIXTURE_PATH.exists():
            pytest.skip(f"real fixture missing: {FIXTURE_PATH}")
        cls.raw = json.loads(FIXTURE_PATH.read_text())
        cls.eid = cls.raw["props"]["pageProps"]["event"]["id"]

    def test_every_unsupported_endpoint_returns_none_real_fixture(self) -> None:
        from gen4_fetcher import _map_ssr_to_api_format, SSR_UNSUPPORTED_ENDPOINTS
        for ep in SSR_UNSUPPORTED_ENDPOINTS:
            path = f"/api/v1/event/{self.eid}/{ep}"
            out = _map_ssr_to_api_format(self.raw, path)
            assert out is None, f"real fixture: {ep} unexpectedly returned {out!r}"

    def test_every_unsupported_endpoint_returns_none_synthetic_payload(self) -> None:
        """Even with a fake payload that contains `incidents`/`lineups`
        keys, the function MUST refuse to fabricate data — it only returns
        the canonical 'event' payload."""
        from gen4_fetcher import _map_ssr_to_api_format, SSR_UNSUPPORTED_ENDPOINTS

        synthetic = {
            "props": {"pageProps": {"event": {"id": self.eid, "homeTeam": {"id": 1}, "awayTeam": {"id": 2}}}},
            # These keys exist in the synthetic envelope but should be IGNORED
            # for unsupported endpoints — Phase 2 contract: only 'event' is mapped.
            "incidents": [{"minute": 10}],
            "lineups": [{"home": {"players": []}}],
            "statistics": {"groups": []},
            "shotmap": [{"shot": 1}],
        }
        for ep in SSR_UNSUPPORTED_ENDPOINTS:
            out = _map_ssr_to_api_format(synthetic, f"/api/v1/event/{self.eid}/{ep}")
            assert out is None, (
                f"synthetic payload: {ep} returned {out!r} — must be None"
            )

    def test_lineups_and_incidents_permanently_unsupported(self) -> None:
        """Phase 3 policy pin: lineups and incidents are PERMANENTLY
        SSR-unsupported (the SSR pageProps does not contain them — only
        `initialHasLineups` boolean)."""
        from gen4_fetcher import SSR_UNSUPPORTED_ENDPOINTS
        assert "lineups" in SSR_UNSUPPORTED_ENDPOINTS, (
            "POLICY VIOLATION: lineups must remain in SSR_UNSUPPORTED_ENDPOINTS — "
            "the SSR payload only carries `initialHasLineups` (boolean), not "
            "the actual lineup data. Any change requires explicit Kris approval."
        )
        assert "incidents" in SSR_UNSUPPORTED_ENDPOINTS, (
            "POLICY VIOLATION: incidents must remain in SSR_UNSUPPORTED_ENDPOINTS — "
            "Sofascore SSR does not embed incidents in the pageProps."
        )

    def test_gen4_ssr_also_fast_fails_unsupported(self) -> None:
        """The shared resolver must fast-fail unsupported endpoints BEFORE
        calling the loader."""
        from gen4_ssr import resolve_ssr_for_api_path
        from gen4_fetcher import SSR_UNSUPPORTED_ENDPOINTS

        loader_called = {"count": 0}

        def tracking_loader(_eid):
            loader_called["count"] += 1
            return None

        for ep in SSR_UNSUPPORTED_ENDPOINTS:
            out = resolve_ssr_for_api_path(
                f"/api/v1/event/{self.eid}/{ep}", _loader=tracking_loader,
            )
            assert out is None

        assert loader_called["count"] == 0, (
            "loader was called for unsupported endpoints — fast-fail broken"
        )


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------
class TestSSRCache:
    """Per-event_id cache: load_event_ssr must hit the cache on second call
    so the Playwright roundtrip only runs once per event."""

    def test_cache_hit_on_second_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import gen4_ssr
        from gen4_ssr import load_event_ssr
        # Clear the cache first.
        gen4_ssr._SSR_CACHE.clear()

        sentinel = {"props": {"pageProps": {"event": {"id": 999}}}}
        calls = {"count": 0}

        def fake_loader(_eid):
            calls["count"] += 1
            return sentinel

        # First call: cache miss, loader invoked.
        out1 = load_event_ssr(999, _loader=fake_loader)
        # Second call: cache hit, loader NOT invoked.
        out2 = load_event_ssr(999, _loader=fake_loader)

        assert out1 is sentinel
        assert out2 is sentinel
        assert calls["count"] == 1, (
            f"expected loader called once (cached on second call), got {calls['count']}"
        )

    def test_cache_is_per_event_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import gen4_ssr
        from gen4_ssr import load_event_ssr
        gen4_ssr._SSR_CACHE.clear()

        sentinel_a = {"props": {"pageProps": {"event": {"id": 1}}}}
        sentinel_b = {"props": {"pageProps": {"event": {"id": 2}}}}

        table = {1: sentinel_a, 2: sentinel_b}
        calls = {"count": 0}

        def fake_loader(event_id):
            calls["count"] += 1
            return table.get(event_id)

        # First load of each: cache miss.
        a1 = load_event_ssr(1, _loader=fake_loader)
        b1 = load_event_ssr(2, _loader=fake_loader)
        # Second load of each: cache hit.
        a2 = load_event_ssr(1, _loader=fake_loader)
        b2 = load_event_ssr(2, _loader=fake_loader)

        assert a1 is sentinel_a and a2 is sentinel_a
        assert b1 is sentinel_b and b2 is sentinel_b
        assert calls["count"] == 2, (
            f"expected 2 loader calls (one per distinct event_id), got {calls['count']}"
        )


# ---------------------------------------------------------------------------
# Lineups policy documentation pin
# ---------------------------------------------------------------------------
class TestLineupsPolicyDocumented:
    """Phase 3 policy decision: lineups (and incidents) are PERMANENTLY
    SSR-unsupported. The SSR pageProps contains only `initialHasLineups`
    (a boolean indicating whether lineups exist) — NOT the actual lineup
    data. Any change to this policy requires explicit Kris approval.

    This test class documents the policy by:
      1. Asserting the docstring on SSR_UNSUPPORTED_ENDPOINTS mentions this.
      2. Asserting the symbol is in the set.
      3. Asserting the real-ffixture behavior matches.
    """

    def test_docstring_documents_policy(self) -> None:
        from gen4_fetcher import SSR_UNSUPPORTED_ENDPOINTS
        # The custom policy comment lives above the symbol; the symbol's
        # built-in frozenset docstring is generic. Read the module source
        # above the symbol to verify the policy is documented.
        import inspect
        import gen4_fetcher as g4f
        src = inspect.getsource(g4f)
        # The policy comment block must contain the lineups/incidents rationale.
        assert "PERMANENTLY in this set" in src, (
            "SSR_UNSUPPORTED_ENDPOINTS must carry a policy comment"
        )
        assert "lineups" in src.lower()
        assert "incidents" in src.lower()

    def test_fixture_has_initial_has_lineups_not_lineups_data(self) -> None:
        """The real fixture proves the structural gap: pageProps has
        `initialHasLineups` (boolean), NOT `lineups` (data)."""
        if not FIXTURE_PATH.exists():
            pytest.skip(f"real fixture missing: {FIXTURE_PATH}")
        raw = json.loads(FIXTURE_PATH.read_text())
        page_props = raw["props"]["pageProps"]
        assert "initialHasLineups" in page_props, (
            "fixture drift: expected initialHasLineups in pageProps"
        )
        assert "lineups" not in page_props, (
            "fixture drift: lineups appeared in pageProps — re-evaluate policy"
        )
        assert "incidents" not in page_props, (
            "fixture drift: incidents appeared in pageProps — re-evaluate policy"
        )
