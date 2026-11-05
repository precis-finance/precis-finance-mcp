# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Metric → Excel emitter.

A thin wrapper over :mod:`precis_mcp.excel.block_emit`: it builds the unified
``financial_table`` block (the same structure the renderers consume) and lets
the shared writer render it, so the metric sheet is structurally identical to
the chat-transcript table and a saved report block.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from precis_mcp.excel.block_emit import report_to_excel


class MetricEmitError(Exception):
    """Raised when the metric can't be materialised (e.g. target conflicts)."""


def metric_to_excel(
    result: dict,
    path: Path,
    *,
    meta_source: dict[str, Any] | None = None,
    target: str = "new",
    overwrite: bool = False,
    sheet_name: str | None = None,
    include_title: bool = True,
) -> Path:
    """Write a metric engine result to Excel via the shared block writer.

    Args:
        result: Engine response dict.
        path: Destination file path.
        meta_source: Optional ``{tool, params, catalogue_version?, user_id?,
            agent_model?}`` dict.
        target: ``"new"`` creates a fresh workbook at ``path``; ``"append"``
            loads the existing workbook and adds the sheet.
        overwrite: On ``target="new"``, allow clobbering an existing file.
            On ``target="append"``, allow replacing a colliding sheet.
        sheet_name: Override the default sheet name (``"Metric"``).
        include_title: Include a standalone title outside the managed region.

    Returns:
        The path written to.
    """
    return report_to_excel(
        result, path,
        default_sheet="Metric",
        error_cls=MetricEmitError,
        meta_source=meta_source,
        target=target,
        overwrite=overwrite,
        sheet_name=sheet_name,
        include_title=include_title,
    )
