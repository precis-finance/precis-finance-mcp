# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Tests for precis_mcp.excel.meta — _precis_meta read/write + schema.

Covers:

* Roundtrip: build → write → save → load → read_meta returns the same dict.
* ``_precis_meta`` sheet is hidden and appended at the end of the sheet
  order (Précis-generated sheets stay out of the user's way).
* Absent sheet → ``read_meta`` returns None.
* Version rejection (unsupported ``precis_meta_version`` raises).
* Malformed YAML / empty A1 / non-mapping YAML all raise ``MetaError``.
* Builders enforce kind allowlists.
* Emitter integration: statement_to_excel and metric_to_excel stamp the
  meta sheet, with ``kind: report`` and the caller-supplied source.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone

import pytest
import yaml
from openpyxl import Workbook, load_workbook

from precis_mcp.excel.meta import (
    CURRENT_META_VERSION,
    META_SHEET_NAME,
    MetaError,
    build_minimal_meta,
    build_regions_meta,
    build_report_meta,
    read_meta,
    stamp_report_meta,
    write_meta,
)
from precis_mcp.excel import metric_to_excel, statement_to_excel


def test_oversized_provenance_is_rejected_before_replacing_existing_metadata():
    wb = Workbook()
    original = build_minimal_meta()
    write_meta(wb, original)
    large = {**original, "note": "x" * 32768}
    with pytest.raises(MetaError, match="cell limit"):
        write_meta(wb, large)
    assert read_meta(wb) == original


def _save_and_reload(wb: Workbook) -> Workbook:
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return load_workbook(buf)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def test_build_minimal_meta_defaults():
    meta = build_minimal_meta()
    assert meta["precis_meta_version"] == CURRENT_META_VERSION
    assert meta["workbook_kind"] == "report"
    assert "generated_at" in meta
    assert meta["generated_by"] == {"user_id": None, "agent_model": None}
    assert "sheets" not in meta


def test_build_minimal_meta_rejects_unknown_workbook_kind():
    with pytest.raises(MetaError, match="workbook_kind"):
        build_minimal_meta(workbook_kind="garbage")


def test_build_minimal_meta_formats_generated_at_utc():
    # Naive datetime is interpreted as UTC; aware datetime is converted.
    naive = datetime(2026, 4, 21, 14, 32, 7)
    meta = build_minimal_meta(generated_at=naive)
    assert meta["generated_at"] == "2026-04-21T14:32:07Z"


def test_build_report_meta_shape():
    meta = build_report_meta(
        tool="run_statement",
        params={"statement": "pnl", "period_from": "2026-01"},
        sheet_name="P&L",
        catalogue_version="2026.04.01",
        user_id="u_abc",
        agent_model="claude-opus-4-7",
        generated_at=datetime(2026, 4, 21, 14, 32, 7, tzinfo=timezone.utc),
    )
    assert meta["workbook_kind"] == "report"
    assert meta["catalogue_version"] == "2026.04.01"
    assert meta["generated_by"] == {
        "user_id": "u_abc", "agent_model": "claude-opus-4-7",
    }
    assert meta["generated_at"] == "2026-04-21T14:32:07Z"
    sheet = meta["sheets"]["P&L"]
    assert sheet["kind"] == "report"
    assert sheet["source"]["tool"] == "run_statement"
    assert sheet["source"]["params"]["statement"] == "pnl"


# ---------------------------------------------------------------------------
# write_meta / read_meta
# ---------------------------------------------------------------------------

def test_write_then_read_roundtrip():
    wb = Workbook()
    wb.active.title = "Data"
    meta = build_report_meta(
        tool="run_metric",
        params={"metrics": ["revenue"], "period_start": "2026-01"},
        sheet_name="Data",
    )
    write_meta(wb, meta)

    wb2 = _save_and_reload(wb)
    got = read_meta(wb2)
    assert got is not None
    assert got["precis_meta_version"] == CURRENT_META_VERSION
    assert got["sheets"]["Data"]["source"]["tool"] == "run_metric"
    assert got["sheets"]["Data"]["source"]["params"]["metrics"] == ["revenue"]


def test_write_meta_hidden_and_last():
    wb = Workbook()
    wb.active.title = "Data"
    wb.create_sheet("Notes")
    write_meta(wb, build_minimal_meta())

    wb2 = _save_and_reload(wb)
    # Must be last in sheet order so the user doesn't land on it.
    assert wb2.sheetnames[-1] == META_SHEET_NAME
    assert wb2[META_SHEET_NAME].sheet_state == "hidden"


def test_write_meta_replaces_existing_sheet():
    wb = Workbook()
    wb.active.title = "Data"
    write_meta(wb, build_minimal_meta(user_id="v1"))
    write_meta(wb, build_minimal_meta(user_id="v2"))
    # Still exactly one meta sheet, with the newer content.
    assert wb.sheetnames.count(META_SHEET_NAME) == 1
    wb2 = _save_and_reload(wb)
    got = read_meta(wb2)
    assert got["generated_by"]["user_id"] == "v2"


def test_read_meta_missing_returns_none():
    wb = Workbook()
    assert read_meta(wb) is None


def test_read_meta_rejects_unsupported_version():
    wb = Workbook()
    meta = build_minimal_meta()
    meta["precis_meta_version"] = 99
    # Bypass write_meta's validation — simulate a future-version file.
    ws = wb.create_sheet(title=META_SHEET_NAME)
    ws["A1"] = yaml.safe_dump(meta)
    ws.sheet_state = "hidden"

    with pytest.raises(MetaError, match="version"):
        read_meta(wb)


def test_read_meta_rejects_missing_version():
    wb = Workbook()
    ws = wb.create_sheet(title=META_SHEET_NAME)
    ws["A1"] = yaml.safe_dump({"workbook_kind": "report"})
    with pytest.raises(MetaError, match="precis_meta_version"):
        read_meta(wb)


def test_read_meta_rejects_empty_cell():
    wb = Workbook()
    ws = wb.create_sheet(title=META_SHEET_NAME)
    ws["A1"] = "   "
    with pytest.raises(MetaError, match="empty or non-string"):
        read_meta(wb)


def test_read_meta_rejects_malformed_yaml():
    wb = Workbook()
    ws = wb.create_sheet(title=META_SHEET_NAME)
    ws["A1"] = "this: is: not: valid: yaml: at: all"
    with pytest.raises(MetaError, match="malformed YAML"):
        read_meta(wb)


def test_read_meta_rejects_non_mapping_yaml():
    wb = Workbook()
    ws = wb.create_sheet(title=META_SHEET_NAME)
    ws["A1"] = "- just\n- a\n- list\n"
    with pytest.raises(MetaError, match="must be a mapping"):
        read_meta(wb)


def test_read_meta_rejects_invalid_sheet_kind():
    wb = Workbook()
    meta = build_minimal_meta()
    meta["sheets"] = {"Data": {"kind": "weaponised"}}
    ws = wb.create_sheet(title=META_SHEET_NAME)
    ws["A1"] = yaml.safe_dump(meta)
    with pytest.raises(MetaError, match="kind='weaponised'"):
        read_meta(wb)


# ---------------------------------------------------------------------------
# stamp_report_meta convenience
# ---------------------------------------------------------------------------

def test_stamp_report_meta_minimal_when_source_none():
    wb = Workbook()
    wb.active.title = "Data"
    stamp_report_meta(wb, "Data", None)
    got = read_meta(_save_and_reload(wb))
    assert got["workbook_kind"] == "report"
    assert "sheets" not in got


def test_stamp_report_meta_from_source_dict():
    wb = Workbook()
    wb.active.title = "P&L"
    stamp_report_meta(wb, "P&L", {
        "tool": "run_statement",
        "params": {"statement": "pnl"},
        "user_id": "u_1",
    })
    got = read_meta(_save_and_reload(wb))
    assert got["sheets"]["P&L"]["source"]["tool"] == "run_statement"
    assert got["generated_by"]["user_id"] == "u_1"


# ---------------------------------------------------------------------------
# Emitter integration
# ---------------------------------------------------------------------------

@pytest.fixture
def minimal_statement_result():
    return {
        "kind": "statement", "dimensions": [], "scenarios": [{"alias": "Actuals"}],
        "scale": 0,
        "caption": {},
        "rows": [
            {
                "grain": "detail", "dimensions": {},
                "item": {"key": "revenue", "label": "Revenue", "style": "line", "indent": 0},
                "values": {"Actuals": 100.0},
            },
        ],
    }


@pytest.fixture
def minimal_metric_result():
    return {
        "kind": "metric", "dimensions": [], "scenarios": [{"alias": "Actuals"}],
        "scale": 0,
        "caption": {},
        "rows": [
            {
                "grain": "detail", "dimensions": {},
                "item": {"key": "revenue", "label": "Revenue", "format": "currency"},
                "values": {"Actuals": 1.0},
            },
        ],
    }


def test_statement_to_excel_stamps_report_meta(tmp_path, minimal_statement_result):
    path = tmp_path / "pnl.xlsx"
    statement_to_excel(
        minimal_statement_result,
        path,
        meta_source={
            "tool": "run_statement",
            "params": {"statement": "pnl"},
            "user_id": "u_1",
        },
    )
    wb = load_workbook(path)
    assert META_SHEET_NAME in wb.sheetnames
    meta = read_meta(wb)
    sheet_name = [n for n in wb.sheetnames if n != META_SHEET_NAME][0]
    assert "source" not in meta["sheets"][sheet_name]
    assert meta["sheets"][sheet_name]["regions"][0]["source"]["tool"] == "run_statement"
    assert meta["generated_by"]["user_id"] == "u_1"


def test_statement_to_excel_defaults_to_minimal_meta(tmp_path, minimal_statement_result):
    path = tmp_path / "pnl.xlsx"
    statement_to_excel(minimal_statement_result, path)
    wb = load_workbook(path)
    meta = read_meta(wb)
    assert meta["workbook_kind"] == "report"
    assert "sheets" not in meta  # No source, no sheet entry.


def test_metric_to_excel_stamps_report_meta(tmp_path, minimal_metric_result):
    path = tmp_path / "metric.xlsx"
    metric_to_excel(
        minimal_metric_result,
        path,
        meta_source={
            "tool": "run_metric",
            "params": {"metrics": ["revenue"]},
        },
    )
    wb = load_workbook(path)
    meta = read_meta(wb)
    sheet_name = [n for n in wb.sheetnames if n != META_SHEET_NAME][0]
    assert "source" not in meta["sheets"][sheet_name]
    assert meta["sheets"][sheet_name]["regions"][0]["source"]["tool"] == "run_metric"


def test_metric_to_excel_empty_result_still_stamps(tmp_path):
    # Empty reports carry no convertible region or dead sheet-level query.
    result = {
        "metadata": {"period_start": "2026-01", "period_end": "2026-03"},
        "caption": {},
        "rows": [],
    }
    path = tmp_path / "empty.xlsx"
    metric_to_excel(
        result,
        path,
        meta_source={"tool": "run_metric", "params": {"metrics": ["x"]}},
    )
    wb = load_workbook(path)
    assert META_SHEET_NAME in wb.sheetnames
    meta = read_meta(wb)
    assert meta["sheets"][list(meta["sheets"])[0]] == {"kind": "report"}


# ---------------------------------------------------------------------------
# Per-region metadata (the add-in STATIC-state encoding, spec §2.3–§2.4)
# ---------------------------------------------------------------------------

class TestRegionsMeta:
    def _src(self, tool="run_statement"):
        return {"tool": tool, "params": {"statement": "pnl", "period_start": "2026-01"}}

    def test_build_regions_meta_shape(self):
        meta = build_regions_meta(
            sheet_name="P&L",
            regions=[
                {"range_name": "precis_r1", "source": self._src()},
                {"range_name": "precis_r2", "source": self._src("run_metric")},
            ],
        )
        assert meta["precis_meta_version"] == CURRENT_META_VERSION  # additive, no bump
        entry = meta["sheets"]["P&L"]
        assert entry["kind"] == "report"
        names = [r["range_name"] for r in entry["regions"]]
        assert names == ["precis_r1", "precis_r2"]
        assert entry["regions"][0]["source"]["tool"] == "run_statement"
        assert entry["regions"][1]["source"]["tool"] == "run_metric"
        # the legacy per-sheet `source` is absent on a region-bearing sheet
        assert "source" not in entry

    def test_regions_roundtrip(self):
        meta = build_regions_meta(
            sheet_name="Sheet1",
            regions=[{"range_name": "precis_x", "source": self._src()}],
        )
        wb = Workbook()
        write_meta(wb, meta)
        reloaded = _save_and_reload(wb)
        got = read_meta(reloaded)
        assert got["sheets"]["Sheet1"]["regions"][0]["range_name"] == "precis_x"
        assert got["sheets"]["Sheet1"]["regions"][0]["source"]["params"]["statement"] == "pnl"

    def test_region_requires_range_name(self):
        with pytest.raises(MetaError, match="range_name"):
            build_regions_meta(
                sheet_name="S", regions=[{"source": self._src()}],
            )

    def test_region_requires_source_tool(self):
        with pytest.raises(MetaError, match="tool"):
            build_regions_meta(
                sheet_name="S",
                regions=[{"range_name": "precis_x", "source": {"params": {}}}],
            )

    def test_validate_rejects_non_list_regions(self):
        # hand-craft a malformed meta and run it through read_meta's validator
        bad = build_minimal_meta()
        bad["sheets"] = {"S": {"kind": "report", "regions": "not-a-list"}}
        wb = Workbook()
        with pytest.raises(MetaError, match="regions must be a list"):
            write_meta(wb, bad)

    def test_validate_rejects_region_without_range_name(self):
        bad = build_minimal_meta()
        bad["sheets"] = {"S": {"kind": "report", "regions": [{"source": {"tool": "run_metric"}}]}}
        wb = Workbook()
        with pytest.raises(MetaError, match="range_name"):
            write_meta(wb, bad)
