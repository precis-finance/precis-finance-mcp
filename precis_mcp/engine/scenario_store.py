# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Shared scenario metadata store with default ClickHouse persistence hooks.

``ScenarioRegistry`` owns in-memory resolution and generated scenario
vocabulary. ``ScenarioStore`` owns persistence reads/writes for
``semantic.scenarios``.
"""

from __future__ import annotations

import json
import re
from typing import Any
from precis_mcp.analytical.sql import dialect_for

from precis_mcp.engine.scenario_registry import (
    RealScenarioRef,
    ScenarioRegistry,
    _validate_alias,
)
from precis_mcp.engine.scenario_registry import (
    invalidate_scenario_registry_cache,
    get_scenario_registry,
    load_scenario_registry,
)


class ScenarioStore:
    def __init__(self, client: Any):
        self.client = client

    def load_registry(self) -> ScenarioRegistry:
        return get_scenario_registry(client=self.client)

    def get(self, value: str) -> RealScenarioRef:
        return self.load_registry().get_real_scenario(value)

    def resolve_writable(self, value: str) -> RealScenarioRef:
        # Mutation targets must reflect current alias mappings, not the TTL cache.
        return load_scenario_registry(self.client).resolve_writable(value)

    def resolve_writable_id(self, value: str) -> str:
        if _looks_like_literal_scenario_id(value):
            return value
        return self.resolve_writable(value).scenario_id

    def exists(self, value: str) -> bool:
        try:
            self.get(value)
        except Exception:
            return False
        return True

    def get_status(self, value: str) -> str:
        return self.get(value).status

    def get_horizon(self, value: str) -> tuple[str, str]:
        scenario = self.get(value)
        return scenario.horizon_start, scenario.horizon_end

    def _read_metadata(self, columns: str, scenario_id: str, *, limit_one: bool = False):
        """Columns are fixed internal expressions; values remain native binds."""
        sql = dialect_for(self.client)
        suffix = " LIMIT 1" if limit_one else ""
        return self.client.query(
            f"SELECT {columns} FROM {sql.table('semantic.scenarios')} "
            f"WHERE scenario_id = {sql.parameter('sid')}{suffix}",
            parameters={"sid": scenario_id},
        )

    def get_horizon_metadata(self, scenario_id: str) -> tuple[str, str] | None:
        result = self._read_metadata("horizon_start, horizon_end", scenario_id)
        if not result.result_rows:
            return None
        hs, he = result.result_rows[0]
        return hs, he

    def get_status_metadata(self, scenario_id: str) -> str | None:
        result = self._read_metadata("status", scenario_id)
        if not result.result_rows:
            return None
        return result.result_rows[0][0]

    def get_variant_parent(self, scenario_id: str) -> str | None:
        result = self._read_metadata("variant_of", scenario_id, limit_one=True)
        if not result.result_rows:
            return None
        parent = result.result_rows[0][0]
        return parent or None

    def list_variants(self, parent: str) -> list[RealScenarioRef]:
        return self.load_registry().list_variants(parent)

    def get_locks(self, value: str) -> list[dict]:
        raw = self.get(value).locks
        return _parse_locks(raw)

    def get_locks_metadata(self, scenario_id: str) -> list[dict] | None:
        result = self._read_metadata("locks", scenario_id)
        if not result.result_rows:
            return None
        return _parse_locks(result.result_rows[0][0])

    def scenario_id_exists(self, scenario_id: str) -> bool:
        result = self._read_metadata("COUNT(*)", scenario_id)
        return bool(result.result_rows and int(result.result_rows[0][0]) > 0)

    def get_creation_metadata(self, scenario_id: str) -> dict[str, Any] | None:
        result = self._read_metadata("horizon_start, horizon_end, actuals_cutoff, granularity, kind", scenario_id)
        if not result.result_rows:
            return None
        hs, he, ac, gran, kind = result.result_rows[0]
        return {
            "horizon_start": hs,
            "horizon_end": he,
            "actuals_cutoff": ac,
            "granularity": gran,
            "kind": kind,
        }

    def get_update_metadata(self, scenario_id: str) -> dict[str, Any] | None:
        result = self._read_metadata("scenario_id, status, horizon_start, horizon_end, actuals_cutoff, granularity", scenario_id)
        if not result.result_rows:
            return None
        sid, status, horizon_start, horizon_end, actuals_cutoff, granularity = (
            result.result_rows[0]
        )
        return {
            "scenario_id": sid,
            "status": status,
            "horizon_start": horizon_start,
            "horizon_end": horizon_end,
            "actuals_cutoff": actuals_cutoff,
            "granularity": granularity,
        }

    def create_scenario(
        self,
        *,
        scenario_id: str,
        name: str,
        base_scenario: str,
        kind: str,
        description: str,
        horizon_start: str,
        horizon_end: str,
        actuals_cutoff: str,
        granularity: str,
        variant_of: str,
        user_id: str,
        alias: str = "",
    ) -> str:
        alias_value = alias or _alias_from_scenario_id(scenario_id)
        _validate_alias(alias_value)
        self._insert_scenario({
            "sid": scenario_id, "alias": alias_value, "name": name,
            "base": base_scenario, "desc": description, "uid": str(user_id),
            "hs": horizon_start, "he": horizon_end,
            "ac": None if not actuals_cutoff else actuals_cutoff,
            "gran": granularity, "vof": variant_of if variant_of else None,
            "kind": kind,
        })
        invalidate_scenario_registry_cache()
        return alias_value

    def _insert_scenario(self, parameters: dict) -> None:
        """Existing open CH persistence; commercial packages may override the SQL."""
        self.client.command(
            """INSERT INTO semantic.scenarios
               (scenario_id, alias, name, base_scenario, status, description,
                created_by, horizon_start, horizon_end, actuals_cutoff,
                granularity, owner_user_id, variant_of, kind)
               VALUES ({sid:String}, {alias:String}, {name:String},
                       {base:String}, 'DRAFT', {desc:String}, {uid:String},
                       {hs:String}, {he:String}, {ac:Nullable(String)},
                       {gran:String}, {uid:String}, {vof:Nullable(String)},
                       {kind:String})""",
            parameters=parameters,
        )

    def update_scenario_metadata(
        self,
        scenario_id: str,
        updates: dict[str, Any],
    ) -> None:
        assignments: dict[str, str] = {}
        dialect = dialect_for(self.client)
        params: dict[str, Any] = {"sid": scenario_id}

        field_specs = {
            "status": ("status", "String", "new_status"),
            "horizon_start": ("horizon_start", "String", "hs"),
            "horizon_end": ("horizon_end", "String", "he"),
            "actuals_cutoff": ("actuals_cutoff", "Nullable(String)", "ac"),
            "granularity": ("granularity", "String", "gran"),
        }
        for field, value in updates.items():
            if field not in field_specs:
                raise ValueError(f"Unsupported scenario metadata field: {field}")
            column, type_name, param_name = field_specs[field]
            if field == "actuals_cutoff" and value is None:
                assignments["actuals_cutoff"] = "NULL"
                continue
            assignments[column] = dialect.parameter(param_name, type_name)
            params[param_name] = value

        if not assignments:
            return
        assignments["updated_at"] = "now()"
        self._update_columns(assignments, params)
        invalidate_scenario_registry_cache()

    def _update_columns(self, assignments: dict[str, str], parameters: dict) -> None:
        self.client.command(
            "ALTER TABLE semantic.scenarios UPDATE "
            + ", ".join(f"{key} = {value}" for key, value in assignments.items())
            + " WHERE scenario_id = {sid:String}",
            parameters=parameters,
        )

    def update_locks(self, scenario_id: str, locks: list[dict]) -> None:
        self._update_columns(
            {"locks": dialect_for(self.client).parameter("locks")},
            {"sid": scenario_id, "locks": json.dumps(locks)},
        )
        invalidate_scenario_registry_cache()


def _alias_from_scenario_id(scenario_id: str) -> str:
    alias = re.sub(r"[^a-z0-9]+", "_", scenario_id.lower()).strip("_")
    return alias or scenario_id.lower()


def _looks_like_literal_scenario_id(value: str) -> bool:
    """Heuristic for storage IDs such as ACTUALS, BUD-2026, FC-2026-Q1."""
    return value == value.upper() and any(ch.isupper() or ch.isdigit() for ch in value)


def _parse_locks(raw: Any) -> list[dict]:
    if not raw or raw == "[]":
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []
