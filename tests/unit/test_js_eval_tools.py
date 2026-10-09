# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Tests for the JS transform helper (run_js_transform)."""

import json

import pytest

from precis_mcp.js_eval import run_js_transform, validate_transform_code


@pytest.fixture
def eval_fn():
    """Return the run_js_transform helper."""
    return run_js_transform


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

SIMPLE_ENGINE_RESULT = json.dumps({
    "title": "P&L Summary",
    "columns": [
        {"key": "fs_line", "label": "P&L Line"},
        {"key": "actuals", "label": "Actuals"},
    ],
    "rows": [
        {"row_type": "line_item", "label": "Revenue", "values": {"actuals": 1500000}},
        {"row_type": "line_item", "label": "COGS", "values": {"actuals": 900000}},
        {"row_type": "total", "label": "Gross Profit", "values": {"actuals": 600000}},
    ],
})

MULTI_PERIOD_RESULT = json.dumps({
    "title": "Revenue Trend",
    "columns": [
        {"key": "period", "label": "Period"},
        {"key": "actuals", "label": "Actuals"},
        {"key": "budget", "label": "Budget"},
    ],
    "rows": [
        {"label": "2026-01", "values": {"actuals": 1200000, "budget": 1300000}},
        {"label": "2026-02", "values": {"actuals": 1350000, "budget": 1300000}},
        {"label": "2026-03", "values": {"actuals": 1100000, "budget": 1300000}},
    ],
})


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

def test_empty_transform_code(eval_fn):
    result = eval_fn(transform_code="", engine_result_json=SIMPLE_ENGINE_RESULT)
    assert result["status"] == "error"
    assert "No transform code" in result["error"]


def test_whitespace_transform_code(eval_fn):
    result = eval_fn(transform_code="   \n\t  ", engine_result_json=SIMPLE_ENGINE_RESULT)
    assert result["status"] == "error"
    assert "No transform code" in result["error"]


def test_empty_engine_result(eval_fn):
    result = eval_fn(transform_code="(x) => x.rows", engine_result_json="")
    assert result["status"] == "error"
    assert "No engine result" in result["error"]


def test_invalid_json_engine_result(eval_fn):
    result = eval_fn(transform_code="(x) => x.rows", engine_result_json="{not valid json")
    assert result["status"] == "error"
    assert "not valid JSON" in result["error"]


# ---------------------------------------------------------------------------
# Successful transforms
# ---------------------------------------------------------------------------

def test_simple_row_extraction(eval_fn):
    transform = """(input) => input.rows
        .filter(r => r.row_type === "line_item")
        .map(r => ({label: r.label, value: r.values.actuals}))"""
    result = eval_fn(transform_code=transform, engine_result_json=SIMPLE_ENGINE_RESULT)
    assert result["status"] == "ok"
    assert result["row_count"] == 2
    assert result["data"] == [
        {"label": "Revenue", "value": 1500000},
        {"label": "COGS", "value": 900000},
    ]
    assert isinstance(result["execution_time_ms"], int)


def test_multi_series_chart_data(eval_fn):
    transform = """(input) => input.rows.map(r => ({
        month: r.label,
        revenue: r.values.actuals,
        budget: r.values.budget
    }))"""
    result = eval_fn(transform_code=transform, engine_result_json=MULTI_PERIOD_RESULT)
    assert result["status"] == "ok"
    assert result["row_count"] == 3
    assert result["data"][0] == {
        "month": "2026-01", "revenue": 1200000, "budget": 1300000,
    }


def test_aggregation_in_transform(eval_fn):
    transform = """(input) => {
        const total = input.rows.reduce((sum, r) => sum + r.values.actuals, 0);
        return [{label: "Total", value: total}];
    }"""
    result = eval_fn(transform_code=transform, engine_result_json=MULTI_PERIOD_RESULT)
    assert result["status"] == "ok"
    assert result["row_count"] == 1
    assert result["data"][0]["value"] == 3650000


def test_empty_array_result(eval_fn):
    transform = """(input) => input.rows.filter(r => r.row_type === "nonexistent")"""
    result = eval_fn(transform_code=transform, engine_result_json=SIMPLE_ENGINE_RESULT)
    assert result["status"] == "ok"
    assert result["data"] == []
    assert result["row_count"] == 0


def test_waterfall_transform(eval_fn):
    """Waterfall charts use bar_type markers — transform should produce them."""
    transform = """(input) => {
        const items = input.rows.filter(r => r.row_type === "line_item");
        const total = items.reduce((s, r) => s + r.values.actuals, 0);
        return [
            {category: "Total", value: total, bar_type: "total"},
            ...items.map(r => ({category: r.label, value: r.values.actuals}))
        ];
    }"""
    result = eval_fn(transform_code=transform, engine_result_json=SIMPLE_ENGINE_RESULT)
    assert result["status"] == "ok"
    assert result["data"][0]["bar_type"] == "total"
    assert result["row_count"] == 3


def test_fixed_parameters_in_transform(eval_fn):
    """Transform can embed fixed constants (ad-hoc / hybrid case)."""
    transform = """(input) => {
        const target = 1300000;
        return input.rows.map(r => ({
            month: r.label,
            revenue: r.values.actuals,
            target: target
        }));
    }"""
    result = eval_fn(transform_code=transform, engine_result_json=MULTI_PERIOD_RESULT)
    assert result["status"] == "ok"
    assert all(row["target"] == 1300000 for row in result["data"])


def test_null_engine_result_with_baked_data(eval_fn):
    """When source is null, transform can return baked-in data."""
    transform = """(input) => [
        {month: "Jan", value: 100},
        {month: "Feb", value: 200}
    ]"""
    # Pass a minimal valid JSON (the transform ignores it)
    result = eval_fn(transform_code=transform, engine_result_json="null")
    assert result["status"] == "ok"
    assert result["row_count"] == 2


# ---------------------------------------------------------------------------
# Variadic multi-input (cross-domain charts)
# ---------------------------------------------------------------------------

def test_single_input_as_list_matches_string(eval_fn):
    """A one-element list is the one-argument case — identical to a bare string."""
    transform = "(input) => input.rows.map(r => ({x: r.label, v: r.values.actuals}))"
    as_str = eval_fn(transform_code=transform, engine_result_json=SIMPLE_ENGINE_RESULT)
    as_list = eval_fn(transform_code=transform, engine_result_json=[SIMPLE_ENGINE_RESULT])
    assert as_str["status"] == "ok"
    assert as_str["data"] == as_list["data"]


def test_variadic_two_inputs_joined(eval_fn):
    """Two inputs arrive as positional args, in order, for a cross-domain join."""
    rev = json.dumps({"rows": [
        {"m": "Jan", "v": 100}, {"m": "Feb", "v": 120}]})
    util = json.dumps({"rows": [
        {"m": "Jan", "v": 80}, {"m": "Feb", "v": 85}]})
    transform = """(rev, util) => rev.rows.map(r => ({
        month: r.m,
        revenue: r.v,
        utilisation: (util.rows.find(u => u.m === r.m) || {}).v ?? null
    }))"""
    result = eval_fn(transform_code=transform, engine_result_json=[rev, util])
    assert result["status"] == "ok"
    assert result["data"] == [
        {"month": "Jan", "revenue": 100, "utilisation": 80},
        {"month": "Feb", "revenue": 120, "utilisation": 85},
    ]


def test_variadic_unmatched_point_is_null_not_broadcast(eval_fn):
    """A finer point with no match emits null — never repeats a coarse value."""
    fine = json.dumps({"rows": [{"m": "Jan"}, {"m": "Feb"}, {"m": "Mar"}]})
    coarse = json.dumps({"rows": [{"m": "Jan", "v": 9}, {"m": "Feb", "v": 9}]})
    transform = """(fine, coarse) => fine.rows.map(r => ({
        month: r.m,
        budget: (coarse.rows.find(c => c.m === r.m) || {}).v ?? null
    }))"""
    result = eval_fn(transform_code=transform, engine_result_json=[fine, coarse])
    assert result["status"] == "ok"
    assert result["data"][2] == {"month": "Mar", "budget": None}


def test_variadic_invalid_json_in_one_input(eval_fn):
    result = eval_fn(transform_code="(a, b) => []",
                     engine_result_json=['{"rows": []}', "{not valid json"])
    assert result["status"] == "error"
    assert "[1]" in result["error"]


def test_empty_input_list_errors(eval_fn):
    result = eval_fn(transform_code="(a) => []", engine_result_json=[])
    assert result["status"] == "error"
    assert "No engine result" in result["error"]


# ---------------------------------------------------------------------------
# JS errors
# ---------------------------------------------------------------------------

def test_runtime_error(eval_fn):
    transform = "(input) => input.nonexistent.property.deep"
    result = eval_fn(transform_code=transform, engine_result_json=SIMPLE_ENGINE_RESULT)
    assert result["status"] == "error"
    assert "TypeError" in result["error"] or "Cannot read" in result["error"]


def test_syntax_error(eval_fn):
    transform = "(input) => {{{invalid syntax"
    result = eval_fn(transform_code=transform, engine_result_json=SIMPLE_ENGINE_RESULT)
    assert result["status"] == "error"
    assert "SyntaxError" in result["error"]


def test_undefined_return(eval_fn):
    transform = "(input) => { input.rows; }"  # no return, implicit undefined
    result = eval_fn(transform_code=transform, engine_result_json=SIMPLE_ENGINE_RESULT)
    assert result["status"] == "error"
    assert "undefined" in result["error"].lower() or "null" in result["error"].lower()


def test_non_array_return(eval_fn):
    transform = '(input) => ({not_an_array: true})'
    result = eval_fn(transform_code=transform, engine_result_json=SIMPLE_ENGINE_RESULT)
    assert result["status"] == "error"
    assert "array" in result["error"].lower()


# ---------------------------------------------------------------------------
# Resource limits
# ---------------------------------------------------------------------------

def test_time_limit_enforced(eval_fn):
    transform = "(input) => { while(true) {} }"
    result = eval_fn(transform_code=transform, engine_result_json="null")
    assert result["status"] == "error"
    assert "interrupted" in result["error"].lower()


def test_memory_limit_enforced(eval_fn):
    transform = """(input) => {
        let arr = [];
        while(true) { arr.push(new Array(100000).fill(0)); }
    }"""
    result = eval_fn(transform_code=transform, engine_result_json="null")
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# Output size
# ---------------------------------------------------------------------------

def test_oversized_output_rejected(eval_fn):
    # Generate a transform that returns a very large array.
    # Depending on the memory limit, quickjs may OOM before JSON.stringify
    # finishes — either "Output too large" or "out of memory" is acceptable.
    transform = """(input) => {
        const big = [];
        for (let i = 0; i < 50000; i++) {
            big.push({k: "x".repeat(100), v: i});
        }
        return big;
    }"""
    result = eval_fn(transform_code=transform, engine_result_json="null")
    assert result["status"] == "error"
    err = result["error"].lower()
    assert "too large" in err or "out of memory" in err


# ---------------------------------------------------------------------------
# Injection-shape resistance + non-function rejection (H-11)
# ---------------------------------------------------------------------------

def test_non_function_value_rejected(eval_fn):
    """A transform that evaluates to a value, not a function, is rejected."""
    result = eval_fn(transform_code="42", engine_result_json=SIMPLE_ENGINE_RESULT)
    assert result["status"] == "error"
    assert "function" in result["error"].lower()


def test_statement_injection_is_syntax_error(eval_fn):
    """A statement-injection attempt cannot execute — the transform sits in an
    expression (argument) position, so an injected ';' is a SyntaxError, not
    code that runs."""
    payload = "x => x); throw new Error('BREAKOUT'); (y => y"
    result = eval_fn(transform_code=payload, engine_result_json=SIMPLE_ENGINE_RESULT)
    assert result["status"] == "error"
    assert "BREAKOUT" not in result["error"]
    assert "SyntaxError" in result["error"]


# ---------------------------------------------------------------------------
# Write-time validation (validate_transform_code) — H-14
# ---------------------------------------------------------------------------

class TestValidateTransformCode:
    def test_valid_arrow_ok(self):
        assert validate_transform_code("(input) => input.rows") is None

    def test_valid_block_body_ok(self):
        assert validate_transform_code("(a, b) => { return a.rows; }") is None

    def test_empty_rejected(self):
        assert "No transform code" in (validate_transform_code("") or "")
        assert "No transform code" in (validate_transform_code("   ") or "")

    def test_non_function_rejected(self):
        err = validate_transform_code("42")
        assert err and "function" in err.lower()

    def test_object_literal_rejected(self):
        err = validate_transform_code("{rows: []}")
        assert err and "function" in err.lower()

    def test_syntax_error_rejected(self):
        err = validate_transform_code("(input) =>")
        assert err and "valid JavaScript" in err

    def test_statement_injection_rejected(self):
        err = validate_transform_code("x => x); doThing(); (y => y")
        assert err is not None  # SyntaxError → not valid JS

    def test_validation_does_not_run_the_body(self):
        """Validation only constructs the function value; it must not invoke it,
        so a body that would throw at call time still validates as well-formed."""
        assert validate_transform_code("(input) => input.nope.deep.crash") is None
