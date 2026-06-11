# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
from __future__ import annotations

import pytest

from precis_mcp.engine.scenario_registry import (
    InvalidScenarioAliasError,
    NonWritableScenarioError,
    RealScenarioRef,
    ScenarioRegistry,
    ShiftedScenarioRef,
    VariancePctScenarioRef,
    VarianceScenarioRef,
    load_scenario_registry,
)


@pytest.fixture
def registry() -> ScenarioRegistry:
    return ScenarioRegistry.from_rows([
        {
            "scenario_id": "ACTUALS",
            "alias": "actuals",
            "name": "Actuals",
            "description": "Actual data",
            "kind": "ACTUAL",
            "status": "LOCKED",
        },
        {
            "scenario_id": "BUD-2026",
            "alias": "budget",
            "name": "Budget 2026",
            "description": "Budget",
            "kind": "BUDGET",
            "status": "APPROVED",
        },
        {
            "scenario_id": "FC-2026-Q1",
            "alias": "forecast_q1",
            "name": "Forecast Q1",
            "description": "Forecast",
            "kind": "FORECAST",
            "status": "DRAFT",
            "base_scenario": "BUD-2026",
        },
        {
            "scenario_id": "FC-2026-Q2",
            "alias": "forecast_q2",
            "name": "Forecast Q2",
            "description": "Updated forecast",
            "kind": "FORECAST",
            "status": "DRAFT",
            "base_scenario": "BUD-2026",
            "variant_of": "FC-2026-Q1",
        },
    ])


def test_real_alias_resolves_to_scenario_id(registry: ScenarioRegistry):
    ref = registry.resolve_key("budget")

    assert isinstance(ref, RealScenarioRef)
    assert ref.scenario_id == "BUD-2026"
    assert ref.key == "budget"
    assert registry.resolve_writable("budget").scenario_id == "BUD-2026"


def test_literal_scenario_id_resolves_to_real_alias(registry: ScenarioRegistry):
    ref = registry.resolve_key("FC-2026-Q1")

    assert ref.key == "forecast_q1"
    assert registry.normalize_key("FC-2026-Q1") == "forecast_q1"


def test_prior_year_compatibility_alias_resolves_to_actuals_py(registry: ScenarioRegistry):
    ref = registry.resolve_key("prior_year")

    assert isinstance(ref, ShiftedScenarioRef)
    assert ref.key == "actuals_py"
    assert ref.base.scenario_id == "ACTUALS"
    assert ref.time_offset_months == -12


def test_prior_period_compatibility_alias_resolves_to_actuals_pp(registry: ScenarioRegistry):
    ref = registry.resolve_key("prior_period")

    assert isinstance(ref, ShiftedScenarioRef)
    assert ref.key == "actuals_pp"
    assert ref.time_offset_months == -1


def test_generated_shifted_alias_uses_real_alias(registry: ScenarioRegistry):
    ref = registry.resolve_key("forecast_q2_py")

    assert isinstance(ref, ShiftedScenarioRef)
    assert ref.key == "forecast_q2_py"
    assert ref.base.scenario_id == "FC-2026-Q2"
    assert registry.label_for("forecast_q2_py") == "Forecast Q2 PY"


def test_generated_variance_expands_to_both_scenarios(registry: ScenarioRegistry):
    ref = registry.resolve_key("forecast_q2_vs_budget")

    assert isinstance(ref, VarianceScenarioRef)
    assert ref.key == "forecast_q2_vs_budget"
    assert registry.expand_dependencies("forecast_q2_vs_budget") == {
        "FC-2026-Q2",
        "BUD-2026",
    }
    assert registry.color_code_for("forecast_q2_vs_budget") is True


def test_generated_variance_pct_metadata(registry: ScenarioRegistry):
    ref = registry.resolve_key("actuals_vs_budget_pct")

    assert isinstance(ref, VariancePctScenarioRef)
    assert ref.key == "actuals_vs_budget_pct"
    assert registry.label_for("actuals_vs_budget_pct") == "Actuals vs Budget 2026 %"
    assert registry.display_format_for("actuals_vs_budget_pct", "currency") == "percent"


def test_writable_rejects_generated_scenarios(registry: ScenarioRegistry):
    with pytest.raises(NonWritableScenarioError, match="generated"):
        registry.resolve_writable("actuals_vs_budget")


def test_variants_list_by_parent_scenario_id(registry: ScenarioRegistry):
    variants = registry.list_variants("forecast_q1")

    assert [v.scenario_id for v in variants] == ["FC-2026-Q2"]


def test_real_metadata_rows_are_shared_route_shape(registry: ScenarioRegistry):
    rows = registry.to_real_metadata_rows()

    actuals = next(row for row in rows if row["scenario_id"] == "ACTUALS")
    assert actuals["key"] == "actuals"
    assert actuals["alias"] == "actuals"
    assert actuals["label"] == "Actuals"
    assert actuals["type"] == "real"


def test_validation_rows_are_real_scenarios_only(registry: ScenarioRegistry):
    rows = registry.to_validation_scenario_rows()

    assert rows[0].keys() == {
        "scenario_id", "alias", "name", "status", "description", "kind",
    }
    assert {row["scenario_id"] for row in rows} == {
        "ACTUALS", "BUD-2026", "FC-2026-Q1", "FC-2026-Q2",
    }


def test_reporting_vocabulary_sections(registry: ScenarioRegistry):
    vocabulary = registry.to_reporting_vocabulary()

    assert {entry["key"] for entry in vocabulary["real"]} >= {"actuals", "budget"}
    assert {entry["key"] for entry in vocabulary["shifted"]} >= {
        "actuals_py",
        "forecast_q2_pp",
    }
    assert {entry["key"] for entry in vocabulary["comparisons"]} >= {
        "actuals_vs_budget",
        "forecast_q2_vs_forecast_q1_pct",
    }
    assert {"key": "prior_year", "resolves_to": "actuals_py"} in (
        vocabulary["compatibility_aliases"]
    )


def test_vocabulary_includes_self_yoy_comparisons(registry: ScenarioRegistry):
    """Each real scenario surfaces a self-YoY pair (signed + %).

    The registry resolves shifted-on-either-side variance keys generically, but
    the curated vocabulary only enumerates the universal FP&A case — a scenario
    compared to its own prior year. Other shifted combinations remain callable
    but are not surfaced to the agent.
    """
    vocabulary = registry.to_reporting_vocabulary()
    keys = {entry["key"] for entry in vocabulary["comparisons"]}

    for alias in ("actuals", "budget", "forecast_q1", "forecast_q2"):
        assert f"{alias}_vs_{alias}_py" in keys
        assert f"{alias}_vs_{alias}_py_pct" in keys

    # Underlying scenario_ids resolve through the shifted operand correctly.
    actuals_yoy = next(
        e for e in vocabulary["comparisons"] if e["key"] == "actuals_vs_actuals_py"
    )
    assert actuals_yoy["scenario_ids"] == ["ACTUALS"]
    assert actuals_yoy["display_format"] == ""

    actuals_yoy_pct = next(
        e for e in vocabulary["comparisons"] if e["key"] == "actuals_vs_actuals_py_pct"
    )
    assert actuals_yoy_pct["display_format"] == "percent"


def test_self_yoy_keys_resolve_at_runtime(registry: ScenarioRegistry):
    """The keys surfaced in the vocabulary must round-trip through resolve_key."""
    ref = registry.resolve_key("budget_vs_budget_py")

    assert isinstance(ref, VarianceScenarioRef)
    assert ref.left.key == "budget"
    assert ref.right.key == "budget_py"
    assert registry.expand_dependencies("budget_vs_budget_py") == {"BUD-2026"}

    pct_ref = registry.resolve_key("budget_vs_budget_py_pct")
    assert isinstance(pct_ref, VariancePctScenarioRef)
    assert registry.display_format_for("budget_vs_budget_py_pct", "currency") == "percent"


def test_duplicate_alias_rejected():
    rows = [
        {"scenario_id": "A", "alias": "budget", "name": "A"},
        {"scenario_id": "B", "alias": "budget", "name": "B"},
    ]

    with pytest.raises(InvalidScenarioAliasError, match="Duplicate"):
        ScenarioRegistry.from_rows(rows)


@pytest.mark.parametrize("alias", ["prior_year", "budget_py", "actuals_vs_budget"])
def test_reserved_generated_alias_shape_rejected(alias: str):
    with pytest.raises(InvalidScenarioAliasError):
        ScenarioRegistry.from_rows([
            {"scenario_id": "A", "alias": alias, "name": "A"},
        ])


def test_load_scenario_registry_queries_semantic_scenarios():
    class Result:
        column_names = ["scenario_id", "alias", "name", "kind"]
        result_rows = [("ACTUALS", "actuals", "Actuals", "ACTUAL")]

    class Client:
        sql = ""

        def query(self, sql: str):
            self.sql = sql
            return Result()

    client = Client()
    loaded = load_scenario_registry(client)

    assert "FROM semantic.scenarios" in client.sql
    actuals_ref = loaded.resolve_key("actuals")
    assert isinstance(actuals_ref, RealScenarioRef)
    assert actuals_ref.scenario_id == "ACTUALS"
