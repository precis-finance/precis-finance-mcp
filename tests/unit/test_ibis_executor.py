# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Unit tests for `precis_mcp/ingestion/ibis_executor.py`.

Pure logic: period regex validation, kind × period contract,
`:period` and `${var_name}` substitution, then write-to-staging
through stubbed Ibis backend + CH client. Tests assert on captured
SQL strings and inserted DataFrames.
"""

from __future__ import annotations

import pandas as pd
import pytest

from precis_mcp.ingestion.ibis_executor import (
    ExecutorResult,
    IbisExecutorError,
    _render_source_query,
    _staging_table,
    _validate_period_args,
    execute,
)


# ---------------------------------------------------------------------------
# Inline stubs — unit tests can't import the real CH client or fake-redis.
# ---------------------------------------------------------------------------


class _StubCH:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.inserts: list[tuple[str, pd.DataFrame]] = []

    def command(self, sql: str) -> None:
        self.commands.append(sql)

    def insert_df(self, table: str, df: pd.DataFrame, **kwargs) -> None:
        self.inserts.append((table, df))


class _StubExpr:
    def __init__(self, df: pd.DataFrame) -> None:
        self._df = df

    def execute(self) -> pd.DataFrame:
        return self._df


class _StubIbis:
    def __init__(self, df: pd.DataFrame, *, exc: Exception | None = None) -> None:
        self.queries: list[str] = []
        self._df = df
        self._exc = exc

    def sql(self, q: str) -> _StubExpr:
        self.queries.append(q)
        if self._exc is not None:
            raise self._exc
        return _StubExpr(self._df)


def _execute_args(**overrides):
    """Common kwargs for execute(); overridable per test."""
    base = dict(
        binding_id="test__fact_gl",
        source_id="test_pg",
        kind="period",
        period="2026-04",
        target="live.fact_gl",
        extract_query="SELECT period FROM x WHERE period = :period",
        load_id="L1",
        ibis_backend=_StubIbis(pd.DataFrame({"period": ["2026-04"]})),
        ch_client=_StubCH(),
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Period × kind contract
# ---------------------------------------------------------------------------


def test_period_kind_requires_period():
    with pytest.raises(IbisExecutorError, match="kind='period'"):
        _validate_period_args("period", None)


def test_period_kind_rejects_bad_format():
    with pytest.raises(IbisExecutorError, match="does not match expected"):
        _validate_period_args("period", "2026/04")


def test_period_kind_accepts_calendar_month():
    _validate_period_args("period", "2026-04")  # no raise


def test_period_kind_accepts_thirteenth_period():
    _validate_period_args("period", "2026-13")


def test_period_kind_accepts_adjustment_period():
    _validate_period_args("period", "2026-12-ADJ")


def test_snapshot_kind_rejects_period():
    with pytest.raises(IbisExecutorError, match="kind='snapshot'"):
        _validate_period_args("snapshot", "2026-04")


def test_unknown_kind_rejected():
    with pytest.raises(IbisExecutorError, match="must be 'period' or 'snapshot'"):
        _validate_period_args("monthly", "2026-04")


# ---------------------------------------------------------------------------
# Staging-table derivation
# ---------------------------------------------------------------------------


def test_staging_table_derived_from_live_prefix_swap():
    assert _staging_table("live.fact_gl") == "staging.fact_gl"


def test_staging_table_rejects_target_without_live_prefix():
    with pytest.raises(IbisExecutorError, match="must start with 'live.'"):
        _staging_table("warehouse.fact_gl")


# ---------------------------------------------------------------------------
# Query rendering — :period + ${var}
# ---------------------------------------------------------------------------


def test_render_source_query_substitutes_period_as_quoted_literal():
    rendered = _render_source_query("SELECT x WHERE p = :period", "2026-04", None)
    assert rendered == "SELECT x WHERE p = '2026-04'"


def test_render_source_query_passes_through_when_period_is_none():
    rendered = _render_source_query("SELECT x FROM y", None, None)
    assert rendered == "SELECT x FROM y"


def test_render_source_query_substitutes_template_vars():
    rendered = _render_source_query(
        "SELECT * FROM read_csv('${source_path}/file.csv')",
        None,
        {"source_path": "/var/lib/precis/uploads/crm"},
    )
    assert rendered == (
        "SELECT * FROM read_csv('/var/lib/precis/uploads/crm/file.csv')"
    )


def test_render_source_query_combines_period_and_template_vars():
    rendered = _render_source_query(
        "SELECT * FROM read_csv('${source_path}/x.csv') WHERE p = :period",
        "2026-04",
        {"source_path": "/data"},
    )
    assert rendered == (
        "SELECT * FROM read_csv('/data/x.csv') WHERE p = '2026-04'"
    )


# ---------------------------------------------------------------------------
# execute() — write path through Ibis + CH
# ---------------------------------------------------------------------------


def test_execute_returns_rows_landed_count():
    ibis = _StubIbis(pd.DataFrame({"period": ["2026-04"] * 5}))
    result = execute(**_execute_args(ibis_backend=ibis))
    assert isinstance(result, ExecutorResult)
    assert result.rows_landed == 5


def test_execute_period_drops_partition_before_insert():
    """Idempotency: the executor clears the period's partition on
    staging before inserting, so retries replace in place."""
    ch = _StubCH()
    execute(**_execute_args(ch_client=ch))
    assert any(
        "DROP PARTITION '2026-04'" in c and "staging.fact_gl" in c
        for c in ch.commands
    )


def test_execute_snapshot_truncates_staging_before_insert():
    ch = _StubCH()
    args = _execute_args(
        kind="snapshot",
        period=None,
        extract_query="SELECT 1",
        ch_client=ch,
    )
    execute(**args)
    assert any(
        "TRUNCATE TABLE IF EXISTS staging.fact_gl" in c for c in ch.commands
    )


def test_execute_injects_load_id_into_inserted_dataframe():
    df = pd.DataFrame({"period": ["2026-04"], "amount": [10.0]})
    ibis = _StubIbis(df)
    ch = _StubCH()
    execute(**_execute_args(ibis_backend=ibis, ch_client=ch, load_id="L42"))
    assert len(ch.inserts) == 1
    _table, inserted = ch.inserts[0]
    assert "_load_id" in inserted.columns
    assert (inserted["_load_id"] == "L42").all()


def test_execute_empty_result_is_valid():
    """An empty source result should not raise — the executor logs a
    warning but returns rows_landed=0. Validate decides whether zero
    rows is a load fail."""
    ibis = _StubIbis(pd.DataFrame())
    ch = _StubCH()
    result = execute(**_execute_args(ibis_backend=ibis, ch_client=ch))
    assert result.rows_landed == 0
    # No insert when result is empty.
    assert ch.inserts == []


def test_execute_wraps_ibis_error_as_executor_error():
    ibis = _StubIbis(pd.DataFrame(), exc=RuntimeError("connection refused"))
    with pytest.raises(IbisExecutorError, match="Source query execution failed"):
        execute(**_execute_args(ibis_backend=ibis))
