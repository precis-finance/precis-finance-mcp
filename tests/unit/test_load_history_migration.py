# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Unit tests for the load_history schema (scripts/migrations/open/001_init.sql).

These tests parse the open init SQL as text and assert the committed
`load_history` schema (the source of truth) — table name, columns, constraints,
indexes. They run without any database.

Real-Postgres apply/reapply tests live in
`tests/e2e/test_load_history_migration_integration.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts" / "migrations" / "open" / "001_init.sql"
)


@pytest.fixture(scope="module")
def migration_sql() -> str:
    assert MIGRATION_PATH.exists(), f"Migration file missing: {MIGRATION_PATH}"
    return MIGRATION_PATH.read_text(encoding="utf-8")


def test_migration_file_exists():
    assert MIGRATION_PATH.exists()
    assert MIGRATION_PATH.is_file()


def test_creates_load_history_table(migration_sql: str):
    """The migration creates the load_history table idempotently."""
    assert re.search(
        r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+load_history",
        migration_sql,
        re.IGNORECASE,
    ), "Expected idempotent CREATE TABLE load_history"


@pytest.mark.parametrize(
    "column,expected_type_fragment",
    [
        ("load_id", "TEXT"),
        ("binding_id", "TEXT"),
        ("source_id", "TEXT"),
        ("dataset_id", "TEXT"),
        ("period", "TEXT"),
        ("scenario_id", "TEXT"),
        ("status", "TEXT"),
        ("triggered_by", "TEXT"),
        ("rows_landed", "BIGINT"),
        ("source_manifest", "JSONB"),
        ("control_total_result", "JSONB"),
        ("started_at", "TIMESTAMPTZ"),
        ("finished_at", "TIMESTAMPTZ"),
        ("duration_ms", "INTEGER"),
        ("swap_committed_at", "TIMESTAMPTZ"),
        ("dbt_refreshed_at", "TIMESTAMPTZ"),
        ("error_message", "TEXT"),
        ("notes", "TEXT"),
    ],
)
def test_column_present_with_type(
    migration_sql: str, column: str, expected_type_fragment: str
):
    """Each expected column declares the expected type."""
    pattern = rf"\b{re.escape(column)}\s+{re.escape(expected_type_fragment)}\b"
    assert re.search(pattern, migration_sql, re.IGNORECASE), (
        f"Column {column!r} not declared with type {expected_type_fragment!r}"
    )


def test_load_id_is_primary_key(migration_sql: str):
    assert re.search(
        r"load_id\s+TEXT\s+PRIMARY\s+KEY", migration_sql, re.IGNORECASE
    ), "load_id must be the primary key"


@pytest.mark.parametrize(
    "column",
    [
        "binding_id",
        "source_id",
        "dataset_id",
        "period",
        "scenario_id",
        "status",
        "triggered_by",
    ],
)
def test_required_identifier_columns_are_not_null(
    migration_sql: str, column: str
):
    pattern = rf"\b{re.escape(column)}\s+TEXT\s+NOT\s+NULL"
    assert re.search(pattern, migration_sql, re.IGNORECASE), (
        f"Column {column!r} must be NOT NULL"
    )


def test_status_check_constraint_lists_all_terminal_states(migration_sql: str):
    """status must be constrained to the documented set."""
    assert "load_history_status_check" in migration_sql
    expected_states = [
        "running",
        "success",
        "failed_extract",
        "failed_recon",
        "failed_swap",
        "failed_dbt",
        "failed_validation",
        "failed_other",
        "failed_checks",
    ]
    for state in expected_states:
        assert f"'{state}'" in migration_sql, (
            f"Status {state!r} missing from CHECK constraint"
        )


def test_period_format_check_constraint(migration_sql: str):
    """The committed schema enforces the canonical period shape
    '^[0-9]{4}-.+$' (covers 'YYYY-MM' and adjustment-period forms). The
    original migration 025 enforced a stricter '^[0-9]{6}$'; migration 026
    relaxed it, and the squashed open init carries the final shape."""
    assert "load_history_period_format_check" in migration_sql
    assert re.search(
        r"period\s*~\s*'\^\[0-9\]\{4\}-\.\+\$'", migration_sql
    ), "period CHECK must enforce the canonical '^[0-9]{4}-.+$'"


def test_source_manifest_defaults_to_empty_object(migration_sql: str):
    assert re.search(
        r"source_manifest\s+JSONB\s+NOT\s+NULL\s+DEFAULT\s+'\{\}'::jsonb",
        migration_sql,
        re.IGNORECASE,
    )


def test_control_total_result_defaults_to_empty_object(migration_sql: str):
    assert re.search(
        r"control_total_result\s+JSONB\s+NOT\s+NULL\s+DEFAULT\s+'\{\}'::jsonb",
        migration_sql,
        re.IGNORECASE,
    )


def test_started_at_defaults_to_now(migration_sql: str):
    assert re.search(
        r"started_at\s+TIMESTAMPTZ\s+NOT\s+NULL\s+DEFAULT\s+now\(\)",
        migration_sql,
        re.IGNORECASE,
    )


@pytest.mark.parametrize(
    "index_name,columns",
    [
        ("idx_load_history_binding_period_started", ["binding_id", "period", "started_at"]),
        ("idx_load_history_status_started", ["status", "started_at"]),
        ("idx_load_history_dataset_started", ["dataset_id", "started_at"]),
    ],
)
def test_index_declared(migration_sql: str, index_name: str, columns: list[str]):
    """Each expected index is declared idempotently with the right columns."""
    pattern = rf"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+{re.escape(index_name)}"
    assert re.search(pattern, migration_sql, re.IGNORECASE), (
        f"Index {index_name} missing"
    )
    # Confirm the index references all expected columns somewhere in the body.
    # We can't easily parse the column list, but at minimum the columns must
    # appear in the file near the index name.
    idx_start = migration_sql.find(index_name)
    idx_end = migration_sql.find(";", idx_start)
    assert idx_start != -1 and idx_end != -1
    body = migration_sql[idx_start:idx_end]
    for col in columns:
        assert col in body, f"Index {index_name} should reference {col!r}"


def test_migration_filename_follows_convention():
    """Filename is NNN_snake_case.sql with three-digit numbering."""
    name = MIGRATION_PATH.name
    assert re.match(r"^\d{3}_[a-z][a-z0-9_]+\.sql$", name), name


def test_migration_is_idempotent_by_construction(migration_sql: str):
    """All DDL guards with IF NOT EXISTS so re-applying is a no-op."""
    # Count CREATE statements and ensure each has IF NOT EXISTS.
    creates = re.findall(r"CREATE\s+(TABLE|INDEX)\s+([A-Z\s]+)?", migration_sql, re.IGNORECASE)
    assert creates, "Expected at least one CREATE statement"
    # All CREATE statements should include IF NOT EXISTS.
    bare_creates = re.findall(
        r"CREATE\s+(?:TABLE|INDEX)\s+(?!IF\s+NOT\s+EXISTS)",
        migration_sql,
        re.IGNORECASE,
    )
    assert not bare_creates, (
        "All CREATE statements must use IF NOT EXISTS for idempotency"
    )
