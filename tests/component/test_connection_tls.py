# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""TLS hardening of the outbound DB connections (federated scope).

All four wire-protocol connection sites take opt-in TLS so a remote / BYO-cloud
target is never reached in plaintext, while a co-located deploy is byte-identical
to before (no TLS kwargs emitted until an operator sets the env var):

  - platform Postgres   `precis_mcp.db.get_platform_db`        — PGSSLMODE
  - ClickHouse          `precis_mcp.db.get_clickhouse_client`  — CHSECURE
  - ingestion + federated read  `ingestion.ibis_backends.build_ibis_backend`  — <REF>_SSLMODE
    (one shared factory; both paths construct their connection through it)

The tests patch the driver entrypoints with a recorder and assert the kwargs the
helpers construct — they verify connection *parameters*, not DB behaviour.
"""
from __future__ import annotations

import pytest

from precis_mcp import db
from precis_mcp.ingestion.registry import Source
from precis_mcp.ingestion.ibis_backends import build_ibis_backend
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
    # Complete pool snapshots now pass the driver's plaintext default explicitly.
    assert rec.kwargs.get("secure", False) is False
    assert rec.kwargs["port"] == 8123


def test_clickhouse_secure_flips_port_and_flag(monkeypatch):
    monkeypatch.setenv("CHSECURE", "true")
    monkeypatch.delenv("CHPORT", raising=False)
    rec = _Recorder()
    monkeypatch.setattr(db.clickhouse_connect, "get_client", rec)
    db.get_clickhouse_client()
    assert rec.kwargs["secure"] is True
    assert rec.kwargs["port"] == 8443  # HTTPS port by default when secure


@pytest.mark.parametrize("secure,port", [("true", 8443), ("false", 8123)])
def test_clickhouse_empty_compose_port_uses_protocol_default(monkeypatch, secure, port):
    monkeypatch.setenv("CHSECURE", secure)
    monkeypatch.setenv("CHPORT", "")
    rec = _Recorder()
    monkeypatch.setattr(db.clickhouse_connect, "get_client", rec)
    db.get_clickhouse_client()
    assert rec.kwargs["port"] == port


def test_ingestion_destination_uses_verified_clickhouse_tls(monkeypatch):
    from precis_mcp.ingestion.wiring import _default_ch_client

    monkeypatch.setenv("CHSECURE", "true")
    monkeypatch.setenv("CHVERIFY", "true")
    monkeypatch.setenv("CHPORT", "")
    monkeypatch.setenv("CHCACERT", "/etc/ssl/ch-ca.pem")
    monkeypatch.setenv("CHDATABASE", "actuals")
    rec = _Recorder()
    monkeypatch.setattr(db.clickhouse_connect, "get_client", rec)
    _default_ch_client()
    assert rec.kwargs["secure"] is True
    assert rec.kwargs["verify"] is True
    assert rec.kwargs["port"] == 8443
    assert rec.kwargs["ca_cert"] == "/etc/ssl/ch-ca.pem"
    assert rec.kwargs["database"] == "actuals"


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
    assert rec.kwargs["pool_mgr"].connection_pool_kw["ca_certs"] == "/etc/ssl/ch-ca.pem"
    assert rec.kwargs["pool_mgr"].connection_pool_kw["cert_reqs"] == "CERT_NONE"


def test_clickhouse_shared_pool_preserves_trust_and_isolates_policies(monkeypatch):
    monkeypatch.setenv("CHSECURE", "true")
    monkeypatch.setenv("CHVERIFY", "true")
    monkeypatch.setenv("CHCACERT", "/etc/ssl/private-ca.pem")
    rec = _Recorder()
    monkeypatch.setattr(db.clickhouse_connect, "get_client", rec)
    db.get_clickhouse_client()
    trusted = rec.kwargs["pool_mgr"]
    assert trusted.connection_pool_kw["ca_certs"] == "/etc/ssl/private-ca.pem"
    assert trusted.connection_pool_kw["cert_reqs"] == "CERT_REQUIRED"
    db.get_clickhouse_client()
    assert rec.kwargs["pool_mgr"] is trusted
    monkeypatch.setenv("CHVERIFY", "false")
    db.get_clickhouse_client()
    assert rec.kwargs["pool_mgr"] is not trusted
    assert rec.kwargs["pool_mgr"].connection_pool_kw["cert_reqs"] == "CERT_NONE"


def test_clickhouse_special_tls_options_are_left_to_driver(monkeypatch):
    monkeypatch.setenv("CHSECURE", "true")
    rec = _Recorder()
    monkeypatch.setattr(db.clickhouse_connect, "get_client", rec)
    db.get_clickhouse_client(server_host_name="warehouse.internal")
    assert "pool_mgr" not in rec.kwargs


# --- ingestion + federated read, one shared factory (<REF>_SSLMODE) ---------
# Both paths now construct their connection through ibis_backends.build_ibis_backend,
# so threading TLS through that single factory covers both wire-sites at once.


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


def test_shared_factory_postgres_no_ssl_by_default(monkeypatch, _pg_source_env):
    rec = _Recorder()
    monkeypatch.setattr("ibis.postgres.connect", rec)
    build_ibis_backend(_pg_source_env)
    assert "sslmode" not in rec.kwargs
    assert rec.kwargs["host"] == "warehouse.internal"


def test_shared_factory_postgres_threads_per_source_tls(monkeypatch, _pg_source_env):
    monkeypatch.setenv("TEST_PG_SSLMODE", "verify-full")
    monkeypatch.setenv("TEST_PG_SSLROOTCERT", "/etc/ssl/wh-ca.pem")
    rec = _Recorder()
    monkeypatch.setattr("ibis.postgres.connect", rec)
    build_ibis_backend(_pg_source_env)
    assert rec.kwargs["sslmode"] == "verify-full"
    assert rec.kwargs["sslrootcert"] == "/etc/ssl/wh-ca.pem"
