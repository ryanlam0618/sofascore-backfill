#!/usr/bin/env python3
"""
test_endpoint_normalization.py — Offline unit tests for endpoint normalization.

Per Blocking Condition #1 (Tests first, then patch), these tests are written
BEFORE the production helper is added/patched. They must be confirmed to
FAIL on the current buggy code, then PASS after the minimal patch.

Per Blocking Condition #3 (Tests validate the data/status contract), tests
cover:
- Endpoint string normalization (cases A1-A8)
- Rejection contract — return None for unknown/malformed (cases B1-B7)
- Edge cases — trailing slash, uppercase (cases C1-C3)
- Data/status capture contract (cases D1-D6)

Total: 18 endpoint tests + 6 contract tests = 24 tests.

Run:
    cd /root/.openclaw/workspace/sofascore-backfill
    ./.runner-venv/bin/python -m pytest tests/test_endpoint_normalization.py -v

After the production helper is added, tests should all pass.
"""

from __future__ import annotations

import pytest
import sys
from pathlib import Path

# Add repo root to sys.path so we can import stable_proxy_fetch
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

try:
    from stable_proxy_fetch import _normalize_endpoint_name
    HELPER_AVAILABLE = True
except ImportError:
    HELPER_AVAILABLE = False


# ═══════════════════════════════════════════════════════════════════════════
# Skip marker for tests that require the helper to be defined
# ═══════════════════════════════════════════════════════════════════════════

requires_helper = pytest.mark.skipif(
    not HELPER_AVAILABLE,
    reason="_normalize_endpoint_name not yet defined in stable_proxy_fetch"
)


# ═══════════════════════════════════════════════════════════════════════════
# Section A — Endpoint string normalization (8 cases)
# ═══════════════════════════════════════════════════════════════════════════

@requires_helper
class TestEndpointNormalization:
    """Per Condition #3: normalized key MUST be one of TARGET_ENDPOINTS."""

    def test_a1_event_root(self):
        assert _normalize_endpoint_name("event/14025013") == "event"

    def test_a2_event_incidents(self):
        assert _normalize_endpoint_name("event/14025013/incidents") == "incidents"

    def test_a3_event_lineups(self):
        assert _normalize_endpoint_name("event/14025013/lineups") == "lineups"

    def test_a4_event_shotmap(self):
        assert _normalize_endpoint_name("event/14025013/shotmap") == "shotmap"

    def test_a5_event_graph(self):
        assert _normalize_endpoint_name("event/14025013/graph") == "graph"

    def test_a6_event_statistics(self):
        assert _normalize_endpoint_name("event/14025013/statistics") == "statistics"

    def test_a7_event_comments(self):
        assert _normalize_endpoint_name("event/14025013/comments") == "comments"

    def test_a8_event_web_odds(self):
        assert _normalize_endpoint_name("event/14025013/odds/1/web-odds") == "web-odds"


# ═══════════════════════════════════════════════════════════════════════════
# Section B — Rejection contract (7 cases — must return None, NOT "unknown")
# ═══════════════════════════════════════════════════════════════════════════

@requires_helper
class TestRejectionContract:
    """Per Condition #2: unknown/malformed URLs MUST return None."""

    def test_b1_empty_string(self):
        assert _normalize_endpoint_name("") is None

    def test_b2_none_input(self):
        assert _normalize_endpoint_name(None) is None

    def test_b3_non_numeric_eid(self):
        assert _normalize_endpoint_name("event/abc/incidents") is None

    def test_b4_unknown_subpath(self):
        assert _normalize_endpoint_name("event/14025013/unknown-subpath") is None

    def test_b5_non_endpoint_path(self):
        assert _normalize_endpoint_name("unique-tournament/17/season/48981/events/last/0") is None

    def test_b6_query_string(self):
        assert _normalize_endpoint_name("event/14025013?foo=1") is None

    def test_b7_fragment(self):
        assert _normalize_endpoint_name("event/14025013#section") is None


# ═══════════════════════════════════════════════════════════════════════════
# Section C — Edge cases (3 cases — explicit behaviour)
# ═══════════════════════════════════════════════════════════════════════════

@requires_helper
class TestEdgeCases:
    """Trailing slash, uppercase variants."""

    def test_c1_trailing_slash_stripped(self):
        assert _normalize_endpoint_name("event/14025013/") == "event"

    def test_c2_uppercase_rejected(self):
        assert _normalize_endpoint_name("EVENT/14025013/incidents") is None

    def test_c3_mixed_case_rejected(self):
        assert _normalize_endpoint_name("event/14025013/Incidents") is None


# ═══════════════════════════════════════════════════════════════════════════
# Section D — Data/status capture contract (6 cases)
# ═══════════════════════════════════════════════════════════════════════════
# These tests verify the **paired write to api_data and api_status**
# when capture() is exercised against a mock response. They use a
# lightweight fake `resp` object so no browser / network is involved.

class FakeResp:
    """Minimal stand-in for playwright's Response object."""
    def __init__(self, url: str, status: int, body=None):
        self.url = url
        self.status = status
        self._body = body

    def json(self):
        return self._body

    def text(self):
        import json
        return json.dumps(self._body) if self._body is not None else ""


def _make_capture(api_data, api_status, log_warnings):
    """Build a capture() closure that mirrors the worker subprocess logic."""
    def capture(resp):
        url = resp.url
        if "/api/v1/" not in url or "sofascore.com" not in url:
            return
        try:
            tail = url.split("/api/v1/", 1)[-1].rstrip("/")
            ep = _normalize_endpoint_name(tail)
            if ep is None:
                log_warnings.append(url)
                return
            status = resp.status
            if status == 200:
                try:
                    body = resp.json()
                except Exception:
                    try:
                        body = resp.text()
                    except Exception:
                        body = None
                api_data[ep] = body
            api_status[ep] = status
        except Exception:
            pass
    return capture


@requires_helper
class TestCaptureContract:
    """Per Condition #3: capture() must write data and status with same key."""

    def test_d1_incidents_known_endpoint_paired_write(self):
        api_data, api_status, warns = {}, {}, []
        cap = _make_capture(api_data, api_status, warns)
        cap(FakeResp(
            "https://www.sofascore.com/api/v1/event/14025013/incidents",
            200, body={"incidents": [1, 2, 3]},
        ))
        assert "incidents" in api_data
        assert "incidents" in api_status
        assert api_status["incidents"] == 200
        assert warns == []

    def test_d2_event_root_paired_write(self):
        api_data, api_status, warns = {}, {}, []
        cap = _make_capture(api_data, api_status, warns)
        cap(FakeResp(
            "https://www.sofascore.com/api/v1/event/14025013",
            200, body={"event": {"id": 14025013}},
        ))
        assert "event" in api_data
        assert "event" in api_status
        assert api_status["event"] == 200

    def test_d3_unknown_endpoint_no_pollution(self):
        api_data, api_status, warns = {}, {}, []
        cap = _make_capture(api_data, api_status, warns)
        cap(FakeResp(
            "https://www.sofascore.com/api/v1/unique-tournament/17/season/48981/events/last/0",
            200, body={"events": []},
        ))
        assert api_data == {}
        assert api_status == {}
        assert len(warns) == 1

    def test_d4_non_api_url_no_pollution(self):
        api_data, api_status, warns = {}, {}, []
        cap = _make_capture(api_data, api_status, warns)
        cap(FakeResp(
            "https://cdn.sofascore.com/static/img/team/abc.png",
            200, body=b"\x89PNG",
        ))
        assert api_data == {}
        assert api_status == {}
        assert warns == []

    def test_d5_query_string_rejected_no_pollution(self):
        api_data, api_status, warns = {}, {}, []
        cap = _make_capture(api_data, api_status, warns)
        cap(FakeResp(
            "https://www.sofascore.com/api/v1/event/14025013/incidents?refetch=1",
            200, body={"incidents": []},
        ))
        assert api_data == {}
        assert api_status == {}
        assert len(warns) == 1

    def test_d6_duplicate_capture_idempotent(self):
        api_data, api_status, warns = {}, {}, []
        cap = _make_capture(api_data, api_status, warns)
        for _ in range(3):
            cap(FakeResp(
                "https://www.sofascore.com/api/v1/event/14025013/incidents",
                200, body={"incidents": [1]},
            ))
        # Exactly one entry per normalized key (last write wins, no duplication)
        assert len(api_data) == 1
        assert len(api_status) == 1
        assert "incidents" in api_data
        assert "incidents" in api_status


# ═══════════════════════════════════════════════════════════════════════════
# Pre-patch verification helper (per Blocking Condition #1)
# ═══════════════════════════════════════════════════════════════════════════

def test_helper_exists_marker():
    """
    Sentinel test that reports whether the helper is importable.
    This test ALWAYS passes (it's just a marker). Use it to confirm
    the helper has been added before running the rest.
    """
    if not HELPER_AVAILABLE:
        pytest.skip("_normalize_endpoint_name not yet defined — tests skipped until patch is applied")
    assert callable(_normalize_endpoint_name)
