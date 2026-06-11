# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Unit tests for precis_mcp/ingestion/pg_lock.py — poll/timeout/lifecycle
logic against an injected fake connection (the real-concurrency behaviour is
covered by tests/e2e/test_pg_advisory_lock_integration.py)."""
from __future__ import annotations

import logging

import pytest

from precis_mcp.ingestion.pg_lock import PgAdvisoryLock


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConn:
    """Grants `pg_try_advisory_lock` per the scripted `grants` sequence
    (last value repeats); `pg_advisory_unlock` returns `unlock_result`."""

    def __init__(self, grants=(True,), unlock_result=True):
        self.grants = list(grants)
        self.unlock_result = unlock_result
        self.closed = False
        self.executed: list[tuple[str, tuple | None]] = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if "pg_try_advisory_lock" in sql:
            grant = self.grants.pop(0) if len(self.grants) > 1 else self.grants[0]
            return _FakeCursor({"acquired": grant})
        if "pg_advisory_unlock" in sql:
            return _FakeCursor({"released": self.unlock_result})
        raise AssertionError(f"unexpected SQL: {sql}")

    def close(self):
        self.closed = True


def _lock(conn, **kwargs) -> PgAdvisoryLock:
    return PgAdvisoryLock("ingest_lock:b1", connect=lambda: conn, **kwargs)


def test_acquire_first_try_keeps_connection_open():
    conn = _FakeConn(grants=(True,))
    lock = _lock(conn)
    assert lock.acquire(blocking=True) is True
    assert conn.closed is False
    sql, params = conn.executed[0]
    assert "hashtext" in sql and params == ("ingest_lock:b1",)


def test_contended_acquire_times_out_and_closes_connection():
    conn = _FakeConn(grants=(False,))
    lock = _lock(conn, blocking_timeout=0.05)
    assert lock.acquire(blocking=True) is False
    assert conn.closed is True
    assert len(conn.executed) >= 1


def test_non_blocking_acquire_returns_immediately():
    conn = _FakeConn(grants=(False,))
    lock = _lock(conn, blocking_timeout=5.0)
    assert lock.acquire(blocking=False) is False
    assert conn.closed is True
    # No polling: exactly one try.
    assert len(conn.executed) == 1


def test_acquire_succeeds_within_timeout_after_polling():
    conn = _FakeConn(grants=(False, False, True))
    lock = _lock(conn, blocking_timeout=5.0)
    assert lock.acquire(blocking=True) is True
    assert conn.closed is False
    assert len(conn.executed) == 3


def test_release_unlocks_and_closes():
    conn = _FakeConn()
    lock = _lock(conn)
    lock.acquire()
    lock.release()
    assert conn.closed is True
    assert any("pg_advisory_unlock" in sql for sql, _ in conn.executed)
    # Idempotent: a second release is a no-op.
    lock.release()


def test_release_warns_but_closes_when_lock_not_held(caplog):
    conn = _FakeConn(unlock_result=False)
    lock = _lock(conn)
    lock.acquire()
    with caplog.at_level(logging.WARNING, logger="precis_mcp.ingestion.pg_lock"):
        lock.release()
    assert conn.closed is True
    assert any("not held" in r.message for r in caplog.records)


def test_double_acquire_raises():
    conn = _FakeConn()
    lock = _lock(conn)
    lock.acquire()
    with pytest.raises(RuntimeError, match="already acquired"):
        lock.acquire()


def test_acquire_error_closes_connection():
    class _BrokenConn(_FakeConn):
        def execute(self, sql, params=None):
            raise OSError("connection reset")

    conn = _BrokenConn()
    lock = _lock(conn)
    with pytest.raises(OSError):
        lock.acquire()
    assert conn.closed is True
