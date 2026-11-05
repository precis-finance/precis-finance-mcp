# SPDX-License-Identifier: Elastic-2.0
"""One package-owned scenario column contract for both physical backends.

CH default expressions retain the established table behaviour. Seed parsing
still supplies every column; Databricks scenario DML is a separate increment.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScenarioColumn:
    name: str
    source_type: str
    default: str | None = None


SCENARIO_COLUMNS = (
    ScenarioColumn("scenario_id", "String"),
    ScenarioColumn("alias", "String"),
    ScenarioColumn("name", "String"),
    ScenarioColumn("base_scenario", "Nullable(String)"),
    ScenarioColumn("status", "String"),
    ScenarioColumn("description", "String"),
    ScenarioColumn("created_by", "String"),
    ScenarioColumn("created_at", "DateTime"),
    ScenarioColumn("locked_at", "Nullable(DateTime)"),
    ScenarioColumn("horizon_start", "String", "''"),
    ScenarioColumn("horizon_end", "String", "''"),
    ScenarioColumn("actuals_cutoff", "Nullable(String)"),
    ScenarioColumn("granularity", "String", "'monthly'"),
    ScenarioColumn("owner_user_id", "String", "''"),
    ScenarioColumn("updated_at", "DateTime", "now()"),
    ScenarioColumn("variant_of", "Nullable(String)"),
    ScenarioColumn("locks", "String", "'[]'"),
    ScenarioColumn("kind", "LowCardinality(String)"),
)
COLUMNS = tuple(column.name for column in SCENARIO_COLUMNS)
