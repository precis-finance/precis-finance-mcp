#!/usr/bin/env python3
# SPDX-License-Identifier: Elastic-2.0
"""Read-only injected-engine diagnostic on an existing actuals demo.

The operator supplies the existing catalogue and scenario seed fixtures.
This exercises the real shared engine, not app authentication,
database scenario governance, customer-managed startup or commercial writes.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import sys
from time import perf_counter
from decimal import Decimal
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml
from precis_mcp.analytical import get_analytical_client
from precis_mcp.analytical.contracts import ReadContractError
from precis_mcp.analytical.sql import SqlDialect
from precis_mcp.engine import execute_report, load_and_validate
from precis_mcp.engine.scenario_registry import ScenarioRegistry


def finance_controls(amounts):
    revenue = -amounts.get("Revenue", Decimal(0))
    direct_cost = amounts.get("DirectCost", Decimal(0))
    margin = revenue-direct_cost
    return {"revenue": revenue, "direct_cost": direct_cost, "gross_margin": margin,
            "gross_margin_pct": margin/revenue*100 if revenue else None}


def assert_amount(actual, expected, label):
    if expected is None:
        assert actual is None, label
    else:
        assert actual is not None and abs(Decimal(str(actual))-expected) < Decimal("0.000001"), label


class TimedReads:
    def __init__(self, client):
        self.client = client
        self.dialect = client.dialect
        self.read_seconds = []

    def query(self, sql, parameters=None):
        started = perf_counter()
        try:
            return self.client.query(sql, parameters=parameters)
        finally:
            self.read_seconds.append(perf_counter()-started)


def benchmark_reports(client, catalogue, registry, context, runs):
    timed = TimedReads(client)
    results = {}
    for name, scenarios in [
        ("pnl_actuals", ["actuals"]),
        ("pnl_actuals_py_yoy", ["actuals", "actuals_py", "actuals_vs_actuals_py_pct"]),
    ]:
        samples = []
        request = {"context":context, "blocks":[
            {"model":"statement:full_pnl", "scenario":scenario, "alias":scenario}
            for scenario in scenarios]}
        for _ in range(runs):
            timed.read_seconds.clear()
            started = perf_counter()
            report = execute_report(request, catalogue, ch_client=timed, scenario_registry=registry)
            samples.append({"seconds":round(perf_counter()-started, 6),
                            "query_count":len(timed.read_seconds),
                            "query_seconds":round(sum(timed.read_seconds), 6),
                            "rows":len(report["rows"])})
        seconds = [sample["seconds"] for sample in samples]
        results[name] = {"median_seconds":median(seconds), "min_seconds":min(seconds),
                         "max_seconds":max(seconds), "samples":samples}
    return {"scope":"Shared execute_report pipeline only, warm session and warehouse; "
                    "catalogue/scenario/read metadata preloaded; no MCP/auth/agent/Excel rendering; caches uncontrolled",
            "runs_per_case":runs, "reports":results}


def check_null_semantics(client):
    """Synthetic read-only cases isolate engine rendering from fixture contents."""
    dialect = SqlDialect("databricks", column_type=lambda relation, name: ("STRING", name == "optional_name"))
    required = dialect.sum("amount")
    optional = dialect.sum("optional")
    source = "(SELECT CAST(12.34 AS DECIMAL(18,2)) amount, CAST(NULL AS DECIMAL(18,2)) optional) t"
    assert client.query(f"SELECT {required}, {optional} FROM {source}").result_rows == [(Decimal("12.34"), None)]
    assert client.query(f"SELECT {required}, {optional} FROM {source} WHERE FALSE").result_rows == [(Decimal("0.00"), Decimal("0.00"))]
    label = dialect.joined_attribute("d.name", "semantic.test_dim", "name", joined_key="d.id")
    optional_label = dialect.joined_attribute("d.optional_name", "semantic.test_dim", "optional_name")
    join = ("FROM VALUES (1), (2) t(id) LEFT JOIN "
            "(SELECT 1 id, 'Present' name, CAST(NULL AS STRING) optional_name) d ON t.id = d.id")
    assert client.query(f"SELECT t.id, {label}, {optional_label} {join} ORDER BY t.id").result_rows == [
        (1, "Present", None), (2, "", None)]
    assert client.query(f"SELECT {required} FROM (SELECT CAST(NULL AS DECIMAL(18,2)) amount) t").result_rows == [(None,)]
    # Invalid nonnullable dimension labels must fail; nullable values remain missing.
    invalid = [
        f"SELECT {label} FROM (SELECT 1 id, CAST(NULL AS STRING) name) d",
    ]
    for sql in invalid:
        try:
            client.query(sql)
        except ReadContractError:
            pass
        else:
            raise AssertionError("Invalid non-null input was accepted")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalogue", default="instance/catalogue")
    parser.add_argument("--scenarios", default="instance/scenarios.yml")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--benchmark-runs", type=int, default=0,
                        help="Optional warm shared-report timings per case (0 disables, maximum 20).")
    args = parser.parse_args()
    if not 0 <= args.benchmark_runs <= 20:
        parser.error("--benchmark-runs must be between 0 and 20")
    catalogue = load_and_validate(args.catalogue)
    registry = ScenarioRegistry.from_rows(yaml.safe_load(Path(args.scenarios).read_text())["scenarios"])
    actuals = registry.get_real_scenario("actuals")
    started = perf_counter()
    with get_analytical_client("databricks") as client:
        view = client.dialect.table("semantic.v_pnl")
        latest = client.query(f"SELECT MAX(period) FROM {view} WHERE scenario = :scenario", {"scenario":actuals.scenario_id}).result_rows[0][0]
        if not latest:
            raise RuntimeError("The actuals fixture does not match the supplied scenario registry.")
        start = latest[:4]+"-01"
        context = {"period_start":start,"period_end":latest,"scale":0}
        expected_by_scenario = {}
        for alias, offset in [("Actuals", 0), ("Prior year", -1)]:
            shifted_start = str(int(start[:4])+offset)+start[4:]
            shifted_end = str(int(latest[:4])+offset)+latest[4:]
            controls = client.query(
                f"SELECT fs_line, SUM(amount) FROM {view} WHERE scenario = :scenario "
                "AND period >= :start AND period <= :end GROUP BY fs_line",
                {"scenario":actuals.scenario_id,"start":shifted_start,"end":shifted_end},
            )
            expected_by_scenario[alias] = finance_controls({row[0]:Decimal(row[1]) for row in controls.result_rows})
        expected = expected_by_scenario["Actuals"]
        aggregate = execute_report({"context":context,"blocks":[
            {"model":"statement:full_pnl","scenario":"actuals","alias":"Actuals"},
            {"model":"statement:full_pnl","scenario":"actuals_py","alias":"Prior year"},
            {"model":"statement:full_pnl","scenario":"actuals_vs_actuals_py_pct","alias":"YoY %"},
        ]},catalogue,ch_client=client,scenario_registry=registry)
        for key, amount in expected.items():
            row = next(row for row in aggregate["rows"] if row["item"]["key"] == key)
            previous = expected_by_scenario["Prior year"][key]
            assert_amount(row["values"]["Actuals"], amount, key)
            assert_amount(row["values"]["Prior year"], previous, key)
            yoy = (amount-previous)/abs(previous)*100 if amount is not None and previous else None
            assert_amount(row["values"]["YoY %"], yoy, key)
        breakdown = execute_report({"context":context,"dimensions":["division","period"],
            "grains":{"detail":True,"subtotals":True,"grand_total":True},
            "blocks":[{"model":"metrics:revenue,gross_margin,gross_margin_pct","scenario":"actuals","alias":"Actuals"}],
        },catalogue,ch_client=client,scenario_registry=registry)
        dimension = client.dialect.table("semantic.dim_cost_centre")
        detail_controls = client.query(
            f"SELECT d.division, t.period, t.fs_line, SUM(t.amount) FROM {view} t "
            f"LEFT JOIN {dimension} d ON t.cost_centre = d.cost_centre "
            "WHERE t.scenario = :scenario AND t.period >= :start AND t.period <= :end "
            "GROUP BY d.division, t.period, t.fs_line",
            {"scenario":actuals.scenario_id,"start":start,"end":latest},
        )
        groups = defaultdict(lambda: defaultdict(Decimal))
        for division, period, fs_line, amount in detail_controls.result_rows:
            groups[(division, period)][fs_line] += Decimal(amount)
            groups[(division,)][fs_line] += Decimal(amount)
            groups[()][fs_line] += Decimal(amount)
        observed = set()
        for row in breakdown["rows"]:
            codes = row["dimension_codes"]
            dimensions = tuple(codes[axis] for axis in ("division", "period") if axis in codes)
            assert set(codes) in ({"division", "period"}, {"division"}, set()), codes
            assert row["grain"] == {0:"grand_total", 1:"subtotal", 2:"detail"}[len(dimensions)]
            key = row["item"]["key"]
            identity = (dimensions, key)
            assert identity not in observed, identity
            observed.add(identity)
            assert dimensions in groups, dimensions
            assert_amount(row["values"]["Actuals"], finance_controls(groups[dimensions])[key], identity)
        assert observed == {(dimensions, metric) for dimensions in groups
                            for metric in ("revenue", "gross_margin", "gross_margin_pct")}
        # An empty resolved scope remains deny-all through the real retriever.
        from precis_mcp.engine.retriever import DataQuery, generate_sql, execute_queries
        empty = DataQuery("actuals",actuals.scenario_id,start,latest,["revenue"])
        result = execute_queries(generate_sql(empty,catalogue,[],{"cost_centre":[]},dialect=client.dialect),client)
        assert result[0][0]["revenue"] == 0, "Non-null canonical empty SUM changed"
        from precis_mcp.engine.inspect import inspect_rows
        inspection = inspect_rows(catalogue, "gl", limit=2, scenario_id=actuals.scenario_id,
                                  period_start=start, period_end=latest, ch_client=client)
        assert inspection["row_count"] == 2 and inspection["truncated"]
        from precis_mcp.auth import DimensionScope, ScopeSpec
        denied = inspect_rows(catalogue, "gl", scenario_id=actuals.scenario_id, ch_client=client,
                              per_scenario_scope={actuals.scenario_id:ScopeSpec(
                                  dimensions=DimensionScope(allow={"cost_centre":[]}))})
        assert denied["row_count"] == 0 and not denied["truncated"]
        benchmarks = benchmark_reports(client, catalogue, registry, context, args.benchmark_runs) if args.benchmark_runs else None
        check_null_semantics(client)
    evidence = {"status":"passed","checks":["full_pnl_actuals_py_yoy","two_axis_grains","financial_control_totals","empty_scope", "bounded_inspection", "inspection_deny_all", "nullable_and_empty_sums", "missing_dimension_defaults", "invalid_nonnull_dimension_rejected"],
                "elapsed_seconds":round(perf_counter()-started,3),"period":context,
                "scope":"Injected shared engine with operator-supplied scenario fixtures; not app/MCP, live scenario governance, independent financial acceptance or benchmark",
                "reports":{"aggregate":aggregate,"breakdown":breakdown}}
    if benchmarks is not None:
        evidence["benchmarks"] = benchmarks
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(evidence,indent=2,default=str)+"\n")
    print(json.dumps({k:v for k,v in evidence.items() if k != "reports"}))


if __name__ == "__main__":
    main()
