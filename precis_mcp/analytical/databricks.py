# SPDX-License-Identifier: Elastic-2.0
"""Native SQL connector. No Ibis, financial logic or application write API."""
from __future__ import annotations

from contextlib import suppress
import os
import logging
import re
import traceback
from threading import RLock, Lock

from .config import DatabricksConfig
from .sql import SqlDialect
from .contracts import (ClientClosedError, ConfigurationError, DependencyError, ExecutionError,
                        ManagedClient, Parameters, QueryResult, ReadContractError, StatementError)


class _CloudFetchLogFilter(logging.Filter):
    """Connector warnings can contain bearer-like signed result-download URLs."""

    _url = re.compile(r"https?://[^\s\"'<>]+|(?<![\w/])/[^\s\"'<>]*\?[^\s\"'<>]+", re.IGNORECASE)

    def filter(self, record):
        record.msg = self._url.sub("[redacted download URL]", record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = self._url.sub(
                "[redacted download URL]", "".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = self._url.sub("[redacted download URL]", record.exc_text)
        if record.stack_info:
            record.stack_info = self._url.sub("[redacted download URL]", record.stack_info)
        return True


_download_log_filter = _CloudFetchLogFilter()


def _protect_download_diagnostics():
    # Parent logger filters do not run for propagated child records. Install on
    # the connector's emitting loggers, keeping useful throughput/error context.
    for name in ("databricks.sql.cloudfetch.downloader", "databricks.sql.cloudfetch.download_manager",
                 "databricks.sql.common.unified_http_client", "urllib3.connectionpool", "urllib3.connection"):
        logging.getLogger(name).addFilter(_download_log_filter)


def credential_provider(config: DatabricksConfig):
    """One lazy SDK configuration per pool; the SDK owns token renewal."""
    lock = Lock()
    credentials = None

    def provider():
        nonlocal credentials
        with lock:
            if credentials is None:
                from databricks.sdk.core import Config
                options = dict(host=config.host, auth_type=config.auth_type)
                if config.auth_type == "oauth-m2m":
                    options.update(client_id=config.client_id, client_secret=config.client_secret,
                                   config_file=os.devnull, scopes=["sql"])
                elif config.profile:
                    options["profile"] = config.profile
                credentials = Config(**options)
            return credentials.authenticate
    return provider


def _connect(config: DatabricksConfig, *, credentials_provider=None):
    _protect_download_diagnostics()
    try:
        from databricks import sql
        from databricks.sdk.core import Config  # noqa: F401 — check optional dependency
    except ImportError:
        raise DependencyError("Install precis-finance-mcp[databricks-primary] for the native client.") from None

    # Config is explicit: production never selects an implicit credential chain
    # or reads a user credential profile. The common secrets loader supplies
    # *_FILE values before this point. Do not log SDK/connector exceptions.
    try:
        authenticate = (credentials_provider or credential_provider(config))()
        return sql.connect(
            server_hostname=config.server_hostname,
            http_path=config.http_path,
            credentials_provider=lambda: authenticate,
            use_inline_params=False,
            session_configuration={"TIMEZONE": "UTC", "ANSI_MODE": "true",
                                   "STATEMENT_TIMEOUT": str(config.statement_timeout)},
            _socket_timeout=config.socket_timeout,
            _retry_stop_after_attempts_count=1,
            user_agent_entry="precis-finance-mcp",
        )
    except Exception:
        raise ExecutionError("Databricks authentication or connection failed; verify endpoint, auth settings and grants.") from None


class DatabricksClient(ManagedClient):
    """Own one lazy session; serialize calls, use a new cursor for every read.

    SQL is trusted compiler/operator input, not an agent-facing arbitrary SQL
    tool or a read-only SQL firewall. Basic execution has no planning policy.
    Session timeout limits execution, socket timeout limits individual RPCs;
    neither claims a complete question-to-report wall-clock deadline.
    """

    def __init__(self, config: DatabricksConfig, *, execution=None, credentials_provider=None):
        self.config = config
        self._execution = execution
        self._credentials_provider = credentials_provider
        self.dialect = SqlDialect("databricks", config.catalog, (
            ("semantic", config.semantic_schema), ("planning", config.planning_schema),
            ("live", config.actuals_schema), ("actuals", config.actuals_schema),
        ), column_type=self._column_type)
        self._column_types = {}
        self._connection = None
        self._closed = False
        self._lock = RLock()
        self._cursor_lock = Lock()
        self._active_cursor = None

    def _column_type(self, relation: str, column: str) -> tuple[str, bool]:
        """Cache actual warehouse types for this client, without an expected-schema file."""
        from .config import quote_identifier
        from .contracts import ConfigurationError
        with self._lock:
            if relation not in self._column_types:
                self.dialect.table(relation)  # validate the logical name/role
                role, table = relation.split(".")
                result = self.query(
                    f"SELECT column_name, full_data_type, is_nullable FROM {quote_identifier(self.config.catalog)}.information_schema.columns "
                    "WHERE table_schema = :schema AND table_name = :table",
                    {"schema":dict(self.dialect.schemas)[role], "table":table})
                if any(nullable not in {"YES", "NO"} for _, _, nullable in result.result_rows):
                    raise ConfigurationError("Warehouse column nullability is unavailable.")
                self._column_types[relation] = {
                    name:(native_type.upper(), nullable == "YES")
                    for name, native_type, nullable in result.result_rows}
            try:
                return self._column_types[relation][column]
            except KeyError:
                raise ConfigurationError(f"Required input column is missing or inaccessible: {relation}.{column}.") from None

    def query(self, sql: str, parameters: Parameters = None) -> QueryResult:
        if self._execution is not None:
            return self._execution.query(sql, parameters)
        return self._run(sql, parameters, fetch=True)

    def execute(self, sql: str, parameters: Parameters = None, *, settings=None) -> QueryResult:
        """Bound basic SQL execution; no planning mutation or recovery semantics."""
        if settings:
            raise ConfigurationError("Databricks execution does not accept CH statement settings.")
        if self._execution is not None:
            return self._execution.execute(sql, parameters)
        return self._run(sql, parameters, fetch=False)

    def _run(self, sql, parameters, *, fetch):
        with self._lock:
            if self._closed:
                raise ClientClosedError("Analytical client is closed.")
            if self._connection is None:
                try:
                    self._connection = (_connect(self.config) if self._credentials_provider is None else
                                        _connect(self.config, credentials_provider=self._credentials_provider))
                except Exception:
                    if not fetch:
                        raise StatementError("not_submitted") from None
                    raise
            cursor = None
            submitted = False
            try:
                cursor = self._connection.cursor()
                with self._cursor_lock:
                    self._active_cursor = cursor
                # Pass values unchanged; never render literals or translate CH SQL.
                submitted = True
                cursor.execute(sql, parameters=parameters)
                if not fetch:
                    # A normally returned execute can still carry CANCELLED.
                    # The synchronous Thrift connector exposes its final state
                    # on the result set, without another warehouse round trip.
                    state = getattr(getattr(getattr(cursor, "active_result_set", None), "status", None), "name", None)
                    if state != "SUCCEEDED":
                        raise StatementError("cancelled" if state == "CANCELLED" else "unknown",
                                             getattr(cursor, "query_id", None))
                description = cursor.description or []
                rows = [tuple(row) for row in cursor.fetchall()] if fetch else []
                result = QueryResult(
                    [col[0] for col in description], rows,
                    [str(col[1]) for col in description],
                    getattr(cursor, "query_id", None),
                )
            except Exception as exc:
                # Failed sessions are never silently replayed. A subsequent
                # explicit read may establish a fresh connection.
                statement_id = None
                if not fetch and cursor is not None:
                    with suppress(Exception):
                        statement_id = cursor.query_id
                self._discard_connection()
                if not fetch:
                    if isinstance(exc, StatementError):
                        raise exc from None
                    from databricks.sql.exc import ServerOperationError
                    outcome = ("not_submitted" if not submitted else
                               "failed" if isinstance(exc, ServerOperationError) else "unknown")
                    raise StatementError(outcome, statement_id) from None
                if "PRECIS_READ_CONTRACT_VIOLATION" in str(exc):
                    raise ReadContractError("Queried values violate the canonical non-null input contract.") from None
                raise ExecutionError("Databricks query failed; no automatic replay was performed.") from None
            finally:
                with self._cursor_lock:
                    self._active_cursor = None
                if cursor is not None:
                    try:
                        cursor.close()
                    except Exception:
                        self._discard_connection()
            return result

    def cancel(self) -> bool:
        """Best-effort cancellation of the active read, callable from another thread.

        True means a cancellation attempt against an active cursor, not proof
        of cancellation. Before a command ID exists, the native connector can
        ignore it; cancellation is not a terminality or write-admission guard.
        """
        if self._execution is not None:
            return self._execution.cancel()
        with self._cursor_lock:
            cursor = self._active_cursor
            if cursor is None:
                return False
            try:
                cursor.cancel()
            except Exception:
                raise ExecutionError("Databricks cancellation could not be confirmed.") from None
            return True

    def _discard_connection(self):
        connection, self._connection = self._connection, None
        self._column_types.clear()
        if connection is not None:
            with suppress(Exception):
                connection.close()

    def close(self):
        if self._execution is not None:
            self._execution.close()
        with self._lock:
            self._closed = True
            self._discard_connection()

    @property
    def supports_parallel_queries(self):
        return self._execution is not None
