# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Shared primitives for Excel emitters.

Exports the colour palette, font/fill/border constants, number-format helper,
sheet-name sanitiser, column-width helper, caption-row writer, header-row
styler, and variance-font resolver. Used by statement, metric, hierarchy and
extract emitters.

Everything here is module-private (leading underscore) but imported explicitly
by sibling emitters — the package boundary is the `precis.excel.__init__`
public API.
"""

from __future__ import annotations

from typing import Any

from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from precis_mcp.cell_format import excel_number_format, favorability

# ---------------------------------------------------------------------------
# Colour palette (Précis brand)
# ---------------------------------------------------------------------------

_SLATE_900 = "0F172A"
_SLATE_200 = "E2E8F0"
_SLATE_100 = "F1F5F9"
_SLATE_50 = "F8FAFC"
_WHITE = "FFFFFF"

# ---------------------------------------------------------------------------
# Reusable style primitives
# ---------------------------------------------------------------------------

_FONT_DEFAULT = Font(name="Calibri", size=11)
_FONT_BOLD = Font(name="Calibri", size=11, bold=True)
_FONT_BOLD_WHITE = Font(name="Calibri", size=11, bold=True, color=_WHITE)
_FONT_ITALIC = Font(name="Calibri", size=11, italic=True)

# Variance colour fonts (Précis semantic palette)
_FONT_POSITIVE = Font(name="Calibri", size=11, color="16A34A")  # green
_FONT_NEGATIVE = Font(name="Calibri", size=11, color="DC2626")  # red

_FILL_HEADER = PatternFill(start_color=_SLATE_900, end_color=_SLATE_900, fill_type="solid")
_FILL_SUBTOTAL = PatternFill(start_color=_SLATE_100, end_color=_SLATE_100, fill_type="solid")
_FILL_TOTAL = PatternFill(start_color=_SLATE_200, end_color=_SLATE_200, fill_type="solid")
_FILL_ALT_ROW = PatternFill(start_color=_SLATE_50, end_color=_SLATE_50, fill_type="solid")

_THIN_SIDE = Side(style="thin")
_BORDER_TOP = Border(top=_THIN_SIDE)
_BORDER_BOTTOM = Border(bottom=_THIN_SIDE)
_BORDER_TOP_BOTTOM = Border(top=_THIN_SIDE, bottom=_THIN_SIDE)

_FONT_CAPTION_TITLE = Font(name="Calibri", size=13, bold=True)
_FONT_CAPTION = Font(name="Calibri", size=10, color="555555")


# ---------------------------------------------------------------------------
# Variance colour helper
# ---------------------------------------------------------------------------

def _variance_font(
    value: float | int | None,
    variance_effect: str,
) -> Font | None:
    """Return a coloured font for variance cells, or None for default.

    The favourable/unfavourable *sign logic* lives once in
    ``precis_mcp.cell_format.favorability`` (the single source the add-in and
    React also read); this maps that signal to the brand fonts.
    """
    fav = favorability(value, variance_effect)
    if fav is None:
        return None
    return _FONT_POSITIVE if fav == "favorable" else _FONT_NEGATIVE


# ---------------------------------------------------------------------------
# Number format strings (en-dash negatives: U+2013)
# ---------------------------------------------------------------------------

def _number_format(fmt: str, decimals: int = 0) -> str:
    """Excel custom number-format string — delegates to the open single source."""
    return excel_number_format(fmt, decimals)


# ---------------------------------------------------------------------------
# Sheet name helper
# ---------------------------------------------------------------------------

def _sheet_name(raw: str) -> str:
    """Sanitize and truncate a sheet name to 31 chars (Excel limit)."""
    # Remove characters invalid in Excel sheet names
    for ch in ("\\", "/", "*", "?", ":", "[", "]"):
        raw = raw.replace(ch, "_")
    return raw[:31]


# ---------------------------------------------------------------------------
# Column width estimation
# ---------------------------------------------------------------------------

def _estimate_width(value: Any) -> float:
    """Estimate column width from a cell value."""
    if value is None:
        return 8
    s = str(value)
    # Rough heuristic: character count + small buffer
    return max(8, len(s) + 2)


def _auto_fit_columns(ws: Worksheet) -> None:
    """Set column widths based on content (approximate)."""
    for col_idx, col_cells in enumerate(ws.columns, start=1):
        max_width = 8.0
        col_letter = get_column_letter(col_idx)
        for cell in col_cells:
            width = _estimate_width(cell.value)
            if width > max_width:
                max_width = width
        ws.column_dimensions[col_letter].width = min(max_width, 50)


# ---------------------------------------------------------------------------
# Caption / metadata header rows
# ---------------------------------------------------------------------------

def _write_caption_rows(ws: Worksheet, caption: dict) -> int:
    """Write report metadata rows at the top of a sheet.

    Returns the next available row number (1-based) after the caption block.
    If *caption* is empty, returns 1 (no rows written).
    """
    if not caption:
        return 1

    row = 1

    # Title / description
    desc = caption.get("description")
    if desc:
        cell = ws.cell(row=row, column=1, value=desc)
        cell.font = _FONT_CAPTION_TITLE
        row += 1

    # Scenarios
    scenarios = caption.get("scenarios")
    if scenarios:
        cell = ws.cell(row=row, column=1, value=f"Scenarios: {', '.join(scenarios)}")
        cell.font = _FONT_CAPTION
        row += 1

    # Period
    period = caption.get("period")
    if period:
        cell = ws.cell(row=row, column=1, value=f"Period: {period}")
        cell.font = _FONT_CAPTION
        row += 1

    # Filters
    filters = caption.get("filters")
    if filters and isinstance(filters, dict):
        parts = [f"{k}={v}" for k, v in filters.items()]
        cell = ws.cell(row=row, column=1, value=f"Filters: {', '.join(parts)}")
        cell.font = _FONT_CAPTION
        row += 1

    # Scale
    scale = caption.get("scale")
    if scale:
        cell = ws.cell(row=row, column=1, value=f"Scale: {scale}")
        cell.font = _FONT_CAPTION
        row += 1

    # Generated at
    generated_at = caption.get("generated_at")
    if generated_at:
        cell = ws.cell(row=row, column=1, value=generated_at)
        cell.font = _FONT_CAPTION
        row += 1

    # Blank separator row
    row += 1

    return row


# ---------------------------------------------------------------------------
# Column header styling
# ---------------------------------------------------------------------------

def _apply_header_row(ws: Worksheet, row_num: int, col_count: int) -> None:
    """Style a column-header row with brand colours."""
    for col in range(1, col_count + 1):
        cell = ws.cell(row=row_num, column=col)
        cell.font = _FONT_BOLD_WHITE
        cell.fill = _FILL_HEADER
        cell.alignment = Alignment(horizontal="center", vertical="center")


# ---------------------------------------------------------------------------
# Metadata decimals extractor
# ---------------------------------------------------------------------------

def _get_decimals_from_metadata(metadata: dict) -> int:
    """Extract decimals hint from result metadata."""
    val = metadata.get("decimals")
    return val if val is not None else 0
