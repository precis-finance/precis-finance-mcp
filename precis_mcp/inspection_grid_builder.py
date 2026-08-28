# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Transform an ``inspect_rows`` engine result into an ``inspection_grid`` block.

Open-core module: a pure ``inspect_rows result → inspection_grid block``
transform with no streaming, Redis, or turn-scoped dependencies. It is the
inspection-grid half of the render boundary — the
companion to :mod:`precis_mcp.table_builder` — so the open MCP transport can
build the grid block without importing the streaming emit machinery.

Consumed by:

1. Précis's SSE emitter for the chat transcript path (``inspect_rows``
   with ``out='render'``).
2. ``mcp_external/server.py`` — the open MCP render seam.

The returned dict is already in the shape the frontend inspection-grid
renderer expects.
"""

from __future__ import annotations

# Rows are capped so a wide inspection never floods the transcript / widget;
# `truncated` flags when the cap (or the engine's own limit) elided rows.
_ROW_CAP = 50


def build_inspection_grid_block(data: dict) -> dict | None:
    """Build an ``inspection_grid`` block from an ``inspect_rows`` result.

    Returns ``None`` when the result is not renderable (not a dict, or an
    error envelope), matching the emitter's "no block" behaviour.
    """
    if not isinstance(data, dict) or "error" in data:
        return None

    rows = list(data.get("rows") or [])
    columns = list(data.get("columns") or [])
    caption = data.get("caption") if isinstance(data.get("caption"), dict) else {}
    title = caption.get("description") or f"Inspection: {data.get('source_key', '')}"
    row_count = data.get("row_count")
    if not isinstance(row_count, int) or isinstance(row_count, bool):
        row_count = len(rows)
    source_truncated = bool(data.get("truncated"))
    preview_truncated = bool(
        data.get("agent_truncated")
        or len(rows) > _ROW_CAP
        or row_count > _ROW_CAP
    )

    block = {
        "type": "inspection_grid",
        "title": title,
        "source_key": data.get("source_key"),
        "columns": columns,
        "rows": rows[:_ROW_CAP],
        "row_count": row_count,
        "limit": data.get("limit"),
        "truncated": bool(source_truncated or preview_truncated),
        "source_truncated": source_truncated,
        "preview_truncated": preview_truncated,
        "caption": caption,
    }
    # Optional commercial capability. Open precis_mcp deployments register no
    # data-ref cache, so their widget contract remains the capped preview.
    if isinstance(data.get("data_ref"), str) and data["data_ref"]:
        block["data_ref"] = data["data_ref"]
    return block
