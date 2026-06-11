# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Unit tests for the metric-engine catalogue, resolver helpers, and pure
type-bridging functions.

Split from the legacy `tests/test_engine.py`. Component-level pipeline tests
that exercise `retrieve` against fake ClickHouse, or run `execute_report` with
patched retrievers, live in `tests/component/test_engine_pipeline.py`.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from precis_mcp.engine import (
    Catalogue, load_and_validate,
    _to_retriever_query, _to_formatter_block,
)
from precis_mcp.engine.formatter import FormatterBlock
from precis_mcp.engine.resolver import DataQuery as ResolverDataQuery
from precis_mcp.engine.retriever import DataQuery as RetrieverDataQuery
from precis_mcp.engine.resolver import ResolvedBlock
from precis_mcp.engine.scope_enforcer import ScopeViolationError, resolve_scope_filters
from precis_mcp.engine.catalogue import load_catalogue
from precis_mcp.auth import DimensionScope, ScopeSpec

import textwrap


CATALOGUE_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'instance', 'catalogue')


@pytest.fixture(scope="module")
def catalogue() -> Catalogue:
    return load_and_validate(CATALOGUE_DIR)


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
# Inline-federated scope rejection — pure scope enforcer check
# ---------------------------------------------------------------------------

def test_inline_federated_dimension_rejected_in_scope(tmp_path):
    """Source-only federated dimensions are not valid security scope axes."""
    cat = _write_inline_federated_catalogue(tmp_path)
    scope = ScopeSpec(
        dimensions=DimensionScope(allow={'supplier_id': ['SUP-001']})
    )

    with pytest.raises(ScopeViolationError, match="security scope"):
        resolve_scope_filters(
            scope,
            cat,
            MagicMock(),
            domain='gl_federated',
        )


# ---------------------------------------------------------------------------
# Type-bridging helpers (pure)
# ---------------------------------------------------------------------------

def test_type_bridging_to_retriever_query():
    """_to_retriever_query must produce correct RetrieverDataQuery."""
    rq = ResolverDataQuery(
        scenario_key='actuals',
        scenario_id='ACTUALS',
        period_start='2025-01',
        period_end='2025-12',
        metric_keys=['revenue', 'direct_cost'],
    )
    result = _to_retriever_query(rq)

    assert isinstance(result, RetrieverDataQuery)
    assert result.scenario_key == 'actuals'
    assert result.scenario_id == 'ACTUALS'
    assert result.period_start == '2025-01'
    assert result.period_end == '2025-12'
    assert result.metric_keys == ['revenue', 'direct_cost']
    assert result.domain == 'pnl'  # default


def test_type_bridging_to_formatter_block(catalogue):
    """_to_formatter_block must produce correct FormatterBlock."""
    rb = ResolvedBlock(
        alias='Actuals',
        scenario_key='actuals',
        metric_keys=['revenue', 'gross_margin'],
        display_items=['revenue', 'separator', 'gross_margin'],
        is_statement=True,
    )
    result = _to_formatter_block(rb, catalogue)

    assert isinstance(result, FormatterBlock)
    assert result.alias == 'Actuals'
    assert result.scenario_key == 'actuals'
    assert result.metric_keys == ['revenue', 'gross_margin']
    assert result.display_items == ['revenue', 'separator', 'gross_margin']
    # is_statement is now an explicit ResolvedBlock field (set only for
    # `statement:` models), not inferred from the metric count — it passes
    # through unchanged.
    assert result.is_statement is True


def test_type_bridging_single_metric_not_statement(catalogue):
    """Single metric block without separators → is_statement=False."""
    rb = ResolvedBlock(
        alias='Rev',
        scenario_key='actuals',
        metric_keys=['revenue'],
        display_items=['revenue'],
    )
    result = _to_formatter_block(rb, catalogue)
    assert result.is_statement is False


# ---------------------------------------------------------------------------
# load_and_validate convenience function (catalogue loader)
# ---------------------------------------------------------------------------

def test_load_and_validate():
    """load_and_validate must return a populated Catalogue without errors."""
    cat = load_and_validate(CATALOGUE_DIR)
    assert isinstance(cat, Catalogue)
    assert 'revenue' in cat.metrics
    assert cat.scenarios == {}
    assert 'pnl' in cat.statements
