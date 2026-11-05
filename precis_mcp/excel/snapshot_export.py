# SPDX-License-Identifier: Elastic-2.0
"""Materialize a bounded result without another analytical query."""
from __future__ import annotations
from pathlib import Path
from openpyxl import Workbook
from precis_mcp.excel.meta import stamp_report_meta
from precis_mcp.excel.metric_emit import metric_to_excel
from precis_mcp.excel.statement_emit import statement_to_excel
from precis_mcp.excel.hierarchy_emit import hierarchy_to_excel


def write_snapshot_xlsx(result: dict, source: dict, path: Path, *, include_title: bool = True) -> None:
    """Write a snapshot; financial title placement leaves the grid unchanged."""
    meta = {"tool": source["tool"], "params": source["args"]}
    if source["tool"] == "run_statement":
        statement_to_excel(result, path, meta_source=meta, include_title=include_title)
    elif source["tool"] == "run_metric":
        metric_to_excel(result, path, meta_source=meta, include_title=include_title)
    elif source["tool"] == "search_hierarchy":
        hierarchy_to_excel(result, path, meta_source=meta)
    elif source["tool"] == "inspect_rows":
        wb = Workbook()
        ws = wb.active
        ws.title = "Inspection"
        columns = result.get("columns", [])
        ws.append(columns)
        for row in result.get("rows", []):
            ws.append([row.get(key) for key in columns])
        ws.freeze_panes = "A2"
        if columns:
            ws.auto_filter.ref = ws.dimensions
        stamp_report_meta(wb, "Inspection", meta)
        wb.save(path)
    else:
        raise ValueError("Dataset source does not support XLSX export")
    # Source strings are data, not executable Excel formulas. These snapshot
    # emitters do not intentionally author formulas; commercial editing does.
    from openpyxl import load_workbook
    wb = load_workbook(path)
    for ws in wb.worksheets:
        for row in ws:
            for cell in row:
                if cell.data_type == "f":
                    cell.data_type = "s"
    wb.save(path)
