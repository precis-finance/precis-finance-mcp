# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Isolated PostgreSQL fixtures and legacy shapes for artifact migration tests."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import uuid4

import pytest


def connect():
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(
        host=os.getenv("PGHOST", "localhost"),
        port=int(os.getenv("PGPORT", "5432")),
        user=os.getenv("PGUSER", "postgres"),
        password=os.getenv("PGPASSWORD", ""),
        dbname=os.getenv("PLATFORM_DB_NAME", "precis_platform_test"),
        row_factory=dict_row,
    )


@pytest.fixture
def migration_cursor():
    from psycopg import sql

    conn = connect()
    try:
        with conn.cursor() as cur:
            schema = sql.Identifier("artifact_test_" + uuid4().hex)
            cur.execute(sql.SQL("CREATE SCHEMA {}").format(schema))
            cur.execute(sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(schema))
            yield cur
    finally:
        conn.rollback()
        conn.close()


def apply_open_migration(cur, filename):
    path = Path(__file__).resolve().parents[2] / "scripts" / "migrations" / "open" / filename
    cur.execute(path.read_text())


def create_legacy_registry(cur):
    cur.execute("""CREATE TABLE files (
        file_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, basename TEXT NOT NULL,
        mime_type TEXT NOT NULL, size_bytes BIGINT NOT NULL, sha256 TEXT NOT NULL,
        storage_path TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), expires_at TIMESTAMPTZ,
        deleted_at TIMESTAMPTZ, conversation_id TEXT, workstream_id TEXT,
        source TEXT NOT NULL, producing_tool TEXT
    )""")


def seed_legacy_files(cur, tmp_path):
    """Create historical file rows and one real blob for preservation checks."""
    create_legacy_registry(cur)
    blob = tmp_path / "legacy-blob"
    content = b"preserved legacy bytes"
    blob.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    file_ids = ["file_" + letter * 26 for letter in "abcd"]
    for file_id, source, tool, workstream in zip(
        file_ids,
        ["user_upload", "agent_tool", "sandbox", "user_upload"],
        [None, "legacy-export", "legacy-transform", "legacy-transform"],
        [None, "workstream-1", None, None],
    ):
        cur.execute("""INSERT INTO files (
            file_id,user_id,basename,mime_type,size_bytes,sha256,storage_path,
            conversation_id,workstream_id,source,producing_tool,expires_at
        ) VALUES (%s,'alice','legacy.txt','text/plain',%s,%s,%s,
                  'conversation-1',%s,%s,%s,now()+interval '1 day')""",
            (file_id, len(content), digest, str(blob), workstream, source, tool),
        )
    cur.execute("UPDATE files SET expires_at = now()-interval '1 day' WHERE workstream_id IS NOT NULL")
    cur.execute("SELECT * FROM files ORDER BY file_id")
    return blob, content, digest, file_ids, cur.fetchall()
