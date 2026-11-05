# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Pure deployment selection and catalogue dependency checks."""
import pytest

from precis_mcp.analytical.contracts import ConfigurationError
from precis_mcp.analytical.runtime import ingestion_mode, required_read_columns
from precis_mcp.engine.catalogue import BaseMetric, Catalogue, DomainCatalogue


@pytest.mark.parametrize("backend,configured,expected", [
    ("clickhouse", None, "managed"), ("clickhouse", "customer_managed", "customer_managed"),
    ("databricks", None, "customer_managed"), ("databricks", "managed", None),
    ("clickhouse", "unknown", None),
])
def test_ingestion_selection_is_independent_with_supported_assemblies(monkeypatch, backend, configured, expected):
    monkeypatch.setenv("PRECIS_ANALYTICAL_BACKEND", backend)
    monkeypatch.delenv("PRECIS_INGESTION_MODE", raising=False)
    if configured:
        monkeypatch.setenv("PRECIS_INGESTION_MODE", configured)
    if expected is None:
        with pytest.raises(ConfigurationError):
            ingestion_mode()
    else:
        assert ingestion_mode() == expected


def test_required_inputs_are_derived_from_the_existing_catalogue():
    metric = BaseMetric(key="revenue", label="Revenue", domain="pnl", source_column="amount",
                        aggregation="sum", sign="raw", fs_group="revenue", calculation_note="Source",
                        rollup_method="sum", format="currency")
    catalogue = Catalogue(metrics={"revenue":metric}, scenarios={}, dimensions={}, statements={},
                          domains={"pnl":DomainCatalogue(domain="pnl", source_view="semantic.fact", metrics=[metric])})
    required = required_read_columns(catalogue)
    assert {"scenario", "period", "amount"} <= required["semantic.fact"]
    assert {"scenario_id", "alias", "kind", "locks"} <= required["semantic.scenarios"]
    assert "quarter" not in required["semantic.fact"]
