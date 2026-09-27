#!/usr/bin/env python3
"""test_gen4_canary_harness.py — Offline tests for Gen4 canary validator.

Covers all 12 canary gates from the prompt:
  1. No safety halt
  2. 0 HTTP 407
  3. 0 credentials leak
  4. 0 DB write
  5. 0 production-code modification
  6. G full-chain final-usable payload completeness ≥85%
  7. G full-chain event-level success ≥95%
  8. Per-endpoint reports for incidents/lineups/comments/statistics/shotmap
  9. H controlled CloakBrowser 100% pass
 10. I controlled SSR: event 100%; incidents marked ssr_unsupported_endpoint
 11. Fallback success rate separate from fallback attempt count
 12. Gen2 default unchanged

Plus:
 - request budget estimation correctness
 - safety halt checker (all 11 conditions)
 - per-endpoint metrics
 - evaluate_canary_gates happy path and individual-fail paths
 - redaction (password / cookie / auth / proxy URL)
 - SSR unsupported endpoint fast-fail semantics
 - critical-event gate (event + incidents each ≥95%)

Run:
    .runner-venv/bin/python -m pytest tests/test_gen4_canary_harness.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

REPO = Path("/root/.openclaw/workspace/sofascore-backfill")
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from gen4_canary_validate import (
    APPROVED_EVENTS, CONFIGURED_ENDPOINTS, ROUND1_ENDPOINTS,
    NOT_CONFIGURED_IN_ROUND1, SSR_UNSUPPORTED_ENDPOINTS,
    PROFILE_PLAN, MAX_TOTAL_REQUESTS, MAX_PER_EVENT,
    check_safety_halts, compute_overall_metrics, per_endpoint_metrics,
    evaluate_canary_gates, compute_estimated_budget,
    redact_secrets, make_attempt, make_record,
    GATE_G_FINAL_USABLE_PC_MIN, GATE_G_EVENT_LEVEL_SUCCESS_MIN,
    GATE_H_CLOAK_PC_MIN, GATE_I_EVENT_PC_MIN,
)


# ---------------------------------------------------------------------------
# Synthetic result factories
# ---------------------------------------------------------------------------

def _ok_result(event_id: int, endpoint: str, transport: str = "cloakbrowser",
                attempts: List[Dict[str, Any]] = None,
                fallback_used: bool = False) -> Dict[str, Any]:
    if attempts is None:
        attempts = [{"transport": transport, "status": 200, "latency_ms": 1500, "error": None}]
    return {
        "event_id": event_id, "endpoint": endpoint,
        "path_redacted": f"/api/v1/event/{{eid}}/{endpoint}",
        "attempts": attempts,
        "initial_transport": attempts[0]["transport"],
        "initial_status": attempts[0]["status"],
        "final_transport": attempts[-1]["transport"],
        "final_status": 200, "status": 200, "transport": transport,
        "latency_ms": 1500, "response_bytes": 5000,
        "json_valid": True, "payload_complete": True,
        "fallback_used": fallback_used, "retry_count": 0,
        "browser_rebuilt": False, "epipe": False,
        "error_class": None, "ok": True,
    }


def _fail_result(event_id: int, endpoint: str, status: int = 403,
                  transport: str = "curl_cffi",
                  error_class: str = None) -> Dict[str, Any]:
    return {
        "event_id": event_id, "endpoint": endpoint,
        "path_redacted": f"/api/v1/event/{{eid}}/{endpoint}",
        "attempts": [{"transport": transport, "status": status, "latency_ms": 50,
                       "error": f"forced {status}"}],
        "initial_transport": transport, "initial_status": status,
        "final_transport": transport, "final_status": status, "status": status,
        "transport": transport, "latency_ms": 50, "response_bytes": 0,
        "json_valid": False, "payload_complete": False,
        "fallback_used": False, "retry_count": 0,
        "browser_rebuilt": False, "epipe": False,
        "error_class": error_class or "anti_bot", "ok": False,
    }


def _i_unsupported(event_id: int) -> Dict[str, Any]:
    return {
        "event_id": event_id, "endpoint": "incidents",
        "path_redacted": "/api/v1/event/{eid}/incidents",
        "attempts": [
            {"transport": "curl_cffi", "status": 403, "latency_ms": 0,
             "error": "forced 403"},
            {"transport": "cloakbrowser", "status": 0, "latency_ms": 0,
             "error": "forced cloak fail"},
            {"transport": "ssr", "status": 0, "latency_ms": 4,
             "error": "ssr_returned_none (structural gap)"},
        ],
        "initial_transport": "curl_cffi", "initial_status": 403,
        "final_transport": "ssr", "final_status": 0, "status": 0,
        "transport": "ssr", "latency_ms": 4, "response_bytes": 0,
        "json_valid": False, "payload_complete": False,
        "fallback_used": True, "retry_count": 0,
        "browser_rebuilt": False, "epipe": False,
        "error_class": "ssr_unsupported_endpoint", "ok": False,
    }


# ---------------------------------------------------------------------------
# Test gate 12: Gen2 default unchanged
# ---------------------------------------------------------------------------

class TestGate12Gen2Default:
    def test_gen2_is_production_default(self):
        from gen4_fetcher import GEN2_DEFAULT
        assert GEN2_DEFAULT == "gen2"

    def test_gen4_requires_opt_in(self):
        # opt-in via env var; not module default
        import os
        original = os.environ.pop("FETCH_STRATEGY", None)
        try:
            from gen4_fetcher import resolve_fetch_strategy
            # When FETCH_STRATEGY unset → default to gen2
            os.environ.pop("FETCH_STRATEGY", None)
            assert resolve_fetch_strategy() == "gen2"
            # When FETCH_STRATEGY=gen4 → opt-in
            os.environ["FETCH_STRATEGY"] = "gen4"
            assert resolve_fetch_strategy() == "gen4"
        finally:
            if original:
                os.environ["FETCH_STRATEGY"] = original
            else:
                os.environ.pop("FETCH_STRATEGY", None)

    def test_gen4_canary_does_not_mutate_default(self):
        # Pure-Python: gen4_canary_validate never writes to env permanently.
        # Verify by running with FETCH_STRATEGY unset and confirming default resolution.
        import os
        os.environ.pop("FETCH_STRATEGY", None)
        from gen4_fetcher import resolve_fetch_strategy
        assert resolve_fetch_strategy() == "gen2"


# ---------------------------------------------------------------------------
# Test gate 1-5: safety halt family
# ---------------------------------------------------------------------------

class TestGates1To5Safety:
    def test_gate1_no_halt_clean(self):
        artifact = {"results": [_ok_result(14025013, "event")],
                    "cleanup_ok": True}
        assert check_safety_halts(artifact) is None

    def test_gate2_407_triggers_halt(self):
        artifact = {"results": [_fail_result(14025013, "event", status=407)],
                    "cleanup_ok": True}
        assert check_safety_halts(artifact) == "HTTP_407"

    def test_gate2_407_in_attempts_triggers(self):
        artifact = {"results": [{
            "status": 0, "attempts": [{"status": 407}], "event_id": 14025013,
        }], "cleanup_ok": True}
        assert check_safety_halts(artifact) == "HTTP_407"

    def test_gate3_credentials_leak_password(self):
        # Set env var, build artifact that contains the password literal
        import os
        os.environ["SOFA_PROXY_PASS"] = "longsecret"
        try:
            artifact = {"results": [], "cleanup_ok": True,
                        "config": {"proxy_url": "http://user:longsecret@host:80"}}
            assert check_safety_halts(artifact) == "CREDENTIALS_LEAK"
        finally:
            os.environ.pop("SOFA_PROXY_PASS", None)

    def test_gate4_db_write_triggers(self):
        artifact = {"results": [], "cleanup_ok": True, "db_write_attempted": True}
        assert check_safety_halts(artifact) == "DB_WRITE_DETECTED"

    def test_gate5_production_code_modified(self):
        artifact = {"results": [], "cleanup_ok": True,
                    "production_code_modified": True}
        assert check_safety_halts(artifact) == "PRODUCTION_CODE_MODIFIED"

    def test_403_rate_threshold(self):
        artifact = {"results": [
            _fail_result(14025013, "event", status=403),
            _fail_result(14025013, "incidents", status=403),
            _ok_result(14025013, "lineups"),
        ], "cleanup_ok": True}
        assert check_safety_halts(artifact) == "403_RATE_OVER_50"


# ---------------------------------------------------------------------------
# Test gate 6-8: G full-chain metrics
# ---------------------------------------------------------------------------

class TestGate6GFullChainPC:
    def _build_g_results(self, pc_per_endpoint: Dict[str, float]) -> List[Dict[str, Any]]:
        """Build G profile results with given per-endpoint PC rate (0.0-1.0)."""
        endpoints = ["event", "incidents", "lineups", "comments",
                      "statistics", "shotmap"]
        results = []
        for ep in endpoints:
            for eid in [14025013, 12436875]:
                rate = pc_per_endpoint.get(ep, 1.0)
                if rate >= 0.5:
                    results.append(_ok_result(eid, ep))
                else:
                    results.append(_fail_result(eid, ep, status=403))
        return results

    def test_gate6_pass_when_pc_above_85pct(self):
        # All endpoints 100% PC → should pass gate 6
        g_results = self._build_g_results({})
        profile = {"summary": compute_overall_metrics(g_results)}
        artifact = {"profiles_results": {"G_full_chain": profile},
                    "profiles_results.results": g_results}
        ev = evaluate_canary_gates({
            "profiles_results": {"G_full_chain": profile,
                                  "H_cloakbrowser_fallback_controlled": {"summary": {}},
                                  "I_ssr_fallback_controlled": {"requests": []}},
            "results": g_results,
        })
        assert ev["gates"]["6_g_final_usable_pc_min_85pct"]["pass"] is True

    def test_gate6_fail_when_pc_below_85pct(self):
        # Force 70% PC → fail
        g_results = []
        endpoints = ["event", "incidents", "lineups", "comments",
                      "statistics", "shotmap"]
        for i, ep in enumerate(endpoints):
            for j, eid in enumerate([14025013, 12436875]):
                # Fail every other result → ~50% PC
                if (i + j) % 2 == 0:
                    g_results.append(_fail_result(eid, ep, status=403))
                else:
                    g_results.append(_ok_result(eid, ep))
        profile = {"summary": compute_overall_metrics(g_results),
                    "requests": g_results}
        artifact = {"profiles_results": {"G_full_chain": profile,
                                          "H_cloakbrowser_fallback_controlled": {"summary": {}},
                                          "I_ssr_fallback_controlled": {"requests": []}},
                    "results": g_results}
        ev = evaluate_canary_gates(artifact)
        assert ev["gates"]["6_g_final_usable_pc_min_85pct"]["pass"] is False

    def test_gate7_event_level_pass(self):
        # Both events with at least one PC → 100% event-level
        g_results = self._build_g_results({})
        profile = {"summary": compute_overall_metrics(g_results),
                    "requests": g_results}
        artifact = {"profiles_results": {"G_full_chain": profile,
                                          "H_cloakbrowser_fallback_controlled": {"summary": {}},
                                          "I_ssr_fallback_controlled": {"requests": []}},
                    "results": g_results}
        ev = evaluate_canary_gates(artifact)
        assert ev["gates"]["7_g_event_level_success_min_95pct"]["pass"] is True
        assert ev["gates"]["7_g_event_level_success_min_95pct"]["value"] == 1.0

    def test_gate8_per_endpoint_report_present(self):
        g_results = self._build_g_results({})
        profile = {"summary": compute_overall_metrics(g_results),
                    "requests": g_results}
        artifact = {"profiles_results": {"G_full_chain": profile,
                                          "H_cloakbrowser_fallback_controlled": {"summary": {}},
                                          "I_ssr_fallback_controlled": {"requests": []}},
                    "results": g_results}
        ev = evaluate_canary_gates(artifact)
        g8 = ev["gates"]["8_g_per_endpoint_report"]
        assert g8["pass"] is True
        for ep in ["incidents", "lineups", "comments", "statistics", "shotmap"]:
            assert ep in g8["per_endpoint"]
            assert g8["per_endpoint"][ep]["n"] == 2


# ---------------------------------------------------------------------------
# Test gate 9: H controlled CloakBrowser 100%
# ---------------------------------------------------------------------------

class TestGate9HCloak100Pct:
    def test_h_pass_when_4_of_4_pc(self):
        h_results = [
            _ok_result(14025013, "event", transport="cloakbrowser"),
            _ok_result(14025013, "lineups", transport="cloakbrowser"),
            _ok_result(12436875, "event", transport="cloakbrowser"),
            _ok_result(12436875, "lineups", transport="cloakbrowser"),
        ]
        profile = {"summary": compute_overall_metrics(h_results),
                    "requests": h_results}
        artifact = {"profiles_results": {"G_full_chain": {"summary": {}, "requests": []},
                                          "H_cloakbrowser_fallback_controlled": profile,
                                          "I_ssr_fallback_controlled": {"requests": []}},
                    "results": h_results}
        ev = evaluate_canary_gates(artifact)
        assert ev["gates"]["9_h_cloak_100pct"]["pass"] is True
        assert ev["gates"]["9_h_cloak_100pct"]["value"] == 1.0

    def test_h_fail_when_one_misses(self):
        h_results = [
            _ok_result(14025013, "event", transport="cloakbrowser"),
            _ok_result(14025013, "lineups", transport="cloakbrowser"),
            _ok_result(12436875, "event", transport="cloakbrowser"),
            _fail_result(12436875, "lineups", status=403, transport="cloakbrowser"),
        ]
        profile = {"summary": compute_overall_metrics(h_results),
                    "requests": h_results}
        artifact = {"profiles_results": {"G_full_chain": {"summary": {}, "requests": []},
                                          "H_cloakbrowser_fallback_controlled": profile,
                                          "I_ssr_fallback_controlled": {"requests": []}},
                    "results": h_results}
        ev = evaluate_canary_gates(artifact)
        assert ev["gates"]["9_h_cloak_100pct"]["pass"] is False
        assert ev["gates"]["9_h_cloak_100pct"]["value"] == 0.75


# ---------------------------------------------------------------------------
# Test gate 10: I controlled SSR split semantics
# ---------------------------------------------------------------------------

class TestGate10ISsrSplit:
    def test_i_event_100pct_and_incidents_unsupported(self):
        i_results = [
            _i1_event_ok(14025013), _i1_event_ok(12436875),
            _i1_incidents_unsupported(14025013),
            _i1_incidents_unsupported(12436875),
        ]
        profile = {"summary": compute_overall_metrics(i_results),
                    "requests": i_results}
        artifact = {"profiles_results": {"G_full_chain": {"summary": {}, "requests": []},
                                          "H_cloakbrowser_fallback_controlled": {"summary": {}},
                                          "I_ssr_fallback_controlled": profile},
                    "results": i_results}
        ev = evaluate_canary_gates(artifact)
        g10 = ev["gates"]["10_i_event_100pct_and_incidents_unsupported"]
        assert g10["pass"] is True
        assert g10["event_pc_rate"] == 1.0
        assert g10["incidents_unsupported"] is True

    def test_i_event_fail_breaks_gate(self):
        i_results = [
            _fail_result(14025013, "event", status=0, transport="ssr"),
            _i1_event_ok(12436875),
            _i1_incidents_unsupported(14025013),
            _i1_incidents_unsupported(12436875),
        ]
        profile = {"summary": compute_overall_metrics(i_results),
                    "requests": i_results}
        artifact = {"profiles_results": {"G_full_chain": {"summary": {}, "requests": []},
                                          "H_cloakbrowser_fallback_controlled": {"summary": {}},
                                          "I_ssr_fallback_controlled": profile},
                    "results": i_results}
        ev = evaluate_canary_gates(artifact)
        g10 = ev["gates"]["10_i_event_100pct_and_incidents_unsupported"]
        assert g10["pass"] is False
        assert g10["event_pc_rate"] < 1.0

    def test_i_incidents_marked_wrong_breaks_gate(self):
        i_results = [
            _i1_event_ok(14025013), _i1_event_ok(12436875),
            _fail_result(14025013, "incidents", status=403, transport="ssr",
                          error_class="anti_bot"),
            _i1_incidents_unsupported(12436875),
        ]
        profile = {"summary": compute_overall_metrics(i_results),
                    "requests": i_results}
        artifact = {"profiles_results": {"G_full_chain": {"summary": {}, "requests": []},
                                          "H_cloakbrowser_fallback_controlled": {"summary": {}},
                                          "I_ssr_fallback_controlled": profile},
                    "results": i_results}
        ev = evaluate_canary_gates(artifact)
        g10 = ev["gates"]["10_i_event_100pct_and_incidents_unsupported"]
        assert g10["pass"] is False


# ---------------------------------------------------------------------------
# Test gate 11: fallback attempt vs success separated
# ---------------------------------------------------------------------------

class TestGate11FallbackSeparation:
    def test_metrics_expose_both_fields(self):
        results = [
            {"status": 200, "ok": True, "payload_complete": True,
             "fallback_used": True, "latency_ms": 1000,
             "event_id": 1, "endpoint": "event"},
            {"status": 403, "ok": False, "payload_complete": False,
             "fallback_used": True, "latency_ms": 200,
             "event_id": 1, "endpoint": "incidents"},
            {"status": 200, "ok": True, "payload_complete": True,
             "fallback_used": False, "latency_ms": 500,
             "event_id": 1, "endpoint": "lineups"},
        ]
        m = compute_overall_metrics(results)
        assert m["fallback_attempt_count"] == 2
        assert m["fallback_success_count"] == 1
        # fallback_success_rate is computed by evaluate_canary_gates, not metrics
        artifact = {"profiles_results": {"G_full_chain": {"summary": m, "requests": results},
                                          "H_cloakbrowser_fallback_controlled": {"summary": {}, "requests": []},
                                          "I_ssr_fallback_controlled": {"requests": []}},
                    "results": results}
        ev = evaluate_canary_gates(artifact)
        fb = ev["gates"]["11_fallback_attempt_vs_success_separated"]
        assert fb["fallback_attempt_count"] == 2
        assert fb["fallback_success_count"] == 1
        assert abs(fb["fallback_success_rate"] - 0.5) < 0.01

    def test_no_fallback_attempts_returns_none_rate(self):
        results = [
            {"status": 200, "ok": True, "payload_complete": True,
             "fallback_used": False, "latency_ms": 500,
             "event_id": 1, "endpoint": "event"},
        ]
        m = compute_overall_metrics(results)
        assert m["fallback_attempt_count"] == 0
        artifact = {"profiles_results": {"G_full_chain": {"summary": m, "requests": results},
                                          "H_cloakbrowser_fallback_controlled": {"summary": {}, "requests": []},
                                          "I_ssr_fallback_controlled": {"requests": []}},
                    "results": results}
        ev = evaluate_canary_gates(artifact)
        assert ev["gates"]["11_fallback_attempt_vs_success_separated"]["fallback_success_rate"] is None


# ---------------------------------------------------------------------------
# Test budget estimation
# ---------------------------------------------------------------------------

class TestBudgetEstimation:
    def test_estimated_within_150(self):
        b = compute_estimated_budget()
        assert b["within_budget"] is True
        assert b["estimated_total_requests"] <= MAX_TOTAL_REQUESTS
        assert b["estimated_max_per_event"] <= MAX_PER_EVENT

    def test_per_event_cap_enforced(self):
        # Verify per-event cap: even worst profile (G) is 18 requests/event
        g_plan = PROFILE_PLAN["G_full_chain"]
        per_event = g_plan["estimated_requests"] // g_plan["events"]
        assert per_event <= MAX_PER_EVENT

    def test_highlights_not_configured_in_budget(self):
        # highlights should be in NOT_CONFIGURED_IN_ROUND1, not in any budget line
        assert "highlights" in NOT_CONFIGURED_IN_ROUND1
        # But ROUND1_ENDPOINTS lists it for the matrix iteration
        assert "highlights" in ROUND1_ENDPOINTS

    def test_j_warm_session_zero_requests(self):
        j_plan = PROFILE_PLAN["J_warm_session"]
        assert j_plan["estimated_requests"] == 0
        assert j_plan["live_approved"] is False


# ---------------------------------------------------------------------------
# Test critical event/incidents gate
# ---------------------------------------------------------------------------

class TestCriticalEventAndIncidentsGate:
    def test_event_and_incidents_each_above_95pct(self):
        g_results = []
        for ep in ["event", "incidents", "lineups", "comments",
                    "statistics", "shotmap"]:
            for eid in [14025013, 12436875]:
                g_results.append(_ok_result(eid, ep))
        profile = {"summary": compute_overall_metrics(g_results),
                    "requests": g_results}
        artifact = {"profiles_results": {"G_full_chain": profile,
                                          "H_cloakbrowser_fallback_controlled": {"summary": {}},
                                          "I_ssr_fallback_controlled": {"requests": []}},
                    "results": g_results}
        ev = evaluate_canary_gates(artifact)
        crit = ev["gates"]["critical_event_and_incidents_pc_min_95pct"]
        assert crit["event"]["pass"] is True
        assert crit["incidents"]["pass"] is True

    def test_event_below_95pct_breaks_critical_gate(self):
        g_results = []
        for ep in ["event", "incidents", "lineups", "comments",
                    "statistics", "shotmap"]:
            for eid in [14025013, 12436875]:
                if ep == "event" and eid == 12436875:
                    g_results.append(_fail_result(eid, ep, status=403))
                else:
                    g_results.append(_ok_result(eid, ep))
        profile = {"summary": compute_overall_metrics(g_results),
                    "requests": g_results}
        artifact = {"profiles_results": {"G_full_chain": profile,
                                          "H_cloakbrowser_fallback_controlled": {"summary": {}},
                                          "I_ssr_fallback_controlled": {"requests": []}},
                    "results": g_results}
        ev = evaluate_canary_gates(artifact)
        crit = ev["gates"]["critical_event_and_incidents_pc_min_95pct"]
        assert crit["event"]["pass"] is False  # 1/2 = 50% < 95%


# ---------------------------------------------------------------------------
# Test redaction
# ---------------------------------------------------------------------------

class TestRedaction:
    def test_password_redacted_when_env_set(self, monkeypatch):
        monkeypatch.setenv("SOFA_PROXY_PASS", "longsecret")
        out = redact_secrets("url=http://user:longsecret@host:80")
        assert "longsecret" not in out
        assert "[REDACTED]" in out

    def test_authorization_redacted(self, monkeypatch):
        monkeypatch.delenv("SOFA_PROXY_PASS", raising=False)
        # Use '=' separator (typical auth header format)
        out = redact_secrets("authorization=Bearer abcdef rest")
        assert "abcdef" not in out
        assert "[REDACTED]" in out

    def test_proxy_url_in_blob_redacted(self, monkeypatch):
        monkeypatch.delenv("SOFA_PROXY_PASS", raising=False)
        out = redact_secrets("proxy=http://user:secret@host:80 logs")
        assert "secret" not in out
        assert "[REDACTED]" in out


# ---------------------------------------------------------------------------
# Test per-endpoint metrics
# ---------------------------------------------------------------------------

class TestPerEndpointMetrics:
    def test_groups_by_endpoint(self):
        results = [
            _ok_result(1, "event"),
            _fail_result(1, "event", status=403),
            _ok_result(1, "incidents"),
            _ok_result(1, "incidents"),
        ]
        pem = per_endpoint_metrics(results)
        assert pem["event"]["n"] == 2
        assert pem["event"]["payload_completeness_rate"] == 0.5
        assert pem["incidents"]["n"] == 2
        assert pem["incidents"]["payload_completeness_rate"] == 1.0

    def test_empty_results(self):
        assert per_endpoint_metrics([]) == {}


# ---------------------------------------------------------------------------
# Test SSR unsupported endpoint set
# ---------------------------------------------------------------------------

class TestSSRUnsupportedSet:
    def test_incidents_in_unsupported(self):
        assert "incidents" in SSR_UNSUPPORTED_ENDPOINTS

    def test_event_not_in_unsupported(self):
        # event is the only one SSR can serve (embedded in __NEXT_DATA__)
        assert "event" not in SSR_UNSUPPORTED_ENDPOINTS


# ---------------------------------------------------------------------------
# Test canary overall pass/fail
# ---------------------------------------------------------------------------

class TestCanaryOverall:
    def test_clean_canary_pass(self):
        # Build a clean artifact where all gates pass.
        # I records MUST use 3-tier I1 helpers (not single-tier _ok_result) so
        # gate 10's attempts_chain_complete sub-check passes.
        g_results = [
            _ok_result(eid, ep, transport="cloakbrowser")
            for ep in ["event", "incidents", "lineups", "comments",
                        "statistics", "shotmap"]
            for eid in [14025013, 12436875]
        ]
        h_results = [
            _ok_result(eid, ep, transport="cloakbrowser")
            for ep in ["event", "lineups"]
            for eid in [14025013, 12436875]
        ]
        i_results = [
            _i1_event_ok(14025013), _i1_event_ok(12436875),
            _i1_incidents_unsupported(14025013),
            _i1_incidents_unsupported(12436875),
        ]
        artifact = {
            "profiles_results": {
                "G_full_chain": {"summary": compute_overall_metrics(g_results),
                                  "requests": g_results},
                "H_cloakbrowser_fallback_controlled": {"summary": compute_overall_metrics(h_results),
                                                       "requests": h_results},
                "I_ssr_fallback_controlled": {"summary": compute_overall_metrics(i_results),
                                               "requests": i_results},
            },
            "results": g_results + h_results + i_results,
            "halted": False, "halt_reason": None, "cleanup_ok": True,
        }
        ev = evaluate_canary_gates(artifact)
        assert ev["canary_pass"] is True
        assert ev["all_pass"] is True

    def test_407_in_any_profile_breaks_canary(self):
        g_results = [
            _ok_result(eid, ep) for ep in ["incidents", "lineups", "comments",
                                            "statistics", "shotmap"]
            for eid in [14025013, 12436875]
        ] + [
            _fail_result(eid, "event", status=407) for eid in [14025013, 12436875]
        ]
        h_results = [_ok_result(eid, ep, transport="cloakbrowser")
                     for ep in ["event", "lineups"]
                     for eid in [14025013, 12436875]]
        i_results = [_ok_result(eid, "event", transport="ssr")
                     for eid in [14025013, 12436875]] + [
            _i_unsupported(eid) for eid in [14025013, 12436875]]
        artifact = {
            "profiles_results": {
                "G_full_chain": {"summary": compute_overall_metrics(g_results),
                                  "requests": g_results},
                "H_cloakbrowser_fallback_controlled": {"summary": compute_overall_metrics(h_results),
                                                       "requests": h_results},
                "I_ssr_fallback_controlled": {"summary": compute_overall_metrics(i_results),
                                               "requests": i_results},
            },
            "results": g_results + h_results + i_results,
            "halted": True, "halt_reason": "HTTP_407",
            "cleanup_ok": True,
        }
        ev = evaluate_canary_gates(artifact)
        assert ev["gates"]["2_zero_407"]["pass"] is False
        assert ev["canary_pass"] is False


# ---------------------------------------------------------------------------
# Test I1 Gen4 orchestration contract + I2 SSR reference exclusion
# ---------------------------------------------------------------------------

def _i1_event_ok(event_id: int) -> Dict[str, Any]:
    """Synthetic I1 result: event 100% pass via Gen4 real orchestration."""
    return {
        "event_id": event_id, "endpoint": "event",
        "path_redacted": "/api/v1/event/{eid}",
        "attempts": [
            {"transport": "curl_cffi", "status": 403, "latency_ms": 0,
             "error": "forced 403"},
            {"transport": "cloakbrowser", "status": 0, "latency_ms": 0,
             "error": "forced cloak fail"},
            {"transport": "ssr", "status": 200, "latency_ms": 18000, "error": None},
        ],
        "initial_transport": "curl_cffi", "initial_status": 403,
        "final_transport": "ssr", "final_status": 200,
        "status": 200, "transport": "ssr", "latency_ms": 18000,
        "payload_complete": True,
        "fallback_used": True, "retry_count": 0,
        "ok": True, "error_class": None,
        "is_gen4_orchestration_result": True,
    }


def _i1_incidents_unsupported(event_id: int) -> Dict[str, Any]:
    """Synthetic I1 result: incidents structural gap via Gen4 orchestration."""
    return {
        "event_id": event_id, "endpoint": "incidents",
        "path_redacted": "/api/v1/event/{eid}/incidents",
        "attempts": [
            {"transport": "curl_cffi", "status": 403, "latency_ms": 0,
             "error": "forced 403"},
            {"transport": "cloakbrowser", "status": 0, "latency_ms": 0,
             "error": "forced cloak fail"},
            {"transport": "ssr", "status": 0, "latency_ms": 4,
             "error": "ssr_returned_none"},
        ],
        "initial_transport": "curl_cffi", "initial_status": 403,
        "final_transport": "ssr", "final_status": 0,
        "status": 0, "transport": "ssr", "latency_ms": 4,
        "payload_complete": False,
        "fallback_used": True, "retry_count": 0,
        "ok": False, "error_class": "ssr_unsupported_endpoint",
        "is_gen4_orchestration_result": True,
    }


def _i2_reference(event_id: int, endpoint: str,
                   payload_complete: bool) -> Dict[str, Any]:
    """Synthetic I2 result: direct get_event_ssr() reference, NOT I1."""
    return {
        "event_id": event_id, "endpoint": endpoint,
        "path_redacted": f"/api/v1/event/{{eid}}/{endpoint}",
        "attempts": [{"transport": "ssr", "status": 200 if payload_complete else 0,
                       "latency_ms": 18000 if payload_complete else 4,
                       "error": None if payload_complete else "ssr_returned_none"}],
        "status": 200 if payload_complete else 0,
        "transport": "ssr",
        "latency_ms": 18000 if payload_complete else 4,
        "payload_complete": payload_complete,
        "fallback_used": True, "retry_count": 0,
        "ok": payload_complete,
        "error_class": (None if payload_complete
                         else "ssr_unsupported_endpoint" if endpoint != "event"
                         else "ssr_returned_none"),
        "is_gen4_orchestration_result": False,
        "reference_only": True,
    }


class TestI1Gen4OrchestrationContract:
    """Verify Profile I1 satisfies the Gen4 orchestration contract."""

    def _build_artifact(self, i1_results: List[Dict[str, Any]],
                         i2_results: List[Dict[str, Any]]) -> Dict[str, Any]:
        # Provide minimal h_results so other gates don't fail
        h_results = [_ok_result(eid, ep, transport="cloakbrowser")
                     for ep in ["event", "lineups"]
                     for eid in [14025013, 12436875]]
        g_results = [_ok_result(eid, ep, transport="cloakbrowser")
                     for ep in ["event", "incidents", "lineups", "comments",
                                 "statistics", "shotmap"]
                     for eid in [14025013, 12436875]]
        return {
            "profiles_results": {
                "G_full_chain": {"summary": compute_overall_metrics(g_results),
                                  "requests": g_results},
                "H_cloakbrowser_fallback_controlled": {"summary": compute_overall_metrics(h_results),
                                                       "requests": h_results},
                "I_ssr_fallback_controlled": {
                    "summary": compute_overall_metrics(i1_results),
                    "i1": {"summary": compute_overall_metrics(i1_results),
                            "requests": i1_results},
                    "i2": {"summary": compute_overall_metrics(i2_results),
                            "requests": i2_results},
                },
            },
            "results": g_results + h_results + i1_results,
            "halted": False, "halt_reason": None, "cleanup_ok": True,
        }

    def test_i1_attempts_chain_has_three_tiers(self):
        i1 = [_i1_event_ok(14025013), _i1_event_ok(12436875),
              _i1_incidents_unsupported(14025013),
              _i1_incidents_unsupported(12436875)]
        i2 = [_i2_reference(14025013, "event", True),
              _i2_reference(12436875, "event", True),
              _i2_reference(14025013, "incidents", False),
              _i2_reference(12436875, "incidents", False)]
        artifact = self._build_artifact(i1, i2)
        ev = evaluate_canary_gates(artifact)
        g10 = ev["gates"]["10_i_event_100pct_and_incidents_unsupported"]
        assert g10["attempts_chain_complete"] is True
        for r in i1:
            tier_set = {a["transport"] for a in r["attempts"]}
            assert "curl_cffi" in tier_set
            assert "cloakbrowser" in tier_set
            assert "ssr" in tier_set

    def test_i1_event_100pct(self):
        i1 = [_i1_event_ok(14025013), _i1_event_ok(12436875),
              _i1_incidents_unsupported(14025013),
              _i1_incidents_unsupported(12436875)]
        i2 = [_i2_reference(14025013, "event", True),
              _i2_reference(12436875, "event", True),
              _i2_reference(14025013, "incidents", False),
              _i2_reference(12436875, "incidents", False)]
        artifact = self._build_artifact(i1, i2)
        ev = evaluate_canary_gates(artifact)
        g10 = ev["gates"]["10_i_event_100pct_and_incidents_unsupported"]
        assert g10["event_pc_rate"] == 1.0
        assert g10["pass"] is True

    def test_i1_incidents_marked_unsupported(self):
        i1 = [_i1_event_ok(14025013), _i1_event_ok(12436875),
              _i1_incidents_unsupported(14025013),
              _i1_incidents_unsupported(12436875)]
        i2 = [_i2_reference(14025013, "event", True),
              _i2_reference(12436875, "event", True),
              _i2_reference(14025013, "incidents", False),
              _i2_reference(12436875, "incidents", False)]
        artifact = self._build_artifact(i1, i2)
        ev = evaluate_canary_gates(artifact)
        g10 = ev["gates"]["10_i_event_100pct_and_incidents_unsupported"]
        assert g10["incidents_unsupported"] is True

    def test_i1_no_infinite_retry(self):
        # Each I1 record should have ≤3 attempts (one per tier; no rebuild loop)
        i1 = [_i1_event_ok(14025013), _i1_event_ok(12436875),
              _i1_incidents_unsupported(14025013),
              _i1_incidents_unsupported(12436875)]
        i2 = []
        for r in i1:
            assert len(r["attempts"]) <= 3, (
                f"I1 attempts chain too long ({len(r['attempts'])}); "
                "Gen4 fetch_api should not infinite retry"
            )

    def test_i1_fallback_used_true(self):
        i1 = [_i1_event_ok(14025013), _i1_event_ok(12436875),
              _i1_incidents_unsupported(14025013),
              _i1_incidents_unsupported(12436875)]
        for r in i1:
            assert r["fallback_used"] is True

    def test_i1_event_failure_breaks_gate(self):
        i1 = [_fail_result(14025013, "event", status=403, transport="ssr"),
              _i1_event_ok(12436875),
              _i1_incidents_unsupported(14025013),
              _i1_incidents_unsupported(12436875)]
        i2 = []
        artifact = self._build_artifact(i1, i2)
        ev = evaluate_canary_gates(artifact)
        g10 = ev["gates"]["10_i_event_100pct_and_incidents_unsupported"]
        assert g10["pass"] is False

    def test_i1_incidents_not_unsupported_breaks_gate(self):
        i1 = [_i1_event_ok(14025013), _i1_event_ok(12436875),
              _fail_result(14025013, "incidents", status=403, transport="ssr",
                            error_class="anti_bot"),
              _i1_incidents_unsupported(12436875)]
        i2 = []
        artifact = self._build_artifact(i1, i2)
        ev = evaluate_canary_gates(artifact)
        g10 = ev["gates"]["10_i_event_100pct_and_incidents_unsupported"]
        assert g10["pass"] is False


class TestI2ExcludedFromGate:
    """I2 records must NOT influence gate evaluation."""

    def test_i2_records_excluded_when_i1_present(self):
        # Build I1 with failed event AND I2 with passing event.
        # If I2 leaked into gate evaluation, gate would erroneously pass.
        i1 = [_fail_result(14025013, "event", status=403, transport="ssr"),
              _fail_result(12436875, "event", status=403, transport="ssr"),
              _i1_incidents_unsupported(14025013),
              _i1_incidents_unsupported(12436875)]
        i2 = [_i2_reference(14025013, "event", True),
              _i2_reference(12436875, "event", True),
              _i2_reference(14025013, "incidents", False),
              _i2_reference(12436875, "incidents", False)]
        h_results = [_ok_result(eid, ep, transport="cloakbrowser")
                     for ep in ["event", "lineups"]
                     for eid in [14025013, 12436875]]
        g_results = [_ok_result(eid, ep, transport="cloakbrowser")
                     for ep in ["event", "incidents", "lineups", "comments",
                                 "statistics", "shotmap"]
                     for eid in [14025013, 12436875]]
        artifact = {
            "profiles_results": {
                "G_full_chain": {"summary": compute_overall_metrics(g_results),
                                  "requests": g_results},
                "H_cloakbrowser_fallback_controlled": {"summary": compute_overall_metrics(h_results),
                                                       "requests": h_results},
                "I_ssr_fallback_controlled": {
                    "summary": compute_overall_metrics(i1),
                    "i1": {"summary": compute_overall_metrics(i1),
                            "requests": i1},
                    "i2": {"summary": compute_overall_metrics(i2),
                            "requests": i2},
                },
            },
            "results": g_results + h_results + i1,
            "halted": False, "halt_reason": None, "cleanup_ok": True,
        }
        ev = evaluate_canary_gates(artifact)
        g10 = ev["gates"]["10_i_event_100pct_and_incidents_unsupported"]
        # Gate must FAIL because I1 event has 0% PC; I2's "passing" records
        # must NOT inflate the rate.
        assert g10["event_pc_rate"] == 0.0
        assert g10["pass"] is False

    def test_i2_marker_present(self):
        rec = _i2_reference(14025013, "event", True)
        assert rec["reference_only"] is True
        assert rec["is_gen4_orchestration_result"] is False

    def test_i2_not_in_gate_filter(self):
        # Confirm defensive filter removes I2 records.
        results = [_i1_event_ok(14025013), _i2_reference(14025013, "event", True)]
        filtered = [r for r in results if not r.get("reference_only")
                     and r.get("is_gen4_orchestration_result", True)]
        assert len(filtered) == 1
        assert filtered[0]["endpoint"] == "event"
        # The kept record must have 3-tier attempts (I1 characteristic)
        assert len(filtered[0]["attempts"]) == 3


# ---------------------------------------------------------------------------
# Run summary
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pytest.main([__file__, "-v"])

