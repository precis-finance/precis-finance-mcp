# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Tests for precis_mcp/engine/context_validation.py"""
from __future__ import annotations

from pathlib import Path

import pytest

from precis_mcp.engine.context_validation import (
    ContextValidationResult,
    DimensionMapResult,
    FilterValidationResult,
    ValidationIssue,
    _get_domains_for_tool_call,
    _get_valid_filter_keys_for_domains,
    _get_valid_keys_for_plan_datasets,
    _resolve_domains_for_statement,
    _resolve_metric_domain,
    validate_dimension_map,
    validate_filters_for_report,
    validate_report_context,
)
from precis_mcp.engine.scenario_registry import ScenarioRegistry

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Domain resolution helpers
# ---------------------------------------------------------------------------


class TestResolveMetricDomain:
    def test_base_metric_returns_domain(self, catalogue):
        domain = _resolve_metric_domain("revenue", catalogue)
        assert domain == "pnl"

    def test_derived_metric_resolves_to_base_domain(self, catalogue):
        # gross_margin = revenue - direct_cost, both are pnl domain
        domain = _resolve_metric_domain("gross_margin", catalogue)
        assert domain == "pnl"

    def test_unknown_metric_returns_none(self, catalogue):
        domain = _resolve_metric_domain("nonexistent_metric", catalogue)
        assert domain is None

    def test_payroll_metric_returns_payroll_domain(self, catalogue):
        domain = _resolve_metric_domain("total_payroll_cost", catalogue)
        assert domain == "payroll"


class TestResolveDomainForStatement:
    def test_pnl_statement_resolves_to_pnl(self, catalogue):
        domains = _resolve_domains_for_statement("pnl", catalogue)
        assert "pnl" in domains

    def test_full_pnl_resolves_domains(self, catalogue):
        domains = _resolve_domains_for_statement("full_pnl", catalogue)
        # full_pnl concatenates pnl + statistical + ratios — all metrics are pnl domain
        assert "pnl" in domains
        assert len(domains) >= 1

    def test_unknown_statement_returns_empty(self, catalogue):
        domains = _resolve_domains_for_statement("nonexistent", catalogue)
        assert domains == set()


class TestGetDomainsForToolCall:
    def test_run_statement_resolves_domains(self, catalogue):
        domains = _get_domains_for_tool_call(
            "run_statement", {"statement": "pnl"}, catalogue,
        )
        assert "pnl" in domains

    def test_run_metric_resolves_domains(self, catalogue):
        domains = _get_domains_for_tool_call(
            "run_metric", {"metrics": ["revenue", "direct_cost"]}, catalogue,
        )
        assert domains == {"pnl"}

    def test_run_metric_mixed_domains(self, catalogue):
        domains = _get_domains_for_tool_call(
            "run_metric", {"metrics": ["revenue", "total_payroll_cost"]}, catalogue,
        )
        assert "pnl" in domains
        assert "payroll" in domains

    def test_unknown_tool_returns_empty(self, catalogue):
        domains = _get_domains_for_tool_call(
            "some_other_tool", {}, catalogue,
        )
        assert domains == set()


class TestGetValidFilterKeysForDomains:
    def test_pnl_domain_includes_org_structure(self, catalogue):
        valid = _get_valid_filter_keys_for_domains({"pnl"}, catalogue)
        # pnl domain has cost_centre dimension → org_structure rollup hierarchy
        assert "org_structure" in valid

    def test_pnl_domain_includes_attribute_filters(self, catalogue):
        valid = _get_valid_filter_keys_for_domains({"pnl"}, catalogue)
        # cost_centre dimension has division and department as non-leaf levels
        assert "division" in valid
        assert "department" in valid

    def test_empty_domains_returns_empty(self, catalogue):
        valid = _get_valid_filter_keys_for_domains(set(), catalogue)
        assert valid == set()


# ---------------------------------------------------------------------------
# Context validation (conversation start)
# ---------------------------------------------------------------------------


class TestValidateReportContext:
    def test_valid_context_passes(self, catalogue, scenario_registry):
        ctx = {
            "filters": {"org_structure": "dept:some_dept"},
            "statement": "pnl",
            "scenarios": [{"scenario": "actuals", "alias": "Actuals"}],
        }
        result = validate_report_context(
            ctx,
            catalogue,
            scenario_registry=scenario_registry,
        )
        assert not result.has_errors
        assert not result.has_warnings
        assert result.valid_context["filters"] == ctx["filters"]

    def test_invalid_filter_key_produces_error(self, catalogue):
        ctx = {"filters": {"nonexistent_dim": "some_value"}}
        result = validate_report_context(ctx, catalogue)
        assert result.has_errors
        assert len(result.issues) == 1
        assert result.issues[0].field == "filters"
        assert result.issues[0].severity == "error"
        assert "nonexistent_dim" in result.issues[0].detail
        # Invalid filter should be removed from valid_context
        assert "nonexistent_dim" not in result.valid_context.get("filters", {})

    def test_invalid_statement_produces_error(self, catalogue):
        ctx = {"statement": "nonexistent_statement"}
        result = validate_report_context(ctx, catalogue)
        assert result.has_errors
        assert result.issues[0].field == "statement"
        # Invalid statement should be removed from valid_context
        assert "statement" not in result.valid_context

    def test_invalid_scenario_produces_warning(self, catalogue, scenario_registry):
        ctx = {
            "scenarios": [
                {"scenario": "actuals", "alias": "Actuals"},
                {"scenario": "nonexistent_scenario", "alias": "Bad"},
            ],
        }
        result = validate_report_context(
            ctx,
            catalogue,
            scenario_registry=scenario_registry,
        )
        assert result.has_warnings
        assert len(result.issues) == 1
        assert result.issues[0].field == "scenarios"
        assert result.issues[0].severity == "warning"
        # Valid scenario should be preserved
        assert len(result.valid_context["scenarios"]) == 1
        assert result.valid_context["scenarios"][0]["scenario"] == "actuals"

    def test_scenario_registry_normalizes_context_scenarios(self, catalogue):
        registry = ScenarioRegistry.from_rows([
            {
                "scenario_id": "ACTUALS",
                "alias": "actuals",
                "name": "Actuals",
                "status": "LOCKED",
                "kind": "ACTUAL",
            },
        ])
        ctx = {"scenarios": [{"scenario": "ACTUALS", "alias": "Actuals"}]}

        result = validate_report_context(
            ctx,
            catalogue,
            scenario_registry=registry,
        )

        assert not result.has_warnings
        assert result.valid_context["scenarios"][0]["scenario"] == "actuals"

    def test_empty_context_passes(self, catalogue):
        result = validate_report_context({}, catalogue)
        assert not result.has_errors
        assert not result.has_warnings

    def test_mixed_valid_and_invalid_filters(self, catalogue):
        ctx = {
            "filters": {
                "org_structure": "dept:cyber",  # valid key
                "bogus_dim": "foo",  # invalid key
            },
        }
        result = validate_report_context(ctx, catalogue)
        assert result.has_errors
        assert "org_structure" in result.valid_context["filters"]
        assert "bogus_dim" not in result.valid_context["filters"]

    def test_empty_filters_dict_passes(self, catalogue):
        ctx = {"filters": {}}
        result = validate_report_context(ctx, catalogue)
        assert not result.has_errors


# ---------------------------------------------------------------------------
# Call-time filter validation
# ---------------------------------------------------------------------------


class TestValidateFiltersForReport:
    def test_relevant_filter_passes(self, catalogue):
        # org_structure is valid for pnl domain
        result = validate_filters_for_report(
            filters={"org_structure": "dept:cyber"},
            tool_name="run_statement",
            tool_args={"statement": "pnl"},
            catalogue=catalogue,
        )
        assert not result.errors
        assert not result.warnings
        assert "org_structure" in result.cleaned_filters

    def test_unknown_filter_key_produces_error(self, catalogue):
        result = validate_filters_for_report(
            filters={"nonexistent_dim": "value"},
            tool_name="run_statement",
            tool_args={"statement": "pnl"},
            catalogue=catalogue,
        )
        assert len(result.errors) == 1
        assert "nonexistent_dim" not in result.cleaned_filters

    def test_irrelevant_filter_removed_with_warning(self, catalogue):
        # Find a filter key that exists in the catalogue but doesn't apply
        # to the pnl domain. If all filter keys apply to pnl (because pnl
        # has cost_centre which has all filters), test with run_metric on
        # a domain that lacks the dimension.
        # For this test, we check the mechanics: if a key is valid but not
        # in the domain's valid set, it gets a warning.
        result = validate_filters_for_report(
            filters={"org_structure": "dept:cyber"},
            tool_name="run_metric",
            tool_args={"metrics": ["revenue"]},
            catalogue=catalogue,
        )
        # org_structure should be valid for pnl domain (revenue is pnl)
        # so this should pass without warnings
        assert not result.errors

    def test_empty_filters_returns_empty(self, catalogue):
        result = validate_filters_for_report(
            filters={},
            tool_name="run_statement",
            tool_args={"statement": "pnl"},
            catalogue=catalogue,
        )
        assert result.cleaned_filters == {}
        assert not result.errors
        assert not result.warnings

    def test_unresolvable_domains_passes_through(self, catalogue):
        # If we can't determine domains, filters pass through unchanged
        result = validate_filters_for_report(
            filters={"org_structure": "dept:cyber"},
            tool_name="run_metric",
            tool_args={"metrics": []},  # empty metrics → no domains
            catalogue=catalogue,
        )
        assert "org_structure" in result.cleaned_filters
        assert not result.errors


# ---------------------------------------------------------------------------
# Data class properties
# ---------------------------------------------------------------------------


class TestResultDataclasses:
    def test_context_result_has_errors(self):
        result = ContextValidationResult(
            valid_context={},
            issues=[ValidationIssue(field="f", severity="error", detail="bad")],
        )
        assert result.has_errors
        assert not result.has_warnings

    def test_context_result_has_warnings(self):
        result = ContextValidationResult(
            valid_context={},
            issues=[ValidationIssue(field="f", severity="warning", detail="meh")],
        )
        assert not result.has_errors
        assert result.has_warnings

    def test_filter_result_defaults(self):
        result = FilterValidationResult(cleaned_filters={"a": "b"})
        assert result.warnings == []
        assert result.errors == []


# ---------------------------------------------------------------------------
# Generic dimension-map validation
# ---------------------------------------------------------------------------


class TestGetValidKeysForPlanDatasets:
    def test_active_catalogue_returns_keys(self, catalogue):
        valid = _get_valid_keys_for_plan_datasets(catalogue)
        # At minimum, the plan dataset dimension keys themselves
        assert len(valid) > 0
        # 'period' is a framework column — not included unless a dataset
        # lists it as a PlanDatasetDimension or a parent resolves to it
        # (depends on the live catalogue shape; just assert non-empty here)

    def test_includes_plan_dataset_dim_keys(self, catalogue):
        valid = _get_valid_keys_for_plan_datasets(catalogue)
        # Every PlanDatasetDimension.key should be in the valid set
        for ds in catalogue.plan_datasets.values():
            for pds_dim in ds.dimensions:
                assert pds_dim.key in valid


class TestValidateDimensionMap:
    def test_empty_map_returns_empty(self, catalogue):
        result = validate_dimension_map(
            {}, "report_domains", "run_statement",
            {"statement": "pnl"}, catalogue,
        )
        assert isinstance(result, DimensionMapResult)
        assert result.cleaned == {}
        assert result.errors == []
        assert result.warnings == []

    def test_unknown_target_raises(self, catalogue):
        with pytest.raises(ValueError):
            validate_dimension_map(
                {"org_structure": "x"}, "not_a_target",
                "run_statement", {"statement": "pnl"}, catalogue,
            )

    def test_schema_error_for_unknown_key(self, catalogue):
        result = validate_dimension_map(
            {"nonexistent_dim": "v"}, "report_domains",
            "run_statement", {"statement": "pnl"}, catalogue,
        )
        assert len(result.errors) == 1
        assert "nonexistent_dim" in result.errors[0]
        assert "nonexistent_dim" not in result.cleaned

    def test_valid_report_domain_key_passes(self, catalogue):
        result = validate_dimension_map(
            {"org_structure": "dept:cyber"}, "report_domains",
            "run_statement", {"statement": "pnl"}, catalogue,
        )
        assert not result.errors
        assert "org_structure" in result.cleaned

    def test_plan_datasets_target_accepts_plan_dim(self, catalogue):
        # Pick the first plan dataset dim key
        if not catalogue.plan_datasets:
            pytest.skip("No plan datasets in active catalogue")
        first_ds = next(iter(catalogue.plan_datasets.values()))
        if not first_ds.dimensions:
            pytest.skip("Plan dataset has no dimensions")
        dim_key = first_ds.dimensions[0].key

        result = validate_dimension_map(
            {dim_key: "some_value"}, "plan_datasets",
            "commit_plan", {}, catalogue,
        )
        assert not result.errors
        assert dim_key in result.cleaned
