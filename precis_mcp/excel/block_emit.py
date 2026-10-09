# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Block-driven Excel writer — the single report-table emitter.

Both ``metric_to_excel`` and ``statement_to_excel`` are thin wrappers over this
module. They build the unified ``financial_table`` block with
``table_builder.build_financial_table_block`` (the same structure the renderers
consume) and hand it here, so the Excel sheet, the chat-transcript table, and a
saved report block are structurally identical by construction.

This module owns:
  - :func:`write_block_to_sheet` — pure block → worksheet rendering (caption
    rows, one- or two-level header, styled data rows, per-column number
    formats, variance fonts, freeze panes);
  - :func:`report_to_excel` — workbook lifecycle (create/append), the write,
    and meta stamping, shared by both wrappers.

``extract_to_excel`` is deliberately NOT routed through here — it is a flat,
detail-only, refreshable data layer with named ranges, a different artefact.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from openpyxl.styles import Alignment, Font
from openpyxl.worksheet.worksheet import Worksheet

from precis_mcp.excel.engine import (
    _BORDER_BOTTOM,
    _BORDER_TOP,
    _BORDER_TOP_BOTTOM,
    _FILL_HEADER,
    _FILL_SUBTOTAL,
    _FILL_TOTAL,
    _FONT_BOLD,
    _FONT_BOLD_WHITE,
    _FONT_DEFAULT,
    _FONT_ITALIC,
    _apply_header_row,
    _auto_fit_columns,
    _number_format,
    _sheet_name,
    _variance_font,
    _write_caption_rows,
)


def _apply_separator_row(ws: Worksheet, row_num: int, col_count: int) -> None:
    """Style a separator row (empty, thin bottom border across all columns)."""
    for col in range(1, col_count + 1):
        ws.cell(row=row_num, column=col).border = _BORDER_BOTTOM
from precis_mcp.excel.meta import stamp_report_meta
from precis_mcp.excel.workbook import (
    WorkbookLifecycleError,
    finalize_workbook,
    merge_meta,
    prepare_workbook,
)
from precis_mcp.table_builder import build_financial_table_block


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------


def _write_header(ws: Worksheet, columns: list[dict], header_row: int) -> int:
    """Write the column header(s); return the first data row.

    When any column declares a ``group`` (metrics-as-columns layout) a two-level
    header is written: the group label spans its scenario columns on the top
    row, leaf labels sit beneath, and the label column is merged vertically.
    Otherwise a single header row is written.
    """
    col_count = len(columns)
    if not any(c.get("group") for c in columns):
        for ci, col in enumerate(columns, start=1):
            ws.cell(row=header_row, column=ci, value=col.get("label", ""))
        _apply_header_row(ws, header_row, col_count)
        return header_row + 1

    top, bottom = header_row, header_row + 1
    # Label column spans both header rows.
    ws.cell(row=top, column=1, value=columns[0].get("label", ""))
    ws.merge_cells(start_row=top, start_column=1, end_row=bottom, end_column=1)

    value_cols = columns[1:]
    i = 0
    while i < len(value_cols):
        grp = value_cols[i].get("group")
        j = i
        while j < len(value_cols) and value_cols[j].get("group") == grp:
            j += 1
        first_col = 2 + i
        last_col = 2 + j - 1
        if grp:
            ws.cell(row=top, column=first_col, value=grp)
            if last_col > first_col:
                ws.merge_cells(
                    start_row=top, start_column=first_col,
                    end_row=top, end_column=last_col,
                )
        for k in range(i, j):
            ws.cell(row=bottom, column=2 + k, value=value_cols[k].get("label", ""))
        i = j

    _apply_header_row(ws, top, col_count)
    _apply_header_row(ws, bottom, col_count)
    return bottom + 1


# ---------------------------------------------------------------------------
# Row styling
# ---------------------------------------------------------------------------


def _style_block_row(
    ws: Worksheet,
    row_num: int,
    col_count: int,
    row_type: str,
    indent: int,
    value_cols: list[dict],
    row: dict,
) -> None:
    """Apply fonts / fills / borders / indent + variance colour to a data row.

    Number formats are applied per cell by the caller (they vary by column);
    this only handles row-level presentation.
    """
    label_cell = ws.cell(row=row_num, column=1)
    label_cell.alignment = Alignment(indent=indent * 2, vertical="center")

    if row_type == "header":
        for col in range(1, col_count + 1):
            cell = ws.cell(row=row_num, column=col)
            cell.font = _FONT_BOLD_WHITE
            cell.fill = _FILL_HEADER
    elif row_type == "subtotal":
        for col in range(1, col_count + 1):
            cell = ws.cell(row=row_num, column=col)
            cell.font = _FONT_BOLD
            cell.fill = _FILL_SUBTOTAL
            cell.border = _BORDER_TOP
    elif row_type == "total":
        for col in range(1, col_count + 1):
            cell = ws.cell(row=row_num, column=col)
            cell.font = _FONT_BOLD
            cell.fill = _FILL_TOTAL
            cell.border = _BORDER_TOP_BOTTOM
    elif row_type == "group_header":
        label_cell.font = _FONT_BOLD
    elif row_type == "ratio":
        label_cell.font = _FONT_ITALIC
        for col in range(2, col_count + 1):
            ws.cell(row=row_num, column=col).font = _FONT_ITALIC
    else:
        label_cell.font = _FONT_DEFAULT

    # Variance colour on value cells (merge into the existing font).
    values = row.get("values", {})
    for offset, col in enumerate(value_cols):
        if not col.get("variance"):
            continue
        effect = col.get("variance_effect") or row.get("variance_effect") or "natural"
        vfont = _variance_font(values.get(col["key"]), effect)
        if vfont:
            cell = ws.cell(row=row_num, column=2 + offset)
            existing = cell.font
            cell.font = Font(
                name=existing.name, size=existing.size,
                bold=existing.bold, italic=existing.italic,
                color=vfont.color,
            )


# ---------------------------------------------------------------------------
# Block → worksheet
# ---------------------------------------------------------------------------


def write_block_to_sheet(ws: Worksheet, block: dict) -> None:
    """Render a ``financial_table`` block onto a worksheet."""
    caption = block.get("caption", {})
    header_row = _write_caption_rows(ws, caption)

    columns: list[dict] = block.get("columns", [])
    rows: list[dict] = block.get("rows", [])
    col_count = len(columns)
    if col_count == 0:
        ws.cell(row=header_row, column=1, value="No data")
        return

    data_start = _write_header(ws, columns, header_row)
    value_cols = columns[1:]

    current = data_start
    for row in rows:
        row_type = row.get("row_type", "line_item")
        if row_type == "separator":
            _apply_separator_row(ws, current, col_count)
            current += 1
            continue

        ws.cell(row=current, column=1, value=row.get("label", ""))
        row_values = row.get("values", {})

        for offset, col in enumerate(value_cols):
            col_idx = 2 + offset
            fmt = col.get("format") or row.get("format") or "currency"
            dec = col.get("decimals")
            if dec is None:
                dec = row.get("decimals")
            val = row_values.get(col["key"])
            if val is not None:
                # Excel % format multiplies by 100; engine percents are already
                # human-readable (e.g. 8.7 for 8.7%).
                if fmt == "percent" and isinstance(val, (int, float)):
                    val = val / 100
                ws.cell(row=current, column=col_idx, value=val)
            cell = ws.cell(row=current, column=col_idx)
            cell.number_format = _number_format(fmt, dec or 0)
            cell.alignment = Alignment(horizontal="right", vertical="center")

        _style_block_row(
            ws, current, col_count, row_type, row.get("indent", 0), value_cols, row,
        )
        # Grand-total column: a subtle band on plain rows. Header/subtotal/total
        # rows already fill every cell, so their stronger fill wins the
        # intersection (the total row × total column corner reads as the total).
        if row_type not in ("header", "subtotal", "total"):
            for offset, col in enumerate(value_cols):
                if col.get("is_total"):
                    ws.cell(row=current, column=2 + offset).fill = _FILL_SUBTOTAL
        current += 1

    ws.freeze_panes = f"B{data_start}"
    _auto_fit_columns(ws)


# ---------------------------------------------------------------------------
# Workbook lifecycle (shared by metric / statement wrappers)
# ---------------------------------------------------------------------------


def report_to_excel(
    result: dict,
    path: Path,
    *,
    default_sheet: str,
    error_cls: type[Exception],
    meta_source: dict[str, Any] | None = None,
    target: str = "new",
    overwrite: bool = False,
    sheet_name: str | None = None,
) -> Path:
    """Build the unified block from ``result`` and write it to ``path``.

    Handles workbook create/append and meta stamping. ``error_cls`` is raised
    (from a ``WorkbookLifecycleError``) so callers keep their own error type.
    """
    requested_name = _sheet_name(sheet_name) if sheet_name else _sheet_name(default_sheet)

    try:
        wb, existing_meta, pre_existing_sheet_names = prepare_workbook(
            path, target, overwrite, requested_name
        )
    except WorkbookLifecycleError as e:
        raise error_cls(str(e)) from e

    ws: Worksheet = wb.create_sheet(title=requested_name)

    block = build_financial_table_block(result, caption=result.get("caption"))
    if block is None:
        # No scenarios / no rows — write a header-only marker so the sheet exists.
        caption = result.get("caption", {})
        header_row = _write_caption_rows(ws, caption)
        ws.cell(row=header_row, column=1, value="No data")
    else:
        write_block_to_sheet(ws, block)

    if target == "new" and existing_meta is None and not pre_existing_sheet_names:
        stamp_report_meta(wb, ws.title, meta_source)
        wb.save(str(path))
        return path

    sheet_entry: dict[str, Any] = {"kind": "report"}
    if meta_source is not None:
        sheet_entry["source"] = {
            "tool": meta_source["tool"],
            "params": dict(meta_source.get("params", {})),
        }
    meta = merge_meta(
        existing_meta=existing_meta,
        pre_existing_sheet_names=pre_existing_sheet_names,
        new_sheet_name=ws.title,
        new_sheet_entry=sheet_entry,
        workbook_kind_default="report",
        catalogue_version=(meta_source or {}).get("catalogue_version"),
        user_id=(meta_source or {}).get("user_id"),
        agent_model=(meta_source or {}).get("agent_model"),
    )
    finalize_workbook(wb, path, meta)
    return path
