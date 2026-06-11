# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Tests for PostgresLoadHistoryWriter.

The writer's only external dependency is `precis_mcp.db` (Postgres). Tests
monkeypatch the two free functions it calls so we don't touch a real DB.
"""

from __future__ import annotations

import json

import pytest

from precis_mcp.ingestion.load_history import PostgresLoadHistoryWriter


@pytest.fixture
def fake_db(monkeypatch):
    """Replace precis_mcp.db.execute_platform and query_platform with stubs."""
    executions: list[tuple[str, tuple]] = []
    queries: list[tuple[str, tuple]] = []

    # query_platform's canned response: by default no in-flight row.
    query_response: list[list[dict]] = [[]]

    def fake_execute(sql: str, params: tuple | list | None = None):
        executions.append((sql, tuple(params or ())))
        return None

    def fake_query(sql: str, params: tuple | list | None = None):
        queries.append((sql, tuple(params or ())))
        return query_response[0]

    monkeypatch.setattr("precis_mcp.ingestion.load_history.db.execute_platform", fake_execute)
    monkeypatch.setattr("precis_mcp.ingestion.load_history.db.query_platform", fake_query)

    class Handle:
        def __init__(self):
            self.executions = executions
            self.queries = queries

        def set_running_for_binding(self, binding_id: str, load_id: str) -> None:
            query_response[0] = [{"load_id": load_id}]

    return Handle()


def test_insert_attempt_writes_running_row(fake_db):
    w = PostgresLoadHistoryWriter()
    w.insert_attempt(
        load_id="L1",
        binding_id="b1",
        source_id="s1",
        dataset_id="d1",
        period="2026-04",
        scenario_id="ACTUALS",
        triggered_by="admin:t",
    )
    assert len(fake_db.executions) == 1
    sql, params = fake_db.executions[0]
    assert "INSERT INTO load_history" in sql
    assert "'running'" in sql
    assert params == ("L1", "b1", "s1", "d1", "2026-04", "ACTUALS", "admin:t", None)


def test_record_extract_writes_rows_and_manifest(fake_db):
    w = PostgresLoadHistoryWriter()
    manifest = {"row_count": 100, "total_amount": 12345.67}
    w.record_extract("L1", 100, manifest)
    sql, params = fake_db.executions[0]
    assert "UPDATE load_history" in sql
    assert "rows_landed" in sql
    assert "source_manifest" in sql
    assert params[0] == 100
    # Manifest is JSON-encoded.
    assert json.loads(params[1]) == manifest
    assert params[2] == "L1"


def test_record_swap_stamps_timestamp(fake_db):
    w = PostgresLoadHistoryWriter()
    w.record_swap("L1")
    sql, params = fake_db.executions[0]
    assert "swap_committed_at = now()" in sql
    assert params == ("L1",)


def test_record_dbt_refresh_stamps_timestamp(fake_db):
    w = PostgresLoadHistoryWriter()
    w.record_dbt_refresh("L1")
    sql, params = fake_db.executions[0]
    assert "dbt_refreshed_at = now()" in sql
    assert params == ("L1",)


def test_finalise_writes_terminal_status(fake_db):
    w = PostgresLoadHistoryWriter()
    w.finalise("L1", "success", error_message=None)
    sql, params = fake_db.executions[0]
    assert "status = %s" in sql
    assert "finished_at = now()" in sql
    assert "duration_ms" in sql
    assert params == ("success", None, "L1")


def test_finalise_writes_error_message(fake_db):
    w = PostgresLoadHistoryWriter()
    w.finalise("L1", "failed_extract", error_message="api down")
    _sql, params = fake_db.executions[0]
    assert params[0] == "failed_extract"
    assert params[1] == "api down"


def test_find_running_returns_none_when_no_match(fake_db):
    w = PostgresLoadHistoryWriter()
    assert w.find_running_for_binding("b1") is None


def test_find_running_returns_load_id(fake_db):
    fake_db.set_running_for_binding("b1", "L99")
    w = PostgresLoadHistoryWriter()
    assert w.find_running_for_binding("b1") == "L99"
