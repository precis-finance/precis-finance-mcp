# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Shared client lifecycle and native connector contract using driver fakes."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from datetime import date, datetime, timezone
import builtins
import os
import logging
from contextvars import ContextVar
from threading import Barrier, Event, Lock

import pytest

from precis_mcp.analytical import get_analytical_client, close_analytical_resources
from precis_mcp.analytical.config import DatabricksConfig
from precis_mcp.analytical.contracts import ClientClosedError, ConfigurationError, DependencyError, ExecutionError, ReadContractError, StatementError
from precis_mcp.analytical.databricks import DatabricksClient
from tests.fakes.fake_analytical import FakeCursor, FakeDatabricksConnection, FakeNativeClient
from tests.fakes.fake_clickhouse import FakeQueryResult
from precis_mcp.analytical.resources import ClientPool, PooledQueries


def config(**kwargs):
    return DatabricksConfig("https://workspace.example", "/sql/1.0/warehouses/abc",
                            client_id="principal", client_secret="private-secret", **kwargs)


def test_native_execute_confirms_success_without_fetching_or_replaying(monkeypatch):
    connection = FakeDatabricksConnection(fetch_error=AssertionError("DML must not fetch rows"))
    monkeypatch.setattr("precis_mcp.analytical.databricks._connect", lambda _: connection)
    with DatabricksClient(config()) as client:
        params = {"amount": Decimal("123456.1234"), "missing": None}
        result = client.execute("INSERT INTO t VALUES (:amount, :missing)", params)
        assert result.query_id == "fake-query"
        assert result.result_rows == []
        assert connection.calls == [("INSERT INTO t VALUES (:amount, :missing)", params)]
    assert connection.closed and connection.cursors[0].closed


@pytest.mark.parametrize("state,outcome", [("CANCELLED", "cancelled"), ("RUNNING", "unknown"),
                                          ("CLOSED", "unknown"), (None, "unknown")])
def test_native_execute_never_reports_unconfirmed_completion(monkeypatch, state, outcome):
    connection = FakeDatabricksConnection(command_state=state)
    monkeypatch.setattr("precis_mcp.analytical.databricks._connect", lambda _: connection)
    with DatabricksClient(config()) as client, pytest.raises(StatementError) as error:
        client.execute("UPDATE t SET value = :v", {"v": 42})
    assert error.value.outcome == outcome
    assert error.value.statement_id == "fake-query"
    assert len(connection.calls) == 1 and connection.closed


def test_native_execute_preserves_unknown_outcome_and_recovers_only_on_new_request(monkeypatch):
    pytest.importorskip("databricks.sql.exc")
    failed = FakeDatabricksConnection(execute_error=OSError("private-secret"))
    healthy = FakeDatabricksConnection()
    connections = iter([failed, healthy])
    monkeypatch.setattr("precis_mcp.analytical.databricks._connect", lambda _: next(connections))
    with DatabricksClient(config()) as client:
        with pytest.raises(StatementError) as error:
            client.execute("INSERT INTO t VALUES (1)")
        assert error.value.outcome == "unknown" and "private-secret" not in str(error.value)
        assert len(failed.calls) == 1 and healthy.calls == []
        client.execute("INSERT INTO t VALUES (2)")
    assert failed.closed and healthy.closed


def test_pooled_basic_execution_reuses_only_successful_resources():
    calls = []
    native = FakeNativeClient(lambda sql, params: calls.append((sql, params)))
    pool = ClientPool(lambda: native, capacity=1)
    with_handle = PooledQueries(pool)
    with_handle.execute("UPDATE t SET v = :v", {"v": Decimal("1.20")})
    with_handle.execute("DELETE FROM t")
    assert len(calls) == 2 and native.close_calls == 0
    with_handle.close()
    pool.close()
    assert native.close_calls == 1


@pytest.mark.parametrize("name", ["databricks.sql.cloudfetch.downloader",
    "databricks.sql.cloudfetch.download_manager", "databricks.sql.common.unified_http_client",
    "urllib3.connectionpool", "urllib3.connection"])
@pytest.mark.parametrize("style", ["argument", "exception", "plain"])
def test_cloudfetch_diagnostics_keep_context_without_signed_download_urls(caplog, name, style):
    from precis_mcp.analytical.databricks import _protect_download_diagnostics
    _protect_download_diagnostics()
    logger = logging.getLogger(name)
    caplog.set_level(logging.WARNING, logger=logger.name)
    url = "https://storage.example/result?X-Databricks-Signature=private-download-capability"
    if style == "argument":
        logger.warning("Download slower than threshold: %s MB/s from %s", "0.01", url)
        assert "0.01 MB/s" in caplog.text
    elif style == "exception":
        try:
            raise ValueError(url)
        except ValueError:
            logger.exception("Download failed")
        assert "Download failed" in caplog.text and "ValueError" in caplog.text
    else:
        logger.warning("Download completed without a URL")
        assert "Download completed without a URL" in caplog.text
    assert url not in caplog.text and "private-download-capability" not in caplog.text
    if style != "plain":
        assert "[redacted download URL]" in caplog.text


def test_failed_cloudfetch_http_request_redacts_real_transport_retry_logs(monkeypatch, caplog):
    http = pytest.importorskip("databricks.sql.common.unified_http_client")
    from urllib3 import HTTPSConnectionPool
    from urllib3.exceptions import ProtocolError
    from urllib3.util.retry import Retry
    from precis_mcp.analytical.databricks import _protect_download_diagnostics
    from tests.fakes.fake_analytical import FakeDownloadConnection, FakeDownloadPoolManager
    _protect_download_diagnostics()
    pool = HTTPSConnectionPool("storage.example", retries=Retry(total=1, backoff_factor=0))
    monkeypatch.setattr(pool, "_get_conn", lambda **kwargs: FakeDownloadConnection())
    monkeypatch.setattr(pool, "_put_conn", lambda connection: None)
    attempts = []
    def failed_request(connection, method, url, **kwargs):
        attempts.append(url)
        raise ProtocolError("Download connection lost")
    monkeypatch.setattr(pool, "_make_request", failed_request)
    # Run the real connector HTTP context and urllib3 retry loop, with fake I/O.
    transport = http.UnifiedHttpClient.__new__(http.UnifiedHttpClient)
    monkeypatch.setattr(transport, "_prepare_headers", lambda headers: {})
    monkeypatch.setattr(transport, "_prepare_retry_policy", lambda: None)
    monkeypatch.setattr(transport, "_get_pool_manager_for_url", lambda url: FakeDownloadPoolManager(pool))
    caplog.set_level(logging.WARNING)
    url = "https://storage.example/result?X-Databricks-Signature=private-download-capability"
    with pytest.raises(http.RequestError):
        with transport.request_context(http.HttpMethod.GET, url):
            pytest.fail("Unexpected successful download")
    assert len(attempts) == 2
    assert "Retrying" in caplog.text and "HTTP request failed after retries" in caplog.text
    assert "private-download-capability" not in caplog.text
    assert "X-Databricks-Signature" not in caplog.text
    assert "/result?" not in caplog.text


def test_lazy_session_native_binding_precision_and_cursor_ownership(monkeypatch):
    connection = FakeDatabricksConnection()
    opened = []
    def connect(settings, **kwargs):
        opened.append(settings)
        return connection
    monkeypatch.setattr("precis_mcp.analytical.databricks._connect", connect)
    client = get_analytical_client("databricks", databricks_config=config())
    assert opened == []
    params = {"amount": Decimal("12.3400"), "label": "x'; DROP TABLE t; --"}
    with client:
        result = client.query("SELECT :amount, :label", params)
        client.query("SELECT ?", [None])
    assert result.column_names == ["amount", "missing"]
    assert result.column_types == ["decimal", "int"]
    assert result.result_rows == [(Decimal("12345678901234567890.1234"), None)]
    assert connection.calls[0] == ("SELECT :amount, :label", params)
    assert connection.calls[0][1] is params
    assert len(opened) == 1 and len(connection.cursors) == 2
    assert all(c.closed for c in connection.cursors) and not connection.closed
    close_analytical_resources()
    assert connection.closed
    client.close()
    with pytest.raises(ClientClosedError):
        client.query("SELECT 1")


@pytest.mark.parametrize("failure", ["execute_error", "fetch_error"])
def test_failed_reads_cleanup_and_never_replay(monkeypatch, failure):
    bad = FakeDatabricksConnection(**{failure: RuntimeError("private-secret")})
    healthy = FakeDatabricksConnection()
    connections = iter([bad, healthy])
    monkeypatch.setattr("precis_mcp.analytical.databricks._connect", lambda _: next(connections))
    with DatabricksClient(config()) as client:
        with pytest.raises(ExecutionError) as exc:
            client.query("SELECT 1")
        assert "private-secret" not in str(exc.value) and exc.value.__suppress_context__
        assert len(bad.calls) == 1 and bad.closed and bad.cursors[0].closed
        assert client.query("SELECT 2").result_rows
    assert healthy.closed


def test_concurrent_reads_get_distinct_closed_cursors(monkeypatch):
    connection = FakeDatabricksConnection()
    monkeypatch.setattr("precis_mcp.analytical.databricks._connect", lambda _: connection)
    with DatabricksClient(config()) as client:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda n: client.query("SELECT ?", [n]), range(8)))
    assert len(results) == len(connection.cursors) == 8
    assert all(c.closed for c in connection.cursors)


def test_read_contract_violation_is_safe_and_discards_failed_session(monkeypatch):
    connection = FakeDatabricksConnection(execute_error=RuntimeError(
        "private-secret PRECIS_READ_CONTRACT_VIOLATION raw customer data"))
    monkeypatch.setattr("precis_mcp.analytical.databricks._connect", lambda _: connection)
    with DatabricksClient(config()) as client:
        with pytest.raises(ReadContractError) as exc:
            client.query("SELECT invalid_input")
        assert str(exc.value) == "Queried values violate the canonical non-null input contract."
        assert exc.value.__suppress_context__
        assert connection.closed and connection.cursors[0].closed
        assert len(connection.calls) == 1


def test_legacy_ch_calls_cannot_reach_a_second_financial_store(monkeypatch):
    from precis_mcp.db import get_clickhouse_client
    monkeypatch.setenv("PRECIS_ANALYTICAL_BACKEND", "databricks")
    with pytest.raises(ConfigurationError, match="Direct ClickHouse access is disabled"):
        get_clickhouse_client()


def test_native_input_check_uses_existing_catalogue_without_a_contract_file(monkeypatch):
    from precis_mcp.analytical import runtime
    from precis_mcp.analytical.sql import SqlDialect
    from precis_mcp.engine.catalogue import Catalogue
    from tests.fakes.fake_clickhouse import FakeClickHouseClient
    client = FakeClickHouseClient()
    client.dialect = SqlDialect("databricks", "customer")
    client.push(column_names=[])
    client.push(("ACTUALS", "actuals", "Actuals", "ACTUAL"),
                column_names=["scenario_id", "alias", "name", "kind"])
    monkeypatch.setenv("PRECIS_ANALYTICAL_BACKEND", "databricks")
    monkeypatch.delenv("PRECIS_INGESTION_MODE", raising=False)
    monkeypatch.setattr(runtime, "get_analytical_client", lambda: client)
    monkeypatch.setattr("precis_mcp.db.get_clickhouse_client", lambda: pytest.fail("opened CH"))
    runtime.validate_runtime_inputs(Catalogue(metrics={}, scenarios={}, dimensions={}, statements={}, domains={}))
    assert len(client.queries) == 2 and client.close_calls == 1
    assert "LIMIT 0" in client.queries[0][0]
    assert client.commands == [] and client.inserts == []


def test_client_factory_needs_credentials_but_no_global_catalogue_receipt(monkeypatch):
    for key, value in {"PRECIS_ANALYTICAL_BACKEND":"databricks", "DATABRICKS_HOST":"https://workspace.example",
                       "DATABRICKS_HTTP_PATH":"/sql/1.0/warehouses/abc", "DATABRICKS_AUTH_TYPE":"oauth-m2m",
                       "DATABRICKS_CLIENT_ID":"principal", "DATABRICKS_CLIENT_SECRET":"private-secret"}.items():
        monkeypatch.setenv(key,value)
    with get_analytical_client() as client:
        assert isinstance(client, DatabricksClient)
        assert client._connection is None


def test_dimension_metadata_is_read_from_warehouse_and_cached_only_on_the_client(monkeypatch):
    from tests.fakes.fake_clickhouse import FakeClickHouseClient
    fake = FakeClickHouseClient()
    fake.push(("flag", "SMALLINT", "NO"), ("name", "STRING", "NO"),
              ("optional_name", "STRING", "YES"), column_names=["column_name", "full_data_type", "is_nullable"])
    client = DatabricksClient(config(catalog="customer"))
    monkeypatch.setattr(client, "query", fake.query)
    assert client.dialect.boolean(True, relation="semantic.dim", source_column="flag") == "1"
    assert "THEN ''" in client.dialect.joined_attribute("d.name", "semantic.dim", "name", joined_key="d.id")
    assert client.dialect.joined_attribute("d.optional_name", "semantic.dim", "optional_name") == "d.optional_name"
    assert len(fake.queries) == 1
    with pytest.raises(ConfigurationError, match="missing or inaccessible"):
        client._column_type("semantic.dim", "missing")
    client.close()
    assert not client._column_types


def test_close_before_connect_and_cancel_when_idle(monkeypatch):
    monkeypatch.setattr("precis_mcp.analytical.databricks._connect", lambda _: pytest.fail("opened"))
    client = DatabricksClient(config())
    assert client.cancel() is False
    client.close()
    with pytest.raises(ClientClosedError):
        client.query("SELECT 1")


def test_missing_optional_driver_has_actionable_safe_error(monkeypatch):
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name == "databricks":
            raise ImportError("private-secret")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded)
    with DatabricksClient(config()) as client:
        with pytest.raises(DependencyError, match="databricks-primary"):
            client.query("SELECT 1")


def test_native_connect_explicit_auth_and_timeout_settings(monkeypatch):
    sql = pytest.importorskip("databricks.sql")
    core = pytest.importorskip("databricks.sdk.core")
    seen = {}
    class Credentials:
        def __init__(self, **kwargs):
            seen["auth"] = kwargs
        def authenticate(self):
            return {"Authorization": "not-real"}
    def connect(**kwargs):
        seen["connection"] = kwargs
        assert callable(kwargs["credentials_provider"]())
        return FakeDatabricksConnection()
    monkeypatch.setattr(core, "Config", Credentials)
    monkeypatch.setattr(sql, "connect", connect)
    with DatabricksClient(config()) as client:
        client.query("SELECT 1")
    assert seen["auth"]["auth_type"] == "oauth-m2m"
    assert seen["auth"]["config_file"] == os.devnull
    assert seen["auth"]["scopes"] == ["sql"]
    assert seen["connection"]["use_inline_params"] is False
    assert seen["connection"]["_retry_stop_after_attempts_count"] == 1
    assert seen["connection"]["session_configuration"]["TIMEZONE"] == "UTC"
    assert "access_token" not in seen["connection"]


def test_connection_failure_is_redacted(monkeypatch):
    pytest.importorskip("databricks.sql")
    core = pytest.importorskip("databricks.sdk.core")
    def fail(**kwargs):
        raise ValueError("private-secret")
    monkeypatch.setattr(core, "Config", fail)
    with DatabricksClient(config()) as client:
        with pytest.raises(ExecutionError) as exc:
            client.query("SELECT 1")
    assert "private-secret" not in str(exc.value) and exc.value.__suppress_context__


def test_provider_selection_is_explicit_and_does_not_open_ch(monkeypatch):
    monkeypatch.delenv("PRECIS_ANALYTICAL_BACKEND", raising=False)
    from precis_mcp.analytical.clickhouse import ClickHouseClient
    with get_analytical_client() as client:
        assert isinstance(client, ClickHouseClient)
    with pytest.raises(ConfigurationError):
        get_analytical_client("unknown")
    with pytest.raises(ConfigurationError):
        get_analytical_client("clickhouse", databricks_config=config())


def test_cli_config_pins_workspace_and_explicit_auth(monkeypatch):
    sql = pytest.importorskip("databricks.sql")
    core = pytest.importorskip("databricks.sdk.core")
    seen = {}
    class Credentials:
        def __init__(self, **kwargs):
            seen.update(kwargs)
        def authenticate(self):
            return {}
    monkeypatch.setattr(core, "Config", Credentials)
    monkeypatch.setattr(sql, "connect", lambda **_: FakeDatabricksConnection())
    cli = DatabricksConfig("https://workspace.example", "/sql/1.0/warehouses/abc",
                           auth_type="databricks-cli", profile="local-dev")
    with DatabricksClient(cli) as client:
        client.query("SELECT 1")
    assert seen == {"host": "https://workspace.example", "auth_type": "databricks-cli", "profile": "local-dev"}


def test_shared_secret_loader_delivers_file_secret_to_factory(monkeypatch, tmp_path):
    from precis_mcp import secrets
    secret_file = tmp_path / "databricks-secret"
    secret_file.write_text("file-secret\n")
    monkeypatch.setenv("DATABRICKS_HOST", "https://workspace.example")
    monkeypatch.setenv("DATABRICKS_HTTP_PATH", "/sql/1.0/warehouses/abc")
    monkeypatch.setenv("DATABRICKS_AUTH_TYPE", "oauth-m2m")
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "principal")
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    monkeypatch.delenv("DATABRICKS_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET_FILE", str(secret_file))
    # This is the common process-start step, not a second driver-specific reader.
    secrets.resolve_secret_files()
    with get_analytical_client("databricks") as client:
        assert client.config.client_secret == "file-secret"
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "operator-override")
    secrets.resolve_secret_files()
    with get_analytical_client("databricks") as client:
        assert client.config.client_secret == "operator-override"


def test_native_temporal_values_are_not_stringified_or_reinterpreted(monkeypatch):
    connection = FakeDatabricksConnection()
    day = date(2024, 2, 29)
    instant = datetime(2026, 1, 2, tzinfo=timezone.utc)
    monkeypatch.setattr(FakeCursor, "fetchall", lambda _: [(day, instant)])
    monkeypatch.setattr("precis_mcp.analytical.databricks._connect", lambda _: connection)
    with DatabricksClient(config()) as client:
        row = client.query("SELECT :day, :instant", {"day": day, "instant": instant}).result_rows[0]
    assert row[0] is day and row[1] is instant


@pytest.mark.parametrize("stage", ["cursor", "description", "close"])
def test_partial_cursor_lifecycle_always_releases_session(monkeypatch, stage):
    connection = FakeDatabricksConnection()
    if stage == "cursor":
        def fail():
            raise RuntimeError("private-secret")
        monkeypatch.setattr(connection, "cursor", fail)
    elif stage == "description":
        original = connection.cursor
        def invalid():
            cursor = original()
            cursor.description = [("bad",)]
            return cursor
        monkeypatch.setattr(connection, "cursor", invalid)
    else:
        def fail_close(_):
            raise RuntimeError("private-secret")
        monkeypatch.setattr(FakeCursor, "close", fail_close)
    monkeypatch.setattr("precis_mcp.analytical.databricks._connect", lambda _: connection)
    with DatabricksClient(config()) as client:
        if stage == "close":
            assert client.query("SELECT 1").result_rows
        else:
            with pytest.raises(ExecutionError):
                client.query("SELECT 1")
        assert connection.closed


@pytest.mark.parametrize("failure", [False, True])
def test_active_read_cancellation_is_attempted_and_errors_are_safe(monkeypatch, failure):
    connection = FakeDatabricksConnection()
    monkeypatch.setattr("precis_mcp.analytical.databricks._connect", lambda _: connection)
    with DatabricksClient(config()) as client:
        def execute(cursor, sql, parameters=None):
            if failure:
                def fail_cancel():
                    raise RuntimeError("private-secret")
                cursor.cancel = fail_cancel
                with pytest.raises(ExecutionError) as exc:
                    client.cancel()
                assert "private-secret" not in str(exc.value)
            else:
                assert client.cancel() is True
                assert cursor.cancelled
        monkeypatch.setattr(FakeCursor, "execute", execute)
        assert client.query("SELECT 1").result_rows


def test_clickhouse_adapter_normalizes_results_and_closes(monkeypatch):
    from types import SimpleNamespace
    from precis_mcp import db
    connection = FakeDatabricksConnection()
    def query(sql, parameters):
        connection.calls.append((sql, parameters))
        return SimpleNamespace(column_names=["amount"], result_rows=[[Decimal("1.2300")]],
                               column_types=["Decimal(18,4)"], query_id="ch-query")
    connection.query = query
    monkeypatch.setattr(db, "get_clickhouse_client", lambda **kwargs: connection)
    with get_analytical_client("clickhouse") as client:
        result = client.query("SELECT {amount:Decimal(18,4)}", {"amount": Decimal("1.2300")})
        assert result.result_rows == [(Decimal("1.2300"),)]
        assert result.column_types == ["Decimal(18,4)"]
        from types import SimpleNamespace
        connection.query = lambda *args, **kwargs: SimpleNamespace(
            column_names=["amount"], result_rows=[], column_types=[SimpleNamespace(name="Decimal(18,4)")])
        assert client.query("SELECT 1").column_types == ["Decimal(18,4)"]
        with pytest.raises(ConfigurationError):
            client.query("SELECT ?", [1])
    assert not connection.closed
    close_analytical_resources()
    assert connection.closed
    with pytest.raises(ClientClosedError):
        client.query("SELECT 1")


def test_sdk_authentication_is_shared_across_three_native_sessions(monkeypatch):
    sql = pytest.importorskip("databricks.sql")
    core = pytest.importorskip("databricks.sdk.core")
    configurations = []
    sessions = []
    barrier = Barrier(3)

    class Credentials:
        def __init__(self, **kwargs):
            configurations.append(kwargs)

        def authenticate(self):
            return {"Authorization": "fake-renewable-credential"}

    def connect(**kwargs):
        assert kwargs["credentials_provider"]()() == {"Authorization": "fake-renewable-credential"}
        connection = FakeDatabricksConnection(query_handler=lambda *_: (
            barrier.wait(timeout=3), FakeQueryResult([(1,)], ["value"]),
        )[1])
        sessions.append(connection)
        return connection

    monkeypatch.setattr(core, "Config", Credentials)
    monkeypatch.setattr(sql, "connect", connect)
    with get_analytical_client("databricks", databricks_config=config()) as client:
        with ThreadPoolExecutor(max_workers=3) as workers:
            results = list(workers.map(lambda _: client.query("SELECT 1"), range(3)))
    assert len(results) == len(sessions) == 3
    assert len(configurations) == 1
    assert not any(s.closed for s in sessions)
    close_analytical_resources()
    assert all(s.closed for s in sessions)


def test_reused_session_does_not_extend_request_column_metadata_cache(monkeypatch):
    metadata_calls = []

    def query(sql, parameters):
        metadata_calls.append(parameters)
        kind = "SMALLINT" if len(metadata_calls) == 1 else "BOOLEAN"
        return FakeQueryResult([("flag", kind, "NO")], ["column_name", "full_data_type", "is_nullable"])

    connection = FakeDatabricksConnection(query_handler=query)
    opened = []

    def connect(*args, **kwargs):
        opened.append(1)
        return connection

    monkeypatch.setattr("precis_mcp.analytical.databricks._connect", connect)
    for expected in ["1", "TRUE"]:
        with get_analytical_client("databricks", databricks_config=config(catalog="customer")) as client:
            for _ in range(2):
                assert client.dialect.boolean(True, relation="semantic.fact", source_column="flag") == expected
    assert len(opened) == 1 and len(metadata_calls) == 2


@pytest.mark.parametrize("backend", ["clickhouse", "databricks"])
def test_pool_limits_all_reports_and_preserves_query_order_and_context(backend):
    trace = pytest.importorskip("opentelemetry.trace")
    from precis_mcp.analytical.clickhouse import ClickHouseClient
    from precis_mcp.engine.retriever import execute_queries
    context = ContextVar("report_context")
    barrier = Barrier(3)
    lock = Lock()
    running = 0
    peak = 0
    resources = []
    seen = []

    def query(sql, parameters):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
            seen.append((context.get(), parameters["report"], trace.get_current_span().get_span_context().span_id))
        try:
            barrier.wait(timeout=3)
            return FakeQueryResult([(parameters["value"],)], ["value"])
        finally:
            with lock:
                running -= 1

    def factory():
        resource = FakeNativeClient(query)
        resources.append(resource)
        return resource

    pool = ClientPool(factory)

    def report(number):
        context.set(number)
        execution = PooledQueries(pool)
        client = (DatabricksClient(config(), execution=execution) if backend == "databricks"
                  else ClickHouseClient(execution=execution))
        with client:
            with trace.use_span(trace.NonRecordingSpan(trace.SpanContext(
                trace_id=number, span_id=number, is_remote=False,
            ))):
                return execute_queries([
                    ("query", {"value": i, "report": number}) for i in range(3)
                ], client)

    try:
        with ThreadPoolExecutor(max_workers=2) as reports:
            outputs = list(reports.map(report, [1, 2]))
        assert outputs == [[[{"value": i}] for i in range(3)]] * 2
        assert peak == len(resources) == 3
        assert all(actual == expected == span for actual, expected, span in seen)
    finally:
        pool.close()
    assert all(resource.close_calls == 1 for resource in resources)


def test_failed_parallel_report_drains_and_does_not_cancel_next_borrower():
    from precis_mcp.analytical.clickhouse import ClickHouseClient
    from precis_mcp.engine.retriever import execute_queries
    started = Barrier(2)
    released = Event()
    resources = []

    def query(sql, parameters):
        if sql == "next":
            return FakeQueryResult([(2,)], ["value"])
        started.wait(timeout=3)
        if sql == "fail":
            raise ExecutionError("Failed required query")
        assert released.wait(timeout=3)
        return FakeQueryResult([(1,)], ["value"])

    def factory():
        resource = FakeNativeClient(query, cancel_handler=released.set)
        resources.append(resource)
        return resource

    pool = ClientPool(factory)
    try:
        with ClickHouseClient(execution=PooledQueries(pool)) as client:
            with pytest.raises(ExecutionError, match="required query"):
                execute_queries([("fail", {}), ("slow", {})], client)
            cancellations = sum(r.cancel_calls for r in resources)
            assert client.query("next").result_rows == [(2,)]
            assert sum(r.cancel_calls for r in resources) == cancellations
        assert any(r.close_calls == 1 for r in resources)
    finally:
        pool.close()


def test_pool_wait_failure_idle_expiry_and_shutdown(monkeypatch):
    import precis_mcp.analytical.resources as module
    clock = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    resources = []

    def factory():
        client = FakeNativeClient(lambda *_: FakeQueryResult([(0,)], ["value"]))
        resources.append(client)
        return client

    pool = ClientPool(factory, capacity=1, wait_timeout=0, idle_timeout=5)
    resource = pool.acquire()
    handle = PooledQueries(pool)
    with pytest.raises(ExecutionError, match="busy"):
        handle.query("blocked")
    pool.release(resource)
    assert handle.query("reuse").result_rows == [(0,)]
    assert len(resources) == 1
    clock[0] = 6
    assert handle.query("expired").result_rows == [(0,)]
    assert len(resources) == 2 and resources[0].close_calls == 1
    borrowed = pool.acquire()
    pool.close()
    assert borrowed.close_calls == 0
    pool.release(borrowed)
    assert borrowed.close_calls == 1
    with pytest.raises(ClientClosedError):
        handle.query("shutdown")
    handle.close()


def test_request_close_waits_for_its_query_without_returning_connection_early():
    entered, released = Event(), Event()
    resource = FakeNativeClient(lambda *_: (
        entered.set(), released.wait(timeout=3), FakeQueryResult([(1,)], ["value"]),
    )[-1])
    pool = ClientPool(lambda: resource, capacity=1, wait_timeout=0)
    handle = PooledQueries(pool)
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            query = workers.submit(handle.query, "running")
            assert entered.wait(timeout=3)
            closed = workers.submit(handle.close)
            with handle._condition:
                assert handle._condition.wait_for(lambda: handle._closed, timeout=3)
            assert not closed.done()
            with pytest.raises(ExecutionError, match="busy"):
                pool.acquire()
            released.set()
            assert query.result(timeout=3).result_rows == [(1,)]
            closed.result(timeout=3)
        with pytest.raises(ClientClosedError):
            handle.query("closed")
    finally:
        released.set()
        pool.close()


def test_delayed_ch_connection_retains_original_identity_and_tls_snapshot(monkeypatch):
    from precis_mcp import db
    from tests.fakes.fake_clickhouse import FakeClickHouseClient
    options = []
    monkeypatch.setenv("CHSECURE", "true")
    monkeypatch.setenv("CHVERIFY", "true")
    monkeypatch.delenv("CHCACERT", raising=False)
    monkeypatch.setenv("CHUSER", "original")
    original = get_analytical_client("clickhouse")
    monkeypatch.setenv("CHVERIFY", "false")
    monkeypatch.setenv("CHCACERT", "/different/trust.pem")
    monkeypatch.setenv("CHUSER", "changed")

    def connect(**kwargs):
        options.append(kwargs)
        return FakeClickHouseClient()

    monkeypatch.setattr(db, "get_clickhouse_client", connect)
    with original:
        original.query("SELECT 1")
    with get_analytical_client("clickhouse") as changed:
        changed.query("SELECT 1")
    assert options[0]["username"] == "original" and options[0]["verify"] is True
    assert options[0]["ca_cert"] is None
    assert options[1]["username"] == "changed" and options[1]["verify"] is False
    assert options[1]["ca_cert"] == "/different/trust.pem"


def test_failed_pooled_session_is_discarded_without_replaying_sql(monkeypatch):
    failed = FakeDatabricksConnection(execute_error=RuntimeError("private-secret"))
    healthy = FakeDatabricksConnection()
    connections = iter([failed, healthy])
    monkeypatch.setattr("precis_mcp.analytical.databricks._connect", lambda *a, **k: next(connections))
    with get_analytical_client("databricks", databricks_config=config()) as client:
        with pytest.raises(ExecutionError) as exc:
            client.query("first attempt")
        assert "private-secret" not in str(exc.value)
    assert failed.closed and len(failed.calls) == 1
    with get_analytical_client("databricks", databricks_config=config()) as client:
        assert client.query("explicit next read").result_rows
    assert healthy.calls == [("explicit next read", None)]


def test_factory_failure_restores_pool_capacity_for_an_explicit_next_read():
    attempts = []

    def factory():
        attempts.append(1)
        if len(attempts) == 1:
            raise ExecutionError("Could not open session")
        return FakeNativeClient(lambda *_: FakeQueryResult([(1,)], ["value"]))

    pool = ClientPool(factory, capacity=1, wait_timeout=0)
    try:
        with pytest.raises(ExecutionError, match="session"):
            PooledQueries(pool).query("first")
        assert len(attempts) == 1
        assert PooledQueries(pool).query("next").result_rows == [(1,)]
    finally:
        pool.close()


def test_cancellation_of_completed_request_cannot_reach_next_connection_borrower():
    entered, released = Event(), Event()

    def query(sql, parameters):
        if sql == "second":
            entered.set()
            assert released.wait(timeout=3)
        return FakeQueryResult([(1,)], ["value"])

    resource = FakeNativeClient(query, cancel_handler=released.set)
    pool = ClientPool(lambda: resource, capacity=1)
    first, second = PooledQueries(pool), PooledQueries(pool)
    try:
        first.query("first")
        with ThreadPoolExecutor(max_workers=1) as worker:
            pending = worker.submit(second.query, "second")
            assert entered.wait(timeout=3)
            assert first.cancel() is False
            assert resource.cancel_calls == 0 and not pending.done()
            released.set()
            pending.result(timeout=3)
    finally:
        released.set()
        first.close()
        second.close()
        pool.close()


def test_parallel_submission_shutdown_drains_only_started_work(monkeypatch):
    from concurrent.futures import wait
    from precis_mcp.engine import retriever
    from precis_mcp.analytical import resources
    from tests.fakes.fake_analytical import FakeStoppingExecutor
    executor = FakeStoppingExecutor()
    acquisitions = []
    drained = []

    def get_executor():
        acquisitions.append(1)
        return executor

    def drain(futures):
        assert all(not future.cancelled() for future in futures)
        drained.extend(futures)
        return wait(futures, timeout=1)

    client = FakeNativeClient(lambda *_: pytest.fail("unexpected execution"))
    client.supports_parallel_queries = True
    monkeypatch.setattr(resources, "query_executor", get_executor)
    monkeypatch.setattr(retriever, "wait", drain)
    with pytest.raises(RuntimeError, match="stopping"):
        retriever.execute_queries([("one", {}), ("two", {}), ("three", {})], client)
    assert len(acquisitions) == 1
    assert drained == executor.futures[:1]
