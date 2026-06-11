# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
from __future__ import annotations

from dataclasses import dataclass

import pytest

from precis_mcp.engine.catalogue import Catalogue, CubeDimension, DomainCatalogue
from precis_mcp.engine.inspect import (
    InspectionError,
    get_inspection_schema,
    inspect_rows,
    list_inspection_sources,
)


@dataclass
class _Result:
    column_names: list[str]
    result_rows: list[tuple]


class _FakeClickHouse:
    def __init__(self, rows: list[tuple] | None = None):
        self.rows = rows or []
        self.sql = ""
        self.parameters = {}

    def query(self, sql, parameters=None):
        self.sql = sql
        self.parameters = parameters or {}
        return _Result(["period", "account", "amount"], self.rows)


def _catalogue() -> Catalogue:
    return Catalogue(
        metrics={},
        scenarios={},
        statements={},
        domains={
            "gl": DomainCatalogue(
                domain="gl",
                source_view="semantic.v_gl",
                metrics=[],
                dimensions=[
                    CubeDimension(
                        key="cost_centre",
                        label="Cost Centre",
                        source="cost_centre",
                    )
                ],
                inspect_enabled=True,
                inspect_columns=["period", "account", "amount"],
            ),
            "payroll": DomainCatalogue(
                domain="payroll",
                source_view="semantic.v_payroll",
                metrics=[],
                inspect_enabled=False,
                inspect_columns=["period", "employee", "gross_salary"],
            ),
        },
    )


def test_list_inspection_sources_only_returns_enabled_domains():
    result = list_inspection_sources(_catalogue())
    assert [source["source_key"] for source in result] == ["gl"]
    assert result[0]["inspect_columns"] == ["period", "account", "amount"]
    assert result[0]["filter_dimensions"] == [
        {
            "key": "cost_centre",
            "label": "Cost Centre",
            "source": "cost_centre",
            "source_column": "cost_centre",
        }
    ]


def test_get_inspection_schema_rejects_disabled_domain():
    with pytest.raises(InspectionError, match="not enabled"):
        get_inspection_schema(_catalogue(), "payroll")


def test_inspect_rows_generates_capped_clickhouse_select():
    ch = _FakeClickHouse(
        rows=[
            ("2026-01", "4000", 100),
            ("2026-01", "4010", 200),
            ("2026-01", "4020", 300),
        ]
    )

    result = inspect_rows(
        _catalogue(),
        "gl",
        columns=["period", "account", "amount"],
        limit=2,
        scenario_id="ACTUALS",
        period_start="2026-01",
        period_end="2026-01",
        ch_client=ch,
    )

    assert result["rows"] == [
        {"period": "2026-01", "account": "4000", "amount": 100},
        {"period": "2026-01", "account": "4010", "amount": 200},
    ]
    assert result["truncated"] is True
    assert "SELECT `period`, `account`, `amount`" in ch.sql
    assert "FROM semantic.v_gl" in ch.sql
    assert "scenario = {scenario_id:String}" in ch.sql
    assert "period >= {period_start:String}" in ch.sql
    assert "period <= {period_end:String}" in ch.sql
    assert "commit_id != '__uncommitted__'" in ch.sql
    assert "LIMIT {inspect_limit:UInt64}" in ch.sql
    assert ch.parameters["inspect_limit"] == 3


def test_inspect_rows_rejects_columns_outside_allow_list():
    with pytest.raises(InspectionError, match="not enabled"):
        inspect_rows(
            _catalogue(),
            "gl",
            columns=["period", "supplier_id"],
            ch_client=_FakeClickHouse(),
        )


def test_inspect_rows_resolves_dimension_filters(monkeypatch):
    seen = {}

    def fake_resolve_filters(filters, catalogue, ch_client, domain):
        seen["filters"] = filters
        seen["domain"] = domain
        return {"cost_centre": ["CC-01"]}

    monkeypatch.setattr(
        "precis_mcp.engine.inspect.resolve_filters",
        fake_resolve_filters,
    )
    ch = _FakeClickHouse(rows=[("2026-01", "4000", 100)])

    inspect_rows(
        _catalogue(),
        "gl",
        filters={"cost_centre": "CC-01"},
        ch_client=ch,
    )

    assert seen == {"filters": {"cost_centre": "CC-01"}, "domain": "gl"}
    assert "toString(cost_centre) IN ({dimf_cost_centre:Array(String)})" in ch.sql
    assert ch.parameters["dimf_cost_centre"] == ["CC-01"]


def test_inspect_rows_empty_filter_list_matches_nothing(monkeypatch):
    """A resolved deny-all scope ({col: []}) must still emit the IN
    predicate — dropping it would invert deny-all into allow-all."""
    monkeypatch.setattr(
        "precis_mcp.engine.inspect.resolve_filters",
        lambda filters, catalogue, ch_client, domain: {"cost_centre": []},
    )
    ch = _FakeClickHouse(rows=[])

    inspect_rows(
        _catalogue(),
        "gl",
        filters={"cost_centre": "CC-OUT-OF-SCOPE"},
        ch_client=ch,
    )

    assert "toString(cost_centre) IN ({dimf_cost_centre:Array(String)})" in ch.sql
    assert ch.parameters["dimf_cost_centre"] == []
