# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Statement/metric → Extract sheet (raw refreshable data).

An *Extract* is a flat table of engine output where columns are semantic
(scenario-block aliases — "Actuals", "Budget", "Variance") and rows are
the leaf-level data (one row per dimension combination, plus a
``line_item`` column when the source is a statement). Downstream models
on adjacent sheets reference the Extract via named ranges or Excel
Table structured refs — never literal A1s — so refresh can grow the
table without breaking user formulas.

The companion ``_precis_meta`` entry pins the refresh contract:

* ``source`` — tool + params to re-run.
* ``shape`` — ordered ``row_identities`` so refresh matches by identity,
  not position (see §6.6 of docs/excel_agent_research_2026.md).
* ``refresh_policy`` — per-axis policy (``accept`` / ``warn`` /
  ``refuse``) for rows/columns added/removed and value changes.

Only the ``role_indexed`` layout is supported in this slice. Period as
a layout axis is deferred; when/if it returns, the preferred path is a
``month``/``quarter`` dimension on the engine rather than a second
layout in this module.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from openpyxl import Workbook
from openpyxl.styles import Alignment
from openpyxl.utils import get_column_letter
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.table import Table, TableStyleInfo

from precis_mcp.excel.cells import write_source_cell, write_footer_row
from precis_mcp.excel.worksheet import extract_worksheet, flatten_extract as _flatten_role_indexed, caption_text
from precis_mcp.excel.engine import (
    _FILL_ALT_ROW,
    _FONT_DEFAULT,
    _apply_header_row,
    _auto_fit_columns,
    _sheet_name,
)
from precis_mcp.excel.workbook import (
    WorkbookLifecycleError,
    finalize_workbook,
    merge_meta,
    prepare_workbook,
)


SUPPORTED_LAYOUTS: frozenset[str] = frozenset({"role_indexed"})

DEFAULT_REFRESH_POLICY: dict[str, str] = {
    "rows_added": "accept",
    "rows_removed": "warn",
    "columns_added": "refuse",
    "columns_removed": "refuse",
    "value_changes": "accept",
}

_VALID_POLICY_VALUES: frozenset[str] = frozenset({"accept", "warn", "refuse"})


class ExtractError(Exception):
    """Raised when the engine result can't be materialised as an Extract."""


def extract_to_excel(
    result: Mapping[str, Any],
    path: Path,
    *,
    source_tool: str,
    source_params: Mapping[str, Any],
    sheet_name: str = "Extract",
    layout: str = "role_indexed",
    refresh_policy: Mapping[str, str] | None = None,
    catalogue_version: str | None = None,
    user_id: str | None = None,
    agent_model: str | None = None,
    generated_at: datetime | None = None,
    target: str = "new",
    overwrite: bool = False,
) -> tuple[Path, str]:
    """Write (or append) an Extract data sheet into a workbook.

    Args:
        result: Unified engine response dict from ``run_statement`` or
            ``run_metric``. Must contain ``scenarios`` and flat grain-tagged
            ``rows``. Empty results (no data rows) are rejected with
            ``ExtractError`` — there is nothing to refresh.
        path: Destination file path.
        source_tool: Canonical tool name (``run_statement`` / ``run_metric``).
            Persisted in meta so refresh can dispatch.
        source_params: Args the source tool was called with. Copied into
            meta verbatim (minus ``out``, which is the only shape-only
            arg we strip).
        sheet_name: Display name of the data sheet. Truncated/sanitised
            to Excel's 31-char limit.
        layout: ``role_indexed`` only. Anything else raises.
        refresh_policy: Overrides per axis; missing keys fall back to
            ``DEFAULT_REFRESH_POLICY``.
        catalogue_version, user_id, agent_model, generated_at: passed
            through to meta.
        target: ``"new"`` creates a fresh workbook at ``path`` (raises if
            the path exists and ``overwrite`` is False). ``"append"``
            loads the existing workbook at ``path`` and adds the sheet
            (raises if the file doesn't exist, or if the sheet name
            collides and ``overwrite`` is False). On append to a non-Précis
            workbook, pre-existing sheets are recorded in meta as
            ``kind: user`` so refresh leaves them alone.
        overwrite: On ``target="new"``, allow clobbering an existing file.
            On ``target="append"``, allow replacing a colliding sheet.

    Returns:
        ``(path, actual_sheet_name)`` — the path written to and the
        final (possibly sanitised) sheet name.
    """
    if layout not in SUPPORTED_LAYOUTS:
        raise ExtractError(
            f"layout={layout!r} not supported "
            f"(currently: {sorted(SUPPORTED_LAYOUTS)})"
        )

    scenarios = list(result.get("scenarios", []))
    block_aliases = [s.get("alias") for s in scenarios if s.get("alias")]
    if not block_aliases:
        raise ExtractError("result.scenarios is empty — nothing to extract")

    key_columns, value_columns, data_rows, row_identities = _flatten_role_indexed(
        result
    )
    if not data_rows:
        raise ExtractError("result has no data rows — nothing to extract")

    policy = _resolve_refresh_policy(refresh_policy)

    requested_name = _sheet_name(sheet_name)

    try:
        wb, existing_meta, pre_existing_sheet_names = prepare_workbook(
            path, target, overwrite, requested_name
        )
    except WorkbookLifecycleError as e:
        raise ExtractError(str(e)) from e

    ws = wb.create_sheet(title=requested_name)
    actual_sheet_name = ws.title

    header_row = 1
    all_columns = key_columns + value_columns
    col_count = len(all_columns)

    for col_idx, name in enumerate(all_columns, start=1):
        write_source_cell(ws, row=header_row, column=col_idx, value=name)
    _apply_header_row(ws, header_row, col_count)

    worksheet = extract_worksheet(result)
    key_col_count = len(key_columns)
    data_start_row = header_row + 1

    for row_offset, row_data in enumerate(data_rows):
        row_idx = data_start_row + row_offset
        for col_idx, col_name in enumerate(all_columns, start=1):
            val = row_data.get(col_name)
            if val is not None:
                write_source_cell(ws, row=row_idx, column=col_idx, value=val)
            cell = ws.cell(row=row_idx, column=col_idx)
            if col_idx <= key_col_count:
                cell.alignment = Alignment(horizontal="left", vertical="center")
                cell.font = _FONT_DEFAULT
            else:
                cell.alignment = Alignment(horizontal="right", vertical="center")
                cell.number_format = worksheet["number_formats"][row_offset + 1][col_idx - 1]
        if row_idx % 2 == 0:
            for col_idx in range(1, col_count + 1):
                ws.cell(row=row_idx, column=col_idx).fill = _FILL_ALT_ROW

    last_row = data_start_row + len(data_rows) - 1
    last_col_letter = get_column_letter(col_count)
    table_ref = f"A{header_row}:{last_col_letter}{last_row}"
    region_name = _unique_range_name(wb, f"precis_region_{_slug(actual_sheet_name)}")
    wb.defined_names[region_name] = DefinedName(
        region_name, attr_text=f"'{actual_sheet_name.replace(chr(39), chr(39) * 2)}'!$A${header_row}:${last_col_letter}${last_row + 1}"
    )

    table_name = _unique_table_name(wb, actual_sheet_name)
    tbl = Table(displayName=table_name, ref=table_ref)
    tbl.tableStyleInfo = TableStyleInfo(
        name="TableStyleMedium2",
        showFirstColumn=False,
        showLastColumn=False,
        showRowStripes=True,
        showColumnStripes=False,
    )
    ws.add_table(tbl)
    excel_tables_meta = [{"name": table_name, "ref": f"'{actual_sheet_name}'!{table_ref}"}]

    named_ranges_meta: list[dict[str, str]] = []
    sheet_slug = _slug(actual_sheet_name)
    for value_idx, value_col in enumerate(value_columns):
        col_idx = key_col_count + 1 + value_idx
        letter = get_column_letter(col_idx)
        ref = f"'{actual_sheet_name}'!${letter}${data_start_row}:${letter}${last_row}"
        range_name = _unique_range_name(wb, f"ext_{sheet_slug}_{_slug(value_col)}")
        wb.defined_names[range_name] = DefinedName(range_name, attr_text=ref)
        named_ranges_meta.append({"name": range_name, "ref": ref})

    ws.freeze_panes = f"{get_column_letter(key_col_count + 1)}{data_start_row}"
    _auto_fit_columns(ws)
    write_footer_row(ws, row=last_row + 1, columns=col_count, text=caption_text(result.get("caption")))

    sheet_entry = _build_sheet_entry(
        source_tool=source_tool,
        source_params=source_params,
        layout=layout,
        key_columns=key_columns,
        value_columns=value_columns,
        row_count=len(data_rows),
        header_row=header_row,
        data_start_row=data_start_row,
        row_identities=row_identities,
        named_ranges=named_ranges_meta,
        excel_tables=excel_tables_meta,
        refresh_policy=policy,
    )
    # Effective settings come from the result, never an implicit MCP default.
    sheet_entry["shape"]["footer_rows"] = 1
    effective_params = dict(sheet_entry["source"]["params"])
    effective_params["scale"] = result.get("scale", effective_params.get("scale"))
    if "decimals" in result:
        effective_params["decimals"] = result["decimals"]
    else:
        effective_params.setdefault("decimals", None)
    sheet_entry["source"]["params"] = effective_params
    sheet_entry["regions"] = [{
        "range_name": region_name, "state": "static", "layout": "extract",
        "origin": os.environ.get("PRECIS_BASE_URL", "").rstrip("/") or None,
        "source": sheet_entry["source"], "table_name": table_name,
        "named_ranges": named_ranges_meta,
        "shape": {key: value for key, value in sheet_entry["shape"].items() if key != "row_identities"},
        "presentation": {"scale": effective_params["scale"],
                         "decimals": effective_params["decimals"]},
    }]
    del sheet_entry["source"]

    meta = merge_meta(
        existing_meta=existing_meta,
        pre_existing_sheet_names=pre_existing_sheet_names,
        new_sheet_name=actual_sheet_name,
        new_sheet_entry=sheet_entry,
        workbook_kind_default="extract",
        catalogue_version=catalogue_version,
        user_id=user_id,
        agent_model=agent_model,
        generated_at=generated_at,
    )
    finalize_workbook(wb, path, meta)
    return path, actual_sheet_name


# ---------------------------------------------------------------------------
# Meta builder
# ---------------------------------------------------------------------------

def _build_sheet_entry(
    *,
    source_tool: str,
    source_params: Mapping[str, Any],
    layout: str,
    key_columns: list[str],
    value_columns: list[str],
    row_count: int,
    header_row: int,
    data_start_row: int,
    row_identities: list[dict],
    named_ranges: list[dict[str, str]],
    excel_tables: list[dict[str, str]],
    refresh_policy: dict[str, str],
) -> dict[str, Any]:
    """Build the per-sheet meta entry for an Extract sheet."""
    params_clean = {k: v for k, v in source_params.items() if k != "out"}
    return {
        "kind": "extract",
        "source": {
            "tool": source_tool,
            "params": params_clean,
        },
        "shape": {
            "layout": layout,
            "key_columns": key_columns,
            "value_columns": value_columns,
            "row_count": row_count,
            "header_row": header_row,
            "data_start_row": data_start_row,
            "row_identities": row_identities,
        },
        "named_ranges": named_ranges,
        "excel_tables": excel_tables,
        "refresh_policy": refresh_policy,
    }


def _resolve_refresh_policy(
    overrides: Mapping[str, str] | None,
) -> dict[str, str]:
    policy = dict(DEFAULT_REFRESH_POLICY)
    if not overrides:
        return policy
    for key, val in overrides.items():
        if key not in DEFAULT_REFRESH_POLICY:
            raise ExtractError(f"refresh_policy has unknown key {key!r}")
        if val not in _VALID_POLICY_VALUES:
            raise ExtractError(
                f"refresh_policy[{key!r}]={val!r} not in "
                f"{sorted(_VALID_POLICY_VALUES)}"
            )
        policy[key] = val
    return policy


# ---------------------------------------------------------------------------
# Naming helpers
# ---------------------------------------------------------------------------

_SLUG_RX = re.compile(r"[^A-Za-z0-9]+")


def _slug(text: str) -> str:
    """ASCII-safe lowercase slug usable as part of a defined-name identifier."""
    s = _SLUG_RX.sub("_", str(text)).strip("_").lower()
    return s or "x"


def _table_name(sheet_name: str) -> str:
    """Build an Excel Table name from the sheet name (not yet uniqued)."""
    base = _slug(sheet_name)
    if not base[0].isalpha():
        base = f"t_{base}"
    return f"tbl_{base}"


def _unique_table_name(wb: Workbook, sheet_name: str) -> str:
    """Return a workbook-unique table name derived from the sheet name.

    Excel requires table names to be unique across the whole workbook,
    so on ``target=append`` we may collide with a previous extract's
    table. Suffix with ``_2``, ``_3`` … until free.
    """
    base = _table_name(sheet_name)
    existing: set[str] = set()
    for ws in wb.worksheets:
        try:
            existing.update(ws.tables.keys())
        except Exception:
            continue
    if base not in existing:
        return base
    i = 2
    while f"{base}_{i}" in existing:
        i += 1
    return f"{base}_{i}"


def _unique_range_name(wb: Workbook, base: str) -> str:
    """Return a workbook-unique defined-name. Same rationale as tables."""
    existing = set(wb.defined_names.keys() if wb.defined_names else [])
    if base not in existing:
        return base
    i = 2
    while f"{base}_{i}" in existing:
        i += 1
    return f"{base}_{i}"
