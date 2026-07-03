# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Stage 5 (Output) of the metric engine pipeline.

Takes complete computed results and shapes them into the response schema.
No I/O, no database — pure computation.

Dimension display/sort:
  The formatter receives an optional ``dim_formats`` dict that maps each
  dimension column name to a :class:`DimensionFormat` descriptor.  This is
  populated by the orchestrator from catalogue metadata + a source-table
  lookup.  The formatter never queries the database itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from typing import TYPE_CHECKING

from precis_mcp.engine.catalogue import Catalogue
from precis_mcp.engine.types import ROLLED_UP, DimensionKey, ResultData

if TYPE_CHECKING:
    from precis_mcp.engine.catalogue import Metric


# ---------------------------------------------------------------------------
# Dimension display / sort descriptor
# ---------------------------------------------------------------------------

@dataclass
class DimensionFormat:
    """Per-dimension formatting instructions, resolved by the orchestrator.

    ``display_attr``: attribute name whose value replaces the raw code in
    output.  Empty string means use the raw code as-is.

    ``sort_attr``: attribute name whose value is used for row/group ordering.
    Empty string means no sort (preserve query order).

    ``lookup``: maps raw leaf codes to their attribute values.
    E.g. ``{"CC-CLOUD-01": {"name": "Cloud - AWS Team"}}``
    """
    display_attr: str = ""
    sort_attr: str = ""
    lookup: dict[str, dict[str, str | None]] = field(default_factory=dict)

    def display_value(self, code: str) -> str:
        """Return the display value for a code, falling back to the code itself."""
        if not self.display_attr:
            return code
        attrs = self.lookup.get(code, {})
        return attrs.get(self.display_attr) or code

    def sort_value(self, code: str) -> str:
        """Return the sort value for a code, falling back to the code itself."""
        if not self.sort_attr:
            return code
        attrs = self.lookup.get(code, {})
        return attrs.get(self.sort_attr) or code


# ---------------------------------------------------------------------------
# FormatterBlock — minimal input descriptor (avoids importing resolver)
# ---------------------------------------------------------------------------

@dataclass
class FormatterBlock:
    alias: str
    scenario_key: str
    metric_keys: list[str]      # without separators
    display_items: list[str]    # with separators, preserving order
    is_statement: bool          # True if model was statement:xxx
    display_format: str = ""    # from catalogue scenario (e.g. "percent")
    color_code: bool = False    # from catalogue scenario — variance column?


# ---------------------------------------------------------------------------
# Scaling constants
# ---------------------------------------------------------------------------

SCALE_LABELS: dict[int, str] = {
    0: "",
    3: "€ thousands",
    6: "€ millions",
    9: "€ billions",
}

# Default decimals per scale tier (used when caller doesn't specify)
_DEFAULT_DECIMALS_BY_SCALE: dict[int, int] = {
    0: 0,     # units: no decimals
    3: 0,     # thousands: no decimals
    6: 1,     # millions: 1 decimal
    9: 2,     # billions: 2 decimals
}

# Decimals for non-scaled formats (always applied regardless of scale)
_EXEMPT_DECIMALS: dict[str, int] = {
    "percent": 1,
    "number": 0,
}


# ---------------------------------------------------------------------------
# Rounding / scaling helpers
# ---------------------------------------------------------------------------

def _is_scale_exempt(metric: "Metric") -> bool:
    return getattr(metric, "scale_exempt", False) or metric.format == "percent"


def _scale_value(value: float | None, metric: "Metric", scale: int) -> float | None:
    """Apply the scaling divisor to a single value, WITHOUT rounding.

    Rounding is deferred to the display layer (render / agent payload) so Excel
    and other precision-sensitive consumers receive the full-precision figure.
    scale_exempt metrics (percentages, FTEs, hours, ratios) are never divided.
    """
    if value is None:
        return None
    if _is_scale_exempt(metric):
        return value
    divisor = 10 ** scale if scale > 0 else 1
    return value / divisor if divisor > 1 else value


def resolve_decimals(metric: "Metric", scale: int, decimals: int | None) -> int:
    """Resolve the display decimal places for a metric.

    The single source of truth for "how many decimals should this figure show".
    Consumers apply it themselves: the renderers round at display time, the
    agent payload rounds before returning, and Excel feeds it to its number
    format (storing full precision, displaying rounded). ``decimals=None`` falls
    back to tier defaults.
    """
    if _is_scale_exempt(metric):
        return decimals if decimals is not None else _EXEMPT_DECIMALS.get(metric.format, 0)
    if decimals is not None:
        return decimals
    return _DEFAULT_DECIMALS_BY_SCALE.get(scale, 0)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _uses_statements(blocks: list[FormatterBlock]) -> bool:
    return any(b.is_statement for b in blocks)


def _get_value(
    results: ResultData,
    scenario_key: str,
    dim_key: DimensionKey,
    metric_key: str,
) -> float | None:
    """Safely retrieve a value from results, returning None if missing."""
    scenario_data = results.get(scenario_key, {})
    dim_data = scenario_data.get(dim_key, {})
    return dim_data.get(metric_key)


def _all_zero_or_none(values: dict[str, float | None]) -> bool:
    return all(v is None or v == 0.0 for v in values.values())


def _collect_dimension_keys(
    results: ResultData,
    blocks: list[FormatterBlock],
    dimensions: list[str] | None = None,
    dim_formats: dict[str, DimensionFormat] | None = None,
) -> list[DimensionKey]:
    """Collect all dimension keys present across all blocks.

    If dim_formats contains a sort_attr for any dimension, rows are sorted
    by that attribute's value.  Otherwise falls back to natural sort on codes.
    """
    keys: set[DimensionKey] = set()
    for block in blocks:
        scenario_data = results.get(block.scenario_key, {})
        keys.update(scenario_data.keys())

    if not dim_formats or not dimensions:
        return sorted(keys)

    def _sort_key(dk: DimensionKey) -> tuple[str, ...]:
        result: list[str] = []
        for i, dim_name in enumerate(dimensions):
            code = dk[i] if i < len(dk) else ""
            fmt = dim_formats.get(dim_name)
            if fmt:
                result.append(fmt.sort_value(code))
            else:
                result.append(code)
        return tuple(result)

    return sorted(keys, key=_sort_key)


def _live_dimensions(dim_key: DimensionKey, dimensions: list[str]) -> list[str]:
    """Dimension names not rolled up in this grain, in declaration order."""
    return [
        dim for i, dim in enumerate(dimensions)
        if i < len(dim_key) and dim_key[i] != ROLLED_UP
    ]


def _dim_key_to_dict(
    dim_key: DimensionKey,
    dimensions: list[str],
    dim_formats: dict[str, DimensionFormat] | None = None,
) -> dict[str, str]:
    """Convert a dimension key tuple to a dict, applying display values.

    Rolled-up positions (ROLLED_UP, from subtotal / grand-total grains) are
    omitted, so the dict holds only the dimensions live at this grain.
    """
    result: dict[str, str] = {}
    for i, dim_name in enumerate(dimensions):
        if i >= len(dim_key):
            continue
        code = dim_key[i]
        if code == ROLLED_UP:
            continue
        fmt = dim_formats.get(dim_name) if dim_formats else None
        result[dim_name] = fmt.display_value(code) if fmt else code
    return result


def _dim_key_to_codes(
    dim_key: DimensionKey,
    dimensions: list[str],
) -> dict[str, str]:
    """The raw leaf code per live dimension, parallel to `_dim_key_to_dict`.

    Surfaces the underlying code (e.g. a cost-centre id) alongside the display
    name so a consumer can drill without a follow-up hierarchy lookup. Mirrors
    `_dim_key_to_dict`'s rolled-up handling, so `dimension_codes` and
    `dimensions` carry the same keys.
    """
    result: dict[str, str] = {}
    for i, dim_name in enumerate(dimensions):
        if i >= len(dim_key):
            continue
        code = dim_key[i]
        if code == ROLLED_UP:
            continue
        result[dim_name] = code
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _grain_of(dim_key: DimensionKey, dimensions: list[str]) -> str:
    """Classify a row's grain from its (possibly ROLLED_UP) dimension key."""
    n = len(dimensions)
    if n == 0:
        return "detail"
    live = len(_live_dimensions(dim_key, dimensions))
    if live == n:
        return "detail"
    if live == 0:
        return "grand_total"
    return "subtotal"


def format_response(
    results: ResultData,
    blocks: list[FormatterBlock],
    catalogue: Catalogue,
    dimensions: list[str],
    period_start: str,
    period_end: str,
    dim_formats: dict[str, DimensionFormat] | None = None,
    scale: int = 0,
    decimals: int | None = None,
) -> dict:
    """Format engine results into the unified result schema.

    One self-describing shape for metric and statement breakdowns alike: a flat
    list of grain-tagged ``rows``, each
    ``{grain, dimensions, dimension_codes, item, values}``, plus the column list
    (``scenarios``) and the breakdown axes (``dimensions``). ``dimension_codes``
    carries the raw leaf code per axis (e.g. a cost-centre id) alongside the
    display name in ``dimensions``, so a consumer can drill without a follow-up
    hierarchy lookup.

    - ``kind`` is ``"statement"`` when any block renders a statement, else
      ``"metric"``; it guides layout, not row shape.
    - One row per (dimension key × item); ``item`` is the metric / statement
      line. Statement separators become ``item.separator_above``.
    - ``values`` are keyed by scenario alias (uniqueness enforced upstream).
    - ``grain`` is ``detail`` / ``subtotal`` / ``grand_total``, derived from how
      many dimensions are live (rolled-up positions decode to ``ROLLED_UP``).
    """
    kind = "statement" if _uses_statements(blocks) else "metric"
    item_keys = blocks[0].display_items if blocks else []

    all_dim_keys = _collect_dimension_keys(results, blocks, dimensions, dim_formats)

    rows: list[dict] = []
    for dim_key in all_dim_keys:
        dim_values = _dim_key_to_dict(dim_key, dimensions, dim_formats)
        dim_codes = _dim_key_to_codes(dim_key, dimensions)
        grain = _grain_of(dim_key, dimensions)
        pending_separator = False
        for ik in item_keys:
            if ik == "separator":
                pending_separator = True
                continue
            metric = catalogue.metrics.get(ik)
            if metric is None:
                continue
            values: dict[str, float | None] = {}
            for block in blocks:
                raw = _get_value(results, block.scenario_key, dim_key, ik)
                if block.display_format == "percent":
                    # Variance-% columns: value is already a percentage; not scaled.
                    values[block.alias] = raw
                else:
                    values[block.alias] = _scale_value(raw, metric, scale)
            if getattr(metric, "hide_if_zero", False) and _all_zero_or_none(values):
                continue  # keep pending_separator for the next visible line
            item = {
                "key": metric.key,
                "label": metric.label,
                "style": getattr(metric, "style", "default"),
                "indent": getattr(metric, "indent", 0),
                "format": getattr(metric, "format", "number"),
                "decimals": resolve_decimals(metric, scale, decimals),
                "variance_effect": getattr(metric, "variance_effect", "natural"),
                "separator_above": pending_separator or getattr(metric, "separator_above", False),
            }
            pending_separator = False
            rows.append({
                "grain": grain,
                "dimensions": dim_values,
                "dimension_codes": dim_codes,
                "item": item,
                "values": values,
            })

    response: dict = {
        "kind": kind,
        "dimensions": list(dimensions),
        "scenarios": [
            {
                "alias": b.alias,
                "format": b.display_format or None,
                "variance": b.color_code,
                # Variance-% columns format uniformly as percent regardless of
                # the per-metric format, so their display decimals live on the
                # column, not the row item.
                **(
                    {"decimals": decimals if decimals is not None else 1}
                    if b.display_format == "percent"
                    else {}
                ),
            }
            for b in blocks
        ],
        "period": {"start": period_start, "end": period_end},
        "scale": scale,
        "rows": rows,
    }
    if decimals is not None:
        response["decimals"] = decimals
    return response
