# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Tests for precis_mcp/scope_resolver.py — lock resolution and scope intersection."""

import pytest
from precis_mcp.scope_resolver import (
    _generate_monthly_range,
    is_cell_locked,
    load_scenario_locks,
    resolve_lock_condition,
)


# ---------------------------------------------------------------------------
# _generate_monthly_range
# ---------------------------------------------------------------------------


class TestGenerateMonthlyRange:

    def test_single_month(self):
        assert _generate_monthly_range("2026-03", "2026-03") == ["2026-03"]

    def test_quarter(self):
        result = _generate_monthly_range("2026-01", "2026-03")
        assert result == ["2026-01", "2026-02", "2026-03"]

    def test_cross_year(self):
        result = _generate_monthly_range("2025-11", "2026-02")
        assert result == ["2025-11", "2025-12", "2026-01", "2026-02"]

    def test_full_year(self):
        result = _generate_monthly_range("2026-01", "2026-12")
        assert len(result) == 12
        assert result[0] == "2026-01"
        assert result[-1] == "2026-12"


# ---------------------------------------------------------------------------
# is_cell_locked
# ---------------------------------------------------------------------------


class TestIsCellLocked:

    def test_no_locks_returns_false(self):
        cell = {"account": "4100", "cost_centre": "CC-01", "period": "2026-03"}
        assert is_cell_locked(cell, []) is False

    def test_period_lock_matches(self):
        cell = {"account": "4100", "cost_centre": "CC-01", "period": "2026-03"}
        resolved = [{"period": {"2026-01", "2026-02", "2026-03"}}]
        assert is_cell_locked(cell, resolved) is True

    def test_period_lock_no_match(self):
        cell = {"account": "4100", "cost_centre": "CC-01", "period": "2026-04"}
        resolved = [{"period": {"2026-01", "2026-02", "2026-03"}}]
        assert is_cell_locked(cell, resolved) is False

    def test_multi_dimension_and(self):
        """All dimensions in a condition must match (AND within)."""
        cell = {"account": "4100", "cost_centre": "CC-01", "period": "2026-03"}
        resolved = [{"period": {"2026-03"}, "cost_centre": {"CC-02"}}]
        assert is_cell_locked(cell, resolved) is False  # CC-01 ≠ CC-02

    def test_multi_dimension_full_match(self):
        cell = {"account": "4100", "cost_centre": "CC-01", "period": "2026-03"}
        resolved = [{"period": {"2026-03"}, "cost_centre": {"CC-01"}}]
        assert is_cell_locked(cell, resolved) is True

    def test_or_across_conditions(self):
        """Any condition matching means locked (OR across)."""
        cell = {"account": "4100", "cost_centre": "CC-01", "period": "2026-05"}
        resolved = [
            {"period": {"2026-01", "2026-02", "2026-03"}},  # no match
            {"cost_centre": {"CC-01"}},                       # match
        ]
        assert is_cell_locked(cell, resolved) is True

    def test_wildcard_dimension(self):
        """Dimensions not in a condition are wildcards."""
        cell = {"account": "4100", "cost_centre": "CC-01", "period": "2026-03"}
        resolved = [{"period": {"2026-03"}}]  # no cost_centre constraint
        assert is_cell_locked(cell, resolved) is True

    def test_cell_missing_dimension(self):
        """If cell doesn't have a dimension the lock requires, it doesn't match."""
        cell = {"account": "4100", "period": "2026-03"}  # no cost_centre
        resolved = [{"cost_centre": {"CC-01"}}]
        assert is_cell_locked(cell, resolved) is False


# ---------------------------------------------------------------------------
# load_scenario_locks
# ---------------------------------------------------------------------------


class TestLoadScenarioLocks:

    def test_empty_when_no_rows(self):
        class MockCH:
            def query(self, sql, parameters=None):
                class R:
                    result_rows = []
                return R()
        assert load_scenario_locks("BUD-2026", MockCH()) == []

    def test_empty_when_empty_json(self):
        class MockCH:
            def query(self, sql, parameters=None):
                class R:
                    result_rows = [("[]",)]
                return R()
        assert load_scenario_locks("BUD-2026", MockCH()) == []

    def test_parses_locks(self):
        import json
        locks = [{"period_from": "2026-01", "period_to": "2026-03"}]

        class MockCH:
            def query(self, sql, parameters=None):
                class R:
                    result_rows = [(json.dumps(locks),)]
                return R()
        result = load_scenario_locks("BUD-2026", MockCH())
        assert result == locks

    def test_returns_empty_on_exception(self):
        class MockCH:
            def query(self, sql, parameters=None):
                raise RuntimeError("connection failed")
        assert load_scenario_locks("BUD-2026", MockCH()) == []


# ---------------------------------------------------------------------------
# resolve_lock_condition — skips _type metadata key
# ---------------------------------------------------------------------------


class TestResolveLockCondition:

    def test_type_key_is_ignored(self):
        """The _type metadata key should not be resolved as a dimension."""
        from unittest.mock import MagicMock

        catalogue = MagicMock()
        catalogue.dimensions = {"period": MagicMock(source=None)}
        ch = MagicMock()

        condition = {
            "_type": "actuals_cutoff",
            "period_from": "2026-01",
            "period_to": "2026-03",
        }
        result = resolve_lock_condition(condition, catalogue, ch)
        assert "period" in result
        assert "_type" not in result
        assert result["period"] == {"2026-01", "2026-02", "2026-03"}
