# SPDX-License-Identifier: Elastic-2.0
"""Précis-owned scenario and hierarchy provisioning; never apply on app startup.

Scenario definitions, parsing and hierarchy closure stay shared. This module
supplies physical types/names, bound seed SQL and explicit hierarchy refresh,
not scenario lifecycle or customer pipeline orchestration.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from precis_mcp.scenario_contract import COLUMNS, SCENARIO_COLUMNS
from precis_mcp.engine.scenario_registry import ScenarioRegistry
from .config import quote_identifier
from .contracts import ConfigurationError
from .sql import SqlDialect


@dataclass(frozen=True)
class ProvisionStatement:
    sql: str
    parameters: dict


def _column_type(column) -> tuple[str, bool]:
    """The package owns this fixed scenario shape; no operator metadata file."""
    return ("TIMESTAMP" if "DateTime" in column.source_type else "STRING",
            column.source_type.startswith("Nullable("))


def scenario_provisioning(dialect: SqlDialect, seeds: list[dict]) -> list[ProvisionStatement]:
    """Render one creation and one seed batch; caller owns exclusive provisioning.

    Existing rows are never updated. MERGE is not a concurrent uniqueness
    guarantee: the operator must serialize provisioning with scenario writers.
    The reader contract supplies every seed field, so native DDL does not yet
    advertise omission/default behaviour for future lifecycle mutations.
    """
    if dialect.backend != "databricks":
        raise ConfigurationError("Native scenario rendering requires a Databricks dialect.")
    table = dialect.table("semantic.scenarios")
    # Validate the same vocabulary, kinds and aliases as the common engine.
    ScenarioRegistry.from_rows(seeds)
    columns = []
    for column in SCENARIO_COLUMNS:
        native_type, nullable = _column_type(column)
        columns.append(f"  {quote_identifier(column.name)} {native_type}" + ("" if nullable else " NOT NULL"))
    statements = [ProvisionStatement(
        f"CREATE TABLE IF NOT EXISTS {table} (\n" + ",\n".join(columns) + "\n) USING DELTA", {})]
    if not seeds:
        return statements
    parameters = {}
    rows = []
    for index, seed in enumerate(seeds):
        if set(seed) != set(COLUMNS):
            raise ConfigurationError("Scenario seeds must include the complete shared column contract.")
        expressions = []
        for column in SCENARIO_COLUMNS:
            name = column.name
            native_type, nullable = _column_type(column)
            value = seed[name]
            if value is None and not nullable:
                raise ConfigurationError("Scenario seed has a null required column.")
            if value is not None:
                if native_type == "TIMESTAMP":
                    if not isinstance(value, datetime):
                        raise ConfigurationError("Scenario seed timestamps must be parsed datetimes.")
                    value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
                elif not isinstance(value, str):
                    raise ConfigurationError("Scenario seed text columns must be strings.")
            parameter = f"seed_{index}_{name}"
            parameters[parameter] = value
            expressions.append(f"CAST({dialect.parameter(parameter)} AS {native_type}) AS {quote_identifier(name)}")
        rows.append("SELECT " + ", ".join(expressions))
    names = ", ".join(quote_identifier(name) for name in COLUMNS)
    values = ", ".join(f"s.{quote_identifier(name)}" for name in COLUMNS)
    statements.append(ProvisionStatement(
        f"MERGE INTO {table} t USING (" + " UNION ALL ".join(rows) + ") s "
        "ON t.scenario_id = s.scenario_id WHEN NOT MATCHED THEN "
        f"INSERT ({names}) VALUES ({values})", parameters))
    return statements


def read_provisioning(catalogue, seeds: list[dict], dialect: SqlDialect) -> list[ProvisionStatement]:
    """Render only package-owned objects, never customer actuals/dimensions."""
    namespace = dialect.table("semantic.scenarios").rsplit(".", 1)[0]
    statements = [ProvisionStatement(f"CREATE SCHEMA IF NOT EXISTS {namespace}", {})]
    statements.extend(scenario_provisioning(dialect, seeds))
    statements.extend(hierarchy_provisioning(catalogue, dialect))
    return statements


def hierarchy_provisioning(catalogue, dialect: SqlDialect, *,
                           refresh_only: bool = False, schema: str = "semantic") -> list[ProvisionStatement]:
    """Render closure replacements and stable projection views on either backend.

    Refresh-only is the pipeline operation after initial provisioning. Source
    publication and replacement of multiple closures are not one transaction.
    The caller must serialize hierarchy loads and quiesce affected reporting.
    """
    from precis_mcp.engine.ragged_views import build_ragged_views

    rollups = {f"dim_{dim.leaf_dimension}_{dim.key}_rollup"
               for dim in catalogue.dimensions.values() if dim.is_ragged and dim.leaf_dimension}
    statements = []
    for name, body in build_ragged_views(catalogue, dialect):
        if name in rollups:
            backing = f"{schema}.{name}_materialized"
            statements.append(ProvisionStatement(dialect.replace_table_as(backing, body), {}))
            body = f"SELECT * FROM {dialect.table(backing)}"
        if not refresh_only:
            statements.append(ProvisionStatement(
                f"CREATE OR REPLACE VIEW {dialect.table(schema+'.'+name)} AS\n{body}", {}))
    return statements


def refresh_hierarchies(catalogue, execute, dialect: SqlDialect, *, provision: bool = False) -> int:
    """Explicit operator/pipeline refresh, stopping on the first failure.

    ``execute`` runs trusted generated DDL with provisioning credentials. This
    function neither changes source data nor schedules or retries refreshes.
    ``provision`` installs/updates the owned view definitions as well, for first
    installation or catalogue hierarchy-definition changes.
    """
    statements = hierarchy_provisioning(catalogue, dialect, refresh_only=not provision)
    for statement in statements:
        execute(statement.sql)
    return sum(statement.sql.startswith("CREATE OR REPLACE TABLE") for statement in statements)
