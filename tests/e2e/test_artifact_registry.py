# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Artifact quota queries against real PostgreSQL, using session-local tables.

Opt in with PRECIS_TEST_PG=1 and the usual PG connection settings. The tests
never read or change application tables: search_path excludes public, and all
fixture tables and rows are temporary and rolled back.
"""
from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        os.getenv("PRECIS_TEST_PG") != "1",
        reason="Set PRECIS_TEST_PG=1 with a reachable PostgreSQL to run quota tests",
    ),
]


@pytest.fixture
def quota_cursor():
    import psycopg
    from psycopg.rows import dict_row

    conn = psycopg.connect(
        host=os.getenv("PGHOST", "localhost"),
        port=int(os.getenv("PGPORT", "5432")),
        user=os.getenv("PGUSER", "postgres"),
        password=os.getenv("PGPASSWORD", ""),
        dbname=os.getenv("PLATFORM_DB_NAME", "precis_platform_test"),
        row_factory=dict_row,
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL search_path TO pg_temp")
            cur.execute("CREATE TEMP TABLE files (user_id TEXT, size_bytes BIGINT)")
            cur.execute("CREATE TEMP TABLE artifact_blob_gc (user_id TEXT, size_bytes BIGINT)")
            cur.execute("""CREATE TEMP TABLE artifact_uploads (
                upload_id TEXT, user_id TEXT, size_bytes BIGINT, state TEXT
            )""")
            cur.execute("INSERT INTO files VALUES ('alice', 40), ('bob', 900)")
            cur.execute("INSERT INTO artifact_blob_gc VALUES ('alice', 10), ('bob', 900)")
            cur.execute("""INSERT INTO artifact_uploads VALUES
                ('upload-a', 'alice', 30, 'uploaded'),
                ('upload-b', 'alice', 40, 'pending'),
                ('upload-done', 'alice', 200, 'complete'),
                ('upload-other-owner', 'bob', 900, 'pending')
            """)
            yield cur
    finally:
        conn.rollback()
        conn.close()


@pytest.mark.parametrize("exclude_upload, expected", [
    (None, 120),
    ("upload-a", 90),
    ("missing-upload", 120),
])
@pytest.mark.parametrize("use_transaction_cursor", [True, False], ids=["cursor", "query"])
def test_quota_accounts_for_nullable_upload_exclusion(
    quota_cursor, monkeypatch, exclude_upload, expected, use_transaction_cursor,
):
    from precis_mcp.artifacts import registry

    # PostgreSQL must parse and execute the actual registry SQL; shared fakes
    # cannot detect an untyped NULL placeholder in an IS NULL expression.
    def query(sql, params):
        quota_cursor.execute(sql, params)
        return quota_cursor.fetchall()

    monkeypatch.setattr(registry, "query_platform", query)
    cursor = quota_cursor if use_transaction_cursor else None
    assert registry.quota_used("alice", cursor, exclude_upload=exclude_upload) == expected
