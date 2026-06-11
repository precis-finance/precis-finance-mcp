# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
from __future__ import annotations

import os
import pytest
from dataclasses import dataclass

from precis_mcp.engine.transformer import (
    ExpressionError,
    evaluate_expression,
    compute_derived_metrics,
    compute_scenario,
    transform,
)

# ---------------------------------------------------------------------------
# Mock ComputedScenarioEval (avoid importing resolver)
# ---------------------------------------------------------------------------

@dataclass
class ComputedScenarioEval:
    scenario_key: str
    formula: str
    dependencies: list[str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_metrics():
    """Return a dict of base metric values for one dimension key.

    Costs are positive (as stored in the DB). Derived metric formulas subtract them:
    gross_margin = revenue - direct_cost, etc.
    """
    return {
        "revenue": 1_000_000.0,
        "direct_cost": 600_000.0,
        "indirect_cost": 100_000.0,
        "sga": 50_000.0,
        "billable_hours": 15_000.0,
        "total_hours": 20_000.0,
        "avg_fte_billable": 50.0,
        "avg_fte_overhead": 20.0,
        "closing_fte_billable": 52.0,
        "closing_fte_overhead": 21.0,
    }


# ---------------------------------------------------------------------------
# Test 1: evaluate_expression — basic arithmetic
# ---------------------------------------------------------------------------

def test_evaluate_basic_add():
    assert evaluate_expression("a + b", {"a": 10, "b": 20}) == 30.0


def test_evaluate_basic_sub():
    assert evaluate_expression("a - b", {"a": 100, "b": 40}) == 60.0


def test_evaluate_basic_mul():
    assert evaluate_expression("a * b", {"a": 5, "b": 3}) == 15.0


def test_evaluate_basic_div():
    assert evaluate_expression("a / b", {"a": 100, "b": 4}) == 25.0


# ---------------------------------------------------------------------------
# Test 2: evaluate_expression — division by zero
# ---------------------------------------------------------------------------

def test_evaluate_div_by_zero():
    assert evaluate_expression("a / b", {"a": 100, "b": 0}) is None


# ---------------------------------------------------------------------------
# Test 3: evaluate_expression — None propagation
# ---------------------------------------------------------------------------

def test_evaluate_none_propagation_add():
    assert evaluate_expression("a + b", {"a": 10, "b": None}) is None


def test_evaluate_none_propagation_mul():
    assert evaluate_expression("a * b", {"a": None, "b": 5}) is None


# ---------------------------------------------------------------------------
# Test 4: evaluate_expression — abs()
# ---------------------------------------------------------------------------

def test_evaluate_abs_complex():
    # (90 - 100) / abs(100) * 100 = -10 / 100 * 100 = -10.0
    # abs() strips the sign of the denominator so the sign of the ratio
    # reflects only the numerator sign, not the denominator sign.
    result = evaluate_expression("(a - b) / abs(b) * 100", {"a": 90, "b": 100})
    assert result == pytest.approx(-10.0)


def test_evaluate_abs_simple():
    result = evaluate_expression("abs(a)", {"a": -50})
    assert result == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# Test 5: evaluate_expression — complex formula
# ---------------------------------------------------------------------------

def test_evaluate_complex_formula():
    result = evaluate_expression(
        "gross_margin / revenue * 100",
        {"gross_margin": 45, "revenue": 100},
    )
    assert result == pytest.approx(45.0)


# ---------------------------------------------------------------------------
# Test 6: evaluate_expression — parentheses
# ---------------------------------------------------------------------------

def test_evaluate_parentheses():
    result = evaluate_expression("(a + b) * c", {"a": 2, "b": 3, "c": 4})
    assert result == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# Test 6b: evaluate_expression — IfExp (ternary conditional)
# ---------------------------------------------------------------------------

def test_evaluate_ifexp_truthy():
    """IfExp returns body when condition is non-zero."""
    result = evaluate_expression(
        "a / b * 100 if b else 0",
        {"a": 45, "b": 100},
    )
    assert result == pytest.approx(45.0)


def test_evaluate_ifexp_zero_guard():
    """IfExp returns orelse when condition is zero (division guard)."""
    result = evaluate_expression(
        "a / b * 100 if b else 0",
        {"a": 45, "b": 0},
    )
    assert result == pytest.approx(0.0)


def test_evaluate_ifexp_none_guard():
    """IfExp returns orelse when condition is None."""
    result = evaluate_expression(
        "a / b * 100 if b else 0",
        {"a": 45, "b": None},
    )
    assert result == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Test 7: compute_derived_metrics — P&L chain
# ---------------------------------------------------------------------------

# Topological order for all metrics in the catalogue (base first, then derived in dep order)
ALL_METRIC_KEYS = [
    # Base metrics
    "revenue",
    "direct_cost",
    "indirect_cost",
    "sga",
    "billable_hours",
    "total_hours",
    "avg_fte_billable",
    "avg_fte_overhead",
    "closing_fte_billable",
    "closing_fte_overhead",
    # Derived in dependency order
    "gross_margin",           # revenue - direct_cost
    "gross_margin_pct",       # gross_margin / revenue * 100
    "contribution_margin",    # gross_margin - indirect_cost
    "ebitda",                 # contribution_margin - sga
    "ebitda_margin_pct",      # ebitda / revenue * 100
    "avg_fte_total",          # avg_fte_billable + avg_fte_overhead
    "utilisation_rate",       # billable_hours / total_hours * 100
    "revenue_per_fte",        # revenue / avg_fte_billable
    "payroll_cost_per_fte",   # direct_cost / avg_fte_billable
]


def test_compute_derived_metrics_pnl_chain(catalogue):
    dim_key = ("2025-01",)
    scenario_data = {dim_key: _base_metrics()}

    compute_derived_metrics(scenario_data, catalogue, ALL_METRIC_KEYS)

    result = scenario_data[dim_key]

    assert result["gross_margin"] == pytest.approx(400_000.0)
    assert result["gross_margin_pct"] == pytest.approx(40.0)
    assert result["contribution_margin"] == pytest.approx(300_000.0)
    assert result["ebitda"] == pytest.approx(250_000.0)
    assert result["ebitda_margin_pct"] == pytest.approx(25.0)
    assert result["avg_fte_total"] == pytest.approx(70.0)
    assert result["utilisation_rate"] == pytest.approx(75.0)
    assert result["revenue_per_fte"] == pytest.approx(20_000.0)
    assert result["payroll_cost_per_fte"] == pytest.approx(12_000.0)


# ---------------------------------------------------------------------------
# Test 8: compute_derived_metrics — multiple dimension keys
# ---------------------------------------------------------------------------

def test_compute_derived_metrics_multiple_dim_keys(catalogue):
    dim_key_1 = ("2025-01",)
    dim_key_2 = ("2025-02",)

    metrics_1 = _base_metrics()
    metrics_2 = {**_base_metrics(), "revenue": 1_200_000.0, "direct_cost": 700_000.0}

    scenario_data = {
        dim_key_1: metrics_1,
        dim_key_2: metrics_2,
    }

    compute_derived_metrics(scenario_data, catalogue, ALL_METRIC_KEYS)

    # Period 1
    assert scenario_data[dim_key_1]["gross_margin"] == pytest.approx(400_000.0)
    # Period 2: 1_200_000 - 700_000 = 500_000
    assert scenario_data[dim_key_2]["gross_margin"] == pytest.approx(500_000.0)

    # Verify independence: different gross_margin_pct
    # Period 1: 400_000 / 1_000_000 * 100 = 40.0
    assert scenario_data[dim_key_1]["gross_margin_pct"] == pytest.approx(40.0)
    # Period 2: 500_000 / 1_200_000 * 100 ≈ 41.67
    assert scenario_data[dim_key_2]["gross_margin_pct"] == pytest.approx(41.666, rel=1e-3)


# ---------------------------------------------------------------------------
# Test 9: compute_scenario — simple variance
# ---------------------------------------------------------------------------

def test_compute_scenario_simple_variance():
    dim_key = ("2025-01",)
    results = {
        "actuals": {dim_key: {"revenue": 1000.0}},
        "budget": {dim_key: {"revenue": 1200.0}},
    }

    compute_scenario(results, "budget_variance", "actuals - budget", ["revenue"])

    assert results["budget_variance"][dim_key]["revenue"] == pytest.approx(-200.0)


# ---------------------------------------------------------------------------
# Test 10: compute_scenario — percentage variance
# ---------------------------------------------------------------------------

def test_compute_scenario_percentage_variance():
    dim_key = ("2025-01",)
    results = {
        "actuals": {dim_key: {"revenue": 1000.0}},
        "budget": {dim_key: {"revenue": 1200.0}},
    }

    compute_scenario(
        results,
        "budget_variance_pct",
        "(actuals - budget) / abs(budget) * 100",
        ["revenue"],
    )

    expected = (1000.0 - 1200.0) / abs(1200.0) * 100
    assert results["budget_variance_pct"][dim_key]["revenue"] == pytest.approx(expected, rel=1e-3)


# ---------------------------------------------------------------------------
# Test 11: compute_scenario — None propagation
# ---------------------------------------------------------------------------

def test_compute_scenario_none_propagation():
    dim_key = ("2025-01",)
    # actuals has the dim_key, budget does not
    results = {
        "actuals": {dim_key: {"revenue": 1000.0}},
        "budget": {},  # no data for this dim_key
    }

    compute_scenario(results, "budget_variance", "actuals - budget", ["revenue"])

    # budget value for dim_key is None -> variance is None
    assert results["budget_variance"][dim_key]["revenue"] is None


# ---------------------------------------------------------------------------
# Test 12: compute_scenario — multiple metrics
# ---------------------------------------------------------------------------

def test_compute_scenario_multiple_metrics():
    dim_key = ("2025-01",)
    results = {
        "actuals": {dim_key: {"revenue": 1000.0, "direct_cost": 600.0}},
        "budget": {dim_key: {"revenue": 1200.0, "direct_cost": 700.0}},
    }

    compute_scenario(results, "budget_variance", "actuals - budget", ["revenue", "direct_cost"])

    assert results["budget_variance"][dim_key]["revenue"] == pytest.approx(-200.0)
    # direct_cost variance: 600 - 700 = -100 (actuals spent less than budget → favorable)
    assert results["budget_variance"][dim_key]["direct_cost"] == pytest.approx(-100.0)


# ---------------------------------------------------------------------------
# Test 13: transform — full pipeline
# ---------------------------------------------------------------------------

def test_transform_full_pipeline(catalogue):
    dim_key = ("2025-01",)

    actuals_base = _base_metrics()
    budget_base = {
        "revenue": 1_100_000.0,
        "direct_cost": 650_000.0,
        "indirect_cost": 110_000.0,
        "sga": 55_000.0,
        "billable_hours": 16_000.0,
        "total_hours": 21_000.0,
        "avg_fte_billable": 52.0,
        "avg_fte_overhead": 22.0,
        "closing_fte_billable": 53.0,
        "closing_fte_overhead": 23.0,
    }

    raw_results = {
        "actuals": {dim_key: actuals_base},
        "budget": {dim_key: budget_base},
    }

    computed_evals = [
        ComputedScenarioEval(
            scenario_key="budget_variance",
            formula="actuals - budget",
            dependencies=["actuals", "budget"],
        )
    ]

    result = transform(raw_results, catalogue, computed_evals, ALL_METRIC_KEYS)

    # Derived metrics computed for actuals
    assert result["actuals"][dim_key]["gross_margin"] == pytest.approx(400_000.0)
    # Derived metrics computed for budget: 1_100_000 - 650_000 = 450_000
    assert result["budget"][dim_key]["gross_margin"] == pytest.approx(450_000.0)

    # budget_variance scenario created
    assert "budget_variance" in result
    # Revenue variance: 1_000_000 - 1_100_000 = -100_000
    assert result["budget_variance"][dim_key]["revenue"] == pytest.approx(-100_000.0)
    # gross_margin variance: 400_000 - 450_000 = -50_000
    assert result["budget_variance"][dim_key]["gross_margin"] == pytest.approx(-50_000.0)


# ---------------------------------------------------------------------------
# Test 13b: transform — ratio metric through a computed scenario
# Regression: variance of a ratio must be A/C − B/D, not (A−B)/(C−D).
# ---------------------------------------------------------------------------

def test_transform_ratio_metric_variance_is_scalar_subtraction(catalogue):
    dim_key = ("2025-01",)
    # Inputs chosen so the two formulas give clearly different answers:
    #   actuals.realised_rate = 700 / 10  = 70
    #   budget.realised_rate  = 1540 / 20 = 77
    #   correct variance (A/C − B/D) = 70 − 77 = -7
    #   buggy re-derivation ((A−B)/(C−D)) = (700−1540)/(10−20) = -840/-10 = 84
    raw_results = {
        "actuals": {dim_key: {"revenue": 700.0, "billable_hours": 10.0}},
        "budget":  {dim_key: {"revenue": 1540.0, "billable_hours": 20.0}},
    }
    computed_evals = [
        ComputedScenarioEval(
            scenario_key="budget_variance",
            formula="actuals - budget",
            dependencies=["actuals", "budget"],
        )
    ]
    metric_keys = ["revenue", "billable_hours", "realised_rate"]

    result = transform(raw_results, catalogue, computed_evals, metric_keys)

    # Source scenarios derive the ratio correctly.
    assert result["actuals"][dim_key]["realised_rate"] == pytest.approx(70.0)
    assert result["budget"][dim_key]["realised_rate"] == pytest.approx(77.0)

    # Variance on the ratio must be scalar subtraction of the derived values,
    # not re-derivation from the subtracted base inputs.
    assert result["budget_variance"][dim_key]["realised_rate"] == pytest.approx(-7.0)


# ---------------------------------------------------------------------------
# Test 14: compute_scenario — dimension alignment
# ---------------------------------------------------------------------------

def test_compute_scenario_dimension_alignment():
    dim_key_1 = ("2025-01",)
    dim_key_2 = ("2025-02",)

    results = {
        "actuals": {
            dim_key_1: {"revenue": 1000.0},
            dim_key_2: {"revenue": 1100.0},
        },
        "prior_year": {
            dim_key_1: {"revenue": 900.0},
            dim_key_2: {"revenue": 950.0},
        },
    }

    compute_scenario(results, "py_variance", "actuals - prior_year", ["revenue"])

    assert results["py_variance"][dim_key_1]["revenue"] == pytest.approx(100.0)
    assert results["py_variance"][dim_key_2]["revenue"] == pytest.approx(150.0)


# ---------------------------------------------------------------------------
# Test 15: evaluate_expression — rejects unsafe constructs
# ---------------------------------------------------------------------------

def test_evaluate_rejects_import():
    with pytest.raises(ExpressionError):
        evaluate_expression("__import__('os').system('ls')", {})


def test_evaluate_rejects_attribute_access():
    with pytest.raises(ExpressionError):
        evaluate_expression("a.b", {"a": 10})


def test_evaluate_rejects_unknown_function():
    with pytest.raises(ExpressionError):
        evaluate_expression("eval('1+1')", {})
