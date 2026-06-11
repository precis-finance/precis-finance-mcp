# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Postgres session-level advisory lock — the ingestion lock backend (ADR-0006).

One dedicated connection per lock instance, held for the lock's lifetime: the
connection *is* the lock. `pg_try_advisory_lock` is polled with backoff up to
``blocking_timeout``; release is an explicit `pg_advisory_unlock` plus
connection close, and a crashed holder's lock is released by the server when
its connection dies. There is deliberately no TTL — a Redis-style expiry can
release a lock under a still-running holder mid-swap, while "held until
release or connection death" cannot.

The string key is hashed server-side (`hashtext(...)::bigint`), matching the
platform's existing advisory-lock idiom. Distinct keys can in principle
collide in the bigint keyspace; the failure mode is harmless extra
serialisation, never lost mutual exclusion.

Caveat for operators: session advisory locks require a direct connection to
Postgres — a transaction-pooling proxy (PgBouncer in transaction mode) breaks
them silently. Both shipped deployment shapes connect directly.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 0.1


def _default_connect() -> Any:
    """A dedicated autocommit connection to the platform DB.

    Autocommit matters: without it the polling SELECTs would open a
    transaction that stays idle-in-transaction for the whole load.
    """
    from precis_mcp.db import get_platform_db

    conn = get_platform_db()
    conn.autocommit = True
    return conn


class PgAdvisoryLock:
    """Duck-types the subset of the redis-py lock the orchestrator uses:
    ``acquire(blocking=True) -> bool`` and ``release()``."""

    def __init__(
        self,
        key: str,
        *,
        blocking_timeout: float = 1.0,
        connect: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._key = key
        self._blocking_timeout = blocking_timeout
        self._connect = connect or _default_connect
        self._conn: Any = None

    def acquire(self, blocking: bool = True) -> bool:
        if self._conn is not None:
            raise RuntimeError(f"PgAdvisoryLock {self._key!r} already acquired")
        conn = self._connect()
        try:
            deadline = time.monotonic() + (self._blocking_timeout if blocking else 0.0)
            while True:
                row = conn.execute(
                    "SELECT pg_try_advisory_lock(hashtext(%s)::bigint) AS acquired",
                    (self._key,),
                ).fetchone()
                if row and row["acquired"]:
                    self._conn = conn
                    return True
                remaining = deadline - time.monotonic()
                if not blocking or remaining <= 0:
                    conn.close()
                    return False
                time.sleep(min(_POLL_INTERVAL_SECONDS, remaining))
        except Exception:
            conn.close()
            raise

    def release(self) -> None:
        if self._conn is None:
            return
        conn, self._conn = self._conn, None
        try:
            row = conn.execute(
                "SELECT pg_advisory_unlock(hashtext(%s)::bigint) AS released",
                (self._key,),
            ).fetchone()
            if not (row and row["released"]):
                logger.warning(
                    "pg advisory lock %r was not held at release", self._key
                )
        finally:
            # Closing the connection releases the lock server-side even if
            # the explicit unlock failed.
            conn.close()
