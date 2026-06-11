# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Commercial extension hooks for the open read tools.

The open read path (``run_statement`` / ``run_metric``) is ClickHouse-only and
writes nothing downstream. Two extension points are registered by the commercial
product at startup (in ``agui``); an open deployment leaves them unregistered:

- **Output renderers** — ``out='report'`` dispatches to the registered renderer
  (the SPA report builder). Unregistered → ``out='report'`` is unsupported.
- **Chart-result cache** — the ``data_ref`` a later ``eval_chart_transform``
  reads is produced by the registered cache (Redis). Unregistered → the open
  read path returns figures with **no ``data_ref`` and no Redis write** (the
  open core's last read-path Redis dependency, ADR-0004 / ADR-0006).
- **Excel dispatch** — ``out='excel'`` (and the inspection / hierarchy Excel
  paths) write a workbook through the file-storage subsystem (commercial).
  Unregistered → ``out='excel'`` is unsupported and the open core carries no
  ``files``/``excel`` dependency at all (ADR-0004 Correction 2).

These are simple process-global seams (single-process server; registered once at
import), mirroring the MCP render-builder seam.
"""

from __future__ import annotations

from typing import Any, Callable

_OUTPUT_RENDERERS: dict[str, Callable[..., Any]] = {}
_CHART_CACHE: Callable[..., Any] | None = None
_EXCEL_DISPATCH: Any | None = None


def register_output_renderer(out_mode: str, renderer: Callable[..., Any]) -> None:
    """Register the handler for a non-core ``out`` mode (e.g. ``'report'``)."""
    _OUTPUT_RENDERERS[out_mode] = renderer


def get_output_renderer(out_mode: str) -> Callable[..., Any] | None:
    return _OUTPUT_RENDERERS.get(out_mode)


def unregister_output_renderer(out_mode: str) -> None:
    """Drop a registered renderer (test isolation; no-op if absent)."""
    _OUTPUT_RENDERERS.pop(out_mode, None)


def register_chart_cache(cache: Callable[..., Any]) -> None:
    """Register the chart-result cache that mints a ``data_ref`` for a result."""
    global _CHART_CACHE
    _CHART_CACHE = cache


def get_chart_cache() -> Callable[..., Any] | None:
    return _CHART_CACHE


def register_excel_dispatch(dispatch: Any) -> None:
    """Register the commercial Excel-output dispatcher (files-backed).

    ``dispatch`` exposes ``dispatch_excel_out`` / ``dispatch_inspection_excel``
    / ``dispatch_hierarchy_excel``. Unregistered → ``out='excel'`` is
    unsupported (open core has no file subsystem).
    """
    global _EXCEL_DISPATCH
    _EXCEL_DISPATCH = dispatch


def get_excel_dispatch() -> Any | None:
    return _EXCEL_DISPATCH


def unregister_excel_dispatch() -> None:
    """Drop the registered Excel dispatcher (test isolation; no-op if absent)."""
    global _EXCEL_DISPATCH
    _EXCEL_DISPATCH = None
