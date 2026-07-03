# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Component tests for the metric-engine 5-stage pipeline.

Exercises `execute_report` end-to-end with a patched retriever or an in-test
fake ClickHouse client; exercises `retrieve` directly against a fake CH to
verify shifted-period key remapping. Pure catalogue/resolver/formatter unit
tests live in `tests/unit/test_engine_catalogue.py`.
"""
from __future__ import annotations

import os
import textwrap
from unittest.mock import MagicMock, patch

import pytest

from precis_mcp.engine import (
    Catalogue, execute_report, load_and_validate,
    _build_dim_formats, _resolve_master_dimension,
)
from precis_mcp.engine.formatter import DimensionFormat
from precis_mcp.engine.resolver import ResolverError
from precis_mcp.engine.catalogue import load_catalogue
from tests.fakes.fake_clickhouse import FakeClickHouseClient, FakeQueryResult


CATALOGUE_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'instance', 'catalogue')


@pytest.fixture(scope="module")
def catalogue() -> Catalogue:
    return load_and_validate(CATALOGUE_DIR)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_results(scenario_key: str, metrics_data: dict, dim_key: tuple = ()) -> dict:
    """Build a RawResults fragment for one scenario."""
    return {scenario_key: {dim_key: metrics_data}}


def _pnl_base_values() -> dict:
    """Typical base metric values (no derived metrics — transformer computes those).

    Costs are stored as positive numbers in the ledger (debit-side entries).
    Derived metric formulas use subtraction (e.g. gross_margin = revenue - direct_cost).
    """
    return {
        'revenue': 1_000_000.0,
        'direct_cost': 600_000.0,
        'indirect_cost': 100_000.0,
        'sga': 50_000.0,
        'billable_hours': 15_000.0,
        'total_hours': 20_000.0,
        'avg_fte_billable': 50.0,
        'avg_fte_overhead': 20.0,
        'closing_fte_billable': 52.0,
        'closing_fte_overhead': 21.0,
    }


def _scenario_registry_client() -> FakeClickHouseClient:
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
                    "BUD-2026", "budget", "Budget", "ACTUALS", "DRAFT",
                    "Budget", "system", None, None, "2026-01", "2026-12", None,
                    "monthly", "", None, None, "[]", "BUDGET",
                ),
                (
                    "FC-2026-Q2", "forecast_q2", "Forecast Q2", "BUD-2026", "DRAFT",
                    "Runtime forecast", "system", None, None, "2026-01", "2026-12", None,
                    "monthly", "", None, None, "[]", "FORECAST",
                ),
            ],
        ),
    )
    return ch


def _write_inline_federated_catalogue(tmp_path):
    (tmp_path / "gl_federated.yml").write_text(textwrap.dedent("""
        domain: gl_federated
        source_view: finance.gl_transactions_detail
        backend: customer_pg
        backend_kind: ibis
        versioned: false
        dimensions:
          - key: supplier_id
            label: Supplier
            source_inline: true
            filterable: false
        metrics:
          - key: federated_net_amount
            label: Amount
            source_column: amount
            aggregation: sum
            rollup_method: sum
            sign: raw
            format: currency
            fs_group: GL
    """))
    return load_catalogue(str(tmp_path))


# ---------------------------------------------------------------------------
# Test 1: Simple P&L dry run (no ch_client)
# ---------------------------------------------------------------------------

def test_dry_run_no_crash(catalogue, scenario_registry):
    """With ch_client=None, the pipeline should complete without crashing."""
    request = {
        'context': {'period_start': '2025-01', 'period_end': '2025-12'},
        'blocks': [{'model': 'statement:pnl', 'scenario': 'actuals', 'alias': 'Actuals'}],
    }
    response = execute_report(request, catalogue, ch_client=None, scenario_registry=scenario_registry)

    assert 'rows' in response
    assert response['dimensions'] == []
    assert [s['alias'] for s in response['scenarios']] == ['Actuals']


def test_semantic_runtime_scenario_alias_executes(catalogue, scenario_registry):
    request = {
        'context': {'period_start': '2026-01', 'period_end': '2026-03'},
        'blocks': [
            {
                'model': 'metrics:revenue',
                'scenario': 'forecast_q2',
                'alias': 'Forecast Q2',
            }
        ],
    }

    with patch("precis_mcp.engine.retriever.retrieve") as mock_retrieve:
        mock_retrieve.return_value = _make_mock_results(
            "forecast_q2",
            {"revenue": 123.0},
        )
        response = execute_report(
            request,
            catalogue,
            ch_client=_scenario_registry_client(),
        )

    query = mock_retrieve.call_args[0][0].data_queries[0]
    assert query.scenario_key == "forecast_q2"
    assert query.scenario_id == "FC-2026-Q2"
    assert response["rows"][0]["values"]["Forecast Q2"] == 123.0


def test_semantic_generated_variance_executes(catalogue, scenario_registry):
    request = {
        'context': {'period_start': '2026-01', 'period_end': '2026-03'},
        'blocks': [
            {
                'model': 'metrics:revenue',
                'scenario': 'actuals_vs_budget',
                'alias': 'Actuals vs Budget',
            }
        ],
    }

    with patch("precis_mcp.engine.retriever.retrieve") as mock_retrieve:
        mock_retrieve.return_value = {
            "actuals": {(): {"revenue": 150.0}},
            "budget": {(): {"revenue": 100.0}},
        }
        response = execute_report(
            request,
            catalogue,
            ch_client=_scenario_registry_client(),
        )

    scenario_keys = {dq.scenario_key for dq in mock_retrieve.call_args[0][0].data_queries}
    assert scenario_keys == {"actuals", "budget"}
    assert response["rows"][0]["values"]["Actuals vs Budget"] == 50.0
    assert [s["alias"] for s in response["scenarios"]] == ["Actuals vs Budget"]


def test_inline_federated_dimension_allowed_as_axis(tmp_path, scenario_registry):
    """Source-only federated dimensions can be used as reporting axes."""
    cat = _write_inline_federated_catalogue(tmp_path)
    request = {
        'context': {'period_start': '2025-01', 'period_end': '2025-12'},
        'dimensions': ['supplier_id'],
        'blocks': [
            {
                'model': 'metric:federated_net_amount',
                'scenario': 'actuals',
                'alias': 'Actuals',
            },
        ],
    }

    response = execute_report(
        request,
        cat,
        ch_client=None,
        ibis_backends={'customer_pg': object()},
        scenario_registry=scenario_registry,
    )

    assert response['dimensions'] == ['supplier_id']


def test_inline_federated_dimension_rejected_as_filter(tmp_path, scenario_registry):
    """Source-only federated dimensions are not filterable in this phase."""
    cat = _write_inline_federated_catalogue(tmp_path)
    request = {
        'context': {'period_start': '2025-01', 'period_end': '2025-12'},
        'filters': {'supplier_id': 'SUP-001'},
        'blocks': [
            {
                'model': 'metric:federated_net_amount',
                'scenario': 'actuals',
                'alias': 'Actuals',
            },
        ],
    }

    with pytest.raises(ResolverError, match="reporting axes.*filters"):
        execute_report(
            request,
            cat,
            ch_client=None,
            ibis_backends={'customer_pg': object()},
            scenario_registry=scenario_registry,
        )


# ---------------------------------------------------------------------------
# Test 2: Full P&L with mock data — derived metrics computed
# ---------------------------------------------------------------------------

def test_full_pnl_with_mock_data(catalogue, scenario_registry):
    """Mock the retriever to inject raw base metrics; verify derived metrics computed."""
    raw = _pnl_base_values()
    mock_results = _make_mock_results('actuals', raw)

    request = {
        'context': {'period_start': '2025-01', 'period_end': '2025-12'},
        'blocks': [{'model': 'statement:pnl', 'scenario': 'actuals', 'alias': 'Actuals'}],
    }

    with patch('precis_mcp.engine.retriever.retrieve', return_value=mock_results):
        # ch_client must be non-None to trigger retriever.retrieve
        response = execute_report(request, catalogue, ch_client=MagicMock(), scenario_registry=scenario_registry)

    assert 'rows' in response

    # Build a lookup: metric_key -> row
    row_map = {row['item']['key']: row for row in response['rows']}

    # Check derived metrics were computed
    # gross_margin = revenue - direct_cost = 1_000_000 - 600_000 = 400_000
    assert 'gross_margin' in row_map
    gm_val = row_map['gross_margin']['values']['Actuals']
    assert gm_val == pytest.approx(400_000.0, abs=1.0)

    # contribution_margin = gross_margin - indirect_cost = 400_000 - 100_000 = 300_000
    # ebitda = contribution_margin - sga = 300_000 - 50_000 = 250_000
    assert 'ebitda' in row_map
    ebitda_val = row_map['ebitda']['values']['Actuals']
    assert ebitda_val == pytest.approx(250_000.0, abs=1.0)

    # Separators are a line property in the unified schema
    assert any(r['item'].get('separator_above') for r in response['rows'])


# ---------------------------------------------------------------------------
# Test 3: Actuals vs Budget variance — four blocks
# ---------------------------------------------------------------------------

def test_actuals_vs_actuals_vs_budget(catalogue, scenario_registry):
    """Four-block variance report with mock data; verify variance computation."""
    actuals_raw = {
        'revenue': 1_000_000.0,
        'direct_cost': 600_000.0,
        'indirect_cost': 100_000.0,
        'sga': 50_000.0,
        'billable_hours': 15_000.0,
        'total_hours': 20_000.0,
        'avg_fte_billable': 50.0,
        'avg_fte_overhead': 20.0,
        'closing_fte_billable': 52.0,
        'closing_fte_overhead': 21.0,
    }
    budget_raw = {
        'revenue': 900_000.0,
        'direct_cost': 550_000.0,
        'indirect_cost': 90_000.0,
        'sga': 45_000.0,
        'billable_hours': 14_000.0,
        'total_hours': 19_000.0,
        'avg_fte_billable': 48.0,
        'avg_fte_overhead': 19.0,
        'closing_fte_billable': 50.0,
        'closing_fte_overhead': 20.0,
    }
    mock_results = {
        'actuals': {(): actuals_raw},
        'budget': {(): budget_raw},
    }

    request = {
        'context': {'period_start': '2025-01', 'period_end': '2025-12'},
        'blocks': [
            {'model': 'statement:pnl', 'scenario': 'actuals', 'alias': 'Actuals'},
            {'model': 'statement:pnl', 'scenario': 'budget', 'alias': 'Budget'},
            {'model': 'statement:pnl', 'scenario': 'actuals_vs_budget', 'alias': 'Var'},
            {'model': 'statement:pnl', 'scenario': 'actuals_vs_budget_pct', 'alias': 'Var%'},
        ],
    }

    with patch('precis_mcp.engine.retriever.retrieve', return_value=mock_results):
        response = execute_report(request, catalogue, ch_client=MagicMock(), scenario_registry=scenario_registry)

    assert [s['alias'] for s in response['scenarios']] == ['Actuals', 'Budget', 'Var', 'Var%']

    # Find revenue row
    revenue_row = next(
        (r for r in response['rows'] if r['item']['key'] == 'revenue'),
        None,
    )
    assert revenue_row is not None
    vals = revenue_row['values']
    assert set(['Actuals', 'Budget', 'Var', 'Var%']).issubset(set(vals.keys()))

    # Variance = actuals - budget = 1_000_000 - 900_000 = 100_000
    assert vals['Var'] == pytest.approx(100_000.0, abs=1.0)

    # Variance % = (actuals - budget) / abs(budget) * 100 = 100_000 / 900_000 * 100 ≈ 11.11
    assert vals['Var%'] == pytest.approx(11.11, abs=0.1)


# ---------------------------------------------------------------------------
# Test 4: Period as dimension — one row per period
# ---------------------------------------------------------------------------

def test_period_dimension(catalogue, scenario_registry):
    """Period dimension: response should have one row per period."""
    mock_results = {
        'actuals': {
            ('2025-01',): {'revenue': 100_000.0},
            ('2025-02',): {'revenue': 110_000.0},
            ('2025-03',): {'revenue': 120_000.0},
        }
    }

    request = {
        'context': {'period_start': '2025-01', 'period_end': '2025-03'},
        'dimensions': ['period'],
        'blocks': [{'model': 'metric:revenue', 'scenario': 'actuals', 'alias': 'Actuals'}],
    }

    with patch('precis_mcp.engine.retriever.retrieve', return_value=mock_results):
        response = execute_report(request, catalogue, ch_client=MagicMock(), scenario_registry=scenario_registry)

    assert 'rows' in response
    assert len(response['rows']) == 3

    periods = [row['dimensions']['period'] for row in response['rows']]
    assert '2025-01' in periods
    assert '2025-02' in periods
    assert '2025-03' in periods


def test_ragged_breakdown_end_to_end(catalogue, scenario_registry):
    """Breakdown by a ragged hierarchy: anchor node → Total, children → detail
    rows carrying node display names (from the node master)."""
    mock_results = {'actuals': {
        ('P-DATAAI',): {'revenue': 900_000.0},
        ('S-ANALYTICS',): {'revenue': 500_000.0},
        ('S-MLAI',): {'revenue': 400_000.0},
    }}
    ch = FakeClickHouseClient()
    # filter resolution: anchor node → leaf ids (rollup query)
    ch.set_response("solution_portfolio_rollup", [("CC-DANA-01",), ("CC-SENG-06",)])
    # node labels (node-master query)
    ch.set_response("node_id, display_name", [
        ("P-DATAAI", "Data & AI"),
        ("S-ANALYTICS", "Analytics & BI"),
        ("S-MLAI", "ML & AI Solutions"),
    ])
    request = {
        'context': {'period_start': '2025-01', 'period_end': '2025-12'},
        'dimensions': ['solution_portfolio'],
        'filters': {'solution_portfolio': 'P-DATAAI'},
        'blocks': [{'model': 'metric:revenue', 'scenario': 'actuals', 'alias': 'Actuals'}],
    }
    with patch('precis_mcp.engine.retriever.retrieve', return_value=mock_results):
        response = execute_report(
            request, catalogue, ch_client=ch, scenario_registry=scenario_registry,
        )

    assert 'rows' in response
    dim_vals = [r.get('dimensions', {}).get('solution_portfolio') for r in response['rows']]
    assert 'Analytics & BI' in dim_vals      # child breakdown, display name
    assert 'ML & AI Solutions' in dim_vals
    values = [r['values'].get('Actuals') for r in response['rows']]
    assert pytest.approx(900_000.0, abs=1.0) in values  # the anchor total


# ---------------------------------------------------------------------------
# Test 5: Prior year shifted scenario — retriever receives shifted period
# ---------------------------------------------------------------------------

def test_prior_year_shifted_period(catalogue, scenario_registry):
    """Prior-year block must result in a DataQuery with shifted periods (-12 months)."""
    request = {
        'context': {'period_start': '2025-01', 'period_end': '2025-12'},
        'blocks': [
            {'model': 'metric:revenue', 'scenario': 'actuals', 'alias': 'Actuals'},
            {'model': 'metric:revenue', 'scenario': 'prior_year', 'alias': 'PY'},
        ],
    }

    captured_plans = []

    def fake_retrieve(plan, cat, ch, dimension_filters=None):
        captured_plans.append(plan)
        return {}

    with patch('precis_mcp.engine.retriever.retrieve', side_effect=fake_retrieve):
        execute_report(request, catalogue, ch_client=MagicMock(), scenario_registry=scenario_registry)

    assert len(captured_plans) == 1
    plan = captured_plans[0]

    # Should have two data queries: actuals (2025) and prior_year (2024)
    assert len(plan.data_queries) == 2

    query_map = {dq.scenario_key: dq for dq in plan.data_queries}

    assert 'actuals' in query_map
    assert query_map['actuals'].period_start == '2025-01'
    assert query_map['actuals'].period_end == '2025-12'

    assert 'prior_year' in query_map
    assert query_map['prior_year'].period_start == '2024-01'
    assert query_map['prior_year'].period_end == '2024-12'
    assert query_map['prior_year'].time_offset == -12


# ---------------------------------------------------------------------------
# Test 5b: Prior year retriever remaps period dimension keys
# ---------------------------------------------------------------------------

def test_prior_year_period_remapped_in_retriever():
    """Shifted scenario period keys are remapped so actuals and PY align."""
    from precis_mcp.engine.retriever import (
        DataQuery, ExecutionPlan, DimensionKey, RawResults, retrieve,
    )

    # Simulate ClickHouse returning data at the shifted periods
    fake_ch_rows = {
        'actuals': [
            {'period': '2025-01', 'revenue': 100.0},
            {'period': '2025-02', 'revenue': 110.0},
        ],
        'prior_year': [
            {'period': '2024-01', 'revenue': 90.0},
            {'period': '2024-02', 'revenue': 95.0},
        ],
    }

    class FakeCH:
        def __init__(self):
            self._call_count = 0

        def query(self, sql, parameters=None):
            # Determine which scenario based on period_start param
            scenario = 'actuals' if parameters.get('period_start') == '2025-01' else 'prior_year'
            rows = fake_ch_rows[scenario]

            class Result:
                column_names = list(rows[0].keys())
                result_rows = [tuple(r.values()) for r in rows]
            return Result()

    plan = ExecutionPlan(
        data_queries=[
            DataQuery(
                scenario_key='actuals',
                scenario_id='ACTUALS',
                period_start='2025-01',
                period_end='2025-02',
                metric_keys=['revenue'],
                time_offset=0,
            ),
            DataQuery(
                scenario_key='prior_year',
                scenario_id='ACTUALS',
                period_start='2024-01',
                period_end='2024-02',
                metric_keys=['revenue'],
                time_offset=-12,
            ),
        ],
        dimensions=['period'],
    )

    from precis_mcp.engine.catalogue import load_catalogue
    import os
    cat_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'instance', 'catalogue')
    cat = load_catalogue(cat_dir)

    results = retrieve(plan, cat, FakeCH())

    # Prior year keys should be remapped from 2024-xx to 2025-xx
    actuals_keys = set(results['actuals'].keys())
    py_keys = set(results['prior_year'].keys())

    assert ('2025-01',) in actuals_keys
    assert ('2025-02',) in actuals_keys
    assert ('2025-01',) in py_keys, f"Expected ('2025-01',) in PY keys, got {py_keys}"
    assert ('2025-02',) in py_keys, f"Expected ('2025-02',) in PY keys, got {py_keys}"

    # Values should be the original shifted data, just with remapped keys
    assert results['prior_year'][('2025-01',)]['revenue'] == 90.0
    assert results['prior_year'][('2025-02',)]['revenue'] == 95.0


# ---------------------------------------------------------------------------
# Test 5c: Quarter dimension remapped for shifted scenarios
# ---------------------------------------------------------------------------

def test_prior_year_quarter_remapped_in_retriever():
    """Shifted scenario quarter keys are remapped (2024-Q1 → 2025-Q1)."""
    from precis_mcp.engine.retriever import (
        DataQuery, ExecutionPlan, DimensionKey, RawResults, retrieve,
    )

    fake_ch_rows = {
        'actuals': [
            {'quarter': '2025-Q1', 'revenue': 100.0},
        ],
        'prior_year': [
            {'quarter': '2024-Q1', 'revenue': 90.0},
        ],
    }

    class FakeCH:
        def query(self, sql, parameters=None):
            scenario = 'actuals' if parameters.get('period_start') == '2025-01' else 'prior_year'
            rows = fake_ch_rows[scenario]

            class Result:
                column_names = list(rows[0].keys())
                result_rows = [tuple(r.values()) for r in rows]
            return Result()

    plan = ExecutionPlan(
        data_queries=[
            DataQuery(
                scenario_key='actuals',
                scenario_id='ACTUALS',
                period_start='2025-01',
                period_end='2025-03',
                metric_keys=['revenue'],
                time_offset=0,
            ),
            DataQuery(
                scenario_key='prior_year',
                scenario_id='ACTUALS',
                period_start='2024-01',
                period_end='2024-03',
                metric_keys=['revenue'],
                time_offset=-12,
            ),
        ],
        dimensions=['quarter'],
    )

    from precis_mcp.engine.catalogue import load_catalogue
    import os
    cat_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'instance', 'catalogue')
    cat = load_catalogue(cat_dir)

    results = retrieve(plan, cat, FakeCH())

    py_keys = set(results['prior_year'].keys())
    assert ('2025-Q1',) in py_keys, f"Expected ('2025-Q1',) in PY keys, got {py_keys}"
    assert results['prior_year'][('2025-Q1',)]['revenue'] == 90.0


# ---------------------------------------------------------------------------
# Test 5d: grains request → GROUPING SETS + grain-tagged result keys
# ---------------------------------------------------------------------------

def test_retrieve_grain_tags_grouping_sets_rows():
    """A grains request makes retrieve() emit GROUPING SETS, and the _grouping
    column decodes into ROLLED_UP-tagged keys so the grand total stays distinct
    from any detail row."""
    from precis_mcp.engine.retriever import DataQuery, ExecutionPlan, retrieve
    from precis_mcp.engine.resolver import GrainSpec
    from precis_mcp.engine.types import ROLLED_UP
    from precis_mcp.engine.catalogue import load_catalogue
    import os

    # _grouping: 0 = detail (cost_centre live); 1 = cost_centre rolled up (grand total)
    rows = [
        {'cost_centre': 'CC-SE', 'revenue': 5_000_000.0, '_grouping': 0},
        {'cost_centre': 'CC-DA', 'revenue': 3_200_000.0, '_grouping': 0},
        {'cost_centre': '', 'revenue': 8_200_000.0, '_grouping': 1},
    ]
    captured: dict = {}

    class FakeCH:
        def query(self, sql, parameters=None):
            captured['sql'] = sql

            class Result:
                column_names = list(rows[0].keys())
                result_rows = [tuple(r.values()) for r in rows]
            return Result()

    plan = ExecutionPlan(
        data_queries=[DataQuery(
            scenario_key='actuals', scenario_id='ACTUALS',
            period_start='2025-01', period_end='2025-12', metric_keys=['revenue'],
        )],
        dimensions=['cost_centre'],
        grains=GrainSpec(detail=True, grand_total=True),
    )

    cat_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'instance', 'catalogue')
    results = retrieve(plan, load_catalogue(cat_dir), FakeCH())

    keys = set(results['actuals'].keys())
    assert ('CC-SE',) in keys
    assert ('CC-DA',) in keys
    assert (ROLLED_UP,) in keys  # grand total tagged distinctly, not ('',)
    assert results['actuals'][(ROLLED_UP,)]['revenue'] == 8_200_000.0
    assert 'GROUPING SETS' in captured['sql']  # grains drove GROUPING SETS generation


# ---------------------------------------------------------------------------
# Test 6: ResolverError propagated from execute_report
# ---------------------------------------------------------------------------

def test_resolver_error_propagated(catalogue, scenario_registry):
    """Missing period_start must raise ResolverError from execute_report."""
    request = {
        'context': {'period_end': '2025-12'},  # missing period_start
        'blocks': [{'model': 'statement:pnl', 'scenario': 'actuals'}],
    }
    with pytest.raises(ResolverError):
        execute_report(request, catalogue, ch_client=None, scenario_registry=scenario_registry)


# ---------------------------------------------------------------------------
# Test 7: Single metric request
# ---------------------------------------------------------------------------

def test_single_metric_request(catalogue, scenario_registry):
    """metric:revenue request returns a single-metric result row."""
    mock_results = _make_mock_results('actuals', {'revenue': 500_000.0})

    request = {
        'context': {'period_start': '2025-01', 'period_end': '2025-12'},
        'blocks': [{'model': 'metric:revenue', 'scenario': 'actuals', 'alias': 'Actuals'}],
    }

    with patch('precis_mcp.engine.retriever.retrieve', return_value=mock_results):
        response = execute_report(request, catalogue, ch_client=MagicMock(), scenario_registry=scenario_registry)

    assert 'rows' in response
    # Aggregate no-dimensions mode: rows contain metric rows
    revenue_row = next(
        (r for r in response['rows'] if r['item']['key'] == 'revenue'),
        None,
    )
    assert revenue_row is not None
    assert revenue_row['values']['Actuals'] == pytest.approx(500_000.0, abs=1.0)


# ---------------------------------------------------------------------------
# Test 7b: Multi-metric multi-scenario aggregate — regression for the
# silent-drop bug where N×M block fan-out caused the formatter to emit only
# blocks[0].display_items as canonical rows.
# ---------------------------------------------------------------------------

def test_multi_metric_multi_scenario_aggregate(catalogue, scenario_registry):
    """metrics:revenue,billable_hours × [actuals, prior_year] returns BOTH
    metrics for BOTH scenarios, with one column per scenario alias."""
    # Use integer hour values to avoid default-decimals rounding noise — the
    # point of this test is row/column structure, not number formatting.
    mock_results = {
        'actuals': {(): {'revenue': 628_700.0, 'billable_hours': 13_148.0}},
        'prior_year': {(): {'revenue': 944_100.0, 'billable_hours': 13_401.0}},
    }

    request = {
        'context': {'period_start': '2026-01', 'period_end': '2026-05'},
        'blocks': [
            {'model': 'metrics:revenue,billable_hours',
             'scenario': 'actuals', 'alias': 'Actuals'},
            {'model': 'metrics:revenue,billable_hours',
             'scenario': 'prior_year', 'alias': 'PY'},
        ],
    }

    with patch('precis_mcp.engine.retriever.retrieve', return_value=mock_results):
        response = execute_report(request, catalogue, ch_client=MagicMock(), scenario_registry=scenario_registry)

    # A multi-metric 'metrics:' request is NOT a statement — it must classify as
    # kind='metric' so the table layout pivots metrics into columns. (Regression:
    # the old `is_statement = len(metric_keys) > 1` heuristic misrouted these to
    # the statement crosstab.)
    assert response['kind'] == 'metric'

    # One column per scenario alias — not N×M.
    assert [s['alias'] for s in response['scenarios']] == ['Actuals', 'PY']

    # Both metric rows emitted (this is the formatter behavior the bug broke).
    keys = {r['item']['key'] for r in response['rows']}
    assert keys == {'revenue', 'billable_hours'}

    # Each row carries values for both scenario aliases.
    for r in response['rows']:
        assert set(r['values'].keys()) == {'Actuals', 'PY'}

    revenue = next(r for r in response['rows'] if r['item']['key'] == 'revenue')
    hours = next(r for r in response['rows'] if r['item']['key'] == 'billable_hours')
    assert revenue['values']['Actuals'] == pytest.approx(628_700.0, abs=1.0)
    assert revenue['values']['PY'] == pytest.approx(944_100.0, abs=1.0)
    assert hours['values']['Actuals'] == pytest.approx(13_148.0, abs=1.0)
    assert hours['values']['PY'] == pytest.approx(13_401.0, abs=1.0)


# ---------------------------------------------------------------------------
# Test 8: Cost centre dimension — response uses unified rows
# ---------------------------------------------------------------------------

def test_cost_centre_dimension(catalogue, scenario_registry):
    """When dimensions=['cost_centre'] and statement model, response uses flat rows."""
    mock_results = {
        'actuals': {
            ('CC-01',): {'revenue': 300_000.0, 'direct_cost': 180_000.0,
                         'indirect_cost': 30_000.0, 'sga': 15_000.0,
                         'billable_hours': 5_000.0, 'total_hours': 7_000.0,
                         'avg_fte_billable': 20.0, 'avg_fte_overhead': 8.0,
                         'closing_fte_billable': 21.0, 'closing_fte_overhead': 9.0},
            ('CC-02',): {'revenue': 700_000.0, 'direct_cost': 420_000.0,
                         'indirect_cost': 70_000.0, 'sga': 35_000.0,
                         'billable_hours': 10_000.0, 'total_hours': 13_000.0,
                         'avg_fte_billable': 30.0, 'avg_fte_overhead': 12.0,
                         'closing_fte_billable': 31.0, 'closing_fte_overhead': 13.0},
        }
    }

    request = {
        'context': {'period_start': '2025-01', 'period_end': '2025-12'},
        'dimensions': ['cost_centre'],
        'blocks': [{'model': 'statement:pnl', 'scenario': 'actuals', 'alias': 'Actuals'}],
    }

    with patch('precis_mcp.engine.retriever.retrieve', return_value=mock_results):
        response = execute_report(request, catalogue, ch_client=MagicMock(), scenario_registry=scenario_registry)

    assert response['kind'] == 'statement'
    assert {r['dimensions'].get('cost_centre') for r in response['rows']} == {'CC-01', 'CC-02'}


# ---------------------------------------------------------------------------
# Test 11-15: Dimension format wiring in orchestrator
#
# This class is kept intact (per the "same-class tests stay together" rule).
# Several of its sub-tests are pure catalogue lookups; the rest exercise the
# `_build_dim_formats` path against a mocked CH client and the full pipeline
# with a dim-lookup CH call. Class lives here because some tests genuinely
# exercise the CH client surface.
# ---------------------------------------------------------------------------

class TestDimFormatWiring:
    """Tests for _resolve_master_dimension, _build_dim_formats, and integration."""

    def test_resolve_master_dimension_direct(self, catalogue, scenario_registry):
        """Direct lookup in catalogue.dimensions works."""
        dim = _resolve_master_dimension(catalogue, 'cost_centre')
        assert dim is not None
        assert dim.key == 'cost_centre'
        assert dim.is_hierarchical

    def test_resolve_master_dimension_via_cube(self, catalogue, scenario_registry):
        """Lookup via CubeDimension.source works when key matches cube dim key."""
        # In our catalogue, pnl domain has cube dimension key='cost_centre'
        # which also happens to match the master dimension key directly.
        # This test verifies the fallback path through CubeDimension.
        dim = _resolve_master_dimension(catalogue, 'cost_centre')
        assert dim is not None
        assert dim.key_column == 'cost_centre'

    def test_resolve_master_dimension_unknown(self, catalogue, scenario_registry):
        """Unknown dimension returns None."""
        dim = _resolve_master_dimension(catalogue, 'nonexistent')
        assert dim is None

    def test_resolve_master_dimension_period(self, catalogue, scenario_registry):
        """'period' is now a catalogue dimension with hierarchy levels."""
        dim = _resolve_master_dimension(catalogue, 'period')
        assert dim is not None
        assert dim.key_column == 'period'

    def test_build_dim_formats_no_dimensions(self, catalogue, scenario_registry):
        """Empty dimensions list → None."""
        result = _build_dim_formats(catalogue, [], MagicMock())
        assert result is None

    def test_build_dim_formats_no_client(self, catalogue, scenario_registry):
        """ch_client=None → None (dry-run mode)."""
        result = _build_dim_formats(catalogue, ['cost_centre'], None)
        assert result is None

    def test_build_dim_formats_period_only(self, catalogue, scenario_registry):
        """dimensions=['period'] → None (period is virtual, no lookup needed)."""
        result = _build_dim_formats(catalogue, ['period'], MagicMock())
        assert result is None

    def test_build_dim_formats_with_mock_ch(self, catalogue, scenario_registry):
        """With a properly mocked CH client, builds DimensionFormat with lookup."""
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.column_names = ['cost_centre', 'cost_centre_name']
        mock_result.result_rows = [
            ('CC-CLOUD-01', 'Cloud - AWS Team'),
            ('CC-CLOUD-02', 'Cloud - Azure Team'),
            ('CC-DATA-01', 'Data Engineering'),
        ]
        mock_client.query.return_value = mock_result

        result = _build_dim_formats(catalogue, ['cost_centre'], mock_client)

        assert result is not None
        assert 'cost_centre' in result
        fmt = result['cost_centre']
        assert isinstance(fmt, DimensionFormat)
        assert fmt.display_attr == 'name'
        assert fmt.lookup['CC-CLOUD-01']['name'] == 'Cloud - AWS Team'
        assert fmt.lookup['CC-DATA-01']['name'] == 'Data Engineering'

        # Verify the SQL query was issued against the correct source table
        mock_client.query.assert_called_once()
        sql_arg = mock_client.query.call_args[0][0]
        assert 'semantic.dim_cost_centre' in sql_arg
        assert 'cost_centre' in sql_arg
        assert 'cost_centre_name' in sql_arg

    def test_build_dim_formats_ch_error_graceful(self, catalogue, scenario_registry):
        """If CH query fails, _build_dim_formats returns None (graceful fallback)."""
        mock_client = MagicMock()
        mock_client.query.side_effect = Exception("Connection refused")

        result = _build_dim_formats(catalogue, ['cost_centre'], mock_client)
        assert result is None

    def test_end_to_end_dim_format_passed_to_formatter(self, catalogue, scenario_registry):
        """Full pipeline: dim_formats built from CH lookup reaches formatter output."""
        mock_results = {
            'actuals': {
                ('CC-01',): {'revenue': 300_000.0, 'direct_cost': 180_000.0,
                             'indirect_cost': 30_000.0, 'sga': 15_000.0,
                             'billable_hours': 5_000.0, 'total_hours': 7_000.0,
                             'avg_fte_billable': 20.0, 'avg_fte_overhead': 8.0,
                             'closing_fte_billable': 21.0, 'closing_fte_overhead': 9.0},
            }
        }

        # Mock both retriever and the CH client's query for dimension lookup
        mock_client = MagicMock()
        mock_ch_result = MagicMock()
        mock_ch_result.column_names = ['cost_centre', 'cost_centre_name']
        mock_ch_result.result_rows = [('CC-01', 'Cloud - AWS Team')]
        mock_client.query.return_value = mock_ch_result

        request = {
            'context': {'period_start': '2025-01', 'period_end': '2025-12'},
            'dimensions': ['cost_centre'],
            'blocks': [{'model': 'statement:pnl', 'scenario': 'actuals', 'alias': 'Actuals'}],
        }

        with patch('precis_mcp.engine.retriever.retrieve', return_value=mock_results):
            response = execute_report(request, catalogue, ch_client=mock_client, scenario_registry=scenario_registry)

        assert response['kind'] == 'statement'
        # The dimension value should be the display name, not the raw code
        dim_vals = {r['dimensions'].get('cost_centre') for r in response['rows']}
        assert 'Cloud - AWS Team' in dim_vals
