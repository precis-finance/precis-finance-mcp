# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Security scope enforcement for the engine read and write paths.

Checks domain access and resolves dimension scope constraints into
positive-inclusion filter sets compatible with the retriever.

Each user holds exactly one profile, so each (user, scenario, tool_type)
maps to a single ``ScopeSpec`` (or None = unrestricted). Semantics per
profile_model_spec §5.5–5.6:

  * Within an axis (domains or a given dim key), allow and deny may
    coexist. Effective set = (allow ∨ universe) \\ deny — deny wins.
  * AND across dim keys within a scope. Silence on a key means no
    restriction on that key.

For multi-scenario read queries, per-scenario scopes are resolved
independently and checked for consistency — if the effective filters
differ across scenarios the query is rejected with an actionable error.

For write tools, ``resolve_write_scope`` resolves the scope against a
``PlanDataset``'s domain, returning dimension filters for per-entry
validation or SQL WHERE clause generation.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from precis_mcp.engine.catalogue import Catalogue
from precis_mcp.engine.filter_resolver import (
    _find_filter_target,
    _map_to_view_column,
    _resolve_derived_filter,
    _resolve_leaf_filter,
    _resolve_ragged_filter,
)
from precis_mcp.engine.resolver import ResolverError

if TYPE_CHECKING:
    from precis_mcp.auth import ScopeSpec
    from precis_mcp.engine.catalogue import PlanDataset

logger = logging.getLogger(__name__)


class ScopeViolationError(ResolverError):
    """Raised when a scope check fails — domain denied or dimension empty."""


# ---------------------------------------------------------------------------
# Domain gating
# ---------------------------------------------------------------------------


def check_domain_access(scope: ScopeSpec | None, domain: str) -> None:
    """Raise ScopeViolationError if the scope does not permit the domain.

    Semantics (profile_model_spec §5.5):
      * ``scope is None`` or ``scope.domains is None`` → unrestricted.
      * ``deny`` wins: if ``domain`` is in deny, denied.
      * Otherwise: if ``allow`` is None (silent), domain is permitted;
        if ``allow`` is a list, domain must appear in it.
    """
    if scope is None or scope.domains is None:
        return
    doms = scope.domains
    if doms.deny and domain in doms.deny:
        raise ScopeViolationError(
            f"Access denied to domain '{domain}' (deny list)"
        )
    if doms.allow is not None and domain not in doms.allow:
        raise ScopeViolationError(f"Access denied to domain '{domain}'")


# ---------------------------------------------------------------------------
# Dimension scope resolution
# ---------------------------------------------------------------------------


def _all_leaf_ids(dim_key: str, catalogue: Catalogue, ch_client) -> set[str]:
    """Query all leaf IDs for a leaf dimension from its source table."""
    dim = catalogue.dimensions.get(dim_key)
    if dim is None or dim.source is None:
        return set()
    result = ch_client.query(
        f"SELECT DISTINCT toString({dim.source.key_column}) "
        f"FROM {dim.source.table}"
    )
    return {row[0] for row in result.result_rows}


def _resolve_scope_members(
    members: dict[str, list[str]],
    catalogue: Catalogue,
    ch_client,
) -> dict[str, set[str]]:
    """Resolve scope dimension members to leaf-level ID sets.

    Keys in ``members`` are dimension keys (leaf, derived, or ragged).
    Values are lists of member values at that dimension level.

    Returns ``{leaf_dimension_key: set_of_leaf_ids}``.
    """
    leaf_sets: dict[str, set[str]] = {}

    for dim_key, values in members.items():
        target = _find_filter_target(dim_key, catalogue)
        dim = target.dimension

        for value in values:
            if target.resolution_type == "leaf":
                leaves = _resolve_leaf_filter(dim, value, ch_client)
                leaf_dim_key = dim.key
            elif target.resolution_type == "derived":
                leaves = _resolve_derived_filter(dim, value, ch_client)
                leaf_dim_key = next(iter(dim._transitive.values())).leaf_dimension
            elif target.resolution_type == "ragged":
                leaves = _resolve_ragged_filter(dim, value, ch_client)
                leaf_dim_key = dim.leaf_dimension
            else:
                continue

            if leaf_dim_key not in leaf_sets:
                leaf_sets[leaf_dim_key] = set()
            leaf_sets[leaf_dim_key].update(leaves)

    return leaf_sets


def _reject_inline_dimension_scope(
    scope: ScopeSpec | None,
    catalogue: Catalogue,
    domain: str,
) -> None:
    """Reject member scopes on source-only federated dimensions."""
    if scope is None or scope.dimensions is None:
        return

    domain_cat = catalogue.domains.get(domain)
    if domain_cat is None:
        return

    inline_keys = {
        cd.key
        for cd in domain_cat.dimensions
        if cd.source_inline and not cd.filterable
    }
    scoped_keys: set[str] = set()
    if scope.dimensions.allow:
        scoped_keys.update(scope.dimensions.allow)
    if scope.dimensions.deny:
        scoped_keys.update(scope.dimensions.deny)

    blocked = sorted(scoped_keys & inline_keys)
    if blocked:
        joined = ", ".join(repr(key) for key in blocked)
        raise ScopeViolationError(
            f"Federated-only dimension(s) {joined} can be used as reporting "
            "axes but cannot be used in security scope in this phase"
        )


def union_read_member_sets(
    permissions,
    catalogue: Catalogue,
    ch_client,
) -> dict[str, set[str]] | None:
    """Least-restrictive read scope across the caller's readable scenarios.

    For scenario-less discovery surfaces (e.g. ``search_hierarchy``): a
    dimension member is visible when at least one scenario the caller can
    read allows it. Returns ``{leaf_dimension_key: permitted_leaf_ids}``
    covering only dimensions restricted in *every* readable scenario's read
    scope — a dimension any readable scope is silent on is unrestricted
    overall. Returns ``None`` (no filtering) when the caller is admin, has
    no auth context, or any readable scenario carries an unrestricted read
    scope.

    Callers must handle "no readable scenario at all" themselves (this
    returns ``None`` for that case so it cannot enumerate a deny-all over
    unknown dimensions).
    """
    if permissions is None or permissions.is_admin:
        return None

    read_dim_scopes = []
    for sp in permissions.scenarios.values():
        if "read" not in sp.tool_scopes:
            continue
        scope = sp.tool_scopes["read"]
        dim_scope = scope.dimensions if scope is not None else None
        if dim_scope is None or (not dim_scope.allow and not dim_scope.deny):
            # One dimension-unrestricted readable scenario → no filtering.
            return None
        read_dim_scopes.append(dim_scope)

    if not read_dim_scopes:
        return None

    per_scope: list[dict[str, set[str]]] = []
    for dims in read_dim_scopes:
        allow_resolved = (
            _resolve_scope_members(dims.allow, catalogue, ch_client)
            if dims.allow else {}
        )
        deny_resolved = (
            _resolve_scope_members(dims.deny, catalogue, ch_client)
            if dims.deny else {}
        )
        resolved: dict[str, set[str]] = {}
        for leaf_dim_key in set(allow_resolved) | set(deny_resolved):
            allow_set = allow_resolved.get(leaf_dim_key)
            if allow_set is None:
                allow_set = _all_leaf_ids(leaf_dim_key, catalogue, ch_client)
            resolved[leaf_dim_key] = set(allow_set) - set(
                deny_resolved.get(leaf_dim_key, set())
            )
        if not resolved:
            return None
        per_scope.append(resolved)

    restricted_everywhere = set.intersection(*(set(r) for r in per_scope))
    result = {
        key: set().union(*(r[key] for r in per_scope))
        for key in restricted_everywhere
    }
    return result or None


def resolve_scope_filters(
    scope: ScopeSpec | None,
    catalogue: Catalogue,
    ch_client,
    domain: str = "pnl",
) -> dict[str, list[str]]:
    """Resolve a dimension scope into positive-inclusion filter sets.

    Per leaf dim key mentioned by allow or deny:
      allow_set  = resolved(allow[key]) if key in allow else universe(key)
      deny_set   = resolved(deny[key])  if key in deny  else ∅
      effective  = allow_set − deny_set        # deny wins (§5.5)

    Keys not mentioned by either side are omitted — AND across keys within
    a scope means silence = no restriction on that key (§5.6 + confirmed).

    Returns ``{view_column: [leaf_ids]}`` — same format as
    ``filter_resolver.resolve_filters``. Empty dict = no dimension
    restriction from the scope.
    """
    _reject_inline_dimension_scope(scope, catalogue, domain)

    if scope is None or scope.dimensions is None:
        return {}

    dims = scope.dimensions
    if not dims.allow and not dims.deny:
        return {}

    allow_resolved: dict[str, set[str]] = {}
    if dims.allow:
        allow_resolved = {
            k: set(v)
            for k, v in _resolve_scope_members(dims.allow, catalogue, ch_client).items()
        }

    deny_resolved: dict[str, set[str]] = {}
    if dims.deny:
        deny_resolved = {
            k: set(v)
            for k, v in _resolve_scope_members(dims.deny, catalogue, ch_client).items()
        }

    keys = set(allow_resolved) | set(deny_resolved)
    result: dict[str, list[str]] = {}
    for leaf_dim_key in keys:
        if leaf_dim_key in allow_resolved:
            allow_set = allow_resolved[leaf_dim_key]
        else:
            # deny-only on this key → universe − deny
            allow_set = _all_leaf_ids(leaf_dim_key, catalogue, ch_client)
        effective = allow_set - deny_resolved.get(leaf_dim_key, set())

        dim = catalogue.dimensions.get(leaf_dim_key)
        if dim is None:
            continue
        view_col = _map_to_view_column(dim, catalogue, domain)
        result[view_col] = sorted(effective)

    return result


# ---------------------------------------------------------------------------
# Merge with user-provided filters
# ---------------------------------------------------------------------------


def merge_dimension_filters(
    user_filters: dict[str, list[str]] | None,
    scope_filters: dict[str, list[str]],
) -> dict[str, list[str]] | None:
    """Merge scope dimension filters with user-provided filters.

    - Dimensions in both: intersect (user cannot exceed scope)
    - Dimensions only in scope: use scope values
    - Dimensions only in user: keep as-is (scope doesn't restrict)

    Returns None if the merged result is empty (no filters needed).
    """
    if not scope_filters:
        return user_filters

    if user_filters is None:
        return scope_filters if scope_filters else None

    merged = dict(user_filters)

    for col, scope_ids in scope_filters.items():
        if col in merged:
            # Intersect: user can't see more than scope allows
            user_set = set(merged[col])
            intersection = user_set & set(scope_ids)
            merged[col] = sorted(intersection)
        else:
            merged[col] = scope_ids

    return merged if merged else None


# ---------------------------------------------------------------------------
# Cross-scenario scope enforcement
# ---------------------------------------------------------------------------


def enforce_cross_scenario_scope(
    per_scenario_scope: dict[str, ScopeSpec | None],
    user_filters: dict[str, list[str]] | None,
    domain: str,
    catalogue: Catalogue,
    ch_client,
) -> dict[str, list[str]] | None:
    """Resolve per-scenario scopes into a single consistent filter set.

    For each scenario:
      1. Check domain access
      2. Resolve dimension scope to positive-inclusion filters
      3. Merge with user-provided filters → effective filters for that scenario

    Then verify consistency: all scenarios must produce identical effective
    filters.  If they differ the query would return inconsistent data across
    scenarios — raise ``ScopeViolationError`` with an actionable message
    telling the agent how to narrow the query.

    Returns the effective ``dimension_filters`` (or *None* if unrestricted).
    """
    effective_per_scenario: dict[str, dict[str, list[str]] | None] = {}

    for scenario_id, scope in per_scenario_scope.items():
        if scope is None:
            # Unrestricted on this scenario
            effective_per_scenario[scenario_id] = user_filters
            continue

        # Domain gating (per-scenario)
        check_domain_access(scope, domain)

        # Resolve dimension scope → positive-inclusion filters
        scope_dim_filters = resolve_scope_filters(
            scope, catalogue, ch_client, domain=domain,
        )

        # Merge with user filters
        if scope_dim_filters:
            effective = merge_dimension_filters(user_filters, scope_dim_filters)
        else:
            effective = user_filters

        effective_per_scenario[scenario_id] = effective

    # --- Consistency check ---
    # Normalise for comparison: None → empty dict, sort values
    def _normalise(f: dict[str, list[str]] | None) -> dict[str, tuple[str, ...]]:
        if not f:
            return {}
        return {k: tuple(sorted(v)) for k, v in f.items()}

    normalised = {
        sid: _normalise(eff) for sid, eff in effective_per_scenario.items()
    }
    unique_filters = set()
    for n in normalised.values():
        unique_filters.add(tuple(sorted(n.items())))

    if len(unique_filters) <= 1:
        # All scenarios resolve to the same effective filters — consistent
        return next(iter(effective_per_scenario.values()))

    # Inconsistent — build an actionable error message
    lines = ["Scope restrictions differ across scenarios in this query:"]
    for sid, eff in effective_per_scenario.items():
        if eff:
            dims = ", ".join(
                f"{col}: [{', '.join(ids[:5])}{'...' if len(ids) > 5 else ''}]"
                for col, ids in sorted(eff.items())
            )
            lines.append(f"  {sid}: {dims}")
        else:
            lines.append(f"  {sid}: unrestricted")

    # Compute intersection — the filters that would make it consistent
    all_effective = [
        _normalise(eff) for eff in effective_per_scenario.values()
    ]
    intersected: dict[str, set[str]] = {}
    # Start from the most restricted (the one with the most filter keys)
    all_keys: set[str] = set()
    for n in all_effective:
        all_keys |= n.keys()

    for key in all_keys:
        sets_for_key = []
        for n in all_effective:
            if key in n:
                sets_for_key.append(set(n[key]))
        if sets_for_key:
            intersected[key] = sets_for_key[0]
            for s in sets_for_key[1:]:
                intersected[key] &= s

    if intersected:
        hint_parts = ", ".join(
            f"{col}: [{', '.join(sorted(ids)[:5])}{'...' if len(ids) > 5 else ''}]"
            for col, ids in sorted(intersected.items()) if ids
        )
        if hint_parts:
            lines.append(f"  Narrow filters to: {hint_parts}")
        else:
            lines.append(
                "  No overlapping scope — query these scenarios separately."
            )
    else:
        lines.append("  Query these scenarios separately.")

    raise ScopeViolationError("\n".join(lines))


# ---------------------------------------------------------------------------
# Write-path scope resolution
# ---------------------------------------------------------------------------


def resolve_scope_leaf_sets(
    scope: ScopeSpec | None,
    catalogue: Catalogue,
    ch_client,
) -> dict[str, set[str]] | None:
    """Resolve a scope's dimension allow/deny into leaf-level ID sets.

    Same semantics as :func:`resolve_scope_filters`, but keyed by leaf
    dimension key (not view column) and returning ``set`` values. Intended
    for code paths that compare against leaf-dim-keyed resolved conditions
    (e.g. ``scope_resolver.resolve_lock_condition``).

    Returns None if the scope is unrestricted on dimensions.
    """
    if scope is None or scope.dimensions is None:
        return None
    dims = scope.dimensions
    if not dims.allow and not dims.deny:
        return None

    allow_resolved: dict[str, set[str]] = {}
    if dims.allow:
        allow_resolved = _resolve_scope_members(dims.allow, catalogue, ch_client)
    deny_resolved: dict[str, set[str]] = {}
    if dims.deny:
        deny_resolved = _resolve_scope_members(dims.deny, catalogue, ch_client)

    keys = set(allow_resolved) | set(deny_resolved)
    result: dict[str, set[str]] = {}
    for leaf_dim_key in keys:
        if leaf_dim_key in allow_resolved:
            allow_set = allow_resolved[leaf_dim_key]
        else:
            allow_set = _all_leaf_ids(leaf_dim_key, catalogue, ch_client)
        result[leaf_dim_key] = allow_set - deny_resolved.get(leaf_dim_key, set())
    return result


def resolve_write_scope(
    scope: ScopeSpec | None,
    dataset: PlanDataset,
    catalogue: Catalogue,
    ch_client,
) -> dict[str, list[str]] | None:
    """Resolve the write scope for a plan dataset.

    1. Domain gating: check ``dataset.domain`` against ``DomainScope``
    2. Dimension resolution: resolve ``DimensionScope`` to leaf-level filter sets

    Returns ``{view_column: [leaf_ids]}`` or ``None`` (unrestricted).
    Raises ``ScopeViolationError`` on domain denial.
    """
    if scope is None:
        return None
    if dataset.domain:
        check_domain_access(scope, dataset.domain)
    filters = resolve_scope_filters(
        scope, catalogue, ch_client, domain=dataset.domain or "pnl",
    )
    return filters or None
