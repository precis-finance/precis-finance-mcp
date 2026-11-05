# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Tests for precis_mcp.excel — validates Excel generation from engine results."""

import pytest
from pathlib import Path
from typing import Any

import openpyxl

from precis_mcp.excel import (
    statement_to_excel,
    metric_to_excel,
    hierarchy_to_excel,
)
from precis_mcp.excel.meta import read_meta
from precis_mcp.excel.worksheet import report_worksheet
from precis_mcp.table_builder import build_financial_table_block


def test_python_formula_codec_consumes_shared_addin_fixtures():
    import json
    from precis_mcp.excel.formulas import parse_financial_formula
    fixtures = json.loads((Path(__file__).parents[2]/"excel-addin/tests/fixtures/financial-formulas.json").read_text())
    for fixture in fixtures:
        definition = parse_financial_formula(fixture["formula"], lambda ref:pytest.fail(f"Unexpected reference {ref}"))
        assert definition.params == fixture["source"]["params"]
        assert definition.tool == fixture["source"]["tool"]
        assert definition.layout == fixture["layout"]


@pytest.mark.parametrize("prefix", ["", "_xlfn.", "_xludf.", "_xlfn._xlws."])
def test_formula_codec_preserves_defaults_and_resolves_scalar_arguments(prefix):
    from precis_mcp.excel.formulas import parse_financial_formula
    resolved = {"B1":"revenue", "StartMonth":"2026-01", "'Control sheet'!$C$2":"2026-03"}
    definition = parse_financial_formula(f"={prefix}PRECIS.METRIC(B1,StartMonth,'Control sheet'!$C$2)", resolved.__getitem__)
    assert definition.params == {"metrics":["revenue"],"period_start":"2026-01","period_end":"2026-03","scale":3,"decimals":1}
    assert definition.layout == "report"


@pytest.mark.parametrize("function", ["STATEMENT", "METRIC"])
@pytest.mark.parametrize("prefix", ["_xldudf_PRECIS_", "_xlfn._xldudf_PRECIS_", "_XLDudf_PRECIS_"])
def test_formula_codec_recognizes_excel_saved_functions_without_rewriting_arguments(function, prefix):
    from precis_mcp.excel.formulas import contains_precis_function, parse_financial_formula
    arguments = '"_xldudf_PRECIS_name","2026-01","2026-08"'
    formula = f'={prefix}{function}({arguments})'
    definition = parse_financial_formula(formula, lambda ref: pytest.fail(ref))
    canonical = parse_financial_formula(f'=PRECIS.{function}({arguments})', lambda ref: pytest.fail(ref))
    assert definition == canonical
    assert contains_precis_function(formula)


@pytest.mark.parametrize("formula,expected", [
    ('=SUM(_xldudf_PRECIS_METRIC("a","2026-01","2026-03"))', True),
    ('=_xldudf_PRECIS_MEMBERS("cost_centre")', True),
    ('="_xldudf_PRECIS_METRIC(""a"")"', False),
    ('="PRECIS.METRIC("', False),
    ('=Other_xldudf_PRECIS_METRIC("a")', False),
    ('=1+2', False),
])
def test_live_function_detection_ignores_text_literals(formula, expected):
    from precis_mcp.excel.formulas import contains_precis_function
    assert contains_precis_function(formula) is expected


@pytest.mark.parametrize("formula", ['=SUM(PRECIS.METRIC("a","2026-01","2026-03"))', '=PRECIS.METRIC("a","2026-01","2026-03",NOW())', '=PRECIS.METRIC("a","2026-13","2026-03")'])
def test_formula_codec_rejects_expressions_and_invalid_periods(formula):
    from precis_mcp.excel.formulas import ConversionError, parse_financial_formula
    with pytest.raises(ConversionError):
        parse_financial_formula(formula, lambda ref:None)


@pytest.mark.parametrize("formula", [
    '=PRECIS.METRIC("revenue","2026-01","2026-03","","actuals","","extract",0,2,"default")',
    '=PRECIS.STATEMENT("pnl","2026-01","2026-03","actuals","","",0,2,"report","default")',
])
def test_formula_codec_rejects_removed_tenth_argument(formula):
    from precis_mcp.excel.formulas import ConversionError, parse_financial_formula
    with pytest.raises(ConversionError, match="3 to 9 worksheet arguments"):
        parse_financial_formula(formula, lambda ref: pytest.fail(ref))


def test_financial_caption_footer_has_the_platform_context_order_and_cell_limit():
    from precis_mcp.excel.worksheet import caption_text
    caption = {"description":"User maintained title", "scenarios":["Actuals","Budget"],
               "period":"2026-01 to 2026-08", "filters":{"department":["D1","D2"]},
               "dimensions":["cost_centre"], "scale":"€ thousands", "duration_ms":125,
               "generated_at":"2026-10-22 18:00 UTC"}
    assert caption_text(caption) == "Actuals · Budget · 2026-01 to 2026-08 · department: D1, D2 · cost_centre · € thousands · 125 ms · Data as at 2026-10-22 18:00 UTC"
    with pytest.raises(ValueError, match="cell text limit"):
        caption_text({"period":"x" * 32768})


@pytest.mark.parametrize("fixture_name", ["aggregate_statement_result", "grouped_statement_result"])
@pytest.mark.parametrize("include_title", [False, True])
def test_report_exports_and_live_projection_share_values_formats_and_boundaries(tmp_path, monkeypatch, request, fixture_name, include_title):
    result = request.getfixturevalue(fixture_name)
    monkeypatch.setenv("PRECIS_BASE_URL", "https://finance.example/")
    result["rows"][0]["values"]["Actuals"] = 1.23456789012345
    source = {"tool":"run_statement", "params":{"statement":"pnl", "scale":3, "decimals":None}}
    path = tmp_path / "shared.xlsx"
    statement_to_excel(result, path, meta_source=source, sheet_name="Finance's report", include_title=include_title)
    wb = openpyxl.load_workbook(path)
    ws = wb["Finance's report"]
    region = read_meta(wb)["sheets"][ws.title]["regions"][0]
    _, ref = next(wb.defined_names[region["range_name"]].destinations)
    c1,r1,c2,r2 = openpyxl.utils.range_boundaries(ref)
    projection = report_worksheet(build_financial_table_block(result, caption=result["caption"]))
    actual = list(ws.iter_rows(min_row=r1,max_row=r2,min_col=c1,max_col=c2))
    # Static grouped headers merge the label vertically; the live spill cannot.
    values = [[c.value if c.value is not None else "" for c in row] for row in actual]
    if projection["header_rows"] == 2:
        values[1][0],values[0][0] = values[0][0],""
    assert values == projection["grid"]
    assert region["shape"]["row_count"] == projection["data_rows"]
    assert region["shape"]["footer_rows"] == 1
    assert region["shape"]["header_row"] == 1
    assert region["origin"] == "https://finance.example"
    assert "caption_mode" not in region["presentation"]
    assert not ws.cell(r2,1).alignment.wrap_text
    assert ws.cell(r2,1).number_format == "General"
    assert ws.cell(r2,1).value == projection["grid"][-1][0]
    assert r1 == (3 if include_title else 1)
    if include_title:
        assert ws["A1"].value == result["caption"]["description"]
    body = actual[projection["header_rows"]:-1]
    assert [[c.number_format for c in row] for row in body] == projection["number_formats"][projection["header_rows"]:-1]
    original = region.copy()
    statement_to_excel(result,path,meta_source=source,target="append",sheet_name="Another report",include_title=not include_title)
    appended = openpyxl.load_workbook(path)
    assert read_meta(appended)["sheets"][ws.title]["regions"][0] == original


@pytest.mark.parametrize("grouped", [False, True])
def test_shared_excel_rows_preserve_semantic_blanks_zeroes_and_totals(tmp_path, grouped):
    from copy import deepcopy
    from precis_mcp.excel.block_emit import write_block_to_sheet
    block = {"caption":{"period":"2026-01 to 2026-03"},
             "columns":[{"key":"label","label":"Line"},{"key":"value","label":"Actuals",**({"group":"Metric"} if grouped else {})}],
             "rows":[{"row_type":"separator"},{"row_type":"group_header","label":"Group","values":{}},
                     {"row_type":"group_header","label":"","values":{}},
                     {"row_type":"line_item","label":"Missing","values":{}},
                     {"row_type":"separator"},{"row_type":"line_item","label":"Zero","values":{"value":0}},
                     {"row_type":"subtotal","label":"Subtotal","values":{"value":0}},
                     {"row_type":"total","label":"Total","values":{"value":12.3456789}},{"row_type":"separator"}]}
    original = deepcopy(block)
    wb = openpyxl.Workbook()
    shape = write_block_to_sheet(wb.active,block,include_title=False)
    path = tmp_path / "rows.xlsx"; wb.save(path)
    ws = openpyxl.load_workbook(path).active
    body = list(ws.iter_rows(min_row=shape["data_start_row"],max_row=ws.max_row-1))
    assert len(body) == shape["row_count"] == 6
    assert all(c.value is None for c in body[1])
    assert body[3][1].value == 0 and body[3][1].data_type == "n"
    assert body[4][0].font.bold and body[5][0].font.bold
    assert body[5][0].border.bottom.style is not None
    assert ws.cell(ws.max_row,1).value == "2026-01 to 2026-03"
    assert block == original


@pytest.mark.parametrize("tool", ["run_statement", "run_metric"])
@pytest.mark.parametrize("include_title", [False, True])
def test_financial_snapshot_has_one_footer_and_optional_external_title(tmp_path, aggregate_statement_result, tool, include_title):
    from precis_mcp.excel.snapshot_export import write_snapshot_xlsx
    result = aggregate_statement_result
    source = {"tool":tool,"args":{"statement":"pnl"} if tool=="run_statement" else {"metrics":["revenue"]}}
    path = tmp_path / "snapshot.xlsx"
    write_snapshot_xlsx(result,source,path,include_title=include_title)
    wb = openpyxl.load_workbook(path); ws=wb.worksheets[0]
    region=read_meta(wb)["sheets"][ws.title]["regions"][0]
    assert region["source"]["tool"] == tool
    assert region["shape"]["footer_rows"] == 1
    assert "caption_mode" not in region["presentation"]
    assert not ws.cell(ws.max_row,1).alignment.wrap_text


@pytest.mark.parametrize("writer", [statement_to_excel, metric_to_excel])
@pytest.mark.parametrize("include_title", [False, True])
def test_empty_financial_report_has_no_data_marker(tmp_path, writer, include_title):
    path = tmp_path/"empty.xlsx"
    writer({"scenarios":[],"rows":[],"caption":{"description":"Empty report"}},path,include_title=include_title)
    ws=openpyxl.load_workbook(path).active
    assert ws.cell(1,1).value == ("Empty report" if include_title else "No data")
    assert ws.cell(ws.max_row,1).value == "No data"


def _unified_item(line: dict, separator_above: bool = False) -> dict:
    return {
        "key": line.get("key", ""),
        "label": line.get("label", line.get("key", "")),
        "style": line.get("style", "default"),
        "indent": line.get("indent", 0),
        "format": line.get("format", "currency"),
        "variance_effect": line.get("variance_effect", "natural"),
        "separator_above": separator_above or line.get("separator_above", False),
    }


def _unify_result(old: dict, kind: str = "statement") -> dict:
    """Convert legacy Excel fixtures to the unified engine result schema."""
    meta = old.get("metadata", {})
    aliases = list(meta.get("blocks", []))
    column_meta = list(meta.get("column_meta", []))
    scenarios = [
        {
            # Matches the production formatter: normal scenario columns carry no
            # format (per-row metric format drives the cell), only variance-%
            # columns would set "percent". A blanket "currency" here would
            # wrongly shadow percent line items.
            "alias": alias,
            "format": None,
            "variance": bool(column_meta[i].get("variance")) if i < len(column_meta) else False,
        }
        for i, alias in enumerate(aliases)
    ]
    dimensions = list(meta.get("dimensions", []))
    rows: list[dict] = []

    if old.get("groups"):
        pending_separator = False
        for agg in old.get("aggregate", []):
            if agg.get("separator"):
                pending_separator = True
                continue
            rows.append({
                "grain": "grand_total",
                "dimensions": {},
                "item": _unified_item(agg, pending_separator),
                "values": agg.get("values", {}),
            })
            pending_separator = False
        for group in old["groups"]:
            pending_separator = False
            for line in group.get("lines", []):
                if line.get("separator"):
                    pending_separator = True
                    continue
                rows.append({
                    "grain": "detail",
                    "dimensions": dict(group.get("dimension_values", {})),
                    "item": _unified_item(line, pending_separator),
                    "values": line.get("values", {}),
                })
                pending_separator = False
    else:
        pending_separator = False
        for line in old.get("rows", []):
            if line.get("separator"):
                pending_separator = True
                continue
            dim_values = dict(line.get("dimension_values", {}))
            rows.append({
                "grain": "detail",
                "dimensions": dim_values,
                "item": _unified_item(line, pending_separator),
                "values": line.get("values", {}),
            })
            pending_separator = False

    out = {
        "kind": kind,
        "dimensions": dimensions,
        "scenarios": scenarios,
        "period": {"start": meta.get("period_start"), "end": meta.get("period_end")},
        "scale": meta.get("scale", 0),
        "caption": old.get("caption"),
        "rows": rows,
    }
    if meta.get("decimals") is not None:
        out["decimals"] = meta["decimals"]
    return out


# ---------------------------------------------------------------------------
# Fixtures — engine result structures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_caption():
    return {
        "description": "Full P&L with KPIs",
        "scenarios": ["Actuals", "Budget", "Variance"],
        "period": "2026-01 to 2026-12",
        "filters": {"org_structure": "dept:Cloud & Infrastructure"},
        "scale": "€ thousands",
        "generated_at": "2026-03-30 08:11 UTC",
    }


@pytest.fixture
def aggregate_statement_result(sample_caption):
    return _unify_result({
        "metadata": {
            "period_start": "2026-01",
            "period_end": "2026-12",
            "dimensions": [],
            "blocks": ["Actuals", "Budget"],
            "block_scenarios": ["actuals", "budget"],
            "scale": 0,
            "decimals": None,
        },
        "caption": sample_caption,
        "rows": [
            {
                "key": "revenue",
                "label": "Revenue",
                "format": "currency",
                "style": "header",
                "indent": 0,
                "values": {"Actuals": 5000000, "Budget": 4800000},
            },
            {
                "key": "direct_cost",
                "label": "Direct Costs",
                "format": "currency",
                "style": "default",
                "indent": 0,
                "values": {"Actuals": 2100000, "Budget": 2000000},
            },
            {"separator": True},
            {
                "key": "gross_profit",
                "label": "Gross Profit",
                "format": "currency",
                "style": "subtotal",
                "indent": 0,
                "values": {"Actuals": 2900000, "Budget": 2800000},
            },
            {
                "key": "gp_margin",
                "label": "GP Margin %",
                "format": "percent",
                "style": "ratio",
                "indent": 1,
                "values": {"Actuals": 58.0, "Budget": 58.3},
            },
        ],
    })


@pytest.fixture
def grouped_statement_result(sample_caption):
    return _unify_result({
        "metadata": {
            "period_start": "2026-01",
            "period_end": "2026-12",
            "dimensions": ["cost_centre"],
            "blocks": ["Actuals"],
            "block_scenarios": ["actuals"],
            "scale": 3,
            "decimals": None,
        },
        "caption": sample_caption,
        "groups": [
            {
                "dimension_values": {"cost_centre": "Cloud & Infrastructure"},
                "lines": [
                    {
                        "key": "revenue",
                        "label": "Revenue",
                        "format": "currency",
                        "style": "header",
                        "indent": 0,
                        "values": {"Actuals": 1200},
                    },
                    {
                        "key": "direct_cost",
                        "label": "Direct Costs",
                        "format": "currency",
                        "style": "default",
                        "indent": 0,
                        "values": {"Actuals": 500},
                    },
                ],
            },
            {
                "dimension_values": {"cost_centre": "Data Analytics"},
                "lines": [
                    {
                        "key": "revenue",
                        "label": "Revenue",
                        "format": "currency",
                        "style": "header",
                        "indent": 0,
                        "values": {"Actuals": 800},
                    },
                    {
                        "key": "direct_cost",
                        "label": "Direct Costs",
                        "format": "currency",
                        "style": "default",
                        "indent": 0,
                        "values": {"Actuals": 350},
                    },
                ],
            },
        ],
        "aggregate": [
            {
                "key": "revenue",
                "label": "Revenue",
                "format": "currency",
                "style": "header",
                "indent": 0,
                "values": {"Actuals": 2000},
            },
            {
                "key": "direct_cost",
                "label": "Direct Costs",
                "format": "currency",
                "style": "default",
                "indent": 0,
                "values": {"Actuals": 850},
            },
        ],
    })


@pytest.fixture
def metric_result(sample_caption):
    return _unify_result({
        "metadata": {
            "period_start": "2026-01",
            "period_end": "2026-06",
            "dimensions": ["cost_centre", "period"],
            "blocks": ["Actuals", "Budget"],
            "block_scenarios": ["actuals", "budget"],
            "scale": 0,
            "decimals": None,
        },
        "caption": sample_caption,
        "rows": [
            {
                "dimension_values": {"cost_centre": "CC-SENG-01", "period": "2026-01"},
                "values": {"Actuals": 120000, "Budget": 115000},
                "format": "currency",
            },
            {
                "dimension_values": {"cost_centre": "CC-SENG-01", "period": "2026-02"},
                "values": {"Actuals": 125000, "Budget": 115000},
                "format": "currency",
            },
            {
                "dimension_values": {"cost_centre": "CC-SALES-01", "period": "2026-01"},
                "values": {"Actuals": 95000, "Budget": 100000},
                "format": "currency",
            },
        ],
    }, kind="metric")


@pytest.fixture
def hierarchy_result():
    return {
        "records": [
            {
                "dimension": "cost_centre",
                "code": "CC-SENG-01",
                "display_name": "Software Engineering",
                "department": "Engineering",
            },
            {
                "dimension": "cost_centre",
                "code": "CC-CLOUD-01",
                "display_name": "Cloud Team",
                "department": "Engineering",
            },
        ],
        "hierarchy_nodes": [
            {
                "dimension": "cost_centre",
                "hierarchy": "org_structure",
                "node_id": "dept:Engineering",
                "node_name": "Engineering",
                "display_name": "Engineering",
                "node_type": "department",
                "filter_usage": 'filters: {"org_structure": "dept:Engineering"}',
            },
        ],
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load(path: Path) -> openpyxl.Workbook:
    return openpyxl.load_workbook(path)


def _active(wb: openpyxl.Workbook) -> Any:
    """Get active sheet with type narrowing."""
    ws = wb.active
    assert ws is not None
    return ws


def _find_header_row(ws) -> int:
    """Find the column-header row (first row with 'Line Item' or the dark header fill).

    When no caption is present, returns 1. With caption rows, returns the row
    after the blank separator.
    """
    for r in range(1, ws.max_row + 1):
        cell = ws.cell(row=r, column=1)
        if cell.value == "Line Item":
            return r
        # Metric sheets don't have "Line Item" — detect by header fill
        if cell.fill and cell.fill.fgColor and cell.fill.fgColor.rgb and str(cell.fill.fgColor.rgb) == "000F172A":
            return r
    return 1


def _col_headers(ws) -> list[str]:
    """Return header row values as a list (handles caption offset)."""
    hr = _find_header_row(ws)
    return [cell.value for cell in ws[hr]]


def _two_tier_col(ws, hr: int, group_label: str, leaf_label: str) -> int:
    """1-based column index for a (group, leaf) pair in a two-tier header.

    The group label is written once and merged across its leaf columns, so the
    top row is forward-filled before matching. `hr` is the top (group) row; leaf
    labels sit on `hr + 1`.
    """
    top = [c.value for c in ws[hr]]
    leaf = [c.value for c in ws[hr + 1]]
    filled, last = [], None
    for v in top:
        if v is not None:
            last = v
        filled.append(last)
    for i, (g, l) in enumerate(zip(filled, leaf)):
        if g == group_label and l == leaf_label:
            return i + 1
    raise AssertionError(f"({group_label!r}, {leaf_label!r}) not found in two-tier header")


# ===========================================================================
# statement_to_excel — aggregate (no dimensions)
# ===========================================================================

class TestStatementAggregate:

    def test_columns_match_blocks(self, tmp_path, aggregate_statement_result):
        path = tmp_path / "stmt.xlsx"
        statement_to_excel(aggregate_statement_result, path)
        wb = _load(path)
        ws = _active(wb)
        headers = _col_headers(ws)
        # First column is the block's label column, then block aliases.
        assert headers[0] == "Line"
        assert "Actuals" in headers
        assert "Budget" in headers

    def test_row_count(self, tmp_path, aggregate_statement_result):
        path = tmp_path / "stmt.xlsx"
        statement_to_excel(aggregate_statement_result, path)
        wb = _load(path)
        ws = _active(wb)
        hr = _find_header_row(ws)
        # Decorative separator rows are omitted, matching the live spill.
        data_rows = ws.max_row - hr - 1
        assert data_rows == 4

    def test_header_style_bold(self, tmp_path, aggregate_statement_result):
        """Rows with style='header' should have bold font."""
        path = tmp_path / "stmt.xlsx"
        statement_to_excel(aggregate_statement_result, path)
        wb = _load(path)
        ws = _active(wb)
        hr = _find_header_row(ws)
        # First data row after column header (Revenue, style=header)
        label_cell = ws.cell(row=hr + 1, column=1)
        assert label_cell.font.bold is True

    def test_subtotal_style(self, tmp_path, aggregate_statement_result):
        """Rows with style='subtotal' should have bold + light background."""
        path = tmp_path / "stmt.xlsx"
        statement_to_excel(aggregate_statement_result, path)
        wb = _load(path)
        ws = _active(wb)
        hr = _find_header_row(ws)
        # Rows: Revenue, Direct Costs, Gross Profit, GP Margin %.
        gp_cell = ws.cell(row=hr + 3, column=1)
        assert gp_cell.font.bold is True
        assert gp_cell.fill.patternType is not None

    def test_total_style_bold_with_bg(self, tmp_path):
        """Rows with style='total' should have bold + medium background."""
        result = {
            "metadata": {
                "period_start": "2026-01", "period_end": "2026-12",
                "dimensions": [], "blocks": ["Actuals"],
                "block_scenarios": ["actuals"], "scale": 0, "decimals": None,
            },
            "rows": [
                {"key": "net_income", "label": "Net Income", "format": "currency",
                 "style": "total", "indent": 0, "values": {"Actuals": 1000000}},
            ],
        }
        path = tmp_path / "total.xlsx"
        statement_to_excel(_unify_result(result), path)
        wb = _load(path)
        ws = _active(wb)
        hr = _find_header_row(ws)
        cell = ws.cell(row=hr + 1, column=1)
        assert cell.font.bold is True
        assert cell.fill.patternType is not None

    def test_indent_alignment(self, tmp_path, aggregate_statement_result):
        """Rows with indent > 0 should have openpyxl alignment indent set."""
        path = tmp_path / "stmt.xlsx"
        statement_to_excel(aggregate_statement_result, path)
        wb = _load(path)
        ws = _active(wb)
        hr = _find_header_row(ws)
        # GP Margin % has indent=1; it is the fourth semantic data row.
        gp_margin_cell = ws.cell(row=hr + 4, column=1)
        assert gp_margin_cell.alignment.indent is not None
        assert gp_margin_cell.alignment.indent >= 1

    def test_currency_number_format(self, tmp_path, aggregate_statement_result):
        """Currency values should use en-dash negative format."""
        path = tmp_path / "stmt.xlsx"
        statement_to_excel(aggregate_statement_result, path)
        wb = _load(path)
        ws = _active(wb)
        hr = _find_header_row(ws)
        headers = _col_headers(ws)
        actuals_col = headers.index("Actuals") + 1
        revenue_val = ws.cell(row=hr + 1, column=actuals_col)
        assert revenue_val.value == 5000000
        assert "#,##0" in revenue_val.number_format
        assert "\u2013" in revenue_val.number_format

    def test_percent_number_format(self, tmp_path, aggregate_statement_result):
        """Percent values should use percent format with en-dash negatives."""
        path = tmp_path / "stmt.xlsx"
        statement_to_excel(aggregate_statement_result, path)
        wb = _load(path)
        ws = _active(wb)
        hr = _find_header_row(ws)
        headers = _col_headers(ws)
        actuals_col = headers.index("Actuals") + 1
        pct_cell = ws.cell(row=hr + 4, column=actuals_col)
        assert "%" in pct_cell.number_format
        assert "\u2013" in pct_cell.number_format

    def test_decorative_separator_does_not_consume_a_row(self, tmp_path, aggregate_statement_result):
        """Subtotal follows detail directly; its border preserves emphasis."""
        path = tmp_path / "stmt.xlsx"
        statement_to_excel(aggregate_statement_result, path)
        wb = _load(path)
        ws = _active(wb)
        hr = _find_header_row(ws)
        subtotal = ws.cell(row=hr + 3, column=1)
        assert subtotal.value == "Gross Profit"
        assert subtotal.border.top.style is not None

    def test_freeze_panes(self, tmp_path, aggregate_statement_result):
        """Freeze panes should be set below header row + after label column."""
        path = tmp_path / "stmt.xlsx"
        statement_to_excel(aggregate_statement_result, path)
        wb = _load(path)
        ws = _active(wb)
        hr = _find_header_row(ws)
        assert ws.freeze_panes is not None
        assert ws.freeze_panes == f"B{hr + 1}"

    def test_returns_path(self, tmp_path, aggregate_statement_result):
        path = tmp_path / "stmt.xlsx"
        result = statement_to_excel(aggregate_statement_result, path)
        assert result == path
        assert path.exists()


# ===========================================================================
# statement_to_excel — with dimensions (groups)
# ===========================================================================

class TestStatementGrouped:

    def test_total_column_first(self, tmp_path, grouped_statement_result):
        """Total column should appear first (from grand-total grain)."""
        path = tmp_path / "grouped.xlsx"
        statement_to_excel(grouped_statement_result, path)
        wb = _load(path)
        ws = _active(wb)
        headers = _col_headers(ws)
        assert headers[:2] == ["Line", "Total"]

    def test_all_groups_present(self, tmp_path, grouped_statement_result):
        """Each group's dimension value should appear as a crosstab column."""
        path = tmp_path / "grouped.xlsx"
        statement_to_excel(grouped_statement_result, path)
        wb = _load(path)
        ws = _active(wb)
        header_text = " | ".join(str(v) for v in _col_headers(ws))
        assert "Cloud & Infrastructure" in header_text
        assert "Data Analytics" in header_text

    def test_group_lines_present(self, tmp_path, grouped_statement_result):
        """Lines within each group should be present."""
        path = tmp_path / "grouped.xlsx"
        statement_to_excel(grouped_statement_result, path)
        wb = _load(path)
        ws = _active(wb)
        hr = _find_header_row(ws)
        all_labels = [
            ws.cell(row=r, column=1).value
            for r in range(hr + 1, ws.max_row + 1)
        ]
        revenue_count = sum(1 for v in all_labels if v == "Revenue")
        assert revenue_count == 1

    def test_aggregate_values_in_total(self, tmp_path, grouped_statement_result):
        """Total column should use grand-total grain values."""
        path = tmp_path / "grouped.xlsx"
        statement_to_excel(grouped_statement_result, path)
        wb = _load(path)
        ws = _active(wb)
        hr = _find_header_row(ws)
        headers = _col_headers(ws)
        actuals_col = headers.index("Total") + 1
        for r in range(hr + 1, ws.max_row + 1):
            if ws.cell(row=r, column=1).value == "Revenue":
                val = ws.cell(row=r, column=actuals_col).value
                assert val == 2000  # aggregate revenue
                break


# ===========================================================================
# statement_to_excel — period cross-tabulation
# ===========================================================================

class TestStatementPeriodCrossTab:
    """When groups represent periods, the Excel should cross-tabulate:
    rows = statement lines, columns = Total + period × scenario."""

    @pytest.fixture
    def period_grouped_result(self, sample_caption):
        return _unify_result({
            "metadata": {
                "period_start": "2026-01",
                "period_end": "2026-03",
                "dimensions": ["period"],
                "blocks": ["Actuals", "Budget"],
                "block_scenarios": ["actuals", "budget"],
                "scale": 3,
                "decimals": None,
            },
            "caption": sample_caption,
            "groups": [
                {
                    "dimension_values": {"period": "2026-01"},
                    "lines": [
                        {"key": "revenue", "label": "Revenue", "format": "currency",
                         "style": "header", "indent": 0,
                         "values": {"Actuals": 120, "Budget": 135}},
                        {"key": "direct_cost", "label": "Direct Costs", "format": "currency",
                         "style": "default", "indent": 0,
                         "values": {"Actuals": 170, "Budget": 169}},
                        {"separator": True},
                        {"key": "gp_margin", "label": "GP Margin %", "format": "percent",
                         "style": "ratio", "indent": 1,
                         "values": {"Actuals": -41.7, "Budget": -25.2}},
                    ],
                },
                {
                    "dimension_values": {"period": "2026-02"},
                    "lines": [
                        {"key": "revenue", "label": "Revenue", "format": "currency",
                         "style": "header", "indent": 0,
                         "values": {"Actuals": 168, "Budget": 132}},
                        {"key": "direct_cost", "label": "Direct Costs", "format": "currency",
                         "style": "default", "indent": 0,
                         "values": {"Actuals": 165, "Budget": 169}},
                        {"separator": True},
                        {"key": "gp_margin", "label": "GP Margin %", "format": "percent",
                         "style": "ratio", "indent": 1,
                         "values": {"Actuals": 1.8, "Budget": -28.0}},
                    ],
                },
            ],
            "aggregate": [
                {"key": "revenue", "label": "Revenue", "format": "currency",
                 "style": "header", "indent": 0,
                 "values": {"Actuals": 288, "Budget": 267}},
                {"key": "direct_cost", "label": "Direct Costs", "format": "currency",
                 "style": "default", "indent": 0,
                 "values": {"Actuals": 335, "Budget": 338}},
                {"separator": True},
                {"key": "gp_margin", "label": "GP Margin %", "format": "percent",
                 "style": "ratio", "indent": 1,
                 "values": {"Actuals": -16.3, "Budget": -26.6}},
            ],
        })

    def test_columns_are_cross_tabulated(self, tmp_path, period_grouped_result):
        """Two-tier header: dim-combo (Total, periods) on top, scenario beneath."""
        path = tmp_path / "period.xlsx"
        statement_to_excel(period_grouped_result, path)
        wb = _load(path)
        ws = _active(wb)
        hr = _find_header_row(ws)
        group_row = [c.value for c in ws[hr] if c.value is not None]
        leaf_row = [c.value for c in ws[hr + 1] if c.value is not None]
        assert group_row[0] == "Line"
        # top row carries the dim-combos (Total first, then periods)…
        assert "Total" in group_row and "2026-01" in group_row and "2026-02" in group_row
        # …and the scenarios sit on the leaf row beneath, not flattened into the top
        assert "Actuals" in leaf_row and "Budget" in leaf_row

    def test_revenue_appears_once(self, tmp_path, period_grouped_result):
        """Revenue should appear as a single row, not once per period."""
        path = tmp_path / "period.xlsx"
        statement_to_excel(period_grouped_result, path)
        wb = _load(path)
        ws = _active(wb)
        hr = _find_header_row(ws)
        all_labels = [
            ws.cell(row=r, column=1).value
            for r in range(hr + 1, ws.max_row + 1)
        ]
        revenue_count = sum(1 for v in all_labels if v == "Revenue")
        assert revenue_count == 1, f"Revenue should appear once, found {revenue_count}"

    def test_total_values_from_aggregate(self, tmp_path, period_grouped_result):
        """Total column should use aggregate data, not sum of periods."""
        path = tmp_path / "period.xlsx"
        statement_to_excel(period_grouped_result, path)
        wb = _load(path)
        ws = _active(wb)
        hr = _find_header_row(ws)
        # Total / Actuals column; data starts two rows below the two-tier header.
        total_col = _two_tier_col(ws, hr, "Total", "Actuals")
        val = ws.cell(row=hr + 2, column=total_col).value
        assert val == 288

    def test_period_values_present(self, tmp_path, period_grouped_result):
        """Period columns should contain the correct values."""
        path = tmp_path / "period.xlsx"
        statement_to_excel(period_grouped_result, path)
        wb = _load(path)
        ws = _active(wb)
        hr = _find_header_row(ws)
        jan_col = _two_tier_col(ws, hr, "2026-01", "Actuals")
        val = ws.cell(row=hr + 2, column=jan_col).value
        assert val == 120

    def test_total_column_shaded_on_plain_rows(self, tmp_path, period_grouped_result):
        """The grand-total column gets a subtle fill on plain rows; other value
        columns on the same row stay unfilled."""
        path = tmp_path / "period.xlsx"
        statement_to_excel(period_grouped_result, path)
        wb = _load(path)
        ws = _active(wb)
        hr = _find_header_row(ws)
        total_col = _two_tier_col(ws, hr, "Total", "Actuals")
        jan_col = _two_tier_col(ws, hr, "2026-01", "Actuals")
        for r in range(hr + 2, ws.max_row + 1):
            if ws.cell(row=r, column=1).value == "Direct Costs":  # plain (style="default")
                assert ws.cell(row=r, column=total_col).fill.fill_type == "solid"
                assert ws.cell(row=r, column=jan_col).fill.fill_type in (None, "none")
                break
        else:
            raise AssertionError("Direct Costs row not found")

    def test_percent_values_retain_engine_units(self, tmp_path, period_grouped_result):
        """The literal percent suffix must not change numeric percentage units."""
        path = tmp_path / "period.xlsx"
        statement_to_excel(period_grouped_result, path)
        wb = _load(path)
        ws = _active(wb)
        hr = _find_header_row(ws)
        total_col = _two_tier_col(ws, hr, "Total", "Actuals")
        # GP Margin % row — find it (data starts below the two-tier header)
        for r in range(hr + 2, ws.max_row + 1):
            if ws.cell(row=r, column=1).value == "GP Margin %":
                val = ws.cell(row=r, column=total_col).value
                assert val == -16.3
                assert '"%"' in ws.cell(row=r, column=total_col).number_format
                break

    def test_single_scenario_no_alias_suffix(self, tmp_path, sample_caption):
        """With a single scenario, column headers should be just period names."""
        result = {
            "metadata": {
                "period_start": "2026-01", "period_end": "2026-02",
                "dimensions": ["period"],
                "blocks": ["Actuals"],
                "block_scenarios": ["actuals"],
                "scale": 3, "decimals": None,
            },
            "caption": sample_caption,
            "groups": [
                {"dimension_values": {"period": "2026-01"},
                 "lines": [{"key": "revenue", "label": "Revenue", "format": "currency",
                            "style": "default", "indent": 0, "values": {"Actuals": 120}}]},
                {"dimension_values": {"period": "2026-02"},
                 "lines": [{"key": "revenue", "label": "Revenue", "format": "currency",
                            "style": "default", "indent": 0, "values": {"Actuals": 168}}]},
            ],
            "aggregate": [
                {"key": "revenue", "label": "Revenue", "format": "currency",
                 "style": "default", "indent": 0, "values": {"Actuals": 288}},
            ],
        }
        path = tmp_path / "single_scen.xlsx"
        statement_to_excel(_unify_result(result), path)
        wb = _load(path)
        ws = _active(wb)
        headers = _col_headers(ws)
        # Single scenario: headers should be "Total", "2026-01", "2026-02" (no " — Actuals" suffix)
        assert "Total" in headers
        assert "2026-01" in headers
        assert "2026-02" in headers


# ===========================================================================
# statement_to_excel — None values
# ===========================================================================

class TestStatementNoneValues:

    def test_none_values_render_empty(self, tmp_path):
        result = {
            "metadata": {
                "period_start": "2026-01", "period_end": "2026-12",
                "dimensions": [], "blocks": ["Actuals", "Budget"],
                "block_scenarios": ["actuals", "budget"], "scale": 0, "decimals": None,
            },
            "rows": [
                {"key": "revenue", "label": "Revenue", "format": "currency",
                 "style": "default", "indent": 0,
                 "values": {"Actuals": 5000, "Budget": None}},
            ],
        }
        path = tmp_path / "none.xlsx"
        statement_to_excel(_unify_result(result), path)
        wb = _load(path)
        ws = _active(wb)
        hr = _find_header_row(ws)
        headers = _col_headers(ws)
        budget_col = headers.index("Budget") + 1
        cell = ws.cell(row=hr + 1, column=budget_col)
        assert cell.value is None


# ===========================================================================
# metric_to_excel — single metric with dimensions
# ===========================================================================

class TestMetricBasic:

    def test_label_and_value_columns(self, tmp_path, metric_result):
        """Mirrors the rendered table: a single nested label column (joined
        dimensions) followed by one value column per scenario."""
        path = tmp_path / "metric.xlsx"
        metric_to_excel(metric_result, path)
        wb = _load(path)
        ws = _active(wb)
        headers = _col_headers(ws)
        assert headers[0] == "Cost Centre / Period"
        assert "Actuals" in headers
        assert "Budget" in headers

    def test_row_count(self, tmp_path, metric_result):
        path = tmp_path / "metric.xlsx"
        metric_to_excel(metric_result, path)
        wb = _load(path)
        ws = _active(wb)
        hr = _find_header_row(ws)
        # Grouped by the leftmost dimension: 2 group headers (CC-SENG-01,
        # CC-SALES-01) + 3 indented detail rows = 5.
        assert ws.max_row - hr - 1 == 5

    def test_header_styling(self, tmp_path, metric_result):
        """Header row should have dark bg, white text, bold."""
        path = tmp_path / "metric.xlsx"
        metric_to_excel(metric_result, path)
        wb = _load(path)
        ws = _active(wb)
        hr = _find_header_row(ws)
        header_cell = ws.cell(row=hr, column=1)
        assert header_cell.font.bold is True
        assert header_cell.fill.patternType is not None
        assert header_cell.fill.fgColor is not None
        assert header_cell.font.color is not None

    def test_number_format_applied(self, tmp_path, metric_result):
        """Currency values should have proper number formatting."""
        path = tmp_path / "metric.xlsx"
        metric_to_excel(metric_result, path)
        wb = _load(path)
        ws = _active(wb)
        hr = _find_header_row(ws)
        headers = _col_headers(ws)
        actuals_col = headers.index("Actuals") + 1
        # hr+1 is the CC-SENG-01 group header; the first detail row (2026-01)
        # carries the value.
        val_cell = ws.cell(row=hr + 2, column=actuals_col)
        assert val_cell.value == 120000
        assert "#,##0" in val_cell.number_format

    def test_remaining_dimension_in_labels(self, tmp_path, metric_result):
        """The non-leftmost dimension (period) appears in the nested row labels."""
        path = tmp_path / "metric.xlsx"
        metric_to_excel(metric_result, path)
        wb = _load(path)
        ws = _active(wb)
        hr = _find_header_row(ws)
        labels = [
            ws.cell(row=r, column=1).value
            for r in range(hr + 1, ws.max_row + 1)
        ]
        assert "CC-SENG-01" in labels  # group header
        assert "2026-01" in labels     # indented detail
        assert "2026-02" in labels

    def test_data_values_correct(self, tmp_path, metric_result):
        path = tmp_path / "metric.xlsx"
        metric_to_excel(metric_result, path)
        wb = _load(path)
        ws = _active(wb)
        hr = _find_header_row(ws)
        headers = _col_headers(ws)
        actuals_col = headers.index("Actuals") + 1
        # hr+1: CC-SENG-01 group header; hr+2: its 2026-01 detail row.
        assert ws.cell(row=hr + 1, column=1).value == "CC-SENG-01"
        assert ws.cell(row=hr + 2, column=1).value == "2026-01"
        assert ws.cell(row=hr + 2, column=actuals_col).value == 120000

    def test_returns_path(self, tmp_path, metric_result):
        path = tmp_path / "metric.xlsx"
        result = metric_to_excel(metric_result, path)
        assert result == path
        assert path.exists()


# ===========================================================================
# metric_to_excel — multi-metric with groups
# ===========================================================================

class TestMetricGrouped:

    def test_grouped_metric_metrics_as_columns(self, tmp_path):
        """Multi-metric results put each metric in its own column."""
        result = {
            "metadata": {
                "period_start": "2026-01", "period_end": "2026-06",
                "dimensions": ["cost_centre"],
                "blocks": ["Actuals"],
                "block_scenarios": ["actuals"],
                "scale": 0, "decimals": None,
            },
            "groups": [
                {
                    "dimension_values": {"cost_centre": "CC-SENG-01"},
                    "lines": [
                        {"key": "revenue", "label": "Revenue",
                         "values": {"Actuals": 500000}, "format": "currency"},
                        {"key": "hours_worked", "label": "Hours Worked",
                         "values": {"Actuals": 1200}, "format": "number"},
                    ],
                },
                {
                    "dimension_values": {"cost_centre": "CC-SALES-01"},
                    "lines": [
                        {"key": "revenue", "label": "Revenue",
                         "values": {"Actuals": 300000}, "format": "currency"},
                        {"key": "hours_worked", "label": "Hours Worked",
                         "values": {"Actuals": 800}, "format": "number"},
                    ],
                },
            ],
        }
        path = tmp_path / "grouped_metric.xlsx"
        metric_to_excel(_unify_result(result, kind="metric"), path)
        wb = _load(path)
        ws = _active(wb)
        hr = _find_header_row(ws)
        headers = _col_headers(ws)
        # Single scenario → each metric is its own column; the dimension is the
        # nested label column. One row per dimension value (not per metric).
        assert headers[0] == "Cost Centre"
        assert "Revenue" in headers
        assert "Hours Worked" in headers
        assert ws.max_row - hr - 1 == 2


# ===========================================================================
# hierarchy_to_excel
# ===========================================================================

class TestHierarchy:

    def test_two_sheets_created(self, tmp_path, hierarchy_result):
        path = tmp_path / "hier.xlsx"
        hierarchy_to_excel(hierarchy_result, path)
        wb = _load(path)
        user_sheets = [n for n in wb.sheetnames if not n.startswith("_precis_")]
        assert len(user_sheets) == 2
        assert "Records" in user_sheets
        assert "Hierarchy Nodes" in user_sheets

    def test_records_headers(self, tmp_path, hierarchy_result):
        path = tmp_path / "hier.xlsx"
        hierarchy_to_excel(hierarchy_result, path)
        wb = _load(path)
        ws = wb["Records"]
        headers = _col_headers(ws)
        assert "dimension" in headers
        assert "code" in headers
        assert "display_name" in headers
        assert "department" in headers

    def test_records_data(self, tmp_path, hierarchy_result):
        path = tmp_path / "hier.xlsx"
        hierarchy_to_excel(hierarchy_result, path)
        wb = _load(path)
        ws = wb["Records"]
        # 2 records + 1 header
        assert ws.max_row == 3
        # Verify first record
        assert ws.cell(row=2, column=1).value == "cost_centre"
        assert ws.cell(row=2, column=2).value == "CC-SENG-01"

    def test_hierarchy_nodes_headers(self, tmp_path, hierarchy_result):
        path = tmp_path / "hier.xlsx"
        hierarchy_to_excel(hierarchy_result, path)
        wb = _load(path)
        ws = wb["Hierarchy Nodes"]
        headers = _col_headers(ws)
        expected = {"dimension", "hierarchy", "node_id", "node_name",
                    "display_name", "node_type", "filter_usage"}
        assert expected.issubset(set(headers))

    def test_hierarchy_nodes_data(self, tmp_path, hierarchy_result):
        path = tmp_path / "hier.xlsx"
        hierarchy_to_excel(hierarchy_result, path)
        wb = _load(path)
        ws = wb["Hierarchy Nodes"]
        # 1 node + 1 header
        assert ws.max_row == 2
        headers = _col_headers(ws)
        node_id_col = headers.index("node_id") + 1
        assert ws.cell(row=2, column=node_id_col).value == "dept:Engineering"

    def test_returns_path(self, tmp_path, hierarchy_result):
        path = tmp_path / "hier.xlsx"
        result = hierarchy_to_excel(hierarchy_result, path)
        assert result == path
        assert path.exists()


# ===========================================================================
# Caption metadata rows
# ===========================================================================

class TestCaption:
    @pytest.mark.parametrize("writer,fixture", [(statement_to_excel,"aggregate_statement_result"),(metric_to_excel,"metric_result")])
    def test_context_is_one_unwrapped_footer_and_title_is_separate(self,tmp_path,request,writer,fixture):
        result=request.getfixturevalue(fixture)
        path=tmp_path/"caption.xlsx"; writer(result,path)
        ws=_active(_load(path))
        footer=ws.cell(ws.max_row,1)
        assert not footer.alignment.wrap_text
        assert ws.row_dimensions[ws.max_row].height is None
        for value in ("Actuals","2026-01 to 2026-12","Cloud & Infrastructure","€ thousands","Data as at"):
            assert value in footer.value
        assert ws["A1"].value == "Full P&L with KPIs"
        assert all(ws.cell(ws.max_row,c).value is None for c in range(2,ws.max_column+1))


# ===========================================================================
# Edge cases
# ===========================================================================

class TestEdgeCases:

    def test_empty_statement_result(self, tmp_path):
        """Empty result should produce a valid workbook with a 'No data' marker."""
        result = {
            "metadata": {
                "period_start": "2026-01", "period_end": "2026-12",
                "dimensions": [], "blocks": ["Actuals"],
                "block_scenarios": ["actuals"], "scale": 0, "decimals": None,
            },
            "rows": [],
        }
        path = tmp_path / "empty.xlsx"
        statement_to_excel(_unify_result(result), path)
        wb = _load(path)
        ws = _active(wb)
        # No rows → the shared writer emits a single "No data" cell.
        assert ws.max_row >= 1
        assert ws.cell(row=ws.max_row, column=1).value == "No data"

    def test_empty_metric_result(self, tmp_path):
        """Empty metric result should produce valid workbook with 'No data' message."""
        result = {
            "metadata": {
                "period_start": "2026-01", "period_end": "2026-06",
                "dimensions": ["cost_centre"],
                "blocks": ["Actuals"],
                "block_scenarios": ["actuals"],
                "scale": 0, "decimals": None,
            },
            "rows": [],
        }
        path = tmp_path / "empty_metric.xlsx"
        metric_to_excel(_unify_result(result, kind="metric"), path)
        wb = _load(path)
        ws = _active(wb)
        # Should have at least a "No data" cell
        assert ws.max_row >= 1

    def test_empty_hierarchy_result(self, tmp_path):
        """Empty hierarchy result should produce valid workbook with sheets."""
        result = {"records": [], "hierarchy_nodes": []}
        path = tmp_path / "empty_hier.xlsx"
        hierarchy_to_excel(result, path)
        wb = _load(path)
        user_sheets = [n for n in wb.sheetnames if not n.startswith("_precis_")]
        assert len(user_sheets) == 2

    def test_single_row_statement(self, tmp_path):
        result = {
            "metadata": {
                "period_start": "2026-01", "period_end": "2026-12",
                "dimensions": [], "blocks": ["Actuals"],
                "block_scenarios": ["actuals"], "scale": 0, "decimals": None,
            },
            "rows": [
                {"key": "revenue", "label": "Revenue", "format": "currency",
                 "style": "default", "indent": 0, "values": {"Actuals": 100}},
            ],
        }
        path = tmp_path / "single.xlsx"
        statement_to_excel(_unify_result(result), path)
        wb = _load(path)
        ws = _active(wb)
        hr = _find_header_row(ws)
        assert ws.max_row == hr + 2  # header + data + footer
        assert ws.cell(row=hr + 1, column=1).value == "Revenue"

    def test_long_sheet_name_truncated(self, tmp_path):
        """Sheet names exceeding 31 chars should be truncated."""
        result = {
            "metadata": {
                "period_start": "2026-01", "period_end": "2026-12",
                "dimensions": [], "blocks": ["Actuals"],
                "block_scenarios": ["actuals"], "scale": 0, "decimals": None,
                # Some implementations derive sheet name from metadata
            },
            "rows": [
                {"key": "a_very_long_metric_key_that_exceeds_limit",
                 "label": "A Very Long Metric Key That Exceeds The Thirty One Character Limit",
                 "format": "currency", "style": "default", "indent": 0,
                 "values": {"Actuals": 100}},
            ],
        }
        path = tmp_path / "long_name.xlsx"
        statement_to_excel(_unify_result(result), path)
        wb = _load(path)
        for name in wb.sheetnames:
            assert len(name) <= 31, f"Sheet name '{name}' exceeds 31 chars"


# ---------------------------------------------------------------------------
# Variance font coloring
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("grouped", [False, True])
@pytest.mark.parametrize("include_title", [False, True])
def test_report_source_strings_are_literal_and_numbers_keep_their_type(tmp_path, grouped, include_title):
    from precis_mcp.excel.block_emit import write_block_to_sheet

    wb = openpyxl.Workbook()
    ws = wb.active
    block = {
        "caption": {"description": "=1+1", "generated_at": "=2+2"},
        "columns": [
            {"key": "label", "label": "=3+3"},
            {"key": "value", "label": "=4+4", **({"group": "=5+5"} if grouped else {})},
        ],
        "rows": [
            {"label": "=6+6", "values": {"value": "=7+7"}},
            {"label": "-123", "values": {"value": -123.5}},
        ],
    }
    write_block_to_sheet(ws, block, include_title=include_title)
    path = tmp_path / "report.xlsx"
    wb.save(path)
    restored = openpyxl.load_workbook(path).active
    assert not any(cell.data_type == "f" for row in restored for cell in row)
    values = {cell.value: cell.data_type for row in restored for cell in row if cell.value is not None}
    for text in ("=3+3", "=4+4", "=6+6", "=7+7", "-123"):
        assert values[text] == "s"
    if include_title:
        assert values["=1+1"] == "s"
    else:
        assert "=1+1" not in values
    assert values["Data as at =2+2"] == "s"
    assert values[-123.5] == "n"
    if grouped:
        assert values["=5+5"] == "s"


def test_hierarchy_source_strings_are_literal(tmp_path):
    path = tmp_path / "hierarchy.xlsx"
    hierarchy_to_excel({
        "records": [{"=header": "=1+1", "code": "-42"}],
        "hierarchy_nodes": [{"node_name": "=2+2"}],
    }, path)
    wb = openpyxl.load_workbook(path)
    for sheet, address, expected in [
        ("Records", "A1", "=header"),
        ("Records", "A2", "=1+1"),
        ("Records", "B2", "-42"),
        ("Hierarchy Nodes", "D2", "=2+2"),
    ]:
        assert wb[sheet][address].value == expected
        assert wb[sheet][address].data_type == "s"


class TestVarianceFontColoring:
    """Variance columns get green/red font based on metric variance_effect."""

    def test_natural_metric_positive_variance_is_green(self, tmp_path):
        result = {
            "metadata": {
                "blocks": ["Actuals", "Variance"],
                "block_scenarios": ["actuals", "budget_variance"],
                "column_meta": [{"variance": False}, {"variance": True}],
            },
            "rows": [
                {"key": "revenue", "label": "Revenue", "format": "currency",
                 "style": "default", "indent": 0, "variance_effect": "natural",
                 "values": {"Actuals": 1000, "Variance": 50}},
            ],
        }
        path = tmp_path / "var.xlsx"
        statement_to_excel(_unify_result(result), path)
        wb = _load(path)
        ws = wb.active
        # Column C (index 3) is the Variance column
        cell = ws.cell(row=2, column=3)
        assert cell.font.color is not None
        # Green = 16A34A
        assert cell.font.color.rgb is not None
        assert "16A34A" in str(cell.font.color.rgb)

    def test_inverse_metric_positive_variance_is_red(self, tmp_path):
        result = {
            "metadata": {
                "blocks": ["Actuals", "Variance"],
                "block_scenarios": ["actuals", "budget_variance"],
                "column_meta": [{"variance": False}, {"variance": True}],
            },
            "rows": [
                {"key": "direct_cost", "label": "Direct Costs", "format": "currency",
                 "style": "default", "indent": 0, "variance_effect": "inverse",
                 "values": {"Actuals": 500, "Variance": 30}},
            ],
        }
        path = tmp_path / "var_inv.xlsx"
        statement_to_excel(_unify_result(result), path)
        wb = _load(path)
        ws = wb.active
        cell = ws.cell(row=2, column=3)
        assert cell.font.color is not None
        # Red = DC2626
        assert "DC2626" in str(cell.font.color.rgb)

    def test_neutral_metric_no_color(self, tmp_path):
        result = {
            "metadata": {
                "blocks": ["Actuals", "Variance"],
                "block_scenarios": ["actuals", "budget_variance"],
                "column_meta": [{"variance": False}, {"variance": True}],
            },
            "rows": [
                {"key": "avg_fte_total", "label": "Avg FTEs", "format": "number",
                 "style": "default", "indent": 0, "variance_effect": "neutral",
                 "values": {"Actuals": 60, "Variance": 5}},
            ],
        }
        path = tmp_path / "var_neutral.xlsx"
        statement_to_excel(_unify_result(result), path)
        wb = _load(path)
        ws = wb.active
        cell = ws.cell(row=2, column=3)
        # Default font — no explicit colour override
        color = cell.font.color
        if color is not None:
            # openpyxl default theme color or None
            assert color.rgb is None or "16A34A" not in str(color.rgb)

    def test_non_variance_column_no_color(self, tmp_path):
        result = {
            "metadata": {
                "blocks": ["Actuals", "Variance"],
                "block_scenarios": ["actuals", "budget_variance"],
                "column_meta": [{"variance": False}, {"variance": True}],
            },
            "rows": [
                {"key": "revenue", "label": "Revenue", "format": "currency",
                 "style": "default", "indent": 0, "variance_effect": "natural",
                 "values": {"Actuals": 1000, "Variance": 50}},
            ],
        }
        path = tmp_path / "var_nocolor.xlsx"
        statement_to_excel(_unify_result(result), path)
        wb = _load(path)
        ws = wb.active
        # Column B (index 2) is the Actuals column — should not have variance color
        cell = ws.cell(row=2, column=2)
        color = cell.font.color
        if color is not None:
            assert color.rgb is None or "16A34A" not in str(color.rgb)
