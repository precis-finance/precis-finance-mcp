# SPDX-License-Identifier: Elastic-2.0
"""Pure worksheet projection shared by static extracts and the add-in.

Worksheet values retain engine precision. Number formats are presentation only.
This module deliberately has no workbook, transport or authorization side effects.
"""
from __future__ import annotations

from typing import Any, Mapping

from precis_mcp.cell_format import excel_number_format, report_cell_number_format
from precis_mcp.excel.formulas import contains_precis_function

WORKSHEET_META_KEY = "precis/worksheet"


def caption_text(caption: Mapping[str, Any] | None) -> str:
    """One inline context bar, in the same order as the Précis table caption.

    The report description is a standalone title, never part of this footer.
    """
    caption = caption or {}
    parts = []
    if caption.get("scenarios"):
        parts.append(" · ".join(map(str, caption["scenarios"])))
    if caption.get("period"):
        parts.append(str(caption["period"]))
    if caption.get("filters"):
        parts.append(", ".join(f"{key}: {', '.join(map(str, value)) if isinstance(value, list) else value}"
                               for key, value in caption["filters"].items()))
    if caption.get("dimensions"):
        parts.append(", ".join(map(str, caption["dimensions"])))
    if caption.get("scale"):
        parts.append(str(caption["scale"]))
    if isinstance(caption.get("duration_ms"), (int, float)):
        parts.append(f"{caption['duration_ms']:,} ms")
    if caption.get("generated_at"):
        parts.append(f"Data as at {caption['generated_at']}")
    text = " · ".join(parts)
    if len(text) > 32767:
        raise ValueError("Report context exceeds Excel's cell text limit")
    return text


def _append_footer(grid, formats, caption, width):
    if width:
        grid.append([caption_text(caption)] + [""] * (width - 1))
        formats.append(["General"] * width)


def report_body_rows(block: Mapping[str, Any]) -> list[dict]:
    """Shared Excel row policy: skip decoration, retain blank semantic rows."""
    return [row for row in block.get("rows", []) if row.get("row_type") != "separator"]


def report_worksheet(block: Mapping[str, Any]) -> dict:
    """Live report rectangle: headers, semantic body, one context footer.

    The static report writer uses the same block and per-cell format resolver.
    Neither path changes the engine's percentage units or numeric precision.
    """
    columns = block.get("columns", [])
    grid = []
    if any(c.get("group") for c in columns):
        groups, previous = [""], None
        for column in columns[1:]:
            group = column.get("group")
            groups.append(group if group and group != previous else "")
            previous = group
        grid.append(groups)
    grid.append([c.get("label", c["key"]) for c in columns])
    formats = [["General"] * len(columns) for _ in grid]
    header_rows = len(grid)
    for row in report_body_rows(block):
        values = row.get("values", {})
        grid.append([row.get("label", "")] + [values.get(c["key"]) if values.get(c["key"]) is not None else "" for c in columns[1:]])
        formats.append(["General"] + [report_cell_number_format(row, c) for c in columns[1:]])
    data_rows = len(grid) - header_rows
    _append_footer(grid, formats, block.get("caption"), len(columns))
    return {"version": 1, "grid": grid, "number_formats": formats,
            "column_keys": [c["key"] for c in columns], "header_rows": header_rows,
            "data_rows": data_rows, "footer_rows": int(bool(columns))}


def live_formula_anchors(workbook) -> list[str]:
    """Inspect formula text without executing or resaving the workbook."""
    from openpyxl.worksheet.formula import ArrayFormula
    anchors = []
    for sheet in workbook.worksheets:
        for row in sheet:
            for cell in row:
                formula = cell.value.text if isinstance(cell.value, ArrayFormula) else cell.value if cell.data_type == "f" else None
                if isinstance(formula, str) and contains_precis_function(formula):
                    anchors.append(f"{sheet.title}!{cell.coordinate}")
    return anchors


def flatten_extract(result: Mapping[str, Any]) -> tuple[list[str], list[str], list[dict], list[dict]]:
    """Project detail grains to stable keys and scenario-alias columns."""
    dimensions = list(result.get("dimensions", []))
    value_columns = [s["alias"] for s in result.get("scenarios", []) if s.get("alias")]
    kind = result.get("kind", "metric")
    detail = [r for r in result.get("rows", []) if r.get("grain") == "detail"]
    multi_metric = kind == "metric" and len({r.get("item", {}).get("key") for r in detail} - {None, ""}) > 1
    data_rows, identities = [], []
    saw_item = False
    for row in detail:
        identity = {d: row.get("dimensions", {}).get(d) for d in dimensions}
        item = row.get("item", {}).get("key")
        if item and (kind == "statement" or multi_metric):
            identity["line_item" if kind == "statement" else "metric"] = item
            saw_item = True
        data_rows.append({**identity, **{alias: row.get("values", {}).get(alias) for alias in value_columns}})
        identities.append(identity)
    keys = dimensions + (["line_item" if kind == "statement" else "metric"] if saw_item else [])
    return keys, value_columns, data_rows, identities


def extract_worksheet(result: Mapping[str, Any]) -> dict:
    """Return a versioned flat grid plus per-cell formats; never round values."""
    keys, values, rows, _ = flatten_extract(result)
    columns = keys + values
    grid = [columns] + [["" if row.get(col) is None else row[col] for col in columns] for row in rows]
    formats = [["General"] * len(columns)]
    scenarios = {s["alias"]: s for s in result.get("scenarios", []) if s.get("alias")}
    detail = [r for r in result.get("rows", []) if r.get("grain") == "detail"]
    for row in detail:
        item = row.get("item", {})
        nf = ["General"] * len(keys)
        for alias in values:
            scenario = scenarios[alias]
            # A percentage comparison is independent of the metric's unit.
            fmt = "percent" if scenario.get("format") == "percent" else item.get("format", scenario.get("format", "number"))
            decimals = scenario.get("decimals", 1) if scenario.get("format") == "percent" else item.get("decimals", result.get("decimals", 0))
            nf.append(excel_number_format(fmt, decimals or 0, percent_decimals=decimals or 0 if fmt == "percent" else None))
        formats.append(nf)
    _append_footer(grid, formats, result.get("caption"), len(columns))
    return {"version": 1, "grid": grid, "number_formats": formats,
            "key_columns": keys, "value_columns": values,
            "header_rows": 1, "data_rows": len(rows), "footer_rows": int(bool(columns))}


def worksheet_options(params: Mapping[str, Any], tool_name: str) -> dict | None:
    """Validate an explicit transport opt-in without changing ordinary callers."""
    meta = params.get("_meta")
    if not isinstance(meta, Mapping) or WORKSHEET_META_KEY not in meta:
        return None
    options = meta[WORKSHEET_META_KEY]
    if tool_name not in {"run_metric", "run_statement"}:
        raise ValueError("Worksheet presentation is supported only for financial reads")
    if not isinstance(options, Mapping) or options.get("version") != 1:
        raise ValueError("Unsupported worksheet presentation version")
    if set(options) - {"version"}:
        raise ValueError("Unknown worksheet presentation option")
    return dict(options)
