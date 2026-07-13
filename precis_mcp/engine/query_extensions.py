# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Optional scenario-query extension boundary for embedding applications.

The open engine has no registered extension and always reads one scenario.
An embedding application may attach opaque data during resolution and turn it
into an alternate SQL scope during retrieval.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ScenarioSqlScope:
    """Complete scenario predicate supplied by an embedding application."""

    outer_condition: str
    inner_condition: str
    params: dict[str, Any]
    commit_passthrough_condition: str = ""


_DataFactory = Callable[[Any, Any], dict[str, Any]]
_SqlFactory = Callable[[Any, bool], ScenarioSqlScope | None]

_data_factory: _DataFactory | None = None
_sql_factory: _SqlFactory | None = None


def register_scenario_query_extension(
    data_factory: _DataFactory,
    sql_factory: _SqlFactory,
) -> None:
    """Register one application-owned scenario-query extension."""
    global _data_factory, _sql_factory
    _data_factory = data_factory
    _sql_factory = sql_factory


def scenario_query_data(scenario_ref: Any, registry: Any) -> dict[str, Any]:
    """Return opaque resolver data, or an empty mapping in the open engine."""
    if _data_factory is None:
        return {}
    return dict(_data_factory(scenario_ref, registry))


def scenario_sql_scope(data_query: Any, delta_only: bool) -> ScenarioSqlScope | None:
    """Return an application-owned SQL scope when one is registered."""
    if _sql_factory is None:
        return None
    return _sql_factory(data_query, delta_only)
