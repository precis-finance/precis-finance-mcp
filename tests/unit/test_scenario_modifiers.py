# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Tests for scenario modifier parsing and commit-aware WHERE clause generation.

Covers 7C.2 (modifier parser), 7C.3 (default committed-only),
7C.4 (uncommitted modifiers), 7C.5 (commit time-travel modifiers).
"""
from __future__ import annotations

import os

import pytest

from precis_mcp.engine.resolver import (
    DataQuery as ResolverDataQuery,
    ResolverError,
    parse_scenario_modifiers,
    resolve,
    strip_scenario_modifiers,
)
from precis_mcp.engine.retriever import (
    DataQuery as RetrieverDataQuery,
    generate_sql,
)

# ---------------------------------------------------------------------------
# parse_scenario_modifiers
# ---------------------------------------------------------------------------

class TestParseScenarioModifiers:
    def test_no_modifiers(self):
        base, mods = parse_scenario_modifiers("budget")
        assert base == "budget"
        assert mods == {}

    def test_flag_modifier(self):
        base, mods = parse_scenario_modifiers("budget&uncommitted")
        assert base == "budget"
        assert mods == {"uncommitted": ""}

    def test_value_modifier(self):
        base, mods = parse_scenario_modifiers("budget&commit=abc123")
        assert base == "budget"
        assert mods == {"commit": "abc123"}

    def test_multiple_modifiers(self):
        # Not a realistic combo, but tests the parser
        base, mods = parse_scenario_modifiers("forecast&uncommitted&commit=xyz")
        assert base == "forecast"
        assert mods == {"uncommitted": "", "commit": "xyz"}

    def test_unknown_modifier_raises(self):
        with pytest.raises(ResolverError, match="Unknown scenario modifier.*bogus"):
            parse_scenario_modifiers("budget&bogus")

    def test_value_with_equals_in_value(self):
        base, mods = parse_scenario_modifiers("budget&commit=abc=def")
        assert base == "budget"
        assert mods == {"commit": "abc=def"}


class TestStripScenarioModifiers:
    def test_no_modifiers(self):
        assert strip_scenario_modifiers("budget") == "budget"

    def test_with_modifiers(self):
        assert strip_scenario_modifiers("budget&uncommitted") == "budget"

    def test_with_value_modifier(self):
        assert strip_scenario_modifiers("budget&commit=abc") == "budget"


# ---------------------------------------------------------------------------
# Resolver: modifier parsing in resolve()
# ---------------------------------------------------------------------------

def _make_request(scenario_key: str, catalogue) -> dict:
    """Build a minimal report request with a single scenario block."""
    return {
        "context": {"period_start": "2026-01", "period_end": "2026-12"},
        "blocks": [
            {"model": "statement:pnl", "scenario": scenario_key, "alias": "Test"}
        ],
    }


class TestResolverModifiers:
    def test_plain_scenario_has_empty_modifiers(self, catalogue, scenario_registry):
        plan = resolve(_make_request("budget", catalogue), catalogue, scenario_registry=scenario_registry)
        assert len(plan.data_queries) >= 1
        budget_dq = [dq for dq in plan.data_queries if "budget" in dq.scenario_key][0]
        assert budget_dq.modifiers == {}

    def test_uncommitted_modifier_propagates(self, catalogue, scenario_registry):
        plan = resolve(_make_request("budget&uncommitted", catalogue), catalogue, scenario_registry=scenario_registry)
        budget_dq = [dq for dq in plan.data_queries if "budget" in dq.scenario_key][0]
        assert budget_dq.modifiers == {"uncommitted": ""}
        assert budget_dq.scenario_id == "BUD-2026"

    def test_commit_modifier_propagates(self, catalogue, scenario_registry):
        plan = resolve(_make_request("budget&commit=ea84da45", catalogue), catalogue, scenario_registry=scenario_registry)
        budget_dq = [dq for dq in plan.data_queries if "budget" in dq.scenario_key][0]
        assert budget_dq.modifiers == {"commit": "ea84da45"}

    def test_unknown_base_scenario_raises(self, catalogue, scenario_registry):
        with pytest.raises(ResolverError, match="unknown scenario"):
            resolve(_make_request("nonexistent&uncommitted", catalogue), catalogue, scenario_registry=scenario_registry)

    def test_unknown_modifier_raises(self, catalogue, scenario_registry):
        with pytest.raises(ResolverError, match="Unknown scenario modifier"):
            resolve(_make_request("budget&bogus", catalogue), catalogue, scenario_registry=scenario_registry)

    def test_modifier_on_computed_scenario_raises(self, catalogue, scenario_registry):
        with pytest.raises(ResolverError, match="not supported on computed"):
            resolve(_make_request("actuals_vs_budget&uncommitted", catalogue), catalogue, scenario_registry=scenario_registry)

    def test_modifier_on_shifted_scenario(self, catalogue, scenario_registry):
        plan = resolve(_make_request("prior_year&uncommitted", catalogue), catalogue, scenario_registry=scenario_registry)
        py_dq = [dq for dq in plan.data_queries if "prior_year" in dq.scenario_key][0]
        assert py_dq.modifiers == {"uncommitted": ""}
        # Shifted scenario resolves to ACTUALS with shifted periods
        assert py_dq.scenario_id == "ACTUALS"

    def test_scenario_key_preserves_modifiers(self, catalogue, scenario_registry):
        """The scenario_key on the DataQuery includes modifiers for unique identity."""
        plan = resolve(_make_request("budget&uncommitted", catalogue), catalogue, scenario_registry=scenario_registry)
        budget_dq = [dq for dq in plan.data_queries if "budget" in dq.scenario_key][0]
        assert budget_dq.scenario_key == "budget&uncommitted"


# ---------------------------------------------------------------------------
# Retriever: commit-aware WHERE clause
# ---------------------------------------------------------------------------

def _make_retriever_query(modifiers: dict | None = None) -> RetrieverDataQuery:
    return RetrieverDataQuery(
        scenario_key="budget",
        scenario_id="BUD-2026",
        period_start="2026-01",
        period_end="2026-12",
        metric_keys=["revenue"],
        domain="pnl",
        modifiers=modifiers or {},
    )


class TestCommitAwareWhere:
    def test_default_excludes_uncommitted(self, catalogue, scenario_registry):
        """Default (no modifiers): committed-only."""
        dq = _make_retriever_query()
        results = generate_sql(dq, catalogue, [], None)
        sql, params = results[0]
        assert "commit_id != '__uncommitted__'" in sql

    def test_uncommitted_modifier_no_commit_filter(self, catalogue, scenario_registry):
        """&uncommitted: no commit_id filter at all."""
        dq = _make_retriever_query({"uncommitted": ""})
        results = generate_sql(dq, catalogue, [], None)
        sql, _ = results[0]
        assert "commit_id" not in sql

    def test_uncommitted_delta_modifier(self, catalogue, scenario_registry):
        """&uncommitted_delta: only uncommitted rows."""
        dq = _make_retriever_query({"uncommitted_delta": ""})
        results = generate_sql(dq, catalogue, [], None)
        sql, _ = results[0]
        assert "commit_id = '__uncommitted__'" in sql

    def test_commit_delta_modifier(self, catalogue, scenario_registry):
        """&commit_delta={id}: single commit only."""
        dq = _make_retriever_query({"commit_delta": "abc123"})
        results = generate_sql(dq, catalogue, [], None)
        sql, params = results[0]
        assert "commit_id = {mod_commit_id:String}" in sql
        assert params["mod_commit_id"] == "abc123"

    def test_commit_time_travel_modifier(self, catalogue, scenario_registry):
        """&commit={id}: all commits up to and including."""
        dq = _make_retriever_query({"commit": "abc123"})
        results = generate_sql(dq, catalogue, [], None)
        sql, params = results[0]
        assert "commit_id IN" in sql
        assert "planning.commits" in sql
        assert params["mod_target_commit"] == "abc123"

    def test_actuals_default_harmless(self, catalogue, scenario_registry):
        """Default committed-only filter is harmless for ACTUALS."""
        dq = RetrieverDataQuery(
            scenario_key="actuals",
            scenario_id="ACTUALS",
            period_start="2025-01",
            period_end="2025-12",
            metric_keys=["revenue"],
            domain="pnl",
        )
        results = generate_sql(dq, catalogue, [], None)
        sql, _ = results[0]
        # ACTUALS have commit_id = '__actuals__' in the view,
        # so this filter is harmless (never matches '__uncommitted__')
        assert "commit_id != '__uncommitted__'" in sql

    def test_aggregate_mode_also_gets_commit_filter(self, catalogue, scenario_registry):
        """Commit filter applies in aggregate mode too."""
        dq = _make_retriever_query()
        results = generate_sql(dq, catalogue, [], None)
        for sql, _ in results:
            assert "commit_id != '__uncommitted__'" in sql


# ---------------------------------------------------------------------------
# Fork modifier (7C.6) — resolver accepts fork, orchestrator resolves it
# ---------------------------------------------------------------------------

class TestForkModifier:
    def test_fork_modifier_parses(self):
        base, mods = parse_scenario_modifiers("budget&fork")
        assert base == "budget"
        assert mods == {"fork": ""}

    def test_fork_modifier_with_target(self):
        base, mods = parse_scenario_modifiers("budget_v2&fork=BUD-2026")
        assert base == "budget_v2"
        assert mods == {"fork": "BUD-2026"}

    def test_fork_modifier_propagates_to_data_query(self, catalogue, scenario_registry):
        plan = resolve(_make_request("budget&fork", catalogue), catalogue, scenario_registry=scenario_registry)
        budget_dq = [dq for dq in plan.data_queries if "budget" in dq.scenario_key][0]
        assert budget_dq.modifiers == {"fork": ""}
        # scenario_id is still original at resolve time — orchestrator resolves it
        assert budget_dq.scenario_id == "BUD-2026"

    def test_fork_with_explicit_target_propagates(self, catalogue, scenario_registry):
        plan = resolve(_make_request("budget&fork=FC-2026-Q1", catalogue), catalogue, scenario_registry=scenario_registry)
        budget_dq = [dq for dq in plan.data_queries if "budget" in dq.scenario_key][0]
        assert budget_dq.modifiers == {"fork": "FC-2026-Q1"}

    def test_fork_default_committed_only(self, catalogue, scenario_registry):
        """Fork modifier should still apply default committed-only filter."""
        dq = _make_retriever_query({"fork": ""})
        results = generate_sql(dq, catalogue, [], None)
        sql, _ = results[0]
        assert "commit_id != '__uncommitted__'" in sql
