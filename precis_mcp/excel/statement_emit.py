# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Statement → Excel emitter.

A thin wrapper over :mod:`precis_mcp.excel.block_emit`: it builds the unified
``financial_table`` block (the same structure the renderers consume) and lets
the shared writer render it. The dimensioned-statement crosstab and the styling
all live in the block builder / block writer now, so there is one report-table
Excel path for both statements and metrics.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from precis_mcp.excel.block_emit import report_to_excel


class StatementEmitError(Exception):
    """Raised when the statement can't be materialised (e.g. target conflicts)."""


def statement_to_excel(
    result: dict,
    path: Path,
    catalogue: Any = None,
    *,
    meta_source: dict[str, Any] | None = None,
    target: str = "new",
    overwrite: bool = False,
    sheet_name: str | None = None,
    include_title: bool = True,
) -> Path:
    """Write a statement engine result to Excel via the shared block writer.

    Args:
        result: Unified engine response dict (``scenarios`` + grain-tagged
            ``rows``, optionally ``dimensions`` for a crosstab).
        path: Destination file path.
        catalogue: Accepted for call-site compatibility; unused — ``style`` /
            ``indent`` / ``format`` are already embedded in the block rows.
        meta_source: Optional ``{tool, params, catalogue_version?, user_id?,
            agent_model?}`` dict.
        target: ``"new"`` creates a fresh workbook; ``"append"`` adds a sheet.
        overwrite: On ``target="new"``, allow clobbering an existing file.
            On ``target="append"``, allow replacing a colliding sheet.
        sheet_name: Override the default sheet name (``"Statement"``).
        include_title: Include a standalone title outside the managed region.

    Returns:
        The path written to.
    """
    _ = catalogue
    return report_to_excel(
        result, path,
        default_sheet="Statement",
        error_cls=StatementEmitError,
        meta_source=meta_source,
        target=target,
        overwrite=overwrite,
        sheet_name=sheet_name,
        include_title=include_title,
    )
