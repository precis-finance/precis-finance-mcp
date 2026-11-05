# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Pure SQL boundary tests; no database or driver imports."""
from types import SimpleNamespace

import pytest

from precis_mcp.analytical.contracts import ConfigurationError
from precis_mcp.analytical.sql import CLICKHOUSE_SQL, SqlDialect, dialect_for


def test_fully_qualified_native_names_and_unchanged_ch_names():
    sql = SqlDialect("databricks", "customer", (("semantic", "finance"), ("planning", "plan")))
    assert sql.table("semantic.v_pnl") == "`customer`.`finance`.`v_pnl`"
    assert sql.table("planning.commits") == "`customer`.`plan`.`commits`"
    assert CLICKHOUSE_SQL.table("semantic.v_pnl") == "semantic.v_pnl"
    for reference in ["v_pnl", "other.v_pnl", "a.b.c", "semantic.v_pnl;DROP"]:
        with pytest.raises(ConfigurationError):
            sql.table(reference)
    with pytest.raises(ConfigurationError):
        SqlDialect("databricks").table("semantic.v_pnl")


def test_list_values_are_bound_and_empty_scope_remains_deny_all():
    sql = SqlDialect("databricks", "customer")
    parameters = {}
    values = ["x'; DROP TABLE t; --", "cost centre"]
    expression = sql.in_values(sql.string("t.cost_centre"), "allowed", values, parameters)
    assert expression == "CAST(t.cost_centre AS STRING) IN (:allowed_0, :allowed_1)"
    assert parameters == {"allowed_0": values[0], "allowed_1": values[1]}
    assert values[0] not in expression
    assert sql.in_values("t.cost_centre", "allowed", [], {}) == "0 = 1"
    assert CLICKHOUSE_SQL.in_values("t.cost_centre", "allowed", [], parameters) == "t.cost_centre IN ({allowed:Array(String)})"
    with pytest.raises(ConfigurationError, match="collision"):
        sql.in_values("t.cost_centre", "allowed", ["a"], {"allowed_0": "prior"})


def test_cast_boolean_and_mask_rendering_preserve_shared_semantics():
    sql = SqlDialect("databricks", "customer")
    assert sql.parameter("sid") == ":sid"
    assert CLICKHOUSE_SQL.parameter("sid") == "{sid:String}"
    assert sql.boolean(True) == "TRUE" and CLICKHOUSE_SQL.boolean(True) == "1"
    assert sql.grouping_mask(["t.division", "t.period"]) == "GROUPING_ID(t.division, t.period)"
    assert CLICKHOUSE_SQL.grouping_mask(["t.division", "t.period"]) == "GROUPING(t.division, t.period)"


def test_legacy_clients_default_to_ch_but_declared_invalid_dialect_fails():
    assert dialect_for(None) is CLICKHOUSE_SQL
    assert dialect_for(SimpleNamespace()) is CLICKHOUSE_SQL
    with pytest.raises(ConfigurationError):
        dialect_for(SimpleNamespace(dialect="databricks"))


def test_empty_sum_policy_is_shared_and_does_not_zero_fill_missing_values():
    native = SqlDialect("databricks", "customer")
    expression = "CASE WHEN t.account = '4000' THEN t.amount ELSE 0 END"
    expected = f"CASE WHEN COUNT(*) = 0 THEN 0 ELSE SUM({expression}) END"
    assert native.sum(expression) == CLICKHOUSE_SQL.sum(expression) == expected
    assert "COALESCE" not in expected


@pytest.mark.parametrize("native_type,nullable,default", [
    ("STRING",False,"''"), ("INT",False,"0"), ("STRING",True,None)])
def test_dimension_defaults_use_warehouse_types(native_type,nullable,default):
    sql = SqlDialect("databricks", "customer", column_type=lambda relation, column: (native_type,nullable))
    rendered = sql.joined_attribute("d.name", "semantic.dim", "name", joined_key="d.id")
    if nullable:
        assert rendered == "d.name"
    else:
        assert rendered.startswith(f"CASE WHEN d.id IS NULL THEN {default} ELSE")
        assert "raise_error" in rendered


@pytest.mark.parametrize("backend", ["clickhouse", "databricks"])
def test_hierarchy_materialization_preserves_closure_and_stable_read_views(catalogue, backend):
    from precis_mcp.analytical.provisioning import hierarchy_provisioning
    from precis_mcp.engine.ragged_views import build_ragged_views

    dialect = SqlDialect(backend, "customer")
    bodies = dict(build_ragged_views(catalogue, dialect))
    provision = hierarchy_provisioning(catalogue, dialect)
    refresh = hierarchy_provisioning(catalogue, dialect, refresh_only=True)
    replacements = [s.sql for s in provision if s.sql.startswith("CREATE OR REPLACE TABLE")]
    assert replacements == [s.sql for s in refresh]
    assert len(refresh) == sum(d.is_ragged for d in catalogue.dimensions.values())
    for dim in catalogue.dimensions.values():
        if not dim.is_ragged:
            continue
        name = f"dim_{dim.leaf_dimension}_{dim.key}_rollup"
        table = dialect.table(f"semantic.{name}_materialized")
        replacement = next(sql for sql in replacements if sql.startswith(f"CREATE OR REPLACE TABLE {table} "))
        assert replacement.endswith(bodies[name])
        assert "WITH RECURSIVE" in replacement and "SELECT DISTINCT" in replacement
        assert "ENGINE = MergeTree" in replacement if backend == "clickhouse" else "USING DELTA" in replacement
        view = next(s.sql for s in provision if s.sql.startswith(f"CREATE OR REPLACE VIEW {dialect.table('semantic.'+name)} AS"))
        assert view.endswith(f"SELECT * FROM {table}")
        assert "RECURSIVE" not in view
        assert [s.sql for s in provision].index(replacement) < [s.sql for s in provision].index(view)
    assert not any(s.sql.startswith(("DROP", "TRUNCATE")) for s in provision)


def test_explicit_hierarchy_refresh_stops_after_failure_without_replay(catalogue):
    from precis_mcp.analytical.provisioning import refresh_hierarchies

    calls = []

    def execute(sql):
        calls.append(sql)
        if len(calls) == 2:
            raise RuntimeError("refresh failed")

    with pytest.raises(RuntimeError, match="refresh failed"):
        refresh_hierarchies(catalogue, execute, CLICKHOUSE_SQL)
    assert len(calls) == 2
    assert all(sql.startswith("CREATE OR REPLACE TABLE semantic.dim_") for sql in calls)
