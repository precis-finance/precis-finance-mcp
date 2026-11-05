# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Tests for chart tools (eval_chart_transform + data_ref caching)."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from tests.fakes.fake_redis import FakeRedis
from tests.fakes.mock_mcp import MockMCP


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

ENGINE_RESULT = {
    "kind": "statement",
    "dimensions": [],
    "scenarios": [{"alias": "Actuals", "format": "currency", "variance": False}],
    "period": {"start": "2026-01", "end": "2026-03"},
    "scale": 3,
    "decimals": 1,
    "rows": [
        {"grain": "detail", "dimensions": {},
         "item": {"key": "revenue", "label": "Revenue", "style": "header"},
         "values": {"Actuals": 1500.0}},
        {"grain": "detail", "dimensions": {},
         "item": {"key": "direct_cost", "label": "Direct Costs", "style": "default"},
         "values": {"Actuals": 900.0}},
        {"grain": "detail", "dimensions": {},
         "item": {"key": "gross_margin", "label": "Gross Margin", "style": "subtotal"},
         "values": {"Actuals": 600.0}},
    ],
    "caption": "P&L Summary | Jan–Mar 2026",
}

GROUPED_STATEMENT_RESULT = {
    "kind": "statement",
    "dimensions": ["cost_centre"],
    "scenarios": [{"alias": "Actuals", "format": "currency", "variance": False}],
    "period": {"start": "2026-01", "end": "2026-03"},
    "scale": 3,
    "decimals": 1,
    "rows": [
        {"grain": "detail", "dimensions": {"cost_centre": "CC-100"},
         "item": {"key": "revenue", "label": "Revenue", "style": "header"},
         "values": {"Actuals": 500.0}},
        {"grain": "detail", "dimensions": {"cost_centre": "CC-100"},
         "item": {"key": "direct_cost", "label": "Direct Costs", "style": "default"},
         "values": {"Actuals": 300.0}},
        {"grain": "detail", "dimensions": {"cost_centre": "CC-200"},
         "item": {"key": "revenue", "label": "Revenue", "style": "header"},
         "values": {"Actuals": 1000.0}},
        {"grain": "detail", "dimensions": {"cost_centre": "CC-200"},
         "item": {"key": "direct_cost", "label": "Direct Costs", "style": "default"},
         "values": {"Actuals": 600.0}},
        {"grain": "grand_total", "dimensions": {},
         "item": {"key": "revenue", "label": "Revenue", "style": "header"},
         "values": {"Actuals": 1500.0}},
        {"grain": "grand_total", "dimensions": {},
         "item": {"key": "direct_cost", "label": "Direct Costs", "style": "default"},
         "values": {"Actuals": 900.0}},
    ],
    "caption": "P&L by CC | Jan–Mar 2026",
}


@pytest.fixture(autouse=True)
def dataset_context():
    from precis_mcp.auth import AuthContext, UserPermissions, set_auth_context, clear_auth_context
    set_auth_context(AuthContext(user_id="u1", permissions=UserPermissions(user_id="u1", is_admin=True)))
    yield
    clear_auth_context()


@pytest.fixture
def fake_redis():
    return FakeRedis(decode_responses=True)


def _seed_cache(fake_redis, result, user_id="u1", conv_id=None, nonce=None, tool_name="run_statement", tool_args=None):
    from precis_mcp.datasets import cache_read_result
    with patch("precis_mcp.datasets.get_redis_client", return_value=fake_redis):
        return cache_read_result(result, tool_name, tool_args or {"statement": "pnl"})


@pytest.fixture
def eval_tool(fake_redis):
    from precis_mcp.tools.chart_tools import eval_chart_transform
    with patch("precis_mcp.datasets.get_redis_client", return_value=fake_redis):
        yield eval_chart_transform, fake_redis


VALID_CHART_SPEC = {
    "chart_type": "bar",
    "x_key": "label",
    "y_keys": ["value"],
}


def _formatted_metrics(catalogue, metrics, raw_values, scale, *, statement=False):
    from precis_mcp.engine.formatter import FormatterBlock, SCALE_LABELS, format_response

    result = format_response(
        results={"actuals": {(): raw_values}},
        blocks=[FormatterBlock(
            alias="Actuals", scenario_key="actuals", metric_keys=metrics,
            display_items=metrics, is_statement=statement,
        )],
        catalogue=catalogue, dimensions=[], period_start="2026-01",
        period_end="2026-03", scale=scale, decimals=1,
    )
    result["caption"] = {"period": "Jan–Mar 2026"}
    if SCALE_LABELS[scale]:
        result["caption"]["scale"] = SCALE_LABELS[scale]
    return result


@pytest.mark.parametrize("scale", [0, 3])
@pytest.mark.parametrize("worked,expected", [(100, 75), (0, None), (None, None)])
def test_federated_percentage_contract_across_consumers(eval_tool, tmp_path, catalogue, scale, worked, expected):
    """Load the actual catalogue formula, then preserve percentage points everywhere."""
    from precis_mcp.engine.transformer import compute_derived_metrics

    values = {"federated_billable_hours": 75, "federated_hours_worked": worked}
    compute_derived_metrics({(): values}, catalogue, ["federated_utilisation"])
    result = _formatted_metrics(catalogue, ["federated_utilisation"], values, scale, statement=True)
    _assert_percentage_consumers(eval_tool, tmp_path, result, expected)


@pytest.mark.parametrize("scale", [0, 3])
@pytest.mark.parametrize("fraction,expected", [(0.75, 75.0), (0.0, 0.0), (None, None)])
def test_project_percentage_contract_across_consumers(eval_tool, tmp_path, catalogue, scale, fraction, expected):
    from pathlib import Path
    import sqlglot
    from sqlglot import exp
    from sqlglot.executor import execute

    sql = (Path(__file__).resolve().parents[2] / "instance/semantic/views/v_revenue_subledger.sql").read_text()
    actuals = sqlglot.parse_one(sql, read="clickhouse").find(exp.Select)
    projection = next(item for item in actuals.expressions if item.alias_or_name == "percent_complete")
    value = execute(exp.select(projection.copy()).from_("s"), tables={"s": [{"percent_complete": fraction}]}).rows[0][0]
    result = _formatted_metrics(
        catalogue, ["project_percent_complete"], {"project_percent_complete": value}, scale, statement=True,
    )
    _assert_percentage_consumers(eval_tool, tmp_path, result, expected)


def _assert_percentage_consumers(eval_tool, tmp_path, result, expected):
    import openpyxl
    from precis_mcp.excel import statement_to_excel
    from precis_mcp.table_builder import build_financial_table_block

    assert result["rows"][0]["values"]["Actuals"] == expected
    assert result["rows"][0]["item"]["format"] == "percent"

    table = build_financial_table_block(result, for_excel=True)
    row = table["rows"][0]
    assert row["values"]["Actuals"] == expected
    assert row["format"] == "percent"
    assert '"%"' in row["nf"]

    path = tmp_path / "percentage.xlsx"
    statement_to_excel(result, path)
    workbook = openpyxl.load_workbook(path)
    percent_cells = [cell for sheet in workbook for cells in sheet for cell in cells if '"%"' in cell.number_format]
    assert percent_cells
    assert all(cell.value == expected for cell in percent_cells)
    workbook.close()

    fn, redis = eval_tool
    ref = _seed_cache(redis, result)
    chart_result = fn(
        data_refs=[ref],
        transform_code="input => input.rows.map(r => ({label: r.item.label, value: r.values.Actuals}))",
        chart_spec={**VALID_CHART_SPEC, "y_format": "percent"}, user_id="u1",
    )
    assert chart_result["status"] == "ok"
    assert chart_result["chart"]["data"][0]["value"] == expected
    assert chart_result["chart"]["y_format"] == "percent"
    assert chart_result["chart"]["y_scale"] == 0


@pytest.mark.parametrize("scale", [0, 3])
@pytest.mark.parametrize("multi_source", [False, True])
def test_currency_and_fte_chart_units(eval_tool, catalogue, scale, multi_source):
    """Use real formatter scaling before caching; FTE stays in units on its own axis."""
    fn, redis = eval_tool
    raw_values = {"revenue": 2579289.74, "avg_fte_billable": 12.5}
    if multi_source:
        currency = _formatted_metrics(catalogue, ["revenue"], raw_values, scale)
        fte = _formatted_metrics(catalogue, ["avg_fte_billable"], raw_values, 0)
        refs = [_seed_cache(redis, currency), _seed_cache(redis, fte)]
        transform = "(currency, fte) => [{label: 'Total', revenue: currency.rows[0].values.Actuals, fte: fte.rows[0].values.Actuals}]"
    else:
        mixed = _formatted_metrics(catalogue, list(raw_values), raw_values, scale, statement=True)
        refs = [_seed_cache(redis, mixed)]
        transform = "input => [{label: 'Total', revenue: input.rows.find(r => r.item.key === 'revenue').values.Actuals, fte: input.rows.find(r => r.item.key === 'avg_fte_billable').values.Actuals}]"
    currency_unit = "€ thousands" if scale == 3 else "€"
    output = fn(
        data_refs=refs, transform_code=transform, user_id="u1",
        chart_spec={
            "chart_type": "line", "x_key": "label", "y_keys": ["revenue", "fte"],
            "y_axis_map": {"revenue": "left", "fte": "right"},
            "y_format": "currency", "y_format_right": "number",
            "y_label": f"Revenue ({currency_unit})", "y_label_right": "FTE",
            "y_decimals_right": 1,
        },
    )
    assert output["status"] == "ok"
    chart = output["chart"]
    assert chart["data"][0]["revenue"] == pytest.approx(2579289.74 / 10 ** scale)
    assert chart["data"][0]["fte"] == 12.5
    assert chart["y_scale"] == 0
    assert chart["y_decimals"] == 1 and chart["y_decimals_right"] == 1
    assert chart["y_axis_map"] == {"revenue": "left", "fte": "right"}
    assert chart["y_format"] == "currency" and chart["y_format_right"] == "number"
    assert chart["y_label"] == f"Revenue ({currency_unit})" and chart["y_label_right"] == "FTE"
    assert chart["caption"]["period"] == "Jan–Mar 2026"
    # A source with no scale label does not remove the currency source's label;
    # axis labels still disambiguate FTE. Conflicting nonempty labels are tested below.
    assert chart["caption"].get("scale") == ("€ thousands" if scale == 3 else None)


class TestEvalChartTransformValidation:
    def test_missing_data_ref(self, eval_tool):
        fn, _ = eval_tool
        result = fn(data_refs=[""], transform_code="x=>x",
                    chart_spec=VALID_CHART_SPEC, user_id="u1")
        assert result["status"] == "error"
        assert "data_ref" in result["error"]

    def test_missing_transform_code(self, eval_tool):
        fn, _ = eval_tool
        result = fn(data_refs=["ds_" + "0" * 32], transform_code="",
                    chart_spec=VALID_CHART_SPEC, user_id="u1")
        assert result["status"] == "error"
        assert "transform code" in result["error"]

    def test_malformed_transform_rejected_at_write(self, eval_tool):
        """A non-function transform is rejected at author time (H-14) — before
        any data_ref fetch — so it can never be stored and re-run at refresh."""
        fn, _ = eval_tool
        result = fn(data_refs=["ds_" + "0" * 32], transform_code="42",
                    chart_spec=VALID_CHART_SPEC, user_id="u1")
        assert result["status"] == "error"
        assert "function" in result["error"].lower()

    def test_invalid_chart_type(self, eval_tool):
        fn, _ = eval_tool
        result = fn(data_refs=["ds_" + "0" * 32], transform_code="x=>x",
                    chart_spec={"chart_type": "pie", "x_key": "a", "y_keys": ["b"]},
                    user_id="u1")
        assert result["status"] == "error"
        assert "pie" in result["error"]

    def test_missing_x_key(self, eval_tool):
        fn, _ = eval_tool
        result = fn(data_refs=["ds_" + "0" * 32], transform_code="x=>x",
                    chart_spec={"chart_type": "line", "y_keys": ["b"]},
                    user_id="u1")
        assert result["status"] == "error"
        assert "x_key" in result["error"]

    def test_expired_data_ref(self, eval_tool):
        fn, _ = eval_tool
        result = fn(data_refs=["ds_" + "0" * 32], transform_code="x=>x.rows",
                    chart_spec=VALID_CHART_SPEC, user_id="u1")
        assert result["status"] == "error"
        assert "expired" in result["error"]


# ---------------------------------------------------------------------------
# eval_chart_transform — non-dimensioned rows data
# ---------------------------------------------------------------------------


class TestEvalChartTransformAggregate:
    def test_transform_on_rows(self, eval_tool):
        fn, redis = eval_tool
        data_ref = _seed_cache(redis, ENGINE_RESULT)

        transform = """(input) => input.rows
            .filter(r => r.grain === "detail" && r.item.style !== "subtotal")
            .map(r => ({label: r.item.label, value: r.values.Actuals}))"""
        result = fn(data_refs=[data_ref], transform_code=transform,
                    chart_spec=VALID_CHART_SPEC, user_id="u1")

        assert result["status"] == "ok"
        assert result["row_count"] == 2
        assert result["chart"]["data"] == [
            {"label": "Revenue", "value": 1500.0},
            {"label": "Direct Costs", "value": 900.0},
        ]
        assert result["chart"]["chart_type"] == "bar"
        assert "chart_ref" in result
        assert result["chart_ref"].startswith("chart_")
        assert result["source"]["tool"] == "run_statement"
        assert result["transform_code"] == transform
        # y_scale forced to 0, y_decimals from top-level engine result
        assert result["chart"]["y_scale"] == 0
        assert result["chart"]["y_decimals"] == 1

    def test_y_scale_forced_to_zero(self, eval_tool):
        """Agent-supplied y_scale is ignored — always 0 since data is pre-scaled."""
        fn, redis = eval_tool
        data_ref = _seed_cache(redis, ENGINE_RESULT)

        transform = """(input) => input.rows
            .filter(r => r.grain === "detail" && r.item.style !== "subtotal")
            .map(r => ({label: r.item.label, value: r.values.Actuals}))"""
        spec = {
            "chart_type": "bar",
            "x_key": "label",
            "y_keys": ["value"],
            "y_scale": 6,  # agent passes 6 — should be overridden to 0
        }
        result = fn(data_refs=[data_ref], transform_code=transform,
                    chart_spec=spec, user_id="u1")

        assert result["status"] == "ok"
        assert result["chart"]["y_scale"] == 0
        assert result["chart"]["y_decimals"] == 1

    def test_top_level_period_and_scale_accessible_in_transform(self, eval_tool):
        """Transform can access top-level period and scale from the engine result."""
        fn, redis = eval_tool
        data_ref = _seed_cache(redis, ENGINE_RESULT)

        transform = """(input) => [{
            label: input.period.start + ' to ' + input.period.end,
            value: input.scale
        }]"""
        result = fn(data_refs=[data_ref], transform_code=transform,
                    chart_spec=VALID_CHART_SPEC, user_id="u1")

        assert result["status"] == "ok"
        assert result["chart"]["data"] == [
            {"label": "2026-01 to 2026-03", "value": 3},
        ]

    def test_chart_spec_optional_fields(self, eval_tool):
        fn, redis = eval_tool
        data_ref = _seed_cache(redis, ENGINE_RESULT)

        transform = """(input) => input.rows
            .filter(r => r.grain === "detail" && r.item.style !== "subtotal")
            .map(r => ({label: r.item.label, value: r.values.Actuals}))"""
        spec = {
            "chart_type": "line",
            "x_key": "label",
            "y_keys": ["value"],
            "series_labels": {"value": "Actuals"},
            "y_format": "currency",
            "y_scale": 3,
            "title": "Revenue vs COGS",
        }
        result = fn(data_refs=[data_ref], transform_code=transform,
                    chart_spec=spec, user_id="u1")

        assert result["status"] == "ok"
        chart = result["chart"]
        assert chart["series_labels"] == {"value": "Actuals"}
        assert chart["y_format"] == "currency"
        # y_scale forced to 0 (data already scaled by engine), y_decimals from top-level result
        assert chart["y_scale"] == 0
        assert chart["y_decimals"] == 1  # from ENGINE_RESULT decimals
        assert chart["title"] == "Revenue vs COGS"

    def test_transform_error_returned(self, eval_tool):
        fn, redis = eval_tool
        data_ref = _seed_cache(redis, ENGINE_RESULT)

        transform = "(input) => input.nonexistent.deep.path"
        result = fn(data_refs=[data_ref], transform_code=transform,
                    chart_spec=VALID_CHART_SPEC, user_id="u1")
        assert result["status"] == "error"


# ---------------------------------------------------------------------------
# eval_chart_transform — cross-domain (multiple data_refs)
# ---------------------------------------------------------------------------

UTIL_RESULT = {
    "kind": "metric",
    "dimensions": ["month"],
    "scenarios": [{"alias": "Actuals", "format": "percent", "variance": False}],
    "period": {"start": "2026-01", "end": "2026-02"},
    "scale": 0,
    "decimals": 1,
    "rows": [
        {"grain": "detail", "dimensions": {"month": "Jan"},
         "item": {"key": "utilisation", "label": "Utilisation"},
         "values": {"Actuals": 80.0}},
        {"grain": "detail", "dimensions": {"month": "Feb"},
         "item": {"key": "utilisation", "label": "Utilisation"},
         "values": {"Actuals": 85.0}},
    ],
    "caption": {"period": "FY2025", "scenarios": ["Actuals"], "scale": "%"},
}

REV_BY_MONTH_RESULT = {
    "kind": "metric",
    "dimensions": ["month"],
    "scenarios": [{"alias": "Actuals", "format": "currency", "variance": False}],
    "period": {"start": "2026-01", "end": "2026-02"},
    "scale": 3,
    "decimals": 1,
    "rows": [
        {"grain": "detail", "dimensions": {"month": "Jan"},
         "item": {"key": "revenue", "label": "Revenue"},
         "values": {"Actuals": 100.0}},
        {"grain": "detail", "dimensions": {"month": "Feb"},
         "item": {"key": "revenue", "label": "Revenue"},
         "values": {"Actuals": 120.0}},
    ],
    "caption": {"period": "FY2025", "scenarios": ["Actuals"], "scale": "€'000"},
}

_JOIN_TRANSFORM = """(rev, util) => rev.rows
    .filter(r => r.grain === 'detail')
    .map(r => ({
        month: r.dimensions.month,
        revenue: r.values.Actuals,
        utilisation: (util.rows.find(u =>
            u.dimensions.month === r.dimensions.month) || {}).values?.Actuals ?? null
    }))"""


class TestEvalChartTransformCrossDomain:
    def test_multi_ref_joined(self, eval_tool):
        fn, redis = eval_tool
        rev_ref = _seed_cache(redis, REV_BY_MONTH_RESULT, nonce="rev1",
                              tool_name="run_metric", tool_args={"metrics": ["revenue"]})
        util_ref = _seed_cache(redis, UTIL_RESULT, nonce="util1",
                               tool_name="run_metric", tool_args={"metrics": ["utilisation"]})

        result = fn(
            data_refs=[rev_ref, util_ref],
            transform_code=_JOIN_TRANSFORM,
            chart_spec={
                "chart_type": "line", "x_key": "month",
                "y_keys": ["revenue", "utilisation"],
                "y_axis_map": {"revenue": "left", "utilisation": "right"},
                "y_format_right": "percent",
            },
            user_id="u1",
        )
        assert result["status"] == "ok"
        assert result["chart"]["data"] == [
            {"month": "Jan", "revenue": 100.0, "utilisation": 80.0},
            {"month": "Feb", "revenue": 120.0, "utilisation": 85.0},
        ]
        # dual-axis spec carried through
        assert result["chart"]["y_axis_map"] == {"revenue": "left", "utilisation": "right"}
        assert result["chart"]["y_format_right"] == "percent"
        # source persisted as an ordered list, parallel to data_ref
        assert isinstance(result["source"], list)
        assert [s["args"]["metrics"] for s in result["source"]] == [["revenue"], ["utilisation"]]

    def test_multi_ref_json_encoded_string(self, eval_tool):
        """The canonical array contract rejects double-encoded lists."""
        fn, redis = eval_tool
        rev_ref = _seed_cache(redis, REV_BY_MONTH_RESULT, nonce="rev2",
                              tool_name="run_metric")
        util_ref = _seed_cache(redis, UTIL_RESULT, nonce="util2",
                               tool_name="run_metric")
        result = fn(
            data_refs=json.dumps([rev_ref, util_ref]),
            transform_code=_JOIN_TRANSFORM,
            chart_spec={"chart_type": "bar", "x_key": "month", "y_keys": ["revenue"]},
            user_id="u1",
        )
        assert result["status"] == "error"
        assert "array" in result["error"]

    def test_multi_ref_one_expired_errors(self, eval_tool):
        fn, redis = eval_tool
        rev_ref = _seed_cache(redis, REV_BY_MONTH_RESULT, nonce="rev3")
        result = fn(
            data_refs=[rev_ref, "ds_" + "0" * 32],
            transform_code="(a, b) => []",
            chart_spec=VALID_CHART_SPEC, user_id="u1",
        )
        assert result["status"] == "error"
        assert "expired" in result["error"]

    def test_single_ref_source_stays_dict(self, eval_tool):
        """A single ref keeps the bare-dict source shape (unchanged on disk)."""
        fn, redis = eval_tool
        data_ref = _seed_cache(redis, ENGINE_RESULT)
        transform = """(input) => input.rows
            .filter(r => r.grain === 'detail' && r.item.style !== 'subtotal')
            .map(r => ({label: r.item.label, value: r.values.Actuals}))"""
        result = fn(data_refs=[data_ref], transform_code=transform,
                    chart_spec=VALID_CHART_SPEC, user_id="u1")
        assert result["status"] == "ok"
        assert isinstance(result["source"], dict)

    def test_consolidated_caption_drops_mixed_scale(self, eval_tool):
        """Cross-domain caption merges: shared period kept, mixed scale dropped."""
        fn, redis = eval_tool
        rev_ref = _seed_cache(redis, REV_BY_MONTH_RESULT, nonce="rev4")
        util_ref = _seed_cache(redis, UTIL_RESULT, nonce="util4")
        result = fn(
            data_refs=[rev_ref, util_ref],
            transform_code=_JOIN_TRANSFORM,
            chart_spec={"chart_type": "line", "x_key": "month", "y_keys": ["revenue"]},
            user_id="u1",
        )
        assert result["status"] == "ok"
        caption = result["chart"]["caption"]
        assert caption["period"] == "FY2025"      # shared → kept
        assert "scale" not in caption             # €'000 vs % → dropped


# ---------------------------------------------------------------------------
# eval_chart_transform — dimensioned unified rows
# ---------------------------------------------------------------------------


class TestEvalChartTransformDimensioned:
    def test_transform_on_detail_rows(self, eval_tool):
        """Transform can iterate detail-grain rows in the unified schema."""
        fn, redis = eval_tool
        data_ref = _seed_cache(redis, GROUPED_STATEMENT_RESULT)

        transform = """(input) => input.rows
            .filter(r => r.grain === 'detail' && r.item.key === 'revenue')
            .map(r => {
            return {
                cc: r.dimensions.cost_centre,
                revenue: r.values.Actuals
            };
        })"""
        result = fn(
            data_refs=[data_ref],
            transform_code=transform,
            chart_spec={"chart_type": "bar", "x_key": "cc", "y_keys": ["revenue"]},
            user_id="u1",
        )

        assert result["status"] == "ok"
        assert result["row_count"] == 2
        assert result["chart"]["data"] == [
            {"cc": "CC-100", "revenue": 500.0},
            {"cc": "CC-200", "revenue": 1000.0},
        ]

    def test_grand_total_accessible_in_transform(self, eval_tool):
        """Transform can access grand-total grain rows for totals."""
        fn, redis = eval_tool
        data_ref = _seed_cache(redis, GROUPED_STATEMENT_RESULT)

        transform = """(input) => {
            const total = input.rows.find(r => r.grain === 'grand_total' && r.item.key === 'revenue');
            return [{label: 'Total Revenue', value: total.values.Actuals}];
        }"""
        result = fn(
            data_refs=[data_ref],
            transform_code=transform,
            chart_spec=VALID_CHART_SPEC,
            user_id="u1",
        )

        assert result["status"] == "ok"
        assert result["chart"]["data"] == [
            {"label": "Total Revenue", "value": 1500.0},
        ]
