# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Artifact quota queries and open schema upgrades against real PostgreSQL.

Opt in with PRECIS_TEST_PG=1 and the usual PG connection settings. The tests
never read or change application tables: search_path excludes public. Quota
tables are temporary; migrations use an isolated schema rolled back on teardown.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from tests.support.artifact_migrations import (
    apply_open_migration,
    connect,
    migration_cursor,
    seed_legacy_files,
)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        os.getenv("PRECIS_TEST_PG") != "1",
        reason="Set PRECIS_TEST_PG=1 with a reachable PostgreSQL to run artifact tests",
    ),
]


@pytest.fixture
def quota_cursor():
    conn = connect()
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


@pytest.mark.parametrize("already_applied_008", [False, True])
def test_legacy_upgrade_preserves_files_context_and_retention(
    migration_cursor, tmp_path, monkeypatch, already_applied_008,
):
    from precis_mcp.artifacts import registry
    from precis_mcp.auth import UserPermissions

    cur = migration_cursor
    blob, content, digest, file_ids, before = seed_legacy_files(cur, tmp_path)
    apply_open_migration(cur, "008_artifacts.sql")
    if already_applied_008:
        apply_open_migration(cur, "008_artifacts.sql")
    apply_open_migration(cur, "009_upgrade_legacy_artifacts.sql")

    cur.execute("SELECT * FROM files ORDER BY file_id")
    after = cur.fetchall()
    for old, new in zip(before, after, strict=True):
        for key, value in old.items():
            if key not in ("conversation_id", "workstream_id", "expires_at"):
                assert new[key] == value
        assert new["conversation_id"] == old["conversation_id"]
        assert new["workstream_id"] == old["workstream_id"]
        assert new["expires_at"] == old["expires_at"]
        assert new["version"] == 1 and new["provenance"] == {}

    def query(sql, params):
        cur.execute(sql, params)
        return cur.fetchall()

    monkeypatch.setattr(registry, "query_platform", query)
    upload = registry.get_file(file_ids[0], user_id="alice")
    assert upload is not None and upload["access"] == {}
    assert registry.get_file(file_ids[0], user_id="bob") is None
    # Unknown historical read grants remain inaccessible, including to admins.
    for row in after[1:]:
        with pytest.raises(FileNotFoundError):
            registry.authorize_file(row, UserPermissions(user_id="alice", is_admin=True))
    assert Path(upload["storage_path"]).read_bytes() == content
    assert hashlib.sha256(blob.read_bytes()).hexdigest() == digest
    apply_open_migration(cur, "009_upgrade_legacy_artifacts.sql")
    cur.execute("SELECT * FROM files ORDER BY file_id")
    assert cur.fetchall() == after


def test_forward_upgrade_leaves_current_artifacts_unchanged(migration_cursor):
    cur = migration_cursor
    apply_open_migration(cur, "008_artifacts.sql")
    cur.execute("""INSERT INTO files (
        file_id,user_id,basename,mime_type,size_bytes,sha256,storage_path,source,
        version,provenance,access
    ) VALUES ('current','alice','current.json','application/json',2,'digest',
              '/existing/blob','agent_tool',7,'{"tool":"export_dataset"}',
              '{"owner":"alice","grants":{"is_admin":true,"scenarios":{}}}')""")
    cur.execute("SELECT * FROM files")
    before = cur.fetchall()
    for _ in range(2):
        apply_open_migration(cur, "009_upgrade_legacy_artifacts.sql")
    cur.execute("SELECT * FROM files")
    assert cur.fetchall() == before
