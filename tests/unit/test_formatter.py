# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Tests for precis_mcp/engine/formatter.py — the unified result schema.

The formatter emits one self-describing shape for metric and statement
breakdowns: top-level kind/dimensions/scenarios/period/scale + a flat list of
grain-tagged rows, each {grain, dimensions, item, values}.
"""
from __future__ import annotations

import types
from typing import Any

from precis_mcp.engine.catalogue import resolve_statement
from precis_mcp.engine.formatter import (
    FormatterBlock,
    _scale_value,
    format_response,
    resolve_decimals,
)
from precis_mcp.engine.types import ROLLED_UP


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _metric_block(alias="Actuals", scenario="actuals", metrics=("revenue",), **kw):
    return FormatterBlock(
        alias=alias, scenario_key=scenario,
        metric_keys=list(metrics), display_items=list(metrics),
        is_statement=False, **kw,
    )


def _fmt(results, blocks, dimensions, catalogue, **kw):
    return format_response(
        results=results, blocks=blocks, catalogue=catalogue,
        dimensions=dimensions, period_start="2025-01", period_end="2025-12", **kw,
    )


# ---------------------------------------------------------------------------
# Top-level envelope
# ---------------------------------------------------------------------------

class TestEnvelope:
    def test_top_level_keys_and_kind_metric(self, catalogue):
        resp = _fmt({"actuals": {("CC-SE",): {"revenue": 5e6}}},
                    [_metric_block()], ["cost_centre"], catalogue)
        assert resp["kind"] == "metric"
        assert resp["dimensions"] == ["cost_centre"]
        assert resp["period"] == {"start": "2025-01", "end": "2025-12"}
        assert resp["scale"] == 0
        assert set(resp) >= {"kind", "dimensions", "scenarios", "period", "scale", "rows"}

    def test_scenarios_block_lists_columns(self, catalogue):
        resp = _fmt({"actuals": {("CC-SE",): {"revenue": 5e6}}},
                    [_metric_block()], ["cost_centre"], catalogue)
        assert resp["scenarios"] == [{"alias": "Actuals", "format": None, "variance": False}]

    def test_no_dimensions_is_detail_grain(self, catalogue):
        resp = _fmt({"actuals": {(): {"revenue": 5e6}}}, [_metric_block()], [], catalogue)
        assert resp["rows"][0]["grain"] == "detail"
        assert resp["rows"][0]["dimensions"] == {}


# ---------------------------------------------------------------------------
# Metric breakdown — flat rows, grain tagging
# ---------------------------------------------------------------------------

class TestMetricRows:
    def test_detail_row_shape(self, catalogue):
        resp = _fmt({"actuals": {("CC-SE",): {"revenue": 5e6}}},
                    [_metric_block()], ["cost_centre"], catalogue)
        row = resp["rows"][0]
        assert set(row) == {"grain", "dimensions", "item", "values"}
        assert row["grain"] == "detail"
        assert row["dimensions"] == {"cost_centre": "CC-SE"}
        assert row["item"]["key"] == "revenue"
        assert row["values"] == {"Actuals": 5e6}

    def test_grand_total_omits_rolled_up_dim(self, catalogue):
        resp = _fmt({"actuals": {(ROLLED_UP,): {"revenue": 8.2e6}}},
                    [_metric_block()], ["cost_centre"], catalogue)
        row = resp["rows"][0]
        assert row["grain"] == "grand_total"
        assert row["dimensions"] == {}
        assert row["values"] == {"Actuals": 8.2e6}

    def test_subtotal_keeps_live_dim_only(self, catalogue):
        resp = _fmt({"actuals": {("CC-SE", ROLLED_UP): {"revenue": 5e6}}},
                    [_metric_block()], ["cost_centre", "period"], catalogue)
        row = resp["rows"][0]
        assert row["grain"] == "subtotal"
        assert row["dimensions"] == {"cost_centre": "CC-SE"}

    def test_multi_metric_one_row_per_metric(self, catalogue):
        resp = _fmt(
            {"actuals": {("CC-SE",): {"revenue": 5e6, "direct_cost": 3e6}}},
            [_metric_block(metrics=("revenue", "direct_cost"))],
            ["cost_centre"], catalogue,
        )
        keys = [r["item"]["key"] for r in resp["rows"]]
        assert keys == ["revenue", "direct_cost"]

    def test_multi_block_values_keyed_by_alias(self, catalogue):
        resp = _fmt(
            {"actuals": {("CC-SE",): {"revenue": 5e6}},
             "budget": {("CC-SE",): {"revenue": 5.2e6}}},
            [_metric_block("Actuals", "actuals"), _metric_block("Budget", "budget")],
            ["cost_centre"], catalogue,
        )
        assert resp["rows"][0]["values"] == {"Actuals": 5e6, "Budget": 5.2e6}
        assert [s["alias"] for s in resp["scenarios"]] == ["Actuals", "Budget"]


# ---------------------------------------------------------------------------
# Statement breakdown — flat rows, separators as a line property
# ---------------------------------------------------------------------------

class TestStatementRows:
    def _stmt_blocks(self, catalogue):
        items = resolve_statement(catalogue, "pnl")
        mkeys = [i for i in items if i != "separator"]
        return items, mkeys, [FormatterBlock(
            alias="Actuals", scenario_key="actuals",
            metric_keys=mkeys, display_items=items, is_statement=True,
        )]

    def test_kind_statement_and_flat_rows(self, catalogue):
        items, mkeys, blocks = self._stmt_blocks(catalogue)
        vals = {m: 100.0 for m in mkeys}
        resp = _fmt({"actuals": {("CC-SE",): vals}}, blocks, ["cost_centre"], catalogue)
        assert resp["kind"] == "statement"
        assert all(set(r) == {"grain", "dimensions", "item", "values"} for r in resp["rows"])

    def test_separator_is_line_property(self, catalogue):
        items, mkeys, blocks = self._stmt_blocks(catalogue)
        vals = {m: 100.0 for m in mkeys}
        resp = _fmt({"actuals": {("CC-SE",): vals}}, blocks, ["cost_centre"], catalogue)
        assert any(r["item"]["separator_above"] for r in resp["rows"])
        assert all("separator" not in r for r in resp["rows"])  # no pseudo-rows

    def test_detail_and_grand_total_grains(self, catalogue):
        items, mkeys, blocks = self._stmt_blocks(catalogue)
        vals = {m: 100.0 for m in mkeys}
        resp = _fmt({"actuals": {("CC-SE",): vals, (ROLLED_UP,): vals}},
                    blocks, ["cost_centre"], catalogue)
        assert {r["grain"] for r in resp["rows"]} == {"detail", "grand_total"}


# ---------------------------------------------------------------------------
# hide_if_zero + scaling still apply per row
# ---------------------------------------------------------------------------

class TestValueHandling:
    def test_scale_applied_to_values(self, catalogue):
        resp = _fmt({"actuals": {("CC-SE",): {"revenue": 5_000_000.0}}},
                    [_metric_block()], ["cost_centre"], catalogue, scale=6)
        # scaled to millions
        assert resp["rows"][0]["values"]["Actuals"] == 5.0
        assert resp["scale"] == 6


# ---------------------------------------------------------------------------
# Decimal resolution + full-precision values
#
# The formatter scales but no longer rounds: it stamps a resolved per-item
# `decimals` and leaves values full-precision so Excel gets the exact figure.
# Display rounding happens downstream (renderers, agent payload).
# ---------------------------------------------------------------------------

class TestResolveDecimals:
    def _m(self, fmt="currency", scale_exempt=False) -> Any:
        # Duck-typed metric stand-in; resolve_decimals only reads
        # `.format` / `.scale_exempt`.
        return types.SimpleNamespace(format=fmt, scale_exempt=scale_exempt)

    def test_tier_defaults_by_scale(self):
        assert resolve_decimals(self._m(), 0, None) == 0
        assert resolve_decimals(self._m(), 3, None) == 0
        assert resolve_decimals(self._m(), 6, None) == 1
        assert resolve_decimals(self._m(), 9, None) == 2

    def test_explicit_override_wins_over_tier(self):
        assert resolve_decimals(self._m(), 6, 3) == 3
        assert resolve_decimals(self._m(), 0, 2) == 2

    def test_percent_is_one_decimal_and_exempt_from_scale(self):
        assert resolve_decimals(self._m(fmt="percent"), 6, None) == 1
        # explicit override still applies
        assert resolve_decimals(self._m(fmt="percent"), 6, 0) == 0

    def test_scale_exempt_metric_ignores_tier(self):
        # FTE / hours / ratios are never scaled, so use the exempt default.
        assert resolve_decimals(self._m(fmt="number", scale_exempt=True), 6, None) == 0


class TestPrecisionPreserved:
    def test_scale_value_divides_without_rounding(self):
        m: Any = types.SimpleNamespace(format="currency", scale_exempt=False)
        assert _scale_value(1_234_567.89, m, 3) == 1_234_567.89 / 1000
        assert _scale_value(None, m, 3) is None

    def test_scaled_values_are_full_precision(self, catalogue):
        # 5_000_123.45 / 1e6 = 5.00012345 — NOT rounded to the 1-dp millions tier.
        resp = _fmt({"actuals": {("CC-SE",): {"revenue": 5_000_123.45}}},
                    [_metric_block()], ["cost_centre"], catalogue, scale=6)
        assert resp["rows"][0]["values"]["Actuals"] == 5_000_123.45 / 1e6

    def test_item_carries_resolved_decimals(self, catalogue):
        resp0 = _fmt({"actuals": {("CC-SE",): {"revenue": 5e6}}},
                     [_metric_block()], ["cost_centre"], catalogue, scale=0)
        assert resp0["rows"][0]["item"]["decimals"] == 0
        resp6 = _fmt({"actuals": {("CC-SE",): {"revenue": 5e6}}},
                     [_metric_block()], ["cost_centre"], catalogue, scale=6)
        assert resp6["rows"][0]["item"]["decimals"] == 1

    def test_percent_scenario_column_carries_decimals_and_unrounded_value(self, catalogue):
        block = _metric_block(alias="Var %", display_format="percent", color_code=True)
        resp = _fmt({"actuals": {("CC-SE",): {"revenue": 12.345}}},
                    [block], ["cost_centre"], catalogue)
        scen = resp["scenarios"][0]
        assert scen["format"] == "percent"
        assert scen["decimals"] == 1
        # variance-% value is stored unrounded; display rounds downstream.
        assert resp["rows"][0]["values"]["Var %"] == 12.345
