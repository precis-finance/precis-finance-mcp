# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""TLS hardening of the outbound DB connections (audit H-18 + federated scope).

All four wire-protocol connection sites take opt-in TLS so a remote / BYO-cloud
target is never reached in plaintext, while a co-located deploy is byte-identical
to before (no TLS kwargs emitted until an operator sets the env var):

  - platform Postgres   `precis_mcp.db.get_platform_db`        — PGSSLMODE
  - ClickHouse          `precis_mcp.db.get_clickhouse_client`  — CHSECURE
  - ingestion read      `wiring._build_ibis_backend`           — <REF>_SSLMODE
  - federated read      `ibis_registry._connect_postgres`      — <REF>_SSLMODE

The tests patch the driver entrypoints with a recorder and assert the kwargs the
helpers construct — they verify connection *parameters*, not DB behaviour.
"""
from __future__ import annotations

import pytest

from precis_mcp import db
from precis_mcp.ingestion.registry import Source
from precis_mcp.ingestion.wiring import _build_ibis_backend
from precis_mcp.engine.ibis_registry import _connect_postgres
from tests.factories.ingestion import make_source


class _Recorder:
    """Capture the kwargs a connect call is made with; return a sentinel."""

    def __init__(self):
        self.kwargs: dict = {}

    def __call__(self, *args, **kwargs):
        self.kwargs = kwargs
        return object()


# --- platform Postgres (db.get_platform_db) ---------------------------------


def test_platform_db_no_ssl_by_default(monkeypatch):
    monkeypatch.delenv("PGSSLMODE", raising=False)
    conninfo = db._platform_conninfo()
    assert "sslmode" not in conninfo
    assert "sslrootcert" not in conninfo


def test_platform_db_threads_sslmode_and_ca(monkeypatch):
    monkeypatch.setenv("PGSSLMODE", "verify-full")
    monkeypatch.setenv("PGSSLROOTCERT", "/etc/ssl/pg-ca.pem")
    conninfo = db._platform_conninfo()
    assert "sslmode=verify-full" in conninfo
    assert "sslrootcert=/etc/ssl/pg-ca.pem" in conninfo


def test_platform_db_sslmode_without_ca(monkeypatch):
    monkeypatch.setenv("PGSSLMODE", "require")
    monkeypatch.delenv("PGSSLROOTCERT", raising=False)
    conninfo = db._platform_conninfo()
    assert "sslmode=require" in conninfo
    assert "sslrootcert" not in conninfo


# --- ClickHouse (db.get_clickhouse_client) ----------------------------------


def test_clickhouse_plaintext_by_default(monkeypatch):
    monkeypatch.delenv("CHSECURE", raising=False)
    monkeypatch.delenv("CHPORT", raising=False)
    rec = _Recorder()
    monkeypatch.setattr(db.clickhouse_connect, "get_client", rec)
    db.get_clickhouse_client()
    assert "secure" not in rec.kwargs
    assert rec.kwargs["port"] == 8123


def test_clickhouse_secure_flips_port_and_flag(monkeypatch):
    monkeypatch.setenv("CHSECURE", "true")
    monkeypatch.delenv("CHPORT", raising=False)
    rec = _Recorder()
    monkeypatch.setattr(db.clickhouse_connect, "get_client", rec)
    db.get_clickhouse_client()
    assert rec.kwargs["secure"] is True
    assert rec.kwargs["port"] == 8443  # HTTPS port by default when secure


def test_clickhouse_secure_respects_explicit_port_and_ca(monkeypatch):
    monkeypatch.setenv("CHSECURE", "1")
    monkeypatch.setenv("CHPORT", "9440")
    monkeypatch.setenv("CHCACERT", "/etc/ssl/ch-ca.pem")
    monkeypatch.setenv("CHVERIFY", "false")
    rec = _Recorder()
    monkeypatch.setattr(db.clickhouse_connect, "get_client", rec)
    db.get_clickhouse_client()
    assert rec.kwargs["port"] == 9440
    assert rec.kwargs["ca_cert"] == "/etc/ssl/ch-ca.pem"
    assert rec.kwargs["verify"] is False


# --- ingestion + federated Postgres (per-source <REF>_SSLMODE) --------------


@pytest.fixture
def _pg_source_env(monkeypatch):
    """A postgres Source whose `secret_ref` is `test_pg` → env prefix TEST_PG."""
    for suffix in ("HOST", "USER", "DATABASE", "PORT", "SSLMODE", "SSLROOTCERT"):
        monkeypatch.delenv(f"TEST_PG_{suffix}", raising=False)
    monkeypatch.setenv("TEST_PG_HOST", "warehouse.internal")
    monkeypatch.setenv("TEST_PG_USER", "reader")
    monkeypatch.setenv("TEST_PG_DATABASE", "gl")
    monkeypatch.setenv("TEST_PG_PASSWORD", "s3cret")
    return Source(**make_source(source_id="test_pg", kind="postgres"))


@pytest.mark.parametrize("connect_fn", [_build_ibis_backend, _connect_postgres])
def test_federated_postgres_no_ssl_by_default(monkeypatch, _pg_source_env, connect_fn):
    rec = _Recorder()
    monkeypatch.setattr("ibis.postgres.connect", rec)
    connect_fn(_pg_source_env)
    assert "sslmode" not in rec.kwargs
    assert rec.kwargs["host"] == "warehouse.internal"


@pytest.mark.parametrize("connect_fn", [_build_ibis_backend, _connect_postgres])
def test_federated_postgres_threads_per_source_tls(monkeypatch, _pg_source_env, connect_fn):
    monkeypatch.setenv("TEST_PG_SSLMODE", "verify-full")
    monkeypatch.setenv("TEST_PG_SSLROOTCERT", "/etc/ssl/wh-ca.pem")
    rec = _Recorder()
    monkeypatch.setattr("ibis.postgres.connect", rec)
    connect_fn(_pg_source_env)
    assert rec.kwargs["sslmode"] == "verify-full"
    assert rec.kwargs["sslrootcert"] == "/etc/ssl/wh-ca.pem"
