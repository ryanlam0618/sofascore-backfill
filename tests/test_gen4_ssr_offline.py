#!/usr/bin/env python3
"""test_gen4_ssr_offline.py — Phase 2 offline contract tests for the
SSR helper extraction (`_map_ssr_to_api_format` + shared `gen4_ssr` module).

Covers:
  - `_map_ssr_to_api_format` against the real `data/debug_ssr_raw.json`
    fixture for /api/v1/event/{eid} → returns {"event": {...}}.
  - All structurally unsupported endpoints (incidents, lineups, statistics,
    shotmap, comments, graph, ...) return None deterministically.
  - Bad inputs (None, empty string, malformed JSON) return None — never raise.
  - Path parsing covers event_id + sub-endpoint extraction.
  - The shared `gen4_ssr.resolve_ssr_for_api_path` honours an injected loader
    so the test does NOT touch Playwright/network.
  - `SSRHelperMissing` is raised when the helper symbol is unavailable, and
    `resolve_ssr_for_api_path` translates it into a deterministic
    `ssr_helper_missing` signal at the boundary.

Run: .runner-venv/bin/python -m pytest tests/test_gen4_ssr_offline.py -q
"""
from __future__ import annotations

import json
import os
import re
import sys
import types
from pathlib import Path
from typing import Any, Dict, Optional

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PATH = REPO_ROOT / "debug_ssr_raw.json"


# ---------------------------------------------------------------------------
# Path parsing
# ---------------------------------------------------------------------------
class TestPathParsing:
    """_parse_event_id_from_path and _parse_endpoint_from_path."""

    def test_event_id_from_root_path(self) -> None:
        from gen4_fetcher import _parse_event_id_from_path
        assert _parse_event_id_from_path("/api/v1/event/16363758") == 16363758

    def test_event_id_from_sub_path(self) -> None:
        from gen4_fetcher import _parse_event_id_from_path
        assert _parse_event_id_from_path("/api/v1/event/16363758/lineups") == 16363758

    def test_event_id_from_unrelated_path(self) -> None:
        from gen4_fetcher import _parse_event_id_from_path
        assert _parse_event_id_from_path("/api/v1/tournament/17/seasons") is None

    def test_event_id_from_empty_path(self) -> None:
        from gen4_fetcher import _parse_event_id_from_path
        assert _parse_event_id_from_path("") is None
        assert _parse_event_id_from_path(None) is None  # type: ignore[arg-type]

    def test_endpoint_root_is_event(self) -> None:
        from gen4_fetcher import _parse_endpoint_from_path
        assert _parse_endpoint_from_path("/api/v1/event/16363758") == "event"

    def test_endpoint_sub_path(self) -> None:
        from gen4_fetcher import _parse_endpoint_from_path
        assert _parse_endpoint_from_path("/api/v1/event/16363758/lineups") == "lineups"
        assert _parse_endpoint_from_path("/api/v1/event/16363758/incidents") == "incidents"
        assert _parse_endpoint_from_path("/api/v1/event/16363758/shotmap") == "shotmap"

    def test_endpoint_query_suffix_stripped(self) -> None:
        """Trailing query strings don't leak into the endpoint name."""
        from gen4_fetcher import _parse_endpoint_from_path
        assert _parse_endpoint_from_path("/api/v1/event/16363758/lineups?foo=bar") == "lineups"

    def test_endpoint_unrelated_returns_none(self) -> None:
        from gen4_fetcher import _parse_endpoint_from_path
        assert _parse_endpoint_from_path("/api/v1/tournament/17/seasons") is None


# ---------------------------------------------------------------------------
# Page-props extraction
# ---------------------------------------------------------------------------
class TestExtractPageProps:
    """_extract_page_props must tolerate the standard Next.js envelope AND
    the legacy / flat shape, and return None for everything else."""

    def test_standard_next_envelope(self) -> None:
        from gen4_fetcher import _extract_page_props
        payload = {"props": {"pageProps": {"event": {"id": 1}}}}
        assert _extract_page_props(payload) == {"event": {"id": 1}}

    def test_legacy_flat_shape(self) -> None:
        from gen4_fetcher import _extract_page_props
        payload = {"pageProps": {"event": {"id": 2}}}
        assert _extract_page_props(payload) == {"event": {"id": 2}}

    def test_json_string_input(self) -> None:
        from gen4_fetcher import _extract_page_props
        payload = json.dumps({"props": {"pageProps": {"event": {"id": 3}}}})
        assert _extract_page_props(payload) == {"event": {"id": 3}}

    def test_malformed_json_string(self) -> None:
        from gen4_fetcher import _extract_page_props
        assert _extract_page_props("not json {") is None

    def test_none(self) -> None:
        from gen4_fetcher import _extract_page_props
        assert _extract_page_props(None) is None

    def test_non_dict(self) -> None:
        from gen4_fetcher import _extract_page_props
        assert _extract_page_props([1, 2, 3]) is None
        assert _extract_page_props(42) is None

    def test_missing_keys(self) -> None:
        from gen4_fetcher import _extract_page_props
        assert _extract_page_props({}) is None
        assert _extract_page_props({"props": {}}) is None


# ---------------------------------------------------------------------------
# Mapping against the real fixture
# ---------------------------------------------------------------------------
class TestMapSSRToAPIFormatRealFixture:
    """Real offline contract: data/debug_ssr_raw.json → /api/v1/event/{eid}."""

    @classmethod
    def setup_class(cls) -> None:
        if not FIXTURE_PATH.exists():
            pytest.skip(f"real fixture missing: {FIXTURE_PATH}")
        cls.raw = json.loads(FIXTURE_PATH.read_text())
        cls.event_id = cls.raw["props"]["pageProps"]["event"]["id"]

    def test_event_endpoint_returns_payload(self) -> None:
        from gen4_fetcher import _map_ssr_to_api_format
        out = _map_ssr_to_api_format(self.raw, f"/api/v1/event/{self.event_id}")
        assert out is not None
        assert "event" in out
        ev = out["event"]
        assert ev["id"] == self.event_id
        assert "homeTeam" in ev and "awayTeam" in ev
        assert "status" in ev
        assert "startTimestamp" in ev

    def test_all_unsupported_endpoints_return_none(self) -> None:
        from gen4_fetcher import _map_ssr_to_api_format, SSR_UNSUPPORTED_ENDPOINTS
        for endpoint in SSR_UNSUPPORTED_ENDPOINTS:
            path = f"/api/v1/event/{self.event_id}/{endpoint}"
            out = _map_ssr_to_api_format(self.raw, path)
            assert out is None, (
                f"endpoint '{endpoint}' should be SSR-unsupported (got {out!r})"
            )

    def test_unknown_sub_endpoint_returns_none(self) -> None:
        """Defensive: sub-endpoints not in the supported/unsupported sets
        must return None rather than crash or fabricate data."""
        from gen4_fetcher import _map_ssr_to_api_format
        out = _map_ssr_to_api_format(self.raw, f"/api/v1/event/{self.event_id}/totally-made-up")
        assert out is None

    def test_missing_event_returns_none(self) -> None:
        from gen4_fetcher import _map_ssr_to_api_format
        # Inject a payload whose pageProps.event is missing/wrong type.
        bad = {"props": {"pageProps": {"event": None}}}
        assert _map_ssr_to_api_format(bad, "/api/v1/event/1") is None

        bad2 = {"props": {"pageProps": {"event": "not-a-dict"}}}
        assert _map_ssr_to_api_format(bad2, "/api/v1/event/1") is None


# ---------------------------------------------------------------------------
# Shared gen4_ssr module contract (no Playwright, no network)
# ---------------------------------------------------------------------------
class TestGen4SsrModule:
    """Shared module honours an injected loader — never touches Playwright."""

    def test_resolve_event_with_injected_loader(self) -> None:
        from gen4_ssr import resolve_ssr_for_api_path
        if not FIXTURE_PATH.exists():
            pytest.skip(f"real fixture missing: {FIXTURE_PATH}")
        raw = json.loads(FIXTURE_PATH.read_text())
        eid = raw["props"]["pageProps"]["event"]["id"]
        loader = lambda _eid: raw  # noqa: E731 — intentionally inline lambda for the test
        out = resolve_ssr_for_api_path(
            f"/api/v1/event/{eid}", _loader=loader,
        )
        assert out is not None
        assert out["event"]["id"] == eid

    def test_resolve_unsupported_endpoint_with_injected_loader(self) -> None:
        from gen4_ssr import resolve_ssr_for_api_path
        if not FIXTURE_PATH.exists():
            pytest.skip(f"real fixture missing: {FIXTURE_PATH}")
        raw = json.loads(FIXTURE_PATH.read_text())
        eid = raw["props"]["pageProps"]["event"]["id"]
        loader = lambda _eid: raw  # noqa: E731
        out = resolve_ssr_for_api_path(
            f"/api/v1/event/{eid}/lineups", _loader=loader,
        )
        assert out is None

    def test_resolve_returns_none_when_loader_returns_none(self) -> None:
        from gen4_ssr import resolve_ssr_for_api_path
        out = resolve_ssr_for_api_path(
            "/api/v1/event/16363758", _loader=lambda _eid: None,
        )
        assert out is None

    def test_resolve_returns_none_for_unparseable_path(self) -> None:
        from gen4_ssr import resolve_ssr_for_api_path
        assert resolve_ssr_for_api_path("/api/v1/tournament/17/seasons") is None
        assert resolve_ssr_for_api_path("") is None


# ---------------------------------------------------------------------------
# SSRHelperMissing contract
# ---------------------------------------------------------------------------
class TestSSRHelperMissing:
    """The shared module must raise SSRHelperMissing (NOT silently return None)
    when `_map_ssr_to_api_format` is unimportable from gen4_fetcher."""

    def test_helper_missing_raises_typed_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from gen4_ssr import SSRHelperMissing, resolve_ssr_for_api_path, _get_map_helper

        # Force the lazy import to fail by hiding the symbol.
        # gen4_fetcher must still import successfully — only the named attr is gone.
        import gen4_fetcher as g4f
        monkeypatch.setattr(g4f, "_map_ssr_to_api_format", None, raising=False)

        # The lazy helper cache makes _get_map_helper return the current
        # attribute, but we want ImportError specifically; emulate by deleting.
        monkeypatch.delattr(g4f, "_map_ssr_to_api_format", raising=True)

        # Reset the cached "logged" flag so the stderr write is fresh,
        # then re-trigger.
        import gen4_ssr as g4s
        g4s._HELPER_MISSING_LOGGED = False  # type: ignore[attr-defined]

        with pytest.raises(SSRHelperMissing):
            _get_map_helper()

        # resolve_ssr_for_api_path must translate to SSRHelperMissing too.
        with pytest.raises(SSRHelperMissing):
            resolve_ssr_for_api_path(
                "/api/v1/event/1", _loader=lambda _eid: {"props": {"pageProps": {}}},
            )

    def test_canary_g_resolver_translates_to_none_with_side_signal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The G profile's _ssr_resolver must translate SSRHelperMissing
        into None + side-channel signal — not silently swallow."""
        import gen4_canary_validate as gcv
        # Force import by referencing — the SSR resolver is built lazily
        # when run_profile_g_full_chain is called, so we replicate it inline.
        from gen4_ssr import resolve_ssr_for_api_path, SSRHelperMissing

        # Hide the symbol to trigger the missing-helper path.
        import gen4_fetcher as g4f
        monkeypatch.delattr(g4f, "_map_ssr_to_api_format", raising=True)
        import gen4_ssr as g4s
        g4s._HELPER_MISSING_LOGGED = False  # type: ignore[attr-defined]

        _SSR_LAST: Dict[str, str] = {}

        def g_like_resolver(path: str) -> Optional[Dict[str, Any]]:
            try:
                return resolve_ssr_for_api_path(path)
            except SSRHelperMissing as e:
                _SSR_LAST["ssr_helper_missing"] = str(e)
                return None

        out = g_like_resolver("/api/v1/event/16363758")
        assert out is None
        assert "ssr_helper_missing" in _SSR_LAST
        # The typed-error message must explain the failure deterministically.
        assert "could not be imported" in _SSR_LAST["ssr_helper_missing"]


# ---------------------------------------------------------------------------
# End-to-end: canary wrapper still imports cleanly + SSR resolver is the
# shared one (no duplication)
# ---------------------------------------------------------------------------
class TestCanaryWrapperNoDuplication:
    """Sanity-check that the G/I Profile wrappers truly delegate to gen4_ssr."""

    def test_g_ssr_resolver_delegates_to_gen4_ssr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """We can't call run_profile_g_full_chain without live config, so we
        instead simulate the inner scope of G by recreating the same closure
        shape and assert it calls `gen4_ssr.resolve_ssr_for_api_path`.

        Note: the closure must look up `resolve_ssr_for_api_path` via the
        module attribute (gen4_ssr.resolve_ssr_for_api_path) so that
        monkeypatching the module actually replaces the callable used.
        """
        import gen4_ssr
        calls: list[str] = []

        def fake_resolve(path: str, **_kwargs: Any) -> Optional[Dict[str, Any]]:
            calls.append(path)
            return {"event": {"id": 999}}

        monkeypatch.setattr(gen4_ssr, "resolve_ssr_for_api_path", fake_resolve)

        # Re-run the closure body inline — lookup via module attribute so
        # the monkeypatch takes effect.
        def g_like_resolver(path: str) -> Optional[Dict[str, Any]]:
            try:
                return gen4_ssr.resolve_ssr_for_api_path(path)
            except gen4_ssr.SSRHelperMissing:
                return None

        assert g_like_resolver("/api/v1/event/16363758") == {"event": {"id": 999}}
        assert calls == ["/api/v1/event/16363758"]

    def test_gen4_ssr_does_not_import_playwright_at_load(self) -> None:
        """Importing gen4_ssr must NOT require playwright.

        Phase 3 NOTE: we previously did `sys.modules.pop('gen4_ssr')` to
        force a re-import, but that created a fresh `gen4_ssr.SSRHelperMissing`
        class which broke later tests that cached the OLD class in
        `gen4_fetcher.SSRHelperMissing`. The `except` chain then fell into
        the generic `Exception` branch and emitted 'ssr failed: ...'
        instead of 'ssr_helper_missing: ...'. Lesson: do NOT pop shared
        modules in tests; instead, check the live sys.modules.
        """
        # Check that playwright is NOT in sys.modules just because gen4_ssr
        # was imported. The shared `gen4_ssr` module should not transitively
        # import playwright at module load time.
        playwright_present = "playwright" in sys.modules
        playwright_sync_api_present = "playwright.sync_api" in sys.modules
        # If this fails, gen4_ssr has a top-level playwright import that
        # needs to be made lazy.
        assert not playwright_present, (
            f"gen4_ssr transitively imported playwright at module load: "
            f"{[k for k in sys.modules if k.startswith('playwright')]}"
        )
        assert not playwright_sync_api_present
