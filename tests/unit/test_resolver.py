# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Tests for precis_mcp/engine/resolver.py"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from precis_mcp.engine.period_codes import Grain
from precis_mcp.engine.resolver import (
    ExecutionPlan,
    GrainSpec,
    ResolvedBlock,
    ResolverError,
    resolve,
    shift_period,
    shift_year,
    step_period,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_request(
    blocks: list[dict],
    period_start: str = "2025-01",
    period_end: str = "2025-12",
    dimensions: list[str] | None = None,
) -> dict:
    context: dict = {"period_start": period_start, "period_end": period_end}
    req: dict = {"context": context, "blocks": blocks}
    if dimensions is not None:
        req["dimensions"] = dimensions
    return req


# ---------------------------------------------------------------------------
# 0. grains request → ExecutionPlan.grains (GrainSpec)
# ---------------------------------------------------------------------------

class TestGrainsSpec:
    def _req(self, grains: dict | None = None) -> dict:
        req = _make_request([{"model": "statement:pnl", "scenario": "actuals"}])
        if grains is not None:
            req["grains"] = grains
        return req

    def test_default_detail_only_when_absent(self, catalogue, scenario_registry):
        plan = resolve(self._req(), catalogue, scenario_registry=scenario_registry)
        assert plan.grains == GrainSpec()
        assert (plan.grains.detail, plan.grains.subtotals, plan.grains.grand_total) == (True, False, False)

    def test_empty_grains_dict_defaults_detail_only(self, catalogue, scenario_registry):
        plan = resolve(self._req({}), catalogue, scenario_registry=scenario_registry)
        assert plan.grains == GrainSpec()

    def test_full_ladder_parsed(self, catalogue, scenario_registry):
        plan = resolve(
            self._req({"detail": True, "subtotals": True, "grand_total": True}),
            catalogue, scenario_registry=scenario_registry,
        )
        assert plan.grains == GrainSpec(detail=True, subtotals=True, grand_total=True)

    def test_partial_dict_fills_defaults(self, catalogue, scenario_registry):
        plan = resolve(self._req({"grand_total": True}), catalogue, scenario_registry=scenario_registry)
        assert plan.grains == GrainSpec(detail=True, subtotals=False, grand_total=True)


# ---------------------------------------------------------------------------
# 1. Simple P&L request — single block with statement:pnl, scenario actuals
# ---------------------------------------------------------------------------

class TestSimplePnlRequest:
    def test_query_mode_aggregate(self, catalogue, scenario_registry):
        req = _make_request([{"model": "statement:pnl", "scenario": "actuals"}])
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        assert plan.query_mode == "aggregate"

    def test_one_data_query(self, catalogue, scenario_registry):
        req = _make_request([{"model": "statement:pnl", "scenario": "actuals"}])
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        assert len(plan.data_queries) == 1
        assert plan.data_queries[0].scenario_id == "ACTUALS"

    def test_no_computed_evals(self, catalogue, scenario_registry):
        req = _make_request([{"model": "statement:pnl", "scenario": "actuals"}])
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        assert plan.computed_evals == []

    def test_pnl_display_items_count(self, catalogue, scenario_registry):
        """pnl statement has 11 lines (9 metrics + 2 separators)."""
        req = _make_request([{"model": "statement:pnl", "scenario": "actuals"}])
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        assert len(plan.blocks) == 1
        block = plan.blocks[0]
        assert len(block.display_items) == 11

    def test_separators_in_display_items(self, catalogue, scenario_registry):
        req = _make_request([{"model": "statement:pnl", "scenario": "actuals"}])
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        block = plan.blocks[0]
        assert block.display_items.count("separator") == 2

    def test_metric_keys_no_separator(self, catalogue, scenario_registry):
        req = _make_request([{"model": "statement:pnl", "scenario": "actuals"}])
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        block = plan.blocks[0]
        assert "separator" not in block.metric_keys

    def test_transitive_base_metrics_included(self, catalogue, scenario_registry):
        """all_base_metric_keys must include deps of derived metrics."""
        req = _make_request([{"model": "statement:pnl", "scenario": "actuals"}])
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        # gross_margin is derived from revenue + direct_cost — both must be in base keys
        assert "revenue" in plan.all_base_metric_keys
        assert "direct_cost" in plan.all_base_metric_keys
        # ebitda is derived transitively through contribution_margin, gross_margin
        assert "sga" in plan.all_base_metric_keys
        assert "indirect_cost" in plan.all_base_metric_keys


# ---------------------------------------------------------------------------
# 2. Period as dimension (monthly mode removed — always aggregate)
# ---------------------------------------------------------------------------

class TestPeriodDimension:
    def test_period_dimension_is_aggregate(self, catalogue, scenario_registry):
        req = _make_request(
            [{"model": "metric:revenue", "scenario": "actuals"}],
            dimensions=["period"],
        )
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        assert plan.query_mode == "aggregate"

    def test_dimensions_stored(self, catalogue, scenario_registry):
        req = _make_request(
            [{"model": "metric:revenue", "scenario": "actuals"}],
            dimensions=["period", "cost_centre"],
        )
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        assert plan.dimensions == ["period", "cost_centre"]

    def test_quarter_dimension_accepted(self, catalogue, scenario_registry):
        req = _make_request(
            [{"model": "metric:revenue", "scenario": "actuals"}],
            dimensions=["quarter"],
        )
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        assert plan.dimensions == ["quarter"]

    def test_fiscal_year_dimension_accepted(self, catalogue, scenario_registry):
        req = _make_request(
            [{"model": "metric:revenue", "scenario": "actuals"}],
            dimensions=["fiscal_year"],
        )
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        assert plan.dimensions == ["fiscal_year"]


# ---------------------------------------------------------------------------
# 3. Shifted scenario (prior_year)
# ---------------------------------------------------------------------------

class TestShiftedScenario:
    def test_prior_year_shifts_period(self, catalogue, scenario_registry):
        req = _make_request(
            [{"model": "metric:revenue", "scenario": "prior_year"}],
            period_start="2025-01",
            period_end="2025-12",
        )
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        assert len(plan.data_queries) == 1
        dq = plan.data_queries[0]
        assert dq.scenario_id == "ACTUALS"
        assert dq.period_start == "2024-01"
        assert dq.period_end == "2024-12"

    def test_prior_year_scenario_key(self, catalogue, scenario_registry):
        req = _make_request(
            [{"model": "metric:revenue", "scenario": "prior_year"}],
            period_start="2025-01",
            period_end="2025-12",
        )
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        assert plan.data_queries[0].scenario_key == "prior_year"

    def test_prior_year_time_offset(self, catalogue, scenario_registry):
        req = _make_request(
            [{"model": "metric:revenue", "scenario": "prior_year"}],
            period_start="2025-01",
            period_end="2025-12",
        )
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        assert plan.data_queries[0].time_offset == -12


# ---------------------------------------------------------------------------
# 4. Computed scenario (actuals_vs_budget)
# ---------------------------------------------------------------------------

class TestComputedScenario:
    def test_two_data_queries(self, catalogue, scenario_registry):
        req = _make_request([{"model": "metric:revenue", "scenario": "actuals_vs_budget"}])
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        assert len(plan.data_queries) == 2

    def test_data_queries_scenario_ids(self, catalogue, scenario_registry):
        req = _make_request([{"model": "metric:revenue", "scenario": "actuals_vs_budget"}])
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        scenario_ids = {dq.scenario_id for dq in plan.data_queries}
        assert scenario_ids == {"ACTUALS", "BUD-2026"}

    def test_one_computed_eval(self, catalogue, scenario_registry):
        req = _make_request([{"model": "metric:revenue", "scenario": "actuals_vs_budget"}])
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        assert len(plan.computed_evals) == 1

    def test_computed_eval_formula(self, catalogue, scenario_registry):
        req = _make_request([{"model": "metric:revenue", "scenario": "actuals_vs_budget"}])
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        eval_ = plan.computed_evals[0]
        assert eval_.scenario_key == "actuals_vs_budget"
        assert eval_.formula == "actuals - budget"

    def test_computed_eval_dependencies(self, catalogue, scenario_registry):
        req = _make_request([{"model": "metric:revenue", "scenario": "actuals_vs_budget"}])
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        eval_ = plan.computed_evals[0]
        assert set(eval_.dependencies) == {"actuals", "budget"}


# ---------------------------------------------------------------------------
# 5. Multi-block: actuals + budget + actuals_vs_budget + actuals_vs_budget_pct
# ---------------------------------------------------------------------------

class TestMultiBlock:
    def _make_multi_block_request(self):
        return _make_request([
            {"model": "metric:revenue", "scenario": "actuals"},
            {"model": "metric:revenue", "scenario": "budget"},
            {"model": "metric:revenue", "scenario": "actuals_vs_budget"},
            {"model": "metric:revenue", "scenario": "actuals_vs_budget_pct"},
        ])

    def test_deduplicated_data_queries(self, catalogue, scenario_registry):
        """actuals and budget should not be duplicated."""
        plan = resolve(self._make_multi_block_request(), catalogue, scenario_registry=scenario_registry)
        assert len(plan.data_queries) == 2

    def test_data_query_scenario_ids(self, catalogue, scenario_registry):
        plan = resolve(self._make_multi_block_request(), catalogue, scenario_registry=scenario_registry)
        ids = {dq.scenario_id for dq in plan.data_queries}
        assert ids == {"ACTUALS", "BUD-2026"}

    def test_two_computed_evals(self, catalogue, scenario_registry):
        plan = resolve(self._make_multi_block_request(), catalogue, scenario_registry=scenario_registry)
        assert len(plan.computed_evals) == 2

    def test_computed_eval_order(self, catalogue, scenario_registry):
        """Neither actuals_vs_budget nor actuals_vs_budget_pct depends on the other,
        so both should be present — order does not matter for this test."""
        plan = resolve(self._make_multi_block_request(), catalogue, scenario_registry=scenario_registry)
        keys = [e.scenario_key for e in plan.computed_evals]
        assert set(keys) == {"actuals_vs_budget", "actuals_vs_budget_pct"}

    def test_four_blocks(self, catalogue, scenario_registry):
        plan = resolve(self._make_multi_block_request(), catalogue, scenario_registry=scenario_registry)
        assert len(plan.blocks) == 4


# ---------------------------------------------------------------------------
# 6. Metric dependencies — ebitda_margin_pct transitive deps
# ---------------------------------------------------------------------------

class TestMetricDependencies:
    def test_ebitda_margin_pct_base_deps(self, catalogue, scenario_registry):
        req = _make_request([{"model": "metric:ebitda_margin_pct", "scenario": "actuals"}])
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        base = set(plan.all_base_metric_keys)
        # ebitda_margin_pct = ebitda / revenue * 100
        # ebitda = contribution_margin + sga
        # contribution_margin = gross_margin + indirect_cost
        # gross_margin = revenue + direct_cost
        assert "revenue" in base
        assert "direct_cost" in base
        assert "indirect_cost" in base
        assert "sga" in base

    def test_ebitda_margin_pct_derived_includes_intermediaries(self, catalogue, scenario_registry):
        req = _make_request([{"model": "metric:ebitda_margin_pct", "scenario": "actuals"}])
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        derived = set(plan.all_derived_metric_keys)
        assert "ebitda_margin_pct" in derived
        assert "ebitda" in derived
        assert "contribution_margin" in derived
        assert "gross_margin" in derived


# ---------------------------------------------------------------------------
# 7. Single metric model
# ---------------------------------------------------------------------------

class TestSingleMetricModel:
    def test_one_metric_key(self, catalogue, scenario_registry):
        req = _make_request([{"model": "metric:revenue", "scenario": "actuals"}])
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        block = plan.blocks[0]
        assert block.metric_keys == ["revenue"]

    def test_no_separators(self, catalogue, scenario_registry):
        req = _make_request([{"model": "metric:revenue", "scenario": "actuals"}])
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        block = plan.blocks[0]
        assert "separator" not in block.display_items
        assert len(block.display_items) == 1


# ---------------------------------------------------------------------------
# 7b. metrics: multi-metric model
# ---------------------------------------------------------------------------

class TestMultiMetricModel:
    def test_two_metrics_resolved(self, catalogue, scenario_registry):
        req = _make_request(
            [{"model": "metrics:revenue,billable_hours", "scenario": "actuals"}]
        )
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        block = plan.blocks[0]
        assert block.metric_keys == ["revenue", "billable_hours"]
        assert block.display_items == ["revenue", "billable_hours"]

    def test_preserves_order(self, catalogue, scenario_registry):
        req = _make_request(
            [{"model": "metrics:billable_hours,revenue", "scenario": "actuals"}]
        )
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        block = plan.blocks[0]
        assert block.display_items == ["billable_hours", "revenue"]

    def test_whitespace_tolerated(self, catalogue, scenario_registry):
        req = _make_request(
            [{"model": "metrics: revenue , billable_hours ", "scenario": "actuals"}]
        )
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        block = plan.blocks[0]
        assert block.metric_keys == ["revenue", "billable_hours"]

    def test_single_metric_via_metrics_ref(self, catalogue, scenario_registry):
        # 'metrics:foo' with a single key is equivalent to 'metric:foo'.
        req = _make_request([{"model": "metrics:revenue", "scenario": "actuals"}])
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        block = plan.blocks[0]
        assert block.metric_keys == ["revenue"]
        assert block.display_items == ["revenue"]

    def test_no_separators(self, catalogue, scenario_registry):
        req = _make_request(
            [{"model": "metrics:revenue,billable_hours", "scenario": "actuals"}]
        )
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        block = plan.blocks[0]
        assert "separator" not in block.display_items

    def test_derived_metric_supported(self, catalogue, scenario_registry):
        # realised_rate is a derived ratio; the resolver must accept it the same
        # way it does inside statement: blocks.
        req = _make_request(
            [{"model": "metrics:billable_hours,realised_rate", "scenario": "actuals"}]
        )
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        block = plan.blocks[0]
        assert "realised_rate" in block.metric_keys

    def test_empty_metrics_ref_errors(self, catalogue, scenario_registry):
        req = _make_request([{"model": "metrics:", "scenario": "actuals"}])
        with pytest.raises(ResolverError, match="at least one metric key"):
            resolve(req, catalogue, scenario_registry=scenario_registry)

    def test_whitespace_only_metrics_ref_errors(self, catalogue, scenario_registry):
        req = _make_request([{"model": "metrics:   ,  ", "scenario": "actuals"}])
        with pytest.raises(ResolverError, match="at least one metric key"):
            resolve(req, catalogue, scenario_registry=scenario_registry)

    def test_unknown_metric_errors(self, catalogue, scenario_registry):
        req = _make_request(
            [{"model": "metrics:revenue,not_a_real_metric", "scenario": "actuals"}]
        )
        with pytest.raises(ResolverError, match="unknown metric key 'not_a_real_metric'"):
            resolve(req, catalogue, scenario_registry=scenario_registry)


# ---------------------------------------------------------------------------
# 8. shift_period function
# ---------------------------------------------------------------------------

class TestShiftPeriod:
    def test_shift_minus_12(self):
        assert shift_period("2025-06", -12) == "2024-06"

    def test_shift_minus_1_year_boundary(self):
        assert shift_period("2025-01", -1) == "2024-12"

    def test_shift_plus_1_year_boundary(self):
        assert shift_period("2024-12", 1) == "2025-01"

    def test_shift_minus_24(self):
        assert shift_period("2025-06", -24) == "2023-06"

    def test_shift_zero(self):
        assert shift_period("2025-06", 0) == "2025-06"

    def test_shift_plus_12(self):
        assert shift_period("2024-03", 12) == "2025-03"

    def test_shift_minus_6(self):
        assert shift_period("2025-03", -6) == "2024-09"


# ---------------------------------------------------------------------------
# Grain-aware shift primitives
# ---------------------------------------------------------------------------

class TestGrainShift:
    @pytest.mark.parametrize(
        "code, expected",
        [("2025-06", "2024-06"), ("2025-Q2", "2024-Q2"),
         ("2025", "2024"), ("2025-06-14", "2024-06-14"), ("2025-W37", "2024-W37")],
    )
    def test_shift_year_is_grain_preserving(self, code, expected):
        assert shift_year(code, -1) == expected

    def test_step_period_month_matches_shift_period(self):
        assert step_period("2025-01", Grain.MONTH, -1) == "2024-12"
        assert step_period("2025-06", Grain.MONTH, -1) == "2025-05"

    def test_step_period_quarter_year_borrow(self):
        assert step_period("2025-Q1", Grain.QUARTER, -1) == "2024-Q4"
        assert step_period("2025-Q3", Grain.QUARTER, -1) == "2025-Q2"

    def test_step_period_year(self):
        assert step_period("2025", Grain.YEAR, -1) == "2024"

    def test_step_period_irregular_grain_raises(self):
        # Week/date use calendar-based resolution, not this arithmetic helper.
        with pytest.raises(ResolverError, match="calendar-based resolution"):
            step_period("2025-W01", Grain.WEEK, -1)


# ---------------------------------------------------------------------------
# 9. Validation errors
# ---------------------------------------------------------------------------

class TestValidationErrors:
    def test_missing_period_start(self, catalogue, scenario_registry):
        req = {"context": {"period_end": "2025-12"}, "blocks": [{"model": "metric:revenue", "scenario": "actuals"}]}
        with pytest.raises(ResolverError, match="period_start"):
            resolve(req, catalogue, scenario_registry=scenario_registry)

    def test_missing_context(self, catalogue, scenario_registry):
        req = {"blocks": [{"model": "metric:revenue", "scenario": "actuals"}]}
        with pytest.raises(ResolverError, match="context"):
            resolve(req, catalogue, scenario_registry=scenario_registry)

    def test_legacy_filters_format_still_works(self, catalogue, scenario_registry):
        """Old-format requests with period in filters should still work."""
        req = {
            "filters": {"period_start": "2025-01", "period_end": "2025-12"},
            "blocks": [{"model": "metric:revenue", "scenario": "actuals"}],
        }
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        assert plan.period_start == "2025-01"

    def test_empty_blocks_list(self, catalogue, scenario_registry):
        req = _make_request([])
        with pytest.raises(ResolverError, match="at least one block"):
            resolve(req, catalogue, scenario_registry=scenario_registry)

    def test_unknown_metric_key(self, catalogue, scenario_registry):
        req = _make_request([{"model": "metric:nonexistent_metric", "scenario": "actuals"}])
        with pytest.raises(ResolverError, match="unknown metric key"):
            resolve(req, catalogue, scenario_registry=scenario_registry)

    def test_unknown_scenario(self, catalogue, scenario_registry):
        req = _make_request([{"model": "metric:revenue", "scenario": "ghost_scenario"}])
        with pytest.raises(ResolverError, match="unknown scenario"):
            resolve(req, catalogue, scenario_registry=scenario_registry)

    def test_unknown_statement(self, catalogue, scenario_registry):
        req = _make_request([{"model": "statement:ghost_statement", "scenario": "actuals"}])
        with pytest.raises(ResolverError, match="unknown statement"):
            resolve(req, catalogue, scenario_registry=scenario_registry)

    def test_mixed_grain_bounds_rejected(self, catalogue, scenario_registry):
        # A year-grain start with a month-grain end is inconsistent. (Pre
        # arbitrary-grain this was an "Invalid period format" error because a
        # bare year was unrecognised; now the year is a valid grain and the
        # mismatch is caught by the same-grain check.)
        req = _make_request(
            [{"model": "metric:revenue", "scenario": "actuals"}],
            period_start="2025",
            period_end="2025-12",
        )
        with pytest.raises(ResolverError, match="same grain"):
            resolve(req, catalogue, scenario_registry=scenario_registry)

    def test_invalid_period_format_bad_month(self, catalogue, scenario_registry):
        req = _make_request(
            [{"model": "metric:revenue", "scenario": "actuals"}],
            period_start="2025-1",
            period_end="2025-12",
        )
        with pytest.raises(ResolverError, match="Invalid period format"):
            resolve(req, catalogue, scenario_registry=scenario_registry)

    def test_period_start_after_period_end(self, catalogue, scenario_registry):
        req = _make_request(
            [{"model": "metric:revenue", "scenario": "actuals"}],
            period_start="2025-12",
            period_end="2025-01",
        )
        with pytest.raises(ResolverError, match="period_start"):
            resolve(req, catalogue, scenario_registry=scenario_registry)


# ---------------------------------------------------------------------------
# Period filter grain
# ---------------------------------------------------------------------------

class TestPeriodFilterGrain:
    def _resolve(self, catalogue, scenario_registry, *, start, end,
                 model="metric:revenue", scenario="actuals"):
        req = _make_request(
            [{"model": model, "scenario": scenario}],
            period_start=start, period_end=end,
        )
        return resolve(req, catalogue, scenario_registry=scenario_registry)

    def test_month_targets_period_column(self, catalogue, scenario_registry):
        plan = self._resolve(catalogue, scenario_registry, start="2025-01", end="2025-12")
        assert plan.data_queries[0].period_column == "period"

    def test_quarter_targets_quarter_column(self, catalogue, scenario_registry):
        plan = self._resolve(catalogue, scenario_registry, start="2025-Q1", end="2025-Q4")
        dq = plan.data_queries[0]
        assert dq.period_column == "quarter"
        assert (dq.period_start, dq.period_end) == ("2025-Q1", "2025-Q4")

    def test_fiscal_year_targets_fiscal_year_column(self, catalogue, scenario_registry):
        plan = self._resolve(catalogue, scenario_registry, start="2024", end="2025")
        assert plan.data_queries[0].period_column == "fiscal_year"

    def test_unsupported_grain_errors(self, catalogue, scenario_registry):
        # pnl (revenue) declares no week column, so week is unsupported there.
        with pytest.raises(ResolverError, match="not supported"):
            self._resolve(catalogue, scenario_registry, start="2025-W01", end="2025-W52")

    def test_week_resolves_for_domain_with_week_column(self, catalogue, scenario_registry):
        # timesheets declares a week cube dimension, so week is filterable there.
        plan = self._resolve(
            catalogue, scenario_registry,
            start="2025-W01", end="2025-W10", model="metric:hours_worked",
        )
        assert plan.data_queries[0].period_column == "week"

    def test_day_resolves_for_domain_with_day_column(self, catalogue, scenario_registry):
        plan = self._resolve(
            catalogue, scenario_registry,
            start="2025-01-06", end="2025-01-10", model="metric:hours_worked",
        )
        assert plan.data_queries[0].period_column == "day"

    def test_prior_period_at_week_uses_calendar_seq(self, catalogue, scenario_registry):
        # Irregular grain: the window is NOT shifted; the retriever resolves the
        # predecessor of each week via the calendar seq.
        plan = self._resolve(
            catalogue, scenario_registry,
            start="2025-W02", end="2025-W05",
            model="metric:hours_worked", scenario="prior_period",
        )
        dq = plan.data_queries[0]
        assert dq.period_column == "week"
        assert dq.calendar_table == "semantic.dim_week"
        assert dq.calendar_key == "week"
        assert (dq.period_start, dq.period_end) == ("2025-W02", "2025-W05")
        assert dq.time_offset == 0

    def test_calendar_pp_not_deduped_with_actuals(self, catalogue, scenario_registry):
        # Regression: a calendar-shift PP keeps the original window and its base
        # scenario_id, so its dedup key would collide with the actuals query —
        # dropping the PP seq-join and blanking the PP column in a multi-scenario
        # report. The calendar must be part of the dedup key so both survive.
        req = _make_request(
            [{"model": "metric:hours_worked", "scenario": "actuals"},
             {"model": "metric:hours_worked", "scenario": "prior_period"}],
            period_start="2025-W01", period_end="2025-W10", dimensions=["week"],
        )
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        by_key = {dq.scenario_key: dq for dq in plan.data_queries}
        assert "actuals" in by_key and "prior_period" in by_key
        assert by_key["prior_period"].calendar_table == "semantic.dim_week"
        assert by_key["actuals"].calendar_table == ""

    @pytest.mark.parametrize("scenario", ["prior_period", "prior_year"])
    def test_guard_shift_with_finer_breakdown_than_filter(self, catalogue, scenario_registry, scenario):
        # Filter month, break by week + a shift → refuse because the shift is
        # resolved at the filter grain and would misalign the comparison.
        req = _make_request(
            [{"model": "metric:hours_worked", "scenario": scenario}],
            period_start="2026-04", period_end="2026-06", dimensions=["week"],
        )
        with pytest.raises(ResolverError, match="needs the period filter at 'week'"):
            resolve(req, catalogue, scenario_registry=scenario_registry)

    def test_guard_allows_same_grain_breakdown(self, catalogue, scenario_registry):
        # filter week, break week + PP → fine (calendar shift, aligned).
        req = _make_request(
            [{"model": "metric:hours_worked", "scenario": "prior_period"}],
            period_start="2026-W14", period_end="2026-W22", dimensions=["week"],
        )
        assert resolve(req, catalogue, scenario_registry=scenario_registry).data_queries

    def test_guard_allows_coarser_breakdown(self, catalogue, scenario_registry):
        # filter month, break quarter + PY → fine (coarser; historical behaviour).
        req = _make_request(
            [{"model": "metric:revenue", "scenario": "prior_year"}],
            period_start="2026-04", period_end="2026-06", dimensions=["quarter"],
        )
        assert resolve(req, catalogue, scenario_registry=scenario_registry).data_queries

    def test_guard_allows_aggregate_shift(self, catalogue, scenario_registry):
        # filter month, no time breakdown + PP → fine (aggregate over prior period).
        req = _make_request(
            [{"model": "metric:revenue", "scenario": "prior_period"}],
            period_start="2026-04", period_end="2026-06",
        )
        assert resolve(req, catalogue, scenario_registry=scenario_registry).data_queries

    def test_prior_period_at_day_uses_calendar_seq(self, catalogue, scenario_registry):
        plan = self._resolve(
            catalogue, scenario_registry,
            start="2025-01-06", end="2025-01-10",
            model="metric:hours_worked", scenario="prior_period",
        )
        dq = plan.data_queries[0]
        assert dq.calendar_table == "semantic.dim_date"
        assert dq.calendar_key == "day"

    def test_prior_year_at_week_shifts_year_token(self, catalogue, scenario_registry):
        # Prior year is closed-form at every grain (year-token decrement) — no
        # calendar needed even for week.
        plan = self._resolve(
            catalogue, scenario_registry,
            start="2025-W37", end="2025-W40",
            model="metric:hours_worked", scenario="prior_year",
        )
        dq = plan.data_queries[0]
        assert (dq.period_start, dq.period_end) == ("2024-W37", "2024-W40")
        assert dq.calendar_table == ""
        assert dq.time_offset == -12

    # --- edge cases (hardening) -------------------------------------------

    def test_prior_year_week_53_no_existence_check(self, catalogue, scenario_registry):
        # PY of W53 yields 2024-W53 even if the prior year has no W53 — no clamp,
        # no error; the query returns empty rows, not a crash.
        plan = self._resolve(
            catalogue, scenario_registry, start="2025-W53", end="2025-W53",
            model="metric:hours_worked", scenario="prior_year",
        )
        assert plan.data_queries[0].period_start == "2024-W53"

    def test_prior_year_leap_day_no_existence_check(self, catalogue, scenario_registry):
        # 2028-02-29 (leap) PY -> 2027-02-29, which does not exist; still no clamp.
        plan = self._resolve(
            catalogue, scenario_registry, start="2028-02-29", end="2028-02-29",
            model="metric:hours_worked", scenario="prior_year",
        )
        assert plan.data_queries[0].period_start == "2027-02-29"

    def test_prior_period_week_boundary_defers_to_calendar(self, catalogue, scenario_registry):
        # PP of W01 crosses the year boundary (no closed form) — the resolver
        # keeps the window and carries the calendar for the seq lookup.
        plan = self._resolve(
            catalogue, scenario_registry, start="2025-W01", end="2025-W01",
            model="metric:hours_worked", scenario="prior_period",
        )
        dq = plan.data_queries[0]
        assert dq.calendar_table == "semantic.dim_week"
        assert (dq.period_start, dq.period_end) == ("2025-W01", "2025-W01")

    @pytest.mark.parametrize("start, end", [
        ("2025-W01", "2025-03"),     # week vs month
        ("2025-01-01", "2025-W05"),  # day vs week
        ("2025-Q1", "2025"),         # quarter vs year
        ("2025-06", "2025-06-30"),   # month vs day
    ])
    def test_mixed_grain_bounds_rejected_matrix(self, catalogue, scenario_registry, start, end):
        with pytest.raises(ResolverError, match="same grain"):
            self._resolve(catalogue, scenario_registry, start=start, end=end,
                          model="metric:hours_worked")

    @pytest.mark.parametrize("start, end", [
        ("2025-06-14", "2025-06-20"),  # day — pnl has no day column
        ("2025-W01", "2025-W05"),      # week — pnl has no week column
    ])
    def test_unsupported_grain_on_pnl_rejected(self, catalogue, scenario_registry, start, end):
        with pytest.raises(ResolverError, match="not supported"):
            self._resolve(catalogue, scenario_registry, start=start, end=end,
                          model="metric:revenue")

    def test_non_month_closing_resolves(self, catalogue, scenario_registry):
        # Closing/avg are keyed off the domain's native grain, so a closing
        # metric filtered by a non-month grain resolves instead of erroring.
        plan = self._resolve(
            catalogue, scenario_registry,
            start="2025-Q1", end="2025-Q4",
            model="metric:closing_fte_billable",
        )
        assert plan.data_queries[0].period_column == "quarter"

    def test_prior_year_at_quarter(self, catalogue, scenario_registry):
        plan = self._resolve(
            catalogue, scenario_registry,
            start="2025-Q1", end="2025-Q4", scenario="prior_year",
        )
        dq = plan.data_queries[0]
        assert (dq.period_start, dq.period_end) == ("2024-Q1", "2024-Q4")
        assert dq.period_column == "quarter"
        assert dq.time_offset == -12

    def test_prior_period_at_quarter(self, catalogue, scenario_registry):
        plan = self._resolve(
            catalogue, scenario_registry,
            start="2025-Q1", end="2025-Q4", scenario="prior_period",
        )
        dq = plan.data_queries[0]
        # PP steps the window back one quarter (with year-borrow at Q1).
        assert (dq.period_start, dq.period_end) == ("2024-Q4", "2025-Q3")
        assert dq.time_offset == -3  # one quarter, month-equivalent

    def test_prior_year_at_fiscal_year(self, catalogue, scenario_registry):
        plan = self._resolve(
            catalogue, scenario_registry,
            start="2025", end="2025", scenario="prior_year",
        )
        dq = plan.data_queries[0]
        assert (dq.period_start, dq.period_end) == ("2024", "2024")
        assert dq.period_column == "fiscal_year"
        assert dq.time_offset == -12


# ---------------------------------------------------------------------------
# 10. Chained shifted scenario (budget_py: shifted from budget, offset -12)
# ---------------------------------------------------------------------------

class TestChainedShiftedScenario:
    def test_budget_py_scenario_id(self, catalogue, scenario_registry):
        """budget_py: base=budget (data, BUD-2026), time_offset=-12"""
        req = _make_request(
            [{"model": "metric:revenue", "scenario": "budget_py"}],
            period_start="2025-01",
            period_end="2025-12",
        )
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        assert len(plan.data_queries) == 1
        dq = plan.data_queries[0]
        assert dq.scenario_id == "BUD-2026"
        assert dq.period_start == "2024-01"
        assert dq.period_end == "2024-12"

    def test_budget_py_scenario_key(self, catalogue, scenario_registry):
        req = _make_request(
            [{"model": "metric:revenue", "scenario": "budget_py"}],
            period_start="2025-01",
            period_end="2025-12",
        )
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        assert plan.data_queries[0].scenario_key == "budget_py"


# ---------------------------------------------------------------------------
# 11. Additional plan fields
# ---------------------------------------------------------------------------

class TestExecutionPlanFields:
    def test_period_start_end_stored(self, catalogue, scenario_registry):
        req = _make_request(
            [{"model": "metric:revenue", "scenario": "actuals"}],
            period_start="2025-03",
            period_end="2025-09",
        )
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        assert plan.period_start == "2025-03"
        assert plan.period_end == "2025-09"

    def test_block_alias_default(self, catalogue, scenario_registry):
        req = _make_request([{"model": "metric:revenue", "scenario": "actuals"}])
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        assert plan.blocks[0].alias == "actuals"

    def test_block_alias_custom(self, catalogue, scenario_registry):
        req = _make_request([{"model": "metric:revenue", "scenario": "actuals", "alias": "Actuals YTD"}])
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        assert plan.blocks[0].alias == "Actuals YTD"

    def test_base_metrics_in_data_query(self, catalogue, scenario_registry):
        """DataQuery.metric_keys should contain only base metric keys."""
        req = _make_request([{"model": "metric:gross_margin", "scenario": "actuals"}])
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        dq = plan.data_queries[0]
        # gross_margin is derived; its deps (revenue, direct_cost) must be in the query
        assert "revenue" in dq.metric_keys
        assert "direct_cost" in dq.metric_keys
        # gross_margin itself is derived — should not be in base query
        assert "gross_margin" not in dq.metric_keys


# ---------------------------------------------------------------------------
# 12. actuals_vs_prior_year — computed depending on actuals + prior_year (shifted)
# ---------------------------------------------------------------------------

class TestPyVarianceComputed:
    def test_actuals_vs_prior_year_data_queries(self, catalogue, scenario_registry):
        """actuals_vs_prior_year depends on actuals (data) + prior_year (shifted from actuals -12)."""
        req = _make_request(
            [{"model": "metric:revenue", "scenario": "actuals_vs_prior_year"}],
            period_start="2025-01",
            period_end="2025-12",
        )
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        # Two data queries: ACTUALS 2025 and ACTUALS 2024
        assert len(plan.data_queries) == 2
        ids = {dq.scenario_id for dq in plan.data_queries}
        assert ids == {"ACTUALS"}
        # One for 2025, one for 2024
        starts = {dq.period_start for dq in plan.data_queries}
        assert "2025-01" in starts
        assert "2024-01" in starts

    def test_actuals_vs_prior_year_computed_eval(self, catalogue, scenario_registry):
        req = _make_request(
            [{"model": "metric:revenue", "scenario": "actuals_vs_prior_year"}],
            period_start="2025-01",
            period_end="2025-12",
        )
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        assert len(plan.computed_evals) == 1
        assert plan.computed_evals[0].scenario_key == "actuals_vs_prior_year"
        assert set(plan.computed_evals[0].dependencies) == {"actuals", "actuals_py"}


# ---------------------------------------------------------------------------
# 14. Domain routing
# ---------------------------------------------------------------------------

class TestDomainRouting:
    def test_pnl_metric_gets_pnl_domain(self, catalogue, scenario_registry):
        req = _make_request(
            [{"model": "metric:revenue", "scenario": "actuals"}],
        )
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        assert all(dq.domain == "pnl" for dq in plan.data_queries)

    def test_gl_metric_gets_gl_domain(self, catalogue, scenario_registry):
        req = _make_request(
            [{"model": "metric:gl_amount", "scenario": "actuals"}],
        )
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        assert all(dq.domain == "gl" for dq in plan.data_queries)

    def test_timesheets_metric_gets_timesheets_domain(self, catalogue, scenario_registry):
        req = _make_request(
            [{"model": "metric:hours_worked", "scenario": "actuals"}],
        )
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        assert all(dq.domain == "timesheets" for dq in plan.data_queries)

    def test_derived_timesheets_metric_infers_domain(self, catalogue, scenario_registry):
        req = _make_request(
            [{"model": "metric:ts_utilisation_rate", "scenario": "actuals"}],
        )
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        # Derived metrics don't have domain, but their base dependencies do
        assert all(dq.domain == "timesheets" for dq in plan.data_queries)


# ---------------------------------------------------------------------------
# 15. Dimension-domain validation
# ---------------------------------------------------------------------------

class TestDimensionDomainValidation:
    """Pre-flight validation: dimensions must exist in the inferred domain."""

    def test_valid_dimension_for_pnl(self, catalogue, scenario_registry):
        """cost_centre exists in pnl domain — should pass."""
        req = _make_request(
            [{"model": "metric:revenue", "scenario": "actuals"}],
            dimensions=["cost_centre"],
        )
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        assert plan.dimensions == ["cost_centre"]

    def test_valid_dimension_for_timesheets(self, catalogue, scenario_registry):
        """project exists in timesheets domain — should pass."""
        req = _make_request(
            [{"model": "metric:hours_billable", "scenario": "actuals"}],
            dimensions=["project"],
        )
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        assert plan.dimensions == ["project"]

    def test_invalid_dimension_for_pnl_strict(self, catalogue, scenario_registry):
        """project does NOT exist in pnl domain — should raise with hint."""
        req = _make_request(
            [{"model": "metric:revenue", "scenario": "actuals"}],
            dimensions=["project"],
        )
        with pytest.raises(ResolverError, match="project.*not available.*pnl"):
            resolve(req, catalogue, scenario_registry=scenario_registry)

    def test_invalid_dimension_error_suggests_alternatives(self, catalogue, scenario_registry):
        """Error message should mention which domains have the dimension."""
        req = _make_request(
            [{"model": "metric:revenue", "scenario": "actuals"}],
            dimensions=["project"],
        )
        with pytest.raises(ResolverError, match="timesheets"):
            resolve(req, catalogue, scenario_registry=scenario_registry)

    def test_invalid_dimension_for_pnl_employee(self, catalogue, scenario_registry):
        """employee does NOT exist in pnl domain."""
        req = _make_request(
            [{"model": "metric:revenue", "scenario": "actuals"}],
            dimensions=["employee"],
        )
        with pytest.raises(ResolverError, match="employee.*not available.*pnl"):
            resolve(req, catalogue, scenario_registry=scenario_registry)

    def test_period_dimension_always_valid(self, catalogue, scenario_registry):
        """period is a catalogue dimension — valid for any domain that declares it."""
        req = _make_request(
            [{"model": "metric:revenue", "scenario": "actuals"}],
            dimensions=["period"],
        )
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        assert plan.dimensions == ["period"]

    def test_strict_dimensions_false_allows_invalid(self, catalogue, scenario_registry):
        """When _strict_dimensions=False (statements), invalid dims are allowed."""
        req = _make_request(
            [{"model": "metric:revenue", "scenario": "actuals"}],
            dimensions=["project"],
        )
        req["_strict_dimensions"] = False
        # Should NOT raise
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        assert "project" in plan.dimensions

    def test_no_dimensions_no_validation(self, catalogue, scenario_registry):
        """No dimensions requested — no validation needed."""
        req = _make_request(
            [{"model": "metric:revenue", "scenario": "actuals"}],
        )
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        assert plan.dimensions == []

    def test_gl_domain_account_dimension(self, catalogue, scenario_registry):
        """account exists in gl domain — should pass."""
        req = _make_request(
            [{"model": "metric:gl_amount", "scenario": "actuals"}],
            dimensions=["account"],
        )
        plan = resolve(req, catalogue, scenario_registry=scenario_registry)
        assert plan.dimensions == ["account"]

    def test_gl_domain_rejects_project_dimension(self, catalogue, scenario_registry):
        """project does NOT exist in gl domain."""
        req = _make_request(
            [{"model": "metric:gl_amount", "scenario": "actuals"}],
            dimensions=["project"],
        )
        with pytest.raises(ResolverError, match="project.*not available.*gl"):
            resolve(req, catalogue, scenario_registry=scenario_registry)


# ---------------------------------------------------------------------------
# Model-kind classification — is_statement reflects the model type, not the
# metric count. A multi-metric breakdown must NOT be misclassified as a
# statement (which would route it to the statement crosstab instead of the
# metric-columns layout).
# ---------------------------------------------------------------------------

class TestModelKindClassification:
    def test_statement_model_is_statement(self, catalogue, scenario_registry):
        plan = resolve(
            _make_request([{"model": "statement:pnl", "scenario": "actuals"}]),
            catalogue, scenario_registry=scenario_registry,
        )
        assert plan.blocks[0].is_statement is True

    def test_single_metric_model_is_not_statement(self, catalogue, scenario_registry):
        plan = resolve(
            _make_request([{"model": "metric:revenue", "scenario": "actuals"}]),
            catalogue, scenario_registry=scenario_registry,
        )
        assert plan.blocks[0].is_statement is False

    def test_multi_metric_model_is_not_statement(self, catalogue, scenario_registry):
        # Regression: the old `len(metric_keys) > 1` heuristic flagged this True.
        plan = resolve(
            _make_request([{"model": "metrics:revenue,billable_hours", "scenario": "actuals"}]),
            catalogue, scenario_registry=scenario_registry,
        )
        assert plan.blocks[0].is_statement is False
