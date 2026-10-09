# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Tests for precis_mcp.excel.extract_emit (slice 4 / F3).

Covers:

* Flattening: statement no-dims (``rows`` with ``key``), metric with dims
  (``rows`` with ``dimension_values``), statement with dims (``groups``
  with ``lines``).
* Separator rows dropped from data and from ``row_identities``.
* Meta stamp: ``workbook_kind='extract'``, per-sheet ``kind='extract'``,
  full ``shape`` (layout, key/value columns, row_count, header_row,
  data_start_row, row_identities), ``named_ranges``, ``excel_tables``,
  ``refresh_policy``.
* Named ranges + Excel Table are actually written (round-trip through
  ``load_workbook``).
* ``refresh_policy`` defaults + overrides; unknown key/value raises.
* Layout validation (only ``role_indexed`` accepted).
* Empty results rejected (``ExtractError``).
* ``out`` stripped from persisted ``source.params``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from openpyxl import load_workbook

from precis_mcp.excel.extract_emit import (
    DEFAULT_REFRESH_POLICY,
    ExtractError,
    extract_to_excel,
)
from precis_mcp.excel.meta import META_SHEET_NAME, read_meta


def _item(line: dict) -> dict:
    return {
        "key": line.get("key", ""),
        "label": line.get("label", line.get("key", "")),
        "style": line.get("style", "default"),
        "indent": line.get("indent", 0),
        "format": line.get("format", "currency"),
        "variance_effect": line.get("variance_effect", "natural"),
        "separator_above": line.get("separator_above", False),
    }


def _unify(old: dict, kind: str) -> dict:
    meta = old.get("metadata", {})
    rows: list[dict] = []
    if old.get("groups"):
        for group in old["groups"]:
            for line in group.get("lines", []):
                if line.get("separator"):
                    continue
                rows.append({
                    "grain": "detail",
                    "dimensions": dict(group.get("dimension_values", {})),
                    "item": _item(line),
                    "values": line.get("values", {}),
                })
    else:
        for line in old.get("rows", []):
            if line.get("separator"):
                continue
            rows.append({
                "grain": "detail",
                "dimensions": dict(line.get("dimension_values", {})),
                "item": _item(line),
                "values": line.get("values", {}),
            })
    return {
        "kind": kind,
        "dimensions": list(meta.get("dimensions", [])),
        "scenarios": [
            {"alias": alias, "format": "currency", "variance": False}
            for alias in meta.get("blocks", [])
        ],
        "period": {"start": meta.get("period_start"), "end": meta.get("period_end")},
        "scale": meta.get("scale", 0),
        "rows": rows,
        "caption": old.get("caption", {}),
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def statement_no_dims():
    return _unify({
        "metadata": {
            "period_start": "2026-01",
            "period_end": "2026-03",
            "dimensions": [],
            "blocks": ["Actuals", "Budget"],
            "block_scenarios": ["actuals", "BUD-2026"],
            "statement": "pnl",
        },
        "rows": [
            {"key": "revenue.services", "label": "Revenue — Services",
             "values": {"Actuals": 420000, "Budget": 400000}},
            {"key": "revenue.licence", "label": "Revenue — Licence",
             "values": {"Actuals": 80000, "Budget": 90000}},
            {"separator": True},
            {"key": "cogs.total", "label": "COGS",
             "values": {"Actuals": 250000, "Budget": 260000}},
        ],
        "caption": {},
    }, "statement")


@pytest.fixture
def metric_with_dims():
    return _unify({
        "metadata": {
            "period_start": "2026-01",
            "period_end": "2026-03",
            "dimensions": ["cost_centre"],
            "blocks": ["Actuals"],
            "block_scenarios": ["actuals"],
        },
        "rows": [
            {"dimension_values": {"cost_centre": "CC-SENG-01"},
             "values": {"Actuals": 1.0}, "format": "currency"},
            {"dimension_values": {"cost_centre": "CC-SALES-01"},
             "values": {"Actuals": 2.0}, "format": "currency"},
        ],
        "caption": {},
    }, "metric")


@pytest.fixture
def statement_with_groups():
    return _unify({
        "metadata": {
            "period_start": "2026-01",
            "period_end": "2026-03",
            "dimensions": ["cost_centre"],
            "blocks": ["Actuals", "Budget"],
            "block_scenarios": ["actuals", "BUD-2026"],
            "statement": "pnl",
        },
        "groups": [
            {"dimension_values": {"cost_centre": "CC-SENG-01"}, "lines": [
                {"key": "revenue", "values": {"Actuals": 420000, "Budget": 400000}},
                {"separator": True},
                {"key": "cogs", "values": {"Actuals": 250000, "Budget": 260000}},
            ]},
            {"dimension_values": {"cost_centre": "CC-SALES-01"}, "lines": [
                {"key": "revenue", "values": {"Actuals": 180000, "Budget": 200000}},
                {"key": "cogs", "values": {"Actuals": 100000, "Budget": 110000}},
            ]},
        ],
        "caption": {},
    }, "statement")


# ---------------------------------------------------------------------------
# Statement, no dimensions
# ---------------------------------------------------------------------------

def test_statement_no_dims_sheet_layout(tmp_path, statement_no_dims):
    path = tmp_path / "x.xlsx"
    extract_to_excel(
        statement_no_dims, path,
        source_tool="run_statement",
        source_params={"statement": "pnl", "scenarios": ["ACTUALS", "BUD-2026"]},
    )
    wb = load_workbook(path)
    ws = [wb[n] for n in wb.sheetnames if n != META_SHEET_NAME][0]
    rows = list(ws.iter_rows(values_only=True))
    assert rows[0] == ("line_item", "Actuals", "Budget")
    assert rows[1] == ("revenue.services", 420000, 400000)
    assert rows[2] == ("revenue.licence", 80000, 90000)
    # Separator dropped — COGS moves up to row 4
    assert rows[3] == ("cogs.total", 250000, 260000)
    assert len(rows) == 4


def test_statement_no_dims_meta_shape(tmp_path, statement_no_dims):
    path = tmp_path / "x.xlsx"
    extract_to_excel(
        statement_no_dims, path,
        source_tool="run_statement",
        source_params={"statement": "pnl"},
    )
    meta = read_meta(load_workbook(path))
    assert meta["workbook_kind"] == "extract"
    entry = meta["sheets"]["Extract"]
    assert entry["kind"] == "extract"
    assert entry["source"]["tool"] == "run_statement"
    shape = entry["shape"]
    assert shape["layout"] == "role_indexed"
    assert shape["key_columns"] == ["line_item"]
    assert shape["value_columns"] == ["Actuals", "Budget"]
    assert shape["row_count"] == 3
    assert shape["header_row"] == 1
    assert shape["data_start_row"] == 2
    assert shape["row_identities"] == [
        {"line_item": "revenue.services"},
        {"line_item": "revenue.licence"},
        {"line_item": "cogs.total"},
    ]


def test_named_ranges_written_and_resolve(tmp_path, statement_no_dims):
    path = tmp_path / "x.xlsx"
    extract_to_excel(
        statement_no_dims, path,
        source_tool="run_statement", source_params={"statement": "pnl"},
    )
    wb = load_workbook(path)
    names = list(wb.defined_names)
    assert "ext_extract_actuals" in names
    assert "ext_extract_budget" in names
    # Attr text points into the data rows only (row 2..4), not the header.
    assert "$B$2:$B$4" in wb.defined_names["ext_extract_actuals"].attr_text
    assert "$C$2:$C$4" in wb.defined_names["ext_extract_budget"].attr_text


def test_excel_table_written_and_resolves(tmp_path, statement_no_dims):
    path = tmp_path / "x.xlsx"
    extract_to_excel(
        statement_no_dims, path,
        source_tool="run_statement", source_params={"statement": "pnl"},
    )
    wb = load_workbook(path)
    ws = [wb[n] for n in wb.sheetnames if n != META_SHEET_NAME][0]
    assert "tbl_extract" in ws.tables
    # Excel Table must span header row + all data rows
    assert ws.tables["tbl_extract"].ref == "A1:C4"


# ---------------------------------------------------------------------------
# Metric with dimensions
# ---------------------------------------------------------------------------

def test_metric_with_dims_layout(tmp_path, metric_with_dims):
    path = tmp_path / "y.xlsx"
    extract_to_excel(
        metric_with_dims, path,
        source_tool="run_metric",
        source_params={"metrics": ["revenue"], "dimensions": ["cost_centre"]},
    )
    wb = load_workbook(path)
    ws = [wb[n] for n in wb.sheetnames if n != META_SHEET_NAME][0]
    rows = list(ws.iter_rows(values_only=True))
    assert rows[0] == ("cost_centre", "Actuals")
    assert rows[1] == ("CC-SENG-01", 1.0)
    assert rows[2] == ("CC-SALES-01", 2.0)


def test_metric_with_dims_identities(tmp_path, metric_with_dims):
    path = tmp_path / "y.xlsx"
    extract_to_excel(
        metric_with_dims, path,
        source_tool="run_metric", source_params={"metrics": ["revenue"]},
    )
    meta = read_meta(load_workbook(path))
    shape = meta["sheets"]["Extract"]["shape"]
    assert shape["key_columns"] == ["cost_centre"]
    # No line_item for single-metric — rows keyed purely by dimension values
    assert shape["row_identities"] == [
        {"cost_centre": "CC-SENG-01"},
        {"cost_centre": "CC-SALES-01"},
    ]


# ---------------------------------------------------------------------------
# Statement with dimensions (groups)
# ---------------------------------------------------------------------------

def test_statement_groups_flattens(tmp_path, statement_with_groups):
    path = tmp_path / "z.xlsx"
    extract_to_excel(
        statement_with_groups, path,
        source_tool="run_statement",
        source_params={"statement": "pnl", "dimensions": ["cost_centre"]},
    )
    wb = load_workbook(path)
    ws = [wb[n] for n in wb.sheetnames if n != META_SHEET_NAME][0]
    rows = list(ws.iter_rows(values_only=True))
    assert rows[0] == ("cost_centre", "line_item", "Actuals", "Budget")
    # Separator dropped; rows interleave by group then by line
    assert rows[1] == ("CC-SENG-01", "revenue", 420000, 400000)
    assert rows[2] == ("CC-SENG-01", "cogs", 250000, 260000)
    assert rows[3] == ("CC-SALES-01", "revenue", 180000, 200000)
    assert rows[4] == ("CC-SALES-01", "cogs", 100000, 110000)


def test_statement_groups_identities(tmp_path, statement_with_groups):
    path = tmp_path / "z.xlsx"
    extract_to_excel(
        statement_with_groups, path,
        source_tool="run_statement", source_params={"statement": "pnl"},
    )
    meta = read_meta(load_workbook(path))
    shape = meta["sheets"]["Extract"]["shape"]
    assert shape["key_columns"] == ["cost_centre", "line_item"]
    assert shape["row_identities"] == [
        {"cost_centre": "CC-SENG-01", "line_item": "revenue"},
        {"cost_centre": "CC-SENG-01", "line_item": "cogs"},
        {"cost_centre": "CC-SALES-01", "line_item": "revenue"},
        {"cost_centre": "CC-SALES-01", "line_item": "cogs"},
    ]


# ---------------------------------------------------------------------------
# Refresh policy
# ---------------------------------------------------------------------------

def test_refresh_policy_defaults(tmp_path, statement_no_dims):
    path = tmp_path / "p.xlsx"
    extract_to_excel(
        statement_no_dims, path,
        source_tool="run_statement", source_params={"statement": "pnl"},
    )
    meta = read_meta(load_workbook(path))
    assert meta["sheets"]["Extract"]["refresh_policy"] == DEFAULT_REFRESH_POLICY


def test_refresh_policy_override_merges_with_defaults(tmp_path, statement_no_dims):
    path = tmp_path / "p.xlsx"
    extract_to_excel(
        statement_no_dims, path,
        source_tool="run_statement", source_params={"statement": "pnl"},
        refresh_policy={"columns_added": "accept"},
    )
    meta = read_meta(load_workbook(path))
    policy = meta["sheets"]["Extract"]["refresh_policy"]
    assert policy["columns_added"] == "accept"
    # Untouched axes inherit defaults
    assert policy["rows_added"] == "accept"
    assert policy["rows_removed"] == "warn"


def test_refresh_policy_rejects_unknown_key(tmp_path, statement_no_dims):
    with pytest.raises(ExtractError, match="unknown key"):
        extract_to_excel(
            statement_no_dims, tmp_path / "p.xlsx",
            source_tool="run_statement", source_params={"statement": "pnl"},
            refresh_policy={"bogus": "accept"},
        )


def test_refresh_policy_rejects_unknown_value(tmp_path, statement_no_dims):
    with pytest.raises(ExtractError, match="not in"):
        extract_to_excel(
            statement_no_dims, tmp_path / "p.xlsx",
            source_tool="run_statement", source_params={"statement": "pnl"},
            refresh_policy={"rows_added": "maybe"},
        )


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

def test_empty_rows_rejected(tmp_path):
    result = {
        "kind": "metric",
        "dimensions": [],
        "scenarios": [{"alias": "Actuals", "format": "currency", "variance": False}],
        "rows": [],
        "caption": {},
    }
    with pytest.raises(ExtractError, match="no data rows"):
        extract_to_excel(
            result, tmp_path / "e.xlsx",
            source_tool="run_metric", source_params={},
        )


def test_empty_blocks_rejected(tmp_path):
    result = {
        "kind": "statement",
        "dimensions": [],
        "scenarios": [],
        "rows": [{
            "grain": "detail",
            "dimensions": {},
            "item": {"key": "x", "label": "x"},
            "values": {},
        }],
        "caption": {},
    }
    with pytest.raises(ExtractError, match="scenarios is empty"):
        extract_to_excel(
            result, tmp_path / "e.xlsx",
            source_tool="run_statement", source_params={},
        )


def test_unsupported_layout_rejected(tmp_path, statement_no_dims):
    with pytest.raises(ExtractError, match="layout"):
        extract_to_excel(
            statement_no_dims, tmp_path / "e.xlsx",
            source_tool="run_statement", source_params={},
            layout="period_indexed",
        )


# ---------------------------------------------------------------------------
# Params handling
# ---------------------------------------------------------------------------

def test_out_stripped_from_persisted_params(tmp_path, statement_no_dims):
    path = tmp_path / "x.xlsx"
    extract_to_excel(
        statement_no_dims, path,
        source_tool="run_statement",
        source_params={"statement": "pnl", "out": "excel"},
    )
    meta = read_meta(load_workbook(path))
    # The caller's `out=excel` is an emit-time detail, not a refresh input —
    # meta persists only refresh-relevant params.
    assert "out" not in meta["sheets"]["Extract"]["source"]["params"]
    assert meta["sheets"]["Extract"]["source"]["params"]["statement"] == "pnl"


def test_user_id_and_generated_at_propagate(tmp_path, statement_no_dims):
    path = tmp_path / "x.xlsx"
    extract_to_excel(
        statement_no_dims, path,
        source_tool="run_statement", source_params={"statement": "pnl"},
        user_id="u_abc",
        agent_model="claude-opus-4-7",
        generated_at=datetime(2026, 4, 21, 14, 32, 7, tzinfo=timezone.utc),
        catalogue_version="2026.04.01",
    )
    meta = read_meta(load_workbook(path))
    assert meta["generated_by"] == {
        "user_id": "u_abc",
        "agent_model": "claude-opus-4-7",
    }
    assert meta["generated_at"] == "2026-04-21T14:32:07Z"
    assert meta["catalogue_version"] == "2026.04.01"


def test_meta_sheet_hidden(tmp_path, statement_no_dims):
    path = tmp_path / "x.xlsx"
    extract_to_excel(
        statement_no_dims, path,
        source_tool="run_statement", source_params={"statement": "pnl"},
    )
    wb = load_workbook(path)
    assert wb[META_SHEET_NAME].sheet_state == "hidden"
    assert wb.sheetnames[-1] == META_SHEET_NAME
