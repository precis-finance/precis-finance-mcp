# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Unit tests for the shared Ibis backend factory (ingestion/ibis_backends.py).

The kwargs resolvers are pure (env -> dict), so every kind is testable without
its driver installed. The driver-bound connect call is covered for postgres in
tests/component/test_connection_tls.py; here we assert per-kind kwargs, the
missing-var error, the unknown-kind error, and the optional-extra message.
"""
from __future__ import annotations

import sys

import pytest

from precis_mcp.ingestion.ibis_backends import build_ibis_backend, resolve_connect_kwargs
from precis_mcp.ingestion.registry import IntegrationConfigError, Source
from tests.factories.ingestion import make_source


def _source(kind: str, source_id: str = "wh") -> Source:
    # secret_ref == source_id, so the env-var prefix is the uppercased id.
    return Source(**make_source(source_id=source_id, kind=kind))


def _setenv(monkeypatch, prefix: str, **vals: str) -> None:
    for suffix, value in vals.items():
        monkeypatch.setenv(f"{prefix}_{suffix}", value)


def _clearenv(monkeypatch, prefix: str, *suffixes: str) -> None:
    for suffix in suffixes:
        monkeypatch.delenv(f"{prefix}_{suffix}", raising=False)


# --- postgres ---------------------------------------------------------------


def test_postgres_kwargs_defaults(monkeypatch):
    _clearenv(monkeypatch, "WH", "PASSWORD", "PORT", "SSLMODE", "SSLROOTCERT")
    _setenv(monkeypatch, "WH", HOST="h", USER="u", DATABASE="d")
    kw = resolve_connect_kwargs(_source("postgres"))
    assert kw["host"] == "h"
    assert kw["user"] == "u"
    assert kw["database"] == "d"
    assert kw["password"] == ""
    assert kw["port"] == 5432
    assert "sslmode" not in kw


def test_postgres_kwargs_tls_and_port(monkeypatch):
    _setenv(monkeypatch, "WH", HOST="h", USER="u", DATABASE="d", PASSWORD="p",
            PORT="6543", SSLMODE="verify-full", SSLROOTCERT="/ca.pem")
    kw = resolve_connect_kwargs(_source("postgres"))
    assert kw["port"] == 6543
    assert kw["password"] == "p"
    assert kw["sslmode"] == "verify-full"
    assert kw["sslrootcert"] == "/ca.pem"


# --- mssql ------------------------------------------------------------------


def test_mssql_default_port_and_driver(monkeypatch):
    _clearenv(monkeypatch, "WH", "PORT", "DRIVER")
    _setenv(monkeypatch, "WH", HOST="h", USER="u", DATABASE="d")
    kw = resolve_connect_kwargs(_source("mssql"))
    assert kw["port"] == 1433
    assert kw["driver"] == "ODBC Driver 18 for SQL Server"


def test_mssql_driver_override(monkeypatch):
    _setenv(monkeypatch, "WH", HOST="h", USER="u", DATABASE="d", DRIVER="FreeTDS")
    assert resolve_connect_kwargs(_source("mssql"))["driver"] == "FreeTDS"


# --- snowflake --------------------------------------------------------------


def test_snowflake_kwargs(monkeypatch):
    _clearenv(monkeypatch, "WH", "SCHEMA")
    _setenv(monkeypatch, "WH", ACCOUNT="ac", USER="u", DATABASE="d",
            WAREHOUSE="wh", ROLE="ANALYST")
    kw = resolve_connect_kwargs(_source("snowflake"))
    assert kw["account"] == "ac"
    assert kw["warehouse"] == "wh"
    assert kw["role"] == "ANALYST"
    assert "schema" not in kw  # optional, unset


# --- bigquery ---------------------------------------------------------------


def test_bigquery_kwargs(monkeypatch):
    _clearenv(monkeypatch, "WH", "CREDENTIALS_JSON")
    _setenv(monkeypatch, "WH", PROJECT_ID="proj", DATASET_ID="ds",
            CREDENTIALS_JSON='{"type": "service_account"}')
    kw = resolve_connect_kwargs(_source("bigquery"))
    assert kw["project_id"] == "proj"
    assert kw["dataset_id"] == "ds"
    # The SA-key JSON is a value secret carried as-is; the credentials object is
    # built at connect time via from_service_account_info (google import behind
    # the bigquery extra). It rides the same env / *_FILE seam as every secret.
    assert kw["credentials_json"] == '{"type": "service_account"}'
    assert "credentials" not in kw


def test_bigquery_credentials_optional(monkeypatch):
    _clearenv(monkeypatch, "WH", "DATASET_ID", "CREDENTIALS_JSON")
    _setenv(monkeypatch, "WH", PROJECT_ID="proj")
    kw = resolve_connect_kwargs(_source("bigquery"))
    assert kw == {"project_id": "proj"}


# --- databricks -------------------------------------------------------------


def test_databricks_kwargs(monkeypatch):
    _clearenv(monkeypatch, "WH", "SCHEMA")
    _setenv(monkeypatch, "WH", SERVER_HOSTNAME="h", HTTP_PATH="/sql/1",
            ACCESS_TOKEN="tok", CATALOG="main")
    kw = resolve_connect_kwargs(_source("databricks"))
    assert kw["server_hostname"] == "h"
    assert kw["http_path"] == "/sql/1"
    assert kw["access_token"] == "tok"
    assert kw["catalog"] == "main"
    assert "schema" not in kw


# --- http_upload ------------------------------------------------------------


def test_http_upload_needs_no_env():
    assert resolve_connect_kwargs(_source("http_upload")) == {}


# --- errors -----------------------------------------------------------------


def test_missing_required_env_lists_all(monkeypatch):
    _clearenv(monkeypatch, "WH", "USER", "DATABASE", "PASSWORD", "PORT")
    _setenv(monkeypatch, "WH", HOST="h")  # USER + DATABASE missing
    with pytest.raises(IntegrationConfigError) as exc:
        resolve_connect_kwargs(_source("postgres"))
    assert "WH_USER" in str(exc.value)
    assert "WH_DATABASE" in str(exc.value)


def test_unknown_kind_lists_supported():
    with pytest.raises(IntegrationConfigError) as exc:
        resolve_connect_kwargs(_source("oracle"))
    msg = str(exc.value)
    assert "postgres" in msg and "snowflake" in msg


def test_missing_extra_names_the_extra(monkeypatch):
    _setenv(monkeypatch, "WH", ACCOUNT="ac", USER="u", DATABASE="d", WAREHOUSE="wh")

    class _NoDriver:
        def __getattr__(self, name):
            raise ModuleNotFoundError(f"No module named '{name}'")

    # build_ibis_backend does `import ibis` internally; swap it for a stub
    # whose backend access fails, simulating the extra not being installed.
    monkeypatch.setitem(sys.modules, "ibis", _NoDriver())
    with pytest.raises(IntegrationConfigError) as exc:
        build_ibis_backend(_source("snowflake"))
    assert "precis-mcp[snowflake]" in str(exc.value)
