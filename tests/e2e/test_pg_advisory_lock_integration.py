# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""E2E concurrency test for the Postgres advisory ingestion lock (ADR-0006).

The lock serialises the ClickHouse `live.*` swap, so it ships with a focused
test against a real Postgres rather than a blind swap — mutual exclusion,
hand-over on release, key independence, and the connection-death
auto-release that replaces the Redis TTL as the crash-recovery story.

Skipped unless ``PRECIS_TEST_PG=1`` (same explicit opt-in as the other
Postgres integration tests). Lock-only — no DDL, safe against any Postgres.
"""
from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("PRECIS_TEST_PG") != "1",
        reason="Set PRECIS_TEST_PG=1 with a reachable Postgres to run integration tests",
    ),
]


def _lock(key: str, blocking_timeout: float = 0.3):
    from precis_mcp.ingestion.pg_lock import PgAdvisoryLock

    return PgAdvisoryLock(key, blocking_timeout=blocking_timeout)


def test_mutual_exclusion_and_handover():
    a = _lock("e2e:ingest_lock:contended")
    b = _lock("e2e:ingest_lock:contended")
    assert a.acquire(blocking=True) is True
    try:
        # Held by A on a different session: B must time out, not deadlock.
        assert b.acquire(blocking=True) is False
    finally:
        a.release()
    # Hand-over after release.
    assert b.acquire(blocking=True) is True
    b.release()


def test_distinct_keys_do_not_contend():
    a = _lock("e2e:ingest_lock:binding_a")
    b = _lock("e2e:ingest_lock:binding_b")
    assert a.acquire() is True
    try:
        assert b.acquire() is True
        b.release()
    finally:
        a.release()


def test_connection_death_releases_lock():
    """The crash-recovery story: a dead holder's lock frees server-side."""
    a = _lock("e2e:ingest_lock:crash")
    assert a.acquire() is True
    # Simulate a crashed holder — drop the session without pg_advisory_unlock.
    a._conn.close()
    a._conn = None

    b = _lock("e2e:ingest_lock:crash", blocking_timeout=2.0)
    assert b.acquire(blocking=True) is True
    b.release()
