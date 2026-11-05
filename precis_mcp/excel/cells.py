# SPDX-License-Identifier: Elastic-2.0
"""Write analytical source values without interpreting strings as formulas."""

from typing import Any

from openpyxl.cell.cell import Cell
from openpyxl.styles import Alignment, Font
from openpyxl.worksheet.worksheet import Worksheet


def write_source_cell(ws: Worksheet, *, row: int, column: int, value: Any) -> Cell:
    """Preserve source strings literally and retain native numeric/date types.

    openpyxl infers formulas for strings beginning with ``=`` and error cells
    for Excel error tokens. Source values are data, so explicitly store strings
    as text. Intentional workbook-edit formulas use their separate write path.
    """
    cell = ws.cell(row=row, column=column)
    cell.value = value
    if isinstance(value, str):
        cell.data_type = "s"
    return cell


def write_footer_row(ws: Worksheet, *, row: int, columns: int, text: str) -> None:
    """Unmerged, unwrapped context row shared by report and flat exports."""
    write_source_cell(ws, row=row, column=1, value=text)
    for column in range(1, columns + 1):
        footer = ws.cell(row=row, column=column)
        footer.number_format = "General"
        footer.alignment = Alignment(horizontal="left", vertical="top", wrap_text=False)
        footer.font = Font(name="Calibri", size=9, color="667580")
    if not any(ws.cell(row, c).value is not None for c in range(columns + 1, ws.max_column + 1)):
        ws.row_dimensions[row].height = None
