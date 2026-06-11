# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Scope resolver — scenario lock resolution.

Resolves the ``semantic.scenarios.locks`` JSON array into leaf-level
dimension sets, and provides ``is_cell_locked`` for per-entry enforcement.

Security scope (profile-based) is resolved separately by
:mod:`precis_mcp.engine.scope_enforcer` and intersected with commit/entry
scope in :mod:`precis.planning.write`.

Period handling:
    Lock conditions use ``period_from`` / ``period_to`` (inclusive range)
    instead of listing every period.  The resolver expands these to leaf
    period IDs using the catalogue.

Consumers:
- ``save_plan_entries`` (per-entry lock check)
- ``lock_tools.py`` (lock display and scope enforcement)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from precis_mcp.engine.catalogue import Catalogue

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lock resolution — parse + expand scenario locks to leaf-level sets
# ---------------------------------------------------------------------------


def _expand_period_range(
    period_from: str,
    period_to: str,
    catalogue: "Catalogue",
    ch_client,
) -> list[str]:
    """Expand a period_from/period_to range to leaf period IDs.

    Uses the period dimension's source table (gl.dim_period) to find all
    periods in the range.  Period IDs are YYYY-MM strings, so lexicographic
    ordering works.
    """
    period_dim = catalogue.dimensions.get("period")
    if period_dim is None or period_dim.source is None:
        # Fallback: generate YYYY-MM sequence (works for monthly granularity)
        return _generate_monthly_range(period_from, period_to)

    result = ch_client.query(
        f"SELECT DISTINCT toString({period_dim.source.key_column}) "
        f"FROM {period_dim.source.table} "
        f"WHERE toString({period_dim.source.key_column}) >= {{pf:String}} "
        f"AND toString({period_dim.source.key_column}) <= {{pt:String}} "
        f"ORDER BY toString({period_dim.source.key_column})",
        parameters={"pf": period_from, "pt": period_to},
    )
    return [row[0] for row in result.result_rows]


def _generate_monthly_range(start: str, end: str) -> list[str]:
    """Fallback: generate YYYY-MM strings between start and end inclusive."""
    periods = []
    year, month = int(start[:4]), int(start[5:7])
    end_year, end_month = int(end[:4]), int(end[5:7])
    while (year, month) <= (end_year, end_month):
        periods.append(f"{year:04d}-{month:02d}")
        month += 1
        if month > 12:
            month = 1
            year += 1
    return periods


def resolve_lock_condition(
    condition: dict,
    catalogue: "Catalogue",
    ch_client,
) -> dict[str, set[str]]:
    """Resolve one lock condition to leaf-level dimension sets.

    A condition is a dict like::

        {"period_from": "2026-01", "period_to": "2026-03",
         "division": "Cloud & Infrastructure"}

    Returns a dict of leaf_dimension_key -> set of locked leaf IDs.
    All dimensions in the result must match (AND) for a cell to be locked
    by this condition.

    Special keys:
    - ``period_from`` / ``period_to``: expanded to period leaf IDs
    - All other keys: resolved via filter_resolver.resolve_single_value
    """
    from precis_mcp.engine.filter_resolver import (
        FilterResolutionError,
        resolve_single_value,
    )

    resolved: dict[str, set[str]] = {}

    # Handle period range
    period_from = condition.get("period_from")
    period_to = condition.get("period_to")
    if period_from and period_to:
        periods = _expand_period_range(period_from, period_to, catalogue, ch_client)
        resolved["period"] = set(periods)
    elif period_from or period_to:
        # Single-bound: treat as same start/end
        p: str = period_from or period_to  # type: ignore[assignment]
        resolved["period"] = {p}

    # Handle all other dimension keys
    for key, value in condition.items():
        if key in ("period_from", "period_to", "_type"):
            continue

        if isinstance(value, list):
            # Multiple values: resolve each and union
            all_leaves: set[str] = set()
            leaf_dim_key = None
            for v in value:
                try:
                    ldk, leaves = resolve_single_value(key, str(v), catalogue, ch_client)
                    leaf_dim_key = ldk
                    all_leaves.update(leaves)
                except FilterResolutionError as exc:
                    logger.warning("Lock resolution failed for %s=%s: %s", key, v, exc)
            if leaf_dim_key and all_leaves:
                if leaf_dim_key in resolved:
                    resolved[leaf_dim_key] |= all_leaves
                else:
                    resolved[leaf_dim_key] = all_leaves
        else:
            try:
                leaf_dim_key, leaves = resolve_single_value(
                    key, str(value), catalogue, ch_client,
                )
                if leaf_dim_key in resolved:
                    resolved[leaf_dim_key] |= set(leaves)
                else:
                    resolved[leaf_dim_key] = set(leaves)
            except FilterResolutionError as exc:
                logger.warning("Lock resolution failed for %s=%s: %s", key, value, exc)

    return resolved


def load_scenario_locks(
    scenario_id: str,
    ch_client,
) -> list[dict]:
    """Load the locks JSON array from semantic.scenarios.

    Returns an empty list if the scenario has no locks or the column is absent.
    """
    from precis_mcp.engine.scenario_store import ScenarioStore

    try:
        locks = ScenarioStore(ch_client).get_locks_metadata(scenario_id)
        return locks or []
    except Exception as exc:
        logger.warning("Failed to load locks for %s: %s", scenario_id, exc)
        return []


def resolve_scenario_locks(
    scenario_id: str,
    catalogue: "Catalogue",
    ch_client,
) -> list[dict[str, set[str]]]:
    """Load and resolve all lock conditions for a scenario.

    Returns a list of resolved conditions (each is a dict of
    leaf_dim_key -> set of locked IDs).  A cell is locked if it matches
    ANY condition (OR across conditions).
    """
    raw_locks = load_scenario_locks(scenario_id, ch_client)
    resolved = []
    for cond in raw_locks:
        r = resolve_lock_condition(cond, catalogue, ch_client)
        if r:  # skip empty conditions
            resolved.append(r)
    return resolved


# ---------------------------------------------------------------------------
# Cell-level lock check
# ---------------------------------------------------------------------------


def is_cell_locked(
    cell: dict[str, str],
    resolved_locks: list[dict[str, set[str]]],
) -> bool:
    """Check if a single cell (entry) is locked by any resolved lock condition.

    Args:
        cell: Dict of dimension_key -> value (e.g. {"account": "4100",
              "cost_centre": "CC-SENG-01", "period": "2026-03"}).
        resolved_locks: Output of ``resolve_scenario_locks``.

    A cell is locked if it matches ANY condition (OR across conditions).
    Within a condition, ALL specified dimensions must match (AND within).
    Dimensions not specified in a condition are treated as wildcards.
    """
    for condition in resolved_locks:
        matches_all_dims = True
        for dim_key, locked_values in condition.items():
            cell_value = cell.get(dim_key)
            if cell_value is None:
                # Cell doesn't have this dimension — can't match
                matches_all_dims = False
                break
            if cell_value not in locked_values:
                matches_all_dims = False
                break
        if matches_all_dims:
            return True
    return False

