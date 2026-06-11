# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
from __future__ import annotations

import pytest

from precis_mcp.engine.scenario_registry import (
    NonWritableScenarioError,
    UnknownScenarioError,
)
from precis_mcp.engine.scenario_store import ScenarioStore
from tests.fakes.fake_clickhouse import FakeClickHouseClient, FakeQueryResult


def _client() -> FakeClickHouseClient:
    ch = FakeClickHouseClient()
    ch.set_response(
        "FROM semantic.scenarios",
        FakeQueryResult(
            column_names=[
                "scenario_id", "alias", "name", "base_scenario", "status",
                "description", "created_by", "created_at", "locked_at",
                "horizon_start", "horizon_end", "actuals_cutoff",
                "granularity", "owner_user_id", "updated_at", "variant_of",
                "locks", "kind",
            ],
            result_rows=[
                (
                    "ACTUALS", "actuals", "Actuals", None, "LOCKED",
                    "Actual data", "system", None, None, "", "", None,
                    "monthly", "", None, None, "[]", "ACTUAL",
                ),
                (
                    "BUD-2026", "budget", "Budget 2026", "ACTUALS", "APPROVED",
                    "Budget", "system", None, None, "2026-01", "2026-12", None,
                    "monthly", "", None, None,
                    '[{"period_from": "2026-01", "period_to": "2026-03"}]',
                    "BUDGET",
                ),
                (
                    "BUD-2026-OPT", "budget_optimistic", "Budget Optimistic",
                    "BUD-2026", "DRAFT", "Variant", "alice", None, None,
                    "", "", None, "monthly", "", None, "BUD-2026", "[]",
                    "BUDGET",
                ),
            ],
        ),
    )
    return ch


def test_store_reads_status_horizon_and_locks():
    store = ScenarioStore(_client())

    assert store.get_status("budget") == "APPROVED"
    assert store.get_horizon("budget") == ("2026-01", "2026-12")
    assert store.get_locks("budget") == [
        {"period_from": "2026-01", "period_to": "2026-03"}
    ]


def test_store_lists_variants_by_alias_or_id():
    store = ScenarioStore(_client())

    assert [s.alias for s in store.list_variants("budget")] == ["budget_optimistic"]
    assert [s.alias for s in store.list_variants("BUD-2026")] == ["budget_optimistic"]


def test_store_exists_handles_unknown():
    store = ScenarioStore(_client())

    assert store.exists("budget") is True
    assert store.exists("missing") is False


def test_store_resolves_writable_alias_and_id():
    store = ScenarioStore(_client())

    assert store.resolve_writable_id("actuals") == "ACTUALS"
    assert store.resolve_writable_id("budget") == "BUD-2026"
    assert store.resolve_writable_id("BUD-2026") == "BUD-2026"
    assert store.resolve_writable_id("TEST-SANDBOX-01") == "TEST-SANDBOX-01"


def test_store_rejects_generated_or_unknown_writable_refs():
    store = ScenarioStore(_client())

    with pytest.raises(NonWritableScenarioError):
        store.resolve_writable_id("actuals_vs_budget")

    with pytest.raises(NonWritableScenarioError):
        store.resolve_writable_id("prior_year")

    with pytest.raises(UnknownScenarioError):
        store.resolve_writable_id("missing")


def test_store_creates_scenario_with_derived_alias():
    ch = FakeClickHouseClient()
    store = ScenarioStore(ch)

    alias = store.create_scenario(
        scenario_id="FC-2026-Q2",
        name="Forecast Q2",
        base_scenario="BUD-2026",
        kind="FORECAST",
        description="",
        horizon_start="2026-01",
        horizon_end="2026-12",
        actuals_cutoff="",
        granularity="monthly",
        variant_of="",
        user_id=7,
    )

    assert alias == "fc_2026_q2"
    sql, params = ch.commands[0]
    assert "INSERT INTO semantic.scenarios" in sql
    assert params["alias"] == "fc_2026_q2"
    assert params["ac"] is None
    assert params["vof"] is None


def test_store_updates_scenario_metadata():
    ch = FakeClickHouseClient()
    store = ScenarioStore(ch)

    store.update_scenario_metadata(
        "FC-2026",
        {"status": "PUBLISHED", "actuals_cutoff": None},
    )

    sql, params = ch.commands[0]
    assert "ALTER TABLE semantic.scenarios UPDATE" in sql
    assert "status = {new_status:String}" in sql
    assert "actuals_cutoff = NULL" in sql
    assert params["sid"] == "FC-2026"
    assert params["new_status"] == "PUBLISHED"


def test_store_reads_and_updates_lock_metadata():
    ch = FakeClickHouseClient()
    ch.set_query_responses(
        FakeQueryResult(result_rows=[('[{"period_from": "2026-01"}]',)]),
    )
    store = ScenarioStore(ch)

    assert store.get_locks_metadata("FC-2026") == [{"period_from": "2026-01"}]
    store.update_locks("FC-2026", [{"period_from": "2026-02"}])

    sql, params = ch.commands[0]
    assert "ALTER TABLE semantic.scenarios UPDATE locks" in sql
    assert params["sid"] == "FC-2026"
    assert params["locks"] == '[{"period_from": "2026-02"}]'
