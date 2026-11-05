# SPDX-License-Identifier: Elastic-2.0
"""Adapter around the existing CH factory; existing runtime callers are unchanged."""
from __future__ import annotations

from collections.abc import Mapping
from threading import RLock
from .sql import CLICKHOUSE_SQL

from .contracts import (ClientClosedError, ConfigurationError, ExecutionError,
                        ManagedClient, Parameters, QueryResult, StatementError)


class ClickHouseClient(ManagedClient):
    dialect = CLICKHOUSE_SQL

    def __init__(self, *, execution=None, connection_options=None):
        self._execution = execution
        self._connection_options = connection_options or {}
        self._client = None
        self._closed = False
        self._lock = RLock()

    def query(self, sql: str, parameters: Parameters = None) -> QueryResult:
        if parameters is not None and not isinstance(parameters, Mapping):
            raise ConfigurationError("ClickHouse requires named typed parameters.")
        if self._execution is not None:
            return self._execution.query(sql, parameters)
        with self._lock:
            if self._closed:
                raise ClientClosedError("Analytical client is closed.")
            try:
                if self._client is None:
                    from precis_mcp.db import get_clickhouse_client
                    from . import selected_backend
                    self._client = (get_clickhouse_client(_explicit_provider=True, **self._connection_options)
                                    if selected_backend() != "clickhouse" else
                                    get_clickhouse_client(**self._connection_options))
                raw = self._client.query(sql, parameters=parameters or {})
                return QueryResult(list(raw.column_names), [tuple(r) for r in raw.result_rows],
                                   [str(getattr(t, "name", t)) for t in raw.column_types],
                                   getattr(raw, "query_id", None))
            except Exception:
                self._discard()
                raise ExecutionError("ClickHouse query failed; no automatic replay was performed.") from None

    def _discard(self):
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def execute(self, sql: str, parameters: Parameters = None, *, settings=None) -> QueryResult:
        """Execute trusted command SQL; completion settings belong to its caller."""
        if parameters is not None and not isinstance(parameters, Mapping):
            raise ConfigurationError("ClickHouse requires named typed parameters.")
        if self._execution is not None:
            return self._execution.execute(sql, parameters, settings=settings)
        with self._lock:
            if self._closed:
                raise ClientClosedError("Analytical client is closed.")
            if self._client is None:
                try:
                    from precis_mcp.db import get_clickhouse_client
                    self._client = get_clickhouse_client(_explicit_provider=True, **self._connection_options)
                except Exception:
                    raise StatementError("not_submitted") from None
            try:
                self._client.command(sql, parameters=parameters or {}, settings=settings)
                return QueryResult([], [], [])
            except Exception:
                self._discard()
                # Even an ALTER error can mean a registered mutation is still
                # running (e.g. mutations_sync wait timeout). Do not guess.
                raise StatementError() from None

    def close(self):
        if self._execution is not None:
            self._execution.close()
        with self._lock:
            self._closed = True
            self._discard()

    def cancel(self):
        return self._execution.cancel() if self._execution is not None else False

    @property
    def supports_parallel_queries(self):
        return self._execution is not None
