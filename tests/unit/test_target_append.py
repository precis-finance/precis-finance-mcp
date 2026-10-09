# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Tests for ``target='new'`` / ``target='append'`` semantics across the
three Précis Excel emitters (statement, metric, extract).

All three emitters share the same workbook-lifecycle helpers
(``precis_mcp.excel.workbook``). These tests cover the contract the
agent-facing ``run_statement`` / ``run_metric`` dispatcher relies on:

* ``target='new'`` on an existing file raises unless ``overwrite=True``.
* ``target='append'`` on a missing file raises.
* ``target='append'`` onto a Précis workbook preserves prior sheets and
  bumps ``generated_at``.
* ``target='append'`` onto a non-Précis workbook tags pre-existing
  sheets as ``kind: user``.
* Sheet-name collisions on append require ``overwrite=True``.
* Every target stamps ``_precis_meta`` so the workbook is refreshable
  downstream.
"""

from __future__ import annotations

import pytest
from openpyxl import Workbook, load_workbook

from precis_mcp.excel.extract_emit import ExtractError, extract_to_excel
from precis_mcp.excel.meta import META_SHEET_NAME, read_meta
from precis_mcp.excel.metric_emit import MetricEmitError, metric_to_excel
from precis_mcp.excel.statement_emit import (
    StatementEmitError,
    statement_to_excel,
)


# ---------------------------------------------------------------------------
# Fixtures — minimal engine results for each emitter
# ---------------------------------------------------------------------------

@pytest.fixture
def statement_result():
    return {
        "kind": "statement",
        "period": {"start": "2026-01", "end": "2026-03"},
        "dimensions": [],
        "scenarios": [{"scenario": "actuals", "alias": "Actuals"}],
        "scale": 0,
        "decimals": None,
        "rows": [
            {
                "grain": "detail",
                "item": {
                    "key": "revenue",
                    "label": "Revenue",
                    "format": "currency",
                    "style": "header",
                    "indent": 0,
                },
                "dimensions": {},
                "values": {"Actuals": 1000},
            },
        ],
    }


@pytest.fixture
def metric_result():
    return {
        "kind": "metric",
        "period": {"start": "2026-01", "end": "2026-03"},
        "dimensions": ["cost_centre"],
        "scenarios": [{"scenario": "actuals", "alias": "Actuals"}],
        "scale": 0,
        "decimals": None,
        "rows": [
            {
                "grain": "detail",
                "item": {
                    "key": "revenue",
                    "label": "Revenue",
                    "format": "currency",
                },
                "dimensions": {"cost_centre": "CC-1"},
                "values": {"Actuals": 100},
            },
        ],
    }


@pytest.fixture
def extract_result():
    return {
        "kind": "metric",
        "period": {"start": "2026-01", "end": "2026-03"},
        "dimensions": ["cost_centre"],
        "scenarios": [{"scenario": "actuals", "alias": "Actuals"}],
        "rows": [
            {
                "grain": "detail",
                "item": {"key": "revenue", "label": "Revenue"},
                "dimensions": {"cost_centre": "CC-1"},
                "values": {"Actuals": 100},
            },
            {
                "grain": "detail",
                "item": {"key": "revenue", "label": "Revenue"},
                "dimensions": {"cost_centre": "CC-2"},
                "values": {"Actuals": 200},
            },
        ],
        "caption": {},
    }


def _user_sheets(wb) -> list[str]:
    return [s for s in wb.sheetnames if s != META_SHEET_NAME]


# ===========================================================================
# statement_to_excel — target=new / target=append
# ===========================================================================

class TestStatementTarget:

    def test_new_on_missing_file_writes(self, tmp_path, statement_result):
        path = tmp_path / "stmt.xlsx"
        statement_to_excel(statement_result, path, target="new")
        assert path.exists()
        wb = load_workbook(path)
        assert "Statement" in _user_sheets(wb)
        assert read_meta(wb) is not None

    def test_new_on_existing_file_raises_without_overwrite(
        self, tmp_path, statement_result
    ):
        path = tmp_path / "stmt.xlsx"
        statement_to_excel(statement_result, path, target="new")
        with pytest.raises(StatementEmitError, match="already exists"):
            statement_to_excel(statement_result, path, target="new")

    def test_new_with_overwrite_replaces(self, tmp_path, statement_result):
        path = tmp_path / "stmt.xlsx"
        statement_to_excel(statement_result, path, target="new")
        original_size = path.stat().st_size
        # Rewrite with different sheet name — sheet count in rewritten wb
        # should be 1 (plus meta), proving clobber not append.
        statement_to_excel(
            statement_result, path, target="new", overwrite=True,
            sheet_name="Rewritten",
        )
        wb = load_workbook(path)
        assert _user_sheets(wb) == ["Rewritten"]
        _ = original_size  # touched to document intent

    def test_append_on_missing_file_raises(self, tmp_path, statement_result):
        path = tmp_path / "absent.xlsx"
        with pytest.raises(StatementEmitError, match="does not exist"):
            statement_to_excel(statement_result, path, target="append")

    def test_append_adds_sheet_to_precis_workbook(
        self, tmp_path, statement_result
    ):
        path = tmp_path / "stmt.xlsx"
        statement_to_excel(
            statement_result, path, target="new", sheet_name="FirstSheet",
        )
        statement_to_excel(
            statement_result, path, target="append", sheet_name="SecondSheet",
        )
        wb = load_workbook(path)
        user = _user_sheets(wb)
        assert "FirstSheet" in user
        assert "SecondSheet" in user

    def test_append_sheet_collision_raises_without_overwrite(
        self, tmp_path, statement_result
    ):
        path = tmp_path / "stmt.xlsx"
        statement_to_excel(
            statement_result, path, target="new", sheet_name="Dup",
        )
        with pytest.raises(StatementEmitError, match="already exists"):
            statement_to_excel(
                statement_result, path, target="append", sheet_name="Dup",
            )

    def test_append_sheet_collision_overwritten(
        self, tmp_path, statement_result
    ):
        path = tmp_path / "stmt.xlsx"
        statement_to_excel(
            statement_result, path, target="new", sheet_name="Dup",
        )
        statement_to_excel(
            statement_result, path, target="append",
            sheet_name="Dup", overwrite=True,
        )
        wb = load_workbook(path)
        assert _user_sheets(wb).count("Dup") == 1

    def test_append_onto_non_precis_workbook_tags_pre_existing_as_user(
        self, tmp_path, statement_result
    ):
        path = tmp_path / "external.xlsx"
        wb = Workbook()
        default = wb.active
        assert default is not None
        default.title = "external_sheet"
        default["A1"] = "hand-crafted"
        wb.save(str(path))

        statement_to_excel(
            statement_result, path, target="append", sheet_name="AddedByPrecis",
        )

        wb_after = load_workbook(path)
        meta = read_meta(wb_after)
        assert meta is not None
        sheets_meta = meta.get("sheets") or {}
        assert sheets_meta.get("external_sheet", {}).get("kind") == "user"
        assert sheets_meta.get("AddedByPrecis", {}).get("kind") == "report"


# ===========================================================================
# metric_to_excel — target=new / target=append
# ===========================================================================

class TestMetricTarget:

    def test_new_on_missing_file_writes(self, tmp_path, metric_result):
        path = tmp_path / "metric.xlsx"
        metric_to_excel(metric_result, path, target="new")
        assert path.exists()
        assert read_meta(load_workbook(path)) is not None

    def test_new_on_existing_file_raises_without_overwrite(
        self, tmp_path, metric_result
    ):
        path = tmp_path / "metric.xlsx"
        metric_to_excel(metric_result, path, target="new")
        with pytest.raises(MetricEmitError, match="already exists"):
            metric_to_excel(metric_result, path, target="new")

    def test_append_on_missing_file_raises(self, tmp_path, metric_result):
        path = tmp_path / "absent.xlsx"
        with pytest.raises(MetricEmitError, match="does not exist"):
            metric_to_excel(metric_result, path, target="append")

    def test_append_adds_sheet(self, tmp_path, metric_result):
        path = tmp_path / "metric.xlsx"
        metric_to_excel(
            metric_result, path, target="new", sheet_name="First",
        )
        metric_to_excel(
            metric_result, path, target="append", sheet_name="Second",
        )
        wb = load_workbook(path)
        user = _user_sheets(wb)
        assert "First" in user
        assert "Second" in user

    def test_append_sheet_collision_raises(self, tmp_path, metric_result):
        path = tmp_path / "metric.xlsx"
        metric_to_excel(metric_result, path, target="new", sheet_name="Dup")
        with pytest.raises(MetricEmitError, match="already exists"):
            metric_to_excel(
                metric_result, path, target="append", sheet_name="Dup",
            )


# ===========================================================================
# extract_to_excel — target=new / target=append
# ===========================================================================

class TestExtractTarget:

    def test_new_on_missing_file_writes(self, tmp_path, extract_result):
        path = tmp_path / "extract.xlsx"
        extract_to_excel(
            extract_result, path,
            source_tool="run_metric",
            source_params={"metric": "revenue"},
            sheet_name="Extract",
            user_id="u1",
        )
        assert path.exists()
        meta = read_meta(load_workbook(path))
        assert meta is not None
        assert meta.get("workbook_kind") == "extract"

    def test_new_on_existing_file_raises_without_overwrite(
        self, tmp_path, extract_result
    ):
        path = tmp_path / "extract.xlsx"
        extract_to_excel(
            extract_result, path,
            source_tool="run_metric", source_params={"metric": "revenue"},
            sheet_name="Extract", user_id="u1",
        )
        with pytest.raises(ExtractError, match="already exists"):
            extract_to_excel(
                extract_result, path,
                source_tool="run_metric",
                source_params={"metric": "revenue"},
                sheet_name="Extract", user_id="u1",
            )

    def test_append_on_missing_file_raises(self, tmp_path, extract_result):
        path = tmp_path / "absent.xlsx"
        with pytest.raises(ExtractError, match="does not exist"):
            extract_to_excel(
                extract_result, path,
                source_tool="run_metric",
                source_params={"metric": "revenue"},
                sheet_name="Extract", user_id="u1",
                target="append",
            )

    def test_append_adds_sheet_preserving_prior(self, tmp_path, extract_result):
        path = tmp_path / "extract.xlsx"
        extract_to_excel(
            extract_result, path,
            source_tool="run_metric", source_params={"metric": "revenue"},
            sheet_name="First", user_id="u1",
        )
        extract_to_excel(
            extract_result, path,
            source_tool="run_metric", source_params={"metric": "revenue"},
            sheet_name="Second", user_id="u1",
            target="append",
        )
        wb = load_workbook(path)
        user = _user_sheets(wb)
        assert "First" in user
        assert "Second" in user
        meta = read_meta(wb)
        assert meta is not None
        sheets_meta = meta.get("sheets") or {}
        assert "First" in sheets_meta
        assert "Second" in sheets_meta
