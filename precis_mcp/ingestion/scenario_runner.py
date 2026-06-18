# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Seed `semantic.scenarios` from an instance-declared `scenarios.yml`.

`semantic.scenarios` is the canonical scenario registry the engine loads
at startup (`ScenarioRegistry`). Its *table schema* is package-owned —
fixed, identical in every deployment — but its *rows* (which scenarios
exist, their ids/aliases/horizons/kinds) are deployment-specific config.
This runner is the counterpart of `live_ddl_runner` (tables) and
`semantic_runner` (views): the operator declares the rows once in
`instance/scenarios.yml`, and the runner upserts them into
`semantic.scenarios`, whose `CREATE TABLE` it owns.

Seed semantics, not destructive sync
------------------------------------
`apply()` is **seed-if-absent**: it inserts only declared scenarios whose
`scenario_id` is not already present. Rows already in the registry are
left untouched, because the Précis scenario-lifecycle tools
mutate them at runtime (status, locks, new variants) and
a re-run must not clobber that state. Re-running is therefore idempotent
and non-destructive. A full declarative replace-on-change sync is a
heavier "config-as-code" concern and deliberately out of scope here.

File convention
---------------
`instance/scenarios.yml` is either a bare list of scenario mappings or a
mapping with a top-level `scenarios:` list. Each mapping carries at least
the required fields (`scenario_id`, `alias`, `name`, `kind`); every other
column takes its declared value or a default. Unknown fields, missing
required fields, and duplicate ids/aliases fail fast with a precise
message — the same fail-fast posture the catalogue/preflight surface uses.

The `scenarios` schema (CH database) is created if missing; the table is
`CREATE TABLE IF NOT EXISTS` (never dropped — a regen that wants a clean
slate drops the table itself before calling this runner).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from precis_mcp.observability import get_logger

_logger = get_logger("ingestion.scenario_runner")


class ScenarioSeedError(Exception):
    """Raised on an invalid `scenarios.yml` (unknown/missing field,
    duplicate id/alias, unreadable file)."""


# The package-owned column contract — order matches the CH DDL below and the
# `semantic.scenarios` table. The insert always
# names every column; omitted optionals get a default (DEFAULTS) so the row
# shape is uniform and does not rely on CH-side DEFAULT resolution.
COLUMNS: tuple[str, ...] = (
    "scenario_id",
    "alias",
    "name",
    "base_scenario",
    "status",
    "description",
    "created_by",
    "created_at",
    "locked_at",
    "horizon_start",
    "horizon_end",
    "actuals_cutoff",
    "granularity",
    "owner_user_id",
    "updated_at",
    "variant_of",
    "locks",
    "kind",
)

REQUIRED: frozenset[str] = frozenset({"scenario_id", "alias", "name", "kind"})

_ALLOWED: frozenset[str] = frozenset(COLUMNS)
_DT_COLS: frozenset[str] = frozenset({"created_at", "locked_at", "updated_at"})

# Sentinel: default to "now at parse time" (created_at / updated_at).
_NOW = object()

DEFAULTS: dict[str, Any] = {
    "base_scenario": None,
    "status": "DRAFT",
    "description": "",
    "created_by": "system",
    "created_at": _NOW,
    "locked_at": None,
    "horizon_start": "",
    "horizon_end": "",
    "actuals_cutoff": None,
    "granularity": "monthly",
    "owner_user_id": "",
    "updated_at": _NOW,
    "variant_of": None,
    "locks": "[]",
}


@dataclass
class AppliedReport:
    """Per-run summary: schema/table ensured, which ids were seeded vs.
    skipped (already present)."""

    schema_ensured: bool = False
    table_ensured: bool = False
    seeded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _scenarios_ddl(schema: str) -> str:
    """`CREATE TABLE IF NOT EXISTS` for the package-owned scenario registry.

    The schema is fixed; only the (qualified) name varies by `schema`.
    """
    return (
        f"CREATE TABLE IF NOT EXISTS {schema}.scenarios (\n"
        "    scenario_id     String,\n"
        "    alias           String,\n"
        "    name            String,\n"
        "    base_scenario   Nullable(String),\n"
        "    status          String,\n"
        "    description     String,\n"
        "    created_by      String,\n"
        "    created_at      DateTime,\n"
        "    locked_at       Nullable(DateTime),\n"
        "    horizon_start   String         DEFAULT '',\n"
        "    horizon_end     String         DEFAULT '',\n"
        "    actuals_cutoff  Nullable(String),\n"
        "    granularity     String         DEFAULT 'monthly',\n"
        "    owner_user_id   String         DEFAULT '',\n"
        "    updated_at      DateTime       DEFAULT now(),\n"
        "    variant_of      Nullable(String),\n"
        "    locks           String         DEFAULT '[]',\n"
        "    kind            LowCardinality(String)\n"
        ") ENGINE = MergeTree()\n"
        "ORDER BY scenario_id"
    )


def _coerce_dt(value: Any) -> Any:
    """Normalise a YAML scalar into something clickhouse-connect accepts for
    a DateTime column. `None` passes through (Nullable); a `date` is widened
    to midnight; an ISO string is parsed."""
    if value is None or isinstance(value, datetime.datetime):
        return value
    if isinstance(value, datetime.date):
        return datetime.datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        try:
            return datetime.datetime.fromisoformat(value)
        except ValueError as exc:
            raise ScenarioSeedError(
                f"invalid datetime {value!r}: {exc}"
            ) from exc
    raise ScenarioSeedError(f"expected a datetime, got {type(value).__name__}")


def _value_for(col: str, item: dict[str, Any], now: datetime.datetime) -> Any:
    if col in item:
        return _coerce_dt(item[col]) if col in _DT_COLS else item[col]
    default = DEFAULTS.get(col)
    if default is _NOW:
        return now
    return _coerce_dt(default) if col in _DT_COLS else default


def parse_scenarios(path: Path) -> list[dict[str, Any]]:
    """Parse + validate `scenarios.yml`, returning one full-column dict per
    scenario (defaults applied). Pure — no ClickHouse contact. Raises
    `ScenarioSeedError` on any structural problem."""
    if not path.exists():
        raise ScenarioSeedError(f"scenarios file not found: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        items: Any = []
    elif isinstance(raw, dict):
        items = raw.get("scenarios", [])
    elif isinstance(raw, list):
        items = raw
    else:
        raise ScenarioSeedError(
            f"{path}: expected a list or a mapping with a 'scenarios:' key"
        )
    if not isinstance(items, list):
        raise ScenarioSeedError(f"{path}: 'scenarios' must be a list")

    now = datetime.datetime.now()
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_aliases: set[str] = set()

    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ScenarioSeedError(f"{path}: scenario #{i} is not a mapping")
        unknown = set(item) - _ALLOWED
        if unknown:
            raise ScenarioSeedError(
                f"{path}: unknown field(s) {sorted(unknown)} in scenario "
                f"{item.get('scenario_id', '#' + str(i))!r}; "
                f"allowed: {sorted(_ALLOWED)}"
            )
        for req in sorted(REQUIRED):
            if not item.get(req):
                raise ScenarioSeedError(
                    f"{path}: scenario #{i} missing required field {req!r}"
                )
        sid = item["scenario_id"]
        alias = item["alias"]
        if sid in seen_ids:
            raise ScenarioSeedError(f"{path}: duplicate scenario_id {sid!r}")
        if alias in seen_aliases:
            raise ScenarioSeedError(f"{path}: duplicate alias {alias!r}")
        seen_ids.add(sid)
        seen_aliases.add(alias)
        rows.append({col: _value_for(col, item, now) for col in COLUMNS})

    return rows


def _existing_ids(ch_client: Any, schema: str) -> set[str]:
    result = ch_client.query(f"SELECT scenario_id FROM {schema}.scenarios")
    return {row[0] for row in result.result_rows}


def apply(
    scenarios_path: Path,
    ch_client: Any,
    *,
    schema: str = "semantic",
) -> AppliedReport:
    """Ensure `{schema}.scenarios` exists and seed any declared scenario whose
    `scenario_id` is not already present (seed-if-absent; see module docstring).

    `ch_client` matches the clickhouse-connect surface (`.command`, `.query`,
    `.insert`). Raises `ScenarioSeedError` on an invalid file; CH errors
    propagate.
    """
    report = AppliedReport()

    ch_client.command(f"CREATE DATABASE IF NOT EXISTS {schema}")
    report.schema_ensured = True
    ch_client.command(_scenarios_ddl(schema))
    report.table_ensured = True
    _logger.info("ingestion.scenarios.table_ensured", schema=schema)

    rows = parse_scenarios(scenarios_path)
    existing = _existing_ids(ch_client, schema)

    to_seed = [r for r in rows if r["scenario_id"] not in existing]
    report.skipped = [
        r["scenario_id"] for r in rows if r["scenario_id"] in existing
    ]

    if to_seed:
        ch_client.insert(
            f"{schema}.scenarios",
            [[r[col] for col in COLUMNS] for r in to_seed],
            column_names=list(COLUMNS),
        )
    report.seeded = [r["scenario_id"] for r in to_seed]

    _logger.info(
        "ingestion.scenarios.seeded",
        schema=schema,
        seeded=report.seeded,
        skipped=report.skipped,
        source_file=str(scenarios_path),
    )
    return report
