# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Hierarchy search → Excel emitter.

Two-sheet workbook: leaf records on sheet 1, hierarchy nodes on sheet 2.
Shared primitives come from `precis_mcp.excel.engine`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment
from openpyxl.worksheet.worksheet import Worksheet

from precis_mcp.excel.engine import (
    _FILL_ALT_ROW,
    _FONT_DEFAULT,
    _apply_header_row,
    _auto_fit_columns,
)
from precis_mcp.excel.meta import stamp_report_meta


def hierarchy_to_excel(
    result: dict,
    path: Path,
    *,
    meta_source: dict[str, Any] | None = None,
) -> Path:
    """Write a hierarchy search result to Excel (two sheets).

    Args:
        result: Dict with ``records`` (list[dict]) and
            ``hierarchy_nodes`` (list[dict]).
        path: Destination file path.
        meta_source: Optional ``{tool, params, ...}`` dict. When provided,
            the ``Records`` sheet is registered as the refreshable source
            in ``_precis_meta``.

    Returns:
        The path written to.
    """
    wb = Workbook()

    # --- Sheet 1: Records ---
    ws_records: Worksheet = wb.active  # type: ignore[assignment]
    ws_records.title = "Records"

    records = result.get("records", [])
    if records:
        rec_cols = list(records[0].keys())
        _write_flat_sheet(ws_records, rec_cols, records)
    else:
        ws_records.cell(row=1, column=1, value="No records found")

    # --- Sheet 2: Hierarchy Nodes ---
    ws_nodes = wb.create_sheet(title="Hierarchy Nodes")
    node_cols = [
        "dimension", "hierarchy", "node_id",
        "node_name", "display_name", "node_type", "filter_usage",
    ]

    nodes = result.get("hierarchy_nodes", [])
    if nodes:
        _write_flat_sheet(ws_nodes, node_cols, nodes)
    else:
        ws_nodes.cell(row=1, column=1, value="No hierarchy nodes found")

    stamp_report_meta(wb, ws_records.title, meta_source)
    wb.save(str(path))
    return path


def _write_flat_sheet(
    ws: Worksheet,
    columns: list[str],
    rows: list[dict],
) -> None:
    """Write a simple flat table with Précis header styling."""
    col_count = len(columns)

    # Header row
    for col_idx, col_name in enumerate(columns, start=1):
        ws.cell(row=1, column=col_idx, value=col_name)
    _apply_header_row(ws, 1, col_count)

    # Data rows
    for row_idx, row_data in enumerate(rows, start=2):
        for col_idx, col_name in enumerate(columns, start=1):
            val = row_data.get(col_name)
            if val is not None:
                ws.cell(row=row_idx, column=col_idx, value=str(val))
            cell = ws.cell(row=row_idx, column=col_idx)
            cell.font = _FONT_DEFAULT
            cell.alignment = Alignment(horizontal="left", vertical="center")

        # Alternating row shading
        if row_idx % 2 == 0:
            for col_idx in range(1, col_count + 1):
                ws.cell(row=row_idx, column=col_idx).fill = _FILL_ALT_ROW

    # Freeze header row
    ws.freeze_panes = "A2"
    _auto_fit_columns(ws)
