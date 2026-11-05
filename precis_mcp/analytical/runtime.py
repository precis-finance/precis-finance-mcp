# SPDX-License-Identifier: Elastic-2.0
"""Small deployment checks using the existing catalogue and warehouse inputs."""
from __future__ import annotations

from contextlib import closing
import os

from . import get_analytical_client, selected_backend
from .config import quote_identifier
from .contracts import ConfigurationError
from precis_mcp.scenario_contract import COLUMNS


def ingestion_mode() -> str:
    backend = selected_backend()
    mode = os.getenv("PRECIS_INGESTION_MODE", "customer_managed" if backend == "databricks" else "managed")
    if mode not in {"managed", "customer_managed"}:
        raise ConfigurationError("PRECIS_INGESTION_MODE must be managed or customer_managed.")
    if backend == "databricks" and mode != "customer_managed":
        raise ConfigurationError("Databricks currently requires customer-managed ingestion.")
    return mode


def required_read_columns(catalogue) -> dict[str, set[str]]:
    """Catalogue declares required names; physical types remain input contracts."""
    from precis_mcp.engine.catalogue import BaseMetric
    required = {}
    for domain in catalogue.domains.values():
        if domain.backend_kind == "ibis":
            continue
        names = required.setdefault(domain.source_view, set())
        names.update(("scenario", "period", domain.native_grain_column))
        period = catalogue.dimensions.get("period")
        if period:
            names.update(parent for parent in period.parents
                         if catalogue.dimensions.get(parent) and catalogue.dimensions[parent].grain)
        if domain.versioned:
            names.add("commit_id")
        names.update(domain.inspect_columns)
        names.update(dimension.source for dimension in domain.dimensions)
        for metric in domain.metrics:
            if not isinstance(metric, BaseMetric):
                continue
            if metric.aggregation != "count" and metric.source_column != "1":
                names.add(metric.source_column)
            names.update(predicate.column for predicate in metric.where)
    for dimension in catalogue.dimensions.values():
        if dimension.is_leaf and dimension.source:
            names = required.setdefault(dimension.source.table, set())
            names.add(dimension.source.key_column)
            names.update(dimension.source.attribute_mapping.values())
            names.update(parent.source_column for parent in dimension.parents.values())
            if dimension.grain is not None and dimension.grain.value in {"week", "date"}:
                names.add("seq")
        if dimension.is_ragged and dimension.leaf_dimension:
            base = f"semantic.dim_{dimension.leaf_dimension}_{dimension.key}"
            required.setdefault(base, set()).update(("node_id", "node_name", "display_name", "node_type"))
            required.setdefault(base+"_edges", set()).update(("child_node_id", "parent_node_id"))
            resolution = dimension._transitive.get(dimension.leaf_dimension)
            if resolution:
                required.setdefault(base+"_rollup", set()).update(("node_id", resolution.leaf_key_column))
        for resolution in dimension._transitive.values():
            if not dimension.is_ragged:
                required.setdefault(resolution.source_table, set()).update((resolution.filter_column, resolution.leaf_key_column))
    required.setdefault("semantic.scenarios", set()).update(COLUMNS)
    return {relation:{name for name in names if name} for relation, names in required.items()}


def validate_runtime_inputs(catalogue) -> None:
    """Check named inputs read-only before startup/reload; no additional instance format."""
    ingestion_mode()
    if selected_backend() == "clickhouse":
        return
    with closing(get_analytical_client()) as client:
        for relation, names in sorted(required_read_columns(catalogue).items()):
            columns = ", ".join(quote_identifier(name) for name in sorted(names))
            client.query(f"SELECT {columns} FROM {client.dialect.table(relation)} LIMIT 0")
        from precis_mcp.engine.scenario_registry import load_scenario_registry
        load_scenario_registry(client)
