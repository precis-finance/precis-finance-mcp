# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
# pyright: reportArgumentType=false, reportCallIssue=false, reportOptionalSubscript=false
"""E2E tests for the load_history schema (scripts/migrations/open/001_init.sql).

These tests apply the open init against a real Postgres and verify the
load_history schema landed correctly. Skipped unless the environment is
configured to reach a test Postgres (set ``PRECIS_TEST_PG=1``).

Structural / regex tests live in
`tests/unit/test_load_history_migration.py`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts" / "migrations" / "open" / "001_init.sql"
)


def _has_test_postgres() -> bool:
    """Heuristic for 'we can reach a Postgres safe to mutate'.

    Requires PRECIS_TEST_PG=1 to be explicit — we never want to apply DDL
    against a real Postgres just because PGHOST happens to be set.
    """
    return os.environ.get("PRECIS_TEST_PG") == "1"


@pytest.mark.slow
@pytest.mark.skipif(
    not _has_test_postgres(),
    reason="Set PRECIS_TEST_PG=1 with a disposable Postgres to run integration tests",
)
def test_migration_applies_and_schema_lands():
    """Apply 025 against a real Postgres, verify the table is queryable."""
    import psycopg

    conn = psycopg.connect(
        host=os.getenv("PGHOST", "localhost"),
        port=int(os.getenv("PGPORT", 5432)),
        user=os.getenv("PGUSER", "postgres"),
        password=os.getenv("PGPASSWORD", ""),
        dbname=os.getenv("PLATFORM_DB_NAME", "precis_platform_test"),
    )
    try:
        with conn.cursor() as cur:
            # Apply the migration in a savepoint so we can roll back cleanly.
            cur.execute("BEGIN")
            cur.execute(MIGRATION_PATH.read_text(encoding="utf-8"))

            # Verify the table exists with all expected columns.
            cur.execute(
                """
                SELECT column_name, data_type, is_nullable
                  FROM information_schema.columns
                 WHERE table_name = 'load_history'
                 ORDER BY ordinal_position
                """
            )
            columns = {row[0]: (row[1], row[2]) for row in cur.fetchall()}
            assert "load_id" in columns
            assert columns["load_id"][0] == "text"
            assert columns["binding_id"][1] == "NO"  # NOT NULL
            assert columns["finished_at"][1] == "YES"  # nullable

            # Verify indexes exist.
            cur.execute(
                """
                SELECT indexname FROM pg_indexes
                 WHERE tablename = 'load_history'
                """
            )
            index_names = {row[0] for row in cur.fetchall()}
            assert "idx_load_history_binding_period_started" in index_names
            assert "idx_load_history_status_started" in index_names
            assert "idx_load_history_dataset_started" in index_names

            # Verify CHECK constraint rejects bad period format. The canonical
            # shape is '^[0-9]{4}-.+$', so the old compact '202604' (no dash)
            # must be rejected — while 'YYYY-MM' is accepted.
            cur.execute(
                """
                INSERT INTO load_history
                  (load_id, binding_id, source_id, dataset_id, period,
                   scenario_id, status, triggered_by)
                VALUES ('test-1', 'b', 's', 'd', '202604', 'ACTUALS',
                        'running', 'admin:test')
                """
            )
        # We expect the above INSERT to have failed; if we reach here, the
        # CHECK didn't fire.
        pytest.fail("Expected period CHECK constraint to reject '202604'")
    except psycopg.errors.CheckViolation:
        pass  # Good — the CHECK rejected the bad value.
    finally:
        conn.rollback()
        conn.close()


@pytest.mark.slow
@pytest.mark.skipif(
    not _has_test_postgres(),
    reason="Set PRECIS_TEST_PG=1 with a disposable Postgres to run integration tests",
)
def test_migration_reapply_is_noop():
    """Applying the migration twice does not fail."""
    import psycopg

    conn = psycopg.connect(
        host=os.getenv("PGHOST", "localhost"),
        port=int(os.getenv("PGPORT", 5432)),
        user=os.getenv("PGUSER", "postgres"),
        password=os.getenv("PGPASSWORD", ""),
        dbname=os.getenv("PLATFORM_DB_NAME", "precis_platform_test"),
    )
    try:
        sql = MIGRATION_PATH.read_text(encoding="utf-8")
        with conn.cursor() as cur:
            cur.execute(sql)
            cur.execute(sql)  # Second apply — must be a no-op.
        conn.commit()
    finally:
        conn.close()
